#!/usr/bin/env python3
"""root_cause_guard が「症状だけを消す変更」を捕まえ、普通の仕事を邪魔しないこと。

このフックは全リポジトリに効く。誤爆が多ければ切られるだけなので、
**通すべきものを通すこと**のほうを重く検査する。

痕跡の見本（noqa 等）は実行時に組み立てる。本文に直接書くと、このリポジトリ自身の
検査がそれを痕跡として拾うため（test_release_gate.py と同じ作法）。

  python3 tests/test_root_cause_guard.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _registry import require  # noqa: E402

require("patch_smells.toml")
HOOK = ROOT / "hooks" / "root_cause_guard.py"

NOQA = "# " + "noqa"
TS_IGNORE = "@ts-" + "ignore"
SKIP = "@pytest." + "mark.skip"


def call(tool: str, tool_input: dict, mode: str = "block",
         registry: str | None = None) -> tuple[bool, str]:
    """(拒否されたか, 理由) を返す。registry を渡すと痕跡の宣言の在り処を差し替える。"""
    env = {**dict(__import__("os").environ), "WORKOS_ROOT_CAUSE": mode}
    if registry is not None:
        env["WORKOS_REGISTRY"] = registry
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"tool_name": tool, "tool_input": tool_input}),
        capture_output=True, text=True, timeout=60, env=env)
    out = (proc.stdout or "").strip()
    if not out:
        return False, (proc.stderr or "").strip()
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return False, out
    spec = data.get("hookSpecificOutput", {})
    return spec.get("permissionDecision") == "deny", spec.get("permissionDecisionReason", "")


def edit(old: str, new: str) -> dict:
    return {"file_path": "/nonexistent/x.py", "old_string": old, "new_string": new}


def main() -> int:
    failed: list[str] = []

    def check(desc: str, ok: bool) -> None:
        print(f"  {'OK ' if ok else 'NG '} {desc}")
        if not ok:
            failed.append(desc)

    print("\n抑制に理由が無ければ止める")
    denied, _ = call("Edit", edit("x = f()\n", f"x = f()  {NOQA}\n"))
    check("理由の無い抑制を止める", denied)
    denied, _ = call("Edit", edit("x = f()\n",
                                  f"x = f()  {NOQA}  # 生成コードなので行長は直せない\n"))
    check("同じ行に理由があれば通す", not denied)
    denied, _ = call("Edit", edit("x = f()\n",
                                  "# 外部 SDK の型定義が壊れているため\n"
                                  f"const y = z;  // {TS_IGNORE}\n"))
    check("直前の行に理由があれば通す", not denied)
    denied, _ = call("Edit", edit("def t():\n", f"{SKIP}\ndef t():\n"))
    check("理由の無いテスト停止を止める", denied)

    print("\n合格線は向きで見る")
    denied, _ = call("Edit", edit("timeout = 30\n", "timeout = 300\n"))
    check("緩む向きの閾値変更を止める", denied)
    denied, _ = call("Edit", edit("timeout = 300\n", "timeout = 30\n"))
    check("厳しくする変更は止めない", not denied)
    denied, _ = call("Edit", edit("min_coverage = 80\n", "min_coverage = 50\n"))
    check("下限を下げる変更を止める", denied)
    denied, _ = call("Edit", edit("", "timeout = 30\n"))
    check("新しく閾値を書くのは止めない", not denied)
    denied, _ = call("Edit", edit("timeout = 30\n",
                                  "# 実測で 90 秒かかるため、上限を実態に合わせる\n"
                                  "timeout = 300\n"))
    check("理由が書いてあれば閾値も通す", not denied)

    print("\n列挙に足す形")
    denied, _ = call("Edit", edit('exclude_globs = [\n    "*.py",\n]\n',
                                  'exclude_globs = [\n    "*.py",\n    "*.md",\n]\n'))
    check("既存の除外リストに項目だけ足すのを止める", denied)
    denied, _ = call("Edit", edit('exclude_globs = [\n    "*.py",\n]\n',
                                  '# .md は生成物で、原本は docs/src にある\n'
                                  'exclude_globs = [\n    "*.py",\n    "*.md",\n]\n'))
    check("理由があれば列挙の追加も通す", not denied)
    denied, _ = call("Edit", edit("", 'exclude_globs = ["*.py"]\n'))
    check("除外リストを新設するのは止めない", not denied)
    # 変数名の列挙で見ていた頃、globs / patterns / lanes は永久に見えなかった。
    # release_gate が拡張子の列挙で 7 割見逃したのと同じ形の誤りだったので、
    # 名前を知らなくても形で当たることを固定する。
    for key in ("globs", "patterns", "lanes", "sources", "targets"):
        denied, _ = call("Edit", edit(f'{key} = [\n    "a",\n]\n',
                                      f'{key} = [\n    "a",\n    "b",\n]\n'))
        check(f"{key} という名前でも列挙の追加を捕まえる", denied)

    # 形だけで見ると依存の追加もデータの追加も同じ形に見える。実測（ハンク5920件）で
    # 当たった9件のうち本物は3件で、残りは依存とプロンプト文だった。項目の形で絞る。
    for desc, before, after in [
        ("依存を足す", '[\n    "pytest>=8.0",\n]\n', '[\n    "pytest>=8.0",\n    "pytest-cov>=7.1.0",\n]\n'),
        ("データを足す", '[\n    "一つ目の文",\n]\n', '[\n    "一つ目の文",\n    "二つ目の文",\n]\n'),
    ]:
        denied, _ = call("Edit", edit(before, after))
        check(f"{desc}: 逃がすための列挙ではないので止めない", not denied)
    denied, _ = call("Edit", edit('exclude = [\n    "dist/**",\n]\n',
                                  'exclude = [\n    "dist/**",\n    ".next/**/*.ts",\n]\n'))
    check("逃がすための列挙は止める", denied)

    print("\n普通の仕事を邪魔しない")
    for desc, new in [
        ("関数を足す", "def add(a, b):\n    return a + b\n"),
        ("テストを足す", "def test_add():\n    assert add(1, 2) == 3\n"),
        ("例外を握らず投げ直す",
         "try:\n    run()\nexcept ValueError as exc:\n    raise SystemExit(exc)\n"),
        ("設定値を書く", 'name = "sample"\nport = 8080\n'),
        ("コメントを直す", "# 入口はここ。read_config が先に呼ばれる\n"),
    ]:
        denied, why = call("Edit", edit("", new))
        check(f"{desc}: 止めない", not denied)

    print("\n逃げ道と壊れ方")
    denied, _ = call("Edit", edit("x = f()\n", f"x = f()  {NOQA}\n"), mode="off")
    check("WORKOS_ROOT_CAUSE=off で無効になる", not denied)
    denied, why = call("Edit", edit("x = f()\n", f"x = f()  {NOQA}\n"), mode="warn")
    check("warn では止めずに stderr に出す", not denied and "work-os" in why)
    denied, _ = call("Bash", {"command": f"echo {NOQA}"})
    check("書き込み以外のツールは見ない", not denied)
    proc = subprocess.run([sys.executable, str(HOOK)], input="not json",
                          capture_output=True, text=True, timeout=30)
    check("壊れた入力でも exit 0（フックは落ちてはいけない）", proc.returncode == 0)

    # 宣言が読めないとき、照合対象がゼロになるので **すべてが黙って通る**。
    # 実測 2026-08-31: registry を別リポへ切り出した直後、WORKOS_REGISTRY を
    # 持たない環境で抑制の追加が警告も出さずに通った。同居していた頃はこの経路に
    # 入りようが無かったので、切り出しが作った穴である。止めはしないが黙らない。
    with tempfile.TemporaryDirectory() as td:
        missing = str(Path(td) / "no-registry")
        denied, why = call("Edit", edit("x = f()\n", f"x = f()  {NOQA}\n"),
                           registry=missing)
        check("宣言が読めないとき止めはしない", not denied)
        check("宣言が読めないとき黙って通さない（stderr に出す）",
              "検査していません" in why)

    print()
    if failed:
        print(f"失敗 {len(failed)} 件")
        for f in failed:
            print(f"  - {f}")
        return 1
    print(f"全ケース通過")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
