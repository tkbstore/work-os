#!/usr/bin/env python3
"""release_gate が「落とすべきものを落とし、通すべきものを通す」ことを検査する。

公開ゲートには固有の罠がある。**ゲートは自分自身のテストを読む**。
欠陥の見本（秘密鍵の形、裸の except、固有名詞、絶対パス）をこのファイルに直接書くと、
work-os 自身の safety / provenance / robustness レーンがそれを検出して落ちる。

だから見本は必ず実行時に組み立てる。連結・ディスクからの借用で、
このファイルの本文にはどの形も現れないようにしてある。同じ理由で、
固有名詞は registry から、ホームパスは実環境から借りる（test_guards と同じ作法）。

  python3 tests/test_release_gate.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _registry import require  # noqa: E402

require("release_lanes.toml", "secret_patterns.toml", "private_terms.toml")
GATE = ROOT / "engine" / "release_gate.py"
sys.path.insert(0, str(ROOT / "engine"))
from release_gate import obs_history_pattern_absent  # noqa: E402

sys.path.insert(0, str(ROOT / "engine"))
# E402（import が先頭に無い）は、上で engine/ を sys.path に挿してから import する
# ためで、抑制しているのは順序の指摘だけである。動かない理由を隠してはいない。
from release_gate import _match_glob, check_severity  # noqa: E402
from workos import _load_toml, registry_root  # noqa: E402


# --------------------------------------------------------------------------- #
# 欠陥の見本を実行時に組み立てる（本文に現れさせないため）
# --------------------------------------------------------------------------- #

def a_private_term(group: str = "clients") -> str:
    """registry が「持ち出してはいけない」と宣言している語を1つ借りる。

    provenance が見るのは [clients] だけになった。自社製品名（products）や
    自分の名前（org）は、そのリポにとっては書いてあって当然のものなので、
    公開判定では裁かない。あちらは work-os 自身の抽象度ゲートの管轄。
    """
    terms = registry_root() / "private_terms.toml"
    if not terms.is_file():
        # 落ちること自体は正しい（検査していないものを通さない）が、
        # FileNotFoundError だけでは何が足りないのか読めない。registry を
        # 別リポへ切り出した後、これが最初に踏まれる場所になった。
        raise SystemExit(
            f"検査語の宣言が見つかりません: {terms}\n"
            f"  registry の場所を教えてください（WORKOS_REGISTRY=<path>）")
    data = _load_toml(terms)
    body = data.get(group, {})
    for val in (body.values() if isinstance(body, dict) else [body]):
        for term in (val if isinstance(val, list) else [val]):
            t = str(term).strip()
            if len(t) >= 4 and t.isalnum():
                return t
    return "placeholderterm"


def some_private_terms(count: int, group: str = "clients") -> list[str]:
    """registry が禁じている語を count 個借りる。

    1語では「どの語が当たったか」を出しているかが分からない。語を捨てる実装でも、
    語が1つしか在らない木では「当たった」ことだけは正しく出るためである。
    2語以上を入れて、両方が名指しされることを見る必要がある。
    """
    terms = registry_root() / "private_terms.toml"
    if not terms.is_file():
        raise SystemExit(f"検査語の宣言が見つかりません: {terms}")
    body = _load_toml(terms).get(group, {})
    out: list[str] = []
    for val in (body.values() if isinstance(body, dict) else [body]):
        for term in (val if isinstance(val, list) else [val]):
            t = str(term).strip()
            if len(t) >= 4 and t.isalnum() and t not in out:
                out.append(t)
            if len(out) == count:
                return out
    raise SystemExit(f"[{group}] に {count} 語の借りられる宣言がありません")


FAKE_KEY = "AKIA" + "IOSFODNN7EXAMPLE"          # 形だけの AWS アクセスキー
BARE_EXCEPT = "    " + "except" + ":\n        " + "pass\n"
HOME_PATH = str(Path.home()) + "/somewhere/"


def defective_source() -> str:
    return (
        f'KEY = "{FAKE_KEY}"\n'
        f'PATH = "{HOME_PATH}"\n'
        f'ORG = "{a_private_term()}"\n'
        "def f():\n    try:\n        pass\n" + BARE_EXCEPT
    )


HEALTHY_README = (
    "# sample\n\n"
    "## Install\n\nPython 3.10 以上が要ります。\n\n"
    "```sh\npython3 -m pip install -e .\n```\n\n"
    "## Usage\n\n```sh\npython3 -m sample --help\n```\n"
)


# --------------------------------------------------------------------------- #
# 器
# --------------------------------------------------------------------------- #

# 仕掛けのリポでも commit する。history レーンは履歴を見るので、add しただけの
# 木では HEAD が無く、観測が skip になる。skip は「落ちない」なので、レーンを
# 足したのに何も確かめていない状態になる（実際 2026-08-31 にこれで1往復した）。
GIT_ENV = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}


# メッセージは Conventional Commits に従う。init.templateDir 経由で commit-msg
# hook が仕掛けのリポにも入るため、従わないと commit が落ちる。--no-verify で
# 迂回すると、ガードを外した状態でしか通らないテストになる。
def commit(repo: Path, message: str) -> None:
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, timeout=60)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", message],
                   check=True, timeout=60, env=GIT_ENV)


def build(tmp: Path, files: dict[str, str]) -> Path:
    repo = tmp
    for rel, body in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(repo)], check=True, timeout=30)
    # 健全リポは1コミット。history_erased が通るのは公開の起点だけを持つ木である。
    commit(repo, "chore: first")
    return repo


def run(repo: Path, *args: str) -> tuple[int, list[dict]]:
    proc = subprocess.run([sys.executable, str(GATE), str(repo), "--json", *args],
                          capture_output=True, text=True, timeout=180)
    try:
        return proc.returncode, json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return proc.returncode, []


def run_text(repo: Path, *args: str) -> str:
    """--json を付けずに走らせる。人間が読む案内文はここにしか出ない。"""
    proc = subprocess.run([sys.executable, str(GATE), str(repo), *args],
                          capture_output=True, text=True, timeout=180)
    return proc.stdout


def run_scan(scan_root: Path, *args: str) -> str:
    """--scan で走らせて、人間が読む出力を返す。"""
    proc = subprocess.run([sys.executable, str(GATE), "--scan", str(scan_root), *args],
                          capture_output=True, text=True, timeout=300)
    return proc.stdout


def states(results: list[dict]) -> dict[str, str]:
    out = {}
    for r in results:
        for lane in r["lanes"]:
            for f in lane["findings"]:
                out[f["id"]] = f["state"]
    return out


DEFECTIVE_FILES = {
    "work.toml": '[repo]\nname = "sample"\nenforcement = "block"\n'
                 '[publish]\nintent = "public"\n'
                 '[publish.commands]\ntest = "python3 -c \\"raise SystemExit(3)\\""\n',
    "README.md": "# sample\n説明だけがあり、手順が無い。\n",
    "src/mod.py": "PLACEHOLDER",
    # docs 側の見本は block ではなく warn で拾う。解説と本物を機械は見分けられない。
    "docs/guide.md": "PLACEHOLDER_DOC",
}

HEALTHY_FILES = {
    "work.toml": '[repo]\nname = "sample"\nenforcement = "block"\n'
                 '[publish]\nintent = "public"\n'
                 '[publish.commands]\ntest = "python3 -c \\"pass\\""\n'
                 'smoke = "python3 -c \\"pass\\""\n'
                 'help = "python3 -c \\"pass\\""\n',
    "README.md": HEALTHY_README,
    "LICENSE": "MIT License\n",
    "CONTRIBUTING.md": "PR を歓迎します。\n",
    ".gitignore": "__pycache__/\n",
    ".github/ISSUE_TEMPLATE.md": "## 再現手順\n",
    "scripts/repo-qa.py": "raise SystemExit(0)\n",
    "tests/test_sample.py": "def test_ok():\n    assert True\n",
    "src/mod.py": "def f():\n    try:\n        pass\n"
                  "    except ValueError as exc:\n        raise SystemExit(exc)\n",
}


# 欠陥リポで fail になっているべき観測（1つでも pass ならゲートに穴がある）
MUST_FAIL = [
    "license_exists", "install_section", "usage_section", "runnable_example",
    "contributing", "issue_template",
    "tests_exist", "gate_runs_in_repo", "test_passes", "tutorial_runs",
    "no_bare_except", "no_silent_pass",
    "no_secrets", "no_secrets_in_docs", "gitignore_exists",
    "no_private_terms", "no_absolute_home_paths",
    "command_appears_early", "help_available",
    "history_erased",
]

# 健全リポで pass になっているべき観測（fail なら誤爆）
MUST_PASS = [
    "readme_exists", "license_exists", "install_section", "usage_section",
    "runnable_example", "contributing", "issue_template",
    "tests_exist", "test_command_declared", "gate_runs_in_repo", "test_passes",
    "tutorial_runs", "no_bare_except", "no_silent_pass", "no_debug_print_left",
    "no_secrets", "env_example_not_real", "gitignore_exists",
    "no_private_terms", "no_absolute_home_paths",
    "command_appears_early", "zero_config_start", "help_available",
    "history_erased", "no_private_terms_in_history",
]


def main() -> int:
    failed: list[str] = []

    def check(desc: str, ok: bool) -> None:
        print(f"  {'OK ' if ok else 'NG '} {desc}")
        if not ok:
            failed.append(desc)

    with tempfile.TemporaryDirectory() as td:
        # --- 欠陥リポ: 全レーンが落ちること -------------------------------
        bad = build(Path(td) / "bad",
                    {**DEFECTIVE_FILES, "src/mod.py": defective_source(),
                     "docs/guide.md": f'key = "{FAKE_KEY}"\n'})
        # 公開の起点より前が残っている状態を作る。1コミットしか無い木は
        # history_erased を通ってしまい、このレーンを検査できない。
        (bad / "NOTES.md").write_text("2つ目のコミット\n", encoding="utf-8")
        commit(bad, "chore: second")
        code, res = run(bad, "--execute")
        print("\n欠陥リポ（落ちるべきもの）")
        check("公開不可と判定される", bool(res) and not res[0]["publishable"])
        check("enforcement=block なので exit 1", code == 1)
        st = states(res)
        for cid in MUST_FAIL:
            check(f"{cid} を検出する", st.get(cid) == "fail")
        # provenance は warn だけのレーンになったので「落ちる」ことはない。
        # 見たいのは「どのレーンも黙って素通りしない」ことなので、そう書く。
        check("どのレーンも黙って素通りしない",
              bool(res) and all(l["blocked"] or l["warnings"] for l in res[0]["lanes"]))
        check("第1段で止まる（どこにも出せない）",
              bool(res) and res[0]["reached"] == "" and res[0]["blocked_at"] == "internal")

        # --- 健全リポ: 誤爆しないこと -------------------------------------
        good = build(Path(td) / "good", HEALTHY_FILES)
        code, res = run(good, "--execute")
        print("\n健全リポ（通るべきもの）")
        check("公開可と判定される", bool(res) and res[0]["publishable"])
        check("第2段まで到達する", bool(res) and res[0]["reached"] == "public")
        check("exit 0", code == 0)
        st = states(res)
        for cid in MUST_PASS:
            check(f"{cid} が誤爆しない", st.get(cid) == "pass")

        # --- 対象の絞り込み ------------------------------------------------
        print("\n対象の絞り込み")
        undeclared = build(Path(td) / "undeclared",
                           {"work.toml": '[repo]\nname = "x"\n', "README.md": "# x\n"})
        _, res = run(undeclared)
        check("[publish] 未宣言のリポは対象外", res == [])

        private = build(Path(td) / "private",
                        {"work.toml": '[repo]\nname = "x"\n'
                                      '[publish]\nintent = "private"\n'})
        _, res = run(private)
        check("intent=private は対象外", res == [])

        no_work = build(Path(td) / "nowork", {"README.md": "# x\n"})
        _, res = run(no_work)
        check("work.toml が無いリポは対象外", res == [])

        # --- exclude が効くこと --------------------------------------------
        print("\n除外と実行の既定")
        excl = build(Path(td) / "excl", {
            **HEALTHY_FILES,
            "work.toml": HEALTHY_FILES["work.toml"].replace(
                'intent = "public"', 'intent = "public"\nexclude = ["private/"]'),
            "private/notes.py": defective_source(),
        })
        _, res = run(excl)
        st = states(res)
        check("exclude 配下の秘密は検出しない", st.get("no_secrets") == "pass")
        check("exclude 配下の固有名詞は検出しない", st.get("no_private_terms") == "pass")

        # --- 段で consequence が変わること ----------------------------------
        # 同じ観測でも、受け手が同僚から世間に変われば結果が変わってよい。
        # 顧客名を block 一本にすると社内共有まで止まり、warn 一本にすると
        # public に出てしまう。段別 severity が要るのはこの1点のためである。
        print("\n段で結果が変わること")
        named = build(Path(td) / "named",
                      {**HEALTHY_FILES, "src/note.py": f'CLIENT = "{a_private_term()}"\n'})
        _, res = run(named)
        st = states(res)
        check("顧客名を検出する", st.get("no_private_terms") == "fail")
        check("第1段は通る（同僚には配れる）",
              bool(res) and not res[0]["stages"][0]["blocked"])
        check("第2段で止まる（public には出せない）",
              bool(res) and res[0]["blocked_at"] == "public")
        check("到達点は internal と出る", bool(res) and res[0]["reached"] == "internal")

        # --- 当たった語が出力に残ること --------------------------------------
        # 分類（fail）と同じ粒度で「どの語が当たったか」が出ていないと、誤判定が
        # 見えない。実害は 2026-09-26 に出た: 33 本が全部「3 件以上該当
        # [clients.names]」と表示され、自分の顧客名なのか他社の名前なのかを
        # 仕分けられなかった。このチェックの目的は「外販物に他の顧客の名前が
        # 入っていたら渡せない」なので、語が出ないと目的を果たさない。
        print("\n当たった語が出ること")
        two = some_private_terms(2)
        multi = build(Path(td) / "multi", {
            **HEALTHY_FILES,
            "src/note.py": "".join(f'C{i} = "{t}"\n' for i, t in enumerate(two)),
        })
        _, res = run(multi)
        finding = next((f for r in res for lane in r["lanes"]
                        for f in lane["findings"] if f["id"] == "no_private_terms"), None)
        check("顧客名を検出する", bool(finding) and finding["state"] == "fail")
        detail = (finding or {}).get("detail", "")
        for t in two:
            check(f"当たった語が detail に出る（{len(t)} 文字の語）", t in detail)
        check("語ごとの件数が出る", "1 件" in detail)
        # 1語目で打ち切ると2語目が消える。件数の合計だけでは区別できない。
        check("2 語とも名指しされる（1語目で打ち切らない）",
              all(t in detail for t in two))

        check("severity のスカラーはどの段でも同じ",
              check_severity({"severity": "block"}, "internal") == "block"
              and check_severity({"severity": "block"}, "public") == "block")
        check("severity のテーブルは段で引く",
              check_severity({"severity": {"internal": "warn", "public": "block"}},
                             "internal") == "warn"
              and check_severity({"severity": {"internal": "warn", "public": "block"}},
                                 "public") == "block")
        check("宣言の無い段は warn に倒す（黙って block が増えない）",
              check_severity({"severity": {"public": "block"}}, "internal") == "warn"
              and check_severity({}, "public") == "warn")

        # --- 宣言と実態の突き合わせ -----------------------------------------
        # exclude が言えるのは「観測しない」だけで「公開物に入れない」ではない。
        # git は work.toml を読まないので、追跡下にあれば公開の瞬間に一緒に出る。
        print("\n宣言と実態の突き合わせ")
        lying = build(Path(td) / "lying", {
            **HEALTHY_FILES,
            "work.toml": HEALTHY_FILES["work.toml"].replace(
                'intent = "public"', 'intent = "public"\nexclude = ["private/"]'),
            "private/notes.md": "社内メモ\n",
        })
        _, res = run(lying)
        st = states(res)
        check("exclude に宣言したパスが追跡下なら検出する",
              st.get("excluded_paths_not_tracked") == "fail")
        check("第1段は止めない（社内共有には支障が無い）",
              bool(res) and not res[0]["stages"][0]["blocked"])
        check("第2段で止まる（公開すればそのまま出る）",
              bool(res) and res[0]["blocked_at"] == "public")

        honest = build(Path(td) / "honest", {
            **HEALTHY_FILES,
            "work.toml": HEALTHY_FILES["work.toml"].replace(
                'intent = "public"', 'intent = "public"\nexclude = ["private/"]'),
            ".gitignore": "__pycache__/\nprivate/\n",
            "private/notes.md": "社内メモ\n",
        })
        _, res = run(honest, "--execute")
        st = states(res)
        check("gitignore 済みなら誤爆しない",
              st.get("excluded_paths_not_tracked") == "pass")
        check("宣言が整合していれば第2段まで到達する",
              bool(res) and res[0]["reached"] == "public")

        no_excl = build(Path(td) / "noexcl", HEALTHY_FILES)
        _, res = run(no_excl)
        check("exclude の宣言が無ければ pass",
              states(res).get("excluded_paths_not_tracked") == "pass")

        # --- --execute を付けなければ実行しない -----------------------------
        code, res = run(bad)
        st = states(res)
        check("--execute 無しでは実行レーンは skip", st.get("test_passes") == "skip")
        check("skip はレーンを落とさない",
              not any(l["name"] == "correctness" and l["blocked"] for l in res[0]["lanes"])
              or st.get("tests_exist") == "fail")

        # --- 実行を伴う観測は、配られた段でしか当たらない ------------------
        # 実測: 第1段までしか測らないリポが test を宣言していたのに、correctness
        # が第2段だけのレーンだったので --execute しても1件も走らなかった。
        # 宣言が在るのに一度も走らないのは、落ちるのではなく通ってしまう
        # 壊れ方なので、走ったことを結果で確かめる。
        print("\n実行レーンが段に配られていること")
        internal_exec = build(Path(td) / "internal_exec", {
            "work.toml": '[repo]\nname = "s"\n[publish]\nintent = "internal"\n'
                         '[publish.commands]\ntest = "false"\n',
            "README.md": "# s\n説明だけ。\n",
            ".gitignore": "__pycache__/\n",
            "tests/test_s.py": "def test_ok():\n    assert True\n",
        })
        _, res = run(internal_exec, "--execute")
        st = states(res)
        check("intent=internal でもテストを実際に走らせる", st.get("test_passes") == "fail")
        check("落ちたテストは社内共有までは止めない",
              bool(res) and res[0]["reached"] == "internal")

        # 同じ観測でも段で consequence が変わる。第1段は事実を出すだけ、
        # 第2段は止める。動くと言って動かないものを世間に出さないため。
        flaky = build(Path(td) / "flaky", {
            **HEALTHY_FILES,
            "work.toml": HEALTHY_FILES["work.toml"].replace(
                'test = "python3 -c \\"pass\\""', 'test = "false"'),
        })
        _, res = run(flaky, "--execute")
        corr = [l for l in res[0]["lanes"] if l["name"] == "correctness"] if res else []
        check("correctness が両方の段に当たる", len(corr) == 2)
        check("落ちたテストは第1段では止めない", len(corr) == 2 and not corr[0]["blocked"])
        check("落ちたテストは第2段で止める", len(corr) == 2 and corr[1]["blocked"])
        check("到達点は internal と出る", bool(res) and res[0]["reached"] == "internal")

        # 累積の梯子なので同じ観測が2度当たる。判定は2度、実行は1度。
        counted = build(Path(td) / "counted", {
            **HEALTHY_FILES,
            "work.toml": '[repo]\nname = "s"\nenforcement = "block"\n'
                         '[publish]\nintent = "public"\n'
                         '[publish.commands]\ntest = "echo t >> ran.txt"\n'
                         'smoke = "echo s >> ran.txt"\nhelp = "true"\n',
        })
        run(counted, "--execute")
        ran = (counted / "ran.txt").read_text().split() if (counted / "ran.txt").is_file() else []
        check("同じコマンドを段ごとに走らせ直さない", ran == ["t", "s"])

        # --- 存在が登録である場合にだけ、存在で見てよい ---------------------
        # 実測 2026-08-28: .github/workflows が在るので pass していたリポが、
        # 同じ日にトリガーを外して push では二度と走らない状態になっていた。
        # ファイルは在るまま、観測は pass のまま。一方 scripts/*-qa.py という
        # 実際に使われている入口を持つリポは fail していた。判定が反転していた。
        print("\n機械が走らせる入口")
        workflow_only = build(Path(td) / "workflow_only", {
            **{k: v for k, v in HEALTHY_FILES.items() if k != "scripts/repo-qa.py"},
            ".github/workflows/ci.yml": "on: workflow_dispatch\njobs: {}\n",
        })
        _, res = run(workflow_only, "--execute")
        check("workflow の存在をゲートとして数えない",
              states(res).get("gate_runs_in_repo") == "fail")
        check("数えないが止めもしない（warn）", bool(res) and res[0]["publishable"])

        _, res = run(good)
        check("名前で拾われる入口は存在で認める",
              states(res).get("gate_runs_in_repo") == "pass")

        # --- 宣言が無ければ問わない観測（optional） -------------------------
        # e2e を持たない形のリポは在る。無いことを欠陥として数えると、
        # 当たらないはずのリポを毎回叱ることになる。
        print("\n宣言した者にだけ求める観測")
        _, res = run(good, "--execute")
        check("e2e を宣言していないリポは問わない（skip）",
              states(res).get("e2e_passes") == "skip")
        check("宣言が無くても公開可のまま", bool(res) and res[0]["publishable"])

        with_e2e = build(Path(td) / "with_e2e", {
            **HEALTHY_FILES,
            "work.toml": HEALTHY_FILES["work.toml"] + 'e2e = "false"\n',
        })
        _, res = run(with_e2e, "--execute")
        check("宣言した e2e は実際に走る", states(res).get("e2e_passes") == "fail")
        check("落ちた e2e は第2段で止める",
              bool(res) and res[0]["blocked_at"] == "public")

        # --- 案内は、走る観測が在るときだけ出す ----------------------------
        # 実測: 実行の当たらない internal 意図のリポにも「--execute で走らせます」
        # と出ていた。付けても何も起きないものを、付ければ走ると言っていた。
        print("\n案内の出し分け")
        check("走る観測が在れば案内を出す", "未実走" in run_text(good))
        check("--execute を付けたら案内は出さない",
              "未実走" not in run_text(good, "--execute"))

        no_cmds = build(Path(td) / "no_cmds", {
            "work.toml": '[repo]\nname = "s"\n[publish]\nintent = "internal"\n',
            "README.md": "# s\n説明だけ。\n",
            ".gitignore": "__pycache__/\n",
        })
        check("走る観測が無ければ案内を出さない", "未実走" not in run_text(no_cmds))
        _, res = run(no_cmds)
        check("コマンド未宣言でも社内共有は止めない",
              bool(res) and res[0]["reached"] == "internal")

        # --- agent_ready は shape で絞られること -------------------------
        # 実測で requests も fastapi も全項目 0 だった。それが正しい。
        # ライブラリに登録動線が無いのは欠陥ではないので、当ててはいけない。
        print("\n形による絞り込み")
        svc_src = ("def runSignup():\n    pass\n"
                   "DEVICE = 'device_code'\n"
                   "FLAGS = ['--with-token', '--json']\n")
        lib = build(Path(td) / "lib", {**HEALTHY_FILES, "src/mod.py": "def f():\n    pass\n"})
        _, res = run(lib)
        check("shape 未宣言なら agent_ready は当たらない",
              bool(res) and not any(l["name"] == "agent_ready" for l in res[0]["lanes"]))

        svc = build(Path(td) / "svc", {
            **HEALTHY_FILES,
            "work.toml": HEALTHY_FILES["work.toml"].replace(
                'intent = "public"', 'intent = "public"\nshape = "service"'),
            "README.md": HEALTHY_README.replace(
                "python3 -m pip install -e .", "npx sample login"),
            "src/mod.py": svc_src,
        })
        _, res = run(svc)
        st = states(res)
        check("shape=service なら agent_ready が当たる",
              bool(res) and any(l["name"] == "agent_ready" for l in res[0]["lanes"]))
        for cid in ("cli_entry_point", "no_form_auth", "headless_escape",
                    "machine_readable_output", "install_free_start"):
            check(f"{cid} が誤爆しない", st.get(cid) == "pass")

        bare = build(Path(td) / "bare", {
            **HEALTHY_FILES,
            "work.toml": HEALTHY_FILES["work.toml"].replace(
                'intent = "public"', 'intent = "public"\nshape = "service"'),
            "src/mod.py": "def f():\n    return 1\n",
        })
        _, res = run(bare)
        st = states(res)
        for cid in ("cli_entry_point", "no_form_auth", "headless_escape"):
            check(f"{cid} の欠落を検出する", st.get(cid) == "fail")

        # --- ** の展開 -----------------------------------------------------
        # 実際に起きた事故: `**/` が 0セグメントを食う場合しか当たらず、
        # `**/tests/*` が `a/tests/t.py` に当たらなかった。除外の宣言が効かない
        # 形で壊れるので、落ちるのではなく誤って落とすほうに倒れる。
        # linter の意図的に壊した fixture を 5階層下から 101 件拾っていた。
        # LICENSE のファイル名を並べると、並べなかった置き方が「無い」になる。
        # 実測: astral-sh/uv と sharkdp/bat の二重ライセンスが落ちていた。
        print("\nライセンスの置き方")
        for name in ("LICENSE-MIT", "LICENSE.md", "COPYING", "LICENCE"):
            dual = build(Path(td) / f"lic_{name}",
                         {k: v for k, v in HEALTHY_FILES.items() if k != "LICENSE"} |
                         {name: "MIT License\n"})
            _, res = run(dual)
            check(f"{name} をライセンスとして認める",
                  states(res).get("license_exists") == "pass")

        # *在ること* を見る観測が除外を守らないと、偽陽性ではなく偽陰性になる。
        # 実測: vendor した PyYAML の LICENSE を自分のものと数え、ライセンスを
        # 持たない2リポジトリが「公開可」に化けた。
        borrowed = build(Path(td) / "borrowed",
                         {k: v for k, v in HEALTHY_FILES.items()
                          if k not in ("LICENSE", "tests/test_sample.py")} |
                         {"vendor/pkg/LICENSE": "MIT License\n",
                          "node_modules/pkg/tests/t.js": "test('x', () => {});\n"})
        _, res = run(borrowed)
        st = states(res)
        check("vendor した依存のライセンスを自分のものと数えない",
              st.get("license_exists") == "fail")
        check("vendor した依存のテストを自分のものと数えない",
              st.get("tests_exist") == "fail")

        # --- 段ごとの到達（累積の梯子） --------------------------------- #
        print("\n段ごとの到達")
        # 漏れは無いが public の作法が無いリポ。社内には渡せるが切り出せない。
        internal_only = build(Path(td) / "internal_only", {
            "work.toml": '[repo]\nname = "s"\n[publish]\nintent = "public"\n',
            "README.md": "# s\n説明だけ。\n",
            ".gitignore": "__pycache__/\n",
        })
        _, res = run(internal_only)
        check("漏れが無ければ第1段は通る", bool(res) and res[0]["reached"] == "internal")
        check("public の作法が無ければ第2段では止まる",
              bool(res) and res[0]["blocked_at"] == "public"
              and not res[0]["publishable"])

        # 秘密が在れば、体裁が完璧でも第1段で止まる。
        leaky = build(Path(td) / "leaky",
                      {**HEALTHY_FILES, "src/leak.py": f'K = "{FAKE_KEY}"\n'})
        _, res = run(leaky)
        check("秘密が在れば体裁が揃っていても第1段で止まる",
              bool(res) and res[0]["blocked_at"] == "internal")

        # intent=internal を宣言したリポに public の作法を要求しない。
        declared_internal = build(Path(td) / "declared_internal", {
            "work.toml": '[repo]\nname = "s"\n[publish]\nintent = "internal"\n',
            "README.md": "# s\n説明だけ。\n",
            ".gitignore": "__pycache__/\n",
        })
        _, res = run(declared_internal)
        check("intent=internal は第1段までしか測らない",
              bool(res) and [st["name"] for st in res[0]["stages"]] == ["internal"])
        check("intent=internal でも LICENSE を要求しない",
              bool(res) and res[0]["reached"] == "internal")

        # --- 公開したあと、履歴の問いが変わる -------------------------------
        # history_erased は「1コミットか」しか見ない。公開は squash でしか
        # 証明できないからで、それは公開**前**の話である。公開した後は消せる
        # 過去がもう無いので、同じ観測が当たり続けると次の1コミットで必ず落ちる
        # （docstring がそれを予告したまま放置されていた）。
        #
        # [publish] released_at に公開した起点を宣言すると、問いが「起点より前が
        # 無いか」に変わり、起点より後は中身で見る（since = "released_at"）。
        print("\n公開したあとの履歴")
        rel = build(Path(td) / "released", HEALTHY_FILES)
        root_sha = subprocess.run(["git", "-C", str(rel), "rev-parse", "HEAD"],
                                  capture_output=True, text=True, timeout=30).stdout.strip()

        _, res = run(rel)
        check("宣言が無ければ 1 コミットを要求する（今までどおり）",
              states(res).get("history_erased") == "pass")
        (rel / "src" / "later.py").write_text("later = 1\n", encoding="utf-8")
        commit(rel, "feat: 公開のあとに積む")
        _, res = run(rel)
        check("宣言が無いまま積むと落ちる",
              states(res).get("history_erased") == "fail")
        check("宣言が無ければ起点以降の観測は当たらない",
              states(res).get("no_private_terms_since_release") in (None, "skip"))

        declare = (rel / "work.toml")
        declare.write_text(declare.read_text(encoding="utf-8").replace(
            'intent = "public"', f'intent = "public"\nreleased_at = "{root_sha}"'),
            encoding="utf-8")
        commit(rel, "chore: 公開の起点を宣言する")
        _, res = run(rel)
        check("起点を宣言すれば、以後に積んでも落ちない",
              states(res).get("history_erased") == "pass")

        wrong = subprocess.run(["git", "-C", str(rel), "rev-parse", "HEAD"],
                               capture_output=True, text=True, timeout=30).stdout.strip()
        declare.write_text(declare.read_text(encoding="utf-8").replace(
            root_sha, wrong), encoding="utf-8")
        commit(rel, "chore: 起点を root でない sha にする")
        _, res = run(rel)
        check("起点が root でなければ落ちる（前が残っている）",
              states(res).get("history_erased") == "fail")

        declare.write_text(declare.read_text(encoding="utf-8").replace(
            wrong, "0" * 40), encoding="utf-8")
        commit(rel, "chore: 存在しない sha にする")
        _, res = run(rel)
        check("存在しない sha を宣言したら落ちる",
              states(res).get("history_erased") == "fail")

        # 起点以降だけを見る観測は、レーンの宣言（registry 側）が要る。宣言を
        # 待たずに範囲の切り方そのものを固定しておく。ここで見たいのは
        # 「木から消しても履歴に残る」ことで、それが範囲を切る理由でもある。
        scoped = build(Path(td) / "scoped", HEALTHY_FILES)
        base = subprocess.run(["git", "-C", str(scoped), "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=30).stdout.strip()
        (scoped / "src" / "leak.py").write_text('name = "ZZQQ-Client"\n', encoding="utf-8")
        commit(scoped, "feat: 起点より後に入れる")
        (scoped / "src" / "leak.py").unlink()
        commit(scoped, "fix: 木からは消した")

        chk = {"id": "x", "kind": "history_pattern_absent",
               "patterns": ["ZZQQ-Client"], "since": "released_at"}
        ctx_no = {"released_at": "", "lanes_cfg": {}}
        ctx_yes = {"released_at": base, "lanes_cfg": {}}
        state, detail, _ = obs_history_pattern_absent(scoped, chk, ctx_no)
        check("起点の宣言が無ければ、起点以降の観測は当たらない", state == "skip")
        state, detail, _ = obs_history_pattern_absent(scoped, chk, ctx_yes)
        check("木から消しても、起点以降の履歴には残っている", state == "fail")
        state, _, _ = obs_history_pattern_absent(
            scoped, {**chk, "patterns": ["絶対に出ない語ZZ"]}, ctx_yes)
        check("入っていない語では誤爆しない", state == "pass")
        state, detail, _ = obs_history_pattern_absent(
            scoped, {**chk, "since": ""}, ctx_yes)
        check("since が無ければ全履歴を見る（今までどおり）",
              state == "fail" and "起点より後" not in detail)

        # 範囲を切る理由そのもの。公開**前**に入っていた分は、公開した時点で
        # もう出ている。消せないものを毎回数え直すと、直しようのない過去で
        # 永久に落ち続け、公開後のゲートが誰にも使われなくなる。
        early = build(Path(td) / "early", HEALTHY_FILES)
        (early / "src" / "old.py").write_text('name = "ZZQQ-Client"\n', encoding="utf-8")
        commit(early, "chore: 公開より前に入っていた")
        cut = subprocess.run(["git", "-C", str(early), "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=30).stdout.strip()
        (early / "src" / "clean.py").write_text("ok = 1\n", encoding="utf-8")
        commit(early, "feat: 公開のあとはきれいに積む")

        state, _, _ = obs_history_pattern_absent(
            early, chk, {"released_at": cut, "lanes_cfg": {}})
        check("起点より前にしか無い語では落ちない", state == "pass")
        state, _, _ = obs_history_pattern_absent(
            early, {**chk, "since": ""}, {"released_at": cut, "lanes_cfg": {}})
        check("全履歴を見る観測のほうは、同じ語で落ちる", state == "fail")

        # --- 走らせていない観測を、通ったことにしない ----------------------
        # skip の理由は1つではない。「当たらない」（e2e を持たない形のリポ、
        # --owned でない）と「走らせていない」は別のことなのに、どちらも skip
        # なので、レーンは同じ顔で OK になっていた。--execute を付けずに回すと、
        # テストが通ることを一度も確かめないまま「public に切り出せる」と
        # 答えていた（2026-09-02 実測）。severity=block の観測が未実走のとき、
        # その段は「通った」ではなく「まだ確かめていない」。
        print("\n走らせていない観測")
        _, res = run(good)
        check("実走しなければ第2段に到達しない",
              bool(res) and res[0]["reached"] == "internal")
        check("落ちたのではなく確かめていない、と言う",
              bool(res) and res[0]["blocked_at"] == "" and res[0]["unproven_at"] == "public")
        check("実走していなければ publishable と言わない",
              bool(res) and not res[0]["publishable"])
        check("何が走っていないかを名指しする",
              bool(res) and "test_passes" in
              [c for st in res[0]["stages"] for c in st.get("unproven", [])])
        _, res = run(good, "--execute")
        check("実走すれば第2段に到達する（誤爆しない）",
              bool(res) and res[0]["reached"] == "public" and res[0]["unproven_at"] == "")
        code, _ = run(good, "--require", "public")
        check("実走していない段を --require したら 2 が返る", code == 2)

        # 走らせても意味の無い skip（宣言が無い e2e）は、段を止めない。
        check("宣言の無い観測は保留にしない",
              bool(res) and "e2e_passes" not in
              [c for st in res[0]["stages"] for c in st.get("unproven", [])])

        # --- 到達すべき段を、呼ぶ側が宣言する（--require） ------------------
        # 既定の終了コードは enforcement に従うので、warn のリポではレーンが
        # 落ちていても 0 が返る。宣言どおりの扱いだが、ローカルゲートから呼ぶと
        # 「走らせるが結果を無視する」ゲートになる。かといって最後の段を要求
        # すると、構造的に届かないリポで永久に差し戻す。どこまで届いていれば
        # 良いかを呼ぶ側が言う形にした。
        #
        # 緑側だけでは穴が塞がった証拠にならないので、**割ったら本当に 2 が
        # 返る**ほうを固定する。落ちるところを見ていない検査は、検出できると
        # 分かっていない。
        print("\n到達すべき段の宣言")
        code, _ = run(good, "--require", "internal")
        check("届いていれば 0", code == 0)
        code, _ = run(good, "--require", "public", "--execute")
        check("最後の段まで届いていても 0", code == 0)

        warn_leaky = build(Path(td) / "warn_leaky", {
            **HEALTHY_FILES,
            "work.toml": HEALTHY_FILES["work.toml"].replace(
                'enforcement = "block"', 'enforcement = "warn"'),
            "src/leak.py": f'K = "{FAKE_KEY}"\n',
        })
        code, res = run(warn_leaky)
        check("enforcement=warn なら既定は 0（宣言どおり）", code == 0)
        check("それでも第1段では止まっている",
              bool(res) and res[0]["blocked_at"] == "internal")
        code, _ = run(warn_leaky, "--require", "internal")
        check("第1段を割ったら 2 が返る", code == 2)

        code, _ = run(good, "--require", "nosuchstage")
        check("知らない段を宣言したら 2", code == 2)

        code, _ = run(declared_internal, "--require", "public")
        check("測っていない段を要求したら 2", code == 2)

        # --- 自分が書いていないコードを裁かない ------------------------- #
        print("\n他人が書いた木（vendored）")
        vendored = build(Path(td) / "vendored", {
            **HEALTHY_FILES,
            "node_modules/pkg/index.js": f'const k = "{FAKE_KEY}";\n',
            ".aws-sam/build/f/botocore/data/x.json": f'{{"k": "{FAKE_KEY}"}}\n',
            "vendor/composer/lib.php": "<?php\n",
        })
        _, res = run(vendored, "--execute")
        check("vendor した木の中の秘密の形で落ちない",
              states(res).get("no_secrets") == "pass")
        check("vendor があっても判定を左右しない",
              bool(res) and res[0]["reached"] == "public")

        # --- 秘密は変数名ではなく値の形で見る --------------------------- #
        print("\n秘密は値の形で見る")
        local_creds = build(Path(td) / "local_creds", {
            **HEALTHY_FILES,
            "src/conf.py": 'URL = "postgres' + 'ql://app:app@db:5432/app"\n'
                           'ALT = "postgres' + 'ql://app:app@localhost:5432/app"\n',
            "config.yaml": 'api' + '_key: "YOUR_' + 'SERVICE_API_KEY"\n',
        })
        _, res = run(local_creds)
        check("到達できないホストへの資格情報は秘密ではない",
              states(res).get("no_secrets") == "pass")
        check("埋めてもらう見本の値は秘密ではない",
              states(res).get("no_secrets_in_docs") != "fail")

        # 秘匿処理の実装・解説コメント・正規表現リテラルは「秘密を扱うコード」であって
        # 「秘密が漏れているコード」ではない。見出しの後に本体が続くかで分ける。
        head = "-----BEGIN" + " PRIVATE KEY-----"
        talks_about_keys = build(Path(td) / "talks_about_keys", {
            **HEALTHY_FILES,
            "src/redact.py": f"# 鍵のブロック: {head} ... -----END" + " PRIVATE KEY-----\n"
                             f'RX = r"{head}[\\s\\S]*?"\n'})
        _, res = run(talks_about_keys)
        check("鍵の見出しを語るだけのコードは秘密ではない",
              states(res).get("no_secrets") == "pass")
        has_a_key = build(Path(td) / "has_a_key", {
            **HEALTHY_FILES,
            "certs/server.key": head + "\n" + ("MIIEvQIBADANBgkqhkiG9w0BAQEFAASC" * 5) + "\n"})
        _, res = run(has_a_key)
        check("本体が続いていれば鍵として落とす",
              states(res).get("no_secrets") == "fail")

        # 秘密は乱数であって語ではない。実測: langchain は api_key_field="anthropic_api_key"
        # という **フィールド名の文字列** で、codex はテスト用ヘルパの偽値で落ちていた。
        identifiers = build(Path(td) / "identifiers", {
            **HEALTHY_FILES,
            "src/conf.py": 'api' + '_key_field = "anthropic_api_key"\n'
                           'api' + '_key = "managed-bedrock-api-key"\n'})
        _, res = run(identifiers)
        check("小文字の識別子は秘密ではない", states(res).get("no_secrets") == "pass")

        # テストを src/ の隣に置く流儀を、置き場（ディレクトリ）だけ見ていて取りこぼしていた。
        beside = build(Path(td) / "beside", {
            **HEALTHY_FILES,
            "src/transport.test.ts": f'const k = "{FAKE_KEY}";\n',
            "src/auth_tests.rs": f'let k = "{FAKE_KEY}";\n'})
        _, res = run(beside)
        check("隣に置かれたテストの中のダミー値で落ちない",
              states(res).get("no_secrets") == "pass")

        # 接頭辞が合うだけの偽値。実測: openclaw の QA シナリオと e2e スクリプト。
        fake_token = build(Path(td) / "fake_token", {
            **HEALTHY_FILES,
            "qa/scenario.yaml": "botToken: xox" + "b-intentionally-invalid-lifecycle\n"})
        _, res = run(fake_token)
        check("接頭辞だけ合う偽のトークンは秘密ではない",
              states(res).get("no_secrets") == "pass")
        real_token = build(Path(td) / "real_token", {
            **HEALTHY_FILES,
            "src/conf.py": 'T = "xox' + 'b-123456789012-987654321098-' + "a" * 12 + "B3xY9zQ1mN7p" + '"\n'})
        _, res = run(real_token)
        check("本物の形のトークンは秘密として落とす",
              states(res).get("no_secrets") == "fail")

        # テストが本番のソースに同居する言語（Rust の #[cfg(test)]）。置き場では分けられない。
        inline_tests = build(Path(td) / "inline_tests", {
            **HEALTHY_FILES,
            "src/lib.rs": "pub fn f() {}\n\n#[cfg(test)]\nmod tests {\n"
                          f'    const K: &str = "{FAKE_KEY}";\n}}\n'})
        _, res = run(inline_tests)
        check("同居するテストモジュールの中のダミー値で落ちない",
              states(res).get("no_secrets") == "pass")

        placeholder_oauth = build(Path(td) / "placeholder_oauth", {
            **HEALTHY_FILES,
            "docs/setup.md": 'client_secret: "GOCSPX-' + 'xxxxx"\n'})
        _, res = run(placeholder_oauth)
        check("接頭辞だけ合う伏せ字は秘密ではない",
              states(res).get("no_secrets_in_docs") == "pass")

        real_creds = build(Path(td) / "real_creds", {
            **HEALTHY_FILES,
            "src/conf.py": 'URL = "postgres' + 'ql://app:hunter2@db.abc123.example.com:5432/app"\n',
        })
        _, res = run(real_creds)
        check("名前解決できるホストへの資格情報は秘密として落とす",
              states(res).get("no_secrets") == "fail")

        # --- 絶対パスは拡張子で逃げられない ----------------------------- #
        print("\n可搬性は拡張子で逃げられない")
        for ext in ("md", "json", "yaml", "py"):
            leaked_path = build(Path(td) / f"home_{ext}", {
                **HEALTHY_FILES, f"notes/memo.{ext}": f'"{HOME_PATH}"\n'})
            _, res = run(leaked_path)
            check(f".{ext} の中の絶対ホームパスを検出する",
                  states(res).get("no_absolute_home_paths") == "fail")

        # 同じ文字列でも文脈で意味が変わる。web の URL の中の /home/ は
        # 誰かのホームではない。実測 275 件中 27 件がこれだった。
        web_only = build(Path(td) / "web_only", {
            **HEALTHY_FILES,
            "data/links.json": '{"url": "https://example.com/home/tools/x?a=1"}\n'})
        _, res = run(web_only)
        check("web の URL の中の /home/ は数えない",
              states(res).get("no_absolute_home_paths") == "pass")
        file_url = build(Path(td) / "file_url", {
            **HEALTHY_FILES,
            "docs/setup.md": f"install from file://{HOME_PATH}pkg\n"})
        _, res = run(file_url)
        check("file:// のホームパスは本物として数える",
              states(res).get("no_absolute_home_paths") == "fail")

        # --- 所有の主張が無ければ当てない ------------------------------- #
        print("\n所有の主張")
        foreign = build(Path(td) / "foreign", {
            "README.md": HEALTHY_README, "LICENSE": "MIT\n",
            "src/mod.py": f'P = "{HOME_PATH}"\nC = "{a_private_term()}"\n'})
        _, res = run(foreign, "--assume-public")
        st = states(res)
        check("宣言も --owned も無ければ顧客名は裁かない",
              st.get("no_private_terms") == "skip")
        check("宣言も --owned も無ければ絶対パスは裁かない",
              st.get("no_absolute_home_paths") == "skip")
        _, res = run(foreign, "--assume-public", "--owned")
        st = states(res)
        check("--owned なら顧客名を検出する", st.get("no_private_terms") == "fail")
        check("--owned なら絶対パスを検出する",
              st.get("no_absolute_home_paths") == "fail")

        # --- private_terms のどのテーブルを見るか ----------------------- #
        print("\n持ち出してはいけない語の範囲")
        own_product = build(Path(td) / "own_product", {
            **HEALTHY_FILES,
            "src/name.py": f'NAME = "{a_private_term("products")}"\n'})
        _, res = run(own_product)
        check("自社製品名では落ちない（それはそのリポの中身である）",
              states(res).get("no_private_terms") == "pass")
        other_client = build(Path(td) / "other_client", {
            **HEALTHY_FILES,
            "src/name.py": f'NAME = "{a_private_term("clients")}"\n'})
        _, res = run(other_client)
        check("他の顧客名では落ちる", states(res).get("no_private_terms") == "fail")

        print("\n** の展開")
        glob_cases = [
            # 深さが増えても当たること
            ("tests/t.py", "**/tests/*", True),
            ("a/tests/t.py", "**/tests/*", True),
            ("a/b/c/tests/t.py", "**/tests/*", True),
            ("crates/x/resources/test/fixtures/cfg/try.py", "**/resources/test/*", True),
            ("crates/x/resources/test/fixtures/cfg/try.py", "**/fixtures/*", True),
            # ファイル名パターンが続く形（既存の宣言が依存している）
            ("x.sample.json", "**/*.sample.*", True),
            ("a/b/x.sample.json", "**/*.sample.*", True),
            ("a/b/c.py", "**/*.py", True),
            # 区切りの途中に当ててはいけない
            ("src/latest/x.py", "**/test/*", False),
            ("src/contests/x.py", "**/tests/*", False),
            ("engine/release_gate.py", "**/tests/*", False),
            ("README.md", "**/*.py", False),
            # ** を持たない宣言の挙動は変えない
            ("samples/company.sample.json", "*.sample", False),
            ("docs/a/b.md", "docs/*", True),
        ]
        for rel, pattern, want in glob_cases:
            got = _match_glob(rel, pattern)
            check(f"{pattern} × {rel} → {want}", got == want)

        # --- 宣言の書き方（パーサ間で解釈が割れないこと）-------------------
        # 実際に起きた事故: 正規表現を基本文字列で書いたため \\s と二重に書く必要が
        # あり、3.9 の fallback パーサでは二重のまま残って robustness レーンが黙って
        # 素通りした。落ちるのではなく「通ってしまう」種類の壊れ方なので固定する。
        print("\n宣言の書き方")
        reg = registry_root()
        for name in ("release_lanes.toml", "secret_patterns.toml"):
            lines = [ln for ln in (reg / name).read_text(encoding="utf-8").splitlines()
                     if not ln.lstrip().startswith("#")]
            basic = [ln.strip() for ln in lines
                     if ln.split("=")[0].strip() == "pattern" and ' = "' in ln]
            check(f"{name}: 正規表現がリテラル文字列で書かれている", not basic)
            doubled = [ln.strip() for ln in lines if chr(92) * 2 in ln]
            check(f"{name}: 二重エスケープが残っていない", not doubled)
        try:
            import tomllib
            for name in ("release_lanes.toml", "secret_patterns.toml"):
                path = reg / name
                check(f"{name}: tomllib と fallback の解釈が一致",
                      tomllib.load(path.open("rb")) == _load_toml(path))
        except ImportError:
            print("  --  tomllib が無いので突き合わせは省略（3.11+ 側で実施）")

        # --- 宣言されたテストが、在るテストを全部走らせるか -----------------
        # --- 見ていない範囲を、見た結果と同じ顔で出さない -------------------
        # --scan は宣言のあるリポだけを歩く。宣言が opt-in なのは意図的だが、
        # 出力が「宣言した N 本が OK」としか言わないので、フリート全体が OK
        # であるかのように読めた。2026-09-02 実測: 82 リポ中 5 本しか観測して
        # いないのに、見ていない 77 本には一言も触れていなかった。
        #
        # --assume-public は「この物差し自体を疑う」ための校正モードなのに、
        # 同じ discover を通っていたため宣言済みの側しか母数に入らなかった。
        # 物差しを受け入れたリポだけで物差しを校正していた。
        print("\n見ていない範囲")
        fleet = Path(td) / "fleet"
        build(fleet / "declared", HEALTHY_FILES)
        build(fleet / "silent_a", {"README.md": "# a\n"})
        build(fleet / "silent_b", {"README.md": "# b\n"})
        (fleet / "not_a_repo").mkdir(parents=True, exist_ok=True)

        text = run_scan(fleet)
        check("宣言のあるリポだけを観測する", "[declared]" in text
              and "[silent_a]" not in text)
        check("見ていない本数を言う", "2 本あります" in text)
        check("見ていないことを、問題が無いこととして出さない",
              "見ていないことは、問題が無いことではありません" in text)
        check("リポジトリでないディレクトリは数に入れない", "not_a_repo" not in text)

        cal = run_scan(fleet, "--assume-public")
        check("校正モードは宣言の無いリポにも物差しを当てる",
              "[silent_a]" in cal and "[silent_b]" in cal)
        check("校正モードでは見ていない本数を言わない（全部見ている）",
              "本あります" not in cal)

        # 実際に起きた事故: [publish.commands] test が test_guards.py だけを
        # 実際に起きた事故: [publish.commands] test が test_guards.py だけを
        # 指していて、他の2本は correctness レーンから漏れていた。テストが
        # 落ちるのではなく「一度も走らない」ので、緑のまま壊れる。
        # 宣言側にファイル名を並べる限り同じことが再発するので、宣言は発見する
        # 側（tests/run_all.py）を指すことを固定する。
        root = Path(__file__).resolve().parent.parent
        declared = _load_toml(root / "work.toml").get(
            "publish", {}).get("commands", {}).get("test", "")
        check("work.toml の test が tests/run_all.py を指している",
              "tests/run_all.py" in declared)
        listed = subprocess.run(
            [sys.executable, str(root / "tests" / "run_all.py"), "--list"],
            capture_output=True, text=True).stdout.split()
        on_disk = sorted(p.name for p in (root / "tests").glob("test_*.py"))
        check("run_all.py が tests/ の test_*.py を取りこぼさない",
              sorted(listed) == on_disk)
        check("走らせる対象が1本以上ある", len(on_disk) >= 1)

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
