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
    # purpose は「飛ばさない最初の1行」ではなく「最初の意味のある文」である。
    # docstring はずっとそう宣言していたのに、実装は行を返していた。行を単位にすると
    # README の書き方次第で2通りに壊れる（実測 2026-09-27、82 本を照合）。
    #   行で折られた1文 → 断片（sales-tel「…APIを呼び出し、」で切れていた）
    #   1行に2文        → 2文目が落ちる（「X monorepo. AEO product.」が前半だけになる）
    # さらに Markdown の飾りが値に残っていた。purpose は TOML の値であって
    # Markdown ではないので、飾りを落とすのは抽出側の仕事である。台帳側で手で
    # 落とすと、下書きと台帳が永久に食い違い「下書きと違う」が鳴り続けて
    # 本物のズレが埋もれる（work-os-registry-1e の指摘）。
    tmp = Path(td)
    fleet, reg = tmp / "fleet", tmp / "registry"
    repo(fleet, "bold-lead",
         "# bold-lead\n\n**収集専任の層。** 各ファイルが単体の CLI である。\n")
    repo(fleet, "wrapped", "# wrapped\n\n**電話営業の入口。** API を呼び出し、\n発信と着信を実装する。\n")
    repo(fleet, "two-sentences", "# two\n\nAlpha monorepo. Beta product.\n")
    repo(fleet, "real-bullet", "# real-bullet\n\n- one\n* two\n+ three\n本文の1文。\n")
    repo(fleet, "italic-lead", "# italic-lead\n\n*斜体で始まる1文である。*\n")
    repo(fleet, "inline-code", "# c\n\n各ファイルが `import` して使う層は持たない。\n")
    repo(fleet, "quote-and-table", "# q\n\n> 引用。\n| 表 | 組 |\n`コード`\n[link]: x\n本文の1文。\n")
    # 飾りしか無い行（<p align="center"> だけの行と閉じタグ）。中身のあるタグ行は
    # 別の検査（prose-after-tag）で拾う。
    repo(fleet, "tag-only", "<p align=\"center\">\n</p>\n<br>\n本文の1文。\n")
    repo(fleet, "prose-after-tag", "<p align=\"center\">A terminal for agents.</p>\n")
    repo(fleet, "nav-row", "# n\n\nEnglish | 日本語 | 한국어 | Deutsch\n本文の1文。\n")
    # 1行ずつ見ると散文、繋ぐと案内の行になるもの。行末に区切りが1つずつ置かれた形。
    repo(fleet, "nav-joined",
         "# nj\n\nHosted for teams →·\nDocs ·\nDiscord ·\nContact\n\n本文の1文。\n")
    # 全角の空白畳みが em dash まで食っていた
    repo(fleet, "em-dash", "# e\n\nMarketing AI MVP — 仮説検証の基盤である。\n")
    repo(fleet, "em-dash-jp", "# e\n\nプラットフォーム — 特定顧客向けの実装。\n")
    repo(fleet, "badge-only", "# b\n\n![platform](https://x/y.svg)\n本文の1文。\n")
    repo(fleet, "long-first",
         "# l\n\n" + "あ" * 130 + "。\n")
    repo(fleet, "budget", "# b\n\n" + "あ" * 60 + "。" + "い" * 40 + "。" + "う" * 40 + "。\n")
    build(reg, fleet)
    catalog = reg / "catalog.toml"

    def got(name: str) -> str:
        return purpose_of_toml(catalog, name)

    check("太字で始まる文を採り、飾りは落とす",
          got("bold-lead") == "収集専任の層。各ファイルが単体の CLI である。", got("bold-lead"))
    check("行で折られた文を繋ぐ",
          got("wrapped") == "電話営業の入口。API を呼び出し、発信と着信を実装する。", got("wrapped"))
    check("1行に2文あれば2文とも採る",
          got("two-sentences") == "Alpha monorepo. Beta product.", got("two-sentences"))
    check("本当の箇条書きは飛ばす", got("real-bullet") == "本文の1文。", got("real-bullet"))
    check("斜体の記号も落とす", got("italic-lead") == "斜体で始まる1文である。", got("italic-lead"))
    check("inline code の ` を落とす",
          got("inline-code") == "各ファイルが import して使う層は持たない。", got("inline-code"))
    check("引用・表・コード・リンク定義は飛ばす",
          got("quote-and-table") == "本文の1文。", got("quote-and-table"))
    check("飾りしか無い行は飛ばす", got("tag-only") == "本文の1文。", got("tag-only"))
    check("タグの後ろの散文は拾う",
          got("prose-after-tag") == "A terminal for agents.", got("prose-after-tag"))
    check("言語切替のような区切りの行は飛ばす", got("nav-row") == "本文の1文。", got("nav-row"))
    # 捨てるのは段落ごと。先頭行だけ落とすと、残りが同じ形で通る
    check("繋ぐと案内の行になるものは段落ごと飛ばす",
          got("nav-joined") == "本文の1文。", got("nav-joined"))
    check("em dash の前後の空白を食わない（英文側）",
          got("em-dash") == "Marketing AI MVP — 仮説検証の基盤である。", got("em-dash"))
    check("em dash の前後の空白を食わない（全角側）",
          got("em-dash-jp") == "プラットフォーム — 特定顧客向けの実装。", got("em-dash-jp"))
    check("画像だけの行は飛ばす", got("badge-only") == "本文の1文。", got("badge-only"))

    # 予算（120字）の扱い。文の途中で切らない
    check("1文目が予算を超えるときだけ途中で切る",
          len(got("long-first")) == 120, f"{len(got('long-first'))}")
    b = got("budget")
    check("予算に収まるぶんを文単位で詰める",
          b.endswith("。") and len(b) <= 120 and b.count("。") == 2, f"{len(b)} / {b[:40]}")

    # 何が当たって飛ばしたかを言えること。言えない分類器は間違いが見えない
    env = {**os.environ, "WORKOS_REGISTRY": str(reg)}
    r = subprocess.run([sys.executable, str(CATALOG_PY), "--why-purpose",
                        str(fleet / "real-bullet")],
                       cwd=ROOT, capture_output=True, text=True, timeout=60, env=env)
    check("--why-purpose が 0 を返す", r.returncode == 0, r.stderr[:200])
    check("採った行を出す", "先頭行" in r.stdout, r.stdout[:300])
    check("飛ばした印を名指しする",
          "箇条書き '-'+空白" in r.stdout and "箇条書き '*'+空白" in r.stdout
          and "前置き '#'" in r.stdout, r.stdout[:400])

    # 道具と本体が食い違わないこと。診断が「組み上げる前の生の行」を採用として
    # 出していたため、それを下書きだと思って飾り付きのまま台帳に入れた事故が
    # 起きた（2026-09-27）。道具が本体と違う答えを出すなら無いほうがましである。
    sys.path.insert(0, str(ROOT / "engine"))
    from catalog import purpose_trace, read_purpose  # noqa: PLC0415
    for name in ("bold-lead", "wrapped", "two-sentences", "real-bullet", "nav-joined",
                 "em-dash", "inline-code", "tag-only", "prose-after-tag", "badge-only"):
        rows = purpose_trace(fleet / name)
        shown = [t for _, _, why, t in rows if why == "値"]
        check(f"診断の値が本体と一致する（{name}）",
              shown == [read_purpose(fleet / name)], f"{shown} != {read_purpose(fleet / name)!r}")

    # 繋いだ行と、段落ごと捨てたことが記録に出る
    rows = purpose_trace(fleet / "wrapped")
    check("繋いだ行を記録に出す", any(why == "繋いだ" for _, _, why, _ in rows),
          str(rows))
    rows = purpose_trace(fleet / "nav-joined")
    check("段落ごと捨てたことを記録に出す",
          sum(1 for _, _, why, _ in rows if why.startswith("段落ごと捨てた")) >= 4, str(rows))

    # 空欄・下書き差分の一覧を打ち切らない（件数だけ出して中身を隠すと穴が見えない）
    for i in range(10):
        repo(fleet, f"noreadme{i}", "# 見出しだけ\n")
    p3 = build(reg, fleet)
    check("空欄を10件以上でも全部名指しする",
          all(f"noreadme{i}" in p3.stdout for i in range(10)), p3.stdout[:500])


