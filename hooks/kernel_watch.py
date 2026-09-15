#!/usr/bin/env python3
"""Claude Code PostToolUse hook — kernel / golden_path が実際に変わったかを見る。

claude_guard.py は **ツール名の列挙**（Edit / Write / MultiEdit / NotebookEdit）で
守っている。列挙しなかった側は素通りする。2026-09-02 実測: 同じ engine/workos.py
に対して Edit は deny されたのに、Bash から python でパッチを当てる形は最後まで
一度も止まらなかった。書き込みの手段は数え切れないので（sed -i / tee / cp /
リダイレクト / エディタ / スクリプト）、手段を数えている限り必ず抜ける。

ここでは手段を見ない。**結果**を見る。git が「HEAD と違う」と言う守護対象が
在れば、それがどう書かれたかに関係なく報告する。ツールは何でもよいので matcher
は全ツールでよい。

止められはしない（PostToolUse は書き込みの後に走る）。止めるのは claude_guard の
仕事で、ここの仕事は **黙って通らないようにすること**。気づかないまま kernel が
書き換わっている状態を無くす。

同じ内容を二度は言わない。守護対象ごとに内容ハッシュを .git/ の下に控えて、
変わったときだけ言う。控え先を .git にするのは、リポの外へ書かないため
（config 層は別リポで、そこへの書き込みは cross-repo-guard が止める）。

  echo '{"cwd": "/path/to/repo"}' | python3 hooks/kernel_watch.py"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))

try:
    from workos import load_repo
except Exception as exc:  # noqa: BLE001 — hook は落ちてはいけない
    # 落ちないことと、黙ることは別。claude_guard と同じ理由で、通したことを言う。
    print(f"[work-os] kernel_watch は何も検査していません"
          f"（kernel の書き換えに気づけません）: {exc}", file=sys.stderr)
    raise SystemExit(0)

STATE = "workos-kernel-seen.json"


def repo_root(start: Path) -> Path | None:
    for parent in [start, *start.parents]:
        if (parent / "work.toml").is_file():
            return parent
    return None


def changed_vs_head(root: Path, paths: list[str]) -> list[str]:
    """HEAD と中身が違う守護対象。追跡されていないものは公開されないので見ない。"""
    if not paths:
        return []
    proc = subprocess.run(["git", "-C", str(root), "diff", "--name-only", "HEAD", "--", *paths],
                          capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        return []                      # 1コミットも無い木など。守るものがまだ無い
    return [line for line in proc.stdout.splitlines() if line.strip()]


def digest(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return "missing"


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:  # noqa: BLE001
        return 0

    cwd = payload.get("cwd") or ""
    root = repo_root(Path(cwd).expanduser().resolve() if cwd else Path.cwd())
    if root is None:
        return 0
    repo = load_repo(root)
    if repo is None:
        return 0

    changed = changed_vs_head(root, repo.protected_paths())
    if not changed:
        return 0

    state_file = root / ".git" / STATE
    try:
        seen = json.loads(state_file.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — 控えが無い/壊れているなら空から始める
        seen = {}

    fresh = []
    for rel in changed:
        d = digest(root / rel)
        if seen.get(rel) != d:
            fresh.append((rel, repo.layer_of(rel)))
        seen[rel] = d

    try:
        state_file.write_text(json.dumps(seen, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        # 控えられないと毎回言うことになる。うるさくはなるが黙るよりよい。
        print(f"[work-os] kernel_watch: 控えを書けません（毎回報告します）: {exc}",
              file=sys.stderr)

    if not fresh:
        return 0

    lines = [f"  - {rel}  [{layer}]" for rel, layer in fresh]
    kernel = [r for r, l in fresh if l == "kernel"]
    body = ("[work-os] 守護対象が HEAD と違います。どのツールで書いたかに関わらず"
            "報告します。\n" + "\n".join(lines))
    if kernel:
        body += ("\n\nkernel は憲法 §6-1 が無条件で直接の書き換えを禁じています。"
                 "\nこのリポが kernel の配布先なら、戻して work-os へ PR を出してください。"
                 "\nこのリポが work-os 本体なら、kernel_version と配布を確かめてください。")
    print(json.dumps({"decision": "block", "reason": body}, ensure_ascii=False))
    return 0


def guarded_main() -> int:
    """どんな入力でも 0 で返す。検出系の故障で編集そのものを止めない。"""
    try:
        return main()
    except Exception as exc:  # noqa: BLE001
        print(f"[work-os] kernel_watch が落ちました（検査していません）: {exc}",
              file=sys.stderr)
        return 0


if __name__ == "__main__":
    raise SystemExit(guarded_main())
