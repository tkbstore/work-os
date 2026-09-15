#!/usr/bin/env python3
"""手元のリポジトリ全体を棚卸しして registry/repos.toml を生成する。

  python3 engine/registry.py <repos> > "$(registry_root)"/repos.toml
  # 置き場は engine/workos.py の registry_root() が決める（README の Configuration 層）

「今どのドメインに何本あって、どれが宣言済みで、どれが放置されているか」を1枚にする。
推測なので、生成後に手で直す前提。読み取り専用で既存には触れない。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from adopt import guess_domain, guess_role  # noqa: E402
from workos import iter_repo_dirs, load_repo  # noqa: E402


def last_commit(repo: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "log", "-1", "--format=%cs"],
            capture_output=True, text=True, timeout=10,
        )
        return out.stdout.strip() or "-"
    except Exception:  # noqa: BLE001
        return "-"


def main() -> int:
    ap = argparse.ArgumentParser(description="リポジトリ全体の棚卸し")
    ap.add_argument("root", type=Path)
    args = ap.parse_args()

    root = args.root.expanduser().resolve()
    repos = iter_repo_dirs(root)

    by_domain: dict[str, list[tuple[Path, str, str, bool]]] = defaultdict(list)
    for repo in repos:
        declared = load_repo(repo)
        domain = declared.domain if declared else guess_domain(repo.name)
        role = declared.role if declared else guess_role(repo, domain)
        by_domain[domain].append((repo, role, last_commit(repo), declared is not None))

    print("# engine/registry.py で生成。推測を含むので手で直してよい。")
    print(f"# root = {root}")
    print(f"# {len(repos)} repositories / {len(by_domain)} domains\n")

    for domain in sorted(by_domain, key=lambda d: (d == "unknown", -len(by_domain[d]), d)):
        items = sorted(by_domain[domain], key=lambda x: (x[1] != "domain", x[0].name))
        declared_n = sum(1 for i in items if i[3])
        print(f"[domain.{domain.replace('-', '_')}]")
        print(f"count    = {len(items)}")
        print(f"declared = {declared_n}   # work.toml を持つ数。ここを増やすのが導入作業")
        print("repos = [")
        for repo, role, date, declared in items:
            mark = "✓" if declared else " "
            print(f'  {{ name = "{repo.name}", role = "{role}", last = "{date}" }},'
                  f"  # {mark}")
        print("]\n")

    total_declared = sum(1 for items in by_domain.values() for i in items if i[3])
    print(f"# 宣言済み {total_declared} / {len(repos)}"
          f"（Compatibility = {total_declared / max(len(repos), 1):.0%}）")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        raise SystemExit(0)
