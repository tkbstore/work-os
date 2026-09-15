#!/usr/bin/env python3
"""レーンの閾値が、本当に群を分けているかを測り直す。

release_gate は「公開に耐えるか」を判定する。その判定基準は誰かの好みではなく
実測に由来する、というのが release_lanes.toml の主張である。主張が正しいかは
同じ物差しを外のリポジトリに当てれば分かる。分けていないチェックが見つかったら、
それは閾値ではなく思い込みなので、warn に落とすか消す。

観測は release_gate.evaluate() をそのまま呼ぶ。ここで観測を書き直すと、
校正に使った物差しと本番の物差しが別物になり、校正の意味が消える。
（同じ理由で昇格の数え方も engine/promotion.py に1つだけ置いてある）

コマンドは実行しない。他所のリポジトリのコマンドを走らせることになるため、
command_exit_zero 系は必ず skip になる。ここで分かるのは静的な観測だけ。

  python3 engine/calibrate.py              観測して calibration/results/ に書く
  python3 engine/calibrate.py --dry-run    clone せず宣言だけ検査する
  python3 engine/calibrate.py --cache DIR  clone の置き場所（既定は一時領域）
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import release_gate  # noqa: E402
from workos import _load_toml  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CORPUS_FILE = ROOT / "calibration" / "corpus.toml"
RESULTS_DIR = ROOT / "calibration" / "results"
CLONE_TIMEOUT = 300
# 対照群を service とみなして観測し直したものの印。実在の slug と混ざらないように付ける。
AS_SERVICE = " (service とみなす)"
CONTRAST_SERVICE = "not_adopted_as_service"


# --------------------------------------------------------------------------- #
# 宣言の読み込みと検査
# --------------------------------------------------------------------------- #

def load_corpus() -> dict:
    if not CORPUS_FILE.is_file():
        sys.exit(f"校正コーパスが見つかりません: {CORPUS_FILE}")
    return _load_toml(CORPUS_FILE)


def lint_corpus(corpus: dict) -> list[str]:
    """宣言そのものの矛盾を返す。空なら健全。"""
    problems: list[str] = []
    groups = corpus.get("groups", {})
    if not groups:
        problems.append("groups が空")
    seen: dict[str, str] = {}
    for name, g in groups.items():
        if not g.get("rule", "").strip():
            problems.append(f"{name}: rule が無い（なぜこの母集団かを答えられない選択は証拠ではない）")
        if not g.get("slugs"):
            problems.append(f"{name}: slugs が空")
        for slug in g.get("slugs", []):
            if slug.count("/") != 1:
                problems.append(f"{name}: slug の形が違う: {slug}")
            if slug in seen:
                problems.append(f"{slug} が {seen[slug]} と {name} の両方に居る")
            seen[slug] = name
    if not corpus.get("separation", {}).get("min_gap"):
        problems.append("separation.min_gap が無い（分けている の定義が宣言されていない）")
    return problems


# --------------------------------------------------------------------------- #
# 取得
# --------------------------------------------------------------------------- #

def clone(slug: str, dest: Path, max_mb: int) -> str:
    """浅く clone する。成功なら空文字、失敗なら理由を返す。"""
    if (dest / ".git").exists():
        return ""
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(
            ["git", "clone", "--depth", "1", "--single-branch", "--quiet",
             f"https://github.com/{slug}.git", str(dest)],
            capture_output=True, text=True, timeout=CLONE_TIMEOUT)
    except subprocess.TimeoutExpired:
        return f"{CLONE_TIMEOUT}秒で終わらない"
    except OSError as exc:
        return f"git を起動できない: {exc}"
    if proc.returncode != 0:
        return (proc.stderr or "").strip().splitlines()[-1:] and \
               (proc.stderr or "").strip().splitlines()[-1] or "clone に失敗"
    size_mb = sum(f.stat().st_size for f in dest.rglob("*") if f.is_file()) // (1024 * 1024)
    if size_mb > max_mb:
        shutil.rmtree(dest, ignore_errors=True)
        return f"{size_mb}MB は上限 {max_mb}MB を超える"
    return ""


def has_manifest(root: Path, manifests: list[str]) -> bool:
    return any((root / m).exists() for m in manifests)


# --------------------------------------------------------------------------- #
# 観測
# --------------------------------------------------------------------------- #

def observe(root: Path, lanes_cfg: dict, shape: str) -> tuple[dict[str, str], bool | None]:
    """check_id → pass | fail | skip と、ゲート全体の判定。

    チェック単位の通過率だけ見ていると「ゲートとして使えるか」が見えない。
    1レーンでも欠けたら落ちる設計なので、個々の差が小さくても全体では落ちる。
    群ごとの公開可率を並べて初めて、物差しとして成立しているかが分かる。
    """
    result = release_gate.evaluate(root, lanes_cfg, execute=False,
                                   assume_public=True, assume_shape=shape)
    if result is None:
        return {}, None
    out: dict[str, str] = {}
    for lane in result.lanes:
        for f in lane.findings:
            out[f"{lane.name}.{f.check_id}"] = f.state
    return out, result.publishable


def rate(states: list[str]) -> tuple[float | None, int, int]:
    """skip を除いた通過率と、(通過数, 判定できた数)。全部 skip なら None。"""
    judged = [s for s in states if s in ("pass", "fail")]
    if not judged:
        return None, 0, 0
    passed = sum(1 for s in judged if s == "pass")
    return passed / len(judged), passed, len(judged)


# --------------------------------------------------------------------------- #
# 出力
# --------------------------------------------------------------------------- #

def compare(per_repo: dict[str, dict[str, str]], members: dict[str, list[str]],
            left: str, right: str, min_gap: float) -> list[dict]:
    """左の群と右の群で、各チェックの通過率を並べる。"""
    checks = sorted({c for slug in members.get(left, []) + members.get(right, [])
                     for c in per_repo.get(slug, {})})
    rows = []
    for check in checks:
        l_rate, l_pass, l_n = rate([per_repo[s][check] for s in members.get(left, [])
                                    if check in per_repo.get(s, {})])
        r_rate, r_pass, r_n = rate([per_repo[s][check] for s in members.get(right, [])
                                    if check in per_repo.get(s, {})])
        if l_rate is None or r_rate is None:
            verdict = "測れない"
            gap = None
        else:
            gap = round(l_rate - r_rate, 3)
            verdict = "分けている" if gap >= min_gap else "分けていない"
        rows.append({
            "check": check,
            "left": {"group": left, "pass": l_pass, "n": l_n,
                     "rate": None if l_rate is None else round(l_rate, 3)},
            "right": {"group": right, "pass": r_pass, "n": r_n,
                      "rate": None if r_rate is None else round(r_rate, 3)},
            "gap": gap,
            "verdict": verdict,
        })
    rows.sort(key=lambda r: (-1 if r["gap"] is None else -r["gap"]))
    return rows


def print_table(title: str, rows: list[dict]) -> None:
    print(f"\n{title}")
    print(f"  {'チェック':<38} {'左':>9} {'右':>9} {'差':>7}  判定")
    print("  " + "-" * 78)
    for r in rows:
        left = "—" if r["left"]["rate"] is None else f"{r['left']['pass']}/{r['left']['n']}"
        right = "—" if r["right"]["rate"] is None else f"{r['right']['pass']}/{r['right']['n']}"
        gap = "  —  " if r["gap"] is None else f"{r['gap']:+.2f}"
        print(f"  {r['check']:<38} {left:>9} {right:>9} {gap:>7}  {r['verdict']}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="レーンの閾値が群を分けているかを測る")
    ap.add_argument("--dry-run", action="store_true",
                    help="clone せず、宣言の矛盾だけを検査する")
    ap.add_argument("--cache", metavar="DIR", default="",
                    help="clone の置き場所。省略すると一時領域を使い、終了時に消す")
    ap.add_argument("--write", action="store_true",
                    help="calibration/results/ に結果を書く")
    args = ap.parse_args(argv)

    corpus = load_corpus()
    problems = lint_corpus(corpus)
    if problems:
        print("宣言に矛盾があります", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    print(f"宣言 OK — {len(corpus['groups'])} 群 / "
          f"{sum(len(g['slugs']) for g in corpus['groups'].values())} リポジトリ")
    if args.dry_run:
        return 0

    lanes_cfg = release_gate.load_lanes()
    filters = corpus.get("filters", {})
    manifests = list(filters.get("manifest_required", []))
    max_mb = int(filters.get("max_clone_mb", 400))

    tmp = None
    if args.cache:
        cache = Path(args.cache).expanduser().resolve()
    else:
        tmp = tempfile.TemporaryDirectory()
        cache = Path(tmp.name)

    per_repo: dict[str, dict[str, str]] = {}
    verdicts: dict[str, bool] = {}
    members: dict[str, list[str]] = {}
    excluded: list[dict] = []
    try:
        for gname, g in corpus["groups"].items():
            members[gname] = []
            shape = str(g.get("shape", ""))
            for slug in g["slugs"]:
                dest = cache / slug.replace("/", "__")
                print(f"  取得 {slug} ...", flush=True)
                err = clone(slug, dest, max_mb)
                if err:
                    excluded.append({"slug": slug, "group": gname, "why": err})
                    print(f"    除外: {err}")
                    continue
                if manifests and not g.get("skip_filters") \
                        and not has_manifest(dest, manifests):
                    why = "マニフェストが無い（動かすものではない）"
                    excluded.append({"slug": slug, "group": gname, "why": why})
                    print(f"    除外: {why}")
                    continue
                states, ok = observe(dest, lanes_cfg, shape)
                if not states:
                    excluded.append({"slug": slug, "group": gname, "why": "観測できなかった"})
                    print("    除外: 観測できなかった")
                    continue
                per_repo[slug] = states
                verdicts[slug] = bool(ok)
                members[gname].append(slug)
                if gname == "not_adopted":
                    # agent_ready は shape = "service" を宣言したリポにしか当たらない。
                    # 対照群を tool のまま観測すると、レーンごと走らず「測れない」に
                    # なってしまい、比較したいものが比較できない。同じ物差しを当てる
                    # ために、対照群だけ service とみなした2回目の観測を持つ。
                    # 元の観測は上書きしない（tool としての結果も残す）。
                    alias = slug + AS_SERVICE
                    per_repo[alias], _ = observe(dest, lanes_cfg, "service")
                    members.setdefault(CONTRAST_SERVICE, []).append(alias)
    finally:
        if tmp is not None:
            tmp.cleanup()

    sep = corpus.get("separation", {})
    min_gap = float(sep.get("min_gap", 0.4))
    agent_gap = float(sep.get("agent_lane_min_gap", min_gap))

    main_rows = compare(per_repo, members, "adopted", "not_adopted", min_gap)
    agent_rows = compare(per_repo, members, "agent_native", CONTRAST_SERVICE, agent_gap)

    print("\n" + "=" * 82)
    print("ゲート全体の通過率（1レーンでも欠けたら公開不可）")
    gate_rates: dict[str, dict] = {}
    for gname, slugs in members.items():
        if gname == CONTRAST_SERVICE:
            continue
        judged = [s_ for s_ in slugs if s_ in verdicts]
        passed = sum(1 for s_ in judged if verdicts[s_])
        gate_rates[gname] = {"pass": passed, "n": len(judged)}
        share = f"{passed / len(judged):.0%}" if judged else "—"
        print(f"  {gname:<16} {passed:>2}/{len(judged):<3} ({share})")
        for s_ in judged:
            if not verdicts[s_]:
                print(f"       不可  {s_}")
    if excluded:
        # 黙って削らない。何を測らなかったかは結果の一部である。
        print(f"\n除外 {len(excluded)} 本")
        for e in excluded:
            print(f"  - {e['slug']} ({e['group']}): {e['why']}")

    print_table("採られた vs 採られていない  （左=adopted / 右=not_adopted）", main_rows)
    print_table("エージェント対応 vs 採られていない  "
                "（左=agent_native / 右=not_adopted を service とみなしたもの）",
                agent_rows)

    land = [s_ for s_ in members.get("landmark", []) if s_ in verdicts]
    if land:
        fell = [s_ for s_ in land if not verdicts[s_]]
        print(f"\nカナリア: {len(land) - len(fell)}/{len(land)} 通過")
        for s_ in fell:
            bad = sorted(k for k, v in per_repo[s_].items() if v == "fail")
            print(f"  落ちた  {s_}")
            print(f"          {', '.join(bad)}")
        if fell:
            print("  ※ ここが落ちるなら、疑うのはリポジトリではなく検査のほう")

    weak = [("採用", r) for r in main_rows if r["verdict"] == "分けていない"]
    weak += [("エージェント", r) for r in agent_rows if r["verdict"] == "分けていない"]
    print(f"\n分けていないチェック: {len(weak)} 件")
    for where, r in weak:
        print(f"  - [{where}] {r['check']}（差 {r['gap']:+.2f}）")
    print("  ※ 差が小さいチェックを block のまま置くと、好みで人を落とすことになる")

    if args.write:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = str(corpus.get("corpus", {}).get("frozen_on", "unknown"))
        out = RESULTS_DIR / f"{stamp}.json"
        out.write_text(json.dumps({
            "frozen_on": stamp,
            "members": members,
            "excluded": excluded,
            "per_repo": per_repo,
            "adopted_vs_not_adopted": main_rows,
            "agent_native_vs_not_adopted": agent_rows,
            "verdicts": verdicts,
            "gate_rates": gate_rates,
            "min_gap": min_gap,
            "agent_lane_min_gap": agent_gap,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"\n書き出し: {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
