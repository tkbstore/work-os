#!/usr/bin/env python3
r"""install.sh が、入らなかったときに入ったと言わないかを検査する。

インストーラの壊れ方は「落ちない」側に寄る。取得に失敗しても tar は空の入力で
成功することがあり、ファイルが1つ欠けても python は起動だけはする。どれも
「入った」と表示されたまま、後でそのリポジトリを検査したときに**観測が1件も
載っていない判定**が返る。ゲートを配る側がこれをやると、受け取った側は
「何も指摘されなかった」を「問題が無かった」と読む。

  python3 tests/test_installer.py

ここで見るのは4つ。

  1. 通るべきとき通る — 入れた直後に、registry を持たない利用者の経路
     （骨格だけ）で観測が載った判定が返る
  2. 落ちるべきとき落ちる — python が古い / 取得できない / 中身が欠ける /
     判定基準が空。いずれも exit 0 を返さない
  3. 失敗が前の状態を壊さない — 入れ替えの後で失敗したら前のものに戻す
  4. 形 — 変数展開の直後に非 ASCII を置かない。macOS の /bin/sh は変数名の
     切れ目をバイトで見るので `$have。` が変数名 "have。" になり、set -u の下で
     **エラー経路だけが** unbound variable で死ぬ。実際に3箇所やった。
     語を列挙せず形で見るのは、次に書かれるメッセージも同じ穴を踏むからである。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INSTALL = ROOT / "install.sh"

failed: list[str] = []


def check(desc: str, ok: bool, detail: str = "") -> None:
    if not ok:
        failed.append(desc)
    print(f"  {'OK ' if ok else 'NG '} {desc}{('  — ' + detail) if detail and not ok else ''}")


# --------------------------------------------------------------------------- #
# 配布物を作る
# --------------------------------------------------------------------------- #


def strip_lanes(text: str) -> str:
    """TOML としては読めるのに観測が1件も無い判定基準を作る。

    途中で切れた配布物はこの形になる。構文が壊れていれば読み手が落ちるので
    気づけるが、切れ目がたまたま表の境界だと、読めて空のまま install が通る。
    """
    out = []
    for line in text.splitlines(keepends=True):
        if line.startswith("[lanes.") or line.startswith("[[lanes."):
            break
        out.append(line)
    return "".join(out)


def make_tarball(dest: Path, drop: list[str] = (), empty: list[str] = (),
                 rewrite: dict = None) -> Path:
    """いま作業ツリーに在るものを GitHub の tarball と同じ形に詰める。

    HEAD ではなく作業ツリーを詰める。HEAD を使うと、まだコミットしていない
    判定基準の変更をインストーラの検査が見ないまま通す。
    """
    tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"],
                             capture_output=True, text=True, check=True)
    stage = dest / "work-os-main"
    for rel in filter(None, tracked.stdout.split("\0")):
        if any(rel == d or rel.startswith(d.rstrip("/") + "/") for d in drop):
            continue
        src = ROOT / rel
        if not src.is_file():          # submodule・消したファイル
            continue
        out = stage / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        if rel in empty:
            out.write_text("", encoding="utf-8")
        elif rewrite and rel in rewrite:
            out.write_text(rewrite[rel](src.read_text(encoding="utf-8")), encoding="utf-8")
        else:
            shutil.copy2(src, out)
    tar = dest / "payload.tar.gz"
    subprocess.run(["tar", "-czf", str(tar), "-C", str(dest), "work-os-main"], check=True)
    shutil.rmtree(stage)
    return tar


def run_install(tmp: Path, tarball: Path, name: str, **env_extra) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("WORKOS_GATE_PYTHON", None)
    env.update({
        "WORKOS_GATE_TARBALL": f"file://{tarball}",
        "WORKOS_GATE_HOME": str(tmp / name / "share"),
        "WORKOS_GATE_BIN": str(tmp / name / "bin"),
    })
    env.update({k: str(v) for k, v in env_extra.items()})
    return subprocess.run(["sh", str(INSTALL)], capture_output=True, text=True,
                          env=env, cwd=str(tmp), timeout=120)


def fake_old_python(tmp: Path) -> Path:
    """3.8 を名乗る python。版の判定にだけ答える。"""
    p = tmp / "fake-python3"
    p.write_text(
        "#!/bin/sh\n"
        'case "${2:-}" in\n'
        "  *sys.exit*) exit 1 ;;\n"
        '  *print*)    echo "3.8.10"; exit 0 ;;\n'
        "esac\n"
        "exit 0\n", encoding="utf-8")
    p.chmod(0o755)
    return p


# --------------------------------------------------------------------------- #


def main() -> int:
    if not INSTALL.is_file():
        print(f"{INSTALL} が無い", file=sys.stderr)
        return 1
    if shutil.which("git") is None or shutil.which("tar") is None:
        print("git / tar が無い環境では配布物を組み立てられない。skip")
        return 3

    print("形 — 変数展開の直後に非 ASCII を置かない（macOS の sh が変数名に食う）")
    text = INSTALL.read_text(encoding="utf-8")
    bare = [m.group(0) for m in re.finditer(r"\$[A-Za-z_][A-Za-z0-9_]*(?=[^\x00-\x7f])", text)]
    check("括っていない変数展開が無い", not bare, f"{len(bare)} 箇所: {', '.join(bare[:5])}")
    check("sh -n が通る",
          subprocess.run(["sh", "-n", str(INSTALL)], capture_output=True).returncode == 0)

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        good = make_tarball(tmp / "good", )
        (tmp / "good").mkdir(exist_ok=True)

        print("\n通るべきとき — registry を持たない利用者の初回")
        r = run_install(tmp, good, "ok")
        check("exit 0", r.returncode == 0, r.stderr.strip()[-200:])
        shim = tmp / "ok" / "bin" / "workos-gate"
        check("shim が実行可能で在る", shim.is_file() and os.access(shim, os.X_OK))
        check("gate の実体が在る", (tmp / "ok" / "share" / "engine" / "release_gate.py").is_file())
        check("観測が載ったと報告した", "観測" in r.stdout and "レーン" in r.stdout, r.stdout[-200:])

        # 報告を信じない。shim を別のリポジトリに当てて、観測が実際に載るか見る。
        target = tmp / "probe-target"
        target.mkdir()
        env = dict(os.environ, WORKOS_REGISTRY=str(tmp / "no-registry"))
        env.pop("WORKOS_GATE_PYTHON", None)
        out = subprocess.run([str(shim), str(target), "--json"],
                             capture_output=True, text=True, env=env, timeout=120)
        import json
        try:
            verdict = json.loads(out.stdout)
            findings = sum(len(ln.get("findings") or [])
                           for ln in (verdict[0].get("lanes") or []))
        except Exception as exc:                    # noqa: BLE001
            verdict, findings = None, 0
            print(f"       （判定が読めない: {exc}）")
        check("入れた shim が骨格だけで観測を返す", findings > 0, f"観測 {findings} 件")

        print("\n落ちるべきとき — どれも exit 0 を返してはいけない")

        r = run_install(tmp, good, "old", WORKOS_GATE_PYTHON=fake_old_python(tmp))
        check("python が 3.8 なら落ちる", r.returncode != 0)
        check("  そのとき実体を作らない", not (tmp / "old" / "share").exists())

        r = run_install(tmp, tmp / "missing.tar.gz", "gone")
        check("取得できなければ落ちる", r.returncode != 0)
        # 取得と展開をパイプで繋ぐと、取れなかったことが「中身が違う」として出る。
        check("  取得の失敗として報告する", "取得できなかった" in r.stderr, r.stderr.strip()[-160:])

        noconf = make_tarball(tmp / "noconf", drop=["config"])
        r = run_install(tmp, noconf, "noconf")
        check("判定基準のファイルが無ければ落ちる", r.returncode != 0)

        hollow = make_tarball(tmp / "hollow", empty=["config/release_lanes.toml"])
        r = run_install(tmp, hollow, "hollow")
        check("判定基準が読めなければ落ちる", r.returncode != 0, r.stdout[-160:])
        check("  そのとき実体を作らない", not (tmp / "hollow" / "share").exists())

        # 読めて空。構文が壊れていないので、読み手は何も言わずに 0 件を返す。
        cut = make_tarball(tmp / "cut", rewrite={"config/release_lanes.toml": strip_lanes})
        r = run_install(tmp, cut, "cut")
        check("判定基準が読めても観測が0件なら落ちる", r.returncode != 0, r.stdout[-160:])
        check("  そのとき実体を作らない", not (tmp / "cut" / "share").exists())

        print("\n失敗が、前に入っていたものを壊さないこと")
        r = run_install(tmp, good, "up")
        check("下地の install が通る", r.returncode == 0)
        marker = tmp / "up" / "share" / "MARKER"
        marker.write_text("v1", encoding="utf-8")

        # shim を置けない状態にする。BIN_DIR には書けるので、入れ替えの後で失敗する。
        (tmp / "up" / "bin" / "workos-gate").chmod(0o500)
        r = run_install(tmp, good, "up")
        check("shim を置けなければ落ちる", r.returncode != 0)
        check("  前に入っていたものが残る", marker.is_file(), "MARKER が消えた")
        # 木だけ戻して shim を消すと、利用者から見れば壊れたままである。
        up_shim = tmp / "up" / "bin" / "workos-gate"
        check("  前に入っていた shim も残る", up_shim.is_file() and os.access(up_shim, os.X_OK))
        check("  戻した shim が判定を返す",
              subprocess.run([str(up_shim), str(target), "--json"], capture_output=True,
                             text=True, env=env, timeout=120).stdout.startswith("["))

        # $SHIM がディレクトリのときは、入れ替える前に落ちること。
        up_shim.unlink()
        up_shim.mkdir()
        r = run_install(tmp, good, "up")
        check("shim の場所がディレクトリなら落ちる", r.returncode != 0)
        check("  前に入っていたものが残る", marker.is_file(), "MARKER が消えた")

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
