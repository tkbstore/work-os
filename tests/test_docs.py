#!/usr/bin/env python3
"""ドキュメントが指しているものが実在するかを検査する。

README と CONTRIBUTING は「これを打て」「ここを読め」と書いている。書いた時点では
正しくても、ファイルを動かした瞬間に嘘になる。嘘になったことは、外から来た人が
詰まるまで誰も気づかない。落ちるのではなく黙って通る種類の壊れ方なので固定する。

ここでコマンドを *実行* してはいけない。CONTRIBUTING は release_gate を案内して
おり、release_gate は宣言されたテスト（= run_all.py = このファイル）を走らせる。
実行すると無限に再帰する。実在するかどうかだけを見る。

  python3 tests/test_docs.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ("README.md", "CONTRIBUTING.md", "CONSTITUTION.md", "docs/ADOPTION.md")

# GitHub の issue form が受け付ける型。これ以外を書くとテンプレートは無視され、
# 素の issue 入力欄に落ちる。壊れたことが画面に出ない。
FORM_TYPES = {"markdown", "input", "textarea", "dropdown", "checkboxes"}

failed: list[str] = []


def check(desc: str, ok: bool, detail: str = "") -> None:
    if not ok:
        failed.append(desc + (f" — {detail}" if detail else ""))
    print(f"  {'OK ' if ok else 'NG '} {desc}{('  ' + detail) if detail and not ok else ''}")


def script_refs(text: str) -> set[str]:
    """`python3 <path>.py` の形で案内されているスクリプトを拾う。"""
    return set(re.findall(r"python3\s+([\w./_-]+\.py)", text))


def link_refs(text: str) -> set[str]:
    """markdown のリンク先のうち、リポジトリ内を指すものを拾う。"""
    out = set()
    for target in re.findall(r"\]\(([^)]+)\)", text):
        if target.startswith(("http://", "https://", "#", "mailto:")):
            continue
        out.add(target.split("#")[0])
    return {t for t in out if t}


def main() -> int:
    print("案内されているスクリプトが実在するか")
    for doc in DOCS:
        path = ROOT / doc
        if not path.exists():
            check(f"{doc} が存在する", False)
            continue
        text = path.read_text(encoding="utf-8")
        for ref in sorted(script_refs(text)):
            check(f"{doc} → {ref}", (ROOT / ref).exists())

    print("\nリンク先が実在するか")
    for doc in DOCS:
        path = ROOT / doc
        if not path.exists():
            continue
        base = path.parent
        for ref in sorted(link_refs(path.read_text(encoding="utf-8"))):
            check(f"{doc} → {ref}", (base / ref).exists())

    print("\nissue テンプレート")
    tmpl_dir = ROOT / ".github" / "ISSUE_TEMPLATE"
    templates = sorted(tmpl_dir.glob("*.yml")) if tmpl_dir.exists() else []
    check("テンプレートが1つ以上ある", bool(templates))
    for t in templates:
        lines = t.read_text(encoding="utf-8").splitlines()
        top = {ln.split(":", 1)[0] for ln in lines if ln and not ln[0].isspace()}
        # name / description / body は必須。欠けるとテンプレートは表示されない。
        check(f"{t.name}: name と description と body がある",
              {"name", "description", "body"} <= top,
              detail=f"あるのは {sorted(top)}")
        types = re.findall(r"^\s*-\s*type:\s*(\S+)\s*$", "\n".join(lines), re.M)
        check(f"{t.name}: 項目が1つ以上ある", bool(types))
        unknown = sorted(set(types) - FORM_TYPES)
        check(f"{t.name}: 未知の type が無い", not unknown, detail=str(unknown))
        # タブが混ざると YAML は読めなくなるが、見た目では分からない
        check(f"{t.name}: タブが混ざっていない",
              not any("\t" in ln for ln in lines))

    print()
    if failed:
        print(f"失敗 {len(failed)} 件")
        for f in failed:
            print(f"  - {f}")
        return 1
    print("全ケース通過")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
