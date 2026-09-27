#!/usr/bin/env python3
"""傘（engine/cli.py）が、入口を形で拾い、argv をそのまま渡すことを検査する。

傘の壊れ方は2つあり、どちらも**落ちずに通る**。

1. 入口の一覧を名前で列挙してしまう。次に足したモジュールが一覧に出ず、
   呼べないまま在る。足した本人以外には存在しないのと同じになる。
2. import して main() を呼ぶ。main() の署名は現に3種に分かれているので、
   署名が変わった入口が**傘からだけ**壊れる。直接呼ぶと動くので気づけない。

だから「一覧 == ディスクにある走る入口」を毎回突き合わせ、呼び出しは
subprocess の終了コードで見る。

  python3 tests/test_cli.py
"""

from __future__ import annotations

import ast
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENGINE = ROOT / "engine"
CLI_PY = ENGINE / "cli.py"

sys.path.insert(0, str(ENGINE))
import cli  # noqa: E402

failures: list[str] = []
checked = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global checked
    checked += 1
    if not ok:
        failures.append(f"{name}: {detail}" if detail else name)


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(CLI_PY), *args],
                          capture_output=True, text=True)


# --------------------------------------------------------------------------- #
# 一覧は、ディスクにある「走る入口」と一致する
# --------------------------------------------------------------------------- #

def guarded_on_disk() -> set[str]:
    """テスト側で独立に数え直す。傘の実装を借りると同じ誤りで一致してしまう。"""
    names = set()
    for p in sorted(ENGINE.glob("*.py")):
        if p.name.startswith("_") or p.name == "cli.py":
            continue
        src = p.read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in tree.body:
            if isinstance(node, ast.If) and "__name__" in ast.dump(node.test):
                names.add(p.stem)
    return names


disk = guarded_on_disk()
found = set(cli.entries())
check("一覧がディスクの走る入口と一致する", found == disk,
      f"傘だけ={sorted(found - disk)} ディスクだけ={sorted(disk - found)}")
check("入口が1つ以上ある", len(found) > 1, str(sorted(found)))

out = run().stdout
check("引数なしで一覧を出す（終了コード0）", run().returncode == 0)
for name in sorted(disk):
    check(f"一覧に {name} が出る", name.replace("_", "-") in out, out[:400])

# 走らない .py は一覧に出さない。走らせても何も起きないものが並ぶと、
# 一覧が「呼べるもの」の答えでなくなる。
not_runnable = sorted({p.stem for p in ENGINE.glob("*.py")} - disk - {"cli"})
check("走らない .py が1つ以上ある（この検査が空回りしていない）",
      len(not_runnable) > 0, str(not_runnable))
for name in not_runnable:
    check(f"走らない {name} を一覧に出さない", name not in found, str(sorted(found)))

# --------------------------------------------------------------------------- #
# 形で見ている証拠: docstring や comment の中の __name__ を拾わない
# --------------------------------------------------------------------------- #

with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    decoy = tmp / "decoy.py"
    decoy.write_text(
        '"""__name__ == "__main__" と書いてあるだけの docstring。"""\n'
        '# if __name__ == "__main__": も comment にある\n'
        'x = 1\n', encoding="utf-8")
    check("docstring と comment の __name__ を入口と数えない",
          not cli.is_entry(decoy))

    real = tmp / "real.py"
    real.write_text('if __name__ == "__main__":\n    pass\n', encoding="utf-8")
    check("__main__ ガードを持てば入口と数える", cli.is_entry(real))

    broken = tmp / "broken.py"
    broken.write_text("def f(:\n", encoding="utf-8")
    check("構文が壊れたファイルで例外を投げず False を返す", cli.is_entry(broken) is False)

# --------------------------------------------------------------------------- #
# 一行説明は実体の docstring から取る
# --------------------------------------------------------------------------- #

with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    for fname, body, want in [
        ("a.py", '"""a.py — 説明である。"""\n', "説明である。"),
        ("b.py", '"""説明だけの1行目。\n\n続き\n"""\n', "説明だけの1行目。"),
        ("c.py", "x = 1\n", "（説明なし）"),
        ("d.py", '"""d.py — """\n', "（説明なし）"),
    ]:
        f = tmp / fname
        f.write_text(body, encoding="utf-8")
        got = cli.summary(f)
        check(f"{fname} の一行説明", got == want, f"{got!r} != {want!r}")

for name, path in cli.entries().items():
    check(f"{name} の一行説明が空でない", cli.summary(path).strip() != "", name)

# --------------------------------------------------------------------------- #
# 名前の与え方
# --------------------------------------------------------------------------- #

check("ハイフンとアンダースコアを同じ名前として扱う",
      cli.normalize("release-gate") == cli.normalize("release_gate") == "release_gate")
check("前後の空白を落とす", cli.normalize("  scan \n") == "scan")

bad = run("そんな入口はない")
check("知らない名前は終了コード2", bad.returncode == 2, str(bad.returncode))
check("知らない名前でも一覧を出す（何が呼べるか分かるように）",
      "validate" in bad.stderr, bad.stderr[:300])
check("知らない名前の案内は stdout を汚さない", bad.stdout == "", bad.stdout[:200])

# --------------------------------------------------------------------------- #
# argv をそのまま渡し、終了コードを透過する
# --------------------------------------------------------------------------- #

direct = subprocess.run([sys.executable, str(ENGINE / "validate.py"), "--help"],
                        capture_output=True, text=True)
viaumb = run("validate", "--help")
check("--help が入口に届いている", viaumb.returncode == direct.returncode,
      f"傘={viaumb.returncode} 直接={direct.returncode}")
check("--help の出力が直接呼びと同じ", viaumb.stdout == direct.stdout,
      f"傘={viaumb.stdout[:200]!r}")

# 終了コードが 0 以外になる呼び方で、0 に潰していないことを見る。
#
# **直接呼びが 0 以外であることを先に検査する。** 両方 0 の題材で比べると、
# 傘が常に 0 を返す欠陥でも一致して通る（この検査を書いた最初の版が実際にそうで、
# 欠陥を戻す確認で初めて分かった）。比較する検査は、比較する値が動くことを
# 確かめないと空回りする。
BAD_FLAG = "--no-such-flag-exists"
# 題材は argparse を持つ入口にする。validate.py は引数を path として受けるので
# 知らないフラグでも 0 を返し、題材にすると比較が動かない。
d = subprocess.run([sys.executable, str(ENGINE / "scan.py"), BAD_FLAG],
                   capture_output=True, text=True)
check("題材の直接呼びが 0 以外を返す（この検査が空回りしていない）",
      d.returncode != 0, str(d.returncode))
u = run("scan", BAD_FLAG)
check("0 以外の終了コードを潰さない", u.returncode == d.returncode,
      f"傘={u.returncode} 直接={d.returncode}")

print(f"[test_cli] {checked} 件検査")
if failures:
    for f in failures:
        print(f"  NG {f}", file=sys.stderr)
    print(f"[test_cli] FAIL {len(failures)} / {checked}", file=sys.stderr)
    raise SystemExit(1)
print("[test_cli] PASS")
