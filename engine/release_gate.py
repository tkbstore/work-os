#!/usr/bin/env python3
"""release_gate.py — そのリポジトリを外に出して恥ずかしくないかを機械が見る。

fleet.py は「フリートが壊れていないか」を見る。こちらは「1つのリポジトリが
他人の手に渡せる状態か」を見る。別の問いなので別のゲートにしてある。

原理は1つ。**主張ではなく観測で決める**。
「README がある」ではなく「README に実行できる行があるか」を見る。
「テストがある」ではなく「宣言されたテストコマンドが exit 0 で終わるか」を見る。

このファイルは観測の *種類* だけを持つ。何が公開の条件かは組織が決めることなので
registry/release_lanes.toml に宣言する。基準を変える現場はコードを触らない。

  python3 engine/release_gate.py                  # カレントを検査（静的のみ）
  python3 engine/release_gate.py <repo> --execute # 宣言されたコマンドを実走させる
  python3 engine/release_gate.py --scan <root>    # 公開を宣言した全リポを検査
  python3 engine/release_gate.py --json           # 機械可読

外部依存なし。--execute を付けない限り、読み取りのみ。
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from workos import _load_toml, iter_repo_dirs, registry_root  # noqa: E402
# E402 は「import が先頭に無い」の指摘。sys.path を挿した後でしか解決しない。
import audit  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = registry_root()
# 骨格（観測の種類・段のラダー・既定の severity）は work-os が持つ。
LANES_DEFAULT = Path(__file__).resolve().parent.parent / "config" / "release_lanes.toml"
# 事実（顧客名・秘密のパターン）と組織の選択は registry が持つ。差分だけを書く。
LANES_FILE = REGISTRY / "release_lanes.toml"

# 実行できるコードブロックとみなす言語。空の info string も手順とみなす。
RUNNABLE_LANGS = {"", "sh", "bash", "zsh", "shell", "console",
                  "python", "py", "js", "ts", "node"}
# 罫線・矢印を含むブロックは図であって手順ではない。無印のコードブロックに
# 構成図を置く README は多く、これを「実行できる行」と数えると意味が壊れる。
DIAGRAM_CHARS = set("─│┌┐└┘├┤┬┴┼━┃╴╶▶▼◀▲→←↑↓╔╗╚╝║═")
MAX_BYTES = 512_000       # これを超えるファイルは本文検査から外す（生成物・データ）
MAX_EXAMPLES = 3          # 失敗の例示は何件まで出すか
# 当たった宣言を何種まで名指しするか。例示（場所）とは別に数えるのは、当たった語が
# **分類の結果そのもの**であり、黙って切ると誤判定が見えなくなるためである。
# 切るときは「ほか N 種」と数を言う。
MAX_NAMED = 12


# --------------------------------------------------------------------------- #
# 観測結果の型
# --------------------------------------------------------------------------- #

@dataclass
class Finding:
    check_id: str
    kind: str
    severity: str           # block | warn
    state: str              # pass | fail | skip
    why: str
    detail: str = ""
    examples: list[str] = field(default_factory=list)
    # --execute を付ければ実際に走る観測か。skip の理由は1つではないので、
    # 「走らなかった」と「そもそも走らない」を state では区別できない。
    pending: bool = False


@dataclass
class LaneResult:
    name: str
    title: str
    question: str
    findings: list[Finding] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return any(f.state == "fail" and f.severity == "block" for f in self.findings)

    @property
    def warnings(self) -> int:
        return sum(1 for f in self.findings if f.state == "fail" and f.severity == "warn")

    @property
    def skipped(self) -> int:
        return sum(1 for f in self.findings if f.state == "skip")

    @property
    def unproven(self) -> list[Finding]:
        """止めるはずの観測が、走らないまま残っているもの。

        skip の理由は1つではない。「当たらない」（e2e を持たない形のリポ、
        --owned でない、shape が違う）と「走らせていない」は別のことなのに、
        どちらも skip なので、レーンは同じ顔で OK になっていた。前者は結論だが
        後者は結論ではない。severity=block の観測が後者のとき、その段は
        「通った」ではなく「まだ確かめていない」である。
        """
        return [f for f in self.findings if f.pending and f.severity == "block"]


@dataclass
class StageResult:
    """梯子の一段。段ごとに基準を作るのではなく、1本の梯子を途中で区切る。"""
    name: str
    title: str
    question: str
    lanes: list[LaneResult] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return any(l.blocked for l in self.lanes)

    @property
    def unproven(self) -> list[Finding]:
        return [f for l in self.lanes for f in l.unproven]

    @property
    def settled(self) -> bool:
        """この段の結論が出ているか。落ちてもいないが、確かめてもいない段がある。"""
        return not self.blocked and not self.unproven


@dataclass
class RepoResult:
    name: str
    root: Path
    intent: str
    enforcement: str
    # 宣言が無いリポ（--assume-public の校正モード）では、宣言に依存する観測を
    # 走らせない。何件が未実走かを数えるときも、走る予定の無いものは数えない。
    declared: bool = True
    # intent をどこから得たか。宣言とそれ以外を混ぜると、出力が「宣言した」と
    # 読まれてしまう。段に載せたことと、本人が宣言したことは別の事実である。
    #   declared  work.toml の [publish] intent
    #   default   宣言が無いので判定基準の [gate] default_intent を当てた
    #             （骨格 work-os/config、または registry の上書き）
    #   assumed   --assume-public の校正モード
    intent_source: str = "declared"
    # 自組織のものと見なした根拠。当たった宣言を出す（分類したら根拠を出す）
    owned_note: str = ""
    stages: list[StageResult] = field(default_factory=list)

    @property
    def lanes(self) -> list[LaneResult]:
        return [l for st in self.stages for l in st.lanes]

    @property
    def shown_stages(self) -> list[StageResult]:
        """render が出す範囲。累積の梯子なので、結論の出ていない段より上は見ない。"""
        out: list[StageResult] = []
        for st in self.stages:
            out.append(st)
            if not st.settled:
                break
        return out

    @property
    def pending_executions(self) -> int:
        """--execute を付ければ実際に走る観測の数。

        「--execute で走らせます」という案内は、走る観測が在るときにしか真に
        ならない。intent が段名なら評価はその段で止まるので、実行を伴う観測が
        その段に配られていなければ 0 になる。数えずに案内を出すと、付けても
        何も起きないものを「付ければ走る」と言うことになる。

        実測: 第1段までしか測らないリポが test を宣言しているのに、実行を伴う
        観測が第2段のレーンだったため --execute で1件も走らなかった。それでも
        この案内だけは毎回出ていた。どのリポで起きたかは registry/ 側に書く。
        """
        if not self.declared:
            return 0
        return sum(1 for st in self.shown_stages for lane in st.lanes
                   for f in lane.findings if f.pending)

    @property
    def reached(self) -> str:
        """通り抜けた一番上の段。累積なので、結論の出ていない段より上は見ない。

        止めるはずの観測を走らせていない段は、到達に数えない。数えていた頃は
        `--execute` を付けずに回すと「テストが通ることを一度も確かめずに
        公開できる」と答えた。走らせなかったことと、走らせて通ったことを
        同じ結論にしない。
        """
        top = ""
        for st in self.stages:
            if not st.settled:
                break
            top = st.name
        return top

    @property
    def blocked_at(self) -> str:
        for st in self.stages:
            if st.blocked:
                return st.name
        return ""

    @property
    def unproven_at(self) -> str:
        """落ちてはいないが、確かめていない観測が残っている一番下の段。"""
        for st in self.stages:
            if st.blocked:
                return ""
            if st.unproven:
                return st.name
        return ""

    @property
    def publishable(self) -> bool:
        """最後の段まで通ったか。public リポジトリとして切り出せるか。"""
        return bool(self.stages) and all(st.settled for st in self.stages)


# --------------------------------------------------------------------------- #
# 入力の読み込み
# --------------------------------------------------------------------------- #

def _merge_checks(base: list, over: list) -> list:
    """観測の列を **id で** 突き合わせる。位置では合わせない。

    位置で合わせると、骨格の側に観測を1つ挿し込んだ日に、上書きが全部1つずれて
    別の観測に当たる。しかもその日は何も落ちない（当たり先が在るので）。
    enabled = false は落とす。骨格の観測が自分の組織に合わないとき、上書き側から
    消す手段が無いと、合わない観測を毎回読み飛ばす運用になる。
    """
    out: list[dict] = []
    by_id: dict[str, dict] = {}
    for chk in base:
        d = dict(chk)
        by_id[str(d.get("id"))] = d
        out.append(d)
    for chk in over:
        cid = str(chk.get("id"))
        if cid in by_id:
            by_id[cid].update(dict(chk))
        else:
            d = dict(chk)
            by_id[cid] = d
            out.append(d)
    return [c for c in out if c.get("enabled", True)]


def _merge_cfg(base: dict, over: dict) -> dict:
    """表は再帰的に重ね、観測の列は id で突き合わせ、それ以外の配列は置き換える。

    配列を継ぎ足さないのは、継ぎ足しと置き換えを同じ書き方で表すと、書いた側が
    どちらになるか読めなくなるからである。置き換えなら宣言した通りになる。
    """
    out = dict(base)
    for key, val in over.items():
        cur = out.get(key)
        if isinstance(cur, dict) and isinstance(val, dict):
            out[key] = _merge_cfg(cur, val)
        elif key == "checks" and isinstance(cur, list) and isinstance(val, list):
            out[key] = _merge_checks(cur, val)
        else:
            out[key] = val
    return out


def load_lanes() -> dict:
    """判定基準を2層で読む。骨格は work-os、事実と組織の選択は registry。

    骨格まで registry に置いていたときは、公開されている work-os はゲートの形すら
    持っておらず、install しても1つも観測が走らなかった（load_lanes がその場で
    sys.exit していた）。一方、顧客名や秘密のパターンを work-os に置くことはできない。
    だから形と事実を別の層に置く。engine には事実を持たせない、と同じ理由である。

    上書きは **差分だけ** を書く。骨格を丸ごと写して上書きすると、同じ宣言が2箇所に
    在る状態になり、片方だけが更新されて食い違う（カタログで実際に起きた形）。
    """
    base = _load_toml(LANES_DEFAULT) if LANES_DEFAULT.is_file() else {}
    over = _load_toml(LANES_FILE) if LANES_FILE.is_file() else {}
    if not base:
        if not over:
            sys.exit(f"判定基準が見つかりません: {LANES_DEFAULT}")
        # 骨格が無い配置（registry に全部在る古い形）。そのまま動かす。
        return over
    return _merge_cfg(base, over)


@lru_cache(maxsize=1)
def _policy() -> tuple[dict[str, tuple[str, str]], str]:
    """統治クラスの宣言を1度だけ読む。(宣言, 読めなかった理由) を返す。

    所有者の一覧をここで新しく書かない。同じことを2箇所で宣言すると、片方だけが
    更新されて食い違う。fleet_policy.toml は既に「既定は owned、例外だけ名指し」
    という形を持っているので、それを借りる。

    読み込みの失敗を投げ返さない。宣言が壊れているだけでゲート全体が traceback で
    死ぬと、公開可否をひとつも答えられなくなる（実測 2026-09-26: 重複した表を
    1つ足しただけでそうなった）。読めなかったことは呼ぶ側に伝える。
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        from fleet import load_policy  # noqa: PLC0415
        return load_policy(), ""
    # 壊れ方の種類で扱いは変わらない。どの例外でも「読めなかった」1つに落ちる。
    except Exception as exc:                                   # noqa: BLE001
        return {}, f"{type(exc).__name__}: {exc}"


