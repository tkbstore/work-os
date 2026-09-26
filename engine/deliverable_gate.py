#!/usr/bin/env python3
"""deliverable_gate.py — その成果物を、誰に渡せるかを機械が決める。

release_gate.py は「リポジトリを外に出せるか」を見る。こちらは **1つの成果物を
どの宛先に渡せるか** を見る。単位が違うので別のゲートにしてある。

なぜ単位を分けるか。出す単位はリポジトリとは限らない。提案書1枚、LP の `dist/`、
レポート1本を渡すことのほうが多い。単位が違えば見るべき場所も逆になる:

  release_gate  `dist/` は vendored として **除外する**（著者が自分ではない）
  deliverable   `dist/` こそ **唯一見るべき場所**（渡るのはそれだから）

原理は1つ。**宛先は宣言させず、在る顧客名の集合から計算する。**
リポに `client = "..."` を1つ宣言させる形は、複数顧客を横断するリポで必ず破綻する
（社内ワークスペース系は11〜12社の名前を含む。2026-09-26 実測）。宣言は嘘になった
瞬間に偽陰性になるので、宣言ではなく中身を数える。

  0 社  宛先の制約なし
  1 社  その1社にだけ渡せる
  2 社以上  外には渡せない（社内止まり）

  python3 engine/deliverable_gate.py                # カレントの宣言を全部見る
  python3 engine/deliverable_gate.py <repo> --json
  python3 engine/deliverable_gate.py --scan <root>

exit: 0 = すべてに宛先がある / 1 = 宛先を持てない成果物がある /
      3 = 判定できていない（宣言が無い・読めなかったファイルがある）

**この物差しの限界を先に書いておく。** 顧客名は「その顧客の秘密が入っている」の
proxy でしかない。競合分析は相手の名前を大量に含むのが正常な形で、実測では
ある1社向けリポが別1社の名前を 3858 件含んでいた（競合として分析している）。
名前の数だけでは「秘密の混入」と「競合の言及」を区別できない。だから出力は
**判定と一緒に社ごとの件数を必ず出す**。区別は人がやる。数を出さない判定は、
誤りが見えないので使ってはいけない。

redact（顧客名の除去）はここではやらない。除去して自分の除去結果を信用する閉ループ
を作ると、「消したから安全」が検査を通ってしまう。別コマンドの仕事である。

外部依存なし。読み取りのみ。
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# E402 は「import が先頭に無い」の指摘。上で sys.path を挿してからでないと engine/
# 配下は解決しないので、抑制しているのは順序の指摘だけである。理由は隠していない。
from release_gate import _scan_specs, load_pattern_file, read_text  # noqa: E402
# 同じ理由（sys.path を挿した後にしか解決しない）。抑制は順序の指摘だけ。
from workos import _load_toml, iter_repo_dirs  # noqa: E402
# 同じ理由。人が下した判定を読むのはここだけで、判定そのものは audit が持つ。
import audit  # noqa: E402

# 宛先の計算に使う語の宣言。カテゴリを [clients] に絞るのは、ここで問うのが
# 「他の顧客に渡せるか」だけだからである。自社製品名や自分の名前が成果物に在るのは
# 当然なので、この問いでは裁かない（あちらは release_gate の provenance / abstraction）。
TERMS_FILE = "private_terms.toml"
TERMS_CATEGORIES = ["clients"]

# 渡せる先が確定しない下限。1社なら宛先、2社以上なら外に出せない。
MULTI_CLIENT = 2

# 読めなかったファイルを何件まで名指しするか。数だけ出すと追えない。
MAX_EXAMPLES = 3


@dataclass
class Deliverable:
    """1つの成果物。宣言は path だけで、宛先は書かせない。"""
    path: str
    title: str
    files: list[tuple[str, Path]] = field(default_factory=list)
    clients: dict[str, int] = field(default_factory=dict)
    # 読めなかったファイル。通したことにしないために数える。
    unread: list[str] = field(default_factory=list)
    # 人の判定で抑制した件数（宣言ごと）。**0 件でも行を出す**ので、抑制が
    # 効いていることが出力から消えない。抑制を黙って効かせると rubber-stamp になる。
    suppressed: dict[str, int] = field(default_factory=dict)
    # 記録の健全性。ゲートが毎回出す（黙って育てられないようにするため）。
    audit_notes: list[str] = field(default_factory=list)

    @property
    def recipients(self) -> list[str]:
        return sorted(self.clients, key=lambda t: -self.clients[t])

    @property
    def verdict(self) -> str:
        if not self.files:
            return "unknown"
        if len(self.clients) >= MULTI_CLIENT:
            return "internal_only"
        return "single" if self.clients else "unrestricted"

    @property
    def settled(self) -> bool:
        """判定が出ているか。読めなかったファイルが在る間は出ていない。"""
        return self.verdict != "unknown" and not self.unread


def declared(root: Path) -> list[Deliverable]:
    """work.toml の [[deliverable]]。宣言が無ければ空。

    宣言するのは path と、人が読むための title だけ。宛先を書く欄は置かない。
    置くと、宣言と中身が食い違ったときに宣言のほうが勝ってしまう。
    """
    work = root / "work.toml"
    if not work.is_file():
        return []
    try:
        data = _load_toml(work)
    # 壊れ方の種類で扱いは変わらない。どの例外でも「読めなかった」1つに落ちる。
    except Exception as exc:  # noqa: BLE001
        # 言うのはここ。work.toml が壊れていることは成果物が無いことではないが、
        # 空を返した先では「宣言なし」と区別がつかない。
        print(f"  warn  work.toml を読めません: {exc}", file=sys.stderr)
        return []
    out = []
    for item in data.get("deliverable", []) or []:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path", "")).strip()
        if path:
            out.append(Deliverable(path=path, title=str(item.get("title", "")).strip()))
    return out


def collect(root: Path, pattern: str) -> list[tuple[str, Path]]:
    """宣言された path 配下のファイル。git 追跡の有無で絞らない。

    成果物は生成物であることが普通で、`dist/` も `deliverables/*.html` も追跡下に
    無いことがある。追跡で絞ると「渡るのに見ていない」が生まれる。漏れは宣言範囲の
    外から入って成果物に現れるので、渡るものをそのまま見る。
    """
    target = root / pattern
    if target.is_file():
        return [(pattern, target)]
    if target.is_dir():
        return sorted((str(p.relative_to(root)), p)
                      for p in target.rglob("*") if p.is_file())
    out = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        rel = str(p.relative_to(root))
        if fnmatch.fnmatch(rel, pattern):
            out.append((rel, p))
    return sorted(out)


def observe(root: Path, item: Deliverable) -> Deliverable:
    """成果物に在る顧客名を数える。

    観測は release_gate の `_scan_specs` をそのまま呼ぶ。ここで走査を書き直すと
    物差しが2本になり、片方を直しても片方が古いままになる（校正でも同じ理由で
    release_gate.evaluate() をそのまま呼んでいる）。
    """
    item.files = collect(root, item.path)
    specs, is_rx = load_pattern_file(TERMS_FILE, TERMS_CATEGORIES)
    if not specs:
        item.unread.append(f"<{TERMS_FILE} に {TERMS_CATEGORIES} の宣言が無い>")
        return item
    # 読めなかったものを先に数える。_scan_specs は読めないファイルを黙って飛ばすので、
    # ここで数えないと「512KB の HTML を見ずに通した」が pass として出る。
    readable = []
    for rel, path in item.files:
        if read_text(path) is None:
            item.unread.append(rel)
        else:
            readable.append((rel, path))
    # collect_all=True。抑制は1件ずつ突き合わせるので、1件でも取りこぼすと
    # そこだけ抑制が効かない。例示の上限（3件）では足りない。
    log = audit.load()
    live: set[tuple[str, str, str]] = set()
    for shown, _, hits in _scan_specs(readable, specs, not is_rx, {},
                                      collect_all=True):
        kept, dropped = log.filter(root.name, shown, hits)
        _, _, term = shown.partition(":")
        name = term or shown
        if dropped:
            item.suppressed[name] = dropped
        for h in hits:
            live.add((root.name, shown, audit.line_key(h.body)))
        # 全件が抑制されたら、この宣言は宛先に数えない。人が実物を見て
        # 「混入ではない」と決めたということなので、判定を覆さない。
        if kept:
            item.clients[name] = len(kept)
    item.audit_notes = audit.health(log, root.name, live)
    return item


def evaluate(root: Path) -> list[Deliverable]:
    return [observe(root, item) for item in declared(root)]


VERDICT_TEXT = {
    "unrestricted": "顧客名なし — 宛先の制約なし",
    "single": "1 社にだけ渡せる",
    "internal_only": "外には渡せない（社内止まり）",
    "unknown": "宣言された path にファイルが無い",
}
VERDICT_MARK = {"single": "OK  ", "unrestricted": "OK  ",
                "internal_only": "NG  ", "unknown": "--  "}


def render(root: Path, items: list[Deliverable]) -> None:
    if not items:
        print(f"[{root.name}] [[deliverable]] の宣言なし — この問いは立てられていない")
        return
    print(f"[{root.name}] 成果物 {len(items)} 件")
    for item in items:
        print(f"  {VERDICT_MARK[item.verdict]}{item.title or item.path}"
              f"  ({len(item.files)} ファイル)")
        tail = f": {item.recipients[0]}" if item.verdict == "single" else ""
        print(f"        {VERDICT_TEXT[item.verdict]}{tail}")
        if item.clients:
            # 判定と同じ場所に件数を出す。数の無い判定は誤りが見えない。
            print("        在る顧客名: " + "、".join(
                f"{t} {item.clients[t]} 件" for t in item.recipients))
        if item.suppressed:
            # 抑制した件数は必ず出す。黙って引くと、何を通したか分からなくなる。
            print("        人の判定で抑制: " + "、".join(
                f"{t} {n} 件" for t, n in sorted(item.suppressed.items(),
                                                key=lambda kv: -kv[1])))
        for note in item.audit_notes:
            print(f"        {note}")
        if item.unread:
            head = "、".join(item.unread[:MAX_EXAMPLES])
            print(f"        見ていない {len(item.unread)} ファイル"
                  f"（512KB 超・バイナリ）: {head}")
            print("        見ていないことは、渡せることではありません")


def as_json(results: list[tuple[Path, list[Deliverable]]]) -> str:
    return json.dumps([{
        "repo": root.name,
        "deliverables": [{"path": i.path, "title": i.title, "files": len(i.files),
                          "verdict": i.verdict, "recipients": i.recipients,
                          "clients": i.clients, "unread": i.unread,
                          "suppressed": i.suppressed,
                          "settled": i.settled} for i in items],
    } for root, items in results], ensure_ascii=False, indent=2)


def main() -> int:
    ap = argparse.ArgumentParser(description="その成果物を誰に渡せるかを観測する")
    ap.add_argument("repos", nargs="*", help="検査するリポジトリ")
    ap.add_argument("--scan", metavar="ROOT", help="配下で成果物を宣言した全リポを検査")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    roots = [Path(r).resolve() for r in args.repos] or [Path.cwd()]
    if args.scan:
        roots = list(iter_repo_dirs(Path(args.scan).resolve()))

    results: list[tuple[Path, list[Deliverable]]] = []
    silent = 0
    for root in roots:
        items = evaluate(root)
        if items:
            results.append((root, items))
            continue
        silent += 1
        if not args.scan:
            render(root, items)

    if args.json:
        print(as_json(results))
    else:
        for root, items in results:
            render(root, items)
        if args.scan:
            print(f"\n宣言が無いため見ていないリポジトリが {silent} 本あります。")
            print("見ていないことは、問題が無いことではありません。")

    flat = [i for _, items in results for i in items]
    if any(i.verdict == "internal_only" for i in flat):
        return 1
    if not flat or any(not i.settled for i in flat):
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
