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
    # テストはここでは走らせない。下の release が --execute で
    # [publish.commands] test（= tests/run_all.py）を実走させるので、独立して
    # 置くと **同じスイートを2回走らせる**ことになる。
    #
    # 実測 2026-09-26: test 49.7s / release 47.8s / 合計 97.7s で、BUDGET 90 を
    # 超えて release と catalog が走らないまま終わっていた。時間切れは
    # 「検査していない」なので差し戻しはしないが、**最も重いゲートだけが毎回
    # 走らない**形になる。予算を上げてもテストが増えればまた同じところに来る。
    #
    # 落としても信号は減らない。テストが落ちれば release の correctness レーンに
    # `test_passes: NG` として出て、--require public に届かず error になる。
    # 前段のレーンが止まって correctness に届かなかったときも、release_gate は
    # 「走らせていない観測」を名指しして必須の段に届かないと言う。沈黙にはならない。
    # 自分の公開ゲートを自分で通す。work-os は他リポの公開可否を判定する側なので、
    # 自分に当てていない検査を持っているとその判定が嘘になる。
    #
    # --require が要る。release_gate の既定の終了コードは enforcement に従うので、
    # work-os（warn）ではレーンが途中で止まっていても 0 が返る。それをそのまま
    # 入れると「走らせるが結果を無視する」ゲートになる。かといって到達していない
    # 段を要求すると毎回差し戻すことになる。現在地を宣言して、そこから落ちたときだけ
    # 差し戻す。
    #
    # 2026-09-15 に public（第2段）へ上げた。同日このリポジトリを public な別リポへ
    # 切り出し、[publish] released_at に起点を宣言したことで history レーンが通った。
    # 2026-08-31 に history レーンを足して第1段へ落ちて以来、ここは internal だった。
    # 下げるのは、公開を取り下げると決めたときだけ。
    ("release", [sys.executable, "engine/release_gate.py", ".",
                 "--execute", "--require", "public"], "error"),
    # 台帳が実在とズレていないか。カタログは生成物なので、生成しなおさないかぎり
    # 静かに腐る。実測 2026-09-26: 08-31 生成のカタログが 5 本消えて 5 本増えた状態で
    # 4週間通っていた。本数は 82 / 82 で一致していたので、件数を見ても気づけない。
    #
    # warn である。ズレを直すには registry への書き込みが要り、それは work-os からは
    # できない（別リポで、cross-repo-guard が止める）。自分で直せないものを error に
    # すると毎回差し戻しになり、読まれなくなる。
    #
    # 根は渡さない。カタログの出自に書いてある根を使う。ここにパスを書くと、
    # 「どこを数えたか」の答えが2箇所になり、食い違ったときにどちらが本当か分からない。
    ("catalog", [sys.executable, "engine/catalog.py", "--verify"], "warn"),
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
        # severity が error のゲートしか無かった間は FAIL でよかった。warn のゲートを
        # 足した時点で、exit 1 を FAIL と呼ぶのは「確かめていない」を「落ちた」に
        # 畳むことになる。このファイルの上で自分がやらないと書いたことである。
        label = "FAIL" if severity == "error" else "WARN"
        print(f"[work-os-qa] {name}: {label} (exit {proc.returncode})", file=sys.stderr)
        tail = (proc.stdout + proc.stderr).strip().splitlines()[-25:]
        for line in tail:
            print(f"[work-os-qa]   {line}", file=sys.stderr)
        worst = max(worst, 2 if severity == "error" else 1)
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
