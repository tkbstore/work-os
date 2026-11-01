#!/usr/bin/env python3
"""基準のリポ（calibration/upstream.toml の上流）が、今のゲートで全段に届くことを検査する。

このゲートが問うのは「広く使われているリポと同じ水準のものを出せているか」である。
基準のリポはその定義そのものなので、止めたならゲートの側が誤っている。どの理由でも
（「規則どおり」でも）同じで、基準より厳しく止める検査は「同じ水準」という主張と矛盾する。

読むのは calibration/upstream/audits/ の最新の結果。結果を測ったゲートが今のゲートと
違うなら、その結果は今のゲートについて何も言わないので、通さない
（`python3 engine/upstream_audit.py --recheck --write` で同じコミットを測り直す）。

対照群（採用されていないリポ）は基準ではないので、ここでは数えない。

  python3 tests/test_reference_repos.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
# E402（import が先頭に無い）の抑制。tests/ は上の sys.path の後でしか解決しない
from _registry import require  # noqa: E402

require("release_lanes.toml")
sys.path.insert(0, str(ROOT / "engine"))

# E402 の抑制。理由は上と同じで、engine/ は sys.path の後でしか解決しない
import release_gate  # noqa: E402
# E402 の抑制。理由は上と同じで、engine/ は sys.path の後でしか解決しない
import upstream_audit as ua  # noqa: E402


def main() -> int:
    current = ua.gate_fingerprint(release_gate.load_lanes())
    bad: list[str] = []
    for d in ua.upstream.load_decls():
        key = ua.upstream.key_of(d)
        r = ua.latest(key)
        if r is None:
            bad.append(f"{key}: 当て直した結果が無い")
            continue
        if r.get("gate") != current:
            bad.append(f"{key} {r['version']}: 今のゲートで測っていない "
                       f"（{r.get('gate') or '指紋なし'} ≠ {current}）")
            continue
        if not r.get("publishable"):
            bad.append(f"{key} {r['version']}: 到達 {r.get('reached') or '—'} で止まる "
                       f"（止めた観測: {', '.join(r.get('blocks', [])) or '記録なし'}）")
            continue
        print(f"  OK  {key} {r['version']}")
    for b in bad:
        print(f"  NG  {b}")
    if bad:
        print(f"\n基準のリポを止めている: {len(bad)} 件。ゲートの側を直す"
              "（ゲートを変えたら upstream_audit.py --recheck --write で測り直す）")
        return 1
    print("\n基準のリポは全部、今のゲートで全段に届く")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
