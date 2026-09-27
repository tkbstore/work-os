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


def purpose_rows(catalog: Path) -> list:
    sys.path.insert(0, str(ROOT / "engine"))
    from workos import _load_toml  # noqa: PLC0415
    return list(_load_toml(catalog)["repo"])


def purpose_of_toml(catalog: Path, name: str) -> str:
    """TOML として読んだ値。読めないことも1つの答えとして返す（検査を道連れにしない）。"""
    try:
        rows = purpose_rows(catalog)
    except Exception as e:  # noqa: BLE001
        return f"(読めない: {type(e).__name__})"
    for row in rows:
        if str(row.get("name")) == name:
            return str(row.get("purpose", ""))
    return "(見つからない)"


def set_purpose(registry: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "WORKOS_REGISTRY": str(registry)}
    return subprocess.run([sys.executable, str(CATALOG_PY), "--set-purpose", *args],
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


with tempfile.TemporaryDirectory() as td:
    # 「目視で直してよい」欄には、直すための操作が要る。エディタで生成物を開く運びだと
    # --build との順序次第で消える。--set-purpose は purpose だけを名前で書き換える。
    tmp = Path(td)
    fleet, reg = tmp / "fleet", tmp / "registry"
    repo(fleet, "alpha", "# 見出しだけ\n", code="import requests\n")
    build(reg, fleet)
    catalog = reg / "catalog.toml"
    check("前提: 空で始まる", purpose_of(catalog, "alpha") == "",
          purpose_of(catalog, "alpha"))
    meta_before = [ln for ln in catalog.read_text(encoding="utf-8").splitlines()
                   if ln.startswith("generated_at")]

    r = set_purpose(reg, "alpha", '手で入れた1行。"引用符"と\\も通る。')
    check("--set-purpose は 0 を返す", r.returncode == 0, r.stderr[:300])
    check("旧と新を両方出す", "旧:" in r.stdout and "新:" in r.stdout, r.stdout[:300])

    # 書いた形が TOML として読み直せること（エスケープの往復）
    # エスケープを落とすと、読み手が例外で死ぬ。検査が道連れで落ちると残りを見ないので、
    # 「読めなかった」も1件の NG として受け止める。
    want = '手で入れた1行。"引用符"と\\も通る。'
    try:
        rows = {str(x.get("name")): str(x.get("purpose", ""))
                for x in purpose_rows(catalog)}
    except Exception as e:  # noqa: BLE001
        rows = {}
        check("書いた形が TOML として読める", False, f"{type(e).__name__}: {e}")
    else:
        check("書いた形が TOML として読める", True)
    check("エスケープが往復する", rows.get("alpha") == want, repr(rows.get("alpha")))
    body = catalog.read_text(encoding="utf-8")
    check("terms を触らない", '"requests"' in body, body[:400])
    check("meta を触らない",
          [ln for ln in body.splitlines() if ln.startswith("generated_at")] == meta_before)

    # 入れた値は、次の再生成でも残る（kept として扱われる）
    build(reg, fleet)
    check("再生成しても残る", want == purpose_of_toml(catalog, "alpha"),
          purpose_of_toml(catalog, "alpha"))

    # 落ちるべきときに落ちる。落ちたときは1文字も書かない
    before = catalog.read_text(encoding="utf-8")
    for label, args in (("当たらない名前", ("alpha", "書かれてはいけない", "no-such", "x")),
                        ("奇数個", ("alpha",)),
                        ("120字超", ("alpha", "あ" * 121))):
        r = set_purpose(reg, *args)
        check(f"{label} で 2 を返す", r.returncode == 2, f"{r.returncode} / {r.stderr[:200]}")
        check(f"{label} で何も書かない", catalog.read_text(encoding="utf-8") == before)

    # カタログが無いときは「確かめていない」側（1）。ズレの 2 に畳まない
    r = set_purpose(tmp / "empty-registry", "alpha", "x")
    check("カタログが無ければ 1", r.returncode == 1, f"{r.returncode} / {r.stderr[:200]}")


with tempfile.TemporaryDirectory() as td:
    # 太字の ** を箇条書きの * と取り違えていた。飛ばす印は「印＋空白」で初めて印である。
    # 実測 2026-09-27: scrapers の README で先頭の1文が飛ばされ、次の行
    # 「分析・スコアリングは別リポの仕事」——何を"しない"物かの説明——が purpose になった。
    # 空欄ではないので gate も --verify も通る。台帳は静かに間違ったまま通る。
    tmp = Path(td)
    fleet, reg = tmp / "fleet", tmp / "registry"
    repo(fleet, "bold-lead",
         "# bold-lead\n\n**収集専任の層。** 各ファイルが単体の CLI である。\n"
         "分析は別リポの仕事。\n")
    repo(fleet, "real-bullet", "# real-bullet\n\n- one\n* two\n+ three\n本文の1文。\n")
    repo(fleet, "italic-lead", "# italic-lead\n\n*斜体で始まる1文である。*\n")
    repo(fleet, "quote-and-table", "# q\n\n> 引用。\n| 表 | 組 |\n`コード`\n[link]: x\n本文の1文。\n")
    build(reg, fleet)
    catalog = reg / "catalog.toml"

    check("太字で始まる先頭文を採る",
          purpose_of_toml(catalog, "bold-lead") == "**収集専任の層。** 各ファイルが単体の CLI である。",
          purpose_of_toml(catalog, "bold-lead"))
    check("本当の箇条書きは飛ばす",
          purpose_of_toml(catalog, "real-bullet") == "本文の1文。",
          purpose_of_toml(catalog, "real-bullet"))
    check("斜体で始まる1文も採る",
          purpose_of_toml(catalog, "italic-lead") == "*斜体で始まる1文である。*",
          purpose_of_toml(catalog, "italic-lead"))
    check("引用・表・コード・リンク定義は飛ばす",
          purpose_of_toml(catalog, "quote-and-table") == "本文の1文。",
          purpose_of_toml(catalog, "quote-and-table"))

    # 何が当たって飛ばしたかを言えること。言えない分類器は間違いが見えない
    env = {**os.environ, "WORKOS_REGISTRY": str(reg)}
    r = subprocess.run([sys.executable, str(CATALOG_PY), "--why-purpose",
                        str(fleet / "real-bullet")],
                       cwd=ROOT, capture_output=True, text=True, timeout=60, env=env)
    check("--why-purpose が 0 を返す", r.returncode == 0, r.stderr[:200])
    check("採った行を出す", "採用" in r.stdout, r.stdout[:300])
    # 当たった印そのものを出していること。行の中身に同じ語が混ざっても
    # 通らないよう、印の表記まで含めて見る。
    check("飛ばした印を名指しする",
          "箇条書き '-'+空白" in r.stdout and "箇条書き '*'+空白" in r.stdout
          and "前置き '#'" in r.stdout, r.stdout[:400])

    # 空欄・下書き差分の一覧を打ち切らない（件数だけ出して中身を隠すと穴が見えない）
    for i in range(10):
        repo(fleet, f"noreadme{i}", "# 見出しだけ\n")
    p3 = build(reg, fleet)
    check("空欄を10件以上でも全部名指しする",
          all(f"noreadme{i}" in p3.stdout for i in range(10)), p3.stdout[:500])


print(f"[test_catalog_purpose] {checked} 件検査")
if failures:
    for f in failures:
        print(f"  NG {f}", file=sys.stderr)
    print(f"[test_catalog_purpose] FAIL {len(failures)} / {checked}", file=sys.stderr)
    raise SystemExit(1)
print("[test_catalog_purpose] PASS")
