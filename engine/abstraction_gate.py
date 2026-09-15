#!/usr/bin/env python3
"""abstraction_gate.py — work-os が具体に落ちていないかを機械的に検査する。

work-os は仕組みだけを持つ。「どの顧客が」「どのリポが」「どの指標が」は
組織固有の事実であり、config 層（registry/）に置かれるべきものである。
このゲートは、公開対象の層に固有名詞が漏れていないかを見る。

抽象度の規律と公開可能性は同じ一つの問題である。公開できないものが混ざる位置は、
具体が漏れている位置と一致する。だから片方を測れば両方が守れる。

  python3 engine/abstraction_gate.py            # 検査（漏れがあれば exit 1）
  python3 engine/abstraction_gate.py --list     # 公開対象の一覧を出す

外部依存なし。読み取りのみ。
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(Path(__file__).resolve().parent))
from workos import _load_toml, registry_path  # noqa: E402

TERMS = registry_path("private_terms.toml")

# 公開しない層。ここに具体が集まるのは正しい。
PRIVATE_PREFIXES = ("registry/",)


def public_files() -> list[Path]:
    """git 管理下のうち、公開対象になるファイル。"""
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files"],
                         capture_output=True, text=True, timeout=30)
    files = []
    for rel in out.stdout.splitlines():
        if not rel or rel.startswith(PRIVATE_PREFIXES):
            continue
        p = ROOT / rel
        if p.is_file():
            files.append(p)
    return files


def load_terms() -> list[str]:
    """検出したい固有名詞。registry 側に置く（このリスト自体が固有名詞なので）。"""
    if not TERMS.exists():
        return []
    data = _load_toml(TERMS)
    terms: list[str] = []
    for group in data.values():
        if isinstance(group, dict):
            for v in group.values():
                terms.extend(v if isinstance(v, list) else [v])
        elif isinstance(group, list):
            terms.extend(group)
    return [str(t) for t in terms if str(t).strip()]


def main() -> int:
    ap = argparse.ArgumentParser(description="work-os の抽象度を検査する")
    ap.add_argument("--list", action="store_true", help="公開対象の一覧を出す")
    args = ap.parse_args()

    files = public_files()
    if args.list:
        for f in files:
            print(f.relative_to(ROOT))
        print(f"--- 公開対象 {len(files)} ファイル（{', '.join(PRIVATE_PREFIXES)} は非公開）")
        return 0

    terms = load_terms()
    if not terms:
        print(f"{TERMS} が無いか空です。検査できません。", file=sys.stderr)
        return 2

    pattern = re.compile("|".join(re.escape(t) for t in terms), re.IGNORECASE)
    leaks: list[tuple[Path, int, str, str]] = []
    for f in files:
        try:
            text = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            m = pattern.search(line)
            if m:
                leaks.append((f.relative_to(ROOT), i, m.group(0), line.strip()[:80]))

    if not leaks:
        print(f"抽象度OK — 公開対象 {len(files)} ファイルに固有名詞の漏れなし"
              f"（検査語 {len(terms)} 件）")
        return 0

    print(f"具体が漏れています: {len(leaks)} 箇所")
    print("engine は仕組みだけを持つ。組織固有の事実は registry/ に移すこと。\n")
    for path, line_no, term, ctx in leaks[:40]:
        print(f"  {path}:{line_no}  [{term}]  {ctx}")
    if len(leaks) > 40:
        print(f"  ... 他 {len(leaks) - 40} 箇所")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
