#!/usr/bin/env python3
"""既存リポジトリに work.toml を1枚置く。既存ファイルは一切変更しない。

  python3 engine/adopt.py <repos>/<a-repo>            # 内容を表示するだけ
  python3 engine/adopt.py <repos>/<a-repo> --write    # 実際に置く
  python3 engine/adopt.py <repos> --group <domain-prefix> --write    # まとめて置く

推測した内容がそのまま正しいことは期待していない。
「置いてから直す」ほうが「正しく決めてから置く」より速い、という前提の道具。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from workos import _load_toml, content_hash, in_group, iter_repo_dirs, registry_path  # noqa: E402

NAMING = registry_path("naming.toml")


def _load_naming() -> dict:
    """組織固有の命名規則を読む。無ければ空（＝推測しない）。"""
    return _load_toml(NAMING) if NAMING.exists() else {}

# 組織固有の事実は engine に置かない。registry/naming.toml から読む。
# ここにあるのは「読み方」だけであり、「何がどのドメインか」はこのファイルの外にある。
_NAMING = _load_naming()
DOMAIN_RULES: list[tuple[str, str]] = list((_NAMING.get("domain_rules") or {}).items())
DOMAIN_EXACT: dict[str, str] = dict(_NAMING.get("domain_exact") or {})
LAYER_GUESS: dict[str, list[str]] = dict(_NAMING.get("layer_guess") or {})
EXTENSION_PARENTS: list[str] = list((_NAMING.get("misc") or {}).get("extension_parents") or [])
KERNEL_HINTS: set[str] = set((_NAMING.get("misc") or {}).get("kernel_hints") or [])


def guess_domain(name: str) -> str:
    low = name.lower()
    if low in DOMAIN_EXACT:
        return DOMAIN_EXACT[low]
    for prefix, domain in DOMAIN_RULES:
        if low.startswith(prefix):
            return domain
    return "unknown"


def guess_role(repo: Path, domain: str) -> str:
    name = repo.name.lower()
    if name.endswith(("-lp", "-site", "_lp")):
        return "site"
    for prefix, dom in DOMAIN_RULES:
        if dom == domain and name == prefix:
            return "domain"
    if "-" in name and guess_domain(name) != "unknown":
        return "client"
    return "tool"


def existing(repo: Path, paths: list[str]) -> list[str]:
    return [p for p in paths if (repo / p).exists()]


def guess_extensions(repo: Path, kernel: list[str]) -> list[str]:
    out: list[str] = []
    for parent in EXTENSION_PARENTS:
        d = repo / parent
        if not d.is_dir():
            continue
        for sub in sorted(d.iterdir()):
            if not sub.is_dir() or sub.name.startswith((".", "__")):
                continue
            rel = f"{parent}/{sub.name}"
            if rel in kernel or sub.name in KERNEL_HINTS:
                continue
            out.append(rel)
    return out


def build(repo: Path, domain_repo: str, kernel_version: str) -> str:
    domain = guess_domain(repo.name)
    role = guess_role(repo, domain)

    kernel = existing(repo, LAYER_GUESS["kernel"])
    for hint in sorted(KERNEL_HINTS):
        p = f"mcp_servers/{hint}"
        if (repo / p).exists():
            kernel.append(p)
    kernel = sorted(dict.fromkeys(kernel))
    golden = existing(repo, LAYER_GUESS["golden_path"])
    config = existing(repo, LAYER_GUESS["config"])
    extension = guess_extensions(repo, kernel)

    def arr(items: list[str]) -> str:
        if not items:
            return "[]"
        inner = ",\n  ".join(f'"{i}"' for i in items)
        return f"[\n  {inner},\n]"

    fingerprint = content_hash(repo / kernel[0]) if kernel else ""

    return f'''# work-os declaration — 置くだけ。既存の挙動は何も変わらない。
# 憲法: https://github.com/<you>/work-os/blob/main/CONSTITUTION.md
# 直し方: 下の layers を実態に合わせるだけでよい。分からない行は消してよい。

[repo]
name        = "{repo.name}"
domain      = "{domain}"          # この仕事はどのドメインか
role        = "{role}"            # kernel | domain | client | tool | site | archive
status      = "active"
enforcement = "warn"              # warn = 何も止めない。数字が出てから "block" に上げる

[extends]
domain_repo    = "{domain_repo}"
kernel_version = "{kernel_version}"
kernel_fingerprint = "{fingerprint}"   # scan で世代を判別するための指紋

# --- 層の宣言 -------------------------------------------------------------
# ここに書かれていないパスはすべて local（誰が何をしてもよい）。
# 迷ったら書かない。書いた瞬間に守る義務が発生する。

[layers]
kernel      = {arr(kernel)}
golden_path = {arr(golden)}
config      = {arr(config)}
extension   = {arr(extension)}
'''


def targets(path: Path, group: str | None) -> list[Path]:
    path = path.expanduser().resolve()
    if (path / ".git").exists():
        return [path]
    return [d for d in iter_repo_dirs(path) if in_group(d.name, group)]


def main() -> int:
    ap = argparse.ArgumentParser(description="既存リポジトリに work.toml を追加する")
    ap.add_argument("path", type=Path)
    ap.add_argument("--group", help="親ディレクトリを渡したとき、名前の接頭辞で絞る")
    ap.add_argument("--write", action="store_true", help="実際にファイルを書く")
    ap.add_argument("--domain-repo", default="", help="継承元のドメインリポジトリ名")
    ap.add_argument("--kernel-version", default="0.1.0")
    ap.add_argument("--force", action="store_true", help="既存の work.toml を上書きする")
    args = ap.parse_args()

    repos = targets(args.path, args.group)
    if not repos:
        print("対象がありません。", file=sys.stderr)
        return 1

    for repo in repos:
        dest = repo / "work.toml"
        if dest.exists() and not args.force:
            print(f"skip  {repo.name} : work.toml が既にあります")
            continue
        content = build(repo, args.domain_repo, args.kernel_version)
        if args.write:
            dest.write_text(content, encoding="utf-8")
            print(f"write {repo.name}/work.toml")
        else:
            print(f"\n===== {repo.name}/work.toml =====")
            print(content)
    if not args.write:
        print("\n--write を付けると実際に書き込みます。既存ファイルには触れません。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
