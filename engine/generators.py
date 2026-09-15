#!/usr/bin/env python3
"""generators.py — 「機械が同じものを多数のリポに書いている」形を見つける。読み取り専用。

今日1日で同じ形の事故が3回起きた。session ログ、.mcp.json、自動生成された
CLAUDE.md。いずれも**単体では無害なファイルが、宣言なしに全リポへ複製されていた**。
1リポに1つあるだけなら誰も気づかず、気づいたときには数百件になっている。

検出の原理は単純である。同じ相対パスのファイルが3つ以上のリポに現れ、しかも
中身が同一なら、それは人間が書いたものではない。機械が書いている。

宣言されているものは除外する（憲法 §6-2: 登録されていない成果物は存在しない）。
つまりこのコマンドの出力は、常に**宣言されていない生成器の一覧**である。

  python3 engine/generators.py <repos>
  python3 engine/generators.py <repos> --min 5      # 5リポ以上のものだけ
  python3 engine/generators.py <repos> --json

外部依存なし。何も書き換えない。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from workos import iter_repo_dirs, registry_path  # noqa: E402

KNOWN = registry_path("known_generators.toml")
# ツールのキャッシュは「機械が書いている」が、それは既知で無害である。
# ここを増やすのは registry ではなく engine 側でよい（どの組織でも同じだから）。
SKIP_DIRS = {".git", "node_modules", "venv", "env", ".venv", "__pycache__", "dist",
             "build", ".next", "data", "outputs", "out", ".gradle", "bin",
             ".ruff_cache", ".pytest_cache", ".mypy_cache", ".cache", "coverage",
             "htmlcov", ".turbo", ".astro", "target", "vendor"}
MAX_BYTES = 200_000
# 空の __init__.py のように情報量ゼロのファイルは、同一でも意味を持たない。
MIN_BYTES = 8
MAX_DEPTH = 3


def load_known() -> dict[str, str]:
    if not KNOWN.exists():
        return {}
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from workos import _load_toml  # noqa: PLC0415
    data = _load_toml(KNOWN)
    out = {}
    for group in data.values():
        if isinstance(group, dict):
            out.update({str(k): str(v) for k, v in group.items()})
    return out


def collect(root: Path) -> dict[str, list[tuple[str, str]]]:
    """{相対パス: [(リポ名, 内容ハッシュ), ...]} を返す。"""
    found: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for repo in iter_repo_dirs(root):
        def walk(d: Path, depth: int, repo_name: str = "") -> None:
            if depth > MAX_DEPTH:
                return
            try:
                entries = list(d.iterdir())
            except OSError:
                return
            for e in entries:
                if e.is_dir():
                    if e.name in SKIP_DIRS:
                        continue
                    walk(e, depth + 1, repo_name)
                elif e.is_file():
                    try:
                        size = e.stat().st_size
                        if size > MAX_BYTES or size < MIN_BYTES:
                            continue
                        digest = hashlib.sha256(e.read_bytes()).hexdigest()[:16]
                    except OSError:
                        continue
                    found[str(e.relative_to(repo))].append((repo_name, digest))

        walk(repo, 0, repo.name)
    return found


def main() -> int:
    ap = argparse.ArgumentParser(description="宣言されていない生成器を見つける")
    ap.add_argument("root", type=Path)
    ap.add_argument("--min", type=int, default=3,
                    help="何リポに現れたら生成器とみなすか（既定3＝3社ルール）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    root = args.root.expanduser().resolve()
    known = load_known()
    found = collect(root)

    hits = []
    for rel, entries in found.items():
        if len(entries) < args.min:
            continue
        by_hash: dict[str, list[str]] = defaultdict(list)
        for repo_name, digest in entries:
            by_hash[digest].append(repo_name)
        biggest = max(by_hash.values(), key=len)
        # 中身が同一のものが3つ以上あるなら、人間が書いたものではない
        if len(biggest) < args.min:
            continue
        hits.append({
            "path": rel,
            "repos": len(entries),
            "identical": len(biggest),
            "variants": len(by_hash),
            "declared": rel in known,
            "note": known.get(rel, ""),
            "examples": sorted(biggest)[:5],
        })
    hits.sort(key=lambda h: (-h["identical"], h["path"]))

    if args.json:
        print(json.dumps(hits, ensure_ascii=False, indent=2))
        return 0

    undeclared = [h for h in hits if not h["declared"]]
    print(f"同じ内容のファイルが {args.min} リポ以上に存在するもの ── {len(hits)} 種")
    print(f"うち宣言されていないもの ── {len(undeclared)}\n")
    for h in hits:
        mark = "宣言済" if h["declared"] else "未宣言"
        print(f"  [{mark}] {h['path']}")
        print(f"           {h['identical']} リポで同一内容 / {h['repos']} リポに存在"
              f" / 版 {h['variants']}")
        if h["declared"]:
            print(f"           {h['note']}")
        else:
            print(f"           例: {', '.join(h['examples'])}")
    if undeclared:
        print("\n  未宣言のものは、誰かの機械が黙って書いています。")
        print("  意図したものなら registry/known_generators.toml に登録してください。")
        print("  意図していないなら、それが次のゴミ生成器です。")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
