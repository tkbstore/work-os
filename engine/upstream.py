#!/usr/bin/env python3
"""上流の新しい版を見つける。見つけた版を公開ゲートで当て直す入口になる。

calibration/upstream.toml に宣言した上流ごとに、いまの版とそのコミットを取り、
calibration/upstream/seen.json に残した前回の版と比べる。ここがするのは検知だけで、
当て直し（engine/calibrate.py の clone / --pin と同じコミットの固定）と、
差分から検査の候補を作るのは後段の仕事。

定期実行は launchd に頼らない。前の日次観測は macOS の TCC に ~/Documents を
読むのを止められ、記録された9回はすべて手動だった（2026-08-31〜10-13）。

  python3 engine/upstream.py           新しい版を出す（記録は変えない）
  python3 engine/upstream.py --write   見た版を記録する
  python3 engine/upstream.py --json    機械向けの出力

終了コード: 0 = 全部見られた / 1 = 宣言が壊れている / 3 = 見られなかった上流がある
（見られなかったことを「新しい版は無い」と同じ顔で出さない）。
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))

# E402 の抑制。理由は上と同じで、engine/ は sys.path の後でしか解決しない
from workos import _load_toml  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DECL_FILE = ROOT / "calibration" / "upstream.toml"
SEEN_FILE = ROOT / "calibration" / "upstream" / "seen.json"
KINDS = ("release", "path")
SKIPPED = 3
GH_TIMEOUT = 60
# タグには接頭辞が付くことがある（openai/codex は rust-v0.166.0）。数字の並びだけを見る。
VERSION = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")

Fetch = Callable[[str], dict | list]


@dataclass(frozen=True)
class Seen:
    version: str
    commit: str


@dataclass(frozen=True)
class Change:
    key: str
    before: Seen | None
    after: Seen
    # first（初めて見た）/ major / minor / patch / same / unparsed（版の形が読めない）
    step: str


# --------------------------------------------------------------------------- #
# 宣言
# --------------------------------------------------------------------------- #

def load_decls(path: Path = DECL_FILE) -> list[dict]:
    return list(_load_toml(path).get("upstream", []))


def lint_decls(decls: list[dict]) -> list[str]:
    problems: list[str] = []
    seen: set[str] = set()
    if not decls:
        problems.append("upstream が空")
    for d in decls:
        slug, kind = str(d.get("slug", "")), str(d.get("kind", ""))
        if slug.count("/") != 1:
            problems.append(f"slug の形が違う: {slug!r}")
        if kind not in KINDS:
            problems.append(f"{slug}: kind は {' / '.join(KINDS)} のどれか（{kind!r}）")
        if kind == "path" and not (d.get("path") and d.get("version_file")):
            problems.append(f"{slug}: kind = path には path と version_file が要る")
        k = key_of(d)
        if k in seen:
            problems.append(f"{k} が二度宣言されている")
        seen.add(k)
    return problems


def key_of(decl: dict) -> str:
    """記録の鍵。モノレポの中の1つは、リポとパスの組で区別する。"""
    slug = str(decl.get("slug", ""))
    return f"{slug}:{decl['path']}" if decl.get("kind") == "path" else slug


# --------------------------------------------------------------------------- #
# 取得
# --------------------------------------------------------------------------- #

def gh_api(path: str) -> dict | list:
    """`gh api` の JSON。失敗は理由付きの RuntimeError にする。"""
    try:
        proc = subprocess.run(["gh", "api", path], capture_output=True, text=True,
                              timeout=GH_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise RuntimeError(f"gh api {path}: {exc}") from exc
    if proc.returncode != 0:
        lines = (proc.stderr or "").strip().splitlines()
        raise RuntimeError(f"gh api {path}: {lines[-1] if lines else '失敗'}")
    return json.loads(proc.stdout)


def current(decl: dict, fetch: Fetch = gh_api) -> Seen:
    slug = decl["slug"]
    if decl["kind"] == "release":
        tag = str(fetch(f"repos/{slug}/releases/latest")["tag_name"])
        sha = str(fetch(f"repos/{slug}/commits/{quote(tag, safe='')}")["sha"])
        return Seen(version=tag, commit=sha)
    commits = fetch(f"repos/{slug}/commits?path={quote(decl['path'])}&per_page=1")
    if not commits:
        raise RuntimeError(f"{slug}: {decl['path']} を変えたコミットが無い")
    sha = str(commits[0]["sha"])
    body = fetch(f"repos/{slug}/contents/{quote(decl['version_file'])}?ref={sha}")
    meta = json.loads(base64.b64decode(body["content"]).decode("utf-8"))
    if not meta.get("version"):
        raise RuntimeError(f"{slug}: {decl['version_file']} に version が無い")
    return Seen(version=str(meta["version"]), commit=sha)


# --------------------------------------------------------------------------- #
# 比較
# --------------------------------------------------------------------------- #

def step_of(before: Seen | None, after: Seen) -> str:
    if before is None:
        return "first"
    if before == after:
        return "same"
    a, b = VERSION.search(before.version), VERSION.search(after.version)
    if not (a and b):
        return "unparsed"
    old = tuple(int(x or 0) for x in a.groups())
    new = tuple(int(x or 0) for x in b.groups())
    if new[0] != old[0]:
        return "major"
    if new[1] != old[1]:
        return "minor"
    # 版の文字列が同じでもコミットが動いたもの（タグの付け直し）も patch に数える
    return "patch"


def load_seen(path: Path = SEEN_FILE) -> dict[str, Seen]:
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {k: Seen(version=str(v["version"]), commit=str(v["commit"])) for k, v in raw.items()}


def write_seen(seen: dict[str, Seen], path: Path = SEEN_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {k: {"version": v.version, "commit": v.commit} for k, v in sorted(seen.items())}
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def survey(decls: list[dict], seen: dict[str, Seen],
           fetch: Fetch = gh_api) -> tuple[list[Change], list[tuple[str, str]]]:
    """宣言した上流を全部見る。見られなかったものは理由と一緒に別に返す。"""
    changes: list[Change] = []
    errors: list[tuple[str, str]] = []
    for d in decls:
        k = key_of(d)
        try:
            now = current(d, fetch)
        except (RuntimeError, KeyError, ValueError) as exc:
            errors.append((k, str(exc)))
            continue
        changes.append(Change(key=k, before=seen.get(k), after=now,
                              step=step_of(seen.get(k), now)))
    return changes, errors


# --------------------------------------------------------------------------- #

def render(changes: list[Change], errors: list[tuple[str, str]]) -> str:
    out = []
    for c in changes:
        was = c.before.version if c.before else "—"
        out.append(f"{c.step:<8} {c.key}  {was} → {c.after.version}  ({c.after.commit[:12]})")
    for k, why in errors:
        out.append(f"{'見られず':<8} {k}  {why}")
    moved = [c for c in changes if c.step not in ("same", "first")]
    out.append(f"\n新しい版 {len(moved)} 件 / 初回 {sum(c.step == 'first' for c in changes)} 件 / "
               f"見られなかった {len(errors)} 件")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="上流の新しい版を見つける")
    ap.add_argument("--write", action="store_true", help="見た版を記録する")
    ap.add_argument("--json", action="store_true", help="機械向けの出力")
    args = ap.parse_args(argv)

    decls = load_decls()
    problems = lint_decls(decls)
    if problems:
        print("宣言に矛盾があります", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    seen = load_seen()
    changes, errors = survey(decls, seen)
    if args.json:
        print(json.dumps({
            "changes": [{"key": c.key, "step": c.step,
                         "before": c.before.__dict__ if c.before else None,
                         "after": c.after.__dict__} for c in changes],
            "errors": [{"key": k, "why": w} for k, w in errors],
        }, ensure_ascii=False, indent=2))
    else:
        print(render(changes, errors))
    if args.write:
        # 見られなかった上流は前回の記録を残す。消すと次回「初回」に化ける。
        write_seen({**seen, **{c.key: c.after for c in changes}})
    return SKIPPED if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
