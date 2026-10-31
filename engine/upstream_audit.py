#!/usr/bin/env python3
"""上流の新しい版を、そのコミットに固定して公開ゲートで当て直す。

engine/upstream.py が見つけた版ごとに、engine/calibrate.py と同じ取得（clone のコミット固定）と
同じ観測（release_gate.evaluate）を当て、calibration/upstream/audits/<上流>/<版>.json に残す。
物差しを書き直さないのは calibrate.py と同じ理由で、本番と別の物差しで測った結果は
本番の水準について何も言わないため。

当て直せた版だけを「見た」として記録する。当て直せなかった版を見たことにすると、
次の回にもう試されない。

  python3 engine/upstream_audit.py                 新しい版を当て直して結果を出す（書かない）
  python3 engine/upstream_audit.py --write         結果と見た版を記録する
  python3 engine/upstream_audit.py --cache DIR     clone の置き場所（既定は一時領域）

終了コード: 0 = 全部当て直せた / 1 = 宣言が壊れている / 3 = 見られなかった・当て直せなかった
上流がある（当て直していないことを「変化なし」と同じ顔で出さない）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# E402（import が先頭に無い）の抑制。engine/ は上の sys.path の後でしか解決しない
import calibrate  # noqa: E402
# E402 の抑制。理由は上と同じで、engine/ は sys.path の後でしか解決しない
import release_gate  # noqa: E402
# E402 の抑制。理由は上と同じで、engine/ は sys.path の後でしか解決しない
import upstream  # noqa: E402

AUDITS_DIR = upstream.ROOT / "calibration" / "upstream" / "audits"
DEFAULT_MAX_MB = 400
UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def slot(text: str) -> str:
    """鍵や版をファイル名にする。`/` と `:` を潰す（openai/codex の rust-v0.166.0 など）。"""
    return UNSAFE.sub("__", text)


def audit_path(key: str, version: str, base: Path = AUDITS_DIR) -> Path:
    return base / slot(key) / f"{slot(version)}.json"


def audit(decl: dict, seen: upstream.Seen, cache: Path, lanes_cfg: dict) -> dict:
    """その版のコミットを取ってきてゲートを当てる。当てられなければ RuntimeError。"""
    slug = decl["slug"]
    dest = cache / slot(slug)
    err = calibrate.clone(slug, dest, int(decl.get("max_clone_mb", DEFAULT_MAX_MB)), seen.commit)
    if err:
        raise RuntimeError(f"{seen.commit[:12]} を取れない: {err}")
    got = calibrate.head_sha(dest)
    if got != seen.commit:
        raise RuntimeError(f"固定したコミットに揃わない（{got[:12]} を指している）")
    root = dest / decl["path"] if decl.get("kind") == "path" else dest
    if not root.is_dir():
        raise RuntimeError(f"{decl.get('path')} がそのコミットに無い")
    shape = str(decl.get("shape", "tool"))
    states, ok, reached = calibrate.observe(root, lanes_cfg, shape)
    if not states:
        raise RuntimeError("ゲートを当てられなかった")
    return {"key": upstream.key_of(decl), "version": seen.version, "commit": seen.commit,
            "shape": shape, "audited_on": date.today().isoformat(),
            "states": dict(sorted(states.items())), "publishable": bool(ok), "reached": reached}


def state_diff(before: dict, after: dict) -> list[dict]:
    """検査ごとの判定が動いたもの。片方にしか無い検査も出す（無かった → 有る も変化）。"""
    b, a = before.get("states", {}), after.get("states", {})
    return [{"check": c, "before": b.get(c, "—"), "after": a.get(c, "—")}
            for c in sorted(set(b) | set(a)) if b.get(c) != a.get(c)]


def previous(key: str, version: str, base: Path = AUDITS_DIR) -> dict | None:
    """同じ上流の、この版より前に残した結果のうち一番新しいもの。"""
    folder = base / slot(key)
    if not folder.is_dir():
        return None
    found = [json.loads(p.read_text(encoding="utf-8")) for p in folder.glob("*.json")]
    older = [r for r in found if r.get("version") != version]
    # 同じ日に複数の版を当てることがある（初回の基準づくり）。日付が同じなら版の数字で決める。
    return max(older, key=lambda r: (r.get("audited_on", ""), version_key(r.get("version", ""))),
               default=None)


def version_key(version: str) -> tuple[int, ...]:
    m = upstream.VERSION.search(version)
    return tuple(int(x or 0) for x in m.groups()) if m else ()


def run(decls: list[dict], seen: dict[str, upstream.Seen], cache: Path, lanes_cfg: dict,
        fetch: upstream.Fetch = upstream.gh_api,
        base: Path = AUDITS_DIR) -> tuple[list[dict], dict[str, upstream.Seen], list[tuple[str, str]]]:
    """新しい版を当て直す。返すのは（結果, 次の記録, 当て直せなかったもの）。"""
    changes, errors = upstream.survey(decls, seen, fetch)
    by_key = {upstream.key_of(d): d for d in decls}
    results: list[dict] = []
    done: list[upstream.Change] = []
    for c in changes:
        if c.step == "same" and audit_path(c.key, c.after.version, base).exists():
            continue
        try:
            results.append(audit(by_key[c.key], c.after, cache, lanes_cfg))
        except RuntimeError as exc:
            errors.append((c.key, str(exc)))
            continue
        done.append(c)
    return results, upstream.merged(seen, done), errors


def write_results(results: list[dict], base: Path = AUDITS_DIR) -> list[Path]:
    paths = []
    for r in results:
        p = audit_path(r["key"], r["version"], base)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(r, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        paths.append(p)
    return paths


def render(results: list[dict], errors: list[tuple[str, str]], base: Path = AUDITS_DIR) -> str:
    out = []
    for r in results:
        before = previous(r["key"], r["version"], base)
        moved = state_diff(before, r) if before else []
        was = f"{before['version']} から {len(moved)} 件動いた" if before else "初回"
        out.append(f"{r['key']} {r['version']}  到達 {r['reached'] or '—'}  （{was}）")
        out += [f"    {m['check']}: {m['before']} → {m['after']}" for m in moved]
    out += [f"当て直せず {k}: {why}" for k, why in errors]
    out.append(f"\n当て直した {len(results)} 件 / 当て直せなかった {len(errors)} 件")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="上流の新しい版を公開ゲートで当て直す")
    ap.add_argument("--write", action="store_true", help="結果と見た版を記録する")
    ap.add_argument("--cache", metavar="DIR", default="",
                    help="clone の置き場所。省略すると一時領域を使い、終了時に消す")
    args = ap.parse_args(argv)

    decls = upstream.load_decls()
    problems = upstream.lint_decls(decls)
    if problems:
        print("宣言に矛盾があります", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    lanes_cfg = release_gate.load_lanes()
    with tempfile.TemporaryDirectory() as tmp:
        cache = Path(args.cache).expanduser().resolve() if args.cache else Path(tmp)
        results, nxt, errors = run(decls, upstream.load_seen(), cache, lanes_cfg)
    print(render(results, errors))
    if args.write:
        write_results(results)
        upstream.write_seen(nxt)
    return upstream.SKIPPED if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
