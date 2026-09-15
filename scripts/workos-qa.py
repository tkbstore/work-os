#!/usr/bin/env python3
"""ローカル品質ゲート（Stop hook `repo-quality-gate` が拾う）。

CI は使わない方針（private リポの Actions は従量課金で、中身と無関係に赤くなる）。
同じ内容をここで回す。hook 側の規約:

    exit 0     PASS
    exit 1     警告（通知のみ）
    exit 2以上 エラー（Claude に差し戻す）

work-os は「他リポを検査する側」なので、自分に当てていない検査があると
そのまま嘘になる。ここは work.toml の [publish.commands] と同じものを走らせる。

    python3 scripts/workos-qa.py
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# hook 側の上限が120秒。**合計**でそこに収める。
#
# 以前は `TIMEOUT = 90` を各ゲートに個別に渡していて、コメントだけが「全ゲート合計」と
# 言っていた。ゲートが4本なら最悪 360 秒で、宣言していた上限を実装が守っていない。
# 今日ずっと潰してきた「名前が約束することを実装が見ていない」の形が自分のファイルにも在った。
# 実測 2026-08-29: validate 0.1s / abstraction 0.2s / tests 10.9s / release_gate 5.8s = 17.2s。
BUDGET = 90

# 時間切れは「検査が落ちた」ではなく「検査していない」である。両者を同じ終了コードに
# 畳むと、リポジトリに何の問題も無いのにマシンが混んでいただけで差し戻しが起きる。
# 繰り返せば読まれなくなり、今日ずっと潰してきた「誤検知は誰も報告しない」に着地する。
#
# 判定は3値にする（別リポの責任分界の枠から来た要件。registry の handoff 参照）:
#   0 PASS         検査して通った
#   1 UNVERIFIABLE 検査していない（時間切れ）。通知はするが差し戻さない
#   2 FAIL         検査して落ちた。差し戻す
# unverifiable を PASS に畳むと「確認していないことを確認したと言う」側に回るので、
# 黙って 0 は返さない。FAIL に畳むと誤検知になるので 2 も返さない。
#
# 実測 2026-08-29: 通常 40.4 秒。他の実行と競合した1回だけ tests が 87 秒使い、
# release が走らずに終わった。リポジトリ側には何の問題も無かった。
UNVERIFIABLE = 1

# (表示名, コマンド, 落ちたときの扱い)
GATES: list[tuple[str, list[str], str]] = [
    # 宣言と実態が合っているか。層・能力・不変条件の検査
    ("validate", [sys.executable, "engine/validate.py", "."], "error"),
    # 仕組みの層に組織固有の名前が漏れていないか。
    # 08-28 に自分で4件漏らし、指摘は別セッションから届いた。自分のテストは緑だった
    ("abstraction", [sys.executable, "engine/abstraction_gate.py"], "error"),
    # ガードのテスト。ここが緑でないとフリート全体の検査が信用できない
    ("test", [sys.executable, "tests/run_all.py"], "error"),
    # 自分の公開ゲートを自分で通す。work-os は他リポの公開可否を判定する側なので、
    # 自分に当てていない検査を持っているとその判定が嘘になる。
    #
    # --require internal が要る。release_gate の既定の終了コードは enforcement に
    # 従うので、work-os（warn）ではレーンが第2段で止まっていても 0 が返る。それを
    # そのまま入れると「走らせるが結果を無視する」ゲートになる。かといって第2段を
    # 要求すると、到達していない段でも毎回差し戻すことになる。現在地を宣言して、
    # そこから落ちたときだけ差し戻す。
    #
    # 2026-08-31 に registry/ を切り出して一度 public（第2段）へ上げたが、同じ日に
    # history レーンを足したことで第2段は再び通らなくなった。理由は顧客名ではなく
    # **履歴が61コミットあること** で、公開するなら squash が要る。現在地は第1段
    # なので internal に戻す。上げ直すのは、実際に squash して公開すると決めたとき。
    ("release", [sys.executable, "engine/release_gate.py", ".",
                 "--execute", "--require", "internal"], "error"),
]


def main() -> int:
    worst = 0
    deadline = time.monotonic() + BUDGET
    for name, cmd, severity in GATES:
        left = deadline - time.monotonic()
        if left <= 0:
            print(f"[work-os-qa] {name}: 合計 {BUDGET} 秒を使い切ったので走らせていません"
                  "（検査していません。落ちたのではありません）", file=sys.stderr)
            worst = max(worst, UNVERIFIABLE)
            continue
        try:
            proc = subprocess.run(cmd, cwd=ROOT, capture_output=True,
                                  text=True, timeout=left)
        except subprocess.TimeoutExpired:
            print(f"[work-os-qa] {name}: 残り {left:.0f} 秒で打ち切りました"
                  f"（合計予算 {BUDGET} 秒。検査していません）", file=sys.stderr)
            worst = max(worst, UNVERIFIABLE)
            continue
        if proc.returncode == 0:
            print(f"[work-os-qa] {name}: PASS")
            continue
        print(f"[work-os-qa] {name}: FAIL (exit {proc.returncode})", file=sys.stderr)
        tail = (proc.stdout + proc.stderr).strip().splitlines()[-25:]
        for line in tail:
            print(f"[work-os-qa]   {line}", file=sys.stderr)
        worst = max(worst, 2 if severity == "error" else 1)
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
