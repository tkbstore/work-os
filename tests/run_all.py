#!/usr/bin/env python3
"""tests/ の下の test_*.py をすべて走らせる。

work.toml の [publish.commands] test はここを指す。個別のファイル名を宣言に
書いていたときは、テストを1本足すたびに宣言を直す必要があり、実際に忘れた。
correctness レーンの test_passes は宣言されたコマンドしか走らせないので、
漏れたテストは「在るのに一度も走らない」状態になる。落ちるのではなく通って
しまう種類の壊れ方なので、宣言側ではなく発見側で解く。

  python3 tests/run_all.py          すべて走らせる
  python3 tests/run_all.py --list   走らせる対象を並べる（宣言の検査用）

終了コード: 0 = 全部走って通った / 1 = 失敗あり / 3 = 判定基準が無くて
走らせられなかったものがある（通過とは別に数える）。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
# 走らせられなかったことを表す第三の返事。詳細は tests/_registry.py。
SKIPPED = 3


def discover() -> list[Path]:
    """自分自身を除く test_*.py を名前順に返す。"""
    return sorted(p for p in HERE.glob("test_*.py") if p.name != Path(__file__).name)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    targets = discover()
    if "--list" in args:
        for t in targets:
            print(t.name)
        return 0
    if not targets:
        print("tests/ に test_*.py が1つも無い", file=sys.stderr)
        return 1

    failed: list[str] = []
    skipped: list[str] = []
    for t in targets:
        print(f"\n{'=' * 70}\n{t.name}\n{'=' * 70}")
        code = subprocess.run([sys.executable, str(t)]).returncode
        if code == SKIPPED:
            skipped.append(t.name)
        elif code != 0:
            failed.append(f"{t.name} (exit {code})")

    print(f"\n{'=' * 70}")
    if failed:
        print(f"失敗 {len(failed)}/{len(targets)} ファイル")
        for f in failed:
            print(f"  - {f}")
        if skipped:
            print(f"スキップ {len(skipped)}/{len(targets)} ファイル: {', '.join(skipped)}")
        return 1
    if skipped:
        # 走らせられなかったことを、走らせて通ったのと同じ顔で出さない。
        # ここで 0 を返すと [publish.commands] test = このファイル なので、
        # 公開ゲートの correctness が「テストが通った」と報告する。実際には
        # 判定基準が無くて 3 本走っていない、という状態を通してしまう。
        print(f"スキップ {len(skipped)}/{len(targets)} ファイル"
              f"（判定基準が無いので走らせられなかった）")
        for s_ in skipped:
            print(f"  - {s_}")
        print("残りは通過。判定基準の置き場所は WORKOS_REGISTRY で教えてください。")
        return SKIPPED
    print(f"全 {len(targets)} ファイル通過")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
