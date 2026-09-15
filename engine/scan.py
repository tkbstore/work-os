#!/usr/bin/env python3
"""複数リポジトリを横断して、共通性と変動点を実測する。

work.toml が無いリポジトリでも動く。既存を一切変更しない読み取り専用。

  python3 engine/scan.py <repos> --group <domain-prefix>
  python3 engine/scan.py <repos> --group <domain-prefix> --emit > capabilities.toml

出力：
  1. 全リポで一致するパス          → kernel 候補
  2. 3リポ以上で登場するパス       → 昇格候補（Rule of Three）
  3. 2リポ以下のパス               → extension / local のまま
  4. 共通パスごとの「版数」と世代   → 差分が要件差か複製時期の差かを判別する
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from workos import RULE_OF_THREE, content_hash, in_group, iter_repo_dirs  # noqa: E402

IGNORE = {
    ".git", ".venv", "node_modules", "__pycache__", ".ruff_cache", ".pytest_cache",
    ".DS_Store", ".mypy_cache", "dist", "build", ".next", ".vercel", "logs",
}
# この直下は1階層深くまで見る（機能の単位がここにあることが多い）
PROBE = {"mcp_servers", "scripts", "skills", "engine", "packages", "services", "src", "apps"}


def candidate_paths(repo: Path) -> set[str]:
    out: set[str] = set()
    for child in repo.iterdir():
        if child.name in IGNORE or child.name.startswith(".") and child.name != ".claude":
            continue
        out.add(child.name)
        if child.is_dir() and child.name in PROBE:
            for sub in child.iterdir():
                if sub.name in IGNORE or sub.name.startswith("."):
                    continue
                out.add(f"{child.name}/{sub.name}")
    return out


def scan(root: Path, group: str | None) -> tuple[list[Path], dict[str, dict[str, str]]]:
    repos = [d for d in iter_repo_dirs(root) if in_group(d.name, group)]
    table: dict[str, dict[str, str]] = defaultdict(dict)
    for repo in repos:
        for rel in candidate_paths(repo):
            table[rel][repo.name] = content_hash(repo / rel)
    return repos, table


def classify(n_repos: int, count: int) -> str:
    if count == n_repos and n_repos > 1:
        return "kernel候補"
    if count >= RULE_OF_THREE:
        return "昇格候補"
    return "extension"


def report(repos: list[Path], table: dict[str, dict[str, str]]) -> None:
    n = len(repos)
    print(f"\nscan: {n} リポジトリ")
    for r in repos:
        print(f"  - {r.name}")

    rows = []
    for rel, per_repo in table.items():
        versions = {h for h in per_repo.values() if h}
        rows.append((len(per_repo), rel, len(versions), per_repo))
    rows.sort(key=lambda x: (-x[0], x[1]))

    print(f"\n{'パス':<34}{'社数':>5}{'版数':>5}  判定")
    print("-" * 72)
    for count, rel, nver, _ in rows:
        if count < 2:
            continue
        print(f"{rel:<34}{count:>5}{nver:>5}  {classify(n, count)}")

    singles = [rel for count, rel, _, _ in rows if count == 1]
    if singles:
        print(f"\n1リポのみ（local のまま） : {', '.join(sorted(singles)[:14])}"
              + (" …" if len(singles) > 14 else ""))

    # --- 世代の検出 ---------------------------------------------------------
    print("\n世代の検出（同じハッシュ＝同じ複製世代）")
    print("-" * 72)
    for count, rel, nver, per_repo in rows:
        if count < RULE_OF_THREE or nver < 2 or nver > 4:
            continue
        buckets: dict[str, list[str]] = defaultdict(list)
        for repo_name, h in per_repo.items():
            if h:
                buckets[h].append(repo_name)
        print(f"\n  {rel}  — {nver}版")
        for h, names in sorted(buckets.items(), key=lambda kv: -len(kv[1])):
            print(f"    {h}  {len(names):>2}社  {', '.join(sorted(names))}")
    print(
        "\n  版数が社数よりずっと少ないなら、その差は顧客要件ではなく複製時期の差。"
        "\n  統合コストは想像よりはるかに低い。"
    )


def emit_toml(repos: list[Path], table: dict[str, dict[str, str]]) -> None:
    n = len(repos)
    print("# engine/scan.py --emit で生成。手で status と invariant を埋めてから使う。")
    print("# 置き場所: ドメインリポジトリの capabilities.toml\n")
    rows = sorted(table.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    for rel, per_repo in rows:
        count = len(per_repo)
        if count < 2:
            continue
        status = "proposed" if count >= RULE_OF_THREE else "experimental"
        layer = "kernel" if count == n and n > 1 else "extension"
        print("[[capability]]")
        print(f'id        = "{rel.replace("/", "-")}"')
        print(f'path      = "{rel}"')
        print(f'layer     = "{layer}"   # 壊れると他リポが壊れるなら kernel、それ以外は下げる')
        print(f'status    = "{status}"')
        print(f'summary   = ""')
        print(f"used_by   = [{', '.join(chr(34) + r + chr(34) for r in sorted(per_repo))}]")
        print(f'invariant = []   # stable 以上なら必須：外から見て変えてはいけない契約')
        print(f"configurable = []\n")


def main() -> int:
    ap = argparse.ArgumentParser(description="複数リポジトリの共通性と変動点を実測する")
    ap.add_argument("root", type=Path, help="リポジトリが並んでいる親ディレクトリ")
    ap.add_argument("--group", help="対象を名前の接頭辞で絞る（例: <domain>-）")
    ap.add_argument("--emit", action="store_true", help="capabilities.toml の草案を出力する")
    args = ap.parse_args()

    root = args.root.expanduser().resolve()
    if not root.is_dir():
        print(f"ディレクトリがありません: {root}", file=sys.stderr)
        return 2

    repos, table = scan(root, args.group)
    if not repos:
        print("対象リポジトリが見つかりませんでした。", file=sys.stderr)
        return 1

    if args.emit:
        emit_toml(repos, table)
    else:
        report(repos, table)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
