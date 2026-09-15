#!/usr/bin/env python3
"""work.toml / capabilities.toml が憲法に適合しているかを検証する。

  python3 engine/validate.py [repo_path ...]

引数なしならカレントディレクトリ。終了コードは enforcement に従う（warn なら常に 0）。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from promotion import collect, satisfies_rule_of_three  # noqa: E402
from workos import (  # noqa: E402
    LAYERS,
    PROMOTION_ORDER,
    RULE_OF_THREE,
    STATUSES,
    Report,
    load_repo,
)

VALID_ROLES = {"kernel", "domain", "client", "tool", "site", "archive"}


def validate(root: Path) -> int:
    repo = load_repo(root)
    print(f"\n[{root.name}]")
    if repo is None:
        print("  skip  work.toml がありません（engine/adopt.py で生成できます）")
        return 0

    rep = Report(repo.enforcement)

    # --- repo セクション ---------------------------------------------------
    if repo.role not in VALID_ROLES:
        rep.error(f"repo.role='{repo.role}' は不正。{sorted(VALID_ROLES)} のいずれか")
    if repo.enforcement not in {"warn", "block"}:
        rep.error(f"repo.enforcement='{repo.enforcement}' は不正。'warn' か 'block'")
    if repo.domain == "unknown":
        rep.warn("repo.domain が未設定。どの仕事に属するかを書くと scan で集計される")

    # --- layers ------------------------------------------------------------
    for layer in repo.layers:
        if layer not in LAYERS:
            rep.error(f"[layers] に未知の層 '{layer}'。{list(LAYERS)} のいずれか")
    for layer in ("kernel", "golden_path", "config", "extension"):
        for pattern in repo.layers.get(layer, []):
            if not (repo.root / pattern).exists():
                rep.warn(f"{layer}: '{pattern}' が存在しない（消したなら work.toml からも消す）")

    # --- capabilities ------------------------------------------------------
    seen: set[str] = set()
    for cap in repo.capabilities:
        tag = f"capability '{cap.id}'"
        if not cap.id:
            rep.error("id のない capability がある")
            continue
        if cap.id in seen:
            rep.error(f"{tag} が重複している")
        seen.add(cap.id)

        if cap.layer not in LAYERS:
            rep.error(f"{tag}: layer='{cap.layer}' が不正")
        if cap.status not in STATUSES:
            rep.error(f"{tag}: status='{cap.status}' が不正")
        if cap.path and not (repo.root / cap.path).exists():
            rep.warn(f"{tag}: path='{cap.path}' が存在しない")

        # 憲法 §4 昇格レーンの検証
        if cap.status in ("proposed", "stable", "kernel"):
            # 数え方は promotion.py にしか置かない。promote.py と同じ規則を使う。
            ok, why = satisfies_rule_of_three(collect(cap.used_by, note=cap.note))
            if not ok:
                rep.error(
                    f"{tag}: status='{cap.status}' なのに {why}（3社ルール違反）。"
                    "例外なら note に理由を書く"
                )
        if cap.status in ("stable", "kernel") and not cap.invariant:
            rep.error(f"{tag}: status='{cap.status}' なら invariant の宣言が必須")
        if cap.status == "kernel" and cap.layer != "kernel":
            rep.error(f"{tag}: status='kernel' なのに layer='{cap.layer}'")
        if cap.layer == "kernel" and cap.status != "kernel":
            rep.warn(f"{tag}: layer='kernel' だが status='{cap.status}'。昇格を完了させる")

    # --- 層に属するが未登録のパス -------------------------------------------
    registered = {c.path.rstrip("/") for c in repo.capabilities if c.path}
    for layer in ("kernel", "golden_path"):
        for pattern in repo.layers.get(layer, []):
            if pattern.rstrip("/") not in registered:
                rep.warn(
                    f"{layer} の '{pattern}' が capabilities.toml に未登録"
                    "（憲法 §6-2: 登録されていない成果物は存在しない）"
                )

    if repo.capabilities:
        rep.note(
            f"{len(repo.capabilities)} capabilities / "
            + " ".join(
                f"{s}:{sum(1 for c in repo.capabilities if c.status == s)}"
                for s in PROMOTION_ORDER
                if any(c.status == s for c in repo.capabilities)
            )
        )

    return rep.emit()


def main(argv: list[str]) -> int:
    targets = [Path(a) for a in argv[1:]] or [Path.cwd()]
    worst = 0
    for t in targets:
        worst = max(worst, validate(t.expanduser().resolve()))
    return worst


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