def _fleet_class(name: str) -> tuple[str, str]:
    """リポ名から (統治クラス, 理由)。宣言が無ければ owned（fleet.py と同じ既定）。"""
    pol, err = _policy()
    if err:
        # 読めないことを「例外の宣言なし」と同じ扱いに畳まない。観測は当てたまま
        # にして（当てないほうは検査が静かに消える側である）、根拠が読めなかった
        # ことを出力に残す。黙って owned にすると、他者のリポを自組織として
        # 扱ったことが出力から消える。
        return "owned", f"宣言を読めませんでした（{err}）"
    return pol.get(name, ("owned", ""))


def load_publish(root: Path) -> dict:
    """work.toml の [publish]。宣言が無ければ空。

    公開意図は統治クラス（owned/team/...）と直交する軸なので別に宣言させる。
    宣言していないリポはゲートの対象外。既存を壊さない additive な導入。
    """
    work = root / "work.toml"
    if not work.is_file():
        return {}
    try:
        data = _load_toml(work)
    except Exception as exc:                                   # noqa: BLE001
        print(f"  warn  work.toml を読めません: {exc}", file=sys.stderr)
        return {}
    pub = data.get("publish", {})
    if not isinstance(pub, dict):
        return {}
    pub.setdefault("_enforcement", data.get("repo", {}).get("enforcement", "warn"))
    return pub


def tracked_files(root: Path, exclude: list[str]) -> list[tuple[str, Path]]:
    """git 管理下のファイルだけを見る。公開されるのは追跡されているものだけ。"""
    out = subprocess.run(["git", "-C", str(root), "ls-files"],
                         capture_output=True, text=True, timeout=60)
    files = []
    for rel in out.stdout.splitlines():
        if not rel or any(rel.startswith(e.rstrip("/") + "/") or rel == e.rstrip("/")
                          for e in exclude):
            continue
        p = root / rel
        if p.is_file():
            files.append((rel, p))
    return files


def read_text(path: Path) -> str | None:
    try:
        if path.stat().st_size > MAX_BYTES:
            return None
        raw = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in raw[:4096]:      # バイナリ
        return None
    return raw.decode("utf-8", errors="ignore")


def load_pattern_file(name: str,
                      categories: list[str] | None = None) -> tuple[list[tuple[str, str]], bool]:
    """registry の宣言ファイルからパターンを読む。

    [patterns] テーブルがあれば値は正規表現。無ければ値は literal な語として扱う。
    literal 側（固有名詞の一覧など）を正規表現として解釈しないための規約。

    categories を渡すと、その名前のテーブルだけを使う。1つの宣言ファイルが
    複数の目的に使われるとき、目的ごとに要る部分だけを取るための口。
    """
    # 骨格と事実の2層。形（秘密の正規表現など）は work-os が持ち、事実（固有名詞など）は
    # registry が持つ。同名のファイルが両方に在れば registry を上に重ねる。
    # 重ね方は lanes と同じ規則にそろえる（表は再帰、葉は置き換え）。規則が2通りあると
    # 書いた側がどちらになるか読めなくなる。
    data: dict = {}
    for path in (LANES_DEFAULT.parent / name, REGISTRY / name):
        if path.is_file():
            data = _merge_cfg(data, _load_toml(path))
    if not data:
        return [], True
    if categories:
        data = {k: v for k, v in data.items() if k in categories}
    if isinstance(data.get("patterns"), dict):
        return [(k, str(v)) for k, v in data["patterns"].items()], True
    out: list[tuple[str, str]] = []
    for group, body in data.items():
        if isinstance(body, dict):
            for key, val in body.items():
                for term in (val if isinstance(val, list) else [val]):
                    if str(term).strip():
                        out.append((f"{group}.{key}", str(term)))
        elif isinstance(body, list):
            for term in body:
                if str(term).strip():
                    out.append((group, str(term)))
    return out, False


