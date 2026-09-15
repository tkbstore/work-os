#!/usr/bin/env python3
"""観測できなかったことが、観測できたことに化けないかを検査する。

fleet.observe は git のコミット日を読む。読めなかったとき、以前は例外を
握りつぶして days_idle を既定値のまま残していた。既定値は -1 なので放置判定に
勝つことは無く、実害は出ていなかった。ただし表には -1 がそのまま出ていて、
読み手には「観測に失敗した」ではなく「意味不明な数」に見えていた。
握りつぶしの問題は誤動作ではなく、後から来た人に理由が残らないことにある。

  python3 tests/test_fleet.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))
import fleet  # noqa: E402

failed: list[str] = []


def check(desc: str, ok: bool) -> None:
    if not ok:
        failed.append(desc)
    print(f"  {'OK ' if ok else 'NG '} {desc}")


def make_repo(tmp: Path) -> Path:
    """コミットが1つある git リポジトリを作る。"""
    repo = tmp / "sample"
    repo.mkdir()
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
           "PATH": "/usr/bin:/bin:/usr/local/bin"}
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True, env=env)
    (repo / "a.txt").write_text("x")
    subprocess.run(["git", "-C", str(repo), "add", "a.txt"], check=True, env=env)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "first"],
                   check=True, env=env)
    return repo


def main() -> int:
    print("日付の読み取り")
    check("YYYY-MM-DD を日付にする",
          fleet.parse_day("2026-08-26") == date(2026, 8, 26))
    check("読めない文字列は None（例外を外に出さない）",
          fleet.parse_day("not-a-date") is None)
    check("空文字は None（git が何も返さなかった場合）",
          fleet.parse_day("") is None)

    print("\n観測できなかったことの表し方")
    check("既定値は 0 ではない（今日コミットされたと区別がつく）",
          fleet.UNKNOWN_IDLE < 0)
    check("不明は数値として出さない", fleet.idle_text(fleet.UNKNOWN_IDLE) == "?")
    check("既知の日数はそのまま出す", fleet.idle_text(3) == "3")
    check("単位を付けても不明は不明のまま",
          fleet.idle_text(fleet.UNKNOWN_IDLE, "日前") == "?")
    check("単位を付けた既知の日数", fleet.idle_text(3, "日前") == "3日前")

    print("\n不明が放置判定に化けない")
    unknown = fleet.RepoState(name="x", days_idle=fleet.UNKNOWN_IDLE,
                              has_readme=True, has_work_toml=True, has_remote=True)
    unknown.score()
    check("日付が不明なリポを放置と呼ばない",
          not any("放置" in r for r in unknown.reasons))
    stale = fleet.RepoState(name="y", days_idle=fleet.STALE_DAYS,
                            has_readme=True, has_work_toml=True, has_remote=True)
    stale.score()
    check("実際に放置されたリポは放置と呼ぶ",
          any("放置" in r for r in stale.reasons))

    print("\n実物の git リポジトリ")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        repo = make_repo(tmp)
        s = fleet.observe(repo, date.today())
        check("git リポジトリを観測できる", s is not None)
        check("今日のコミットは 0 日", s is not None and s.days_idle == 0)
        check("コミット日を保持している",
              s is not None and s.last_commit == date.today().isoformat())
        plain = tmp / "not-a-repo"
        plain.mkdir()
        check("git でないディレクトリは観測対象にしない",
              fleet.observe(plain, date.today()) is None)

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
