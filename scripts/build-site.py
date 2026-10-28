#!/usr/bin/env python3
"""公開サイト（site/index.html）を、判定基準と校正の結果から生成する。

サイトに手で数字や検査の一覧を書くと、ゲートを直すたびに食い違う。
calibration/README.md と results/ が一度食い違った（2026-09-16）のと同じ形になるので、
数字と一覧は必ずここで機械から引く。手で書くのは説明文だけ。

判定基準は **骨格だけ** から読む。組織の registry を重ねると、組織固有の宣言が
公開サイトに載る。WORKOS_REGISTRY を空のディレクトリに向けて --checks を呼ぶ。

    python3 scripts/build-site.py            site/index.html を書く
    python3 scripts/build-site.py --check    書かずに、今の site/ と食い違うかだけ見る（exit 1）
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "site" / "index.html"
RESULTS = ROOT / "calibration" / "results"
FORMAL = ROOT / "formal" / "WorkosSpec"
REPO_URL = "https://github.com/tkbstore/work-os"
INSTALL = ("curl -fsSL https://raw.githubusercontent.com/tkbstore/work-os/main/install.sh | sh\n"
           "workos-gate <path-to-a-repo>\n"
           "workos-gate --checks")

GROUP_EN = {
    "adopted": "Widely adopted Python tools",
    "not_adopted": "Rarely adopted tools (organisation-owned)",
    "agent_native": "Agent-friendly CLIs",
    "landmark": "Canaries (must not fail)",
    "formal": "Published Lean / formal-proof repositories",
}


def esc(s: object) -> str:
    return html.escape(str(s), quote=True)


def load_checks() -> dict:
    """骨格だけの判定基準。registry を空にして呼ぶ。"""
    with tempfile.TemporaryDirectory() as empty:
        env = {**os.environ, "WORKOS_REGISTRY": empty}
        proc = subprocess.run(
            [sys.executable, str(ROOT / "engine" / "release_gate.py"), "--checks", "--json"],
            capture_output=True, text=True, env=env, timeout=60)
    if proc.returncode != 0:
        sys.exit(f"--checks が失敗しました: {proc.stderr.strip()}")
    data = json.loads(proc.stdout)
    sources = {layer["layer"] for layer in data.get("layers", []) if layer.get("present")}
    if sources - {"骨格"}:
        sys.exit(f"骨格以外の層が混ざっています: {sorted(sources)}。公開サイトには載せられません")
    return data


def latest_results() -> dict:
    files = sorted(RESULTS.glob("*.json"))
    if not files:
        sys.exit(f"校正の結果がありません: {RESULTS}")
    return json.loads(files[-1].read_text(encoding="utf-8"))


def theorems() -> list[tuple[str, str]]:
    """formal/ の定理名を、ファイルごとに拾う。手で写すと増減に追随しない。"""
    out = []
    for f in sorted(FORMAL.glob("*.lean")):
        for name in re.findall(r"(?m)^theorem\s+(\S+)", f.read_text(encoding="utf-8")):
            out.append((f.stem, name))
    return out


def checks_rows(data: dict) -> str:
    rows = []
    for stage in data.get("stages", []):
        for lane in stage.get("lanes", []):
            for c in lane.get("checks", []):
                sev = c.get("severity", "")
                rows.append(
                    f"<tr><td>{esc(stage['name'])}</td><td>{esc(lane['name'])}</td>"
                    f"<td><code>{esc(c['id'])}</code></td>"
                    f"<td><span class='sev sev-{esc(sev)}'>{esc(sev)}</span></td>"
                    f"<td lang='ja'>{esc(c.get('why', ''))}</td></tr>")
    return "\n".join(rows)


def calibration_rows(res: dict) -> str:
    rows = []
    for group, r in res.get("gate_rates", {}).items():
        n = r.get("n", 0)
        first = r.get("internal")
        first_cell = f"{first}/{n}" if first is not None else "—"
        rows.append(f"<tr><td>{esc(GROUP_EN.get(group, group))}</td><td>{n}</td>"
                    f"<td>{esc(first_cell)}</td><td>{r.get('pass', 0)}/{n}</td></tr>")
    return "\n".join(rows)


def formal_rows() -> str:
    return "\n".join(f"<li><code>{esc(mod)}.{esc(name)}</code></li>" for mod, name in theorems())


def stage1_stops(res: dict) -> str:
    """第1段で止めたリポ。公開済みのものを止めたなら、偽陽性の候補として名前を出す。"""
    reached = res.get("reached", {})
    stops = [s for s, got in reached.items() if not got]
    if not stops:
        return "<p>No published repository in the corpus was stopped at stage 1.</p>"
    items = "".join(f"<li><code>{esc(s)}</code></li>" for s in sorted(stops))
    return (f"<p>{len(stops)} published repositories were stopped at stage 1. "
            "Each is a false-positive candidate and is examined by hand in "
            f"<a href='{REPO_URL}/blob/main/calibration/README.md'>calibration/README.md</a>.</p>"
            f"<ul class='cols'>{items}</ul>")


def render(data: dict, res: dict) -> str:
    template = (ROOT / "site" / "template.html").read_text(encoding="utf-8")
    total = sum(r.get("n", 0) for r in res.get("gate_rates", {}).values())
    values = {
        "INSTALL": esc(INSTALL),
        "REPO_URL": REPO_URL,
        "FROZEN_ON": esc(res.get("frozen_on", "")),
        "CORPUS_N": str(total),
        "EXCLUDED_N": str(len(res.get("excluded", []))),
        "CHECK_ROWS": checks_rows(data),
        "CALIBRATION_ROWS": calibration_rows(res),
        "STAGE1_STOPS": stage1_stops(res),
        "THEOREMS": formal_rows(),
        "THEOREM_N": str(len(theorems())),
    }
    for key, val in values.items():
        template = template.replace("{{" + key + "}}", val)
    left = re.findall(r"\{\{[A-Z_0-9]+\}\}", template)
    if left:
        sys.exit(f"埋まっていない差し込み口があります: {sorted(set(left))}")
    return template


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="site/index.html を生成する")
    ap.add_argument("--check", action="store_true", help="書かずに食い違いだけを見る")
    args = ap.parse_args(argv)
    page = render(load_checks(), latest_results())
    if args.check:
        current = OUT.read_text(encoding="utf-8") if OUT.is_file() else ""
        if current != page:
            print("site/index.html が判定基準・校正の結果と食い違っています。"
                  "python3 scripts/build-site.py で作り直してください", file=sys.stderr)
            return 1
        print("site/index.html は最新です")
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(page, encoding="utf-8")
    print(f"書き出し: {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
