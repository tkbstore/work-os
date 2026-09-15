#!/usr/bin/env python3
"""ガードが「止めるべきものを止め、止めてはいけないものを通す」ことを検査する。

sweep_guard は2度破られた。1度目は find -delete を書き換え操作に数えていなかったため、
2度目はリポ名を1つも書かずに全リポへ届く形を見ていなかったため。どちらも
**実際に破られてから**分かった。ガードは宣言では守れない。

このテストは、過去に破られた形と誤爆した形をすべて含む。同じ穴が二度開かないように
するのが目的で、新しい穴が見つかったらここに1行足すこと。

  python3 tests/test_guards.py
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BLOCK, ALLOW = 2, 0

# 仕掛けに置くリポジトリ名。実在の組織名を借りない（abstraction_gate に止められる）し、
# 実在のディスクにも依存しない。以前は隣のディレクトリから本物のリポ名を借りていたが、
# リポが3つ未満の環境（新規 clone・CI）では「3リポを明示した rm」が架空名になり、
# ガードが反応しないのが正しい状態になっていた。落ちるのではなく黙って空回りする。
# Delta-Repo は大文字を含む。macOS のファイルシステムは大小を区別しないので、
# 小文字で書かれた言及も同じディレクトリに届く。区別して数えると素通しになる。
FIXTURE_REPOS = ("alpha-repo", "beta-repo", "gamma-repo", "Delta-Repo", "one")
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    "PATH": "/usr/bin:/bin:/usr/local/bin",
}


def build_fixture(tmp: Path) -> tuple[Path, Path]:
    """リポジトリ群のルートと、その中に複製した hooks/ を返す。

    sweep_guard と branch_guard は自分の置き場所からリポジトリ群のルートを導く
    （ROOT = hooks/../..）。だから hooks を仕掛けの中に複製して動かせば、
    本物のディスクを一切見ずに検査できる。
    """
    repos = tmp / "repos"
    repos.mkdir()
    for name in FIXTURE_REPOS:
        d = repos / name
        (d / "sessions").mkdir(parents=True)
        (d / ".claude").mkdir()
        (d / "registry").mkdir()
        # branch_guard は保護ブランチ上かどうかを見るので main を作る
        subprocess.run(["git", "-C", str(d), "init", "-q", "-b", "main"],
                       check=True, env=GIT_ENV)
        (d / "README.md").write_text("x", encoding="utf-8")
        subprocess.run(["git", "-C", str(d), "add", "README.md"],
                       check=True, env=GIT_ENV)
        subprocess.run(["git", "-C", str(d), "commit", "-qm", "first"],
                       check=True, env=GIT_ENV)
    hooks = repos / "work-os" / "hooks"
    hooks.mkdir(parents=True)
    for hook in ("sweep_guard.py", "branch_guard.py"):
        shutil.copy2(ROOT / "hooks" / hook, hooks / hook)
    return repos, hooks


def sweep_cases(repos: Path) -> list[tuple[str, str, str, int]]:
    """(説明, cwd, コマンド, 期待する exit)"""
    r = str(repos)
    a, b, c = FIXTURE_REPOS[:3]
    multiline = 'git commit -m "chore: x\n\n' + f'{a} {b} {c}"'
    return [
        # --- 止めるべきもの ---
        ("横断コミットのループ", r, 'for d in */; do git -C $d commit -m "x"; done', BLOCK),
        # 実在するリポ名でなければ横断とみなされない（架空名では検査にならない）
        ("3リポを明示した rm", r, f"rm -f {a}/x {b}/y {c}/z", BLOCK),
        # 実際に破られた形1: find -delete を書き換え操作に数えていなかった
        ("ルートで find -delete", r, 'find . -path "*/sessions/*.md" -empty -delete', BLOCK),
        ("find -exec rm", r, 'find . -name "*.tmp" -exec rm {} +', BLOCK),
        ("xargs rm", r, 'find . -name x | xargs rm -f', BLOCK),
        # 実際に破られた形2: リポ名が1つも出ない横断
        ("cd root してから再帰削除", str(repos / "work-os"),
         f'cd {r} && find . -name "*.tmp" -delete', BLOCK),
        ("ルートで maxdepth 2 の削除", r,
         'find . -maxdepth 2 -name ".mcp.json" -delete', BLOCK),

        # --- 通すべきもの ---
        ("読み取りの横断ループ", r, 'for d in */; do git -C $d log -1; done', ALLOW),
        ("ルートでの find 読み取り", r, 'find . -name "*.md" | head', ALLOW),
        ("単一リポのコミット", r, 'git -C one commit -m "x"', ALLOW),
        ("単一リポ内の find -delete", f"{r}/one", 'find . -name "*.pyc" -delete', ALLOW),
        ("単一リポ内の sed -i", f"{r}/one", 'sed -i "" s/a/b/ README.md', ALLOW),
        # 実際に誤爆した形: メッセージ本文のリポ名を数えていた
        ("メッセージ本文に複数リポ名", r, f'git commit -m "{a} と {b} と {c} を整理"', ALLOW),
        ("複数行メッセージ", r, multiline, ALLOW),
        ("承認済みの横断削除", r, 'WORKOS_SWEEP=1 find . -name "*.tmp" -delete', ALLOW),
        ("普通の ls", r, "ls -la", ALLOW),

        # --- 実際に破られた形3: 大小文字で観測が消える（素通し） ---
        # macOS では Delta-Repo と delta-repo は同じディレクトリ。区別して数えると
        # 名前が1つ消えて閾値に届かず、本物の rm が通り抜ける。
        ("大文字リポを小文字で書いた rm", r,
         f"rm -f delta-repo/x {a}/y {b}/z", BLOCK),

        # --- 実際に誤爆した形2: heredoc 本文を書き込み先として数えていた ---
        # 本文は「書き込まれる中身」で、行き先はリダイレクト側に在る。
        ("heredoc 本文のリポ名と動詞", r,
         "python3 - <<'PY' > /tmp/out.json\n"
         f"# {a} {b} {c} を rm -rf する手順を説明する文字列\n"
         "PY", ALLOW),

        # --- 実際に誤爆した形3: 動詞の引数範囲の外まで数えていた ---
        # && の後ろは読み取り。書き込み先は /tmp の1箇所しかない。
        ("書き込みの後ろに読み取りが連なる", r,
         f'sed -i "" s/x/y/ /tmp/a && python3 {a}/scan.py {b} {c}', ALLOW),
        ("読み取りの後ろに書き込みが連なる", r,
         f"python3 {a}/scan.py {b} {c} | tee /tmp/out.txt", ALLOW),

        # --- ACK の解錠が緩い（素通し） ---
        # 環境変数として前置されたときだけ解錠すべきで、文字列として
        # 出てくるだけで通ってはいけない。08-28 に自分でこの穴を通った。
        ("ACK 名が本文に出るだけでは解錠しない", r,
         f'echo "WORKOS_SWEEP の説明" > /tmp/n.md && rm -f {a}/x {b}/y {c}/z', BLOCK),

        # --- 実際に誤爆した形4: コミットメッセージを対象範囲の判定に使っていた ---
        # git は1回の呼び出しで1リポジトリしか触らない。対象は -C か cwd が決める。
        # だから git の引数に出るリポ名は、定義上すべてメッセージ本文である。
        # -m を文字列として拾う実装は、短縮フラグの組み合わせで必ず抜ける。
        # 実測 2026-08-28: あるリポの7ファイルだけのコミットが「4リポジトリへの
        # 横断書き換え」と判定された。他3リポは未コミット変更 0 件だった。
        ("git commit -am のメッセージ", f"{r}/one",
         f'git commit -am "feat: {a} と {b} と {c} の連携"', ALLOW),
        ("git commit -sm のメッセージ", f"{r}/one",
         f'git commit -sm "feat: {a} と {b} と {c} の連携"', ALLOW),
        ("git commit -aqm のメッセージ", f"{r}/one",
         f'git commit -aqm "feat: {a} と {b} と {c} の連携"', ALLOW),
        ("フラグ無しの引数にリポ名", f"{r}/one",
         f'git tag -a v1 -m "{a} {b} {c} に配る"', ALLOW),
        # --- 実際に破られた形4: heredoc 本文を無条件に潰していた（素通し） ---
        # 本文がデータなら潰してよいが、実行されるなら潰してはいけない。
        # 同じ削除を bash の heredoc に入れるだけで通っていた。しかもこの回避は
        # 誤検知に当たった複数のセッションが独立に学習していて、抜け道と同じ形だった。
        # 列挙するのは「データとして扱う受け手」の側。漏れたときに止まる側に倒す。
        ("bash の heredoc に入れた削除", r,
         f"bash <<'EOF'\nrm -rf {a}/x {b}/x {c}/x\nEOF", BLOCK),
        ("sh の heredoc に入れた削除", r,
         f"sh <<EOF\nrm -rf {a}/x {b}/x {c}/x\nEOF", BLOCK),
        ("env 前置つきの bash heredoc", r,
         f"FOO=1 bash -s <<'EOF'\nrm -rf {a}/x {b}/x {c}/x\nEOF", BLOCK),
        # 列挙に無い受け手は「実行される」側として扱う（安全側）
        ("知らないインタプリタの heredoc", r,
         f"myshell <<'EOF'\nrm -rf {a}/x {b}/x {c}/x\nEOF", BLOCK),
        # 先頭が cat でも、渡る先が bash なら本文は実行される
        ("cat の heredoc を bash に流す", r,
         f"cat <<'EOF' | bash\nrm -rf {a}/x {b}/x {c}/x\nEOF", BLOCK),

        # 対象が -C で明示的に散っているときは、名前ではなく -C の値で数える
        ("-C で3リポに散る git", r,
         f"git -C {a} reset --hard && git -C {b} reset --hard "
         f"&& git -C {c} reset --hard", BLOCK),
    ]


def branch_cases(repos: Path) -> list[tuple[str, str, bool]]:
    """(説明, 対象パス, 警告が出るべきか)"""
    one = repos / "one"
    return [
        ("リポ直下のコード", str(one / "README.md"), True),
        ("sessions/ は素通り", str(one / "sessions" / "x.md"), False),
        (".claude/ は素通り", str(one / ".claude" / "settings.json"), False),
        ("registry/ は素通り", str(one / "registry" / "x.toml"), False),
    ]


# claude_guard は kernel と golden_path を別の規則で扱う。
# kernel は憲法 §6-1 が条件を付けずに禁じているので enforcement に従わない。
# golden_path は §3 が「強制はしない」と書いているので enforcement に従う。
# 両方を同じ enforcement で扱っていたため、kernel が warn で素通りしていた。
# (説明, work-os 相対パス, 止めるべきか)
CLAUDE_CASES: list[tuple[str, str, bool]] = [
    ("kernel の憲法は warn でも止める", "CONSTITUTION.md", True),
    ("kernel のコードも warn でも止める", "engine/workos.py", True),
    ("golden_path は warn なら通す", "engine/release_gate.py", False),
    ("config 層は通す", "registry/release_lanes.toml", False),
    ("宣言の無いパスは通す", "README.md", False),
]


# claude_guard は全プロジェクトの Edit/Write に挟まる。落ちれば編集そのものが
# 止まるので、どんな入力でも exit 0 で返ることが不変条件である。検出系の故障は
# fail-open（誤判定した瞬間に誤爆装置になる系は fail-open にする、という実測から）。
def robustness_cases(tmp: Path) -> list[tuple[str, str, bool]]:
    """(説明, stdin に渡す生の payload, deny を期待するか)"""
    broken = tmp / "broken"
    broken.mkdir(parents=True, exist_ok=True)
    (broken / "work.toml").write_text("this is [not valid toml\n", encoding="utf-8")
    (broken / "a.py").write_text("x", encoding="utf-8")

    link = tmp / "link.md"
    if not link.is_symlink():
        link.symlink_to(ROOT / "CONSTITUTION.md")

    def edit(fp: object) -> str:
        return json.dumps({"tool_name": "Edit", "tool_input": {"file_path": fp}})

    return [
        # 落ちてはいけない形（実測 2026-08-28: この3形で exit 1 だった）
        ("work.toml が壊れていても落ちない", edit(str(broken / "a.py")), False),
        ("file_path が文字列でなくても落ちない", edit(123), False),
        ("異常に長いパスでも落ちない", edit("/" + "a" * 9000), False),
        ("JSON でない stdin", "not json at all", False),
        ("空の stdin", "", False),
        ("tool_input 欠落", json.dumps({"tool_name": "Edit"}), False),
        ("NUL を含むパス", edit("/tmp/a\x00b"), False),
        # symlink 越しの kernel 書き込み。パスを解決してからリポを探さないと素通りする。
        # 08-27 に cross-repo-guard で同じ穴を踏んでいる。
        ("symlink 越しの kernel は止める", edit(str(link)), True),
    ]


def run_claude_raw(payload: str) -> tuple[int, bool]:
    """(exit code, deny を返したか)"""
    proc = subprocess.run(
        [sys.executable, str(ROOT / "hooks" / "claude_guard.py")],
        input=payload, capture_output=True, text=True, timeout=20)
    try:
        out = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        out = {}
    denied = (out.get("hookSpecificOutput") or {}).get("permissionDecision") == "deny"
    return proc.returncode, denied


def run_claude(path: str, tool: str = "Edit") -> bool:
    """deny を返したか。"""
    proc = subprocess.run(
        [sys.executable, str(ROOT / "hooks" / "claude_guard.py")],
        input=json.dumps({"tool_name": tool,
                          "tool_input": {"file_path": str(ROOT / path)}}),
        capture_output=True, text=True, timeout=20)
    if proc.returncode != 0:
        return False
    try:
        out = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return False
    return (out.get("hookSpecificOutput") or {}).get("permissionDecision") == "deny"


def kernel_watch(repo: Path) -> str:
    """kernel_watch が返した理由。何も言わなければ空文字。"""
    proc = subprocess.run(
        [sys.executable, str(ROOT / "hooks" / "kernel_watch.py")],
        input=json.dumps({"cwd": str(repo), "tool_name": "Bash"}),
        capture_output=True, text=True, timeout=20)
    if proc.returncode != 0:
        return f"!exit {proc.returncode}"
    try:
        return str(json.loads(proc.stdout or "{}").get("reason", ""))
    except json.JSONDecodeError:
        return ""


def build_layered_repo(tmp: Path) -> Path:
    """kernel と golden_path を宣言した、1コミットだけの木。"""
    repo = tmp / "layered"
    (repo / "engine").mkdir(parents=True, exist_ok=True)
    (repo / "work.toml").write_text(
        '[repo]\nname = "layered"\nenforcement = "warn"\n\n'
        '[layers]\nkernel = ["engine/core.py"]\n'
        'golden_path = ["engine/tool.py"]\n', encoding="utf-8")
    (repo / "engine" / "core.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "engine" / "tool.py").write_text("y = 1\n", encoding="utf-8")
    (repo / "engine" / "other.py").write_text("z = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(repo)], check=True, timeout=30)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, timeout=30)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "chore: 初期状態"],
                   check=True, timeout=30, env={**GIT_ENV})
    return repo


def run(hooks: Path, hook: str, payload: dict) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, str(hooks / hook)],
        input=json.dumps(payload), capture_output=True, text=True, timeout=20,
    )
    return proc.returncode, proc.stderr


def main() -> int:
    failed: list[str] = []

    with tempfile.TemporaryDirectory() as td:
        # resolve() は必須。macOS の一時ディレクトリは /var/... で渡されるが
        # ガード側は resolve() 済みの /private/var/... を持つので、
        # 解決しないと「ルートに居る」判定が一致せず、検査が空回りする。
        repos, hooks = build_fixture(Path(td).resolve())
        sweeps = sweep_cases(repos)
        branches = branch_cases(repos)

        print("sweep_guard")
        for desc, cwd, command, expected in sweeps:
            code, _ = run(hooks, "sweep_guard.py",
                          {"cwd": cwd, "tool_input": {"command": command}})
            ok = code == expected
            if not ok:
                failed.append(f"sweep_guard: {desc}（期待 {expected} / 実際 {code}）")
            print(f"  {'OK ' if ok else 'NG '} {desc}")

        # branch_guard は警告のみ（何も止めない）。exit は常に0であること自体が不変条件。
        print("\nbranch_guard")
        for desc, path, should_warn in branches:
            code, err = run(hooks, "branch_guard.py", {"tool_input": {"file_path": path}})
            warned = "[work-os]" in err
            # 仕掛けは必ず実在するので、警告の有無をそのまま判定してよい。
            # 以前は「実在するときだけ判定する」と書いてあり、実在しない環境では
            # 4ケースすべてが無条件に通っていた。
            ok = code == 0 and warned == should_warn
            if not ok:
                failed.append(f"branch_guard: {desc}（exit={code} warned={warned}）")
            print(f"  {'OK ' if ok else 'NG '} {desc}")

    print("\nclaude_guard の頑健性")
    with tempfile.TemporaryDirectory() as rtd:
        robust = robustness_cases(Path(rtd).resolve())
        for desc, payload, want_deny in robust:
            code, denied = run_claude_raw(payload)
            ok = code == 0 and denied == want_deny
            if not ok:
                failed.append(
                    f"claude_guard: {desc}"
                    f"（期待 exit=0/deny={want_deny} / 実際 exit={code}/deny={denied}）")
            print(f"  {'OK ' if ok else 'NG '} {desc}")

    print("\nclaude_guard")
    for desc, rel, should_deny in CLAUDE_CASES:
        denied = run_claude(rel)
        ok = denied == should_deny
        if not ok:
            failed.append(f"claude_guard: {desc}（denied={denied}）")
        print(f"  {'OK ' if ok else 'NG '} {desc}")
    # 書き込み以外のツールには一切干渉しない
    ok = not run_claude("CONSTITUTION.md", tool="Read")
    if not ok:
        failed.append("claude_guard: Read まで止めている")
    print(f"  {'OK ' if ok else 'NG '} 読み取りには干渉しない")

    # --- 手段を数えている限り、数えなかった手段が通る --------------------
    #
    # claude_guard はツール名の列挙（Edit / Write / MultiEdit / NotebookEdit）で
    # 守っている。2026-09-02 実測: 同じ engine/workos.py に対して Edit は deny
    # されたのに、Bash から python でパッチを当てる形は最後まで一度も止まらな
    # かった。書き込みの手段は数え切れない（sed -i / tee / cp / リダイレクト /
    # エディタ / スクリプト）ので、手段を数える限り必ず抜ける。
    #
    # kernel_watch は手段を見ない。git が「HEAD と違う」と言う守護対象が在れば、
    # どう書かれたかに関係なく報告する。だからここでも Bash 以外は列挙しない。
    print("\nkernel_watch（手段によらず結果で見る）")
    with tempfile.TemporaryDirectory() as td:
        repo = build_layered_repo(Path(td))
        cases: list[tuple[str, bool]] = []

        cases.append(("変更が無ければ何も言わない", kernel_watch(repo) == ""))

        core = repo / "engine" / "core.py"
        core.write_text("x = 2\n", encoding="utf-8")     # Edit を通さない書き込み
        said = kernel_watch(repo)
        cases.append(("ツールを通さない書き換えでも kernel を報告する",
                      "engine/core.py" in said and "kernel" in said))
        cases.append(("同じ内容では二度言わない", kernel_watch(repo) == ""))

        core.write_text("x = 3\n", encoding="utf-8")
        cases.append(("内容が変わればまた言う", "engine/core.py" in kernel_watch(repo)))

        (repo / "engine" / "other.py").write_text("z = 9\n", encoding="utf-8")
        cases.append(("守護対象でないファイルは報告しない",
                      "other.py" not in kernel_watch(repo)))

        (repo / "engine" / "tool.py").write_text("y = 2\n", encoding="utf-8")
        said = kernel_watch(repo)
        cases.append(("golden_path も報告する", "engine/tool.py" in said))
        cases.append(("golden_path に kernel の文言を付けない",
                      "§6-1" not in said))

        plain = Path(td) / "plain"
        plain.mkdir()
        cases.append(("work.toml が無いリポでは何もしない", kernel_watch(plain) == ""))

        for desc, ok in cases:
            if not ok:
                failed.append(f"kernel_watch: {desc}")
            print(f"  {'OK ' if ok else 'NG '} {desc}")

    # --- 道具や宣言を読めずに抜ける口が、黙っていないこと ------------------
    #
    # hook は落ちてはいけないので、どれも例外を握って抜ける口を持つ。そこで
    # 黙ると「検査して通した」と「一度も検査しなかった」が外から見分けがつかない。
    # 実測 2026-08-31: engine/workos.py に構文エラーを入れると claude_guard が
    # CONSTITUTION.md（kernel）への Write を stderr 0バイトで通した。同型が同日
    # 3箇所（root_cause_guard 2 / claude_guard 1）。
    #
    # 見るのは **try が何を守っているか** である。「入力が壊れている」「自分の
    # 担当外のパスだ」で黙って抜けるのは妥当な設計で、hook 4本に12箇所ある。
    # 分けずに全部を要求すると、正しい設計に雑音を出すだけの検査になる。
    # 区別できるのは try の中身なので、道具（import）と宣言（registry の読み出し）
    # を守っている except だけを対象にする。
    #
    # hook 名では列挙しない。列挙すると次に足された hook が黙って素通りし、
    # この検査が捕まえたい形そのものになる。
    print("\n道具・宣言を読めずに抜ける口")
    tryblk = re.compile(
        r"^([ \t]*)try:\n((?:\1[ \t]+[^\n]*\n|\s*\n)+?)\1except[^\n]*\n"
        r"((?:\1[ \t]+[^\n]*\n|\s*\n)+)", re.M)
    GUARDS = re.compile(r"^\s*(?:from|import)\s|registry_path\(|_load_toml\(", re.M)
    for hook in sorted((ROOT / "hooks").glob("*.py")):
        body = hook.read_text(encoding="utf-8")
        silent = []
        for m in tryblk.finditer(body):
            if not GUARDS.search(m.group(2)):
                continue                      # 入力や担当外。黙って抜けてよい
            # コメントは落としてから見る。注記に「stderr」と書いてあるだけで
            # 「出している」と読んでしまう。この検査の版2がそれで、print を
            # 消しても直前のコメントに残った語で OK と出た（2026-08-31）。
            code = "\n".join(ln for ln in m.group(3).splitlines()
                             if not ln.lstrip().startswith("#"))
            leaves = ("SystemExit" in code or "sys.exit(" in code
                      or re.search(r"\breturn\b", code))
            if leaves and "sys.stderr" not in code:
                silent.append(body[:m.start()].count("\n") + 1)
        ok = not silent
        if not ok:
            failed.append(f"{hook.name}: 道具・宣言を読めずに黙って抜ける口 {silent}")
        print(f"  {'OK ' if ok else 'NG '} {hook.name}")

    # --- 見つけたものが、記録より後ろに来ていないこと ----------------------
    #
    # 実測 2026-08-31: fleet_watch.sh はログ追記と通知を先頭付近で終えていて、
    # その後に続く5つの検査（生成器 / ガード検査 / 公開ゲート / 公開ゲート検査 /
    # inbox）は out に足されるだけだった。「[ガード検査] 失敗。hooks/ に穴が
    # 開いています」は一度も記録にも通知にも出たことがない。
    #
    # さらに5つのうち3つは code を上げてすらいなかった。位置だけ直しても3つは
    # 黙ったままになる。各検査に「足したら code も上げる」を憶えさせる形は、
    # 憶え忘れた検査だけが静かに落ちる。足すことと知らせることを1関数で結び、
    # その形を見る。検査の名前では列挙しない。列挙は次に足す検査を素通しする。
    print("\nfleet_watch.sh")
    watch = (ROOT / "hooks" / "fleet_watch.sh").read_text(encoding="utf-8")
    wl = watch.splitlines()
    fn = re.search(r"^add\(\)\s*\{.*?^\}", watch, re.M | re.S)
    if not fn:
        failed.append("fleet_watch.sh: 追記を集約する関数が無い")
        print("  NG  追記の集約")
    else:
        lo = watch[:fn.start()].count("\n")
        hi = watch[:fn.end()].count("\n")
        loose = [i + 1 for i, ln in enumerate(wl)
                 if 'out="$out' in ln and not lo <= i <= hi]
        if loose:
            failed.append(f"fleet_watch.sh: 集約を通さずに out へ足している {loose}")
        print(f"  {'OK ' if not loose else 'NG '} 追記の集約")

    calls = [i for i, ln in enumerate(wl) if re.match(r'\s*add\s+["\[]', ln)]
    writes = [i for i, ln in enumerate(wl) if '>> "$LOG"' in ln]
    if not calls or not writes:
        failed.append("fleet_watch.sh: 追記の呼び出しか記録の書き込みが見つからない")
        ok = False
    else:
        ok = max(calls) < min(writes)
        if not ok:
            failed.append(
                f"fleet_watch.sh: {min(writes) + 1}行目の記録より後ろに検査がある"
                f"（最後の追記は {max(calls) + 1}行目）")
    print(f"  {'OK ' if ok else 'NG '} 記録は全検査の後")

    print()
    if failed:
        print(f"失敗 {len(failed)} 件")
        for f in failed:
            print(f"  - {f}")
        return 1
    hooks_n = len(list((ROOT / "hooks").glob("*.py")))
    print(f"全 {len(sweeps) + len(branches) + len(robust) + len(CLAUDE_CASES) + 1 + hooks_n + 2} ケース通過")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
