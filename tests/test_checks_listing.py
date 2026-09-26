#!/usr/bin/env python3
r"""ゲートが「何を見ているのか」を、走らせずに答えられるかを検査する。

`--checks` が無かったとき、判定基準を読む方法は2つしか無かった。
config/release_lanes.toml を700行読むか、ゲートを走らせて **落ちた観測だけ** を
見るか。後者は通った観測を出さないので、見えている観測はいつも全体の一部で、
読む側は残りが在ることに気づけない。

ここで固定するのは4つ。

1. **骨格だけ（registry が無い環境）でも全部出る。** 出どころの札は「骨格」になる。
2. **registry が触った欄を名指しする。** 「registry が触った」だけでは、触った先が
   severity（consequence が変わる）なのか why（文言）なのかが読めない。
   欄を足したのと値を変えたのも別に出す。
3. **走らない宣言を、走る宣言と同じ表に出す。** 段が参照するレーンの宣言が無い・
   engine が知らない kind・どの段にも配られていないレーン・enabled = false で
   落とした観測。どれも「落ちる」のではなく「通ってしまう」側の壊れ方である。
   穴が無いときは「なし」と言う（沈黙を通過の代わりにしない）。
4. **一覧と、実際に走る観測がずれない。** 一覧を別経路で組み立てると、ずれても
   両方それらしく出る。実際の判定に現れた観測は、必ず一覧に在ること。

欠陥は registry 層（上書き）から注入する。骨格を書き換えずに、穴の在る判定基準を
作れるので、検査が本物の骨格を相手にしたまま「穴を見つけられるか」を確かめられる。

  python3 tests/test_checks_listing.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GATE = ROOT / "engine" / "release_gate.py"

failed: list[str] = []


def check(desc: str, ok: bool, detail: str = "") -> None:
    if not ok:
        failed.append(desc)
    print(f"  {'OK ' if ok else 'NG '} {desc}{('  — ' + str(detail)) if detail and not ok else ''}")


def run(registry: Path | None, *args: str) -> tuple[int, str]:
    env = dict(os.environ)
    if registry is not None:
        env["WORKOS_REGISTRY"] = str(registry)
    r = subprocess.run([sys.executable, str(GATE), "--checks", *args],
                       capture_output=True, text=True, env=env, timeout=120)
    return r.returncode, r.stdout + r.stderr


def listing(registry: Path | None) -> tuple[str, dict]:
    code, text = run(registry)
    assert code == 0, text
    code, raw = run(registry, "--json")
    assert code == 0, raw
    return text, json.loads(raw)


def pairs(cat: dict) -> set[tuple[str, str]]:
    return {(lane["name"], c["id"])
            for st in cat["stages"] for lane in st["lanes"] for c in lane["checks"]}


def find(cat: dict, stage: str, lane: str, cid: str) -> dict | None:
    for st in cat["stages"]:
        if st["name"] != stage:
            continue
        for ln in st["lanes"]:
            if ln["name"] != lane:
                continue
            for c in ln["checks"]:
                if c["id"] == cid:
                    return c
    return None


def documented_kinds(path: Path) -> set[str]:
    """骨格の冒頭コメントが「書ける」と言っている kind を読む。

    期待する名前をこちらに並べない。並べたら、同じ写しが1枚増えるだけである。
    形で拾う: 「観測の種類」の節の中の、3つの空白で始まる語。
    """
    out: set[str] = set()
    inside = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("#") and "観測の種類" in line:
            inside = True
            continue
        if inside:
            if line.startswith("#   ") and line[4:5].isascii() and line[4:5].isalpha():
                out.add(line[4:].split()[0])
            elif not line.startswith("#") or "共通オプション" in line:
                break
    return out


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        # ------------------------------------------------------------------- #
        print("1. 骨格だけで出る（registry が無い環境）")
        bare = tmp / "no-registry"
        bare.mkdir()
        text, cat = listing(bare)
        n = cat["totals"]["checks"]
        check("観測が1件以上出る", n > 0, f"{n} 件")
        check("レーンが1本以上出る", cat["totals"]["lanes"] > 0)
        check("全部が骨格の札になる",
              cat["totals"]["by_source"]["骨格"] == n,
              cat["totals"]["by_source"])
        check("registry の層が無いことを出す",
              "無し（この層の上書きは効いていない）" in text)
        check("engine が実装している観測の種類を出す",
              "command_exit_zero" in text and "pattern_absent" in text)
        check("段ごとに severity を出す（同じ観測が段で変わる）",
              (find(cat, "internal", "provenance", "no_private_terms") or {}).get("severity")
              == "warn"
              and (find(cat, "public", "provenance", "no_private_terms") or {}).get("severity")
              == "block")
        check("2段目の再掲は、変わった severity を名指しする",
              (find(cat, "public", "provenance", "no_private_terms") or {})
              .get("severity_was") == "warn")
        check("穴が無ければ「なし」と言う（沈黙で済ませない）",
              "なし。段が参照するレーンはすべて宣言されており" in text)
        check("リポジトリに依らないことを明示する", "リポジトリに依らない" in text)
        baseline = pairs(cat)

        # ------------------------------------------------------------------- #
        print("\n2. registry が触った欄を名指しする")
        over = tmp / "registry"
        over.mkdir()
        (over / "release_lanes.toml").write_text('''
[[lanes.safety.checks]]
id = "gitignore_exists"
severity = "warn"

[[lanes.provenance.checks]]
id = "no_private_terms"
auditable = true

[[lanes.safety.checks]]
id = "org_only_check"
kind = "file_present"
any_of = ["ORG.md"]
severity = "warn"
why = "この組織だけの観測"
''', encoding="utf-8")
        text, cat = listing(over)
        c = find(cat, "internal", "safety", "gitignore_exists")
        check("値を変えた欄は overridden に出る",
              c is not None and c["overridden"] == ["severity"], c)
        check("上書きされた値そのものが一覧に出る", c is not None and c["severity"] == "warn")
        check("札は骨格+registry（registry が作った観測ではない）",
              c is not None and c["source"] == "骨格+registry")
        check("上書きした欄が人の読む形にも出る", "骨格+registry: severity" in text)
        a = find(cat, "internal", "provenance", "no_private_terms")
        check("足した欄は added に出る（値の書き換えと区別する）",
              a is not None and a["added"] == ["auditable"] and a["overridden"] == [], a)
        check("足した欄は + 付きで出る", "+auditable" in text)
        new = find(cat, "internal", "safety", "org_only_check")
        check("registry が足した観測は registry の札になる",
              new is not None and new["source"] == "registry", new)

        # ------------------------------------------------------------------- #
        print("\n3. 走らない宣言を、走る宣言と同じ表に出す")
        holes = tmp / "holes"
        holes.mkdir()
        (holes / "release_lanes.toml").write_text('''
[stages.internal]
lanes = ["safety", "ghost_lane"]

[[lanes.safety.checks]]
id = "unknown_kind_check"
kind = "telepathy"
severity = "block"
why = "engine が実装していない種類"

[[lanes.safety.checks]]
id = "gitignore_exists"
enabled = false

[lanes.orphan]
title = "どの段にも配られていないレーン"
[[lanes.orphan.checks]]
id = "never_runs"
kind = "file_present"
any_of = ["NEVER.md"]
''', encoding="utf-8")
        text, cat = listing(holes)
        un = cat["unevaluated"]
        check("段が参照するレーンの宣言が無いことを出す",
              {"stage": "internal", "lane": "ghost_lane"} in un["missing_lanes"],
              un["missing_lanes"])
        check("その穴を人の読む形でも NG として出す",
              "ghost_lane レーンの宣言が無い" in text)
        check("engine が知らない kind を出す",
              any(i["id"] == "unknown_kind_check" and i["kind"] == "telepathy"
                  for i in un["unknown_kinds"]), un["unknown_kinds"])
        check("知らない kind は一覧の中でも「走らない」と印が付く",
              (find(cat, "internal", "safety", "unknown_kind_check") or {})
              .get("implemented") is False)
        check("どの段にも配られていないレーンを出す",
              any(i["lane"] == "orphan" for i in un["unplaced_lanes"]), un["unplaced_lanes"])
        check("enabled = false で落とした観測を出す（消えたことを消さない）",
              any(i["id"] == "gitignore_exists" and i["by"] == "registry"
                  for i in un["disabled"]), un["disabled"])
        check("落とした観測は一覧の本体からは消える",
              find(cat, "internal", "safety", "gitignore_exists") is None)
        check("穴が在るときは「なし」と言わない",
              "なし。段が参照するレーンはすべて宣言されており" not in text)
        check("引退した観測は穴と別の行に出す",
              "引退した観測（宣言に残っているが誰も評価しない）" in text)

        # ------------------------------------------------------------------- #
        print("\n4. 一覧と、実際に走る観測がずれない")
        # 判定を実際に走らせて、出てきた観測が一覧に在ることを確かめる。
        # 一覧を別経路で組み立てると、ずれても両方それらしく出る。
        r = subprocess.run([sys.executable, str(GATE), str(ROOT), "--json"],
                           capture_output=True, text=True, timeout=300)
        check("判定そのものが走る", r.returncode in (0, 1), r.stderr[-400:])
        evaluated: set[tuple[str, str]] = set()
        if r.stdout.strip():
            for repo in json.loads(r.stdout):
                for lane in repo["lanes"]:
                    for f in lane["findings"]:
                        evaluated.add((lane["name"], f["id"]))
        _, real = listing(None)
        catalogued = pairs(real)
        missing = sorted(evaluated - catalogued)
        check("実際に評価された観測は、すべて一覧に在る", not missing, missing)
        check("一覧は評価より狭くならない（shape と intent で絞られた分だけ多い）",
              len(catalogued) >= len(evaluated),
              f"一覧 {len(catalogued)} / 評価 {len(evaluated)}")
        check("骨格だけのときと本物の registry で、観測の集合が食い違わない",
              baseline <= catalogued or catalogued <= baseline,
              f"骨格のみ {len(baseline)} / 本物 {len(catalogued)}")

        # ------------------------------------------------------------------- #
        print("\n5. 宣言の語彙表と engine の実装がずれない")
        # 骨格の冒頭には、書ける kind の一覧が人の言葉で写してある。写しなので
        # ずれる。実測 2026-09-27: 実装されていない first_block_max_lines が
        # 残っており、実装されている pattern_present と exclude_not_tracked が
        # 抜けていた。ここに無い kind を書いた観測は落ちずに skip になるので、
        # 写しがずれている間、現場は「書けない kind」を書ける kind だと読む。
        #
        # 期待する名前をここに並べない（並べれば同じ写しが3枚目になる）。
        # 2つの集合が一致することだけを見る。
        documented = documented_kinds(ROOT / "config" / "release_lanes.toml")
        _, real0 = listing(None)
        implemented = set(real0["kinds"])
        check("語彙表が1件以上読めた", bool(documented), documented)
        check("語彙表に、実装されていない kind が無い",
              not (documented - implemented), sorted(documented - implemented))
        check("実装されている kind が、語彙表から漏れていない",
              not (implemented - documented), sorted(implemented - documented))

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
