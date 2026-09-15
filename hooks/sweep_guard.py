#!/usr/bin/env python3
"""sweep_guard.py — 複数リポジトリを書き換える操作だけを、実行前に一度止める。

単一リポの作業は CWD が権威で守られていて、push しなければ可逆である。
危ないのは横断作業だけなので、ゲートはここ1点に集約する。ゲートを増やすほど
全セッションのコンテキストを圧迫し、やがて誰も読まなくなる。

止める条件は2つが同時に成立したときだけ:
  1. 3つ以上のリポジトリに触れる
  2. 書き換える操作である（読み取りは何個リポを跨いでも素通し）

止めたあとは対象一覧を出す。それが dry-run になる。納得したら
環境変数 WORKOS_SWEEP=1 を付けて同じコマンドを再実行する。

PreToolUse(Bash) フック。exit 2 で停止、exit 0 で通過。
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent   # work-os の親＝リポジトリ群
THRESHOLD = 3
ACK_ENV = "WORKOS_SWEEP"

# 発火と解錠を記録する。誤検知率が誰にも観測されていなかったため。
#
# 実測 2026-08-28: 同じ誤検知を少なくとも3セッションが独立に踏み、3件とも自力で
# 回避し、3件とも報告しなかった。回避が一手で済むので誰にとっても「上げるほどでは
# ない」になる。そして**その回避法（本文を heredoc かファイルに逃がす）は、本物の
# 掃討にもそのまま効いた**。全員が学習した回避の作法が、そのまま抜け道の作法に
# なっていた。報告に頼る限りこのズレは見えないので、機械が数える。
#
# コマンド文字列は記録しない（秘密が載りうる）。数と対象名と cwd だけを残す。
# 記録先は registry（別リポ）にある。無い環境でも止める仕事は続ける。
# 検査用の複製のように engine/ が隣に無い置き方でも、ガードは動かねばならない。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))
try:
    from workos import registry_path
    EVENTS = registry_path("sweep_events.jsonl")
except Exception:                                  # noqa: BLE001 — hook は落ちない
    EVENTS = None                                  # 記録だけ諦める。判定は変えない

# 書き換える操作。ここに無いものは読み取りとみなし、何個リポを跨いでも止めない。
MUTATING = re.compile(
    r"(?:^|[;&|]\s*|\s)(?:"
    r"git\s+(?:-C\s+\S+\s+)?(?:commit|push|rm|reset|revert|merge|rebase|tag|"
    r"cherry-pick|clean|restore|switch|checkout|apply|stash)"
    r"|rm\s+-[rf]|rmdir|mv\s|cp\s+-[ra]|truncate|shred"
    # find -delete / -exec rm / xargs rm も横断的な削除である。
    # 実際 `find ... -empty -delete` が最初の実装をすり抜けた。
    r"|find\s[^|;&]*-delete|find\s[^|;&]*-exec\s+(?:rm|mv|sed|truncate)"
    r"|xargs\s+(?:-[^\s]+\s+)*(?:rm|mv|sed|truncate)"
    r"|sed\s+-i|perl\s+-i|tee\s"
    r"|gh\s+repo\s+(?:delete|archive|edit|transfer)"
    r")", re.I)

# 「全リポを回す」形。名前を数えなくても横断だと分かる。
LOOP_OVER_ALL = re.compile(r"for\s+\w+\s+in\s+(?:\*/|\$\(ls|\$\(find)", re.I)
# リポジトリ群のルートで再帰的に書き換える形。リポ名を1つも書かなくても全リポに届く。
# 実際 `cd <root> && find . -path "*/sessions/*.md" -empty -delete` が最初の実装をすり抜けた。
RECURSIVE = re.compile(r"(?:find\s+[.~/]|\*/|\s-r\b|--recursive|\*\*/)", re.I)


def repos_on_disk() -> set[str]:
    try:
        return {p.name for p in ROOT.iterdir() if p.is_dir() and (p / ".git").exists()}
    except OSError:
        return set()


# git 呼び出しか、そのときの対象リポジトリはどこか。
# git は1回の呼び出しで複数のリポジトリを触れない。対象は -C か cwd が決める。
IS_GIT = re.compile(r"^\s*git(?:\s|$)", re.I)
GIT_DASH_C = re.compile(r"\bgit\s+(?:-c\s+\S+\s+)*-C\s+(\S+)", re.I)

# 環境変数として前置されたときだけ解錠する。文字列として出てくるだけでは通さない。
# 以前は `ACK_ENV in command` だったので、本文に名前を書くだけで素通りした。
ACK = re.compile(rf"(?:^|[\s;&|(]){ACK_ENV}=", re.M)


# heredoc 本文を「データ」として受け取る側。ここに在る受け手にだけ渡っているとき、
# 本文は書き込まれる中身であって、実行されるコードではない。
#
# 既定は逆（潰さない＝シェルとして読む）である。この向きは意図的に選んでいる:
# ここに列挙し忘れた受け手が出たとき、本文は検査され、誤検知として人に届く。
# 逆向き（実行する側を列挙して、それ以外を潰す）にすると、列挙し忘れた受け手で
# 本物の掃討が黙って通る。列挙は必ずどこかで漏れるので、漏れたときに止まる側に倒す。
#
# 限界（塞いでいないことを明記する）: python / node の本文がその中から
# subprocess で shell を呼ぶ形は見ていない。foreign language の本文を
# シェルの正規表現で読んでも意味が取れないため、ここでは扱わない。
DATA_CONSUMERS = {
    "cat", "tee",                                   # そのまま書き出す
    "python", "python3", "node", "ruby", "jq",      # 別の言語。シェルとしては読めない
    # git は stdin を読むが実行しない（commit -F - / apply / am）。本文はメッセージか差分。
    # git の対象リポジトリは -C か cwd が決めるので、本文の名前は数える意味が無い。
    "git",
}

# パイプラインの中のコマンド語。`cat <<EOF | bash` の bash を見落とさないため、
# 先頭語だけでなく `|` の後ろも見る。`FOO=1 bash` の環境変数前置は語ではない。
COMMAND_WORDS = re.compile(r"(?:^|\|)\s*((?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*)([^\s|<>;&]+)")


def heredoc_body_is_data(header: str) -> bool:
    """heredoc の受け手が、本文をデータとして扱う側だけかを返す。

    `python3 - <<PY > out.json` は本文がデータ（別言語のソース）。
    `bash <<EOF` は本文が実行されるシェルコード。
    `cat <<EOF | bash` は先頭が cat でも、渡る先が bash なので実行される。
    """
    words = [m.group(2) for m in COMMAND_WORDS.finditer(header)]
    if not words:
        return False
    return all(Path(w).name in DATA_CONSUMERS for w in words)


def strip_heredocs(command: str) -> str:
    """データとして渡される heredoc の本文だけを空白で潰す（長さは変えない）。

    本文がデータなら、それは「書き込まれる中身」であって「書き込み先」ではない。
    行き先はリダイレクト側に在る。潰さないと、本文に書いた `rm` 一語が全体を
    「書き換え」と判定させ、本文が言及しただけのリポ名が対象として数えられる。
    実測 2026-08-28: 書き込みが1件も無い操作が3リポ分として止まった。

    本文が実行されるなら潰してはいけない。実測 2026-08-28: 同じ削除を
    `bash <<'EOF' ... EOF` に入れるだけで素通りしていた。しかもこの回避は
    誤検知に当たった複数のセッションが独立に学習していて、抜け道の作法と
    同じ形になっていた。
    """
    out = command
    for m in re.finditer(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1", command):
        tag = m.group(2)
        body_start = out.find("\n", m.start())
        if body_start == -1:
            continue
        # `<<` を含む1行（＝受け手が書かれている行）だけを見て、データか否かを決める
        line_start = out.rfind("\n", 0, m.start()) + 1
        if not heredoc_body_is_data(out[line_start:body_start]):
            continue
        rest = out[body_start + 1:]
        hit = re.search(rf"^[\t ]*{re.escape(tag)}[\t ]*$", rest, re.M)
        body_end = body_start + 1 + hit.start() if hit else len(out)
        body = re.sub(r"[^\n]", " ", out[body_start + 1:body_end])
        out = out[:body_start + 1] + body + out[body_end:]
    return out


def segment_from(command: str, start: int) -> str:
    """引用符の外に在る区切りだけを見て、start から始まる1コマンドの範囲を返す。

    引用符の内側の `|` を区切りとして数えると、`rm "/path/with|pipe"` のように
    動詞と対象が分断されて見落とす。安全側に倒すため引用符を追う。
    """
    quote = None
    i = start
    while i < len(command):
        c = command[i]
        if quote:
            if c == "\\" and quote == '"':
                i += 2
                continue
            if c == quote:
                quote = None
        elif c in "'\"":
            quote = c
        elif c == "\\":
            i += 2
            continue
        elif c in "\n;|&)":
            return command[start:i]
        i += 1
    return command[start:]


def mutating_segments(command: str) -> list[str]:
    """書き換える動詞ごとに、その動詞の引数の範囲だけを返す。

    書き込み動詞が在るからといって、コマンド文字列のどこにあるパスも
    書き込み先ではない。`sed -i /tmp/a && python3 <repo>/scan.py <repo> <repo>`
    の後半は読み取りで、`... | tee /tmp/out` の前半も読み取りである。
    """
    parts = []
    for m in MUTATING.finditer(command):
        start = m.start()
        while start < m.end() and command[start] in " \t\n;&|":
            start += 1
        parts.append(segment_from(command, start))
    return parts


def referenced(command: str, known: set[str]) -> list[str]:
    """コマンド文字列から、実在するリポジトリ名への言及を拾う。

    大小文字は区別しない。macOS のファイルシステムが区別しないので、
    `delta-repo/x` と書いても `Delta-Repo/x` に届く。区別して数えると
    名前が観測から消え、閾値に届かず本物の横断削除が素通りする（実測）。
    """
    hits = set()
    for name in known:
        if re.search(rf"(?:^|[\s/'\"]){re.escape(name)}(?:[\s/'\"]|$)", command, re.I):
            hits.add(name)
    return sorted(hits)


def repo_at(path: str, known: set[str], cwd: str) -> str | None:
    """パスが指すリポジトリ名を返す。リポジトリ群の外なら None。"""
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = Path(cwd or ".") / p
    try:
        rel = p.resolve().relative_to(ROOT.resolve())
    except (ValueError, OSError):
        return None
    if not rel.parts:
        return None
    lower = {n.lower(): n for n in known}
    return lower.get(rel.parts[0].lower())


def targets(command: str, known: set[str], cwd: str) -> list[str]:
    """書き換えが届くリポジトリを数える。

    git のセグメントは名前で数えない。git は1回の呼び出しで複数のリポジトリを
    触れないので、対象は -C か cwd がただ一つに決める。つまり git の引数に出る
    リポ名は、定義上すべてメッセージ本文である。

    以前は `-m "..."` の中身を落として数えていたが、これは列挙だった。`-am` の
    ように短縮フラグが結合すると `-m` が一致せず本文が対象として数えられる。
    実測 2026-08-28: あるリポの7ファイルだけのコミットが「4リポジトリへの横断
    書き換え」と判定された（他3リポは未コミット変更 0 件）。次の組み合わせでまた
    抜けるので、文字列を読むのをやめて git の構造から決める。
    """
    hits: set[str] = set()
    for seg in mutating_segments(command):
        if IS_GIT.match(seg):
            m = GIT_DASH_C.search(seg)
            name = repo_at(m.group(1) if m else ".", known, cwd)
            if name:
                hits.add(name)
        else:
            hits.update(referenced(seg, known))
    return sorted(hits)


def record(event: str, names: list[str], cwd: str, sweeping_all: bool) -> None:
    """発火・解錠を1行追記する。失敗しても判定には影響させない。"""
    if EVENTS is None:
        return
    try:
        EVENTS.parent.mkdir(parents=True, exist_ok=True)
        with EVENTS.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "event": event,          # block | ack
                "repos": names,
                "count": len(names),
                "sweeping_all": sweeping_all,
                "cwd": cwd,
            }, ensure_ascii=False) + "\n")
    except OSError as exc:
        # 記録できないことは止める理由にならない。黙らずに理由だけ出す。
        print(f"[work-os] sweep_guard: 記録できませんでした: {exc}", file=sys.stderr)


def main() -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        print(raw, end="")
        return 0

    command = (payload.get("tool_input") or {}).get("command", "")
    if not command:
        print(raw, end="")
        return 0

    cwd_raw = str(payload.get("cwd") or "")
    if os.environ.get(ACK_ENV) or ACK.search(command):
        # 解錠された回数は、誤検知だったか本物だったかを後から数えるための唯一の観測。
        record("ack", targets(strip_heredocs(command), repos_on_disk(), cwd_raw),
               cwd_raw, False)
        print(raw, end="")
        return 0

    # 以降は heredoc 本文を潰した像だけを見る。本文は書き込み先ではない。
    command = strip_heredocs(command)

    if not MUTATING.search(command):
        print(raw, end="")          # 読み取りは素通し
        return 0

    known = repos_on_disk()
    cwd = cwd_raw
    names = targets(command, known, cwd)
    sweeping_all = bool(LOOP_OVER_ALL.search(command)) and "git" in command

    # ルートに居る（または cd する）状態での再帰的な書き換えは、名前が出なくても全リポに届く。
    at_root = cwd.rstrip("/") == str(ROOT).rstrip("/") or re.search(
        rf"cd\s+(?:{re.escape(str(ROOT))}|~/{re.escape(ROOT.name)})(?:\s|$|&|;)", command)
    if at_root and RECURSIVE.search(command):
        sweeping_all = True

    if not sweeping_all and len(names) < THRESHOLD:
        print(raw, end="")          # 単一リポの作業は止めない
        return 0

    record("block", names, cwd, sweeping_all)

    target = f"{len(known)} リポジトリ全体" if sweeping_all else f"{len(names)} リポジトリ"
    listed = ", ".join(names[:12]) + (f" ほか{len(names) - 12}" if len(names) > 12 else "")

    print(
        "[work-os] 横断的な書き換えを検出しました。実行前に一度止めます。\n"
        f"[work-os] 対象: {target}\n"
        + (f"[work-os] {listed}\n" if names else "")
        + "[work-os] \n"
        "[work-os] 内容を確認し、意図したとおりなら次のように再実行してください:\n"
        f"[work-os]   {ACK_ENV}=1 <同じコマンド>\n"
        "[work-os] \n"
        "[work-os] 単一リポの作業と読み取りは止めていません。止めるのは横断的な書き換えだけです。",
        file=sys.stderr,
    )
    return 2


def summary() -> int:
    """発火と解錠の数を出す。誤検知率を人が見られるようにするための唯一の口。"""
    if EVENTS is None or not EVENTS.is_file():
        print("[work-os] 記録がまだありません")
        return 0
    blocks = acks = 0
    for line in EVENTS.read_text(encoding="utf-8").splitlines():
        try:
            ev = json.loads(line).get("event")
        except json.JSONDecodeError:
            continue
        blocks += ev == "block"
        acks += ev == "ack"
    print(f"[work-os] sweep_guard  発火 {blocks} 件 / 解錠 {acks} 件  ({EVENTS})")
    if blocks:
        print(f"[work-os] 解錠された割合 {acks / blocks:.0%}。"
              "高いほど、止めているものの多くが誤検知だったことを意味します。")
    return 0


if __name__ == "__main__":
    if "--summary" in sys.argv:
        raise SystemExit(summary())
    raise SystemExit(main())
