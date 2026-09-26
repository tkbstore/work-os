#!/usr/bin/env python3
"""build() が「人が持つ欄」と「機械が持つ欄」を分けて扱うことを検査する。

catalog.py のヘッダは2つを別のものとして宣言している。

    purpose は README/CLAUDE.md からの下書き。目視で直してよい。
    terms はコードの形から導いた能力語。直すのではなく再生成すること。

実装はこの宣言を見ておらず、purpose も毎回 README から作り直していた。
直してよいと書いてある欄が、直すと次の生成で消える置き場になっていた。
実測 2026-09-26: 台帳のズレを直すために再生成すると、手で書いた 2 本が消える。

  python3 tests/test_catalog_purpose.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CATALOG_PY = ROOT / "engine" / "catalog.py"

failures: list[str] = []
checked = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global checked
    checked += 1
    if not ok:
        failures.append(f"{name}: {detail}" if detail else name)


def repo(root: Path, name: str, readme: str, code: str = "") -> None:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / ".git").mkdir(exist_ok=True)
    (d / "README.md").write_text(readme, encoding="utf-8")
    if code:
        (d / "main.py").write_text(code, encoding="utf-8")


def build(registry: Path, fleet: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "WORKOS_REGISTRY": str(registry)}
    return subprocess.run([sys.executable, str(CATALOG_PY), str(fleet), "--build"],
                          cwd=ROOT, capture_output=True, text=True, timeout=120, env=env)


def purpose_of(catalog: Path, name: str) -> str:
    """素朴に読む。TOML の読み手を挟まずに、書かれた形そのものを見る。"""
    lines = catalog.read_text(encoding="utf-8").splitlines()
    for i, ln in enumerate(lines):
        if ln.strip() == f'name    = "{name}"':
            for nxt in lines[i + 1:i + 3]:
                if nxt.startswith("purpose = "):
                    return nxt[len('purpose = "'):].rstrip('"')
    return "(見つからない)"


with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    fleet, reg = tmp / "fleet", tmp / "registry"
    repo(fleet, "alpha", "README の1文目である。\n")
    repo(fleet, "empty-purpose", "# 見出しだけ\n")
    build(reg, fleet)
    catalog = reg / "catalog.toml"

    check("下書きが入る", purpose_of(catalog, "alpha") == "README の1文目である。",
          purpose_of(catalog, "alpha"))
    check("README に文が無ければ空", purpose_of(catalog, "empty-purpose") == "",
          purpose_of(catalog, "empty-purpose"))

    # 人が直す
    body = catalog.read_text(encoding="utf-8").replace(
        'purpose = "README の1文目である。"', 'purpose = "人が書き直した説明。"')
    catalog.write_text(body, encoding="utf-8")

    # README も別に育つ（下書きは変わる）
    (fleet / "alpha" / "README.md").write_text("README が書き換わった1文。\n", encoding="utf-8")
    p2 = build(reg, fleet)

    check("手で直した purpose が残る",
          purpose_of(catalog, "alpha") == "人が書き直した説明。",
          purpose_of(catalog, "alpha"))
    check("残したことを黙らない", "purpose は残しました" in p2.stdout, p2.stdout[:300])
    check("差がある名前を出す", "alpha" in p2.stdout, p2.stdout[:300])
    check("下書きの中身も出す", "README が書き換わった1文" in p2.stdout, p2.stdout[:300])

    # 空のままの欄は、README が育てば埋まる
    (fleet / "empty-purpose" / "README.md").write_text("あとから書いた1文。\n", encoding="utf-8")
    build(reg, fleet)
    check("空の欄は下書きで埋まる",
          purpose_of(catalog, "empty-purpose") == "あとから書いた1文。",
          purpose_of(catalog, "empty-purpose"))

    # 新しいリポは下書きから入る
    repo(fleet, "later", "後から来たリポの1文。\n")
    build(reg, fleet)
    check("新しいリポは下書きから入る",
          purpose_of(catalog, "later") == "後から来たリポの1文。",
          purpose_of(catalog, "later"))

with tempfile.TemporaryDirectory() as td:
    # terms は逆向き。人が書き換えても再生成で上書きされる（機械が持つ欄である）
    tmp = Path(td)
    fleet, reg = tmp / "fleet", tmp / "registry"
    repo(fleet, "alpha", "1文。\n", code="import requests\n")
    build(reg, fleet)
    catalog = reg / "catalog.toml"
    before = catalog.read_text(encoding="utf-8")
    check("terms が入っている（前提）", "requests" in before, before[:300])

    catalog.write_text(before.replace('terms   = ["requests"]',
                                      'terms   = ["人が書いた嘘"]'), encoding="utf-8")
    build(reg, fleet)
    after = catalog.read_text(encoding="utf-8")
    check("terms は再生成で上書きされる", "人が書いた嘘" not in after, after[:400])
    check("terms は形から入り直す", "requests" in after, after[:400])


print(f"[test_catalog_purpose] {checked} 件検査")
if failures:
    for f in failures:
        print(f"  NG {f}", file=sys.stderr)
    print(f"[test_catalog_purpose] FAIL {len(failures)} / {checked}", file=sys.stderr)
    raise SystemExit(1)
print("[test_catalog_purpose] PASS")
