#!/usr/bin/env python3
"""昇格の証拠をどう数えるかの、ただ一つの定義。

同じ憲法 §4（3社ルール）を validate.py と promote.py が別々に実装していたため、
同じ capability について validate は「違反なし」、promote は「3社ルール違反」と
言う状態になっていた。さらに promote 側は used_by の "*" を1リポジトリとして
数えていたので、全称宣言をするほど件数が水増しされていた。

規則の実装が2つあると、いつか必ず食い違う。数え方はここにしか置かない。

外部依存なし。読み取りのみ。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from workos import RULE_OF_THREE  # noqa: E402

UNIVERSAL = "*"


@dataclass(frozen=True)
class Evidence:
    """その capability を「何が」使っていると宣言されているか。"""
    universal: bool          # "*" があるか（全称。件数では測らない）
    names: tuple[str, ...]   # 実在の名前だけ。"*" は含めない
    note: str = ""

    @property
    def count(self) -> int:
        return len(self.names)


def collect(used_by, declaring_repos=(), note: str = "") -> Evidence:
    """宣言された used_by と、実際に宣言しているリポジトリから証拠を組む。

    "*" は名前ではなく全称の印なので、件数から必ず外す。
    """
    used = [str(u) for u in (used_by or [])]
    universal = UNIVERSAL in used
    names = {u for u in used if u != UNIVERSAL} | {str(r) for r in declaring_repos}
    return Evidence(universal=universal, names=tuple(sorted(names)), note=str(note or ""))


def satisfies_rule_of_three(ev: Evidence) -> tuple[bool, str]:
    """憲法 §4 の3社ルールを満たすか。満たす／満たさない理由も返す。"""
    if ev.universal:
        return True, '"*" による全称宣言（件数では測らない）'
    if ev.count >= RULE_OF_THREE:
        return True, f"{ev.count} リポジトリ"
    if ev.note:
        return True, f"例外として宣言済み: {ev.note}"
    return False, f"{ev.count} リポジトリしか使っていない"


def caveat(ev: Evidence) -> str:
    """昇格の判断をするその瞬間に、目に入れる必要があるもの。

    note を「書いてあるだけ」にすると誰も読まない。実際にそうなっていた。

    ただし全件に同じ文言が付くと、鳴りっぱなしの警告として無視される。
    実測では昇格候補14件すべてに同一文言が付いていた。全称宣言そのものは
    設計どおりなので黙る。警告は「全称だが実態は限られる」と自分で宣言した
    ものにだけ出す。その宣言は note として既に区別されている。
    """
    if ev.universal:
        return f'全称宣言だが実態は限られる。{ev.note}' if ev.note else ""
    return ev.note
