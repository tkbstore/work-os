#!/usr/bin/env python3
"""catalog.py — 「それ、もう作ってあるか？」に答えられるカタログを作る。読み取り専用。

registry/repos.toml は**存在**を漏れなく持っているが、**能力**を持っていない。
だから「pdf」で引くとカタログのヒットは0、実際には26リポに実装がある、という
逆の答えを返す。カタログの沈黙を不在の証拠と読むと、重複を作る。

このカタログは手で書かない。書いたものは腐るからである。コードから機械的に導く。

  python3 engine/catalog.py <repos> --build     # registry/catalog.toml を生成
  python3 engine/catalog.py --find pdf          # どのリポが持っているか
  python3 engine/catalog.py --find pdf --why    # 何を根拠にそう言うか

外部依存なし。生成先は registry/（非公開層）。engine には事実を持たせない。
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
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
            return t[:120]
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
    rows = []
    for repo in iter_repo_dirs(root):
        terms = sorted(capability_terms(repo))
        rows.append((repo.name, read_purpose(repo), terms))

    def esc(s: str) -> str:
        return s.replace('\\', '\\\\').replace('"', '\\"')

    out = ["# catalog.toml — engine/catalog.py で生成。手で書かない（書いたものは腐る）。",
           "# purpose は README/CLAUDE.md からの下書き。目視で直してよい。",
           "# terms はコードの形から導いた能力語。直すのではなく再生成すること。",
           f"# {len(rows)} repositories", ""]
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


def main() -> int:
    ap = argparse.ArgumentParser(description="能力で引けるカタログ")
    ap.add_argument("root", nargs="?", type=Path, help="--build のとき必要")
    ap.add_argument("--build", action="store_true", help="カタログを生成する")
    ap.add_argument("--find", metavar="TERM", help="能力語で引く")
    ap.add_argument("--why", action="store_true", help="一致の根拠を出す")
    ap.add_argument("--compare", nargs="+", metavar="REPO",
                    help="リポ1つ＝近いものを列挙 / 2つ＝共通語を出す")
    ap.add_argument("--limit", type=int, default=8, help="--compare の表示件数")
    args = ap.parse_args()

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