# --------------------------------------------------------------------------- #
# 観測の種類（engine が知っている語彙はこれだけ）
# --------------------------------------------------------------------------- #

def _segment_suffixes(rel: str) -> list[str]:
    """rel を区切りごとに切り落とした後半部分。**/ が食う量の候補になる。

    "a/b/tests/t.py" → ["a/b/tests/t.py", "b/tests/t.py", "tests/t.py", "t.py"]
    """
    out = [rel]
    i = rel.find("/")
    while i != -1:
        out.append(rel[i + 1:])
        i = rel.find("/", i + 1)
    return out


def _match_glob(rel: str, pattern: str) -> bool:
    """`**/` は「0個以上のディレクトリ」を意味する。区切りの位置で切って総当たりする。

    以前は先頭一致とファイル名一致の2つしか見ておらず、`**/` が **0セグメントを
    食う場合しか当たらなかった**。`**/tests/*` は `tests/t.py` には当たるが
    `a/tests/t.py` には当たらない。落ちるのではなく「除外されない」形で壊れるので、
    presets.fixtures（テスト用の見本を除外する宣言）が入れ子のレイアウトに対して
    ほぼ無効だった。実害: linter の意図的に壊した fixture を 101 件、
    5階層下から拾って robustness レーンで落としていた。

    `*` が区切りを跨ぐ fnmatch の挙動はそのまま使う。除外は「その下すべて」を
    意味してほしいので、跨いでくれるほうが正しい。ここを変えると
    `**/*.sample.*` のような既存の宣言の意味が変わる。
    """
    if pattern in ("**/*", "**"):
        return True
    if pattern.startswith("**/"):
        tail = pattern[3:]
        return any(fnmatch.fnmatch(s, tail) for s in _segment_suffixes(rel))
    return fnmatch.fnmatch(rel, pattern)


def _except_globs(chk: dict, lanes_cfg: dict) -> list[str]:
    """除外の指定。使い回す塊は presets に名前を付けて共有する。"""
    out = list(chk.get("except_globs", []))
    for name in chk.get("except_presets", []):
        out += list(lanes_cfg.get("presets", {}).get(name, []))
    return out


def _scoped(files: list[tuple[str, Path]], globs: list[str],
            except_globs: list[str] | None = None) -> list[tuple[str, Path]]:
    skip = except_globs or []
    return [(rel, p) for rel, p in files
            if any(_match_glob(rel, g) for g in globs)
            and not any(_match_glob(rel, g) for g in skip)]


def obs_file_present(root: Path, chk: dict, ctx: dict) -> tuple[str, str, list[str]]:
    """候補のいずれかが在るか。* を含む候補は追跡ファイル全体に当てる。

    トップレベルしか見ないと、crates/*/tests のような入れ子レイアウトを
    「テスト無し」と誤判定する（実際に高star リポで起きた）。

    ただし *在ること* を見る観測こそ除外を守らないと危ない。無いものを在ると
    答えるのは、偽陽性ではなく **偽陰性**——通してはいけないものを通す——だからである。
    実測: `**/LICENSE*` を許した直後、vendor した PyYAML の LICENSE を自分のものとして
    数え、ライセンスを持たない2リポジトリが「公開可」に化けた。
    """
    skip = _except_globs(chk, ctx["lanes_cfg"])
    for cand in chk.get("any_of", []):
        if "*" in cand:
            for rel, _ in ctx["files"]:
                if _match_glob(rel, cand) and not any(_match_glob(rel, g) for g in skip):
                    return "pass", f"{cand} に該当（{rel}）", []
        elif (root / cand).exists():
            return "pass", f"{cand} を確認", []
    return "fail", "いずれも存在しない: " + ", ".join(chk.get("any_of", [])), []


def obs_doc_section(root: Path, chk: dict, ctx: dict) -> tuple[str, str, list[str]]:
    target = root / chk.get("file", "README.md")
    if not target.is_file():
        return "fail", f"{chk.get('file')} が無い", []
    text = read_text(target) or ""
    for rx in chk.get("any_of", []):
        if re.search(rx, text):
            return "pass", "該当する見出しあり", []
    return "fail", f"{chk.get('file')} に該当する節が無い", []


def obs_runnable_block(root: Path, chk: dict, ctx: dict) -> tuple[str, str, list[str]]:
    target = root / chk.get("file", "README.md")
    if not target.is_file():
        return "fail", f"{chk.get('file')} が無い", []
    text = read_text(target) or ""
    text, scope = _window(text, chk)
    langs = set(chk.get("langs", RUNNABLE_LANGS))
    count = 0
    for info, body in re.findall(r"^```([^\n`]*)\n(.*?)^```", text, re.S | re.M):
        if info.strip().lower() in langs and _is_commands(body):
            count += 1
    need = int(chk.get("min", 1))
    if count >= need:
        return "pass", f"{scope}に実行できるブロック {count} 個", []
    return "fail", f"{scope}に実行できるブロックが {count} 個（{need} 個以上必要）", []


def _window(text: str, chk: dict) -> tuple[str, str]:
    """観測する範囲を先頭に絞る。行数と割合の大きいほうを窓にする。

    位置を絶対行数だけで測ると、前置き（バッジ・説明）が長い良い README ほど
    落ちる。実測では高star リポの最初のコマンドは README の 6〜25% に現れ、
    低star 側は 28〜73% だった。効くのは割合であって行数ではない。
    ただし数十行の短い README では割合が意味を失うので、行数を下限に置く。
    """
    ratio = float(chk.get("within_ratio", 0))
    floor = int(chk.get("within_lines", 0))
    if not ratio and not floor:
        return text, "全体"
    lines = text.splitlines()
    window = max(int(len(lines) * ratio), floor) or len(lines)
    label = f"先頭 {window} 行（全 {len(lines)} 行）"
    return "\n".join(lines[:window]), label


def _is_commands(body: str) -> bool:
    """図ではなく、打てる行の集まりか。"""
    lines = [l for l in body.strip().splitlines() if l.strip()]
    if not lines:
        return False
    if any(DIAGRAM_CHARS & set(l) for l in lines):
        return False
    return True


def _voided(text: str, chk: dict) -> list[tuple[int, int]]:
    """`except_within` に当たった範囲。その中の一致は数えない。

    同じ文字列が、置かれた文脈によって別のものになることがある。ホームディレクトリの
    形は、web の URL の中に現れれば web のパスであって、誰かのホームではない。
    実測: 手元の 275 件のうち 27 件がこれで、2 リポジトリは誤検出だけで落ちていた。
    パターン側を複雑にすると読めなくなるので、**文脈を宣言する**形にした。
    """
    out = []
    for raw in chk.get("except_within", []):
        try:
            for m in re.finditer(str(raw), text):
                out.append(m.span())
        except re.error:
            continue
    return out


def _inside(span: tuple[int, int], voids: list[tuple[int, int]]) -> bool:
    return any(a <= span[0] and span[1] <= b for a, b in voids)


def _collect_hits(files: list[tuple[str, Path]], rx: re.Pattern,
                  limit: int, chk: dict | None = None) -> list[str]:
    hits: list[str] = []
    for rel, path in files:
        text = read_text(path)
        if text is None:
            continue
        voids = _voided(text, chk or {})
        for m in rx.finditer(text):
            if _inside(m.span(), voids):
                continue
            line = text.count("\n", 0, m.start()) + 1
            hits.append(f"{rel}:{line}")
            if len(hits) >= limit:
                return hits
    return hits


