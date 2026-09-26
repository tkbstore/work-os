#!/usr/bin/env python3
"""判定基準（registry）が要る検査の、共通の入口。

work-os は仕組みだけを持ち、判定基準そのもの（レーン・秘密のパターン・検査語・
痕跡の宣言）は別リポジトリの config 層にある。新規 clone にはそれが無い。

無いときに落とすのは正しくない（この clone は壊れていない）。かといって黙って
通すのも正しくない（検査していないものを、検査して通ったのと同じ顔で出すことに
なる。2026-09-02 に release_gate が同じ形で「見ていない 77 本」を通していた）。

そこで**第三の返事**を用意する。exit 3 = 走らせられなかった。tests/run_all.py は
これを失敗とも通過とも数えず、スキップとして件数を出し、1件でもあれば 0 を返さない。

ファイル名が test_*.py ではないので、run_all.py の発見対象にはならない。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))

# E402 は「import が先頭に無い」の指摘。上で sys.path を挿してからでないと engine/
# は解決しない。抑制しているのは順序の指摘だけで、動かない理由は隠していない。
from workos import _load_toml, registry_root  # noqa: E402

SKIPPED = 3


# 骨格（形）の置き場。判定基準は work-os と registry の2層で読まれるので、
# 検査の前提も2層で探す。registry だけを見ていた頃は、形を work-os 側へ移した日に
# 「判定基準が無い」と言って2つの検査ファイルが黙ってスキップした（2026-09-26）。
CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


def require(*names: str) -> None:
    """判定基準のファイルが揃っていなければ、理由を出して exit 3。"""
    root = registry_root()
    missing = [n for n in names
               if not (root / n).is_file() and not (CONFIG_DIR / n).is_file()]
    if not missing:
        return
    print(f"  -- この検査は走っていません（判定基準が見つかりません）\n"
          f"     探した場所: {root}\n"
          f"     足りないもの: {', '.join(missing)}\n"
          f"     置き場所を教える: WORKOS_REGISTRY=<path> python3 tests/run_all.py\n"
          f"     何を書くファイルかは README の「Configuration 層はこのリポジトリに"
          f"在りません」を参照。")
    raise SystemExit(SKIPPED)


def borrow_terms(count: int = 1, group: str = "clients") -> list[str]:
    """registry が「持ち出してはいけない」と宣言している語を count 個借りる。

    検査の見本をテストの本文に直接書けないため借りる。**ゲートは自分自身のテストを
    読む**ので、固有名詞を書くと work-os 自身の provenance レーンがそれを検出する。

    2語以上を借りられる口にしているのは、「どの語が当たったか」を出しているかが
    1語では分からないからである。語を捨てる実装でも、語が1つしか在らない木では
    「当たった」ことだけは正しく出る。

    group を [clients] に絞るのが既定。自社製品名（products）や自分の名前（org）は
    そのリポに在って当然のもので、公開判定では裁かない。
    """
    terms = registry_root() / "private_terms.toml"
    if not terms.is_file():
        # 落ちること自体は正しい（検査していないものを通さない）が、
        # FileNotFoundError だけでは何が足りないのか読めない。
        raise SystemExit(f"検査語の宣言が見つかりません: {terms}\n"
                         f"  registry の場所を教えてください（WORKOS_REGISTRY=<path>）")
    body = _load_toml(terms).get(group, {})
    out: list[str] = []
    for val in (body.values() if isinstance(body, dict) else [body]):
        for term in (val if isinstance(val, list) else [val]):
            t = str(term).strip()
            if len(t) >= 4 and t.isalnum() and t not in out:
                out.append(t)
            if len(out) == count:
                return out
    raise SystemExit(f"[{group}] に {count} 語の借りられる宣言がありません: {terms}")
