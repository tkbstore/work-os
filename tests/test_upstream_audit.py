#!/usr/bin/env python3
"""上流の当て直しが、固定した版を測り、当て直せなかった版を見たことにしないことを検査する。

ネットワークには出ない。上流は手元に作った git リポ、`gh api` は答えを決めた偽物。

  python3 tests/test_upstream_audit.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
# E402（import が先頭に無い）の抑制。tests/ は上の sys.path の後でしか解決しない
from _registry import require  # noqa: E402

require("release_lanes.toml")
sys.path.insert(0, str(ROOT / "engine"))

# E402 の抑制。理由は上と同じで、engine/ は sys.path の後でしか解決しない
import calibrate  # noqa: E402
# E402 の抑制。理由は上と同じで、engine/ は sys.path の後でしか解決しない
import release_gate  # noqa: E402
# E402 の抑制。理由は上と同じで、engine/ は sys.path の後でしか解決しない
import upstream_audit as ua  # noqa: E402

Seen = ua.upstream.Seen
failed: list[str] = []


def check(desc: str, ok: bool, detail: str = "") -> None:
    if not ok:
        failed.append(desc + (f" — {detail}" if detail else ""))
    print(f"  {'OK ' if ok else 'NG '} {desc}{('  ' + detail) if detail and not ok else ''}")


def make_upstream(base: Path) -> list[str]:
    """2コミットの上流。2つ目で plugins/p/ を足す。到達できる SHA の取得を許す。"""
    up = base / "up" / "o" / "repo"
    up.mkdir(parents=True)

    def git(*args: str) -> str:
        return subprocess.run(["git", "-C", str(up), *args], check=True, capture_output=True,
                              text=True, timeout=30).stdout.strip()
    git("init", "-q")
    git("config", "uploadpack.allowReachableSHA1InWant", "true")
    shas = []
    for n, files in (("1", {"README.md": "# r\n"}),
                     ("2", {"LICENSE": "MIT\n", "plugins/p/README.md": "# p\n"})):
        for rel, body in files.items():
            (up / rel).parent.mkdir(parents=True, exist_ok=True)
            (up / rel).write_text(body, encoding="utf-8")
        git("add", "-A")
        # 利用者の core.hooksPath（コミットメッセージの検査など）を持ち込まない
        git("-c", "user.name=t", "-c", "user.email=t@example.com",
            "-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", n)
        shas.append(git("rev-parse", "HEAD"))
    return shas


def release(tag: str, sha: str):
    answers = {"repos/o/repo/releases/latest": {"tag_name": tag},
               f"repos/o/repo/commits/{tag}": {"sha": sha}}

    def fetch(path: str):
        if path not in answers:
            raise RuntimeError(f"gh api {path}: HTTP 404")
        return answers[path]
    return fetch


DECL = {"slug": "o/repo", "kind": "release"}


def main() -> int:
    lanes = release_gate.load_lanes()
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        old, new = make_upstream(base)
        calibrate.REMOTE = f"file://{base}/up/{{slug}}"
        audits = base / "audits"
        cache = base / "cache"

        print("固定した版を当て直す")
        res, nxt, err = ua.run([DECL], {}, cache, lanes, release("v1.0.0", old), audits)
        check("当て直せた", len(res) == 1 and not err, detail=str(err))
        check("固定したコミットを測った", bool(res) and res[0]["commit"] == old)
        check("検査ごとの判定が残る", bool(res) and bool(res[0]["states"]))
        check("当て直せた版を見たことにする", nxt.get("o/repo") == Seen("v1.0.0", old))
        ua.write_results(res, audits)

        res2, _, _ = ua.run([DECL], nxt, cache, lanes, release("v1.0.0", old), audits)
        check("同じ版を当て直し済みなら飛ばす", res2 == [])
        res3, _, _ = ua.run([DECL], nxt, base / "cache2", lanes, release("v1.0.0", old),
                            base / "empty")
        check("記録だけあって結果の無い版は当て直す", len(res3) == 1)

        print("\n新しい版")
        res4, nxt4, _ = ua.run([DECL], nxt, cache, lanes, release("v2.0.0", new), audits)
        check("キャッシュが古い版でも新しいコミットを測る",
              bool(res4) and res4[0]["commit"] == new)
        moved = ua.state_diff(res[0], res4[0]) if res and res4 else []
        check("LICENSE を足すと判定が動いたと出る",
              any("license" in m["check"].lower() for m in moved), detail=str(moved))
        before = ua.previous("o/repo", "v2.0.0", audits)
        check("前の版の結果を引ける", bool(before) and before["version"] == "v1.0.0")

        print("\n当て直せない版")
        res5, nxt5, err5 = ua.run([DECL], nxt4, cache, lanes, release("v3.0.0", "0" * 40), audits)
        check("取れないコミットは理由付きで返す",
              res5 == [] and [k for k, _ in err5] == ["o/repo"], detail=str(err5))
        check("当て直せなかった版は見たことにしない", nxt5.get("o/repo") == nxt4.get("o/repo"))

        print("\nモノレポの中の1つ")
        plug = {"slug": "o/repo", "kind": "path", "path": "plugins/p",
                "version_file": "plugins/p/plugin.json"}
        r = ua.audit(plug, Seen("0.1.0", new), cache, lanes)
        check("サブディレクトリに当てる", r["key"] == "o/repo:plugins/p" and bool(r["states"]))
        try:
            ua.audit(plug, Seen("0.0.1", old), cache, lanes)
            check("そのコミットに無いパスは当て直せないと言う", False)
        except RuntimeError as exc:
            check("そのコミットに無いパスは当て直せないと言う", "plugins/p" in str(exc))

        print("\nファイル名")
        check("鍵の / と : を潰す", ua.slot("o/mono:plugins/p") == "o__mono__plugins__p")
        check("版の / も潰す", ua.audit_path("a/b", "release/1.0", audits).name == "release__1.0.json")

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
