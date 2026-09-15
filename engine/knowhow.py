#!/usr/bin/env python3
"""knowhow.py — 各リポジトリの意思決定ログから、繰り返されたノウハウを収穫する。読み取り専用。

intent.py が「今何をやろうとしているか」を見るのに対し、これは「何を学び終えたか」を見る。
別の観測対象なので別のファイルにしてある。

読むのは既に自然発生しつつある context/decisions/*.md（ADR）の front-matter だけである。
新しい強制規約は足さない。intent.py が sessions の front-matter を読むのと同じ型。

    ---
    type: adr
    status: accepted
    tags: [self-healing-doctor, drift-control]   # ノウハウの識別子。昇格候補の判定単位
    sources: [repo-a, repo-b, repo-c]            # そのノウハウを実際に適用した先
    ---

これは SOAD の asset harvesting（Zimmermann 2011, "Architectural Decisions as Reusable
Design Assets"）の最小実装である。「繰り返された decision outcome は再利用資産の起点」。
tag ごとに distinct sources を数え、SPLE の rule of three（＝再利用ライブラリに入れる前に
3つの別々の適用先で試す）を満たした tag を「capabilities.toml に起こす昇格候補」として出す。

promote.py は capabilities.toml の宣言を判定する。knowhow.py はその手前 —
まだ capability になっていないノウハウが、昇格に値するほど繰り返されたかを検出する。
両者は同じ RULE_OF_THREE を共有し、役割だけが違う。

  python3 engine/knowhow.py <repos>
  python3 engine/knowhow.py <repos> --domain <domain>
  python3 engine/knowhow.py <repos> --candidates   # 昇格候補だけを出す

外部依存なし。何も書き換えない。
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from workos import RULE_OF_THREE, iter_repo_dirs  # noqa: E402

# ADR の front-matter で読むキー。ここに無いキーは無視する（規約を最小に保つ）。
LIST_KEYS = ("tags", "sources", "related")
SCALAR_KEYS = ("type", "status", "created", "updated")
DECISIONS_DIR = "context/decisions"


@dataclass
class Decision:
    repo: str
    path: str
    status: str = ""
    tags: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)


def _parse_list(raw: str) -> list[str]:
    """`[a, b, c]` または `a, b, c` を要素に分ける。front-matter の flow list だけ相手にする。"""
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    return [item.strip().strip('"').strip("'") for item in raw.split(",") if item.strip()]


def read_front_matter(path: Path) -> dict[str, object]:
    """ADR 先頭の YAML front-matter を、外部依存なしで最小限だけ読む。

    intent.py の read_front_matter と同じ方針（--- で挟まれた key: value を読むだけ）。
    値が flow list のときだけ配列に展開する。ブロックスタイルや入れ子は扱わない。
    """
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return {}
    if not lines or lines[0].strip() != "---":
        return {}
    out: dict[str, object] = {}
    for line in lines[1:80]:
        if line.strip() == "---":
            break
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        k, v = k.strip(), v.strip()
        if k in LIST_KEYS:
            out[k] = _parse_list(v)
        elif k in SCALAR_KEYS:
            out[k] = v.strip('"').strip("'")
    return out


def collect_decisions(root: Path) -> list[Decision]:
    """root 直下の各リポジトリの context/decisions/*.md から front-matter を集める。"""
    out: list[Decision] = []
    # require_git=False: ADR は git になっていないディレクトリにも在りうる。
    # ここだけ .git を要求しない理由を、呼ぶ側に見えるところへ置いておく。
    for repo in iter_repo_dirs(root, require_git=False):
        d = repo / DECISIONS_DIR
        if not d.is_dir():
            continue
        for md in sorted(d.glob("*.md")):
            fm = read_front_matter(md)
            tags = fm.get("tags") or []
            if not tags:
                continue  # 信号のない ADR は収穫対象にしない
            out.append(Decision(
                repo=repo.name,
                path=str(md.relative_to(root)),
                status=str(fm.get("status", "")),
                tags=list(tags),
                sources=list(fm.get("sources") or [repo.name]),
            ))
    return out


@dataclass
class Knowhow:
    tag: str
    sources: set[str] = field(default_factory=set)
    decisions: list[str] = field(default_factory=list)

    @property
    def reach(self) -> int:
        return len(self.sources)

    @property
    def is_candidate(self) -> bool:
        return self.reach >= RULE_OF_THREE


def harvest(decisions: list[Decision]) -> list[Knowhow]:
    """tag ごとに distinct sources を集約する。frequency-based（Amiri & Zdun 2021）。"""
    by_tag: dict[str, Knowhow] = {}
    for dec in decisions:
        for tag in dec.tags:
            kh = by_tag.setdefault(tag, Knowhow(tag=tag))
            kh.sources.update(dec.sources)
            kh.decisions.append(dec.path)
    # 到達数の多い順、同数ならタグ名順。
    return sorted(by_tag.values(), key=lambda k: (-k.reach, k.tag))


def main() -> int:
    ap = argparse.ArgumentParser(description="意思決定ログから繰り返されたノウハウを収穫する")
    ap.add_argument("root", type=Path, help="リポジトリが並んでいる親ディレクトリ")
    ap.add_argument("--domain", help="（予約）ドメインで絞る。現状は全 ADR を対象にする")
    ap.add_argument("--candidates", action="store_true", help="昇格候補だけを出す")
    args = ap.parse_args()

    root = args.root.expanduser().resolve()
    decisions = collect_decisions(root)
    knowhow = harvest(decisions)

    repos = sorted({d.repo for d in decisions})
    print(f"\nKNOWHOW  {root}")
    print(f"  信号付き ADR {len(decisions)} 件 / {len(repos)} リポジトリ"
          f"（{', '.join(repos) or 'なし'}）")
    print(f"  ノウハウ tag {len(knowhow)} 種 / rule of three = {RULE_OF_THREE} 適用先\n")

    candidates = [k for k in knowhow if k.is_candidate]
    holding = [k for k in knowhow if not k.is_candidate]

    print("■ 昇格候補（capabilities.toml に capability として起こせる）")
    print("-" * 68)
    if not candidates:
        print("  なし")
    for k in candidates:
        print(f"  ▲ {k.tag}: {k.reach} 適用先で実証 → {', '.join(sorted(k.sources))}")
        print(f"      根拠 ADR: {', '.join(k.decisions)}")
    print()

    if not args.candidates:
        print("■ 据え置き（まだ 3 適用先に届いていない）")
        print("-" * 68)
        if not holding:
            print("  なし")
        for k in holding:
            need = RULE_OF_THREE - k.reach
            print(f"  ・ {k.tag}: {k.reach} 適用先（あと {need} で候補）→ "
                  f"{', '.join(sorted(k.sources))}")
        print()

    print("昇格候補を capability にするには、対象リポジトリの capabilities.toml に")
    print("[[capability]] を1件起こし、used_by に適用先を書いて validate.py を通してください。")
    print("そこから先は engine/promote.py が昇格レーンを判定します。")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        raise SystemExit(0)
