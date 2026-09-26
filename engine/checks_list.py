#!/usr/bin/env python3
"""checks_list.py — ゲートが何を見るのかを、走らせる前に列挙する。

release_gate.py は「このリポジトリを出せるか」に答える。こちらは **「そもそも
何を見ているのか」** に答える。判定を読む側（人でも Claude でも）は、OK/NG の
意味を知るために観測の一覧が要る。一覧が無いと、読む側は出力に現れた観測だけを
ゲートの全体だと思う。通った観測は既定では出ないので、その像は必ず欠ける。

3つを守る。

1. **列挙は merge 済みの判定基準そのものから作る。** 一覧のためにもう一度
   読み直して組み立てると、一覧と実際に走るものが静かにずれる。ずれても
   どちらも「それらしく」出るので、気づく手がかりが無い。

2. **1件ごとに出どころを添える。** 判定基準は2層（骨格 = work-os/config、
   上書き = registry）。どちらが決めたかを出さないと、組織が選んだ上書きを
   engine の既定として読まれる。registry が無い環境では骨格だけが出る。

3. **評価されない宣言を、評価される宣言と同じ表に出す。** 宣言されているのに
   走らない観測は、落ちるのではなく **通ってしまう** 側の壊れ方である。
   段がレーンを参照しているのに宣言が無ければ engine は黙って飛ばし
   （release_gate.evaluate の `continue`）、engine が知らない kind は
   severity=warn の skip になる。どちらも一覧に出さなければ、無いのと同じになる。
   無いときは「なし」と言う。沈黙を「問題なし」の代わりにしない。

この一覧は **リポジトリに依らない**。実際に当たる観測は、対象の shape（レーンの
applies_to_shapes）と intent（段の打ち切り）で絞られる。絞った後を見たいときは
release_gate.py <repo> を走らせる。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from release_gate import (  # noqa: E402
    LANES_DEFAULT, LANES_FILE, OBSERVERS, check_severity,
)
# E402 は「import が先頭に無い」の指摘。sys.path を挿した後でしか解決しない。
from workos import _load_toml  # noqa: E402

SKELETON = "骨格"       # work-os/config —— 公開されている側。観測の種類と段のラダー
OVERRIDE = "registry"   # 非公開の側 —— 事実（顧客名・秘密の形）と組織の選択

# 出どころの札。両方に在るときは「骨格を registry が上書きした」であって、
# registry が作った観測ではない。作ったのと直したのを同じ札にしない。
BOTH = f"{SKELETON}+{OVERRIDE}"


# --------------------------------------------------------------------------- #
# 出どころ
# --------------------------------------------------------------------------- #

def layer_files() -> list[tuple[str, Path]]:
    """層の名前とファイル。存在の有無はここでは判定しない（呼ぶ側が出す）。"""
    return [(SKELETON, LANES_DEFAULT), (OVERRIDE, LANES_FILE)]


def _raw_layers() -> list[tuple[str, dict]]:
    """各層を **重ねずに** 読む。重ねた後からは、どちらが書いたかを復元できない。"""
    out: list[tuple[str, dict]] = []
    for label, path in layer_files():
        if path.is_file():
            out.append((label, _load_toml(path)))
    return out


def _check_index(raw: dict) -> dict[tuple[str, str], dict]:
    """(レーン名, 観測 id) → その層が書いた宣言。"""
    out: dict[tuple[str, str], dict] = {}
    for lane, body in (raw.get("lanes") or {}).items():
        if not isinstance(body, dict):
            continue
        for chk in body.get("checks") or []:
            if isinstance(chk, dict) and chk.get("id") is not None:
                out[(lane, str(chk["id"]))] = chk
    return out


def provenance() -> dict:
    """観測とレーンが、どの層から来たかを引けるようにする。

    上書きされた **欄の名前** まで出す。「registry が触った」だけでは、触った先が
    severity なのか why なのかが読めない。severity を上書きされた観測は、骨格の
    一覧を読んだ人が思っているものと consequence が違う。
    """
    raws = _raw_layers()
    idx = {label: _check_index(raw) for label, raw in raws}
    keys = {k for i in idx.values() for k in i}

    checks: dict[tuple[str, str], dict] = {}
    for key in keys:
        layers = [label for label, _ in raws if key in idx[label]]
        merged: dict = {}
        overridden: list[str] = []   # 骨格が書いた欄を、上の層が別の値にした
        added: list[str] = []        # 骨格が書いていない欄を、上の層が足した
        enabled_by = ""
        for i, label in enumerate(layers):
            entry = idx[label][key]
            for field, val in entry.items():
                if field == "id":
                    continue
                if i > 0:
                    # 足したのと変えたのを同じ札にしない。auditable のように骨格が
                    # 黙っている欄を registry が足すのは、値の書き換えではなく
                    # 「この組織はこの観測に人の判定を入れる」という別の宣言である。
                    if field not in merged:
                        added.append(field)
                    elif merged[field] != val:
                        overridden.append(field)
                merged[field] = val
                if field == "enabled":
                    enabled_by = label
        source = BOTH if len(layers) > 1 else (layers[0] if layers else SKELETON)
        checks[key] = {
            "source": source,
            "layers": layers,
            "overridden": sorted(set(overridden)),
            "added": sorted(set(added)),
            # enabled = false は merge の時点で落ちる。落ちたことは merge 後の
            # 判定基準に残らないので、ここでしか言えない。
            "disabled": merged.get("enabled", True) is False,
            "disabled_by": enabled_by,
        }

    lanes: dict[str, dict] = {}
    for label, raw in raws:
        for lane in (raw.get("lanes") or {}):
            lanes.setdefault(lane, {"layers": []})["layers"].append(label)
    for lane, info in lanes.items():
        info["source"] = BOTH if len(info["layers"]) > 1 else info["layers"][0]

    return {"checks": checks, "lanes": lanes,
            "files": [{"layer": label, "path": str(path), "present": path.is_file()}
                      for label, path in layer_files()]}


# --------------------------------------------------------------------------- #
# 一覧の組み立て
# --------------------------------------------------------------------------- #

FLAG_LABELS = [
    ("only_when_owned", "自組織のみ"),
    ("optional", "宣言が無ければ問わない"),
    ("auditable", "人の判定を受ける"),
]


def _flags(chk: dict) -> list[str]:
    out = [label for key, label in FLAG_LABELS if chk.get(key)]
    if chk.get("kind") == "command_exit_zero":
        # 静的観測と実走する観測を一覧の上で分ける。--execute を付けなければ
        # skip で終わる観測を、付けずに読んだ人は「通った」と読む。
        out.append("--execute で実走")
    return out


def _check_entry(chk: dict, lane: str, stage: str, prov: dict) -> dict:
    kind = str(chk.get("kind", ""))
    cid = str(chk.get("id", "?"))
    p = prov["checks"].get((lane, cid),
                           {"source": SKELETON, "overridden": [], "added": []})
    return {
        "id": cid,
        "kind": kind,
        "severity": check_severity(chk, stage),
        "why": str(chk.get("why", "")),
        "source": p["source"],
        "overridden": p["overridden"],
        "added": p["added"],
        "flags": _flags(chk),
        # engine が実装していない kind は、走らずに warn の skip になる。
        # 「当たらなかった」と「当てられなかった」を一覧の上で分ける。
        "implemented": kind in OBSERVERS,
        "executes": kind == "command_exit_zero",
    }


def build(lanes_cfg: dict) -> dict:
    """merge 済みの判定基準から一覧を作る。引数はゲートが使うものと同じ辞書。"""
    prov = provenance()
    gate = lanes_cfg.get("gate", {})
    stage_names = list(gate.get("stages", []))
    declared_lanes = dict(lanes_cfg.get("lanes", {}))

    stages: list[dict] = []
    placed: set[str] = set()
    missing_lanes: list[dict] = []
    seen: dict[tuple[str, str], str] = {}        # (レーン, id) → 先に出た段の severity
    first_stage: dict[tuple[str, str], str] = {}  # (レーン, id) → 先に出た段の名前

    for stage_name in stage_names:
        st_cfg = lanes_cfg.get("stages", {}).get(stage_name, {})
        stage = {"name": stage_name, "title": st_cfg.get("title", stage_name),
                 "question": st_cfg.get("question", ""), "lanes": []}
        for lane_name in st_cfg.get("lanes", []):
            cfg = declared_lanes.get(lane_name)
            if not cfg:
                # evaluate() はここで黙って continue する。段が呼んでいる観測が
                # 1つも走らないまま、その段は OK として出る。
                missing_lanes.append({"stage": stage_name, "lane": lane_name})
                continue
            placed.add(lane_name)
            lane = {
                "name": lane_name,
                "title": cfg.get("title", lane_name),
                "question": cfg.get("question", ""),
                "source": prov["lanes"].get(lane_name, {}).get("source", SKELETON),
                "applies_to_shapes": list(cfg.get("applies_to_shapes") or []),
                "repeat": lane_name in {ln["name"] for s in stages for ln in s["lanes"]},
                "checks": [],
            }
            for chk in cfg.get("checks", []):
                entry = _check_entry(chk, lane_name, stage_name, prov)
                key = (lane_name, entry["id"])
                prev = seen.get(key)
                # 同じ観測が段をまたいで当たる（累積のラダー）。増えている情報は
                # severity だけなので、変わったときだけそう言う。
                entry["repeat_of"] = ""
                entry["repeat_of_index"] = 0
                entry["severity_was"] = ""
                if prev is None:
                    first_stage[key] = stage_name
                else:
                    entry["repeat_of"] = first_stage[key]
                    entry["repeat_of_index"] = stage_names.index(first_stage[key]) + 1
                    if prev != entry["severity"]:
                        entry["severity_was"] = prev
                seen[key] = entry["severity"]
                lane["checks"].append(entry)
            stage["lanes"].append(lane)
        stages.append(stage)

    unknown_kinds = []
    disabled = []
    for lane_name, cfg in declared_lanes.items():
        for chk in cfg.get("checks", []):
            cid = str(chk.get("id", "?"))
            if str(chk.get("kind", "")) not in OBSERVERS:
                unknown_kinds.append({"lane": lane_name, "id": cid,
                                      "kind": str(chk.get("kind", "")),
                                      "placed": lane_name in placed})
    for (lane_name, cid), p in sorted(prov["checks"].items()):
        if p["disabled"]:
            disabled.append({"lane": lane_name, "id": cid, "by": p["disabled_by"]})

    unplaced = [{"lane": name,
                 "source": prov["lanes"].get(name, {}).get("source", SKELETON)}
                for name in declared_lanes if name not in placed]

    retired = [{"id": rid, "note": str(note).strip().splitlines()[0] if note else ""}
               for rid, note in (lanes_cfg.get("retired") or {}).items()]

    flat = [c for s in stages for ln in s["lanes"] for c in ln["checks"]]
    unique = {(ln["name"], c["id"])
              for s in stages for ln in s["lanes"] for c in ln["checks"]}
    return {
        "layers": prov["files"],
        "kinds": sorted(OBSERVERS),
        "stages": stages,
        "unevaluated": {
            "missing_lanes": missing_lanes,
            "unplaced_lanes": sorted(unplaced, key=lambda d: d["lane"]),
            "unknown_kinds": unknown_kinds,
            "disabled": disabled,
            "retired": retired,
        },
        "totals": {
            "stages": len(stages),
            "lanes": len(placed),
            "checks": len(unique),
            "placements": len(flat),
            "executes": len({(ln["name"], c["id"]) for s in stages for ln in s["lanes"]
                             for c in ln["checks"] if c["executes"]}),
            "by_source": {
                label: len({(ln["name"], c["id"]) for s in stages for ln in s["lanes"]
                            for c in ln["checks"] if c["source"] == label})
                for label in (SKELETON, OVERRIDE, BOTH)
            },
        },
    }


# --------------------------------------------------------------------------- #
# 出力
# --------------------------------------------------------------------------- #

def render(cat: dict) -> None:
    print("判定基準の出どころ")
    for f in cat["layers"]:
        state = " — 無し（この層の上書きは効いていない）" if not f["present"] else ""
        print(f"  {f['layer']} = {f['path']}{state}")
    print("\nengine が実装している観測の種類:")
    print(f"  {', '.join(cat['kinds'])}")

    for i, stage in enumerate(cat["stages"], 1):
        print(f"\n第{i}段 {stage['name']}: {stage['title']}")
        if stage["question"]:
            print(f"  問い: {stage['question']}")
        for lane in stage["lanes"]:
            shapes = (f"  [形: {', '.join(lane['applies_to_shapes'])} にのみ当たる]"
                      if lane["applies_to_shapes"] else "")
            src = f"  <{lane['source']}>" if lane["source"] != SKELETON else ""
            again = "（再掲）" if lane["repeat"] else ""
            print(f"  ── {lane['name']}: {lane['title']}{again}{shapes}{src}")
            for c in lane["checks"]:
                if c["repeat_of"]:
                    # 同じ観測が上の段でも当たる。増えている情報は severity だけ。
                    moved = (f"{c['severity_was']} → {c['severity']}"
                             if c["severity_was"] else "同じ severity")
                    print(f"     {c['severity']:<5}  {c['id']:<30}"
                          f" 第{c['repeat_of_index']}段（{c['repeat_of']}）と"
                          f"同じ観測 — {moved}")
                    continue
                tags = list(c["flags"])
                if c["source"] != SKELETON:
                    touched = c["overridden"] + [f"+{f}" for f in c["added"]]
                    tags.append(c["source"]
                                + (f": {', '.join(touched)}" if touched else ""))
                if not c["implemented"]:
                    tags.append("engine が知らない kind — 走らない")
                tag = f"  [{' / '.join(tags)}]" if tags else ""
                print(f"     {c['severity']:<5}  {c['id']:<30} {c['kind']:<22}{tag}"
                      .rstrip())
                if c["why"]:
                    print(f"            {c['why']}")

    _render_unevaluated(cat["unevaluated"])

    t = cat["totals"]
    by = t["by_source"]
    print(f"\n観測 {t['checks']} 件 / レーン {t['lanes']} 本 / 段 {t['stages']} 段"
          f"（延べ {t['placements']} 回当たる）")
    print(f"  出どころ: {SKELETON} {by[SKELETON]} / {OVERRIDE} {by[OVERRIDE]}"
          f" / {BOTH} {by[BOTH]}")
    print(f"  --execute を付けたときだけ走る観測: {t['executes']} 件")
    print("\nこの一覧はリポジトリに依らない。実際に当たる観測は shape と intent で"
          "絞られる。\n絞った後を見るには release_gate.py <repo> を走らせる。")


def _render_unevaluated(un: dict) -> None:
    """走らない宣言。**無いときも「なし」と言う。** 沈黙は通過の証拠にならない。"""
    print("\n評価されない宣言")
    empty = True
    for item in un["missing_lanes"]:
        empty = False
        print(f"  NG  第{item['stage']}段が参照する {item['lane']} レーンの宣言が無い"
              "（engine は黙って飛ばす）")
    for item in un["unknown_kinds"]:
        empty = False
        where = "" if item["placed"] else "（レーン自体もどの段にも配られていない）"
        print(f"  NG  {item['lane']}.{item['id']}: kind={item['kind']} を engine が"
              f"実装していない — skip になる{where}")
    for item in un["unplaced_lanes"]:
        empty = False
        print(f"  --  {item['lane']} レーンはどの段にも配られていない（宣言だけ在る）")
    for item in un["disabled"]:
        empty = False
        print(f"  --  {item['lane']}.{item['id']} は {item['by']} が enabled = false "
              "で落としている")
    if empty:
        print("  なし。段が参照するレーンはすべて宣言されており、"
              "engine が知らない kind も無い")
    if un["retired"]:
        # 引退は穴ではない。宣言に名前だけ残っているので、一覧に出さないと
        # 「在る観測」として読まれる。穴の「なし」とは別の行に置く。
        ids = ", ".join(r["id"] for r in un["retired"])
        print(f"  参考: 引退した観測（宣言に残っているが誰も評価しない）: {ids}")
