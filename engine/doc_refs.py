"""README が名指しする道が、公開する木に在るか（release_gate の観測 doc_refs_resolve）。

README と実装の「合致」そのものは機械では判定できない。ここで見るのは、README が
**確かめられる形で** 主張していることのうち、外れたときに誤検知の少ない2つだけである。

  1. 手順の中のファイル  シェルのコードブロックで `python X.py` / `bash X.sh` /
                         行頭の `./X` として呼ばれるものが、木に在るか
  2. 消した道への言及    本文で名指しした道のうち、**git が「かつて在って消した」と
                         知っている** ものが、まだ README に残っていないか

本文の道を「木に無い」だけで落とさないのは、実測で精度が出なかったからである。
2026-10-03、67 リポの README を測った: 本文の未解決 604 件のうち、大半は出力先・
生成物・gitignore されたデータ・別リポのファイル・ファイル名だけの略記だった。
一方「履歴に在って今は無い」道は、移動して README が追従しなかったもの
（content/ → src/content/）や、消したコンポーネントへの言及が実際に残っていた。
「在ったのに無い」は git が証明するので、推測が要らない。

手順の中のファイルは本文より精度が高い（呼んでいる以上、在ると主張している）が、
`cd` で別のリポへ移ってから打つ手順が実在した。木の外へ出た `cd` の後は見ない。
"""

from __future__ import annotations

import os
import re
from typing import Callable

SHELL_LANGS = {"", "sh", "bash", "zsh", "shell", "console", "shell-session"}
FENCE = re.compile(r"^\s*(```+|~~~+)\s*([\w+-]*)")
EXT = r"(?:py|sh|js|mjs|cjs|ts|tsx|rb|pl)"
# インタプリタに渡されたファイル。`uv run python x.py` もここで拾う（-m は拾わない）。
INTERP = re.compile(
    rf"(?:^|[\s;&|(])(?:python3?|node|bash|sh|zsh|tsx|ts-node|ruby|perl|deno run)"
    rf"\s+((?:\.{{0,2}}/)?[\w@.+-]+(?:/[\w@.+-]+)*\.{EXT})(?=\s|$|[;&|)])")
# 行頭（または ; && | の直後）で実行される ./X。引数位置の ./out は出力先なので見ない。
EXEC = re.compile(r"(?:^|[;&|]\s*)(\./[\w@.+-]+(?:/[\w@.+-]+)*)")
CD = re.compile(r"(?:^|[;&|]\s*)cd\s+(\S+)")
# clone した直後の `cd 名前` は、手元のディレクトリ名ではなくこの木の根へ入る。
CLONE = re.compile(r"\bgit\s+clone\b")
INLINE = re.compile(r"`([^`\s]+)`")
PATHLIKE = re.compile(r"^(?:\./)?[\w@.+-]+(?:/[\w@.+-]+)*/?$")
# 書き方の見本。読み手に置き換えさせる前提の語は、在ることを主張していない。
PLACEHOLDER = re.compile(r"[<>{}$*~]|\.\.\.|path/to|\bmy[-_]|\byour[-_]|YYYY|XXX",
                         re.I)

Present = Callable[[str], bool]


def _blocks(lines: list[str]):
    """(行番号, 行, フェンスの言語 or None, ブロック番号)。フェンスの外は言語 None。"""
    fence = lang = None
    block = 0
    for i, ln in enumerate(lines, 1):
        m = FENCE.match(ln)
        if m and fence is None:
            fence, lang, block = m.group(1)[0] * 3, m.group(2).lower(), block + 1
            continue
        if m and fence is not None and m.group(1).startswith(fence):
            fence = lang = None
            continue
        yield i, ln, (lang if fence is not None else None), block


def _norm(cwd: str, token: str) -> str:
    return os.path.normpath(os.path.join(cwd, token)) if cwd else os.path.normpath(token)


def _enter(cwd: str | None, target: str, present: Present,
           beside: Present) -> tuple[str | None, bool]:
    """`cd target` の後の位置と、行き先が木の中なのに無いか。

    木の外（`..` で抜ける・絶対パス・見本）へ出たら位置は None で、外れとは数えない。
    木の中を指して無い場合も位置は None にするが、それ自体を外れとして返す。
    最初は両者を区別せず、無い場所への cd も「木の外」として黙って見るのをやめていた。
    そのためディレクトリごと消した手順（`cd app && python main.py` の app/ が無い）を
    通していた。

    `..` を付けずに隣のリポへ入る手順もある（親ディレクトリで打つ前提の README）。
    木に無く、隣には在る名前は、木の外へ出たものとして扱う（beside）。
    """
    if cwd is None or PLACEHOLDER.search(target) or target.startswith("/"):
        return None, False
    nxt = _norm(cwd, target)
    if nxt == ".":
        return "", False
    if nxt.startswith(".."):
        return None, False
    if present(nxt):
        return nxt, False
    return None, not (cwd == "" and beside(nxt))


def command_refs(lines: list[str], base: str, present: Present,
                 beside: Present = lambda _: False) -> list[str]:
    """シェルの手順が呼ぶファイルのうち、木に無いもの。beside は木の隣に在るかの述語。"""
    out: list[str] = []
    cwd: str | None = base
    current = -1
    cloned = False
    for i, ln, lang, block in _blocks(lines):
        if lang is None or lang not in SHELL_LANGS:
            continue
        if block != current:
            cwd, current, cloned = base, block, False   # ブロックごとに出発点へ戻る
        body = re.sub(r"^\s*[$%>]\s+", "", ln)
        if body.lstrip().startswith("#"):
            continue
        cloned = cloned or bool(CLONE.search(body))
        for target in CD.findall(body):
            if cloned and "/" not in target.rstrip("/") and not PLACEHOLDER.search(target):
                cwd, cloned = "", False           # 実測: clone 先の名前は手元の名前と違った
                continue
            cwd, missing = _enter(cwd, target, present, beside)
            if missing:
                out.append(f"{i}: 手順が入る `{target}` が木に無い")
        if cwd is None:
            continue                              # 木の外で打つ手順は、この木の主張ではない
        for tok in INTERP.findall(body) + EXEC.findall(body):
            if PLACEHOLDER.search(tok):
                continue
            path = _norm(cwd, tok)
            if not path.startswith("..") and not present(path):
                out.append(f"{i}: 手順が呼ぶ `{tok}` が木に無い")
    return out


def erased_refs(lines: list[str], base: str, present: Present,
                erased: set[str]) -> list[str]:
    """本文で名指しした道のうち、履歴では在ったのに今は無いもの。"""
    out: list[str] = []
    erased_dirs = {d for p in erased for d in _parents(p)}
    for i, ln, lang, _ in _blocks(lines):
        if lang is not None:
            continue
        for tok in INLINE.findall(ln):
            bare = tok[2:] if tok.startswith("./") else tok
            bare = bare.rstrip("/")
            if PLACEHOLDER.search(tok) or not PATHLIKE.match(tok) or "/" not in bare:
                continue
            for path in dict.fromkeys((_norm(base, bare), os.path.normpath(bare))):
                if path.startswith("..") or present(path):
                    break
                if path in erased or path in erased_dirs:
                    out.append(f"{i}: `{tok}` は履歴で消えている（今の木に無い）")
                    break
    return out


def _parents(path: str) -> list[str]:
    parts = path.split("/")[:-1]
    return ["/".join(parts[:n]) for n in range(1, len(parts) + 1)]
