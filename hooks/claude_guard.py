#!/usr/bin/env python3
"""Claude Code PreToolUse hook — kernel / golden_path を AI の書き込みから守る。

stdin に Claude Code から JSON が渡る。書き込み系ツールの対象パスが
そのリポジトリの work.toml で kernel / golden_path と宣言されていたら：

  kernel       → enforcement によらず必ず止める
  golden_path  → enforcement = "block" のときだけ止める。warn なら stderr に出して通す

kernel だけ enforcement に従わないのは、憲法がそう書いているからである。
§3 は golden_path を「中央のみ（強制はしない）」と明記する一方、§6-1 は kernel を
無条件で「直接書き換えない。変更は work-os への PR 経由」と定めている。
両者を同じ enforcement で扱っていたため、kernel が warn で素通りしていた。

work.toml が無いリポジトリでは何もしない（既存を壊さない）。

**ここが見ているのはツール名である。**Edit / Write / MultiEdit / NotebookEdit の
4つに掛かっていて、Bash は入っていない。2026-09-02 実測: 同じ engine/workos.py に
対して Edit は deny されたのに、Bash から python でパッチを当てる形は一度も
止まらなかった。書き込みの手段は数え切れない（sed -i / tee / cp / リダイレクト /
エディタ / スクリプト）ので、ここに Bash を足しても次の手段が抜ける。

**手段ではなく結果を見る側は hooks/kernel_watch.py が持つ。**書き込みの後に走り、
守護対象が HEAD と違えばどう書かれたかに関係なく報告する。止めるのがここ、
黙って通らないようにするのが向こう、という分担。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))

try:
    from workos import load_repo
except Exception as exc:  # noqa: BLE001 — hook は絶対に落ちてはいけない
    # 落ちないことと、黙ることは別である。ここを抜けると kernel の書き込みが
    # 素通りするので、通したこと自体を言ってから抜ける。
    #
    # 実測 2026-08-31: engine/workos.py に構文エラーを入れた複製で、
    # CONSTITUTION.md（kernel 宣言）への Write が stdout `{}` / stderr 0バイトで
    # 通った。この docstring は kernel を「enforcement によらず必ず止める」と
    # 書いているが、その当のものが無言で消える。しかも workos.py は編集頻度が
    # 高く、それ自身が kernel である——壊した瞬間に、壊したことを咎える側が
    # 黙って居なくなる。指摘は work-os-registry 担当セッションから。
    print(json.dumps({}))
    print(f"[work-os] claude_guard は何も検査していません（kernel / golden_path の"
          f"書き込みが素通りします）。engine/ を読み込めません: {exc}", file=sys.stderr)
    raise SystemExit(0)

WRITE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
GUARDED = ("kernel", "golden_path")

REASON = {
    "kernel": (
        "これは kernel です。壊れると他のリポジトリが壊れます。\n"
        "変更したい場合は work-os 側に PR を出し、kernel_version を上げてから配布してください。\n"
        "この場で必要なら extension 層に新しいファイルを作って上書きする形にしてください。"
    ),
    "golden_path": (
        "これは golden_path です。中央が配布している推奨経路で、"
        "ここを個別に書き換えると次の配布で失われます。\n"
        "その変更が3リポジトリで必要なら、work-os 側に昇格させてください。"
    ),
}


def find_repo_root(path: Path) -> Path | None:
    for parent in [path, *path.parents]:
        if (parent / "work.toml").is_file():
            return parent
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:  # noqa: BLE001
        return 0

    if payload.get("tool_name") not in WRITE_TOOLS:
        return 0

    raw = (payload.get("tool_input") or {}).get("file_path")
    if not raw or not isinstance(raw, str):
        return 0

    target = Path(raw).expanduser()
    if not target.is_absolute():
        target = Path.cwd() / target
    # symlink を解決してからリポジトリを探す。解決前のパスで探すと、リポ外に置いた
    # symlink 越しの kernel 書き込みが「どのリポにも属さない」と見えて素通りする。
    # 08-27 に cross-repo-guard で同じ穴を踏んでいる（実体で同一性を見ること）。
    try:
        target = target.resolve()
    except OSError as exc:
        # 解決できないときは解決前のパスのまま判定する。握りつぶさず理由は出す
        # （symlink のループ、パスが長すぎる等。実在しないファイルはここに来ない）。
        print(f"[work-os] claude_guard: パスを解決できません"
              f"（解決前のまま判定します）: {exc}", file=sys.stderr)
    root = find_repo_root(target)
    if root is None:
        return 0

    repo = load_repo(root)
    if repo is None:
        return 0

    try:
        rel = str(target.relative_to(root.resolve()))
    except ValueError:
        return 0

    layer = repo.layer_of(rel)
    if layer not in GUARDED:
        return 0

    message = f"[work-os] {rel} は '{layer}' 層です。\n{REASON[layer]}"

    # kernel は enforcement の対象外。憲法 §6-1 は条件を付けずに禁じている。
    if layer == "kernel" or repo.enforcement == "block":
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": message,
            }
        }))
        return 0

    print(message, file=sys.stderr)
    return 0


def guarded_main() -> int:
    """どんな入力でも 0 で返す。

    この hook は全プロジェクトの Edit / Write に挟まる。落ちれば編集そのものが
    止まるので、検出系の故障は fail-open にする（誤判定した瞬間に誤爆装置になる系は
    fail-open にする。別リポの担当検出で実測された原則と同じ）。
    黙って通すと故障に気づけないので、理由は stderr に出す。

    実測 2026-08-28: work.toml が壊れている / file_path が文字列でない /
    パスが異常に長い、の3形で exit 1 になっていた。
    """
    try:
        return main()
    except Exception as exc:  # noqa: BLE001
        print(f"[work-os] claude_guard が判定できませんでした（通します）: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 0


if __name__ == "__main__":
    raise SystemExit(guarded_main())
