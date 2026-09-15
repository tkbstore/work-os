#!/usr/bin/env bash
# work-os をリポジトリに導入する。既存ファイルは上書きしない。
#
#   bash <repos>/work-os/hooks/install.sh <repos>/<a-repo>
#
# やること（すべて additive）:
#   1. work.toml が無ければ生成する
#   2. .git/hooks/pre-commit を（無ければ）置く
#   3. Claude Code hook が中央に登録されているかを確認する（各リポには何も置かない）

set -euo pipefail

WORKOS="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="${1:?usage: install.sh <repo-path>}"
TARGET="$(cd "$TARGET" && pwd)"

echo "work-os : $WORKOS"
echo "target  : $TARGET"
echo

# 1. work.toml ---------------------------------------------------------------
if [ -f "$TARGET/work.toml" ]; then
  echo "[1/3] work.toml : 既にあります（触れません）"
else
  python3 "$WORKOS/engine/adopt.py" "$TARGET" --write
  echo "[1/3] work.toml : 生成しました。中身を一度見て、layers を実態に合わせてください"
fi

# 2. pre-commit --------------------------------------------------------------
HOOK="$TARGET/.git/hooks/pre-commit"
if [ -f "$HOOK" ]; then
  echo "[2/3] pre-commit : 既にあります。次の1行を手で足してください:"
  echo "        python3 \"$WORKOS/engine/validate.py\" \"$TARGET\" || true"
else
  cat > "$HOOK" <<EOF
#!/usr/bin/env bash
# work-os validation (enforcement は work.toml が決める)
python3 "$WORKOS/engine/validate.py" "$TARGET"
EOF
  chmod +x "$HOOK"
  echo "[2/3] pre-commit : 置きました"
fi

# 3. Claude Code hook --------------------------------------------------------
# hook は各リポに配れない。配ると N 箇所を同時に正しく保つ必要が出て、必ずどこかで
# 抜ける（実測: claude_guard は kernel を宣言する7リポ中、work-os の1本にしか
# 登録されておらず、他6リポでは一度も動いていなかった）。
#
# 正しい形は中央に1回だけ登録して、絶対パスで work-os を指すこと。判定は hook 側が
# 対象パスから上に辿って work.toml を見つけ、そのリポの宣言で決める。だから
# リポが増えても登録は増えない。branch_guard / sweep_guard / root_cause_guard は
# 最初からこの形だった。
echo "[3/3] Claude Code の PreToolUse hook（中央登録・各リポには置きません）"

GLOBAL="$HOME/.claude/settings.json"
if [ -f "$GLOBAL" ] && grep -q "work-os/hooks/claude_guard.py" "$GLOBAL"; then
  echo "      登録済み: $GLOBAL → $WORKOS/hooks/claude_guard.py"
else
  echo "      未登録です。$GLOBAL の hooks.PreToolUse に次を1回だけ足してください:"
  cat <<EOF | sed 's/^/        /'
{
  "matcher": "Edit|Write|MultiEdit|NotebookEdit",
  "hooks": [
    { "type": "command", "command": "python3 \"$WORKOS/hooks/claude_guard.py\"" }
  ]
}
EOF
fi

# 過去に配ってしまった各リポの登録は、二重発火になるので指摘する。
if [ -f "$TARGET/.claude/settings.json" ] \
   && grep -q "claude_guard.py" "$TARGET/.claude/settings.json"; then
  echo "      注意: $TARGET/.claude/settings.json に claude_guard の登録が残っています。"
  echo "            中央登録と二重に発火するので、そちらの登録は外してください。"
fi

echo
echo "完了。何も止まりません（enforcement = warn）。"
echo "確認: python3 $WORKOS/engine/validate.py $TARGET"
