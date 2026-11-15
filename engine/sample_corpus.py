#!/usr/bin/env python3
"""校正コーパスの群を、群に書いた引き方のとおりに増やす。

群が分けているかどうかは、群の大きさで決まる。採られていない群が 6 本のとき、
1 本の出入りで通過率が 17 ポイント動き、min_gap 0.40 の判定は偶然と区別できない
（2026-10-28 の結果で「分けている」は issue_template の 1 件だけだった）。
検査を足す前に、比べる本数を足す。

手で選ぶと、選んだ人の目が群に混ざる。だから corpus.toml の各群に [draw] として
検索の条件を宣言し、ここはそれを機械的に実行するだけにする。宣言の無い群
（agent_native・landmark・formal）は手で選ぶ群なので、ここでは触らない。

増やすときは **足すだけで、差し替えない**（corpus.toml の note）。前回の集合を
残しておけば、同じ集合で測り直すことも、足した分の効き目を見ることもできる。
足したら frozen_on を今日に進める。同じ日付のままだと前回の結果を上書きする。

  python3 engine/sample_corpus.py --group not_adopted --n 10          候補を出すだけ
  python3 engine/sample_corpus.py --group not_adopted --n 10 --write  corpus.toml に足す

取得は gh api（search/repositories）。測る（clone して判定する）のは calibrate.py の仕事で、
manifest_required の足切りもそちらで理由付きで残る。ここで落とすのは、木のどこにも
マニフェストの名前が無いもの（star 順の上位を占めるリンク集・書籍）だけである。
calibrate で足切りされたものも slugs には残るので、次に同じものを引き直すことはない。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

# E402 の抑制。calibrate は上の sys.path の後でしか解決しない
import calibrate  # noqa: E402

# 1 ページ 100 件。Search API は 1000 件で打ち切るので 10 ページが上限。
PER_PAGE = 100
MAX_PAGES = 10
SORTS = {"stars", "updated", "forks"}
OWNER_TYPES = {"", "Organization", "User"}

Search = Callable[[str, str, int], list[dict]]
Tree = Callable[[str], tuple[list[str], bool]]


def gh_search(q: str, sort: str, page: int) -> list[dict]:
    """1 ページ分の検索結果。失敗は黙らずに止める（0 件を「居ない」と読まないため）。"""
    proc = subprocess.run(
        ["gh", "api", "-X", "GET", "search/repositories", "-f", f"q={q}",
         "-f", f"sort={sort}", "-f", "order=desc", "-f", f"per_page={PER_PAGE}",
         "-f", f"page={page}"],
        capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(f"gh api が失敗しました: {proc.stderr.strip()[:300]}")
    return json.loads(proc.stdout).get("items", [])


def lint_draw(name: str, draw_cfg: dict) -> list[str]:
    """[draw] の宣言の矛盾。空なら健全。"""
    problems = []
    if not str(draw_cfg.get("q", "")).strip():
        problems.append(f"{name}: draw.q が空")
    if draw_cfg.get("sort") not in SORTS:
        problems.append(f"{name}: draw.sort は {sorted(SORTS)} のどれか")
    if draw_cfg.get("owner_type", "") not in OWNER_TYPES:
        problems.append(f"{name}: draw.owner_type は {sorted(OWNER_TYPES)} のどれか")
    per_owner = draw_cfg.get("max_per_owner", 0)
    if not isinstance(per_owner, int) or per_owner < 0:
        problems.append(f"{name}: draw.max_per_owner は 0 以上の整数（0 は上限なし）")
    return problems


def owner(slug: str) -> str:
    return slug.split("/", 1)[0].lower()


def known_slugs(corpus: dict) -> set[str]:
    """どれかの群にもう居るもの。測って除外したものも slugs に残っている。"""
    return {s for g in corpus.get("groups", {}).values() for s in g.get("slugs", [])}


def gh_tree(slug: str) -> tuple[list[str], bool]:
    """既定ブランチの木の道と、打ち切られたか。失敗は黙らずに止める。"""
    proc = subprocess.run(
        ["gh", "api", f"repos/{slug}/git/trees/HEAD?recursive=1"],
        capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(f"{slug} の木を取れません: {proc.stderr.strip()[:300]}")
    data = json.loads(proc.stdout)
    return [t.get("path", "") for t in data.get("tree", [])], bool(data.get("truncated"))


def may_have_manifest(paths: list[str], truncated: bool, manifests: list[str]) -> bool:
    """木のどこかにマニフェストの名前が在るか。calibrate.has_manifest より緩い必要条件。

    ここで落とすのは「どこにも無いので calibrate が必ず外すもの」だけにする。
    vendored・fixtures の除外やワークスペースの判定まで写すと、物差しが2本になる。
    木が打ち切られていたら、無いとは言えないので残す。
    """
    if truncated:
        return True
    names = set(manifests)
    return any(path.rsplit("/", 1)[-1] in names for path in paths)


def eligible(item: dict, owner_type: str) -> bool:
    if item.get("fork") or item.get("archived"):
        return False
    return not owner_type or item.get("owner", {}).get("type") == owner_type


def draw(draw_cfg: dict, n: int, skip: set[str], manifests: list[str],
         search: Search = gh_search, tree: Tree = gh_tree,
         members: list[str] | None = None) -> list[str]:
    """宣言どおりに検索し、まだ居ないものを並び順のまま n 本まで返す。"""
    owner_type = draw_cfg.get("owner_type", "")
    # 群に居る分も数える。前の回に引いた所有者から、また引かない
    per_owner = draw_cfg.get("max_per_owner", 0)
    owners = [owner(s) for s in members or []]
    picked: list[str] = []
    for page in range(1, MAX_PAGES + 1):
        items = search(draw_cfg["q"], draw_cfg["sort"], page)
        for item in items:
            slug = item.get("full_name", "")
            if not slug or slug in skip or slug in picked or not eligible(item, owner_type):
                continue
            if per_owner and owners.count(owner(slug)) >= per_owner:
                continue
            if manifests and not may_have_manifest(*tree(slug), manifests):
                continue
            picked.append(slug)
            owners.append(owner(slug))
            if len(picked) >= n:
                return picked
        if len(items) < PER_PAGE:
            break
    return picked


def append_slugs(text: str, group: str, slugs: list[str], today: str) -> str:
    """corpus.toml の本文に slug を足し、frozen_on を進めた本文を返す。

    tomllib は書けないので本文を直す。コメントは宣言の根拠なので、読み書きで落とさない。
    """
    head = re.search(rf"^\[groups\.{re.escape(group)}\]\s*$", text, re.M)
    if not head:
        raise ValueError(f"[groups.{group}] がありません")
    start = text.index("slugs = [", head.end())
    end = text.index("\n]", start)
    added = "".join(f'\n  "{s}",  # {today} に draw で引いた' for s in slugs)
    out = text[:end] + added + text[end:]
    out, count = re.subn(r'^frozen_on = "[^"]*"', f'frozen_on = "{today}"', out, flags=re.M)
    if count != 1:
        raise ValueError("frozen_on が1つではありません")
    return out


def checked_append(text: str, group: str, slugs: list[str], today: str) -> str:
    """足した結果を読み直し、足したものだけが増えたことを確かめてから返す。"""
    before = tomllib.loads(text)["groups"]
    out = append_slugs(text, group, slugs, today)
    after = tomllib.loads(out)
    expect = {name: g["slugs"] + (slugs if name == group else [])
              for name, g in before.items()}
    if {name: g["slugs"] for name, g in after["groups"].items()} != expect:
        raise ValueError("足した後の slugs が、足す前＋足したもの と一致しません")
    problems = calibrate.lint_corpus(after)
    if problems:
        raise ValueError("足した後の宣言に矛盾があります: " + "; ".join(problems))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="校正コーパスの群を、宣言した引き方で増やす")
    ap.add_argument("--group", required=True, help="[draw] を持つ群の名前")
    ap.add_argument("--n", type=int, default=10, help="足す本数の上限")
    ap.add_argument("--write", action="store_true", help="corpus.toml に足す")
    args = ap.parse_args(argv)

    corpus = calibrate.load_corpus()
    group = corpus.get("groups", {}).get(args.group)
    if group is None:
        print(f"群がありません: {args.group}", file=sys.stderr)
        return 1
    draw_cfg = group.get("draw")
    if not draw_cfg:
        print(f"{args.group} は [draw] を持たない（手で選ぶ群）", file=sys.stderr)
        return 1
    problems = lint_draw(args.group, draw_cfg)
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1

    # 足切りを課さない群（landmark）は、木を見ずに引く
    manifests = ([] if group.get("skip_filters")
                 else list(corpus.get("filters", {}).get("manifest_required", [])))
    picked = draw(draw_cfg, args.n, known_slugs(corpus), manifests,
                  members=list(group.get("slugs", [])))
    print(f"{args.group}: q={draw_cfg['q']!r} sort={draw_cfg['sort']} → {len(picked)} 本")
    for s in picked:
        print(f"  {s}")
    if not picked:
        print("新しく足せるものがありません", file=sys.stderr)
        return 1
    if args.write:
        today = dt.date.today().isoformat()
        path = calibrate.CORPUS_FILE
        path.write_bytes((checked_append(path.read_text(encoding="utf-8"), args.group,
                                        picked, today)).encode("utf-8"))
        print(f"書き込み: {path}（frozen_on = {today}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
