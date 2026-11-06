#!/usr/bin/env python3
"""群を増やす道具（engine/sample_corpus.py）が、宣言どおりにだけ引くことを検査する。

壊れ方は3つある。宣言に無い条件で選ぶ（個人アカウントが混ざる・fork が混ざる）、
もう居るものを引き直して本数を水増しする、corpus.toml を書くときに前の集合や
コメントを壊す。どれも結果の数字は出るので、数字からは気づけない。

ネットワークには出ない。検索と木の取得は差し替える。

  python3 tests/test_sample_corpus.py
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "engine"))

# E402 の抑制。engine/ は上の sys.path の後でしか解決しない
import calibrate  # noqa: E402
# E402 の抑制。理由は上と同じ（engine/ は sys.path の後でしか解決しない）
import sample_corpus  # noqa: E402

failed: list[str] = []


def check(desc: str, ok: bool, detail: str = "") -> None:
    if not ok:
        failed.append(desc + (f" — {detail}" if detail else ""))
    print(f"  {'OK ' if ok else 'NG '} {desc}{('  ' + detail) if detail and not ok else ''}")


def repo(slug: str, owner_type: str = "Organization", **kw) -> dict:
    return {"full_name": slug, "owner": {"type": owner_type}, "fork": False,
            "archived": False, **kw}


def pages(*batches: list[dict]):
    """ページ番号ごとに返す検索。呼ばれたページを記録する。"""
    calls: list[int] = []

    def search(q: str, sort: str, page: int) -> list[dict]:
        calls.append(page)
        return batches[page - 1] if page <= len(batches) else []
    return search, calls


def tree_of(table: dict[str, tuple[list[str], bool]]):
    return lambda slug: table.get(slug, (["pyproject.toml"], False))


MANIFESTS = ["pyproject.toml", "package.json"]
ORG = {"q": "x", "sort": "updated", "owner_type": "Organization"}
ANY = {"q": "x", "sort": "stars", "owner_type": ""}


def check_draw() -> None:
    search, _ = pages([repo("o/a"), repo("u/b", "User"), repo("o/c", fork=True),
                       repo("o/d", archived=True), repo("o/e")])
    got = sample_corpus.draw(ORG, 10, set(), MANIFESTS, search, tree_of({}))
    check("個人アカウント・fork・archive を引かない", got == ["o/a", "o/e"], str(got))

    search, _ = pages([repo("u/b", "User"), repo("o/a")])
    got = sample_corpus.draw(ANY, 10, set(), MANIFESTS, search, tree_of({}))
    check("owner_type が空なら個人アカウントも引く", got == ["u/b", "o/a"], str(got))

    search, _ = pages([repo("o/a"), repo("o/b"), repo("o/a")])
    got = sample_corpus.draw(ORG, 10, {"o/a"}, MANIFESTS, search, tree_of({}))
    check("もう居るものと、同じ回の重複を引かない", got == ["o/b"], str(got))

    search, _ = pages([repo("o/a"), repo("o/b"), repo("o/c")])
    got = sample_corpus.draw(ORG, 2, set(), MANIFESTS, search, tree_of({}))
    check("n 本で止める", got == ["o/a", "o/b"], str(got))

    full = [repo(f"o/p1-{i}") for i in range(sample_corpus.PER_PAGE)]
    search, calls = pages(full, [repo("o/next")])
    got = sample_corpus.draw(ORG, sample_corpus.PER_PAGE + 1, set(), MANIFESTS, search,
                             tree_of({}))
    check("1 ページで足りなければ次のページを引く",
          got[-1:] == ["o/next"] and calls == [1, 2], f"calls={calls}")

    search, calls = pages([repo("o/a")])
    sample_corpus.draw(ORG, 10, set(), MANIFESTS, search, tree_of({}))
    check("最後のページ（100 件未満）の先は引かない", calls == [1], f"calls={calls}")

    capped = {**ORG, "max_per_owner": 1}
    search, _ = pages([repo("o/a"), repo("O/b"), repo("p/c"), repo("q/d")])
    got = sample_corpus.draw(capped, 10, set(), MANIFESTS, search, tree_of({}),
                             members=["q/old"])
    check("1 つの所有者から上限までしか引かない（大文字小文字は同じ所有者、群に居る分も数える）",
          got == ["o/a", "p/c"], str(got))
    search, _ = pages([repo("o/a"), repo("o/b")])
    got = sample_corpus.draw(ORG, 10, set(), MANIFESTS, search, tree_of({}))
    check("上限を宣言しなければ同じ所有者からも引く", got == ["o/a", "o/b"], str(got))


def check_manifest_filter() -> None:
    table = {"o/list": (["README.md", "docs/a.md"], False),
             "o/nested": (["README.md", "lean/lakefile.toml", "pkg/package.json"], False),
             "o/huge": (["README.md"], True)}
    search, _ = pages([repo("o/list"), repo("o/nested"), repo("o/huge")])
    got = sample_corpus.draw(ORG, 10, set(), MANIFESTS, search, tree_of(table))
    check("木のどこにもマニフェストが無いものだけを落とす", got == ["o/nested", "o/huge"],
          str(got))
    check("打ち切られた木は、無いと言えないので残す",
          sample_corpus.may_have_manifest(["README.md"], True, MANIFESTS))
    check("名前の一部が当たっただけでは在ると言わない",
          not sample_corpus.may_have_manifest(["not-package.json.md"], False, MANIFESTS))
    search, _ = pages([repo("o/list")])
    got = sample_corpus.draw(ORG, 10, set(), [], search, tree_of(table))
    check("足切りを課さない群は木を見ない", got == ["o/list"], str(got))


def check_append() -> None:
    text = calibrate.CORPUS_FILE.read_text(encoding="utf-8")
    before = tomllib.loads(text)
    out = sample_corpus.checked_append(text, "not_adopted", ["o/new1", "o/new2"], "2099-01-01")
    after = tomllib.loads(out)
    check("足したものが群の末尾に増える",
          after["groups"]["not_adopted"]["slugs"]
          == before["groups"]["not_adopted"]["slugs"] + ["o/new1", "o/new2"])
    check("他の群は変わらない",
          all(after["groups"][g]["slugs"] == before["groups"][g]["slugs"]
              for g in before["groups"] if g != "not_adopted"))
    check("frozen_on を進める（前回の結果を上書きしない）",
          after["corpus"]["frozen_on"] == "2099-01-01")
    check("コメントを落とさない",
          sum(1 for line in text.splitlines() if line.lstrip().startswith("#"))
          == sum(1 for line in out.splitlines() if line.lstrip().startswith("#")))
    check("どの回に引いたかを行に残す", "o/new1\",  # 2099-01-01 に draw で引いた" in out)

    existing = before["groups"]["adopted"]["slugs"][0]
    try:
        sample_corpus.checked_append(text, "not_adopted", [existing], "2099-01-01")
        check("別の群に居るものを足そうとしたら止める", False)
    except ValueError:
        check("別の群に居るものを足そうとしたら止める", True)
    try:
        sample_corpus.checked_append(text, "nope", ["o/x"], "2099-01-01")
        check("無い群には書かない", False)
    except (ValueError, KeyError):
        check("無い群には書かない", True)


def check_declarations() -> None:
    corpus = calibrate.load_corpus()
    drawn = {n: g["draw"] for n, g in corpus["groups"].items() if "draw" in g}
    check("引き方を宣言した群がある", {"adopted", "not_adopted"} <= set(drawn),
          str(sorted(drawn)))
    for name, d in drawn.items():
        problems = sample_corpus.lint_draw(name, d)
        check(f"{name}: draw の宣言に矛盾が無い", not problems, "; ".join(problems))
    check("個人アカウントを除く群は owner_type で除いている",
          drawn.get("not_adopted", {}).get("owner_type") == "Organization")
    check("sort の書き違いを止める",
          bool(sample_corpus.lint_draw("x", {"q": "a", "sort": "star"})))
    check("q が空なら止める", bool(sample_corpus.lint_draw("x", {"q": " ", "sort": "stars"})))
    check("max_per_owner の負の値を止める",
          bool(sample_corpus.lint_draw("x", {"q": "a", "sort": "stars", "max_per_owner": -1})))
    check("採られていない群は 1 つの所有者から 1 本まで",
          drawn.get("not_adopted", {}).get("max_per_owner") == 1)


def main() -> int:
    print("宣言どおりに引く")
    check_draw()
    print("\n動かすものでないものを先に落とす")
    check_manifest_filter()
    print("\ncorpus.toml に足す")
    check_append()
    print("\n引き方の宣言")
    check_declarations()
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
