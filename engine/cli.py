#!/usr/bin/env python3
"""cli.py — engine の入口を1本にまとめた傘。

  workos                      何が呼べるのかを一覧する
  workos <入口> [引数...]     その入口を、フルパスを書かずに走らせる

engine には走る入口が十数個あり、どれも `python3 <この repo>/engine/<名前>.py` と
書かなければ呼べなかった。フルパスを覚えている人しか呼べない道具は、置いてあっても
呼ばれない。憲法 §6-5（導入は additive）に従い、既存の `python3 engine/<名前>.py` は
そのまま動く。この傘はその上に足しただけである。

守っていること。

1. **入口を列挙しない。** 一覧は `engine/*.py` を見て、`if __name__ == "__main__":`
   を持つものを「走る入口」として拾う。名前を並べた表にすると、**次に足した
   モジュールが、呼べないまま在ることになる**。しかも一覧には出ないので、
   足した本人以外には存在しないのと同じになる。走るかどうかは実体が持っている
   性質であり、別の場所に書き写した表が持っている性質ではない。

2. **import して main() を呼ばない。** subprocess で `python3 <入口>.py` を走らせる。
   main() の署名は現に3種類に分かれている（引数なし / `argv=None` / `argv` 必須）。
   傘が署名を知っていると、署名が変わった入口が**傘からだけ**黙って壊れる。
   直接呼んだときは動くので、壊れていることに気づく手がかりが無い。
   argv をそのまま渡す限り、傘は入口の中身を何も知らなくてよい。

3. **一行説明は実体の docstring から取る。** 別に書くと説明と中身がずれる。
   docstring を持たない入口は「（説明なし）」と出す。沈黙を説明の代わりにしない。

`--checks` が「ゲートは何を見ているか」に答えるのと同じ位置に、この一覧は
「work-os は何を呼べるか」に答える。どちらも走らせる前に読める。
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parent
ROOT = ENGINE.parent

# 「走る入口」の定義。名前ではなく形で決める。
RUN_GUARD = '__name__'
# docstring の1行目が `名前.py — 説明` の形かどうか。接頭辞は説明ではない。
HEADED = re.compile(r"^\S+\.py\s*(?:—|--|-)\s*(.*)$")


def is_entry(path: Path) -> bool:
    """`if __name__ == "__main__":` を持つか。構文木で見る。

    文字列 grep だと docstring や comment の中の `__name__` を拾う。拾った側は
    一覧に出てしまい、走らせると何も起きない入口が並ぶ。
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return False
    for node in tree.body:
        if not isinstance(node, ast.If):
            continue
        for sub in ast.walk(node.test):
            if isinstance(sub, ast.Name) and sub.id == RUN_GUARD:
                return True
    return False


def summary(path: Path) -> str:
    """docstring の1行目。`名前.py — 説明` の形なら説明だけを返す。"""
    try:
        doc = ast.get_docstring(ast.parse(path.read_text(encoding="utf-8")))
    except (OSError, SyntaxError):
        doc = None
    if not doc:
        return "（説明なし）"
    first = doc.strip().splitlines()[0].strip()
    # `名前.py — 説明` の接頭辞を落とす。区切りの後が空でも「（説明なし）」に落とす
    # （`d.py —` のように区切りだけ在る形で、接頭辞をそのまま説明として出さない）。
    m = HEADED.match(first)
    if m:
        return m.group(1).strip() or "（説明なし）"
    return first or "（説明なし）"


def entries() -> dict[str, Path]:
    """走る入口を {サブコマンド名: ファイル} で返す。名前順。"""
    found = {}
    for path in sorted(ENGINE.glob("*.py")):
        if path.name.startswith("_") or path.name == Path(__file__).name:
            continue
        if is_entry(path):
            found[path.stem] = path
    return found


def normalize(name: str) -> str:
    """`release-gate` と `release_gate` を同じものとして扱う。"""
    return name.strip().replace("-", "_")


def listing(found: dict[str, Path]) -> str:
    if not found:
        return f"走る入口が {ENGINE} に1つも見つかりません。"
    width = max(len(n) for n in found)
    lines = [
        "work-os の入口（引数なしで一覧、名前を付けるとそれを走らせます）",
        "",
    ]
    for name, path in found.items():
        lines.append(f"  {name.replace('_', '-'):<{width}}  {summary(path)}")
    lines += [
        "",
        f"  実体は {ENGINE} にあります。",
        "  各入口の引数は `workos <入口> --help` で出ます。",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    found = entries()

    if not args or args[0] in ("-h", "--help", "help", "list", "--list"):
        print(listing(found))
        return 0

    name = normalize(args[0])
    if name not in found:
        print(f"そういう入口はありません: {args[0]}", file=sys.stderr)
        print("", file=sys.stderr)
        print(listing(found), file=sys.stderr)
        return 2

    # 入口の中身を何も知らずに argv をそのまま渡す。cwd は呼んだ場所のままにする
    # （相対パスで repo を指す呼び方を壊さないため）。
    return subprocess.call([sys.executable, str(found[name]), *args[1:]])


if __name__ == "__main__":
    sys.exit(main())
