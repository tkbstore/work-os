#!/usr/bin/env python3
"""校正が「証拠として成立しているか」を検査する。

閾値の根拠は、一度書けば終わりではない。チェックの名前を変えれば結果は宙に浮くし、
群の宣言が空でも数字は出る。証拠のほうが黙って腐るのを止めるのが目的。

ここで clone はしない（ネットワークに依存するテストは CI で不安定になる）。
検査するのは宣言の健全性と、既に書き出された結果との対応だけ。

  python3 tests/test_calibration.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _registry import require  # noqa: E402

require("release_lanes.toml")
sys.path.insert(0, str(ROOT / "engine"))

import calibrate  # noqa: E402
import release_gate  # noqa: E402

failed: list[str] = []


def check(desc: str, ok: bool, detail: str = "") -> None:
    if not ok:
        failed.append(desc + (f" — {detail}" if detail else ""))
    print(f"  {'OK ' if ok else 'NG '} {desc}{('  ' + detail) if detail and not ok else ''}")


def lane_check_ids() -> set[str]:
    """今あるチェックの id と、引退したと宣言された id。

    引退を含めるのは、過去の結果が参照している id が「まだ在る」ことを
    意味しないため。ここで見たいのは「宙に浮いた参照が無いか」であって、
    「今も評価されているか」ではない。黙って消えた id と、消したと宣言した
    id を同じに扱うと、宣言する意味が無くなる。
    """
    cfg = release_gate.load_lanes()
    out = set(cfg.get("retired", {}))
    for lane, lcfg in cfg.get("lanes", {}).items():
        for chk in lcfg.get("checks", []):
            out.add(f"{lane}.{chk.get('id', '')}")
    return out


def main() -> int:
    print("コーパスの宣言")
    corpus = calibrate.load_corpus()
    problems = calibrate.lint_corpus(corpus)
    check("宣言に矛盾が無い", not problems, detail="; ".join(problems))

    groups = corpus.get("groups", {})
    check("対照できる群が揃っている",
          {"adopted", "not_adopted", "agent_native"} <= set(groups),
          detail=f"あるのは {sorted(groups)}")
    for name, g in groups.items():
        if g.get("canary"):
            # カナリアは通過率の比較に使わないので件数を問わない。
            # ただし空なら「落としてはいけないもの」が1つも無いことになる。
            check(f"{name}: カナリアが1本以上ある", bool(g.get("slugs")))
        else:
            # 1本しか無い群から出た差は、そのリポの個性であって傾向ではない
            check(f"{name}: 3本以上ある", len(g.get("slugs", [])) >= 3,
                  detail=f"{len(g.get('slugs', []))} 本")
        check(f"{name}: shape が宣言されている", bool(str(g.get("shape", "")).strip()))

    print("\n宣言の不正を捕まえられるか")
    broken = {"groups": {"x": {"rule": "", "slugs": [], "shape": "tool"}},
              "separation": {}}
    check("rule が空なら止める",
          any("rule" in p for p in calibrate.lint_corpus(broken)))
    check("min_gap が無ければ止める",
          any("min_gap" in p for p in calibrate.lint_corpus(broken)))
    dup = {"groups": {"a": {"rule": "r", "slugs": ["o/r"], "shape": "tool"},
                      "b": {"rule": "r", "slugs": ["o/r"], "shape": "tool"}},
           "separation": {"min_gap": 0.4}}
    check("同じリポが2つの群に居たら止める",
          any("両方に居る" in p for p in calibrate.lint_corpus(dup)))

    print("\n通過率の数え方")
    # skip を分母に入れると、測れなかったものが「落ちた」ことになる
    check("skip は分母に入れない", calibrate.rate(["pass", "skip", "fail"]) == (0.5, 1, 2))
    check("全部 skip なら測れないと言う", calibrate.rate(["skip", "skip"]) == (None, 0, 0))
    check("空でも落ちない", calibrate.rate([]) == (None, 0, 0))

    print("\n書き出された結果")
    results = sorted((ROOT / "calibration" / "results").glob("*.json"))
    check("結果が1つ以上ある", bool(results))
    known = lane_check_ids()
    for path in results:
        data = json.loads(path.read_text(encoding="utf-8"))
        for key in ("members", "excluded", "per_repo",
                    "adopted_vs_not_adopted", "agent_native_vs_not_adopted"):
            check(f"{path.name}: {key} がある", key in data)
        # チェックの名前を変えたら、過去の結果は宙に浮く。気づけるようにする。
        used = {r["check"] for r in data.get("adopted_vs_not_adopted", [])}
        used |= {r["check"] for r in data.get("agent_native_vs_not_adopted", [])}
        stale = sorted(used - known)
        check(f"{path.name}: 今のレーンに無いチェックを参照していない",
              not stale, detail=str(stale))
        # 何を測らなかったかは結果の一部。除外の理由が空なら、黙って削ったのと同じ。
        check(f"{path.name}: 除外に理由がある",
              all(e.get("why") for e in data.get("excluded", [])))
        measured = {s for slugs in data.get("members", {}).values() for s in slugs}
        check(f"{path.name}: 群に居るリポの観測が全部ある",
              measured <= set(data.get("per_repo", {})))

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
