#!/usr/bin/env python3
"""capabilities.toml → CAPABILITIES.md を生成する。

  python3 engine/render.py <repos>/<a-domain-repo> > CAPABILITIES.md

人間が読む一覧は「書くもの」ではなく「生成されるもの」にする。
手で書いた一覧は必ず実態とずれるが、生成物ならずれない。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from workos import PROMOTION_ORDER, load_capabilities  # noqa: E402

LABEL = {
    "kernel": "Kernel — 壊れると他リポジトリが壊れる。変更は work-os への PR 経由",
    "stable": "Stable — インターフェース確定。破壊的変更にはバージョンを上げる",
    "proposed": "Proposed — 3リポジトリ以上で必要。まだ契約は固まっていない",
    "experimental": "Experimental — 動いてはいる。いつ消えてもよい",
    "local": "Local — このリポジトリ限定。共通化しない",
    "deprecated": "Deprecated — 消す予定",
}


def main() -> int:
    ap = argparse.ArgumentParser(description="capabilities.toml から一覧を生成する")
    ap.add_argument("repo", type=Path)
    args = ap.parse_args()

    repo = args.repo.expanduser().resolve()
    caps = load_capabilities(repo)
    if not caps:
        print(f"capabilities.toml がありません: {repo}", file=sys.stderr)
        return 1

    print(f"# {repo.name} — Capabilities\n")
    print("<!-- 自動生成: python3 engine/render.py . > CAPABILITIES.md -->")
    print("<!-- 手で編集しない。capabilities.toml を直す。 -->\n")

    order = [*PROMOTION_ORDER[::-1], "deprecated"]
    for status in order:
        group = [c for c in caps if c.status == status]
        if not group:
            continue
        print(f"## {status}\n")
        print(f"> {LABEL.get(status, '')}\n")
        print("| id | path | 使用 | 変えてはいけない | 設定できる | 概要 |")
        print("|---|---|---|---|---|---|")
        for c in sorted(group, key=lambda x: x.id):
            print(
                f"| `{c.id}` | `{c.path}` | {len(c.used_by)} | "
                f"{', '.join(c.invariant) or '—'} | "
                f"{', '.join(c.configurable) or '—'} | {c.summary or '—'} |"
            )
        print()

    total = len(caps)
    kernel = sum(1 for c in caps if c.status == "kernel")
    print("---\n")
    print(f"合計 {total} capability / kernel {kernel} 件 "
          f"（kernel 比率 {kernel / total:.0%}。ここが上がりすぎたら Core が太っている）")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:  # head などにパイプしたとき
        raise SystemExit(0)
