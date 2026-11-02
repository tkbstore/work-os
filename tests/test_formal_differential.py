#!/usr/bin/env python3
"""formal/ の仕様と engine/ の実装に同じ入力を通し、答えが揃うことを検査する。

formal/ の定理はモデルについての定理で、実装がモデルどおりに動くことは言わない。
対応を `#guard` の具体例だけで揃えていたので、モデルを直しても実装を直しても、
片方だけが動いたことに気づく手段が数件の例しか無かった。ここでは乱数で作った入力を
両方に流し、食い違った入力をそのまま出す。

突き合わせるもの:
  layer      Repo.layer_of                    ↔ layerOf
  ladder     RepoResult.reached / publishable ↔ reached / publishable
  rule       collect + satisfies_rule_of_three ↔ collect + satisfiesRuleOfThree
  gitignore  obs_file_present（per_workspace） ↔ gitignorePass

Lean（lake）が無い環境では比べられないので、通過ではなく exit 3（走らせられなかった）。

  python3 tests/test_formal_differential.py
"""

from __future__ import annotations

import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FORMAL = ROOT / "formal"
sys.path.insert(0, str(ROOT / "engine"))

# E402（import が先頭に無い）の抑制。engine/ は上の sys.path の後でしか解決しない
from promotion import collect, satisfies_rule_of_three  # noqa: E402
# E402 の抑制。理由は上と同じで、engine/ は sys.path の後でしか解決しない
from release_gate import (Finding, LaneResult, RepoResult, StageResult,  # noqa: E402
                          obs_file_present)
# E402 の抑制。理由は上と同じで、engine/ は sys.path の後でしか解決しない
from workos import Repo  # noqa: E402

SKIPPED = 3
SEED = 20261030
CASES = 400
LAYERS = ("kernel", "golden_path", "config", "extension")
# 長さの比較が文字数で揃うかを見るため、多バイトの区間も混ぜる。
SEGMENTS = ("a", "b", "ab", "a.b", "日")
MANIFEST = "pyproject.toml"


def lake() -> str | None:
    found = shutil.which("lake")
    if found:
        return found
    elan = Path.home() / ".elan" / "bin" / "lake"
    return str(elan) if elan.exists() else None


def path_of(rng: random.Random, lo: int, hi: int) -> str:
    return "/".join(rng.choice(SEGMENTS) for _ in range(rng.randint(lo, hi)))


# --------------------------------------------------------------------------- #
# 入力の生成
# --------------------------------------------------------------------------- #

def gen_layer(rng: random.Random) -> dict:
    layers = {l: [path_of(rng, 1, 3) + rng.choice(("", "/"))
                  for _ in range(rng.randint(0, 3))] for l in LAYERS}
    rel = path_of(rng, 1, 4)
    return {"layers": layers, "rel": rel}


def gen_ladder(rng: random.Random) -> list:
    def finding() -> dict:
        return {"state": rng.choice(("pass", "fail", "skip")),
                "severity": rng.choice(("block", "warn")),
                "pending": rng.random() < 0.2}
    return [[[finding() for _ in range(rng.randint(0, 3))]
             for _ in range(rng.randint(0, 3))] for _ in range(rng.randint(0, 4))]


def gen_rule(rng: random.Random) -> dict:
    pool = ("a", "b", "c", "d", "*")
    return {"used_by": [rng.choice(pool) for _ in range(rng.randint(0, 5))],
            "declaring": [rng.choice(pool[:4]) for _ in range(rng.randint(0, 3))],
            "note": rng.choice(("", "", "例外"))}


def gen_gitignore(rng: random.Random) -> dict:
    dirs = sorted({path_of(rng, 1, 3) for _ in range(rng.randint(1, 5))})
    wss = [d for d in dirs if rng.random() < 0.6]
    if rng.random() < 0.1:
        wss.append("")
    has = [d for d in dirs if rng.random() < 0.5]
    return {"root_has": rng.random() < 0.15, "has": has, "wss": wss}


# --------------------------------------------------------------------------- #
# Lean に渡す形（仕様の前提に合わせる）
# --------------------------------------------------------------------------- #

def split_dir(d: str) -> list[str]:
    return d.split("/") if d else []


def to_lean(case: dict) -> dict:
    return {
        # 仕様の宣言は「末尾の / を落としたもの」を前提にする（Layers.lean の matchesB）。
        # 並びは実装が走査する順（層の順、層の中は宣言順）。
        "layer": [{"decls": [[p.rstrip("/"), l] for l in LAYERS for p in c["layers"][l]],
                   "rel": c["rel"]} for c in case["layer"]],
        "ladder": case["ladder"],
        "rule": case["rule"],
        "gitignore": [{"root_has": c["root_has"], "has": [split_dir(d) for d in c["has"]],
                       "wss": [split_dir(d) for d in c["wss"]]} for c in case["gitignore"]],
    }


