#!/usr/bin/env python3
"""audit が「人の判定を効かせ、効かなくなったことを黙らない」ことを検査する。

抑制の仕掛けは、入れた瞬間から2つの壊れ方を抱える。detect-secrets の baseline に
実際に起きた壊れ方なので、両方を名指しで検査する。

  1) 行が動くと鍵が外れ、**抑制だけが静かに消える**（カバレッジの無言の低下）
  2) 歴史的な検出が**永久に免除される**（rubber-stamp）

1 は「20行挿入しても効く」で、2 は「leak は抑制しない」「理由の無い記録は効かない」
「死んだ記録を名指しする」で見る。抑制が広がらないこと（別の宣言・別のファイルには
効かない）も、鍵の形の検査として要る。

記録は WORKOS_REGISTRY を一時領域に向けて書く。本物の registry は触らない。

  python3 tests/test_audit.py
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
# E402 は「import が先頭に無い」の指摘。上で sys.path を挿してからでないと tests/ の
# 共通モジュールは解決しない。抑制しているのは順序の指摘だけである。
from _registry import borrow_terms, require  # noqa: E402

require("private_terms.toml", "release_lanes.toml", "secret_patterns.toml")
AUDIT = ROOT / "engine" / "audit.py"
GATE = ROOT / "engine" / "deliverable_gate.py"
REPO_GATE = ROOT / "engine" / "release_gate.py"

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}

REASON = "公開されている出典への引用であり、顧客のデータではない"
DECL_PREFIX = "clients.names:"
UNSETTLED = 3


def make_registry(td: Path) -> Path:
    """一時 registry。検査語の宣言だけを置く（本物は触らない）。"""
    reg = td / "registry"
    reg.mkdir(parents=True, exist_ok=True)
    src = Path(os.environ.get("WORKOS_REGISTRY", "")) if os.environ.get(
        "WORKOS_REGISTRY") else None
    from _registry import registry_root
    src = src or registry_root()
    for name in ("private_terms.toml", "release_lanes.toml", "secret_patterns.toml"):
        if (src / name).is_file():
            (reg / name).write_text((src / name).read_text(encoding="utf-8"),
                                    encoding="utf-8")
    return reg


def set_auditable(reg: Path, check_id: str, value: bool) -> None:
    """1つの観測の auditable の宣言を、在っても無くても宣言しなおす。

    engine ではなく宣言で切り替わることが要点。engine が観測ごとに決める形だと、
    何を人の判断に委ねたかが組織の外（コード）に移ってしまう。

    足す側だけを持っていたときは、**実物の宣言がまだ false であること**に暗黙に
    依存していた。2026-09-26 に実物へ auditable = true を入れた瞬間に2通りに壊れた:
    未宣言側の検査が前提を失って通らなくなり、宣言側では同じ表に同じキーが2つ
    並んで宣言そのものが読めなくなった。仕掛けは現在地に依存させない。
    """
    lanes = reg / "release_lanes.toml"
    out: list[str] = []
    inside = False
    for line in lanes.read_text(encoding="utf-8").splitlines():
        t = line.strip()
        if t.startswith("[["):
            inside = False
        if inside and t.startswith("auditable"):
            continue          # 既に在る宣言は落としてから入れなおす
        out.append(line)
        if t == f'id = "{check_id}"':
            inside = True
            if value:
                out.append("auditable = true")
    lanes.write_text("\n".join(out) + "\n", encoding="utf-8")


def term_counts(out: str) -> dict[str, int]:
    """no_private_terms の行から {語: 件数} を読む。

    件数を literal 文字列で当てると、木の中身が1ファイル増えただけで検査が壊れる
    （実際に一度壊した）。出力の意味の側を読む。
    """
    for line in out.splitlines():
        if "no_private_terms:" in line and "clients.names:" in line:
            body = line.split("clients.names:", 1)[1]
            return {t: int(n) for t, n in re.findall(r"([A-Za-z0-9\-]+) (\d+) 件", body)}
    return {}


def run_repo_gate(reg: Path, repo: Path) -> tuple[int, str]:
    proc = subprocess.run([sys.executable, str(REPO_GATE), str(repo), "--owned"],
                          capture_output=True, text=True, timeout=300, env=env(reg))
    return proc.returncode, proc.stdout + proc.stderr


def make_repo(td: Path, name: str, body: str, extra: dict | None = None) -> Path:
    repo = td / name
    (repo / "deliverables").mkdir(parents=True, exist_ok=True)
    (repo / "work.toml").write_text(
        '[repo]\nname = "sample"\n\n[[deliverable]]\npath = "deliverables/"\n',
        encoding="utf-8")
    (repo / "deliverables" / "report.md").write_text(body, encoding="utf-8")
    for rel, text in (extra or {}).items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(repo)], check=True, timeout=30)
    return repo


def env(reg: Path) -> dict:
    return {**os.environ, **GIT_ENV, "WORKOS_REGISTRY": str(reg)}


def run_gate(reg: Path, repo: Path) -> tuple[int, str]:
    proc = subprocess.run([sys.executable, str(GATE), str(repo)],
                          capture_output=True, text=True, timeout=180, env=env(reg))
    return proc.returncode, proc.stdout + proc.stderr


def run_audit(reg: Path, *args: str) -> tuple[int, str]:
    proc = subprocess.run([sys.executable, str(AUDIT), *args],
                          capture_output=True, text=True, timeout=120, env=env(reg))
    return proc.returncode, proc.stdout + proc.stderr


def clear(reg: Path, repo: Path, target: str, decl: str,
          why: str = REASON, flag: str = "--clear") -> tuple[int, str]:
    return run_audit(reg, flag, str(repo), target, decl, "--why", why, "--who", "t")


def main() -> int:
    failed: list[str] = []

    def check(desc: str, ok: bool) -> None:
        print(f"  {'OK ' if ok else 'NG '} {desc}")
        if not ok:
            failed.append(desc)

    one, two = borrow_terms(2)
    decl_one, decl_two = DECL_PREFIX + one, DECL_PREFIX + two
    # 2社が在る成果物。片方を抑制すると宛先が確定する、という形にしてある。
    body = f"# {one} 様向け\n\n参考: {two} の公開資料\n"
    target = "deliverables/report.md:3"

    with tempfile.TemporaryDirectory() as tmpdir:
        td = Path(tmpdir)
        reg = make_registry(td)

        # --- 抑制が効く ------------------------------------------------------
        print("\n判定を記録すると抑制が効く")
        repo = make_repo(td, "basic", body)
        code, out = run_gate(reg, repo)
        check("記録前は 2 社で止まる", code == 1 and "社内止まり" in out)
        rc, _ = clear(reg, repo, target, decl_two)
        check("記録できる", rc == 0)
        code, out = run_gate(reg, repo)
        check("記録後は宛先が確定する", code == 0 and one in out)
        check("抑制した件数を必ず出す", "人の判定で抑制" in out)
        check("記録の規模を毎回出す", "判定の記録 1 件" in out)

        # --- 行が動いても効く（detect-secrets の壊れ方1） ----------------------
        print("\n行が動いても効く（鍵は行番号ではない）")
        moved = repo / "deliverables" / "report.md"
        moved.write_text("<!-- 追記 -->\n" * 20 + body, encoding="utf-8")
        code, out = run_gate(reg, repo)
        check("20 行挿入しても抑制が効く", code == 0 and "人の判定で抑制" in out)
        check("死んだ記録として報告しない", "もう効いていない" not in out)

        # --- 行の中身が変わると失効する --------------------------------------
        print("\n行の中身が変わると失効する")
        moved.write_text(body.replace("公開資料", "非公開の内部資料"), encoding="utf-8")
        code, out = run_gate(reg, repo)
        check("抑制が外れて止まる", code == 1 and "社内止まり" in out)
        check("死んだ記録を名指しする", "もう効いていない" in out)

        # --- leak は抑制しない（壊れ方2） --------------------------------------
        print("\n混入として記録しても抑制はしない")
        leaky = make_repo(td, "leaky", body)
        rc, _ = clear(reg, leaky, target, decl_two, flag="--leak")
        check("記録できる", rc == 0)
        code, out = run_gate(reg, leaky)
        check("止まったままである", code == 1 and "社内止まり" in out)
        check("記録は免除ではないと言う", "抑制していません" in out)

        # --- 理由の無い記録は効かない ----------------------------------------
        print("\n理由の無い記録")
        rc, out = clear(reg, repo, target, decl_two, why="ok")
        check("短い理由は受け付けない（exit 2）", rc == 2)
        check("何文字必要かを言う", "12 文字以上" in out)

        # --- 抑制は形で広がらない --------------------------------------------
        # 1件の記録が効くのは1つの (path, 宣言, 行の本文) だけ。別の宣言・別の
        # ファイルに effect が漏れると、何を通したか分からなくなる。
        print("\n抑制は形で広がらない")
        wide = make_repo(td, "wide", body)
        rc, _ = clear(reg, wide, target, decl_two)
        code, out = run_gate(reg, wide)
        check("同じ行の別の宣言は抑制しない（宛先は残る）",
              code == 0 and f"{one} " in out)
        same = make_repo(td, "same", body)
        code, out = run_gate(reg, same)
        check("同じ本文でも別のリポは抑制しない", code == 1)

        other = make_repo(td, "other", "x\n",
                          {"deliverables/copy.md": body})
        rc, _ = clear(reg, other, "deliverables/copy.md:3", decl_two)
        code, out = run_gate(reg, other)
        check("path が鍵に入っている（別ファイルは別の判定）",
              "人の判定で抑制" in out)

        # --- 手で書いた不正な記録 ---------------------------------------------
        # CLI は短い理由を弾くが、**ファイルは手で書ける**。抑制の判断は読む側に
        # も要る。無効な記録が黙って抑制すると、理由を要求した意味が消える。
        print("\n手で書いた不正な記録")
        hand = make_repo(td, "hand", body)
        rc, out = clear(reg, hand, target, decl_two)
        # 鍵は1行目の末尾。出力全体の末尾は置き場のパスである。
        good_key = out.splitlines()[0].split()[-1] if rc == 0 else ""
        audit_toml = reg / "audit.toml"
        text = audit_toml.read_text(encoding="utf-8")
        # 同じ鍵・同じ対象で、理由だけを削った記録に差し替える
        audit_toml.write_text(
            text.replace(f'why = "{REASON}"', 'why = "ok"'), encoding="utf-8")
        code, out = run_gate(reg, hand)
        check("理由の無い記録は抑制しない", code == 1 and "社内止まり" in out)
        rc, listing = run_audit(reg, "--list")
        check("無効な記録だと言う", "効いていません" in listing)
        check("何が足りないか言う", "理由が短すぎる" in listing)
        # 鍵の形が違う記録。抑制しないこと自体は鍵が一致しないので自明なので、
        # ここで確かめるのは **黙って積まれないこと** である。形の違う鍵は
        # 「何も抑制しない記録」として静かに増えるのが本来の壊れ方で、
        # 指摘されなければ書いた人は効いていると思い続ける。
        audit_toml.write_text(
            text.replace(f'line_key = "{good_key}"', 'line_key = "zzz"'),
            encoding="utf-8")
        rc, listing = run_audit(reg, "--list")
        check("形の違う鍵を無効として名指しする", "鍵の形が違う" in listing)
        check("効いていないことを言う", "効いていません" in listing)
        audit_toml.write_text(text, encoding="utf-8")

        # --- release_gate 側: 宣言していなければ何も変わらない -------------------
        # 抑制を受け付けるかは観測ごとの宣言で決まる。既定（宣言なし）で挙動が
        # 変わると、抑制を導入していない組織にも「cleared と書けば通る」経路が
        # 生まれる。既定のままで抑制が effect を持たないことを先に固定する。
        print("\nrelease_gate（auditable を宣言していないとき）")
        # 「宣言していない」を仕掛け側で明示する。実物の宣言がどちらであっても
        # この場の前提が動かないようにする（実物は 2026-09-26 に true になった）。
        set_auditable(reg, "no_private_terms", False)
        pub = ('[repo]\nname = "s"\nenforcement = "warn"\n'
               '[publish]\nintent = "internal"\n')
        rg = make_repo(td, "rg", body)
        (rg / "work.toml").write_text(pub, encoding="utf-8")
        (rg / "src").mkdir(exist_ok=True)
        (rg / "src" / "note.py").write_text(f'A = "{one}"\nB = "{two}"\n',
                                           encoding="utf-8")
        subprocess.run(["git", "-C", str(rg), "add", "-A"], check=True, timeout=60)
        subprocess.run(["git", "-C", str(rg), "commit", "-qm", "chore: first"],
                       check=True, timeout=60, env=env(reg))
        # 記録する **前** の件数を取る。「0 より大きい」では判別できない
        # （抑制されて 2→1 になっても真になる）。比較の基準は記録前の実測にする。
        code, out = run_repo_gate(reg, rg)
        pristine = term_counts(out)
        check("記録前に両方の語が当たっている",
              pristine.get(one, 0) > 0 and pristine.get(two, 0) > 0)
        rc, _ = clear(reg, rg, "src/note.py:1", decl_one)
        check("記録できる", rc == 0)
        code, out = run_repo_gate(reg, rg)
        before = term_counts(out)
        check("宣言していない観測は1件も抑制されない", before == pristine)
        check("抑制の注記も出ない", "人の判定で抑制" not in out)

        # --- release_gate 側: 宣言すると効く ------------------------------------
        print("\nrelease_gate（auditable を宣言したとき）")
        set_auditable(reg, "no_private_terms", True)
        code, out = run_repo_gate(reg, rg)
        after = term_counts(out)
        check("宣言した観測は抑制される（1 件減る）",
              after.get(one, 0) == before.get(one, 0) - 1)
        check("抑制していない語は1件も減らない",
              after.get(two, 0) == before.get(two, 0))
        check("抑制した件数を注記に出す", "人の判定で抑制" in out)
        check("記録の規模を出す", "判定の記録" in out)

        # --- 記録が読めないとき ----------------------------------------------
        print("\n記録が読めないとき")
        (reg / "audit.toml").write_text("[[entry]]\nrepo = \n", encoding="utf-8")
        code, out = run_gate(reg, repo)
        check("読めなかったことを言う", "記録を読めません" in out)
        check("抑制が効いていないと言う", "抑制は1件も効きません" in out
              or "抑制は1件も効いていません" in out)

    print()
    if failed:
        print(f"NG {len(failed)} 件")
        for desc in failed:
            print(f"  - {desc}")
        return 1
    print("全ケース通過")
    return 0


if __name__ == "__main__":
    sys.exit(main())
