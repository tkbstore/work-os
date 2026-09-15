#!/bin/bash
# fleet_watch.sh — リポジトリ群を観測し、悪化だけを記録する。
#
# 手で叩く。常駐はしていない。launchd（com.workos.fleet）で毎日回していた
# つもりだったが、macOS の TCC が launchd 配下のプロセスに ~/Documents を
# 読ませないため一度も走っていなかった（`ls` も python の open も
# Operation not permitted。観測対象がそこなので回避のしようがない）。
# 実測すると観測 9 件はすべてセッション中の手動実行で、作業した日に
# 固まっていた。動いていない自動化を残すより実態に合わせる（2026-08-31）。
set -u
WORKOS="$(cd "$(dirname "$0")/.." && pwd)"
ROOT="${1:-$HOME/Documents/GitHub}"
# 走らせる python はここでしか決めない。各行に絶対パスを直に書いていた
# （launchd に PATH が無かった頃の名残）。launchd を畳んだ後も残っていたため、
# 唯一の自動観測だけが常に 3.9 で回っていた。2026-09-02 まで 3.9 の TOML
# フォールバックが判定基準を読み違えており、release_gate が 9 レーン中 5 レーンを
# 落としたまま「公開できる」と答えていた。同じ直書きが 8 箇所あり、直すなら
# 8 箇所すべてを直す必要があった＝1 箇所忘れれば黙って古い方で回る形だった。
PY="$(command -v python3 || echo /usr/bin/python3)"
# 記録先は config 層（別リポ）。所在を知っているのは engine/workos.py だけなので、
# シェル側で組み立てず訊きにいく。答えられないときだけ同居時の位置に落とす。
LOG="$("$PY" -c "import sys; sys.path.insert(0, '$WORKOS/engine'); import workos; print(workos.registry_path('fleet_alerts.log'))" 2>/dev/null)"
[ -n "$LOG" ] || LOG="$WORKOS/registry/fleet_alerts.log"
mkdir -p "$(dirname "$LOG")"

out="$("$PY" "$WORKOS/engine/fleet.py" "$ROOT" --snapshot --diff 2>&1)"
code=$?
found=0

# 見つけたものは、この関数を通してしか out に入らない。各検査が自分で code を
# 上げる形にしていたら、上げ忘れた検査だけが黙って落ちた。実測 2026-08-31: 後続
# 5つのうち [生成器] [公開ゲート] [inbox] の3つが code に触れておらず、記録にも
# 通知にも出ない口になっていた。足すことと知らせることを1箇所で結ぶ。
add() {
  out="$out
$1"
  code=1
  found=$((found + 1))
}

# 抽象度の検査。work-os が具体に落ちていたら、それも「悪化」として扱う。
gate="$("$PY" "$WORKOS/engine/abstraction_gate.py" 2>&1)"
if [ $? != 0 ]; then
  add "[抽象度ゲート] $gate"
fi

# 宣言されていない生成器。今日1日で同じ形の事故が3回起きたので毎日見る。
gen="$("$PY" "$WORKOS/engine/generators.py" "$ROOT" --min 5 2>/dev/null | head -2)"
case "$gen" in
  *"うち宣言されていないもの ── 0"*) : ;;
  *) add "[生成器] $gen" ;;
esac

# ガード自身の検査。ガードは2度破られた。宣言では守れない。
if ! "$PY" "$WORKOS/tests/test_guards.py" >/dev/null 2>&1; then
  add "[ガード検査] 失敗。hooks/ に穴が開いています: python3 $WORKOS/tests/test_guards.py"
fi

# 公開ゲート。公開を宣言したリポが、宣言に見合う状態かを毎日見る。
# 実走（--execute）はしない。無人で任意のコマンドを毎日走らせるのは既定にしない。
rel="$("$PY" "$WORKOS/engine/release_gate.py" --scan "$ROOT" 2>/dev/null \
       | grep -E '公開不可|欠けているレーン')"
if [ -n "$rel" ]; then
  add "[公開ゲート] 公開を宣言したが今は出せないリポがあります
$rel"
fi

# 公開ゲート自身の検査。落とすべきものを落とし、通すべきものを通すか。
if ! "$PY" "$WORKOS/tests/test_release_gate.py" >/dev/null 2>&1; then
  add "[公開ゲート検査] 失敗。ゲートに穴が開いています: python3 $WORKOS/tests/test_release_gate.py"
fi

# 21日を超えた inbox は捨てる候補として毎日出す。溜まらないための唯一の規則。
stale="$("$PY" "$WORKOS/engine/inbox.py" --stale 2>/dev/null)"
case "$stale" in
  *候補*) add "[inbox] $stale" ;;
esac

# 記録と通知は、すべての検査が終わってから。ここが検査の途中にあると、後ろの
# 検査は out に足されるだけで誰にも届かない（実測 2026-08-31: 5つがそうだった）。
if [ $code -ne 0 ]; then
  { echo "=== $(date '+%Y-%m-%d %H:%M') ==="; echo "$out"; echo; } >> "$LOG"
  # 悪化があるときだけ通知する。何もない日は静かにする。
  if command -v osascript >/dev/null 2>&1; then
    n=$(( $(echo "$out" | grep -c '^  - ') + found ))
    osascript -e "display notification \"悪化 ${n} 件 — ${LOG}\" with title \"work-os fleet\"" >/dev/null 2>&1
  fi
fi
echo "$out"
exit 0
