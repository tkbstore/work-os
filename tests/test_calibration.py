#!/usr/bin/env python3
"""校正が「証拠として成立しているか」を検査する。

閾値の根拠は、一度書けば終わりではない。チェックの名前を変えれば結果は宙に浮くし、
群の宣言が空でも数字は出る。証拠のほうが黙って腐るのを止めるのが目的。

ネットワークには出ない（外に依存するテストは不安定になる）。検査するのは宣言の健全性、
既に書き出された結果との対応、手元に作ったリポを相手にしたコミットの固定。

  python3 tests/test_calibration.py
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


def make_upstream(base: Path) -> tuple[str, str]:
    """2コミットを持つ上流を作る。GitHub と同じく、到達できる SHA の取得を許す。"""
    up = base / "up" / "repo"
    up.mkdir(parents=True)

    def git(*args: str) -> str:
        return subprocess.run(["git", "-C", str(up), *args], check=True, capture_output=True,
                              text=True, timeout=30).stdout.strip()
    git("init", "-q")
    git("config", "uploadpack.allowReachableSHA1InWant", "true")
    shas = []
    for n in ("1", "2"):
        (up / "README.md").write_text(n, encoding="utf-8")
        git("add", "README.md")
        # 利用者の core.hooksPath（コミットメッセージの検査など）を持ち込まない
        git("-c", "user.name=t", "-c", "user.email=t@example.com",
            "-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", n)
        shas.append(git("rev-parse", "HEAD"))
    return shas[0], shas[1]


def check_pinning() -> None:
    """測ったコミットが記録され、固定したコミットを測れること。

    日付しか残していなかった頃は、キャッシュの古い木を測っても、測り直しても、
    どの版を測ったかが分からなかった。前回との差分は版が分からなければ取れない。
    """
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        old, new = make_upstream(base)
        calibrate.REMOTE = f"file://{base}/up/{{slug}}"
        a = base / "cache" / "a"
        check("固定しなければ先頭を取る",
              calibrate.clone("repo", a, 100) == "" and calibrate.head_sha(a) == new)
        b = base / "cache" / "b"
        check("固定したコミットを取る",
              calibrate.clone("repo", b, 100, old) == "" and calibrate.head_sha(b) == old)
        # キャッシュが別の版を指していても、固定した版に合わせ直す
        check("キャッシュの版を固定した版に合わせる",
              calibrate.clone("repo", a, 100, old) == "" and calibrate.head_sha(a) == old)
        check("取れないコミットは理由付きで失敗する",
              calibrate.clone("repo", base / "cache" / "c", 100, "0" * 40) != "")
        res = base / "r.json"
        res.write_text(json.dumps({"commits": {"o/r": old}}), encoding="utf-8")
        check("結果の commits を固定に使える", calibrate.load_pins(res) == {"o/r": old})


def main() -> int:
    print("コミットの固定")
    check_pinning()

    print("\nコーパスの宣言")
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
