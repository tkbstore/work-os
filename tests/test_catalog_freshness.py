#!/usr/bin/env python3
"""カタログの鮮度を確かめられることを検査する。

カタログは生成物なので、生成しなおさないかぎり静かに腐る。以前のヘッダは
件数（"82 repositories"）しか持っていなかった。件数は実在と比べられない。
実測 2026-09-26: 5 本消えて 5 本増えていたので、件数は 82 / 82 で一致していた。
**ズレていても数字が動かない**形なので、件数を見る検査を何度足しても取れない。

ここで確かめるのは3つ。
  1. 生成が出自（何を・どこで・いつ数えたか）を書き残すこと
  2. 出自が無いカタログを「通った」と言わないこと（確かめていないと言うこと）
  3. ズレたときに、どの名前がどちら側に在るかを出すこと

  python3 tests/test_catalog_freshness.py
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CATALOG_PY = ROOT / "engine" / "catalog.py"

# 判定できていないことを表す返り値。通過とも失敗とも別に扱う。
UNSETTLED = 3
IN_SYNC, UNVERIFIABLE, DRIFT = 0, 1, 2

failures: list[str] = []
checked = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global checked
    checked += 1
    if ok:
        return
    failures.append(f"{name}: {detail}" if detail else name)


def make_fleet(root: Path, names: list[str], *, without_git: tuple[str, ...] = ()) -> None:
    """仕掛けのフリートを組む。.git は空ディレクトリで足りる（存在だけを見ている）。"""
    for n in names:
        d = root / n
        d.mkdir(parents=True, exist_ok=True)
        if n not in without_git:
            (d / ".git").mkdir(exist_ok=True)


def run(registry: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "WORKOS_REGISTRY": str(registry)}
    return subprocess.run([sys.executable, str(CATALOG_PY), *args],
                          cwd=ROOT, capture_output=True, text=True,
                          timeout=120, env=env)


def out(p: subprocess.CompletedProcess) -> str:
    return p.stdout + p.stderr


# --------------------------------------------------------------------------- #
# 1. 生成が出自を書き残す
# --------------------------------------------------------------------------- #
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    fleet, reg = tmp / "fleet", tmp / "registry"
    make_fleet(fleet, ["alpha", "beta"])
    built = run(reg, str(fleet), "--build")
    check("build が通る", built.returncode == 0, out(built))

    catalog = reg / "catalog.toml"
    check("カタログが在る", catalog.is_file())
    body = catalog.read_text(encoding="utf-8") if catalog.is_file() else ""

    check("出自の表がある", "[meta]" in body, body[:200])
    check("いつ数えたかが在る", re.search(r'generated_at\s*=\s*"\d{4}-\d{2}-\d{2}T', body) is not None,
          body[:400])
    # build は根を resolve() してから書く（macOS では /var が /private/var になる）。
    check("どこで数えたかが在る", f'root         = "{fleet.resolve()}"' in body, body[:400])
    check("何本数えたかが在る", "repo_count   = 2" in body, body[:400])
    check("git を要求して数えたことが在る", "require_git  = true" in body, body[:400])
    # 件数だけを書いていた旧ヘッダの行が残っていないこと。残っていると、比べられない
    # 数字と比べられる出自が並び、どちらを見ればよいか分からなくなる。
    # 語ではなく行の形で当てる。語で当てると、出自を説明する散文に自分で当たる
    # （最初にそう書いて、実装ではなくこの検査が落ちた）。
    check("件数だけの旧ヘッダの行は無い",
          re.search(r"^#\s*\d+\s+repositories\s*$", body, re.M) is None, body[:400])

    # --- 2. 一致していれば 0。出自から根を読めるので引数は要らない ---
    v = run(reg, "--verify")
    check("一致で 0", v.returncode == IN_SYNC, f"exit={v.returncode} {out(v)}")
    check("一致と言う", "一致" in out(v), out(v))

    # --- 3. 実在が1本増えたら、その名前を出して 2 ---
    make_fleet(fleet, ["gamma"])
    v = run(reg, "--verify")
    check("増えたら 2", v.returncode == DRIFT, f"exit={v.returncode} {out(v)}")
    check("増えた名前を出す", "gamma" in out(v), out(v))

    # --- 4. 実在が1本消えたら、その名前を出して 2 ---
    (fleet / "gamma" / ".git").rmdir()
    (fleet / "gamma").rmdir()
    (fleet / "beta" / ".git").rmdir()
    (fleet / "beta").rmdir()
    v = run(reg, "--verify")
    check("消えたら 2", v.returncode == DRIFT, f"exit={v.returncode} {out(v)}")
    check("消えた名前を出す", "beta" in out(v), out(v))

# --------------------------------------------------------------------------- #
# 5. 件数が一致したままズレる（今日の実物の形）
#    ここが件数ヘッダでは絶対に取れなかった形である。
# --------------------------------------------------------------------------- #
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    fleet, reg = tmp / "fleet", tmp / "registry"
    make_fleet(fleet, ["alpha", "beta"])
    run(reg, str(fleet), "--build")
    # 1本消して1本足す。本数は 2 のまま
    (fleet / "beta" / ".git").rmdir()
    (fleet / "beta").rmdir()
    make_fleet(fleet, ["gamma"])

    v = run(reg, "--verify")
    body = reg.joinpath("catalog.toml").read_text(encoding="utf-8")
    check("本数は変わっていない（前提）", "repo_count   = 2" in body
          and len([d for d in fleet.iterdir() if (d / ".git").exists()]) == 2)
    check("件数一致でもズレを出す", v.returncode == DRIFT, f"exit={v.returncode} {out(v)}")
    check("増えた側の名前を出す", "gamma" in out(v), out(v))
    check("消えた側の名前を出す", "beta" in out(v), out(v))

# --------------------------------------------------------------------------- #
# 6. 出自が無いカタログは「通った」と言わない
# --------------------------------------------------------------------------- #
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    fleet, reg = tmp / "fleet", tmp / "registry"
    make_fleet(fleet, ["alpha"])
    run(reg, str(fleet), "--build")
    catalog = reg / "catalog.toml"
    # 出自を落とす。08-31 に生成された実物と同じ形になる
    lines = catalog.read_text(encoding="utf-8").splitlines()
    keep = [ln for ln in lines
            if not ln.startswith(("[meta]", "generated_at", "root  ", "repo_count",
                                  "require_git"))]
    catalog.write_text("\n".join(keep), encoding="utf-8")
    check("出自を落とせた（前提）", "[meta]" not in catalog.read_text(encoding="utf-8"))

    v = run(reg, "--verify")
    check("出自が無ければ 1", v.returncode == UNVERIFIABLE, f"exit={v.returncode} {out(v)}")
    check("確かめていないと言う", "確かめていません" in out(v), out(v))

    # 根を渡せば、出自が無くても突き合わせはできる
    v = run(reg, str(fleet), "--verify")
    check("根を渡せば確かめられる", v.returncode == IN_SYNC, f"exit={v.returncode} {out(v)}")

# --------------------------------------------------------------------------- #
# 7. カタログが無いとき / 根が消えたとき。どちらも 0 を返さない
# --------------------------------------------------------------------------- #
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    reg = tmp / "empty-registry"
    reg.mkdir()
    v = run(reg, "--verify")
    check("カタログが無ければ 1", v.returncode == UNVERIFIABLE, f"exit={v.returncode} {out(v)}")
    check("無いときも通ったと言わない", v.returncode != IN_SYNC, out(v))

with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    fleet, reg = tmp / "fleet", tmp / "registry"
    make_fleet(fleet, ["alpha"])
    run(reg, str(fleet), "--build")
    for d in sorted(fleet.rglob("*"), reverse=True):
        d.rmdir()
    fleet.rmdir()
    v = run(reg, "--verify")
    check("根が消えたら 1", v.returncode == UNVERIFIABLE, f"exit={v.returncode} {out(v)}")
    check("根が消えたときも通ったと言わない", v.returncode != IN_SYNC, out(v))

# --------------------------------------------------------------------------- #
# 8. .git の無いディレクトリは「定義の外」。ズレには数えないが、黙らない
# --------------------------------------------------------------------------- #
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    fleet, reg = tmp / "fleet", tmp / "registry"
    make_fleet(fleet, ["alpha", "loose"], without_git=("loose",))
    run(reg, str(fleet), "--build")
    v = run(reg, "--verify")
    check("定義の外はズレに数えない", v.returncode == IN_SYNC, f"exit={v.returncode} {out(v)}")
    check("定義の外を黙らない", "loose" in out(v), out(v))
    check("定義の外だと言う", "定義の外" in out(v), out(v))


print(f"[test_catalog_freshness] {checked} 件検査")
if failures:
    for f in failures:
        print(f"  NG {f}", file=sys.stderr)
    print(f"[test_catalog_freshness] FAIL {len(failures)} / {checked}", file=sys.stderr)
    raise SystemExit(1)
print("[test_catalog_freshness] PASS")
