#!/usr/bin/env python3
r"""判定基準を読む口が、python の版によって別の答えを出さないかを検査する。

work-os は tomllib（3.11+）が無い環境のために自前の TOML パーサを持っている。
2026-09-02 まで、それが読めなかった値を raw 文字列として黙って返していた。結果:

  * `severity = { internal = "warn", public = "block" }` が文字列になる
  * 正規表現の中の `\[` を配列の継続と数え、後続行を食う

release_lanes.toml では 9 レーン中 5 レーン（portability / provenance / history /
usability / agent_ready）が丸ごと消え、それでもゲートは「OK 第2段 public」と
答えていた。落ちるべきものを、検査が消えたことに気づかないまま通していた。
唯一の自動観測である hooks/fleet_watch.sh は /usr/bin/python3（3.9）を直に呼ぶので、
壊れている側だけが自動で回っていた。

ここでは2つを見る。

  1. 同値 — 実データで tomllib と同じ dict を返すこと。
           構文を列挙しない。列挙すると、次に判定基準へ書かれた未対応構文が
           黙って素通りして、この検査が捕まえたい形そのものになる。
  2. 厳格 — 表せない形に出会ったら例外にすること。推測して返さないこと。

  python3 tests/test_toml_fallback.py"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "engine"))
from workos import TomlSubsetError, _mini_toml, registry_root  # noqa: E402

failed: list[str] = []


def check(desc: str, ok: bool) -> None:
    if not ok:
        failed.append(desc)
    print(f"  {'OK ' if ok else 'NG '} {desc}")


def corpus() -> list[Path]:
    """判定基準と宣言の実物。ここもファイル名では列挙しない。"""
    seen: dict[Path, None] = {}
    for base in (ROOT, Path(registry_root())):
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*.toml")):
            if ".git/" in str(p):
                continue
            seen.setdefault(p.resolve(), None)
    return list(seen)


def main() -> int:
    try:
        import tomllib
    except ModuleNotFoundError:
        print("tomllib が無い python では突き合わせる相手がいない。skip")
        return 0

    files = corpus()
    print(f"同値 — 実データ {len(files)} ファイルで tomllib と突き合わせる")
    check("突き合わせる実データが在る", len(files) >= 5)
    for p in files:
        text = p.read_text(encoding="utf-8")
        try:
            real = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            continue                      # tomllib が読めないものは対象外
        rel = p.name if p.is_relative_to(ROOT) is False else str(p.relative_to(ROOT))
        try:
            mini = _mini_toml(text)
        except TomlSubsetError as exc:
            check(f"{rel} を読める（{exc}）", False)
            continue
        check(f"{rel} が tomllib と同値", mini == real)

    print("\n厳格 — 表せない形は、別の意味にせず落ちる")
    for desc, text in (
        ("ドット付きキー", "a.b = 1\n"),
        ("日付", "d = 2026-09-02\n"),
        ("裸のトークン", "a = nope\n"),
        ("閉じていない配列", "a = [1, 2\n"),
        ("閉じていないインラインテーブル", "a = { b = 1\n"),
        ("k = v でない行", "just a sentence\n"),
        ("未対応のエスケープ", 'a = """x\\qy"""\n'),
    ):
        try:
            got = _mini_toml(text)
            check(f"{desc} で落ちる（{got!r} を返した）", False)
        except TomlSubsetError:
            check(f"{desc} で落ちる", True)
        except Exception as exc:                              # noqa: BLE001
            check(f"{desc} で TomlSubsetError になる（{type(exc).__name__}）", False)

    print("\n厳格 — 読めるものまで落としていないこと（誤爆）")
    for desc, text, want in (
        ("インラインテーブル", 'a = { b = "x", c = 1 }\n', {"a": {"b": "x", "c": 1}}),
        ("文字列の中の角括弧", "a = ['x\\[y]']\n", {"a": ["x\\[y]"]}),
        ("複数行の配列", "a = [\n 1, # c\n 2,\n]\n", {"a": [1, 2]}),
        ("小数", "a = 0.3\n", {"a": 0.3}),
    ):
        try:
            check(f"{desc} を読める", _mini_toml(text) == want)
        except TomlSubsetError as exc:
            check(f"{desc} を読める（{exc}）", False)

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
