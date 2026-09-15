#!/usr/bin/env python3
"""intent.py — 各リポジトリが「何をやろうとしているか」を集める。読み取り専用。

fleet.py が**状態**（未push・未コミット・放置）を見るのに対し、これは**意図**を見る。
別の観測対象なので別のファイルにしてある。混ぜると、同じ場所を2人が書いて衝突する。

読むのは既に自然発生している session の front-matter だけである。新しい規約は足さない。

    ---
    schema_version: 1
    id / timestamp / project / actor / intent / outcome / confidence
    ---

git プロセスを起動せず、.git/HEAD と refs のタイムスタンプ、そして sessions/*.md の
先頭だけを読む。だから 76 リポでも1秒で終わり、index.lock も作らない。

  python3 engine/intent.py <repos>
  python3 engine/intent.py <repos> --days 3
  python3 engine/intent.py <repos> --coverage   # 意図層がどれだけ埋まっているか

外部依存なし。何も書き換えない。
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from adopt import guess_domain  # noqa: E402
from workos import iter_repo_dirs  # noqa: E402

FRONT_KEYS = ("id", "timestamp", "project", "actor", "intent", "outcome",
              "confidence", "generation_method")
# 2026-08-24 以前の heuristic 生成には既知の欠陥が2つある。
#   1. intent が "[user] " 接頭辞で弾かれ、断片しか入らない
#   2. outcome が transcript のどこかに "error" があるだけで blocked になる
# 生成側は直したが、既存レコードは作り直せない。事実として区別して表示する。
GENERATOR_FIXED = "2026-08-24"
# 「終わっていない」を意味する outcome。ここに居続けるものが本当の停滞である。
ATTENTION = {"blocked", "failure", "failed", "partial"}
FAR = 1e12
# 機械抽出が失敗したときに出る intent の形。宣言があることと、意図が書けていることは別である。
NOISE_PREFIXES = ("<", "ran ", "load the", "checked whether", "you asked", "/")


@dataclass
class Row:
    name: str
    domain: str
    branch: str
    commit_age: float = FAR      # 秒
    session_age: float = FAR
    session_id: str = ""
    outcome: str = ""
    intent: str = ""
    confidence: str = ""
    sessions_total: int = 0
    sessions_empty: int = 0
    sessions_declared: int = 0   # front-matter を持つ数
    method: str = ""             # generation_method（heuristic か claude-* か）


def read_head(repo: Path) -> tuple[str, float]:
    """git を起動せずに現在のブランチと最終更新を読む。"""
    head = repo / ".git" / "HEAD"
    if not head.is_file():
        return "-", FAR
    txt = head.read_text(encoding="utf-8", errors="ignore").strip()
    if not txt.startswith("ref: "):
        return "detached", FAR
    ref = txt[5:]
    branch = ref.rsplit("/", 1)[-1]
    for candidate in (repo / ".git" / ref, repo / ".git" / "packed-refs"):
        if candidate.is_file():
            return branch, time.time() - candidate.stat().st_mtime
    return branch, FAR


def read_front_matter(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return {}
    if not lines or lines[0].strip() != "---":
        return {}
    out: dict[str, str] = {}
    for line in lines[1:60]:
        if line.strip() == "---":
            break
        # metadata: 配下のキーはインデントされている。generation_method はそこにいる。
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        if (k := k.strip()) in FRONT_KEYS:
            out[k] = v.strip().strip('"').strip("'")
    return out


def scan_sessions(repo: Path) -> tuple[Path | None, dict[str, str], int, int, int]:
    """sessions/ を数え、front-matter を持つ最新のものを返す。"""
    d = repo / "sessions"
    if not d.is_dir():
        return None, {}, 0, 0, 0
    files = sorted((f for f in d.glob("*.md") if f.is_file()), key=lambda f: f.name, reverse=True)
    empty = sum(1 for f in files if f.stat().st_size == 0)
    declared, newest, fm = 0, None, {}
    for f in files:
        got = read_front_matter(f)
        if got:
            declared += 1
            if newest is None:
                newest, fm = f, got
    return newest, fm, len(files), empty, declared


def collect(root: Path) -> list[Row]:
    rows = []
    for repo in iter_repo_dirs(root):
        branch, commit_age = read_head(repo)
        path, fm, total, empty, declared = scan_sessions(repo)
        rows.append(Row(
            name=repo.name, domain=guess_domain(repo.name), branch=branch,
            commit_age=commit_age,
            session_age=(time.time() - path.stat().st_mtime) if path else FAR,
            session_id=fm.get("id", ""), outcome=fm.get("outcome", ""),
            method=fm.get("generation_method", ""),
            intent=fm.get("intent", ""), confidence=fm.get("confidence", ""),
            sessions_total=total, sessions_empty=empty, sessions_declared=declared,
        ))
    return rows


def ago(seconds: float) -> str:
    if seconds > 1e11:
        return "—"
    if (m := seconds / 60) < 60:
        return f"{int(m)}分前"
    if (h := m / 60) < 48:
        return f"{int(h)}時間前"
    return f"{int(h / 24)}日前"


def clip(s: str, n: int) -> str:
    s = s.replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def is_noise(intent: str) -> bool:
    return intent.strip().lower().startswith(NOISE_PREFIXES)


def print_coverage(rows: list[Row]) -> None:
    total = sum(r.sessions_total for r in rows)
    empty = sum(r.sessions_empty for r in rows)
    declared = sum(r.sessions_declared for r in rows)
    repos_with = sum(1 for r in rows if r.sessions_declared)
    print("\n■ 意図層の充足")
    print(f"  session ファイル {total} 件 / 意図の宣言あり {declared} 件"
          f"（{declared / total:.0%}）" if total else "  session ファイルなし")
    print(f"  空ファイル {empty} 件（{empty / total:.0%}）— 開始時に作られ要約が走らなかったもの"
          if total else "")
    print(f"  宣言のあるリポジトリ {repos_with} / {len(rows)}")
    if repos_with >= 3:
        print("  → 3社ルールを超えている。この規約は既に共通化されている")
    have = [r for r in rows if r.intent]
    if have:
        noise = sum(1 for r in have if is_noise(r.intent))
        weak = sum(1 for r in have if r.confidence and float(r.confidence or 0) <= 0.4)
        print(f"  意図が読める形になっているもの {len(have) - noise} / {len(have)}"
              f"（機械抽出の失敗 {noise}）")
        print(f"  confidence 0.4 以下 {weak} / {len(have)} — 規約はあるが中身はまだ薄い")


def main() -> int:
    ap = argparse.ArgumentParser(description="各リポジトリの意図を集める")
    ap.add_argument("root", type=Path)
    ap.add_argument("--days", type=float, default=2.0, help="「動いている」とみなす日数")
    ap.add_argument("--stale", type=float, default=30.0, help="放置とみなす日数")
    ap.add_argument("--coverage", action="store_true", help="意図層の充足だけを出す")
    args = ap.parse_args()

    root = args.root.expanduser().resolve()
    rows = collect(root)
    if args.coverage:
        print_coverage(rows)
        return 0

    window = args.days * 86400
    print(f"\nINTENT  {root}  ── {len(rows)} リポジトリ\n")

    active = sorted((r for r in rows if min(r.commit_age, r.session_age) < window),
                    key=lambda r: min(r.commit_age, r.session_age))
    print(f"■ 直近 {args.days:g} 日に動いた ── {len(active)}")
    print(f"  {'リポジトリ':<26}{'ブランチ':<32}{'コミット':<10}{'セッション':<12}結果")
    for r in active:
        print(f"  {r.name:<26}{clip(r.branch, 30):<32}{ago(r.commit_age):<10}"
              f"{ago(r.session_age):<12}{r.outcome or '—'}")

    stuck = [r for r in rows if r.outcome in ATTENTION]
    suspect = sum(1 for r in stuck if r.method == "heuristic")
    print(f"\n■ 直近セッションが未完了で終わっている ── {len(stuck)}")
    if suspect:
        print(f"  （うち {suspect} 件は {GENERATOR_FIXED} 以前の heuristic 生成。"
              "blocked が過剰に出る既知の欠陥があり、そのままは信用できない）")
    for r in sorted(stuck, key=lambda r: r.session_age):
        print(f"  {r.name:<26}{r.outcome:<10}conf={r.confidence or '—':<6}{ago(r.session_age)}")
        if r.intent and not is_noise(r.intent):
            print(f"    └ {clip(r.intent, 88)}")
        elif r.intent:
            print("    └ （意図が記録されていない — 機械抽出が失敗している）")

    review = [r for r in rows if r.branch not in ("main", "master", "-", "detached")]
    print(f"\n■ main 以外のブランチ ── {len(review)}（＝マージ待ちの変更）")
    for r in sorted(review, key=lambda r: r.commit_age):
        print(f"  {r.name:<26}{clip(r.branch, 40):<42}{ago(r.commit_age)}")

    stale = [r for r in rows
             if min(r.commit_age, r.session_age) > args.stale * 86400 and r.commit_age < 1e11]
    print(f"\n■ {args.stale:g} 日以上動いていない ── {len(stale)}")
    print("  " + ", ".join(r.name for r in sorted(stale, key=lambda r: -r.commit_age)))

    by_domain: dict[str, int] = {}
    for r in rows:
        by_domain[r.domain] = by_domain.get(r.domain, 0) + 1
    print("\n■ ドメイン別")
    print("  " + " / ".join(f"{k} {v}" for k, v in sorted(by_domain.items(), key=lambda kv: -kv[1])))
    print_coverage(rows)
    print()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        raise SystemExit(0)
