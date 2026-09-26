#!/usr/bin/env python3
r"""宣言していないことが「そう宣言した」に化けないかを検査する。

`load_repo` は work.toml が **宣言している** 内容を返す。宣言の無い欄に
"unknown" や "client" のようなもっともらしい既定値を入れると、読む側には
不在と宣言の区別がつかなくなる。そして台帳（registry.py）は宣言を推測より
優先するので、**もっともらしい既定値が、名前からの正しい推測を負かす**。

実測 2026-09-26（work-os-registry のセッションからの報告を再現）:
`[publish]` だけを持ち `[repo]` が無いリポジトリが、台帳の再生成で、名前から
正しく引けていたドメインと role="site" を失い、domain="unknown" / role="client"
へ落ちた。リポも名前も変わっていない。work.toml を置いたことだけが原因で、
**導入するほど台帳の分類が悪くなる**向きになっていた。

  python3 tests/test_declaration_absence.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "engine"))
from workos import load_repo  # noqa: E402

failed: list[str] = []


def check(desc: str, ok: bool, detail: str = "") -> None:
    if not ok:
        failed.append(desc)
    print(f"  {'OK ' if ok else 'NG '} {desc}{('  — ' + detail) if detail and not ok else ''}")


def make_repo(root: Path, name: str, work: str | None) -> Path:
    repo = root / name
    (repo / ".git").mkdir(parents=True, exist_ok=True)
    if work is not None:
        (repo / "work.toml").write_text(work, encoding="utf-8")
    return repo


def registry_out(root: Path, reg: Path) -> str:
    env = dict(os.environ, WORKOS_REGISTRY=str(reg))
    r = subprocess.run([sys.executable, str(ROOT / "engine" / "registry.py"), str(root)],
                       capture_output=True, text=True, env=env, timeout=120)
    return r.stdout


def domain_of(out: str, repo_name: str) -> str:
    """台帳の出力から、そのリポがどのドメインの節に入ったかを読む。"""
    current = ""
    for line in out.splitlines():
        if line.startswith("[domain."):
            current = line[len("[domain."):].rstrip("]")
        elif f'name = "{repo_name}"' in line:
            return current
    return "(出てこない)"


def role_of(out: str, repo_name: str) -> str:
    for line in out.splitlines():
        if f'name = "{repo_name}"' in line:
            return line.split('role = "')[1].split('"')[0]
    return "(出てこない)"


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        print("不在は不在のまま返す（もっともらしい既定値で埋めない）")
        repo = make_repo(tmp / "a", "acme-alpha", '[publish]\nintent = "internal"\n')
        got = load_repo(repo)
        check("[repo] が無ければ domain は空", got is not None and got.domain == "",
              f"domain={got.domain!r}" if got else "load_repo が None")
        check("[repo] が無ければ role は空", got is not None and got.role == "",
              f"role={got.role!r}" if got else "load_repo が None")

        declared = make_repo(tmp / "a", "acme-gamma",
                             '[repo]\ndomain = "other"\nrole = "tool"\n')
        got = load_repo(declared)
        check("宣言が在ればそれを返す",
              got is not None and (got.domain, got.role) == ("other", "tool"))

        print("\n台帳 — 宣言していない欄は、名前からの推測に委ねる")
        reg = tmp / "registry"
        reg.mkdir()
        (reg / "naming.toml").write_text(
            '[domain_rules]\n"acme-" = "acme"\n', encoding="utf-8")

        root = tmp / "tree"
        make_repo(root, "acme-alpha", '[publish]\nintent = "internal"\n')   # work.toml 在り
        make_repo(root, "acme-beta", None)                                  # work.toml 無し
        make_repo(root, "acme-gamma", '[repo]\ndomain = "other"\nrole = "tool"\n')
        make_repo(root, "acme-delta_lp", '[publish]\nintent = "internal"\n')
        out = registry_out(root, reg)

        beta = domain_of(out, "acme-beta")
        check("work.toml が無いリポは名前から分類される", beta == "acme", f"→ {beta}")
        alpha = domain_of(out, "acme-alpha")
        check("work.toml は在るが [repo] が無いリポも、同じ分類になる",
              alpha == beta, f"acme-alpha → {alpha} / acme-beta → {beta}")
        gamma = domain_of(out, "acme-gamma")
        check("宣言が在れば推測より優先される", gamma == "other", f"→ {gamma}")

        # role も同じ経路で負ける。_lp 接尾辞は site と推測されるはずだった。
        delta = role_of(out, "acme-delta_lp")
        check("role も宣言が無ければ推測に委ねる", delta == "site", f"→ {delta}")
        check("宣言された role は残る", role_of(out, "acme-gamma") == "tool")

        print("\n台帳 — 推測で埋めた欄を、値と同じ粒度で出す")
        # ✓ は「work.toml を持つ」であって「分類を宣言した」ではない。2つを1つの
        # 数に畳むと、何も分類を宣言していないリポが採用率の分子に入ったまま見えなくなる。
        check("work.toml を持つ数は 3", "work.toml を持つ 3 / 4" in out,
              [ln for ln in out.splitlines() if "work.toml を持つ" in ln])
        check("[repo] まで宣言した数は 1", "[repo] まで宣言 1 / 4" in out,
              [ln for ln in out.splitlines() if "まで宣言" in ln])
        alpha_line = next(ln for ln in out.splitlines() if '"acme-alpha"' in ln)
        check("推測で埋めた欄を名指しする",
              "domain=推測" in alpha_line and "role=推測" in alpha_line, alpha_line)
        gamma_line = next(ln for ln in out.splitlines() if '"acme-gamma"' in ln)
        check("宣言された欄には推測と書かない", "推測" not in gamma_line, gamma_line)
        beta_line = next(ln for ln in out.splitlines() if '"acme-beta"' in ln)
        check("work.toml が無いリポは ✓ も推測注記も付けない",
              "✓" not in beta_line and "推測" not in beta_line, beta_line)

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