@dataclass(frozen=True)
class Match:
    """当たった1件。行番号ではなく **本文** を持つのが要点。

    判定の記録（audit）の鍵を行番号にすると、行が動いた瞬間に鍵が外れる。外れたこと
    は出力に現れないので、抑制だけが静かに消える。鍵を内容に結びつけるために本文を運ぶ。
    """
    rel: str
    line: int
    body: str

    def where(self) -> str:
        return f"{self.rel}:{self.line}"


def _scan_specs(files: list[tuple[str, Path]], specs: list[tuple[str, str]],
                literal: bool, chk: dict,
                collect_all: bool = False) -> list[tuple[str, int, list[Match]]]:
    """宣言されたパターンを全部当てて、**宣言ごとに** 件数と当たった場所を返す。

    1件目で打ち切らないことが要点である。打ち切ると「顧客Aの名前だけが在る」と
    「A と B と C が在る」が同じ出力になり、自分の顧客か他社かを仕分けられない
    （2026-09-26 に 33 本が全部「3 件以上該当 [clients.names]」と出て、実際に
    仕分けができなかった）。

    literal 側は宣言された語そのものを名前に含める。語は分類の根拠であって秘密では
    ない。正規表現側は含めない——マッチした文字列が秘密の値そのものになるため、
    ラベル（private_key 等）だけを出す。

    ファイルは1回しか読まない。宣言ごとに読み直すと宣言の数だけ I/O が増える。

    返す Match は行番号だけでなく **行の本文** を持つ。人が下した判定を記録するとき、
    鍵を行番号にすると行が動いた瞬間に静かに外れる（detect-secrets の baseline が
    この形で「カバレッジが黙って落ちる」という壊れ方をしている）。鍵は内容に結び
    つける必要があるので、内容をここから渡す。

    collect_all=False のときは宣言ごとに MAX_EXAMPLES 件で打ち切る。例示は場所を
    指すためのもので、全件は要らない。True にすると全件返す——判定の記録を突き合わ
    せる側は、1件でも取りこぼすとそこだけ抑制が効かないため全件が要る。
    """
    rxs: list[tuple[str, re.Pattern]] = []
    broken: list[tuple[str, int, list[Match]]] = []
    for label, raw in specs:
        try:
            rxs.append((f"{label}:{raw}" if literal else label,
                        re.compile(re.escape(raw) if literal else raw,
                                   re.I if literal else 0)))
        except re.error as exc:
            broken.append((f"<不正な正規表現 {label}: {exc}>", 1, []))
    counts: dict[str, int] = {}
    found: dict[str, list[Match]] = {}
    for rel, path in files:
        text = read_text(path)
        if text is None:
            continue
        lines = text.splitlines()
        voids = _voided(text, chk)
        for shown, rx in rxs:
            for m in rx.finditer(text):
                if _inside(m.span(), voids):
                    continue
                counts[shown] = counts.get(shown, 0) + 1
                seen = found.setdefault(shown, [])
                if collect_all or len(seen) < MAX_EXAMPLES:
                    no = text.count(chr(10), 0, m.start()) + 1
                    body = lines[no - 1] if no - 1 < len(lines) else ""
                    seen.append(Match(rel, no, body))
    hit = [(shown, counts[shown], found[shown]) for shown, _ in rxs
           if shown in counts]
    hit.sort(key=lambda t: -t[1])
    return broken + hit


def _apply_audit(root: Path, chk: dict,
                 hits: list[tuple[str, int, list[Match]]],
                 ) -> tuple[list[tuple[str, int, list[Match]]], list[str]]:
    """人が下した判定を当てて、残った当たりと、出すべき注記を返す。

    抑制した件数と記録の健全性は **必ず注記として返す**。黙って引くと、何を通した
    のかが出力から消える。それが baseline 方式の本来の壊れ方（rubber-stamp）である。
    """
    log = audit.load()
    repo = root.name
    kept: list[tuple[str, int, list[Match]]] = []
    dropped: dict[str, int] = {}
    live: set[tuple[str, str, str]] = set()
    for shown, _, ms in hits:
        left, n = log.filter(repo, shown, ms)
        for m in ms:
            live.add((repo, shown, audit.line_key(m.body)))
        if n:
            _, _, term = shown.partition(":")
            dropped[term or shown] = n
        if left:
            kept.append((shown, len(left), left))
    notes = []
    if dropped:
        notes.append("人の判定で抑制: " + "、".join(
            f"{t} {n} 件" for t, n in sorted(dropped.items(), key=lambda kv: -kv[1])))
    notes += audit.health(log, repo, live)
    return kept, notes


def obs_pattern_absent(root: Path, chk: dict, ctx: dict) -> tuple[str, str, list[str]]:
    files = _scoped(ctx["files"], chk.get("globs", ["**/*"]),
                    _except_globs(chk, ctx["lanes_cfg"]))
    if not files:
        return "pass", "対象ファイル無し", []
    specs: list[tuple[str, str]] = [("inline", p) for p in chk.get("patterns", [])]
    literal = False
    if chk.get("patterns_from"):
        loaded, is_rx = load_pattern_file(chk["patterns_from"],
                                          list(chk.get("categories", [])))
        specs += loaded
        literal = not is_rx
    if not specs:
        return "skip", "パターンが宣言されていない", []
    # 人の判定を受け付けるかは **観測ごとに config で宣言する**。既定は受け付けない。
    # 秘密の検査まで一律に抑制できる形にすると、抑制を宣言していない組織でも
    # 「誰かが cleared と書けば通る」経路が生まれる。auditable を engine の判断で
    # 決めないのは、何を人の判断に委ねるかが組織の決めることだからである。
    auditable = bool(chk.get("auditable"))
    # 全件要るのは抑制を1件ずつ突き合わせるときだけ。3858 件当たるリポもあるので、
    # 宣言していない観測で全件を持つのは無駄でしかない。
    hits = _scan_specs(files, specs, literal, chk, collect_all=auditable)
    notes: list[str] = []
    if auditable:
        hits, notes = _apply_audit(root, chk, hits)
    if not hits:
        if notes:
            # 全件が人の判定で消えたことを、最初から無かったことにはしない。
            return "pass", "人の判定で全件が抑制されています — " + "; ".join(notes), []
        return "pass", f"{len(files)} ファイルに該当なし", []
    total = sum(n for _, n, _ in hits)
    # ラベルが同じものはまとめる。clients.names: を12回繰り返すと、読むための
    # 出力として機能しない（仕分けはこの行を目で読んで進める）。
    groups: dict[str, list[str]] = {}
    for shown, n, _ in hits[:MAX_NAMED]:
        label, _, term = shown.partition(":")
        groups.setdefault(label, []).append(f"{term} {n} 件" if term else f"{n} 件")
    named = " / ".join(f"{k}: " + "、".join(v) for k, v in groups.items())
    more = f" / ほか {len(hits) - MAX_NAMED} 種" if len(hits) > MAX_NAMED else ""
    # 例示は宣言ごとに1件ずつ取る。同じ語の3件を並べると、2種目以降が在ることが
    # 出力から消える（それが 2026-09-26 に 33 本を仕分けられなかった原因である）。
    examples = [ms[0].where() for _, _, ms in hits[:MAX_NAMED] if ms]
    # 抑制と記録の健全性は例示ではなく detail に出す。例示は場所を指すだけなので
    # 目で飛ばされるが、「何件を人の判定で引いたか」は判定の一部である。
    tail = ("  ／ " + "; ".join(notes)) if notes else ""
    return "fail", f"{total} 件該当 — {named}{more}{tail}", examples


