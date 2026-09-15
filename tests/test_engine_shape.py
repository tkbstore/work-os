#!/usr/bin/env python3
"""「リポジトリとは何か」の定義が、また散っていないかを検査する。

engine の9ファイルがそれぞれ iterdir() で76リポを列挙していて、条件が4通りに
割れていた（2026-09-01 実測）。ドット名を除くもの/除かないもの、.git を要求する
もの/しないもの。今の木ではどれも同じ答えを返していたので実害は出ていなかったが、
割れていること自体が実害になる。registry の所在を14ファイルから registry_root()
1本へ寄せたのと同じ形（2026-08-31）。

寄せた先は workos.iter_repo_dirs。ここでは2つを見る。

  1. 形  — kernel の外で、列挙を組み立て直していないこと
  2. 中身 — その1本が、寄せる前の4通りと同じ答えを返すこと

ファイル名では列挙しない。列挙すると次に足された engine が黙って素通りして、
この検査が捕まえたい形そのものになる。

  python3 tests/test_engine_shape.py
"""

from __future__ import annotations

import inspect
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "engine"))
from workos import in_group, iter_repo_dirs  # noqa: E402

# 定義を置いてよい唯一の場所。ここを増やすときは、増やす理由が要る。
HOME = "workos.py"

failed: list[str] = []


def check(desc: str, ok: bool) -> None:
    if not ok:
        failed.append(desc)
    print(f"  {'OK ' if ok else 'NG '} {desc}")


def rebuilds_listing(path: Path) -> list[int]:
    """列挙を組み立て直している行を返す。

    「リポジトリかどうか」を自分で決めるには、ディレクトリを並べて .git の
    有無を見るしかない。その2つが近くに並んでいる箇所を、組み立て直しと見る。
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    return [
        i + 1 for i, ln in enumerate(lines)
        if "iterdir()" in ln
        and any(q in "\n".join(lines[i:i + 5]) for q in ('".git"', "'.git'"))
    ]


def mkrepo(parent: Path, name: str, *, git: bool = True) -> Path:
    d = parent / name
    d.mkdir()
    if git:
        (d / ".git").mkdir()
    return d


def main() -> int:
    print("形 — kernel の外で列挙を組み立て直していないこと")
    for f in sorted((ROOT / "engine").glob("*.py")):
        if f.name == HOME:
            continue
        hits = rebuilds_listing(f)
        check(f"{f.name}" + (f"（{hits}行目）" if hits else ""), not hits)

    print("\n中身 — 寄せた1本が、寄せる前の4通りと同じ答えを返すこと")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        mkrepo(tmp, "alpha")
        mkrepo(tmp, "alpha-child")
        mkrepo(tmp, "beta")
        mkrepo(tmp, ".hidden")              # ドット名。.git は持っている
        mkrepo(tmp, "no-git", git=False)    # git になっていない
        (tmp / "alphafile").write_text("x") # ディレクトリではない

        names = lambda **kw: [p.name for p in iter_repo_dirs(tmp, **kw)]

        check("git のあるものを名前順に返す",
              names() == ["alpha", "alpha-child", "beta"])
        check("git でないものは既定では返さない", "no-git" not in names())
        check("require_git=False なら git でないものも返す",
              "no-git" in names(require_git=False))
        check("ドット名は .git を持っていても返さない",
              ".hidden" not in names() and ".hidden" not in names(require_git=False))
        check("ファイルは返さない", "alphafile" not in names())
        check("scan_root が無ければ空", iter_repo_dirs(tmp / "nowhere") == [])

        # 絞り方は列挙とは別に置く。定義（何を数えるか）と選択（そのうちどれを
        # 見るか）を1つの関数に混ぜると、次に別の絞り方が要る目的が来たときに
        # パラメータを足すか列挙を書き直すかの二択になる（2026-09-01 のレビュー指摘）。
        check("列挙は絞り方を知らない",
              "group" not in inspect.signature(iter_repo_dirs).parameters)
        picked = [p.name for p in iter_repo_dirs(tmp) if in_group(p.name, "alpha")]
        check("絞り方は同名と接頭辞に効く", picked == ["alpha", "alpha-child"])
        check("絞り方に一致しなければ空",
              [p for p in iter_repo_dirs(tmp) if in_group(p.name, "zzz")] == [])
        check("group=None は全部通す",
              all(in_group(n, None) for n in ("alpha", "beta", "zzz")))

        # .git がファイルのもの（worktree）。shell の [ -d ] はここを落とすが、
        # リポジトリではある。2026-09-01 に手元の木で実際にこの形が1本あった。
        wt = tmp / "worktree"
        wt.mkdir()
        (wt / ".git").write_text("gitdir: /elsewhere\n")
        check("git がファイルのもの（worktree）も返す", "worktree" in names())

    print("\n呼ぶ側 — 定義の上に載っていること")
    for name, args in (("promote.py", ["--help"]), ("scan.py", ["--help"])):
        r = subprocess.run([sys.executable, str(ROOT / "engine" / name), *args],
                           capture_output=True, timeout=30)
        check(f"{name} が読み込める", r.returncode == 0)

    print()
    if failed:
        print(f"失敗 {len(failed)} 件")
        for f in failed:
            print(f"  - {f}")
        return 1
    print("全ケース通過")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
