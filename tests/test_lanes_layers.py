#!/usr/bin/env python3
"""判定基準が2層で重なることを検査する。

骨格（観測の種類・段のラダー・既定の severity）は work-os が持ち、事実（固有名詞）と
組織の選択は registry が持つ。骨格まで registry に置いていたときは、公開されている
work-os はゲートの形すら持っておらず、install しても1つも観測が走らなかった。

ここで固定するのは3つ。
  1. 骨格だけで（registry が無くても）観測が走ること
  2. 上書きが **id で** 当たること。位置で当てると、骨格に1つ挿した日に全部ずれる
  3. 重ね方の規則が1通りであること（表は再帰・観測は id・それ以外の配列は置き換え）

  python3 tests/test_lanes_layers.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "engine"))
# E402 は「import が先頭に無い」の指摘。上で sys.path を挿してからでないと engine/ は
# 解決しない。抑制しているのは順序の指摘だけである。
from release_gate import _merge_cfg, _merge_checks  # noqa: E402

GATE = ROOT / "engine" / "release_gate.py"
SKELETON = ROOT / "config" / "release_lanes.toml"

failures: list[str] = []
checked = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global checked
    checked += 1
    if not ok:
        failures.append(f"{name}: {detail}" if detail else name)


# --------------------------------------------------------------------------- #
# 1. 重ね方の規則
# --------------------------------------------------------------------------- #
base = {"gate": {"stages": ["a", "b"], "default_intent": "internal"},
        "presets": {"fixtures": ["**/tests/*"]},
        "lanes": {"safety": {"title": "安全", "checks": [
            {"id": "one", "kind": "pattern_absent", "max": 0},
            {"id": "two", "kind": "file_present"},
        ]}}}

over = {"gate": {"default_intent": ""},
        "presets": {"fixtures": ["**/spec/*"]},
        "lanes": {"safety": {"checks": [
            {"id": "two", "auditable": True},
            {"id": "three", "kind": "file_present"},
        ]}}}
m = _merge_cfg(base, over)

check("表は再帰的に重なる", m["gate"]["stages"] == ["a", "b"], str(m["gate"]))
check("葉は上書きされる", m["gate"]["default_intent"] == "", str(m["gate"]))
check("観測でない配列は置き換え（継ぎ足しではない）",
      m["presets"]["fixtures"] == ["**/spec/*"], str(m["presets"]))
ids = [c["id"] for c in m["lanes"]["safety"]["checks"]]
check("骨格の観測が残る", ids[:2] == ["one", "two"], str(ids))
check("新しい id が足される", "three" in ids, str(ids))
check("骨格にしか無い欄は残る",
      m["lanes"]["safety"]["checks"][0].get("max") == 0)
check("同じ id は欄ごとに上書き",
      m["lanes"]["safety"]["checks"][1].get("auditable") is True
      and m["lanes"]["safety"]["checks"][1].get("kind") == "file_present")
check("レーンの表題は骨格のものが残る",
      m["lanes"]["safety"]["title"] == "安全")

# 位置ではなく id で当たること。骨格の先頭に1つ挿しても結果が変わらない
shifted = {"lanes": {"safety": {"checks": [
    {"id": "zero", "kind": "file_present"},
    {"id": "one", "kind": "pattern_absent", "max": 0},
    {"id": "two", "kind": "file_present"},
]}}}
m2 = _merge_cfg(_merge_cfg(base, shifted), over)
# next(...) で取り出すと、当て先がずれた実装では StopIteration で検査が死ぬ。
# 死ぬのは落ちることではあるが、どの検査が何を見て落ちたかが出力から消える。
by_id2 = {str(c.get("id")): c for c in m2["lanes"]["safety"]["checks"]}
check("挿した観測が消えない", "zero" in by_id2, str(sorted(by_id2)))
check("骨格に1つ挿しても上書きは同じ観測に当たる",
      by_id2.get("two", {}).get("auditable") is True, str(sorted(by_id2)))
check("挿した観測に上書きが移らない",
      "auditable" not in by_id2.get("zero", {}), str(by_id2.get("zero")))

# enabled = false で骨格の観測を落とせる
dropped = _merge_checks([{"id": "one"}, {"id": "two"}], [{"id": "one", "enabled": False}])
check("enabled = false で骨格の観測を落とせる",
      [c["id"] for c in dropped] == ["two"], str(dropped))
check("落とすのは名指しした観測だけ", len(dropped) == 1)


# --------------------------------------------------------------------------- #
# 2. 骨格だけで観測が走る（registry が無い配置）
# --------------------------------------------------------------------------- #
def run(repo: Path, registry: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(GATE), str(repo), *args],
                          capture_output=True, text=True, timeout=180,
                          env={**os.environ, "WORKOS_REGISTRY": str(registry)})


check("骨格が work-os に在る", SKELETON.is_file(), str(SKELETON))

with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    empty = tmp / "no-registry"
    empty.mkdir()
    p = run(ROOT, empty)
    out = p.stdout + p.stderr

    check("registry が無くても判定を出す", f"[{ROOT.name}]" in out, out[:300])
    check("判定基準が無いとは言わない", "判定基準が見つかりません" not in out, out[:300])
    # 形の観測（秘密のパターン）は骨格に在るので走る。通った観測は既定では印字され
    # ないので -v で見る。印字されないことを根拠にすると、この検査は常に真になる
    # （最初にそう書いた）。走ったことを出力の上で確かめる。
    pv = run(ROOT, empty, "-v")
    vout = pv.stdout + pv.stderr
    secret_lines = [ln.strip() for ln in vout.splitlines() if "no_secrets:" in ln]
    check("秘密の観測が走る（1行以上出る）", bool(secret_lines), vout[:300])
    check("秘密の観測が skip になっていない",
          bool(secret_lines) and all("パターンが宣言されていない" not in ln
                                     for ln in secret_lines), str(secret_lines))
    check("秘密の観測が実際にファイルを見ている",
          any("ファイルに該当なし" in ln or "件該当" in ln for ln in secret_lines),
          str(secret_lines))

    # 事実の観測は宣言が無いので skip。黙って pass にしない
    private_lines = [ln.strip() for ln in vout.splitlines() if "no_private_terms:" in ln]
    check("固有名詞の観測が出る", bool(private_lines), vout[:300])
    check("固有名詞の観測は skip になる",
          bool(private_lines) and all("パターンが宣言されていない" in ln
                                      for ln in private_lines), str(private_lines))


print(f"[test_lanes_layers] {checked} 件検査")
if failures:
    for f in failures:
        print(f"  NG {f}", file=sys.stderr)
    print(f"[test_lanes_layers] FAIL {len(failures)} / {checked}", file=sys.stderr)
    raise SystemExit(1)
print("[test_lanes_layers] PASS")