def obs_pattern_max(root: Path, chk: dict, ctx: dict) -> tuple[str, str, list[str]]:
    files = _scoped(ctx["files"], chk.get("globs", ["**/*"]),
                    _except_globs(chk, ctx["lanes_cfg"]))
    if not files:
        return "pass", "対象ファイル無し", []
    try:
        rx = re.compile(chk["pattern"])
    except (KeyError, re.error) as exc:
        return "skip", f"パターンが不正: {exc}", []
    total, examples = 0, []
    for rel, path in files:
        text = read_text(path)
        if text is None:
            continue
        text, _ = _window(text, chk)
        voids = _voided(text, chk)
        for m in rx.finditer(text):
            if _inside(m.span(), voids):
                continue
            total += 1
            if len(examples) < MAX_EXAMPLES:
                examples.append(f"{rel}:{text.count(chr(10), 0, m.start()) + 1}")
    cap = int(chk.get("max", 0))
    if total <= cap:
        return "pass", f"{total} 件（上限 {cap}）", []
    return "fail", f"{total} 件（上限 {cap}）", examples


def obs_pattern_present(root: Path, chk: dict, ctx: dict) -> tuple[str, str, list[str]]:
    """patterns のいずれかが在ること。無いことを言う観測の裏返し。

    安全は「無いこと」で測るが、能力は「在ること」でしか測れない。
    """
    files = _scoped(ctx["files"], chk.get("globs", ["**/*"]),
                    _except_globs(chk, ctx["lanes_cfg"]))
    if not files:
        return "fail", "対象ファイルが無い", []
    for raw in chk.get("patterns", []):
        try:
            rx = re.compile(raw, re.I)
        except re.error as exc:
            return "skip", f"パターンが不正: {exc}", []
        hits = _collect_hits(files, rx, 1)
        if hits:
            return "pass", f"該当あり（{hits[0]}）", []
    return "fail", "いずれのパターンも見つからない", []


def obs_declared_command(root: Path, chk: dict, ctx: dict) -> tuple[str, str, list[str]]:
    name = chk.get("name", "")
    if not ctx.get("declared", True):
        return "skip", "[publish] 未宣言のため裁かない（校正モード）", []
    cmd = ctx["commands"].get(name, "")
    if str(cmd).strip():
        return "pass", f"{name} = {cmd}", []
    return "fail", f"work.toml の [publish.commands] に {name} が無い", []


def obs_command_exit_zero(root: Path, chk: dict, ctx: dict) -> tuple[str, str, list[str]]:
    name = chk.get("name", "")
    if not ctx.get("declared", True):
        return "skip", "[publish] 未宣言のため裁かない（校正モード）", []
    cmd = str(ctx["commands"].get(name, "")).strip()
    if not cmd:
        if chk.get("optional"):
            # 宣言が在れば走らせる観測。無いこと自体は欠陥ではない。
            # e2e を持たない形のリポは在るので、fail にすると当たらないはずの
            # リポを毎回叱ることになる。無いものは「見ていない」と言う。
            return "skip", f"[publish.commands] に {name} の宣言なし", []
        return "fail", f"[publish.commands] に {name} が無い", []
    if not ctx["execute"]:
        return "skip", f"未実行（--execute で実走）: {cmd}", []
    # 累積の梯子なので、同じ観測が第1段と第2段の両方で当たる。同じコマンドを
    # 2度走らせても分かることは増えない一方、pnpm e2e のような宣言では実測で
    # 倍の時間がかかる。段が違っても、走った事実は1つである。
    cached = ctx["ran"].get(cmd)
    if cached is not None:
        return cached
    try:
        proc = subprocess.run(cmd, cwd=str(root), shell=True, capture_output=True,
                              text=True, timeout=int(ctx["timeout"]))
    except subprocess.TimeoutExpired:
        out = ("fail", f"{ctx['timeout']}秒で終わらない: {cmd}", [])
    except OSError as exc:
        out = ("fail", f"起動できない: {exc}", [])
    else:
        if proc.returncode == 0:
            out = ("pass", f"exit 0: {cmd}", [])
        else:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-MAX_EXAMPLES:]
            out = ("fail", f"exit {proc.returncode}: {cmd}", tail)
    ctx["ran"][cmd] = out
    return out


def obs_exclude_not_tracked(root: Path, chk: dict, ctx: dict) -> tuple[str, str, list[str]]:
    """`[publish] exclude` に宣言したパスが、git 追跡下に残っていないか。

    `exclude` が言えるのは「観測しない」だけで、「公開物に入れない」ではない。
    git は work.toml の宣言を知らないので、追跡されていれば公開の瞬間に一緒に出る。
    宣言を信じて観測を止める口は、宣言が実態から外れた瞬間に偽陰性になる。

    実測: work-os 自身が `exclude = ["registry/"]` と宣言しながら registry/ を18ファイル
    追跡しており、顧客名276件が全レーンの観測から外れたまま「public 可」と出ていた。

    これは *列挙* ではなく **宣言と実態の突き合わせ**なので、当て推量にならない。
    宣言した本人しか知らない事実を、その本人の宣言と照合しているだけである。
    """
    excluded = [e for e in (ctx.get("exclude") or []) if str(e).strip()]
    if not excluded:
        return "pass", "exclude の宣言なし", []
    tracked = [rel for rel, _ in tracked_files(root, [])]
    hits, examples = [], []
    for e in excluded:
        pref = str(e).rstrip("/")
        under = [r for r in tracked if r == pref or r.startswith(pref + "/")]
        if under:
            hits.append(f"{e} は追跡下（{len(under)} ファイル）")
            examples.extend(under[:3])
    if not hits:
        return "pass", f"{len(excluded)} 件の宣言はすべて追跡外", []
    return "fail", "; ".join(hits), examples


# --------------------------------------------------------------------------- #
# 履歴
#
# ここまでの観測はすべて **作業ツリー** を見ている（git ls-files）。だから
# 「今の木から消したもの」は、履歴に残っていても全レーンを素通りする。
# 実測 2026-08-31: work-os から registry/（顧客名276件）を消したコミットを積んだ
# 時点で、過去版の README・engine/knowhow.py・コミットメッセージ3件に顧客名が
# 7箇所残ったまま、第2段は OK と出た。宣言のほうには「第2段が通らない理由は
# 履歴だ」と書いてあった。判定する側が、その理由を見る軸を持っていなかった。
#
# 検査は2つに分かれる。**形**（消してあるか）と **走査**（残っていないか）。
# 走査は列挙であり、private_terms.toml に書いた語しか見つけられない。書き忘れた
# 1社は素通りする。形は中身に依存しないので、その弱点が無い。だから止めるのは
# 形のほうで、走査は報告に留める。
# --------------------------------------------------------------------------- #

