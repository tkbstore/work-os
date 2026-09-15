#!/usr/bin/env python3
"""branch_guard.py — main を直接編集しようとしたときに気づかせる。PreToolUse フック。

セッション中に main へ書き込むと、意図しない変更が本流に混ざる。だが実測すると
14リポジトリは既に feature ブランチで作業していた。規約は自然発生している。
足りないのは取りこぼしを拾う仕組みだけである。

憲法 §6-5 に従い warn から始める。何も止めない。警告が出た回数が、
宣言と実態のズレの量そのものになる。ゼロに近づいてから block に上げること。

stdin に PreToolUse の JSON を受け取り、そのまま stdout に返す。
警告は stderr に出す（＝ツール実行は妨げない）。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

PROTECTED = {"main", "master"}
# work.toml を読むまでもなく、ここは常に自由であるべき層（憲法 §6-4 Escape Hatch）
ALWAYS_FREE = {"sessions", ".claude", "registry"}


def repo_root(path: Path) -> Path | None:
    try:
        out = subprocess.run(["git", "-C", str(path.parent), "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=5)
        return Path(out.stdout.strip()) if out.returncode == 0 and out.stdout.strip() else None
    except (subprocess.TimeoutExpired, OSError):
        return None


def current_branch(root: Path) -> str:
    try:
        out = subprocess.run(["git", "-C", str(root), "rev-parse", "--abbrev-ref", "HEAD"],
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        return ""


def main() -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        print(raw, end="")
        return 0

    target = (payload.get("tool_input") or {}).get("file_path", "")
    if not target:
        print(raw, end="")
        return 0

    path = Path(target)
    root = repo_root(path)
    if root is None:
        print(raw, end="")
        return 0

    try:
        rel = path.relative_to(root)
    except ValueError:
        print(raw, end="")
        return 0
    if rel.parts and rel.parts[0] in ALWAYS_FREE:
        print(raw, end="")
        return 0

    branch = current_branch(root)
    if branch in PROTECTED:
        print(
            f"[work-os] {root.name} は {branch} を直接編集しようとしています。\n"
            f"[work-os] ブランチを切ってから作業してください:\n"
            f"[work-os]   git -C {root} switch -c <topic>\n"
            f"[work-os] （warn モードのため、この操作は止めていません）",
            file=sys.stderr,
        )
    print(raw, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
