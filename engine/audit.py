#!/usr/bin/env python3
"""audit.py — 人が実物を見て下した判定を記録し、それを検査に反映する。

走査は「顧客名が在る」までしか言えない。**引用と混入は名前の数では分けられない。**
実測（2026-09-26）: ある顧客向けの提案書8本が「2社以上」で止まったが、2社目の8件は
すべてその会社の公開エンジニアブログへの出典リンクだった。走査を賢くしても解けない。
人が実物を見て決めるしかなく、**決めた結果を置く場所が無いこと**が本当の欠陥だった。

detect-secrets の `.secrets.baseline` が同じ問題を解いている。ただしあの形には
記録された2つの壊れ方があるので、そこは変えてある。

  1) **行番号を鍵にすると、行が動いた瞬間に鍵が外れる。**
     外れたことは出力に現れないので、抑制だけが静かに消える。
     → 鍵を **行の本文のハッシュ** にした（in-toto が subject を content digest に
       結びつけるのと同じ理由）。行が動いても効き、行の中身が変われば失効する。

  2) **歴史的な検出が永久に rubber-stamp される。**
     → 抑制するのは `cleared` だけ。`leak` は記録であって抑制ではない。
       そして毎回 **記録の規模と、今回効いた件数と、死んだ記録の数を必ず出す**。
       黙って育てられない。

原則: **1件の記録が抑制できるのは、1つの (path, 宣言, 行の本文) だけ。**
ファイル単位・宣言単位のワイルドカードは持たない。抑制を形で広げると、広げた側が
何を通したか分からなくなる（列挙で検査を書かないのと同じ理由の裏返し）。
理由（why）が無い記録は無効で、抑制しない。

  python3 engine/audit.py --list              記録を全部出す
  python3 engine/audit.py --health <repo>     記録の規模・死んだ記録を出す
  python3 engine/audit.py --clear <repo> <path>:<line> <宣言> --why "..."

記録の置き場は registry（組織固有の事実なので work-os の履歴に残さない）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Protocol

sys.path.insert(0, str(Path(__file__).resolve().parent))
# E402 は「import が先頭に無い」の指摘。上で sys.path を挿してからでないと engine/
# は解決しない。抑制しているのは順序の指摘だけである。
from workos import _load_toml, registry_path  # noqa: E402


class Hit(Protocol):
    """当たった1件。release_gate.Match がこの形を満たす。

    型で受けずに形で受けるのは、**逆向きの import を作らないため**である。
    release_gate 側から audit を呼ぶので、audit が release_gate を import すると
    循環する。当たりを運ぶ型は観測する側のものなので、こちらは形だけ知る。
    """
    rel: str
    line: int
    body: str

AUDIT_FILE = "audit.toml"
VERDICTS = ("cleared", "leak")
# 理由とみなす最小の長さ。「ok」「問題なし」を理由と呼ばせないための下限。
# hooks/root_cause_guard.py の MIN_REASON と同じ値。同じことを要求しているため。
MIN_REASON = 12
# 鍵の長さ。衝突より、目で読める長さを優先する（16 桁 = 64 bit）。
KEY_LEN = 16


def line_key(body: str) -> str:
    """行の本文から鍵を作る。行番号は入れない。

    行番号を入れると、無関係な編集で行が動いただけで鍵が外れる。外れたことは
    出力に現れないので、抑制だけが静かに消える。末尾の空白だけ落として本文を見る。
    """
    return hashlib.sha256(body.rstrip().encode("utf-8")).hexdigest()[:KEY_LEN]


@dataclass(frozen=True)
class Entry:
    """人が下した判定1件。"""
    repo: str
    path: str
    declaration: str
    line_key: str
    verdict: str
    why: str
    who: str
    when: str
    excerpt: str = ""        # 参考。判定には使わない（鍵は line_key だけ）

    @property
    def invalid(self) -> str:
        """無効なら理由を返す。無効な記録は抑制しない。"""
        if self.verdict not in VERDICTS:
            return f"verdict が {VERDICTS} のいずれでもない: {self.verdict!r}"
        if len(self.why.strip()) < MIN_REASON:
            return f"理由が短すぎる（{MIN_REASON} 文字以上）: {self.why!r}"
        if len(self.line_key) != KEY_LEN or not all(
                c in "0123456789abcdef" for c in self.line_key):
            return f"鍵の形が違う（16 桁の16進）: {self.line_key!r}"
        if not (self.repo and self.path and self.declaration):
            return "repo / path / declaration のいずれかが空"
        return ""

    @property
    def suppresses(self) -> bool:
        """抑制するか。`leak` は記録であって抑制ではない。"""
        return not self.invalid and self.verdict == "cleared"

    def covers(self, repo: str, declaration: str, hit: Hit) -> bool:
        return (self.repo == repo and self.path == hit.rel
                and self.declaration == declaration
                and self.line_key == line_key(hit.body))


@dataclass
class Log:
    """記録の全体。読めなかったことと、記録がゼロであることを区別する。"""
    entries: list[Entry]
    readable: bool
    where: Path

    @property
    def broken(self) -> list[tuple[Entry, str]]:
        return [(e, e.invalid) for e in self.entries if e.invalid]

    def used(self, repo: str, declaration: str, hits: list[Hit]) -> set[str]:
        """この宣言の当たりを抑制した記録の鍵。"""
        return {e.line_key for e in self.entries if e.suppresses
                for h in hits if e.covers(repo, declaration, h)}

    def filter(self, repo: str, declaration: str,
               hits: list[Hit]) -> tuple[list[Hit], int]:
        """抑制されなかった当たりと、抑制された件数。

        抑制は1件ずつしか効かない。同じファイルの別の行、同じ行の別の宣言は
        それぞれ別に判定が要る。**新しい語が既知のファイルに現れたら抑制しない。**
        """
        kept = [h for h in hits
                if not any(e.covers(repo, declaration, h)
                           for e in self.entries if e.suppresses)]
        return kept, len(hits) - len(kept)


def load() -> Log:
    """記録を読む。無ければ空。読めなければ「読めなかった」と言う。

    記録が無いことは欠陥ではない（まだ誰も判定していないだけ）。読めないことは
    欠陥である——照合対象が無いと抑制が全部外れ、出力が突然増えるだけで済むが、
    逆に「抑制されているつもり」の判断が効かなくなる。区別して返す。
    """
    where = registry_path(AUDIT_FILE)
    if not where.is_file():
        return Log([], True, where)
    try:
        data = _load_toml(where)
    # 壊れ方の種類で扱いは変わらない。どの例外でも「読めなかった」1つに落ちる。
    except Exception as exc:  # noqa: BLE001
        print(f"[audit] 記録を読めません: {where} ({type(exc).__name__}: {exc})\n"
              f"  抑制は1件も効きません。出力が増えているのはそのためです。",
              file=sys.stderr)
        return Log([], False, where)
    out = []
    for item in data.get("entry", []) or []:
        if not isinstance(item, dict):
            continue
        out.append(Entry(
            repo=str(item.get("repo", "")).strip(),
            path=str(item.get("path", "")).strip(),
            declaration=str(item.get("declaration", "")).strip(),
            line_key=str(item.get("line_key", "")).strip().lower(),
            verdict=str(item.get("verdict", "")).strip(),
            why=str(item.get("why", "")),
            who=str(item.get("who", "")).strip(),
            when=str(item.get("when", "")).strip(),
            excerpt=str(item.get("excerpt", "")),
        ))
    return Log(out, True, where)


def health(log: Log, repo: str, live: set[tuple[str, str, str]]) -> list[str]:
    """記録の規模と、死んだ記録。**毎回出す**ための行を作る。

    live は今回の走査で実際に当たった (repo, declaration, line_key) の集合。
    ここに無い記録は、抑制していた当たりがもう存在しない——直ったか、行の本文が
    変わったか、ファイルが消えたか。どれでも「もう効いていない」ので、そう言う。
    黙って残すと、記録は掃除されないまま育つ（rubber-stamp はそこから始まる）。
    """
    mine = [e for e in log.entries if e.repo == repo]
    if not log.readable:
        return [f"記録を読めていません: {log.where}（抑制は1件も効いていません）"]
    if not mine:
        return []
    cleared = [e for e in mine if e.verdict == "cleared"]
    leaks = [e for e in mine if e.verdict == "leak"]
    dead = [e for e in cleared
            if (e.repo, e.declaration, e.line_key) not in live]
    lines = [f"判定の記録 {len(mine)} 件"
             f"（抑制 {len(cleared)} / 混入として記録 {len(leaks)}）"]
    if leaks:
        lines.append(f"  混入として記録された {len(leaks)} 件は抑制していません"
                     f"（記録は免除ではない）")
    if dead:
        lines.append(f"  もう効いていない記録 {len(dead)} 件 — 対象が消えたか本文が"
                     f"変わりました。掃除してください:")
        for e in dead[:5]:
            lines.append(f"    {e.path} [{e.declaration}] {e.line_key} ({e.when})")
    if oldest := min((e.when for e in cleared if e.when), default=""):
        lines.append(f"  最も古い抑制: {oldest}")
    return lines


def record(repo: str, target: str, declaration: str, why: str,
           who: str, verdict: str) -> int:
    """判定を1件書く。鍵は実物の行から計算する。

    人に鍵を書かせない。行の本文のハッシュは手では作れないので、書かせると必ず
    間違い、間違った鍵は「何も抑制しない記録」として静かに積まれる。
    """
    if len(why.strip()) < MIN_REASON:
        print(f"理由が短すぎます（{MIN_REASON} 文字以上）: {why!r}", file=sys.stderr)
        return 2
    rel, _, lineno = target.rpartition(":")
    if not rel or not lineno.isdigit():
        print(f"対象は <path>:<line> の形で指定してください: {target!r}", file=sys.stderr)
        return 2
    src = Path(repo).resolve() / rel
    if not src.is_file():
        print(f"ファイルがありません: {src}", file=sys.stderr)
        return 2
    lines = src.read_text(encoding="utf-8", errors="ignore").splitlines()
    idx = int(lineno) - 1
    if not 0 <= idx < len(lines):
        print(f"{rel} は {len(lines)} 行しかありません: {lineno}", file=sys.stderr)
        return 2
    body = lines[idx]
    entry = Entry(repo=Path(repo).resolve().name, path=rel, declaration=declaration,
                  line_key=line_key(body), verdict=verdict, why=why.strip(),
                  who=who, when=date.today().isoformat(),
                  excerpt=body.strip()[:120])
    where = registry_path(AUDIT_FILE)
    if not where.parent.is_dir():
        print(f"記録の置き場がありません: {where.parent}\n"
              f"  registry の場所を教えてください（WORKOS_REGISTRY=<path>）",
              file=sys.stderr)
        return 2
    if not where.is_file():
        where.write_text(
            "# audit.toml — 人が実物を見て下した判定の記録。config 層。\n"
            "#\n"
            "# 鍵は行の本文のハッシュ（line_key）。行番号は鍵に入れない——行が動い\n"
            "# ただけで抑制が静かに外れるため。行の中身が変われば失効する。\n"
            "# verdict = \"cleared\" だけが抑制する。\"leak\" は記録であって免除ではない。\n"
            "# 1件が抑制できるのは1つの (path, declaration, line_key) だけ。\n"
            "# 書き足すときは engine/audit.py --clear を使う（鍵は手では作れない）。\n",
            encoding="utf-8")
    with where.open("a", encoding="utf-8") as fh:
        fh.write("\n[[entry]]\n")
        for key, val in (("repo", entry.repo), ("path", entry.path),
                         ("declaration", entry.declaration),
                         ("line_key", entry.line_key), ("verdict", entry.verdict),
                         ("why", entry.why), ("who", entry.who),
                         ("when", entry.when), ("excerpt", entry.excerpt)):
            fh.write(f"{key} = {json.dumps(val, ensure_ascii=False)}\n")
    print(f"記録しました: {entry.path} [{entry.declaration}] {entry.line_key}")
    print(f"  置き場: {where}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="人が下した判定を記録し、検査に反映する")
    ap.add_argument("--list", action="store_true", help="記録を全部出す")
    ap.add_argument("--health", metavar="REPO", help="記録の規模と死んだ記録を出す")
    ap.add_argument("--clear", nargs=3, metavar=("REPO", "PATH:LINE", "宣言"),
                    help="この1件は混入ではないと記録する")
    ap.add_argument("--leak", nargs=3, metavar=("REPO", "PATH:LINE", "宣言"),
                    help="この1件は混入であると記録する（抑制しない）")
    ap.add_argument("--why", default="", help="判定の理由（必須）")
    ap.add_argument("--who", default="", help="判定した人")
    args = ap.parse_args()

    if args.clear or args.leak:
        target = args.clear or args.leak
        return record(target[0], target[1], target[2], args.why, args.who,
                      "cleared" if args.clear else "leak")

    log = load()
    if args.health:
        # live を渡せないので、ここでは規模だけ。死んだ記録はゲート側が出す
        # （何が当たったかを知っているのはゲートだけである）。
        for line in health(log, Path(args.health).resolve().name, set()) or ["記録なし"]:
            print(line)
        return 0 if log.readable else 3

    if not log.entries:
        print(f"判定の記録はまだありません: {log.where}")
        return 0
    for e in log.entries:
        mark = "抑制" if e.suppresses else ("混入" if e.verdict == "leak" else "無効")
        print(f"  [{mark}] {e.repo}/{e.path} [{e.declaration}] {e.line_key}")
        print(f"         {e.why}  — {e.who or '?'} {e.when}")
        if e.invalid:
            print(f"         この記録は効いていません: {e.invalid}")
    if log.broken:
        print(f"\n無効な記録が {len(log.broken)} 件あります（抑制していません）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