def _git_out(root: Path, args: list[str], timeout: int = 60) -> str | None:
    """git の出力。リポジトリでない・失敗したときは None。"""
    try:
        r = subprocess.run(["git", "-C", str(root), *args],
                           capture_output=True, text=True, timeout=timeout,
                           errors="replace")
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def obs_history_erased(root: Path, chk: dict, ctx: dict) -> tuple[str, str, list[str]]:
    """公開の起点より前の履歴が残っていないか。

    **公開前**: 通るのは履歴が1コミットのときだけである。「宣言した起点と root が
    一致する」形も考えたが、それは今ある root を宣言すれば必ず通るので、何も
    確かめていない。消したことの証明を与えるのは squash だけなので逃げ道を作らない。

    **公開後**: この問いは寿命を終える。出せない過去は公開の時点でもう消して
    あり（起点より前が存在しない）、以後は消しようがない。にもかかわらず同じ
    観測が当たり続けると、公開の次の1コミットで必ず落ちる。docstring がそれを
    予告したまま放置されていた。

    そこで `[publish] released_at` に**公開した起点の sha** を宣言できるようにした。
    宣言があるときは問いが「起点より前が無いか」に変わる。単一 root で、その root が
    宣言された起点と一致し、HEAD から辿れることを見る。

    **これは観測ではなく宣言である。**「本当に公開したのか」を手元から確かめる
    方法は無い（private と public の remote は区別できない）。宣言すれば squash を
    迂回できる。その代わり、宣言は「この sha から公開している」と書き残す行為に
    なり、嘘なら履歴に残る。そして起点より後は history レーンの別の観測が中身で
    見る（no_private_terms_since_release）。消したことの証明は squash だけ、
    以後を保つのは中身の観測、という分担にした。
    """
    count = _git_out(root, ["rev-list", "--count", "HEAD"])
    if count is None:
        return "skip", "git リポジトリではない（履歴が無いので確かめられない）", []
    n = int(count.strip() or 0)
    roots = [ln for ln in (_git_out(root, ["rev-list", "--max-parents=0", "HEAD"]) or "").split() if ln]
    if len(roots) > 1:
        return "fail", f"root コミットが {len(roots)} 個ある（別の履歴が接がれている）", roots[:5]

    released = str(ctx.get("released_at") or "").strip()
    if released:
        full = _git_out(root, ["rev-parse", "--verify", f"{released}^{{commit}}"])
        if full is None:
            return "fail", f"[publish] released_at のコミットが無い: {released}", []
        full = full.strip()
        if not roots or roots[0] != full:
            head = roots[0][:12] if roots else "(不明)"
            return "fail", (f"[publish] released_at は {released} だが、root は {head}。"
                            f"公開の起点より前が残っている"), roots[:5]
        ahead = _git_out(root, ["rev-list", "--count", f"{full}..HEAD"])
        return "pass", (f"公開の起点 {full[:12]} が root（以後 {int((ahead or '0').strip() or 0)} "
                        f"コミット）。起点より前は無い"), []

    if n <= 1:
        return "pass", "履歴は1コミット（公開の起点より前が無い）", []
    oldest = _git_out(root, ["log", "--reverse", "--format=%h %ad %s", "--date=short", "HEAD"])
    first = (oldest or "").splitlines()[:1]
    return "fail", f"履歴が {n} コミットある。公開の起点より前が残っている", first


def obs_history_pattern_absent(root: Path, chk: dict,
                               ctx: dict) -> tuple[str, str, list[str]]:
    """履歴の中身（blob ＋ コミットメッセージ）にパターンが無いか。

    走査できるのは宣言した語だけなので、これは「無い」の証明ではない。
    見つかったときにだけ意味がある観測である。

    `since = "released_at"` を宣言すると、公開の起点より後だけを見る。公開前は
    squash が「無い」の証明を与えるが、公開後の分は消せないので、中身で見るしか
    ない。範囲を切らずに全履歴へ当てると、公開した木そのものを毎回走査し直す
    ことになり、直せない過去で毎回落ちる。
    """
    specs: list[tuple[str, str]] = [("inline", p) for p in chk.get("patterns", [])]
    literal = False
    if chk.get("patterns_from"):
        loaded, is_rx = load_pattern_file(chk["patterns_from"],
                                          list(chk.get("categories", [])))
        specs += loaded
        literal = not is_rx
    if not specs:
        return "skip", "パターンが宣言されていない", []

    # 走査範囲。既定は全履歴。since = "released_at" のときは公開の起点より後だけを
    # 見る。起点そのもの（公開した木）は公開時に一度見ており、以後は変えられない。
    # 見るべきは「公開した後に積んだもの」で、そこは毎回変わる。
    scope, since = ["--all"], ""
    if chk.get("since") == "released_at":
        since = str(ctx.get("released_at") or "").strip()
        if not since:
            return "skip", "[publish] released_at の宣言なし（まだ公開していない）", []
        head = _git_out(root, ["rev-parse", "--verify", f"{since}^{{commit}}"])
        if head is None:
            return "skip", f"[publish] released_at のコミットが無い: {since}", []
        scope = [f"{head.strip()}..HEAD"]

    listing = _git_out(root, ["rev-list", "--objects", *scope], timeout=120)
    if listing is None:
        return "skip", "git リポジトリではない", []
    names: dict[str, str] = {}
    for line in listing.splitlines():
        parts = line.split(maxsplit=1)
        if parts:
            names[parts[0]] = parts[1] if len(parts) == 2 else "<commit/tree>"
    if not names:
        return "pass", ("公開の起点より後にコミットが無い" if since else "履歴が空"), []

    try:
        batch = subprocess.run(["git", "-C", str(root), "cat-file", "--batch"],
                               input="\n".join(names), capture_output=True,
                               text=True, timeout=300, errors="replace").stdout
    except (OSError, subprocess.SubprocessError):
        return "skip", "履歴を読めなかった", []
    messages = _git_out(root, ["log", *scope, "--format=%H%n%B"], timeout=120) or ""

    hits: list[str] = []
    for label, raw in specs:
        pat = re.escape(raw) if literal else raw
        try:
            rx = re.compile(pat, re.I if literal else 0)
        except re.error as exc:
            hits.append(f"<不正な正規表現 {label}: {exc}>")
            continue
        if rx.search(batch):
            hits.append(f"履歴のファイルに該当 [{label}]")
        if rx.search(messages):
            hits.append(f"コミットメッセージに該当 [{label}]")
        if len(hits) >= MAX_EXAMPLES:
            break
    where = "公開の起点より後の " if since else ""
    if hits:
        return "fail", f"{len(hits)} 件該当（{where}{len(names)} オブジェクト走査）", hits[:MAX_EXAMPLES]
    return "pass", f"{where}{len(names)} オブジェクトとコミットメッセージに該当なし", []


OBSERVERS = {
    "exclude_not_tracked": obs_exclude_not_tracked,
    "pattern_present": obs_pattern_present,
    "file_present": obs_file_present,
    "doc_section": obs_doc_section,
    "runnable_block": obs_runnable_block,
    "pattern_absent": obs_pattern_absent,
    "pattern_max": obs_pattern_max,
    "declared_command": obs_declared_command,
    "command_exit_zero": obs_command_exit_zero,
    "history_erased": obs_history_erased,
    "history_pattern_absent": obs_history_pattern_absent,
}


# --------------------------------------------------------------------------- #
# 判定
# --------------------------------------------------------------------------- #