with tempfile.TemporaryDirectory() as td:
    # worktree は clone と同じ作業ツリーを写している。README から下書きを作ると
    # 本体と一字一句同じ purpose が2行並び、台帳は「別のプロダクトが2つある」と
    # 読める（実測 2026-09-27: worktree が1本できただけでリポジトリ数が 82→83 になり、
    # --verify も gate も通った。通ることが問題だった）。
    # 枝であることは機械が読み取れる事実なので、terms と同じ機械の欄にする。
    tmp = Path(td)
    fleet, reg = tmp / "fleet", tmp / "registry"
    repo(fleet, "main-repo", "本体の説明である。\n")
    # worktree を手で作る（git を呼ばない。.git がファイルで gitdir: を指す形）
    wt = fleet / "main-repo-topic"
    wt.mkdir(parents=True)
    (wt / "README.md").write_text("本体の説明である。\n", encoding="utf-8")
    gitdir = fleet / "main-repo" / ".git" / "worktrees" / "main-repo-topic"
    gitdir.mkdir(parents=True)
    (gitdir / "HEAD").write_text("ref: refs/heads/feat/topic\n", encoding="utf-8")
    (wt / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
    p4 = build(reg, fleet)
    catalog = reg / "catalog.toml"

    v = purpose_of_toml(catalog, "main-repo-topic")
    check("worktree は本体の説明を写さない", v != "本体の説明である。", v)
    check("どの clone の枝かを言う", "main-repo" in v and "feat/topic" in v, v)
    check("worktree と名指しする", "worktree" in v, v)
    check("本体のほうは普通に README から入る",
          purpose_of_toml(catalog, "main-repo") == "本体の説明である。",
          purpose_of_toml(catalog, "main-repo"))
    check("数から消さない（存在は数える）", "main-repo-topic" in catalog.read_text(encoding="utf-8"))
    check("worktree であることを報告に出す", "git worktree" in p4.stdout, p4.stdout[:400])

    # 機械の欄なので、人が書き換えても再生成で戻る（terms と同じ向き）
    body = catalog.read_text(encoding="utf-8").replace(v, "人が書いた別の説明。")
    catalog.write_text(body, encoding="utf-8")
    build(reg, fleet)
    check("worktree の purpose は再生成で上書きされる",
          purpose_of_toml(catalog, "main-repo-topic") == v,
          purpose_of_toml(catalog, "main-repo-topic"))

    # submodule は worktree ではない（同じ .git ファイル形式だが別物）
    sm = fleet / "sub"
    sm.mkdir()
    (sm / "README.md").write_text("submodule の説明。\n", encoding="utf-8")
    smdir = fleet / "main-repo" / ".git" / "modules" / "sub"
    smdir.mkdir(parents=True)
    (sm / ".git").write_text(f"gitdir: {smdir}\n", encoding="utf-8")
    build(reg, fleet)
    check("submodule は worktree として扱わない",
          purpose_of_toml(catalog, "sub") == "submodule の説明。",
          purpose_of_toml(catalog, "sub"))


print(f"[test_catalog_purpose] {checked} 件検査")
if failures:
    for f in failures:
        print(f"  NG {f}", file=sys.stderr)
    print(f"[test_catalog_purpose] FAIL {len(failures)} / {checked}", file=sys.stderr)
    raise SystemExit(1)
print("[test_catalog_purpose] PASS")
