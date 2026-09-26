#!/usr/bin/env python3
r"""abstraction_gate が、自分の公開先だけを所在として差し引くかを検査する。

配布の入口（install.sh の取得元）は具体でなければ機能しない。一方で
abstraction_gate は「仕組みの層に固有名詞を置かない」を守る。両立させるために、
**公開を宣言したリポジトリが自分の所在を書くこと**だけを漏れから外した。

外しすぎると、その語がどこに出ても素通りする。外さなすぎると install.sh が
置けない。ここで見るのは、差し引く範囲が slug の内側に閉じていることである。

検査は合成したリポジトリに対して行う。実物の work-os の宣言や remote の
現在地に依存させると、そこを触った日に前提が消えて2通りに壊れる（2026-09-26 に
test_audit で実際に起きた）。

  python3 tests/test_abstraction_self_ref.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

failed: list[str] = []


def check(desc: str, ok: bool, detail: str = "") -> None:
    if not ok:
        failed.append(desc)
    print(f"  {'OK ' if ok else 'NG '} {desc}{('  — ' + detail) if detail and not ok else ''}")


TERM = "acme-org"          # 実在の組織名を借りない
REPO_NAME = "widget-os"
SLUG = f"{TERM}/{REPO_NAME}"


def build(tmp: Path, *, intent: str, remote: str | None, lines: list[str]) -> tuple[Path, Path]:
    """仕組みの層だけを持つリポジトリを合成する。"""
    repo = tmp / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    shutil.copytree(ROOT / "engine", repo / "engine",
                    ignore=shutil.ignore_patterns("__pycache__"))
    (repo / "work.toml").write_text(
        f'[repo]\nname = "{REPO_NAME}"\n\n[publish]\nintent = "{intent}"\n', encoding="utf-8")
    (repo / "NOTES.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    reg = tmp / "registry"
    reg.mkdir(exist_ok=True)
    (reg / "private_terms.toml").write_text(
        f'[orgs]\nnames = ["{TERM}"]\n', encoding="utf-8")

    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    if remote:
        subprocess.run(["git", "remote", "add", "origin", remote], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True,
                   capture_output=True)
    return repo, reg


def gate(repo: Path, reg: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ, WORKOS_REGISTRY=str(reg))
    return subprocess.run([sys.executable, str(repo / "engine" / "abstraction_gate.py")],
                          capture_output=True, text=True, env=env, timeout=120)


def case(desc: str, *, intent: str, remote: str | None, lines: list[str],
         expect_leak: bool) -> None:
    with tempfile.TemporaryDirectory() as td:
        repo, reg = build(Path(td), intent=intent, remote=remote, lines=lines)
        r = gate(repo, reg)
        leaked = r.returncode == 1
        check(desc, leaked == expect_leak,
              f"exit={r.returncode} / {(r.stdout + r.stderr).strip().splitlines()[:1]}")


def main() -> int:
    if shutil.which("git") is None:
        print("git が無い環境では合成できない。skip")
        return 3

    url = f"https://github.com/{SLUG}.git"
    print("公開を宣言していれば、自分の所在は漏れではない")
    case("slug の内側は漏れにしない", intent="public", remote=url,
         lines=[f"curl -fsSL https://github.com/{SLUG}/archive/main.tar.gz"],
         expect_leak=False)
    case("slug そのものも漏れにしない", intent="public", remote=url,
         lines=[f'REPO="{SLUG}"'], expect_leak=False)

    print("\n外しすぎていないこと")
    case("語が単独で出たら漏れのまま", intent="public", remote=url,
         lines=[f"顧客 {TERM} の案件"], expect_leak=True)
    case("同じ行に単独の語が混ざっていれば漏れ", intent="public", remote=url,
         lines=[f"https://github.com/{SLUG} と {TERM} の話"], expect_leak=True)
    case("別の org の slug は外さない", intent="public", remote=url,
         lines=[f"https://github.com/{TERM}/other-repo"], expect_leak=True)

    print("\n公開を宣言していないなら、所在も知られていない前提で扱う")
    case("intent=internal なら外さない", intent="internal", remote=url,
         lines=[f'REPO="{SLUG}"'], expect_leak=True)
    case("remote が無いなら外さない", intent="public", remote=None,
         lines=[f'REPO="{SLUG}"'], expect_leak=True)
    case("github 以外の remote なら外さない", intent="public",
         remote=f"https://gitlab.example.com/{SLUG}.git",
         lines=[f'REPO="{SLUG}"'], expect_leak=True)

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