def evaluate(root: Path, lanes_cfg: dict, execute: bool = False,
             timeout: int = 300, assume_public: bool = False,
             assume_shape: str = "", owned: bool = False) -> RepoResult | None:
    root = Path(root).resolve()
    pub = load_publish(root)
    intent = str(pub.get("intent", "")).strip()
    gate_cfg = lanes_cfg.get("gate", {})
    targets = gate_cfg.get("target_intents", ["public"])
    declared = bool(pub)
    source = "declared"
    owned_note = ""
    if intent not in targets:
        # 校正モード: 他所のリポを同じ物差しに当てて、この物差し自体を疑う。
        # 宣言が無いものは宣言に依存する観測を裁かない（見えないものは採点しない）。
        if assume_public:
            pub, intent, declared, source = {"intent": "public"}, "public", False, "assumed"
            if assume_shape:
                pub["shape"] = assume_shape
        else:
            # 宣言が無いリポに当てる段を registry が宣言していれば、そこに載せる。
            # 載せない場合の実際の挙動は「何も言わずに exit 0」だった。沈黙と通過が
            # 見分けられないので、82 本のうち 77 本について、ゲートは検査したのか
            # していないのかを答えていなかった（2026-09-26 実測）。
            # 既定は engine では決めない。どの段を既定にするかは組織の判断である。
            #
            # 当てるのは **何も宣言していないリポだけ**。intent = "private" のように
            # 宣言があって targets に無いものは、本人が「出さない」と言っている。
            # そこへ既定を当てると、明示した宣言を既定が上書きすることになる。
            default_intent = str(gate_cfg.get("default_intent", "")).strip()
            if intent or default_intent not in targets:
                return None
            pub, intent, declared, source = ({"intent": default_intent},
                                             default_intent, False, "default")

    owned = owned or declared
    if source == "default" and not owned:
        # 自組織のものかは、ここで新しく列挙しない。統治クラスの宣言
        # （fleet_policy.toml）が既に「既定は owned、例外だけ名指し」の形で在る。
        # 当たった宣言を出す。出さないと、外部のフォークを自組織として扱ったのか
        # どうかが出力から消える。
        klass, note = _fleet_class(root.name)
        if klass == "owned":
            owned = True
            owned_note = f"統治クラス owned（{note or '例外の宣言なし'}）"
        else:
            owned_note = f"統治クラス {klass} — 自組織の観測は当てない（{note}）"
    ctx = {
        "files": tracked_files(root, list(pub.get("exclude", []))),
        "exclude": list(pub.get("exclude", [])),
        "commands": dict(pub.get("commands", {})),
        "execute": execute,
        "timeout": timeout,
        # 実行済みコマンドの結果。段をまたいで再利用する（同上）。
        "ran": {},
        "declared": declared,
        "owned": owned,
        "lanes_cfg": lanes_cfg,
        # 公開した起点。宣言があれば history レーンの問いが「消したか」から
        # 「以後を保てているか」へ変わる（obs_history_erased の docstring）。
        "released_at": str(pub.get("released_at", "")).strip(),
    }
    result = RepoResult(name=root.name, root=root, intent=intent,
                       intent_source=source, owned_note=owned_note,
                        enforcement=str(pub.get("_enforcement", "warn")),
                        declared=declared)

    shape = str(pub.get("shape", "")).strip()
    stage_names = list(lanes_cfg.get("gate", {}).get("stages", []))
    # intent が段の名前なら、そこまでで止める。internal を目指すリポに
    # public の作法（LICENSE / CONTRIBUTING）を要求しない。
    if intent in stage_names:
        stage_names = stage_names[:stage_names.index(intent) + 1]
    for stage_name in stage_names:
        st_cfg = lanes_cfg.get("stages", {}).get(stage_name, {})
        stage = StageResult(name=stage_name, title=st_cfg.get("title", stage_name),
                            question=st_cfg.get("question", ""))
        result.stages.append(stage)
        for lane_name in st_cfg.get("lanes", []):
            cfg = lanes_cfg.get("lanes", {}).get(lane_name)
            if not cfg:
                continue
            shapes = cfg.get("applies_to_shapes")
            if shapes and shape not in shapes:
                # 形が違えば当てない。ライブラリに登録動線が無いのは欠陥ではない。
                continue
            lane = _run_lane(lane_name, cfg, root, ctx, stage_name)
            stage.lanes.append(lane)
    return result


def check_severity(chk: dict, stage_name: str) -> str:
    """同じ観測でも、段が上がれば結果が変わってよい。

    顧客名は同僚には配りたいが、public には出せない。観測は1つで、consequence が
    段によって違う。severity に段名のテーブルを書けるようにして、この差を表す。

        severity = "block"                              どの段でも止める
        severity = { internal = "warn", public = "block" }   段で変える

    宣言の無い段は warn に倒す。新しい段を足したときに、黙って block が増えない。
    """
    sev = chk.get("severity", "warn")
    if isinstance(sev, dict):
        return str(sev.get(stage_name, "warn"))
    return str(sev)


def _run_lane(lane_name: str, cfg: dict, root: Path, ctx: dict,
              stage_name: str = "") -> LaneResult:
    owned = ctx["owned"]
    lane = LaneResult(name=lane_name, title=cfg.get("title", lane_name),
                      question=cfg.get("question", ""))
    for chk in cfg.get("checks", []):
        kind = chk.get("kind", "")
        if chk.get("only_when_owned") and not owned:
            lane.findings.append(Finding(
                chk.get("id", "?"), kind, check_severity(chk, stage_name), "skip",
                str(chk.get("why", "")), "自組織のリポにのみ当てる観測（--owned で主張する）"))
            continue
        observer = OBSERVERS.get(kind)
        if observer is None:
            lane.findings.append(Finding(
                chk.get("id", "?"), kind, "warn", "skip",
                "engine が知らない観測の種類", f"kind={kind}"))
            continue
        state, detail, examples = observer(root, chk, ctx)
        # 案内文が数えるのはここ。宣言の無いコマンドは --execute を付けても
        # 走らないので、未実走としては数えない（付ければ走ると言えなくなる）。
        pending = (kind == "command_exit_zero" and state == "skip"
                   and not ctx["execute"]
                   and bool(str(ctx["commands"].get(chk.get("name", ""), "")).strip()))
        lane.findings.append(Finding(
            check_id=chk.get("id", "?"), kind=kind,
            severity=check_severity(chk, stage_name), state=state,
            why=str(chk.get("why", "")), detail=detail, examples=examples,
            pending=pending))
    return lane


# --------------------------------------------------------------------------- #
# 出力
# --------------------------------------------------------------------------- #

MARK = {"pass": "OK  ", "fail": "NG  ", "skip": "--  "}


REACHED = {"": "どこにも出せない", "internal": "社内で共有できる（外販物に同梱できる）",
           "public": "public リポジトリとして切り出せる"}


def render(result: RepoResult, verbose: bool = False) -> None:
    # 出どころを黙ると、既定で載せた段が「本人が宣言した」と読まれる。
    # 段に載せたことと、本人が宣言したことは別の事実なので、必ず並べて出す。
    note = {"declared": "",
            "default": "（未宣言。判定基準の既定を当てた）",
            "assumed": "（未宣言。校正モードで public と仮定）"}.get(
                result.intent_source, "")
    print(f"\n[{result.name}]  intent={result.intent}{note}"
          f"  → {REACHED.get(result.reached, result.reached)}")
    if result.owned_note:
        print(f"  {result.owned_note}")
    for stage in result.stages:
        mark = "NG" if stage.blocked else ("保留" if stage.unproven else "OK")
        print(f"  ── {mark} 第{result.stages.index(stage) + 1}段 {stage.name}: {stage.title}")
        for lane in stage.lanes:
            head = ("NG" if lane.blocked else "保留" if lane.unproven
                    else "WARN" if lane.warnings else "OK")
            extra = f"  warn {lane.warnings}" if lane.warnings else ""
            extra += f"  skip {lane.skipped}" if lane.skipped else ""
            print(f"     {head:<4} {lane.name:<12} {lane.title}{extra}")
            for f in lane.findings:
                if f.state == "pass" and not verbose:
                    continue
                print(f"          {MARK[f.state]}{f.check_id}: {f.detail}")
                if f.state == "fail":
                    print(f"              なぜ: {f.why}")
                for ex in f.examples:
                    print(f"              - {ex}")
        if stage.blocked:
            # 累積の梯子なので、落ちた段より上は見るまでもない。
            blocked = [l.name for l in stage.lanes if l.blocked]
            print(f"     → この段で止まる。欠けているレーン: {', '.join(blocked)}")
            break
        if stage.unproven:
            ids = ", ".join(f.check_id for f in stage.unproven)
            print(f"     → この段は確かめていない。走らせていない観測: {ids}"
                  f"（--execute で実走）")
            break


