#!/usr/bin/env python3
"""inbox.py — 「これ使えないかな」を、投げ先を決めずに受け取る。

投げる瞬間に受け皿を決めようとすると必ず詰まる。決まらないから投げられず、
無理に決めると間違った場所に入って腐る。だから判断を遅らせる。

行を足すだけ。仕分けは週次の promote と同じタイミングで行う。
21日残ったものは捨てる。溜まらないための唯一の規則である。

  python3 engine/inbox.py --add "<url>" --note "<なぜ気になったか>"
  python3 engine/inbox.py                # 一覧（古いものに印がつく）
  python3 engine/inbox.py --stale        # 21日超だけ（捨てる候補）

中身は具体（URL・案件名）なので registry/（非公開層）に置く。
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from workos import registry_path  # noqa: E402

INBOX = registry_path("inbox.md")
DROP_DAYS = 21
LINE = re.compile(r"^-\s+(\d{4}-\d{2}-\d{2})\s+(\S+)\s*(?:—\s*(.*))?$")

HEADER = """# inbox — 投げ先を決めずに受け取る場所

config 層（非公開）。judgement を遅らせるための緩衝地帯である。

規則は3つだけ。

1. 行を足すときに投げ先を決めない
2. 仕分けは週次（promote と同じタイミング）
3. **21日残ったら捨てる**

```
- YYYY-MM-DD  <url または名前>  — なぜ気になったか
```

"""


def load() -> list[tuple[date, str, str]]:
    if not INBOX.exists():
        return []
    out = []
    for line in INBOX.read_text(encoding="utf-8").splitlines():
        m = LINE.match(line.strip())
        if not m:
            continue
        try:
            d = datetime.strptime(m.group(1), "%Y-%m-%d").date()
        except ValueError:
            continue
        out.append((d, m.group(2), (m.group(3) or "").strip()))
    return sorted(out)


def add(item: str, note: str, today: date) -> None:
    if not INBOX.exists():
        INBOX.parent.mkdir(parents=True, exist_ok=True)
        INBOX.write_text(HEADER, encoding="utf-8")
    with INBOX.open("a", encoding="utf-8") as f:
        f.write(f"- {today.isoformat()}  {item}  — {note}\n")
    print(f"追記: {item}（{DROP_DAYS}日後に捨てる候補になります）")


def main() -> int:
    ap = argparse.ArgumentParser(description="投げ先未定のものを受け取る")
    ap.add_argument("--add", metavar="ITEM", help="URL または名前")
    ap.add_argument("--note", default="", help="なぜ気になったか")
    ap.add_argument("--stale", action="store_true", help=f"{DROP_DAYS}日超だけ出す")
    ap.add_argument("--today", default="", help="基準日 YYYY-MM-DD（テスト用）")
    args = ap.parse_args()

    today = datetime.strptime(args.today, "%Y-%m-%d").date() if args.today else date.today()
    if args.add:
        add(args.add, args.note or "(理由未記入)", today)
        return 0

    items = load()
    cutoff = today - timedelta(days=DROP_DAYS)
    stale = [i for i in items if i[0] <= cutoff]
    shown = stale if args.stale else items
    if not shown:
        print("inbox は空です" if not items else f"{DROP_DAYS}日超のものはありません")
        return 0
    for d, item, note in shown:
        age = (today - d).days
        mark = "捨てる候補" if d <= cutoff else f"{age}日"
        print(f"  {d}  [{mark:>10}]  {item}  — {note}")
    if not args.stale and stale:
        print(f"\n  {len(stale)} 件が {DROP_DAYS} 日を超えています。捨てるか、投げ先を決めてください。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