# --------------------------------------------------------------------------- #
# Python の実装に通す
# --------------------------------------------------------------------------- #

def py_layer(c: dict) -> str:
    return Repo(root=Path("."), name="x", layers=c["layers"]).layer_of(c["rel"])


def py_ladder(stages: list) -> dict:
    repo = RepoResult(name="x", root=Path("."), intent="public", enforcement="block", stages=[
        StageResult(name=f"s{i}", title="", question="", lanes=[
            LaneResult(name="l", title="", question="", findings=[
                Finding(check_id="c", kind="k", severity=f["severity"], state=f["state"],
                        why="", pending=f["pending"]) for f in lane])
            for lane in st])
        for i, st in enumerate(stages)])
    top = repo.reached
    return {"reached": int(top[1:]) + 1 if top else 0, "publishable": repo.publishable}


def py_rule(c: dict) -> dict:
    ev = collect(c["used_by"], c["declaring"], c["note"])
    return {"universal": ev.universal, "count": ev.count,
            "satisfies": satisfies_rule_of_three(ev)[0]}


def py_gitignore(c: dict, base: Path) -> bool:
    root = Path(tempfile.mkdtemp(dir=base))
    files = [f"{ws}/{MANIFEST}" if ws else MANIFEST for ws in c["wss"]]
    files += [f"{d}/.gitignore" for d in c["has"]]
    if c["root_has"]:
        files.append(".gitignore")
    for rel in files:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).touch()
    ctx = {"files": [(rel, root / rel) for rel in files],
           "lanes_cfg": {"workspaces": {"manifests": [MANIFEST]}, "presets": {}}}
    state, _, _ = obs_file_present(root, {"any_of": [".gitignore"], "per_workspace": True}, ctx)
    return state == "pass"


# --------------------------------------------------------------------------- #

def run_lean(lake_bin: str, payload: dict, work: Path) -> dict:
    src = work / "input.json"
    src.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    env = {**os.environ, "PATH": f"{Path(lake_bin).parent}:{os.environ.get('PATH', '')}"}
    proc = subprocess.run([lake_bin, "exe", "differential", str(src)], cwd=FORMAL,
                          capture_output=True, text=True, timeout=600, env=env)
    if proc.returncode != 0:
        raise RuntimeError(f"lake exe differential が失敗した:\n{proc.stderr or proc.stdout}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def compare(case: dict, py: dict, lean: dict) -> int:
    failed = 0
    for key in ("layer", "ladder", "rule", "gitignore"):
        diffs = [(i, a, b) for i, (a, b) in enumerate(zip(py[key], lean[key])) if a != b]
        if len(py[key]) != len(lean[key]):
            diffs.append((-1, len(py[key]), len(lean[key])))
        print(f"{'OK' if not diffs else 'NG'}  {key}: {len(py[key])} 件中 {len(diffs)} 件が食い違い")
        for i, a, b in diffs[:5]:
            shown = case[key][i] if i >= 0 else "件数"
            print(f"      入力 {json.dumps(shown, ensure_ascii=False)}\n"
                  f"      Python {a} / Lean {b}")
        failed += bool(diffs)
    return failed


def main() -> int:
    lake_bin = lake()
    if lake_bin is None:
        print("SKIP: lake が無いので仕様と実装を比べられない（formal/README.md を参照）")
        return SKIPPED
    rng = random.Random(SEED)
    case = {"layer": [gen_layer(rng) for _ in range(CASES)],
            "ladder": [gen_ladder(rng) for _ in range(CASES)],
            "rule": [gen_rule(rng) for _ in range(CASES)],
            "gitignore": [gen_gitignore(rng) for _ in range(CASES)]}

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        try:
            lean = run_lean(lake_bin, to_lean(case), work)
        except RuntimeError as exc:
            # 仕様がビルドできないのは比べられないのではなく、仕様が壊れている。
            print(f"NG  {exc}")
            return 1
        py = {"layer": [py_layer(c) for c in case["layer"]],
              "ladder": [py_ladder(c) for c in case["ladder"]],
              "rule": [py_rule(c) for c in case["rule"]],
              "gitignore": [py_gitignore(c, work) for c in case["gitignore"]]}
    return 1 if compare(case, py, lean) else 0


if __name__ == "__main__":
    sys.exit(main())