def to_dict(result: RepoResult) -> dict:
    return {
        "repo": result.name,
        "intent": result.intent,
        # 段に載せた事実と、本人が宣言した事実を機械が読む側でも分けて持つ。
        # 人が読む出力だけで区別できても、JSON を集計する側が混ぜる。
        "intent_source": result.intent_source,
        "owned_note": result.owned_note,
        "reached": result.reached,
        "blocked_at": result.blocked_at,
        "unproven_at": result.unproven_at,
        "publishable": result.publishable,
        "stages": [{"name": st.name, "blocked": st.blocked,
                    "unproven": [f.check_id for f in st.unproven],
                    "lanes": [l.name for l in st.lanes]} for st in result.stages],
        "lanes": [
            {"name": l.name, "blocked": l.blocked, "warnings": l.warnings,
             "findings": [
                 {"id": f.check_id, "kind": f.kind, "severity": f.severity,
                  "state": f.state, "detail": f.detail, "examples": f.examples}
                 for f in l.findings]}
            for l in result.lanes
        ],
    }


def discover(scan_root: Path, assume_public: bool = False) -> list[Path]:
    """観測対象。列挙そのものは kernel の定義に従う。

    既定は「宣言のあるリポジトリ」。宣言していないリポを裁かないのは意図的で、
    ゲートは additive に入れるものだから（宣言が opt-in の意思表示）。

    ただし --assume-public は「他所のリポを同じ物差しに当てて、この物差し自体を
    疑う」ための校正モードなので、宣言でふるうと目的を果たせない。2026-09-02 実測:
    82 リポ中 8 本しか見えておらず、校正の母数が宣言済みのリポだけだった。
    物差しを疑うのに、その物差しを既に受け入れた側だけを見ていた。
    """
    repos = iter_repo_dirs(scan_root)
    if assume_public:
        return repos
    return [p for p in repos if (p / "work.toml").is_file()]


def unmeasured(scan_root: Path, measured: list[RepoResult]) -> list[str]:
    """歩いたが観測しなかったリポ。

    見ていない範囲を黙っていると、「宣言した5本が OK」と「フリートが OK」が
    同じ顔で出る。数えていないものは、問題が無いことの証拠にならない。
    """
    seen = {r.root.resolve() for r in measured}
    return sorted(p.name for p in iter_repo_dirs(scan_root) if p.resolve() not in seen)


def require_stage(results: list[RepoResult], required: str, lanes_cfg: dict) -> int:
    """到達すべき段を呼び出し側が宣言し、届かなければ 2 を返す。

    既定の終了コードは `enforcement` に従う。宣言が warn のリポでは、レーンが
    落ちていても 0 が返る。それはそれで正しい（宣言どおりに扱っている）が、
    ローカルゲートから呼ぶと「走らせるが結果を無視する」ゲートになる。実測:
    work-os は enforcement="warn" なので、第2段で止まっていても exit 0 だった。

    かといって「最後の段まで通ること」を求めると、構造的に届かないリポで
    永久に差し戻し続けることになる（work-os の第2段は registry/ の顧客名が
    初回コミットから履歴に入っており、切り出しと履歴の書き換えが要る）。

    なので **どこまで届いていれば良いかを呼ぶ側が言う**。現在地を宣言して、
    そこから落ちたときだけ差し戻す。上の段に届かないことは差し戻しではない。

    終了コードは Stop hook の規約に合わせる（0 = PASS / 2 = 差し戻し）。
    """
    stages = list(lanes_cfg.get("gate", {}).get("stages", []))
    if required not in stages:
        print(f"--require に知らない段: {required}"
              f"（{', '.join(stages)} のいずれか）", file=sys.stderr)
        return 2

    need = stages.index(required)
    short = []
    for r in results:
        # 評価されなかった段には、届きようがない。intent がその段より下なら
        # 「届いていない」ではなく「測っていない」なので、そう言って落とす。
        measured = [st.name for st in r.stages]
        reached = stages.index(r.reached) if r.reached in stages else -1
        if required not in measured:
            short.append((r, f"intent={r.intent} なのでこの段は測っていない"))
        elif reached < need:
            if r.unproven_at:
                # 落ちたのではない。走らせていないので結論が出ていない。
                # 同じ「届いていない」でも、直し方が違う（欠陥を直す / 走らせる）。
                ids = ", ".join(f.check_id for st in r.stages for f in st.unproven)
                short.append((r, f"到達 = {REACHED.get(r.reached, r.reached)}"
                                 f" / {r.unproven_at} を確かめていない: {ids}"
                                 f"（--execute で実走）"))
            else:
                short.append((r, f"到達 = {REACHED.get(r.reached, r.reached)}"
                                 f" / 落ちた段 = {r.blocked_at or '-'}"))

    for r, why in short:
        print(f"\n[{r.name}] 必須の段 {required} に届いていない: {why}", file=sys.stderr)
    if short:
        return 2
    print(f"\n必須の段 {required} に {len(results)} リポとも到達")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="公開に耐えるかを観測する")
    ap.add_argument("repos", nargs="*", default=["."], help="検査するリポジトリ")
    ap.add_argument("--scan", metavar="ROOT", help="配下で公開を宣言した全リポを検査")
    ap.add_argument("--execute", action="store_true",
                    help="宣言されたコマンドを実際に走らせる（既定は静的観測のみ）")
    ap.add_argument("--timeout", type=int, default=300, help="実行1件あたりの上限秒")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--verbose", "-v", action="store_true", help="通った観測も出す")
    ap.add_argument("--assume-public", action="store_true",
                    help="[publish] 未宣言のリポも公開前提で採点する（物差しの校正用）")
    ap.add_argument("--assume-shape", default="",
                    help="校正時にリポジトリの形を仮定する（service / tool / library）")
    ap.add_argument("--owned", action="store_true",
                    help="検査対象が自組織のリポであると主張する"
                         "（顧客名・絶対パスの観測はこれが無いと当たらない）")
    ap.add_argument("--require", metavar="STAGE",
                    help="この段に届かなければ exit 2（ローカルゲートから呼ぶための口）")
    args = ap.parse_args(argv)

    lanes_cfg = load_lanes()
    roots = (discover(Path(args.scan), args.assume_public) if args.scan
             else [Path(r) for r in args.repos])

    # --scan は自分の木を歩くので、そこに在るものは自組織のリポである。
    owned = args.owned or bool(args.scan)
    results = [r for r in (evaluate(root, lanes_cfg, args.execute, args.timeout,
                                    args.assume_public, args.assume_shape, owned)
                           for root in roots) if r is not None]

    if args.json:
        print(json.dumps([to_dict(r) for r in results], ensure_ascii=False, indent=2))
    else:
        if not results:
            print("公開を宣言したリポジトリがありません"
                  "（work.toml に [publish] intent = \"public\" を書く）")
        for r in results:
            render(r, args.verbose)
        pending = sum(r.pending_executions for r in results)
        if pending and not args.execute:
            print(f"\n実行を伴う観測が {pending} 件、未実走のまま残っています。"
                  "--execute で実際に走らせます。")
        if args.scan:
            rest = unmeasured(Path(args.scan), results)
            if rest:
                head = "、".join(rest[:MAX_EXAMPLES])
                more = f" ほか {len(rest) - MAX_EXAMPLES} 本" if len(rest) > MAX_EXAMPLES else ""
                print(f"\n観測した {len(results)} 本のほかに、宣言が無いため見ていない"
                      f"リポジトリが {len(rest)} 本あります（{head}{more}）。"
                      "\n見ていないことは、問題が無いことではありません。"
                      "\n物差しだけ当てるなら --assume-public、宣言を生やすなら "
                      "engine/adopt.py <repo> --write。")

    if args.require:
        return require_stage(results, args.require, lanes_cfg)

    failed = [r for r in results if not r.publishable and r.enforcement == "block"]
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
