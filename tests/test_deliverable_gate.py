#!/usr/bin/env python3
"""deliverable_gate が「渡せないものを止め、渡せるものに宛先を付ける」ことを検査する。

このゲートには release_gate と逆向きの罠がある。**除外の向きが逆**なので、
release_gate から観測を借りたときに `dist/` を除外する側の規則が付いてきてしまう。
それが起きていないことを、`dist/` を宣言した木で直接確かめる。

見本の固有名詞は registry から実行時に借りる。本文に書くと work-os 自身の
provenance レーンがそれを検出する（test_release_gate と同じ作法）。

  python3 tests/test_deliverable_gate.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
# E402 は「import が先頭に無い」の指摘。上で sys.path を挿してからでないと tests/ の
# 共通モジュールは解決しない。抑制しているのは順序の指摘だけである。
from _registry import borrow_terms, require  # noqa: E402

require("private_terms.toml")
GATE = ROOT / "engine" / "deliverable_gate.py"

GIT_ENV = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}

# 判定できていないことを表す返り値。通過とも失敗とも別に扱う。
UNSETTLED = 3
# read_text の上限（512KB）を確実に超える大きさ。
BIG = 600_000


def build(tmp: Path, files: dict[str, str], declarations: str,
          commit: bool = True) -> Path:
    """仕掛けのリポを組む。declarations は work.toml に足す [[deliverable]] の宣言。"""
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "work.toml").write_text(
        '[repo]\nname = "sample"\n' + declarations, encoding="utf-8")
    for rel, body in files.items():
        p = tmp / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(tmp)], check=True, timeout=30)
    if commit:
        subprocess.run(["git", "-C", str(tmp), "add", "-A"], check=True, timeout=60)
        subprocess.run(["git", "-C", str(tmp), "commit", "-qm", "chore: first"],
                       check=True, timeout=60, env=GIT_ENV)
    return tmp


def run(repo: Path) -> tuple[int, str]:
    """人が読む出力。案内文はここにしか出ない。"""
    proc = subprocess.run([sys.executable, str(GATE), str(repo)],
                          capture_output=True, text=True, timeout=180)
    return proc.returncode, proc.stdout


def run_json(repo: Path) -> tuple[int, dict]:
    """1件だけ宣言した木から、その1件を取る。0件・複数件なら空を返す。"""
    proc = subprocess.run([sys.executable, str(GATE), str(repo), "--json"],
                          capture_output=True, text=True, timeout=180)
    try:
        data = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return proc.returncode, {}
    items = [i for r in data for i in r["deliverables"]]
    return proc.returncode, items[0] if len(items) == 1 else {}


def main() -> int:
    failed: list[str] = []

    def check(desc: str, ok: bool) -> None:
        print(f"  {'OK ' if ok else 'NG '} {desc}")
        if not ok:
            failed.append(desc)

    one, two = borrow_terms(2)
    decl = '\n[[deliverable]]\npath = "deliverables/report.md"\n'

    with tempfile.TemporaryDirectory() as tmpdir:
        td = Path(tmpdir)

        # --- 宣言が無い木 ---------------------------------------------------
        print("\n宣言が無い木")
        code, out = run(build(td / "silent", {"README.md": "# x\n"}, ""))
        check("判定できていないと返す（exit 3）", code == UNSETTLED)
        check("問いが立っていないことを言う", "立てられていない" in out)

        # --- 顧客名なし -----------------------------------------------------
        print("\n顧客名が無い成果物")
        clean = build(td / "clean", {"deliverables/report.md": "# 何も無い\n"}, decl)
        code, item = run_json(clean)
        check("宛先の制約なしと出る", item.get("verdict") == "unrestricted")
        check("渡せる（exit 0）", code == 0)

        # --- 1社だけ --------------------------------------------------------
        print("\n1 社の名前だけが在る成果物")
        single = build(td / "single",
                       {"deliverables/report.md": f"# {one} 様向け\n{one} の件\n"}, decl)
        code, item = run_json(single)
        check("1 社にだけ渡せると出る", item.get("verdict") == "single")
        check("宛先がその1社と出る", item.get("recipients") == [one])
        check("渡せる（exit 0）", code == 0)
        check("件数が出る（数の無い判定は誤りが見えない）",
              item.get("clients", {}).get(one, 0) >= 2)

        # --- 2社以上 --------------------------------------------------------
        print("\n2 社の名前が在る成果物")
        multi = build(td / "multi",
                      {"deliverables/report.md": f"# {one} 様向け\n比較対象: {two}\n"},
                      decl)
        code, item = run_json(multi)
        check("外には渡せないと出る", item.get("verdict") == "internal_only")
        check("止まる（exit 1）", code == 1)
        check("2 社とも名指しされる",
              sorted(item.get("recipients", [])) == sorted([one, two]))
        check("件数を人が読む出力にも出す",
              all(f"{t} " in run(multi)[1] for t in (one, two)))

        # --- dist/ を見る（release_gate とは逆向き） --------------------------
        # release_gate は presets.vendored で **/dist/* を除外する。こちらは dist/
        # こそ渡るものなので見る。観測を借りている以上、除外の規則が付いてきて
        # いないことを直接確かめる必要がある。
        print("\ndist/ を見る（除外の向きが逆であること）")
        dist = build(td / "dist", {"dist/index.html": f"<p>{one} と {two}</p>\n"},
                     '\n[[deliverable]]\npath = "dist/"\n')
        code, item = run_json(dist)
        check("dist/ 配下を観測する", item.get("files") == 1)
        check("dist/ の中の顧客名で止まる", item.get("verdict") == "internal_only")

        # --- git 追跡外も見る -----------------------------------------------
        # 成果物は生成物であることが普通で、追跡で絞ると「渡るのに見ていない」が
        # 生まれる。commit していない木で同じ判定が出ることを見る。
        print("\ngit 追跡外のファイル")
        code, item = run_json(build(td / "untracked",
                                    {"deliverables/report.md": f"# {one}\n{two}\n"},
                                    decl, commit=False))
        check("追跡外でも観測する", item.get("files") == 1)
        check("追跡外でも止まる", item.get("verdict") == "internal_only")

        # --- 読めなかったものを通さない --------------------------------------
        # 512KB を超えるファイルは本文を読めない。読まなかったことを「顧客名が
        # 無かった」と同じ顔で出すと、大きい HTML の提案書が素通りする。
        print("\n読めなかったファイル")
        big = build(td / "big",
                    {"deliverables/report.md": "x\n",
                     "deliverables/huge.html": "y" * BIG},
                    '\n[[deliverable]]\npath = "deliverables/"\n')
        code, item = run_json(big)
        check("読めなかったファイルを数える", len(item.get("unread", [])) == 1)
        check("判定が出ていないことにする", item.get("settled") is False)
        check("顧客名が無くても通さない（exit 3）", code == UNSETTLED)
        check("見ていないことを、渡せることとして出さない",
              "渡せることではありません" in run(big)[1])

        # --- glob の宣言 ----------------------------------------------------
        print("\nglob の宣言")
        code, item = run_json(build(td / "globbed",
                                    {"deliverables/a.html": f"{one}\n",
                                     "deliverables/b.html": f"{one}\n",
                                     "deliverables/notes.md": f"{two}\n"},
                                    '\n[[deliverable]]\npath = "deliverables/*.html"\n'))
        check("glob に当たったファイルだけを見る", item.get("files") == 2)
        check("glob の外の名前は宛先に入れない", item.get("recipients") == [one])

        # --- 宣言された path が無い ------------------------------------------
        print("\n宣言された path が無い")
        code, item = run_json(build(td / "missing", {"README.md": "# x\n"}, decl))
        check("ファイルが無いことを判定にしない", item.get("verdict") == "unknown")
        check("判定できていないと返す（exit 3）", code == UNSETTLED)

    print()
    if failed:
        print(f"NG {len(failed)} 件")
        for desc in failed:
            print(f"  - {desc}")
        return 1
    print("全ケース通過")
    return 0


if __name__ == "__main__":
    sys.exit(main())
