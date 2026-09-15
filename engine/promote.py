#!/usr/bin/env python3
"""昇格レーンの判定。誰を上げてよいか／上げてはいけないかを機械が決める。

  python3 engine/promote.py <repos>

憲法 §4 の昇格条件を、宣言された capabilities.toml だけを根拠に判定する。
人間が決めるのは「上げるかどうか」だけで、「上げてよいか」は判定しない。
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from promotion import caveat, collect, satisfies_rule_of_three  # noqa: E402
from workos import PROMOTION_ORDER, RULE_OF_THREE, discover_repos  # noqa: E402

NEXT = dict(zip(PROMOTION_ORDER, PROMOTION_ORDER[1:]))


def main() -> int:
    ap = argparse.ArgumentParser(description="昇格候補と違反を判定する")
    ap.add_argument("root", type=Path, help="リポジトリが並んでいる親ディレクトリ")
    ap.add_argument("--domain", help="ドメインで絞る")
    args = ap.parse_args()

    repos = discover_repos(args.root)
    if args.domain:
        repos = [r for r in repos if r.domain == args.domain]
    if not repos:
        print("work.toml を持つリポジトリが見つかりません。engine/adopt.py で追加してください。")
        return 1

    # capability id ごとに、どのリポジトリで、どの status か
    seen: dict[str, dict[str, str]] = defaultdict(dict)
    declared: dict[str, list[str]] = defaultdict(list)
    hosts: dict[str, set[str]] = defaultdict(set)
    notes: dict[str, str] = {}
    for repo in repos:
        for cap in repo.capabilities:
            seen[cap.id][repo.name] = cap.status
            declared[cap.id] += list(cap.used_by)
            hosts[cap.id].add(repo.name)
            if cap.note:
                notes[cap.id] = cap.note
    # 証拠の数え方は promotion.py にしか置かない（validate.py と同じ規則）。
    ev = {cid: collect(declared[cid], hosts[cid], notes.get(cid, "")) for cid in seen}

    print(f"対象 {len(repos)} リポジトリ / capability {len(seen)} 種\n")

    promote: list[str] = []
    hold: list[str] = []
    violation: list[str] = []

    for cid, per_repo in sorted(seen.items()):
        n = ev[cid].count
        statuses = set(per_repo.values())
        top = max(statuses, key=lambda s: PROMOTION_ORDER.index(s) if s in PROMOTION_ORDER else -1)
        nxt = NEXT.get(top)

        if len(statuses) > 1:
            violation.append(
                f"{cid}: リポジトリ間で status が食い違っている → "
                + ", ".join(f"{r}={s}" for r, s in sorted(per_repo.items()))
            )

        ok, why = satisfies_rule_of_three(ev[cid])
        if top in ("proposed", "stable", "kernel") and not ok:
            violation.append(f"{cid}: status={top} だが {why}（3社ルール違反）")
            continue

        if top == "experimental" and (ok or n >= RULE_OF_THREE):
            promote.append(f"{cid}: {why} → proposed へ上げられる")
        elif top == "local" and n >= 2:
            promote.append(f"{cid}: {why} → experimental へ上げられる")
        elif nxt:
            hold.append(f"{cid}: {top} のまま（{why}）。次は {nxt}")

    def block(title: str, items: list[str], mark: str) -> None:
        print(title)
        print("-" * 68)
        if not items:
            print("  なし")
        for i in items:
            print(f"  {mark} {i}")
            cid = i.split(":", 1)[0]
            note = caveat(ev.get(cid)) if ev.get(cid) else ""
            if note and mark != "・":
                # 昇格を判断するその瞬間に出す。書いてあるだけでは誰も読まない。
                print(f"      ⚠ {note}")
        print()

    block("■ 昇格できるもの", promote, "▲")
    block("■ 憲法違反", violation, "×")
    block("■ 据え置き", hold, "・")

    print("昇格するには、対象リポジトリの capabilities.toml の status を1段だけ上げ、")
    print("stable 以上にする場合は invariant を宣言してから validate.py を通してください。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
