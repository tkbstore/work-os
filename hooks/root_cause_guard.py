#!/usr/bin/env python3
"""Claude Code PreToolUse hook — 症状を消すだけの変更に、理由を要求する。

根本原因を直したかどうかを機械は判定できない。判定できるのは、
**原因に触れずに症状だけを消す操作には決まった形がある**ということだけである。
その形は registry/patch_smells.toml に宣言する。ここは形を知らない。

止めるのではなく、**同じ場所に理由を書かせる**。抑制が正しい場面は実在するので
（生成コード・外部由来の警告・意図的に壊した fixture）、禁止にすると嘘の回避が増える。
理由が書けないなら、それは症状を消しているだけである可能性が高い。

work.toml は要らない。どのリポジトリでも同じように効く。
逃げ道: 環境変数 WORKOS_ROOT_CAUSE=off で無効、=warn で stderr に出すだけにする。

外部依存なし。
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))

try:
    from workos import _load_toml, registry_path
except Exception as exc:                           # noqa: BLE001 — hook は落ちない
    # 落ちないことと、黙ることは別である。engine/ が隣に無ければこの hook は
    # 一切検査しないので、そのことを言ってから抜ける。実測 2026-08-31: この
    # ファイルだけを別の場所へ複製して動かしたとき、import が失敗して無言の
    # exit 0 になり、「検査して通った」と見分けがつかなかった（宣言が読めない
    # 場合と同じ形。あちらは 205 行目で塞いだ）。
    print(f"[work-os] root_cause_guard は何も検査していません。"
          f"engine/ を読み込めません: {exc}", file=sys.stderr)
    raise SystemExit(0)

SMELLS = registry_path("patch_smells.toml")
WRITE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}

# 理由とみなす最小の長さ。「# fix」「# ok」を理由と呼ばせないための下限。
MIN_REASON = 12
COMMENT = re.compile(r'(?://|#|/\*|<!--|--)\s*(.+?)\s*(?:\*/|-->)?$')

GROUP_ASK = {
    "suppression": "この抑制が正しい理由（生成コードである・外部由来である等）",
    "disabled_test": "このテストを止めてよい理由（何が置き換わったか）",
    "widening": "列挙を1つ増やすのではなく、なぜ一般化できないのか",
    "threshold": "観測を変えずに合格線を動かしてよい理由",
}


def _declared() -> dict:
    """痕跡の宣言。読めなければ空を返す（呼び側が「読めなかった」を扱う）。

    空を「痕跡ゼロ」と解釈してはいけない。宣言が読めないとき、照合対象が無い
    ので **すべてのパッチが黙って通る**。実測 2026-08-31: registry を別リポへ
    切り出した直後、WORKOS_REGISTRY を持たない環境で `# type: ignore` の追加が
    警告も出さずに通った（registry がある環境では deny）。宣言と同居していた
    頃はこの経路に入りようが無かったので、切り出しが作った穴である。

    sweep_guard と degradation の向きが逆な点に注意。あちらは記録を諦めて判定を
    続ける。こちらは判定そのものを宣言に預けているので、諦めると何も見なくなる。
    """
    try:
        return _load_toml(SMELLS)
    except Exception as exc:                       # noqa: BLE001
        # 言うのはここ。読めなかったことを知っているのはこの関数だけで、
        # 空の dict を返した先では「宣言がゼロ」と区別がつかない。
        print(f"[work-os] root_cause_guard は何も検査していません。"
              f"痕跡の宣言を読めません: {SMELLS} ({type(exc).__name__})\n"
              f"  registry の場所を教えるか（WORKOS_REGISTRY=<path>）、"
              f"この検査を切ってください（WORKOS_ROOT_CAUSE=off）", file=sys.stderr)
        return {}


def _compile(section: dict) -> list[tuple[str, str, re.Pattern]]:
    out = []
    for group, body in (section or {}).items():
        if not isinstance(body, dict):
            continue
        for name, raw in body.items():
            try:
                out.append((group, name, re.compile(str(raw))))
            except re.error:
                continue
    return out


def _existing(file_path: object) -> set[str]:
    """上書き前の中身。読めなければ「何も無かった」とみなす。

    読めない理由（新規作成・権限・バイナリ）はどれも「この操作で新しく現れる行を
    知りたい」という目的にとって同じ意味を持つ。区別する必要が無いので区別しない。
    """
    try:
        path = Path(str(file_path or "")).expanduser()
        if path.is_file():
            return set(path.read_text(encoding="utf-8", errors="ignore").splitlines())
    except (OSError, ValueError):
        return set()
    return set()


def edit_pairs(payload: dict) -> list[tuple[str, str]]:
    """(前, 後) の組。向きを見る検査はこれを使う。Write は前を持たない。"""
    tool = payload.get("tool_name")
    inp = payload.get("tool_input") or {}
    if tool == "Write":
        return []
    edits = inp.get("edits") if tool == "MultiEdit" else [inp]
    return [(str(e.get("old_string", "")), str(e.get("new_string", "")))
            for e in (edits or [])]


def _numbers(text: str, key_rx: re.Pattern) -> dict[str, float]:
    """`key = 数値` を拾う。同じ key が前後にあれば向きを比べられる。"""
    out: dict[str, float] = {}
    for m in key_rx.finditer(text):
        tail = text[m.end():m.end() + 40]
        num = re.match(r"-?\d+(?:\.\d+)?", tail.strip())
        if num:
            out[m.group(1).lower()] = float(num.group(0))
    return out


def loosened_thresholds(pairs: list[tuple[str, str]],
                        rules: list[tuple[str, str, re.Pattern]]) -> list[str]:
    """合格線が *緩む向き* に動いたものだけを返す。厳しくする変更は痕跡ではない。"""
    hits: list[str] = []
    for old, new in pairs:
        for _, name, rx in rules:
            before, after = _numbers(old, rx), _numbers(new, rx)
            for key, was in before.items():
                now = after.get(key)
                if now is None or now == was:
                    continue
                if (name.endswith("_up") and now > was) or \
                   (name.endswith("_down") and now < was):
                    hits.append(f"{key}: {was:g} → {now:g}")
    return hits


def widened_lists(pairs: list[tuple[str, str]],
                  rules: dict[str, re.Pattern]) -> list[str]:
    """既にある列挙に項目だけを足した変更を返す。

    変数名では見ない。名前で見ると、列挙しなかった名前が永久に見えなくなる——
    これは release_gate の絶対パス検査が拡張子の列挙で 7 割見逃したのと同じ形の
    誤りで、実際にこのルールの最初の実装がそれを踏んでいた。見るのは形だけ:
    既に括弧が在り、増えた行がすべて引用符で囲まれた項目である。
    """
    opener_rx, item_rx = rules.get("opener"), rules.get("item")
    if not opener_rx or not item_rx:
        return []
    hits: list[str] = []
    for old, new in pairs:
        if not opener_rx.search(old):
            continue
        before = set(old.splitlines())
        added = [l for l in new.splitlines() if l not in before]
        if not added or not all(item_rx.match(l) for l in added):
            continue
        # 逃がすための列挙かどうかを、項目の形で見る。依存（バージョン指定子つき）と
        # 自然文（かな・漢字・空白を含む）は、検査から逃がすための項目ではない。
        values = [l.strip().strip(",").strip("\"'") for l in added]
        if any(_rx_hit(rules.get("not_exemption"), v) or
               _rx_hit(rules.get("prose"), v) for v in values):
            continue
        hits.append(", ".join(values)[:90])
    return hits


def _rx_hit(rx: re.Pattern | None, text: str) -> bool:
    return bool(rx and rx.search(text))


def added_lines(payload: dict) -> list[str]:
    """この操作で **新しく現れる** 行だけを見る。既に在るものは裁かない。"""
    tool = payload.get("tool_name")
    inp = payload.get("tool_input") or {}
    if tool == "Write":
        new = str(inp.get("content", ""))
        return [l for l in new.splitlines() if l not in _existing(inp.get("file_path"))]
    edits = inp.get("edits") if tool == "MultiEdit" else [inp]
    lines: list[str] = []
    for e in edits or []:
        before = set(str(e.get("old_string", "")).splitlines())
        lines += [l for l in str(e.get("new_string", "")).splitlines()
                  if l not in before]
    return lines


def has_reason(lines: list[str], i: int, hit: re.Pattern) -> bool:
    """同じ行か直前の行に、痕跡そのものを除いた散文が書かれているか。"""
    for cand in (lines[i], lines[i - 1] if i else ""):
        stripped = hit.sub("", cand)
        m = COMMENT.search(stripped)
        if m and len(m.group(1).strip()) >= MIN_REASON:
            return True
    return False


def main() -> int:
    mode = os.environ.get("WORKOS_ROOT_CAUSE", "block").lower()
    if mode == "off":
        return 0
    try:
        payload = json.load(sys.stdin)
    except Exception:                              # noqa: BLE001
        return 0
    if payload.get("tool_name") not in WRITE_TOOLS:
        return 0

    declared = _declared()
    if not _compile(declared.get("line")):
        # 検査していないことを、検査に通ったことと同じ顔で返さない。
        # 止めはしない（宣言が無いだけの相手の作業を止める筋合いが無い）が、
        # 黙りもしない。読めなかった場合は _declared() が既に言っているので、
        # ここで言うのは「読めたが痕跡が1つも宣言されていない」場合だけ。
        if SMELLS.is_file():
            print(f"[work-os] root_cause_guard は何も検査していません。"
                  f"痕跡が1つも宣言されていません: {SMELLS}", file=sys.stderr)
        return 0
    lines = added_lines(payload)
    pairs = edit_pairs(payload)

    found: list[tuple[str, str, str]] = []
    for group, name, rx in _compile(declared.get("line")):
        for i, line in enumerate(lines):
            if rx.search(line) and not has_reason(lines, i, rx):
                found.append((group, name, line.strip()[:90]))
                break

    change = declared.get("change") or {}
    rules = _compile({"threshold": change.get("threshold", {})})
    for hit in loosened_thresholds(pairs, rules):
        found.append(("threshold", "loosened", hit))
    wide = {k: re.compile(str(v)) for k, v in (change.get("widening") or {}).items()}
    for hit in widened_lists(pairs, wide):
        found.append(("widening", "appended", hit))

    # 変更の向きで見つけたものも、同じ変更の中に理由が書かれていれば通す。
    if found and any(g in ("threshold", "widening") for g, _, _ in found):
        prose = [l for l in lines if COMMENT.search(l)
                 and len(COMMENT.search(l).group(1).strip()) >= MIN_REASON]
        if prose:
            found = [f for f in found if f[0] not in ("threshold", "widening")]
    if not found:
        return 0

    body = "\n".join(f"  - [{g}/{n}] {ln}" for g, n, ln in found)
    asks = sorted({GROUP_ASK[g] for g, _, _ in found if g in GROUP_ASK})
    message = (
        "[work-os] 症状を消す形の変更が、理由なしで入ろうとしています。\n"
        f"{body}\n\n"
        "禁止ではありません。要求しているのは、同じ場所に理由を書くことだけです:\n"
        + "\n".join(f"  ・{a}" for a in asks)
        + "\n\nそれが書けないなら、消しているのは報告のほうで、原因はまだ在ります。"
        "\n（この検査を切るには WORKOS_ROOT_CAUSE=off）"
    )
    if mode == "warn":
        print(message, file=sys.stderr)
        return 0
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": message,
        }
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
