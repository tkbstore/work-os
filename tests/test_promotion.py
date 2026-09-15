#!/usr/bin/env python3
"""昇格の数え方が、2つのエンジンで食い違わないことを検査する。

実際に起きたこと: validate.py と promote.py が憲法 §4（3社ルール）を別々に
実装していたため、同じ capability について validate は「違反なし」、promote は
「3社ルール違反」と言っていた。さらに promote 側は used_by の "*" を1リポジトリ
として数えており、全称宣言をするほど件数が水増しされていた。

どちらも落ちるのではなく「もっともらしい別々の答えを返す」壊れ方で、
出力を見ているだけでは気づけない。規則の実装は1つに保つ。

  python3 tests/test_promotion.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "engine"))

from promotion import caveat, collect, satisfies_rule_of_three  # noqa: E402

CAP = '''[[capability]]
id        = "sample"
path      = "work.toml"
layer     = "golden_path"
status    = "{status}"
used_by   = {used_by}
{note}invariant = ["読み取り専用であること"]
'''
WORK = '''[repo]
name        = "{name}"
domain      = "sample"
role        = "client"
status      = "active"
enforcement = "warn"
'''


def make(root: Path, name: str, used_by: str, note: str = "",
         status: str = "proposed") -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "work.toml").write_text(WORK.format(name=name), encoding="utf-8")
    (d / "capabilities.toml").write_text(
        CAP.format(used_by=used_by, status=status,
                   note=f'note      = "{note}"\n' if note else ""),
        encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(d)], check=True, timeout=30)
    return d


def flagged_by_validate(repo: Path) -> bool:
    out = subprocess.run([sys.executable, str(ROOT / "engine" / "validate.py"), str(repo)],
                         capture_output=True, text=True, timeout=60).stdout
    return "3社ルール違反" in out


def flagged_by_promote(parent: Path) -> bool:
    out = subprocess.run([sys.executable, str(ROOT / "engine" / "promote.py"), str(parent)],
                         capture_output=True, text=True, timeout=120).stdout
    return "3社ルール違反" in out


def main() -> int:
    failed: list[str] = []

    def check(desc: str, ok: bool) -> None:
        print(f"  {'OK ' if ok else 'NG '} {desc}")
        if not ok:
            failed.append(desc)

    print("\n数え方")
    check('"*" を件数に数えない', collect(["*"], ["one"]).count == 1)
    check('"*" は全称として3社ルールを満たす',
          satisfies_rule_of_three(collect(["*"], ["one"]))[0])
    check("3件あれば満たす", satisfies_rule_of_three(collect(["a", "b", "c"]))[0])
    check("2件で note 無しなら満たさない",
          not satisfies_rule_of_three(collect(["a", "b"]))[0])
    check("2件でも note があれば例外として満たす",
          satisfies_rule_of_three(collect(["a", "b"], note="理由"))[0])
    check("宣言している側のリポも証拠に数える",
          collect(["a", "b"], ["host"]).count == 3)
    check("重複は二重に数えない", collect(["a", "a"], ["a"]).count == 1)

    print("\n警告の出し分け")
    # 全件に同じ文言が付くと、鳴りっぱなしで無視される。実測で14/14がそうだった。
    check("全称かつ note 無しは黙る（設計どおりなので）",
          caveat(collect(["*"], ["one"])) == "")
    check("全称だが note があるものだけ鳴る",
          "実態は限られる" in caveat(collect(["*"], ["one"], note="理由")))
    check("全称でなければ note をそのまま出す",
          caveat(collect(["a"], note="理由")) == "理由")

    with tempfile.TemporaryDirectory() as td:
        for desc, note, expect in (("note無しの全称", "", False),
                                   ("note有りの全称", "実際の適用先は限られる", True)):
            parent = Path(td) / desc
            make(parent, "sample-repo", '["*"]', note, status="experimental")
            out = subprocess.run([sys.executable, str(ROOT / "engine" / "promote.py"),
                                  str(parent)], capture_output=True, text=True,
                                 timeout=120).stdout
            head = out.split("■ 憲法違反")[0]
            check(f"{desc}: 昇格候補に出る", "sample" in head)
            check(f"{desc}: 警告が{'出る' if expect else '出ない'}",
                  ("⚠" in head) == expect)

    print("\n2つのエンジンの一致")
    with tempfile.TemporaryDirectory() as td:
        cases = [
            ("全称宣言", '["*"]', "", False),
            ("3件", '["a", "b", "c"]', "", False),
            ("1件・note無し", '["a"]', "", True),
            ("1件・note有り", '["a"]', "宣言された例外", False),
        ]
        for i, (desc, used_by, note, should_flag) in enumerate(cases):
            parent = Path(td) / f"case{i}"
            repo = make(parent, "sample-repo", used_by, note)
            v, p = flagged_by_validate(repo), flagged_by_promote(parent)
            check(f"{desc}: validate と promote が一致する", v == p)
            check(f"{desc}: 期待どおり{'止める' if should_flag else '通す'}",
                  v == should_flag)

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
