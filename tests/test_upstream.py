#!/usr/bin/env python3
"""上流の新しい版の検知が、見落とさず、見られなかったことを隠さないことを検査する。

ネットワークには出ない。`gh api` の代わりに、答えを決めた偽物を渡す。

  python3 tests/test_upstream.py
"""

from __future__ import annotations

import base64
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "engine"))

# E402（import が先頭に無い）の抑制。engine/ は上の sys.path の後でしか解決しない
import upstream  # noqa: E402

Seen = upstream.Seen
failed: list[str] = []


def check(desc: str, ok: bool, detail: str = "") -> None:
    if not ok:
        failed.append(desc + (f" — {detail}" if detail else ""))
    print(f"  {'OK ' if ok else 'NG '} {desc}{('  ' + detail) if detail and not ok else ''}")


def fake(answers: dict[str, object]):
    """決めた答えを返す。決めていない問い合わせは、gh api の失敗と同じ形で落とす。"""
    def fetch(path: str):
        if path not in answers:
            raise RuntimeError(f"gh api {path}: HTTP 404")
        return answers[path]
    return fetch


RELEASE = {"slug": "o/r", "kind": "release"}
PLUGIN = {"slug": "o/mono", "kind": "path", "path": "plugins/p",
          "version_file": "plugins/p/.claude-plugin/plugin.json"}


def plugin_answers(version: str, sha: str) -> dict[str, object]:
    content = base64.b64encode(json.dumps({"version": version}).encode()).decode()
    return {
        "repos/o/mono/commits?path=plugins/p&per_page=1": [{"sha": sha}],
        f"repos/o/mono/contents/plugins/p/.claude-plugin/plugin.json?ref={sha}": {"content": content},
    }


def check_decls() -> None:
    print("宣言")
    real = upstream.load_decls()
    check("calibration/upstream.toml に矛盾が無い", not upstream.lint_decls(real),
          detail=str(upstream.lint_decls(real)))
    check("空の宣言を通さない", bool(upstream.lint_decls([])))
    check("path には version_file が要る",
          any("version_file" in p for p in upstream.lint_decls([{"slug": "a/b", "kind": "path",
                                                                  "path": "x"}])))
    check("知らない kind を通さない",
          any("kind" in p for p in upstream.lint_decls([{"slug": "a/b", "kind": "tag"}])))
    check("同じものの二重宣言を通さない", bool(upstream.lint_decls([RELEASE, RELEASE])))
    check("モノレポの中の別々のパスは別物として数える",
          not upstream.lint_decls([PLUGIN, {**PLUGIN, "path": "plugins/q"}]))


def check_steps() -> None:
    print("\n版の段")
    s = upstream.step_of
    check("前回が無ければ first", s(None, Seen("v1.0.0", "a")) == "first")
    check("同じなら same", s(Seen("v1.0.0", "a"), Seen("v1.0.0", "a")) == "same")
    check("major", s(Seen("v1.9.9", "a"), Seen("v2.0.0", "b")) == "major")
    check("minor", s(Seen("v2.1.0", "a"), Seen("v2.2.0", "b")) == "minor")
    check("patch", s(Seen("v2.1.315", "a"), Seen("v2.1.316", "b")) == "patch")
    # openai/codex のタグには接頭辞が付く
    check("接頭辞付きのタグも読む", s(Seen("rust-v0.165.0", "a"), Seen("rust-v0.166.0", "b")) == "minor")
    check("読めない版は unparsed と言う", s(Seen("nightly", "a"), Seen("latest", "b")) == "unparsed")
    check("同じ版でコミットが動いたら見落とさない",
          s(Seen("v1.0.0", "a"), Seen("v1.0.0", "b")) != "same")


def check_survey() -> None:
    print("\n上流を見る")
    answers = {"repos/o/r/releases/latest": {"tag_name": "v2.0.0"},
               "repos/o/r/commits/v2.0.0": {"sha": "s2"},
               **plugin_answers("0.12.0", "p2")}
    seen = {"o/r": Seen("v1.4.0", "s1"), "o/mono:plugins/p": Seen("0.11.0", "p1")}
    changes, errors = upstream.survey([RELEASE, PLUGIN], seen, fake(answers))
    by = {c.key: c for c in changes}
    check("リリースの major を見つける", bool(by.get("o/r")) and by["o/r"].step == "major")
    check("リリースのコミットを取る", bool(by.get("o/r")) and by["o/r"].after.commit == "s2")
    check("モノレポの中の1つの版を version_file から読む",
          by.get("o/mono:plugins/p") is not None
          and by["o/mono:plugins/p"].after == Seen("0.12.0", "p2"))
    check("全部見られたら errors は空", errors == [])

    # リリースの無い上流: 「新しい版は無い」ではなく「見られなかった」と言う
    changes, errors = upstream.survey([RELEASE, PLUGIN], seen, fake(plugin_answers("0.11.0", "p1")))
    check("見られなかった上流を理由付きで返す",
          [k for k, _ in errors] == ["o/r"] and "404" in errors[0][1])
    check("見られなかった上流を変化として出さない", all(c.key != "o/r" for c in changes))
    nxt = upstream.merged(seen, changes)
    check("見られなかった上流は前回の記録を残す", nxt.get("o/r") == Seen("v1.4.0", "s1"))

    content = base64.b64encode(b"{}").decode()
    no_version = {**plugin_answers("x", "p3"),
                  "repos/o/mono/contents/plugins/p/.claude-plugin/plugin.json?ref=p3":
                      {"content": content}}
    _, errors = upstream.survey([PLUGIN], {}, fake(no_version))
    check("version の無い version_file は見られなかったと言う",
          bool(errors) and "version" in errors[0][1])


def check_record() -> None:
    print("\n記録")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "seen.json"
        check("記録が無ければ空", upstream.load_seen(path) == {})
        rec = {"o/r": Seen("v2.0.0", "s2"), "o/mono:plugins/p": Seen("0.12.0", "p2")}
        upstream.write_seen(rec, path)
        check("書いた記録を読み戻せる", upstream.load_seen(path) == rec)


def main() -> int:
    check_decls()
    check_steps()
    check_survey()
    check_record()
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
