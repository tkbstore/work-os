#!/usr/bin/env python3
"""doc_refs_resolve が「README が木に無い道を指したら落ち、そうでなければ通す」ことを検査する。

通ることは受け入れ条件ではない。各ケースは、同じ README のまま木のほうを壊して
（ファイルを消す・移す・公開から外す）落ちることを確かめる対になっている。

誤検知のケースは 2026-10-03 のフリート実測で実際に出た形から取った:
書き方の見本（path/to, my_）、別リポへ cd してから打つ手順、出力先の ./out、
一度も在ったことのない本文中の道（出力先・生成物の名前）。

  python3 tests/test_doc_refs.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "engine"))
# E402 は「import が先頭に無い」の指摘。engine/ を sys.path に挿した後でしか解決しない。
from release_gate import obs_doc_refs_resolve, tracked_files  # noqa: E402

GIT_ENV = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
CHK = {"id": "readme_refs_resolve", "kind": "doc_refs_resolve", "file": "README.md"}

failed: list[str] = []


def check(desc: str, ok: bool, detail: str = "") -> None:
    if not ok:
        failed.append(desc)
    print(f"  {'OK ' if ok else 'NG '} {desc}" + (f"  — {detail}" if not ok and detail else ""))


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, timeout=60,
                   env=GIT_ENV, capture_output=True)


def build(td: Path, files: dict[str, str]) -> Path:
    for rel, body in files.items():
        p = td / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    git(td, "init", "-q")
    git(td, "add", "-A")
    git(td, "commit", "-qm", "chore: first")
    return td


def observe(repo: Path, exclude: list[str] | None = None) -> tuple[str, str, list[str]]:
    ctx = {"files": tracked_files(repo, exclude or [])}
    return obs_doc_refs_resolve(repo, CHK, ctx)


def readme(*blocks: str) -> str:
    return "# t\n\n" + "\n\n".join(blocks) + "\n"


def sh(*lines: str) -> str:
    return "```bash\n" + "\n".join(lines) + "\n```"


def pair(desc: str, files: dict[str, str], break_tree) -> None:
    """壊す前は通り、木だけを壊すと落ちる。README は触らない。"""
    with tempfile.TemporaryDirectory() as td:
        repo = build(Path(td), files)
        state, detail, _ = observe(repo)
        check(f"{desc}: 揃っていれば通る", state == "pass", detail)
        exclude = break_tree(repo)
        state, detail, ex = observe(repo, exclude)
        check(f"{desc}: 木を壊すと落ちる", state == "fail", detail)
        check(f"{desc}: 当たった道と行を出す", bool(ex) and "README.md:" in ex[0], str(ex))


def passes(desc: str, files: dict[str, str], history=None) -> None:
    with tempfile.TemporaryDirectory() as td:
        repo = build(Path(td), files)
        if history:
            history(repo)
        state, detail, ex = observe(repo)
        check(desc, state == "pass", f"{detail} {ex}")


def removed(*paths: str):
    def run(repo: Path) -> None:
        git(repo, "rm", "-q", *paths)
        git(repo, "commit", "-qm", "chore: remove")
    return run


def moved(src: str, dst: str):
    def run(repo: Path) -> None:
        (repo / dst).parent.mkdir(parents=True, exist_ok=True)
        git(repo, "mv", src, dst)
        git(repo, "commit", "-qm", "refactor: move")
    return run


def main() -> int:
    print("手順の中のファイル")
    pair("python が呼ぶスクリプト",
         {"README.md": readme(sh("python3 scripts/run.py --dry")), "scripts/run.py": ""},
         lambda r: removed("scripts/run.py")(r))
    pair("行頭で実行する ./X",
         {"README.md": readme(sh("./setup.sh")), "setup.sh": ""},
         lambda r: removed("setup.sh")(r))
    pair("リポ内へ cd してから呼ぶ",
         {"README.md": readme(sh("cd app && python main.py")), "app/main.py": ""},
         lambda r: removed("app/main.py")(r))
    pair("公開から外した道を案内している",
         {"README.md": readme(sh("bash tools/deploy.sh")), "tools/deploy.sh": ""},
         lambda r: ["tools"])

    print("\n消した道への言及")
    pair("移したファイルを旧い道で書いている",
         {"README.md": readme("設定は `config/app.toml` に置く。"), "config/app.toml": ""},
         lambda r: moved("config/app.toml", "src/config/app.toml")(r))
    pair("移したディレクトリを旧い道で書いている",
         {"README.md": readme("本文は `content/pages/` に置く。"), "content/pages/a.md": ""},
         lambda r: moved("content/pages/a.md", "src/content/pages/a.md")(r))

    print("\n誤検知しない（2026-10-03 のフリート実測で出た形）")
    passes("書き方の見本（path/to, my_, <...>）",
           {"README.md": readme(sh("python path/to/x.py", "python my_script.py",
                                   "bash <your-script>.sh"))})
    passes("別リポへ cd してから打つ手順",
           {"README.md": readme(sh("cd ../sibling", "python scripts/merge.py"))})
    with tempfile.TemporaryDirectory() as td:
        # 親ディレクトリから打つ前提の手順（`cd 隣のリポ` に .. が付かない）。実測で在った形。
        (Path(td) / "sibling" / "scripts").mkdir(parents=True)
        repo = build(Path(td) / "repo", {
            "README.md": readme(sh("cd sibling", "python scripts/merge.py"))})
        state, detail, ex = observe(repo)
        check("隣に実在するリポへ .. 無しで cd する手順", state == "pass", f"{detail} {ex}")
    passes("git clone の直後に clone 先へ入る手順",
           {"README.md": readme(sh("git clone YOUR_REPOSITORY_URL", "cd prediction-points",
                                   "node server.js")),
            "server.js": ""})
    pair("clone 先へ入った後に呼ぶファイルは見る",
         {"README.md": readme(sh("git clone https://example.invalid/x.git", "cd x",
                                 "python3 run.py")), "run.py": ""},
         lambda r: removed("run.py")(r))
    passes("引数位置の ./out は出力先",
           {"README.md": readme(sh("python3 gen.py -o ./wiki-export")), "gen.py": ""})
    passes("一度も在ったことのない本文中の道（出力先）",
           {"README.md": readme("結果は `out/report.json` に出る。")})
    passes("コメント行とシェル以外のブロック",
           {"README.md": readme(sh("# python gone.py"),
                                "```python\nimport subprocess  # python gone.py\n```")})
    passes("ブロックを跨いで cd を持ち越さない",
           {"README.md": readme(sh("cd ../elsewhere"), sh("python run.py")), "run.py": ""})
    passes("消した後に同じ道へ戻したもの",
           {"README.md": readme("`lib/core.py` が本体。"), "lib/core.py": "x"},
           history=lambda r: (removed("lib/core.py")(r),
                              (r / "lib").mkdir(exist_ok=True),
                              (r / "lib/core.py").write_text("y"),
                              git(r, "add", "-A"), git(r, "commit", "-qm", "feat: back")))

    print("\n対象が無い")
    with tempfile.TemporaryDirectory() as td:
        repo = build(Path(td), {"a.txt": ""})
        state, _, _ = observe(repo)
        check("README が無ければ skip（無いことは readme_exists が見る）", state == "skip")

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
