#!/usr/bin/env python3
"""catalog.py — 「それ、もう作ってあるか？」に答えられるカタログを作る。読み取り専用。

registry/repos.toml は**存在**を漏れなく持っているが、**能力**を持っていない。
だから「pdf」で引くとカタログのヒットは0、実際には26リポに実装がある、という
逆の答えを返す。カタログの沈黙を不在の証拠と読むと、重複を作る。

このカタログは手で書かない。書いたものは腐るからである。コードから機械的に導く。

  python3 engine/catalog.py <repos> --build     # registry/catalog.toml を生成
  python3 engine/catalog.py --verify            # 宣言と実在のズレを出す
  python3 engine/catalog.py --set-purpose <名前> <本文>   # purpose を手で直す
  python3 engine/catalog.py --find pdf          # どのリポが持っているか
  python3 engine/catalog.py --find pdf --why    # 何を根拠にそう言うか

生成物には出自（[meta]: いつ・どこで・何本数えたか）を書き残す。件数だけでは実在と
比べられない。実測 2026-09-26: 5 本消えて 5 本増えたカタログが 82 / 82 で一致していた。

外部依存なし。生成先は registry/（非公開層）。engine には事実を持たせない。
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from workos import iter_repo_dirs, registry_path  # noqa: E402

CATALOG = registry_path("catalog.toml")
SRC_EXT = {".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".rb", ".sh"}
SKIP_DIRS = {".git", "node_modules", "venv", "env", ".venv", "__pycache__", "dist",
             "build", ".next", "sessions", "data", "outputs", "out", ".claude"}
STOP = {"src", "lib", "app", "test", "tests", "main", "index", "utils", "util",
        "config", "types", "core", "common", "scripts", "tools", "api", "cli",
        "init", "setup", "run", "helpers", "models", "schemas", "base"}
MAX_DEPTH = 3
PURPOSE_MAX = 120


def read_purpose(repo: Path) -> str:
    """README / CLAUDE.md の最初の意味のある1文を purpose の下書きにする。"""
    for name in ("README.md", "CLAUDE.md", "README.rst"):
        f = repo / name
        if not f.is_file():
            continue
        try:
            lines = f.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for line in lines[:40]:
            t = line.strip()
            if not t or t.startswith(("#", "<!--", "-", "*", "|", "`", ">", "[")):
                continue
            t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)
            return t[:PURPOSE_MAX]
    return ""


def capability_terms(repo: Path) -> set[str]:
    """コードの形から能力語を拾う。トップレベルのパス名比較では届かない層を見る。"""
    terms: set[str] = set()

    def walk(d: Path, depth: int) -> None:
        if depth > MAX_DEPTH:
            return
        try:
            entries = list(d.iterdir())
        except OSError:
            return
        for e in entries:
            if e.name.startswith(".") and e.name != ".claude":
                continue
            if e.is_dir():
                if e.name in SKIP_DIRS:
                    continue
                if (n := e.name.lower().replace("_", "-")) not in STOP and len(n) > 2:
                    terms.add(n)
                walk(e, depth + 1)
            elif e.suffix in SRC_EXT:
                stem = e.stem.lower().replace("_", "-")
                if stem not in STOP and len(stem) > 2 and not stem.startswith("test-"):
                    terms.add(stem)

    walk(repo, 0)

    # import 文は能力そのものである。ファイル名に出ない能力（scraping 等）はここで拾う。
    seen = 0
    for f in repo.rglob("*"):
        if seen >= 250:
            break
        if f.suffix not in {".py", ".ts", ".tsx", ".js"} or not f.is_file():
            continue
        if any(part in SKIP_DIRS for part in f.parts):
            continue
        seen += 1
        try:
            head = "".join(f.open(encoding="utf-8", errors="ignore").readlines()[:40])
        except OSError:
            continue
        for m in re.finditer(r"^\s*(?:import|from)\s+([A-Za-z0-9_.@/-]+)", head, re.M):
            mod = m.group(1).split(".")[0].lstrip("@").split("/")[0].lower()
            if mod not in STOP and len(mod) > 2 and not mod.startswith(("_", ".")):
                terms.add(mod)
        for m in re.finditer(r"""from\s+["']([A-Za-z0-9_@/-]+)["']""", head):
            mod = m.group(1).lstrip("@").split("/")[0].lower()
            if mod not in STOP and len(mod) > 2:
                terms.add(mod)

    # 宣言されている依存とプロジェクト説明も能力の一部である
    for meta, keys in ((repo / "package.json", ("dependencies", "devDependencies")),
                       (repo / "pyproject.toml", ())):
        if not meta.is_file():
            continue
        try:
            text = meta.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if meta.suffix == ".json":
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                continue
            for k in keys:
                terms.update(n.lower().lstrip("@").split("/")[-1] for n in (data.get(k) or {}))
        else:
            for m in re.finditer(r'^\s*"?([A-Za-z0-9_.-]+)[><=~!]{1,2}', text, re.M):
                terms.add(m.group(1).lower())
    return {t for t in terms if len(t) > 2}


def build(root: Path) -> int:
    # purpose は人が持ち、terms は機械が持つ。このファイルのヘッダがそう宣言している
    # （「purpose は下書き。目視で直してよい」「terms は直すのではなく再生成すること」）。
    # 実装はその宣言を見ていなかった。毎回 README から作り直して人の修正を上書きするので、
    # 台帳のズレを直す操作そのものが手で直した purpose を壊していた（実測 2026-09-26: 2 本）。
    # 直せと言われている欄を、直しても消える置き場にしてはいけない。
    kept = {str(r.get("name") or ""): str(r.get("purpose") or "") for r in load()}
    rows, drafts = [], []
    for repo in iter_repo_dirs(root):
        terms = sorted(capability_terms(repo))
        draft = read_purpose(repo)
        purpose = kept.get(repo.name) or draft
        if purpose != draft and draft:
            drafts.append((repo.name, purpose, draft))
        rows.append((repo.name, purpose, terms))

    def esc(s: str) -> str:
        return s.replace('\\', '\\\\').replace('"', '\\"')

    out = ["# catalog.toml — engine/catalog.py で生成。手で書かない（書いたものは腐る）。",
           "# purpose は README/CLAUDE.md からの下書き。目視で直してよい。",
           "# terms はコードの形から導いた能力語。直すのではなく再生成すること。",
           "",
           "# 生成の出自。--verify がこれを読んで、宣言と実在のズレを出す。",
           "# 以前はここに件数だけを書いていた（\"82 repositories\"）。件数は実在と",
           "# 比べられない。何がズレたかを言うには「何を、どこで、いつ数えたか」が要る。",
           "# 実測 2026-09-26: 08-31 生成のカタログが 5 本のリポを知らないまま4週間通っていた。",
           "[meta]",
           f'generated_at = "{_now()}"',
           f'root         = "{esc(str(root))}"',
           f"repo_count   = {len(rows)}",
           "require_git  = true", ""]
    for name, purpose, terms in rows:
        out.append("[[repo]]")
        out.append(f'name    = "{esc(name)}"')
        out.append(f'purpose = "{esc(purpose)}"')
        out.append(f'terms   = [{", ".join(chr(34) + esc(t) + chr(34) for t in terms[:120])}]')
        out.append("")
    CATALOG.parent.mkdir(parents=True, exist_ok=True)
    CATALOG.write_text("\n".join(out), encoding="utf-8")
    total = sum(len(t) for _, _, t in rows)
    print(f"生成: {CATALOG}（{len(rows)} リポジトリ / 能力語 {total} 件）")
    missing = [n for n, p, _ in rows if not p]
    if missing:
        print(f"purpose が空 {len(missing)} 件（README も CLAUDE.md も無い）: "
              f"{', '.join(missing[:8])}")
    if drafts:
        # 黙って人の側を残すと、README が育っても台帳が古い説明を持ち続ける。
        # 採るかどうかは人が決めるので、下書きとの差を毎回出す。
        print(f"purpose は残しました。README 側の下書きと違うもの {len(drafts)} 件："
              "採るなら手で書き換えてください。")
        for name, purpose, draft in drafts[:8]:
            print(f"  {name}\n    台帳: {purpose[:70]}\n    下書き: {draft[:70]}")
    return 0


def _now() -> str:
    """生成時刻。TOML の日付型ではなく文字列で持つ。

    読み側が2系統ある（tomllib と workos の部分実装）ので、両方が同じに読める
    形にそろえる。鮮度の記録それ自体が読めなくなるのが一番まずい壊れ方である。
    """
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")


def meta() -> dict:
    if not CATALOG.exists():
        return {}
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from workos import _load_toml  # noqa: PLC0415
    m = _load_toml(CATALOG).get("meta")
    return dict(m) if isinstance(m, dict) else {}


def verify(root: Path | None) -> int:
    """カタログの宣言と、いま実在するリポジトリを突き合わせる。

    終了コードは3値。workos-qa.py の規約と同じ理由で「確かめていない」を
    「落ちた」に畳まない。畳むと、出自の無い古いカタログが差し戻しを出し続けて
    読まれなくなる。読まれない検査は無いのと同じである。

      0  宣言と実在が一致した
      1  確かめられない（カタログが無い / 出自が無い / 根が消えた）
      2  ズレた。どの名前がどちら側に在るかを出す
    """
    rows = load()
    if not rows:
        print("カタログがありません。先に --build してください。"
              "（確かめていません。ズレていないという意味ではありません）",
              file=sys.stderr)
        return 1

    m = meta()
    if root is None:
        declared = str(m.get("root") or "")
        if not declared:
            print("出自が書かれていないカタログです。何をどこで数えたかが分からないので、"
                  "実在と比べられません。--build で作り直すか、根を引数で渡してください。"
                  "（確かめていません）", file=sys.stderr)
            return 1
        root = Path(declared)
    root = Path(root).expanduser()
    if not root.is_dir():
        print(f"数えた根が今は在りません: {root}（確かめていません）", file=sys.stderr)
        return 1

    known = {str(r.get("name") or "") for r in rows} - {""}
    live = {d.name for d in iter_repo_dirs(root)}
    unseen = {d.name for d in iter_repo_dirs(root, require_git=False)} - live

    missing = sorted(live - known)
    stale = sorted(known - live)

    when = str(m.get("generated_at") or "不明")
    print(f"カタログ {len(known)} 本 / 実在 {len(live)} 本   生成 {when}   根 {root}")
    if unseen:
        # ここは「ズレ」ではなく定義の外側。git になっていないものはカタログの
        # 対象ではない（iter_repo_dirs の require_git）。黙ると、木に在るのに
        # どの検査も見ていないディレクトリが沈黙のまま増える。
        print(f"  定義の外 {len(unseen)} 本（.git が無いのでカタログの対象外）: "
              f"{', '.join(sorted(unseen))}")
    for name in missing:
        print(f"  NG 実在するがカタログに無い: {name}")
    for name in stale:
        print(f"  NG カタログに在るが実在しない: {name}")
    if missing or stale:
        print(f"ズレ {len(missing) + len(stale)} 件。"
              f"python3 engine/catalog.py {root} --build で作り直してください。",
              file=sys.stderr)
        return 2
    print("一致。")
    return 0


def weights(rows: list[dict]) -> dict[str, float]:
    """語の重み。多くのリポに出る語ほど軽くする（IDF）。

    除外リストを手で持たない。json / requests / pytest のような汎用語は
    「ほぼ全リポに出る」という観測から自動的に重みがゼロに近づく。
    手で書いた除外リストは、新しい汎用ライブラリが流行るたびに腐る。
    """
    n = len(rows) or 1
    df: dict[str, int] = {}
    for r in rows:
        for t in {str(x) for x in (r.get("terms") or [])}:
            df[t] = df.get(t, 0) + 1
    return {t: math.log(n / c) for t, c in df.items()}


def similarity(a: set[str], b: set[str], w: dict[str, float]) -> float:
    """重み付き Jaccard。共有していても汎用語なら加点されない。"""
    inter = sum(w.get(t, 0.0) for t in a & b)
    union = sum(w.get(t, 0.0) for t in a | b)
    return inter / union if union else 0.0


def compare(name_a: str, name_b: str | None, limit: int) -> int:
    rows = load()
    if not rows:
        print("catalog.toml がありません。先に --build してください。", file=sys.stderr)
        return 2
    index = {str(r["name"]): {str(x) for x in (r.get("terms") or [])} for r in rows}
    purpose = {str(r["name"]): str(r.get("purpose", "")) for r in rows}
    if name_a not in index:
        print(f"'{name_a}' はカタログにありません。", file=sys.stderr)
        return 2
    w = weights(rows)

    if name_b:
        if name_b not in index:
            print(f"'{name_b}' はカタログにありません。", file=sys.stderr)
            return 2
        shared = sorted(index[name_a] & index[name_b], key=lambda t: -w.get(t, 0.0))
        sim = similarity(index[name_a], index[name_b], w)
        print(f"{name_a} ↔ {name_b}   重み付き類似度 {sim:.1%}"
              f"（共通 {len(shared)} 語）\n")
        print("  特徴的な共通語（この2本にしか出ない順）:")
        for t in shared[:15]:
            n_repos = sum(1 for v in index.values() if t in v)
            print(f"    {t:<28} {n_repos} リポに出現")
        generic = [t for t in shared if w.get(t, 0.0) < 0.7]
        if generic:
            print(f"\n  汎用語として無視した {len(generic)} 語: "
                  f"{', '.join(generic[:10])}")
        return 0

    scored = sorted(((similarity(index[name_a], v, w), k) for k, v in index.items()
                     if k != name_a), reverse=True)
    print(f"{name_a} と近いリポジトリ（重み付き類似度・汎用語は自動で除外）\n")
    for sim, k in scored[:limit]:
        if sim <= 0:
            break
        shared = sorted(index[name_a] & index[k], key=lambda t: -w.get(t, 0.0))
        distinctive = [t for t in shared if w.get(t, 0.0) >= 0.7][:5]
        print(f"  {sim:>6.1%}  {k}")
        if purpose.get(k):
            print(f"          {purpose[k][:70]}")
        if distinctive:
            print(f"          特徴語: {', '.join(distinctive)}")
    print("\n  類似度が低くても領域固有語を共有している場合は要確認。"
          "\n  --find <領域語> で突き合わせること（同じ目的の別実装はここに隠れる）。")
    return 0


def load() -> list[dict]:
    if not CATALOG.exists():
        return []
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from workos import _load_toml  # noqa: PLC0415
    return list(_load_toml(CATALOG).get("repo") or [])


def find(term: str, why: bool) -> int:
    rows = load()
    if not rows:
        print("catalog.toml がありません。先に --build してください。", file=sys.stderr)
        return 2
    t = term.lower()
    hits = []
    for r in rows:
        terms = [str(x) for x in (r.get("terms") or [])]
        matched = [x for x in terms if t in x]
        if t in str(r.get("purpose", "")).lower() or t in str(r.get("name", "")).lower():
            matched.insert(0, "(名前/目的)")
        if matched:
            hits.append((r, matched))
    if not hits:
        print(f"'{term}' を持つリポジトリは見つかりません。")
        print("カタログの沈黙は不在の証拠ではありません。次の2つを試してください。\n")
        # 抽象語（scraping 等）はモジュール名に現れない。具体技術名で引き直させる。
        near = sorted({x for r in rows for x in map(str, r.get("terms") or [])
                       if t[:4] and (x.startswith(t[:4]) or t[:4] in x)})[:8]
        if near:
            print(f"  近い語: {', '.join(near)}")
        print(f"  抽象語ではなく具体の技術名で引く（例: 'scraping' ではなく playwright / scrapy）")
        print(f"  それでも出なければ実コードを直接: "
              f"grep -ril '{term}' --include='*.py' --include='*.ts' <repos>")
        return 1
    print(f"'{term}' — {len(hits)} リポジトリ\n")
    for r, matched in hits:
        print(f"  {r['name']}")
        if r.get("purpose"):
            print(f"    {r['purpose']}")
        if why:
            print(f"    根拠: {', '.join(matched[:10])}")
    return 0


def set_purpose(pairs: list[str]) -> int:
    """purpose だけを名前で書き換える。terms と meta は触らない。

    ヘッダは「purpose は目視で直してよい」と宣言しているのに、直す手段が
    「エディタで catalog.toml を開く」しかなかった。台帳は生成物なので、
    手で開く運びだと --build との順序次第で消える（2026-09-26 に実際に消えた）。
    直してよいと書いてある欄には、直すための操作を持たせる。

    2つずつ（名前, 本文）で受ける。1つでも当たらなければ何も書かない。
    """
    if len(pairs) % 2:
        print("--set-purpose は 名前 本文 の2つずつです", file=sys.stderr)
        return 2
    if not CATALOG.exists():
        print(f"{CATALOG} がありません。先に --build してください。", file=sys.stderr)
        return 1
    want = list(zip(pairs[::2], pairs[1::2]))
    known = {str(r.get("name") or "") for r in load()}
    unknown = [n for n, _ in want if n not in known]
    if unknown:
        print(f"カタログに無い名前: {', '.join(unknown)}。--build が先です。", file=sys.stderr)
        return 2
    too_long = [n for n, t in want if len(t) > PURPOSE_MAX]
    if too_long:
        print(f"purpose が {PURPOSE_MAX} 字を超えています: {', '.join(too_long)}", file=sys.stderr)
        return 2

    lines = CATALOG.read_text(encoding="utf-8").splitlines()
    new = list(lines)
    current = ""
    done = []
    for i, line in enumerate(lines):
        m = re.match(r'^name\s*=\s*"(.*)"$', line)
        if m:
            current = m.group(1).replace('\\"', '"')
            continue
        if not line.startswith("purpose"):
            continue
        for name, text in want:
            if name != current:
                continue
            before = re.sub(r'^purpose\s*=\s*"(.*)"$', r"\1", line)
            esc = text.replace("\\", "\\\\").replace('"', '\\"')
            new[i] = f'purpose = "{esc}"'
            done.append((name, before, text))
    CATALOG.write_text("\n".join(new) + "\n", encoding="utf-8")
    for name, before, after in done:
        print(f"{name}\n  旧: {before or '(空)'}\n  新: {after}")
    print(f"{len(done)} 件を書き換えました: {CATALOG}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="能力で引けるカタログ")
    ap.add_argument("root", nargs="?", type=Path, help="--build のとき必要")
    ap.add_argument("--build", action="store_true", help="カタログを生成する")
    ap.add_argument("--verify", action="store_true",
                    help="宣言と実在のズレを出す（root 省略時は出自から読む）")
    ap.add_argument("--set-purpose", nargs="+", metavar="ARG",
                    help="名前 本文 の2つずつ。purpose だけを書き換える")
    ap.add_argument("--find", metavar="TERM", help="能力語で引く")
    ap.add_argument("--why", action="store_true", help="一致の根拠を出す")
    ap.add_argument("--compare", nargs="+", metavar="REPO",
                    help="リポ1つ＝近いものを列挙 / 2つ＝共通語を出す")
    ap.add_argument("--limit", type=int, default=8, help="--compare の表示件数")
    args = ap.parse_args()

    if args.verify:
        return verify(args.root.expanduser().resolve() if args.root else None)
    if args.set_purpose:
        return set_purpose(args.set_purpose)
    if args.build:
        if not args.root:
            print("--build には root が必要です", file=sys.stderr)
            return 2
        return build(args.root.expanduser().resolve())
    if args.compare:
        a = args.compare[0]
        b = args.compare[1] if len(args.compare) > 1 else None
        return compare(a, b, args.limit)
    if args.find:
        return find(args.find, args.why)
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
