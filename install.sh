#!/bin/sh
# install.sh — workos-gate を1コマンドで入れる。
#
#   curl -fsSL https://raw.githubusercontent.com/tkbstore/work-os/main/install.sh | sh
#
# pip も npm も経由しない。中身は外部依存ゼロの Python なので、パッケージマネージャを
# 挟むと利用者の環境に依存が1つ増えるだけになる。
#
#   1. python3 が在るか、3.9 以上かを確かめる
#   2. tarball を取得して作業場に展開する（$WORKOS_GATE_HOME 本体はまだ触らない）
#   3. 展開したものが実際に判定を返すことを確かめてから、初めて入れ替える
#   4. shim を置き、shim 経由でもう一度走らせる。駄目なら元に戻す
#
# 環境変数で変えられる:
#   WORKOS_GATE_REF     入れる版。ブランチ・タグ・SHA（既定 main）
#   WORKOS_GATE_HOME    実体の置き場（既定 ${XDG_DATA_HOME:-~/.local/share}/workos-gate）
#   WORKOS_GATE_BIN     shim の置き場（既定 ~/.local/bin）
#   WORKOS_GATE_PYTHON  使う python3（既定 PATH の python3）
#   WORKOS_GATE_TARBALL 取得元の tar.gz を直接指す（ミラー・社内配布・この script 自体の検査）
#
# 判定基準の事実（顧客名など）を持つ registry は同梱しない。入れた直後は work-os が
# 持つ骨格だけで動く。組織の宣言を足すときは WORKOS_REGISTRY でそれを指す。

set -eu

REPO="tkbstore/work-os"
REF="${WORKOS_GATE_REF:-main}"
HOME_DIR="${WORKOS_GATE_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/workos-gate}"
BIN_DIR="${WORKOS_GATE_BIN:-$HOME/.local/bin}"
MIN_MAJOR=3
MIN_MINOR=9
# 入れたものがゲートであることの最低条件。名前で列挙するのは「在るか」だけで、
# 中身が効いているかは probe() が実際に走らせて見る。
REQUIRED="engine/release_gate.py config/release_lanes.toml config/secret_patterns.toml"

say() { printf '%s\n' "$*"; }
die() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }

# --------------------------------------------------------------------------- #
# 1. python3
# --------------------------------------------------------------------------- #

PY="${WORKOS_GATE_PYTHON:-}"
if [ -z "$PY" ]; then
    PY="$(command -v python3 2>/dev/null || true)"
fi
[ -n "$PY" ] || die "python3 が見つからない。${MIN_MAJOR}.${MIN_MINOR} 以上を入れてから実行する。"
command -v "$PY" >/dev/null 2>&1 || die "python3 として指された '$PY' が実行できない。"

# 版の判定は python 自身にさせる。文字列を sh で切ると 3.10 が 3.9 より小さくなる。
if ! "$PY" -c "import sys; sys.exit(0 if sys.version_info[:2] >= ($MIN_MAJOR, $MIN_MINOR) else 1)" 2>/dev/null; then
    have="$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo '不明')"
    die "python3 が ${have}。${MIN_MAJOR}.${MIN_MINOR} 以上が要る（${PY}）。"
fi
say "python3 $("$PY" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')  ($PY)"

# --------------------------------------------------------------------------- #
# 2. 取得して作業場に展開する
# --------------------------------------------------------------------------- #

if command -v curl >/dev/null 2>&1; then
    fetch() { curl -fsSL "$1" -o "$2"; }
elif command -v wget >/dev/null 2>&1; then
    fetch() { wget -q "$1" -O "$2"; }
else
    die "curl も wget も無い。どちらかを入れてから実行する。"
fi
command -v tar >/dev/null 2>&1 || die "tar が無い。"

TARBALL="${WORKOS_GATE_TARBALL:-https://github.com/$REPO/archive/$REF.tar.gz}"
WORK="${TMPDIR:-/tmp}/workos-gate-install.$$"
STAGE="$WORK/stage"
OLD="$WORK/old"
# 途中で落ちたら中間物を残さない。$HOME_DIR 本体は「判定を返した」の後まで触らない。
trap 'rm -rf "$WORK"' EXIT INT TERM
mkdir -p "$STAGE"

say "取得 $TARBALL"
# 取得と展開を分ける。パイプで繋ぐと、取得が失敗しても tar の成否しか見えず、
# 「取れなかった」が「中身が違う」として報告される。
fetch "$TARBALL" "$WORK/src.tar.gz" \
    || die "取得できなかった: ${TARBALL}（版 '$REF' が存在するかを確かめる）"
tar -xzf "$WORK/src.tar.gz" --strip-components=1 -C "$STAGE" \
    || die "展開できなかった: $TARBALL"

for need in $REQUIRED; do
    [ -f "$STAGE/$need" ] || die "取得したものに $need が無い。版 '$REF' はゲートを含んでいない。"
done

# --------------------------------------------------------------------------- #
# 3. 入れ替える前に、それが判定を返すことを見る
# --------------------------------------------------------------------------- #
# ファイルが在ることは「効いている」を意味しない。空のディレクトリに当てて、骨格の
# 観測が実際に載った判定が返るところまで見る。registry を持たない利用者の初回は
# この経路しか通らないので、ここが静かに空なら install は失敗である。
#
# probe <実行するコマンド…> — 標準出力の JSON を検証する。判定が block を含むのは
# 正常（空ディレクトリなのだから）なので、終了コードではなく中身で見る。

