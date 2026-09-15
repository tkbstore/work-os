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

from workos import registry_root  # noqa: E402

SKIPPED = 3


def require(*names: str) -> None:
    """判定基準のファイルが揃っていなければ、理由を出して exit 3。"""
    root = registry_root()
    missing = [n for n in names if not (root / n).is_file()]
    if not missing:
        return
    print(f"  -- この検査は走っていません（判定基準が見つかりません）\n"
          f"     探した場所: {root}\n"
          f"     足りないもの: {', '.join(missing)}\n"
          f"     置き場所を教える: WORKOS_REGISTRY=<path> python3 tests/run_all.py\n"
          f"     何を書くファイルかは README の「Configuration 層はこのリポジトリに"
          f"在りません」を参照。")
    raise SystemExit(SKIPPED)