probe() {
    _p="$WORK/probe"
    rm -rf "$_p"; mkdir -p "$_p/target"
    WORKOS_REGISTRY="$_p/no-registry" "$@" "$_p/target" --json > "$_p/out.json" 2>"$_p/err" || true
    "$PY" - "$_p/out.json" <<'PROBE_EOF'
import json, sys
try:
    got = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception as exc:
    raise SystemExit(f"判定が JSON として読めない: {exc}")
if not isinstance(got, list) or len(got) != 1:
    raise SystemExit(f"判定が1件ではない: {got!r:.80}")
lanes = got[0].get("lanes") or []
findings = sum(len(ln.get("findings") or []) for ln in lanes)
if not findings:
    raise SystemExit("観測が1件も載っていない（骨格の判定基準が読めていない）")
print(f"観測 {findings} 件 / レーン {len(lanes)} 本")
PROBE_EOF
}

detail="$(probe "$PY" "$STAGE/engine/release_gate.py")" || {
    sed -n '1,20p' "$WORK/probe/err" >&2 2>/dev/null || true
    die "取得したゲートが判定を返さなかった。入れ替えていないので、今の $HOME_DIR はそのまま。"
}
say "検証   $detail"

# --------------------------------------------------------------------------- #
# 4. 入れ替えて shim を置く
# --------------------------------------------------------------------------- #
# 入れ替えの後に失敗したら、前に入っていたものを戻す。戻せる状態のまま進むために、
# 書けるかどうかの確認は $HOME_DIR を触る前に済ませる。

SHIM="${BIN_DIR}/workos-gate"
mkdir -p "$BIN_DIR" || die "shim を置く ${BIN_DIR} を作れない。WORKOS_GATE_BIN で別の場所を指す。"
[ -d "$SHIM" ] && die "${SHIM} がディレクトリになっている。取り除いてからもう一度。"
: > "${SHIM}.probe" 2>/dev/null || die "${BIN_DIR} に書けない。WORKOS_GATE_BIN で別の場所を指す。"
rm -f "${SHIM}.probe"

# shim は作業場で組み立ててから運ぶ。$SHIM へ直接リダイレクトすると、書けなかった
# ときにリダイレクトの失敗としてシェルがその場で終わり、下の restore に届かない。
cat > "$WORK/shim" <<SHIM_EOF
#!/bin/sh
# install.sh が生成した。直接編集しない（次の install で上書きされる）。
PY="\${WORKOS_GATE_PYTHON:-$PY}"
command -v "\$PY" >/dev/null 2>&1 || PY=python3
exec "\$PY" "${HOME_DIR}/engine/release_gate.py" "\$@"
SHIM_EOF
chmod +x "$WORK/shim"

# 前に入っていた shim も控える。実体だけ戻して shim を消すと、木は元どおりなのに
# workos-gate が無い状態になる。利用者から見れば壊れたままである。
if [ -f "$SHIM" ]; then
    cp "$SHIM" "$WORK/shim.old"
fi

# 戻す途中で止まらない。shim を戻せないこと（そこに書けないのが失敗の原因なら
# 戻すときも書けない）を理由に、実体を戻さずに終わるのが一番まずい。
restore() {
    if [ -f "$WORK/shim.old" ]; then
        cp "$WORK/shim.old" "$SHIM" 2>/dev/null && chmod +x "$SHIM" 2>/dev/null || true
    else
        rm -f "$SHIM" || true
    fi
    rm -rf "$HOME_DIR"
    if [ -e "$OLD" ]; then
        mv "$OLD" "$HOME_DIR"
        say "前に入っていた ${HOME_DIR} に戻した。"
    fi
}

mkdir -p "$(dirname "$HOME_DIR")"
if [ -e "$HOME_DIR" ]; then
    mv "$HOME_DIR" "$OLD"
fi
mv "$STAGE" "$HOME_DIR"
say "展開 $HOME_DIR"

cp "$WORK/shim" "$SHIM" || { restore; die "shim を置けなかった: ${SHIM}"; }
chmod +x "$SHIM"  || { restore; die "shim を実行可能にできなかった: ${SHIM}"; }

# shim は install.sh が生成した別のファイルなので、中身を見ずに済ませない。
if ! detail="$(probe "$SHIM")"; then
    sed -n '1,20p' "$WORK/probe/err" >&2 2>/dev/null || true
    restore
    die "shim が判定を返さなかった。"
fi
rm -rf "$OLD"
say "shim   $SHIM  ($detail)"

# --------------------------------------------------------------------------- #

case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) say ""
       say "注意: $BIN_DIR が PATH に無い。シェルの設定に次を足す:"
       say "  export PATH=\"$BIN_DIR:\$PATH\"" ;;
esac

say ""
say "使い方:"
say "  workos-gate                 # カレントのリポジトリを検査"
say "  workos-gate <path> --json   # 機械可読"
say "  workos-gate <path> -v       # 通った観測も出す"
