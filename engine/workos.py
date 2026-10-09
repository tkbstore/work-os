"""work-os 共通ライブラリ。外部依存なし（Python 3.9+ 標準ライブラリのみ）。

work.toml / capabilities.toml の読み込みと、層の判定をここに集約する。
他の engine スクリプトはすべてこのモジュールを経由する。
"""

from __future__ import annotations

import hashlib
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]


# --------------------------------------------------------------------------- #
# TOML 読み込み
#
# Python 3.11 未満でも動くように、work-os が使う TOML の部分集合だけを読む
# フォールバックを持つ。外部依存を増やさないための最小実装。
# 対応：コメント / [table] / [[array of tables]] / 文字列・数値・真偽値・配列 /
#       インラインテーブル / 複数行の配列
#
# **読めなかったものは例外にする。文字列のまま返さない。**
# 2026-09-02 まで、読めない値を raw 文字列として黙って返していた。判定基準
# (release_lanes.toml) の `severity = { internal = "warn", public = "block" }` が
# 文字列になり、さらに正規表現の中の `\[` を配列の継続と数えて後続行を食ったため、
# py3.9 では 9 レーン中 5 レーン（portability / provenance / history / usability /
# agent_ready）が丸ごと消えていた。それでもゲートは「OK 第2段 public」と答えた。
# 落ちるべきものを、検査が消えたことに気づかないまま通していた。
#
# 部分集合であること自体は変えない。変えるのは「知らない形に出会ったときに
# 黙って別の意味にしない」ことだけ。対応構文を足すたびに、次に来る未対応構文が
# また静かに素通りする形（列挙）から抜けられない。
# 同値性は tests/test_toml_fallback.py が実データで tomllib と突き合わせる。
# --------------------------------------------------------------------------- #


class TomlSubsetError(ValueError):
    """フォールバックパーサが読めなかった。推測して返すより落とす。"""


def _multiline_string(body: str, delim: str) -> str:
    """TOML の複数行文字列。開き区切りの直後の改行だけを落とす。"""
    if body.startswith('\r\n'):
        body = body[2:]
    elif body.startswith('\n'):
        body = body[1:]
    if delim == "'''":
        return body
    out, k = "", 0
    esc = {"n": '\n', "t": '\t', "r": '\r', '"': '"', '\\': '\\'}
    while k < len(body):
        ch = body[k]
        if ch != '\\':
            out += ch
            k += 1
            continue
        nxt = body[k + 1] if k + 1 < len(body) else ""
        if nxt in ('\n' + '\r'):                       # 行末バックスラッシュは空白を畳む
            k += 1
            while k < len(body) and body[k] in (" " + '\t' + '\n' + '\r'):
                k += 1
            continue
        if nxt not in esc:
            raise TomlSubsetError("未対応のエスケープ: " + '\\' + nxt)
        out += esc[nxt]
        k += 2
    return out


def _parse_value(raw: str):
    """1 つの値を読む。読めなければ TomlSubsetError。raw 文字列では返さない。"""
    raw = raw.strip()
    if not raw:
        raise TomlSubsetError("値が空")
    if raw[0] == "[":
        if _depth(raw) != 0 or not raw.endswith("]"):
            raise TomlSubsetError(f"配列が閉じていない: {raw[:60]}")
        return _split_array(raw[1:-1])
    if raw[0] == "{":
        if _depth(raw) != 0 or not raw.endswith("}"):
            raise TomlSubsetError(f"インラインテーブルが閉じていない: {raw[:60]}")
        return _inline_table(raw[1:-1])
    return _parse_scalar(raw)


def _parse_scalar(raw: str):
    raw = raw.strip()
    if raw.startswith('"') and raw.endswith('"') and len(raw) >= 2:
        return raw[1:-1].replace('\\"', '"')
    if raw.startswith("'") and raw.endswith("'") and len(raw) >= 2:
        return raw[1:-1]
    if raw in ("true", "false"):
        return raw == "true"
    for cast in (_as_int, float):
        try:
            return cast(raw)
        except ValueError:
            continue                      # 次の型で読む。読めなければ下で落とす
    raise TomlSubsetError(f"読めない値: {raw[:60]}")


def _as_int(raw: str) -> int:
    return int(raw, 0) if raw.lower().startswith(("0x", "0o", "0b")) else int(raw)


def _depth(text: str) -> int:
    """引用符の外側だけを数えた [ { の深さ。

    文字列の中の `\\[` を配列の括弧として数えていたのが、レーンが消えた原因。
    """
    depth, quote = 0, ""
    for ch in text:
        if quote:
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
    return depth


def _inline_table(body: str) -> dict:
    out: dict = {}
    for item in _split_items(body):
        if "=" not in item:
            raise TomlSubsetError(f"インラインテーブルの要素が k = v でない: {item[:60]}")
        k, _, v = item.partition("=")
        out[_bare_key(k)] = _parse_value(v)
    return out


def _bare_key(key: str) -> str:
    key = key.strip()
    if len(key) >= 2 and key[0] == key[-1] and key[0] in "\"'":
        return key[1:-1]
    if "." in key:
        # a.b = 1 は tomllib では {"a": {"b": 1}}。平らなキーとして持つと
        # 読めているように見えて別の意味になる。表せないので落とす。
        raise TomlSubsetError("ドット付きキーは未対応: " + key[:60])
    return key


def _split_items(body: str) -> list[str]:
    """引用符と入れ子の外側のカンマだけで割る。"""
    items, buf, quote, depth = [], "", "", 0
    for ch in body:
        if quote:
            buf += ch
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
            buf += ch
        elif ch in "[{":
            depth += 1
            buf += ch
        elif ch in "]}":
            depth -= 1
            buf += ch
        elif ch == "," and depth == 0:
            items.append(buf)
            buf = ""
        else:
            buf += ch
    if buf.strip():
        items.append(buf)
    return [i for i in items if i.strip()]


def _split_array(body: str) -> list:
    return [_parse_value(i) for i in _split_items(body)]


def _strip_comment(line: str) -> str:
    out, quote = "", ""
    for ch in line:
        if quote:
            out += ch
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
            out += ch
        elif ch == "#":
            break
        else:
            out += ch
    return out


def _mini_toml(text: str) -> dict:
    root: dict = {}
    current: dict = root
    # 明示的に `[x.y]` と宣言された表。同じ表を二度宣言するのは TOML では誤りで、
    # tomllib は落ちる。setdefault で黙って混ぜると、3.11 未満だけが**壊れた宣言を
    # 読めてしまう**。2026-09-26 に fleet_policy.toml の `[third_party]` 重複で実際に
    # 起き、ゲートは 3.13 では「読めなかった」と言い、3.9 では何も言わなかった。
    # 上位表を後から宣言するのは合法なので（[a.b] のあとの [a]）、直接の再宣言だけを見る。
    declared: set = set()

    def descend(parts: list[str], as_array: bool) -> dict:
        node = root
        for p in parts[:-1]:
            nxt = node.setdefault(p, {})
            if isinstance(nxt, list):
                nxt = nxt[-1]
            node = nxt
        last = parts[-1]
        if as_array:
            node.setdefault(last, [])
            node[last].append({})
            return node[last][-1]
        key = tuple(parts)
        if key in declared:
            raise TomlSubsetError("表を二度宣言している: [" + ".".join(parts) + "]")
        declared.add(key)
        return node.setdefault(last, {})

    lines = text.splitlines()
    i = 0
    while i < len(lines):
        n = i + 1
        line = _strip_comment(lines[i]).strip()
        i += 1
        if not line:
            continue
        if line.startswith("[["):
            current = descend(line[2:line.index("]]")].strip().split("."), True)
            continue
        if line.startswith("["):
            current = descend(line[1:line.index("]")].strip().split("."), False)
            continue
        if "=" not in line:
            raise TomlSubsetError(f"{n} 行目が k = v でない: {line[:60]}")
        key, _, raw = line.partition("=")
        key, raw = _bare_key(key), raw.strip()
        # 同じキーを二度書くのも誤り。後の値で黙って上書きすると、宣言を読んだ側は
        # どちらが効いたかを出力から知れない。tomllib と同じく落とす。
        if key in current:
            raise TomlSubsetError(f"{n} 行目でキーを二度宣言している: {key[:60]}")
        delim = next((d for d in ('"""', "'''") if raw.startswith(d)), "")
        if delim:
            raw = lines[n - 1].partition("=")[2].strip()   # 複数行文字列は # も本文
            while not (len(raw) >= 2 * len(delim) and raw.endswith(delim)):
                if i >= len(lines):
                    raise TomlSubsetError(f"{n} 行目の複数行文字列が閉じていない: {key}")
                raw += '\n' + lines[i]
                i += 1
            current[key] = _multiline_string(raw[len(delim):-len(delim)], delim)
            continue
        while _depth(raw) > 0:
            if i >= len(lines):
                raise TomlSubsetError(f"{n} 行目から始まる値が閉じていない: {key}")
            raw += " " + _strip_comment(lines[i]).strip()
            i += 1
        current[key] = _parse_value(raw)
    return root

# --------------------------------------------------------------------------- #
# registry（config 層）の所在
#
# registry は組織固有の事実（顧客名・リポ名・閾値）の置き場であり、work-os 本体
# とは別のリポジトリにある。同居していた頃は 14 ファイルがそれぞれ相対パスを
# 組み立てていた。移した先を全員に知らせる方法が無いのが同居の本質的な問題なので、
# 所在の判断はこの 1 関数だけが持つ。
#
# 探す順序: 環境変数 → 兄弟ディレクトリ → 同居していた頃の位置。
# 見つからなくても既定の場所を返す。「無い」は呼び側が判断する（ここでは落とさない）。
# --------------------------------------------------------------------------- #

REGISTRY_ENV = "WORKOS_REGISTRY"
REGISTRY_DIRNAME = "work-os-registry"


def registry_root() -> Path:
    """registry の所在。存在しなくても既定の場所を返す。"""
    env = os.environ.get(REGISTRY_ENV, "").strip()
    if env:
        return Path(env).expanduser()
    workos = Path(__file__).resolve().parent.parent
    sibling = workos.parent / REGISTRY_DIRNAME
    if sibling.is_dir():
        return sibling
    legacy = workos / "registry"          # 同居していた頃の配置
    if legacy.is_dir():
        return legacy
    return sibling


def registry_path(*parts: str) -> Path:
    """registry 配下のファイルを指す。参照はすべてここを通る。"""
    return registry_root().joinpath(*parts)


LAYERS = ("kernel", "golden_path", "config", "extension", "local")
STATUSES = ("local", "experimental", "proposed", "stable", "kernel", "deprecated")
PROMOTION_ORDER = ("local", "experimental", "proposed", "stable", "kernel")

RULE_OF_THREE = 3


# --------------------------------------------------------------------------- #
# データ構造
# --------------------------------------------------------------------------- #


@dataclass
class Capability:
    id: str
    layer: str = "local"
    status: str = "local"
    path: str = ""
    summary: str = ""
    used_by: list[str] = field(default_factory=list)
    invariant: list[str] = field(default_factory=list)
    configurable: list[str] = field(default_factory=list)
    note: str = ""

    @property
    def rank(self) -> int:
        try:
            return PROMOTION_ORDER.index(self.status)
        except ValueError:
            return -1


@dataclass
class Repo:
    """work.toml が **宣言している** 内容。推測はここには入らない。

    domain / role の既定は空文字である。"unknown" や "client" のような
    もっともらしい値を入れると、**宣言していないこと**が「そう宣言した」に化ける。
    そして化けた値は、名前から推測した答えより弱いのに、読む側では区別がつかない。

    実測 2026-09-26: `[publish]` だけを持ち `[repo]` が無い 1 本が、台帳の再生成で
    名前から正しく引けていたドメインと role="site" を失い、domain="unknown" /
    role="client" へ落ちた。リポも名前も変わっていない。work.toml を置いたことだけが
    原因で、**導入するほど台帳の分類が悪くなる**向きになっていた。
    宣言の不在は不在のまま返し、埋めるかどうかは読む側が決める。
    """

    root: Path
    name: str
    domain: str = ""
    role: str = ""
    status: str = "active"
    enforcement: str = "warn"
    domain_repo: str = ""
    kernel_version: str = ""
    layers: dict[str, list[str]] = field(default_factory=dict)
    capabilities: list[Capability] = field(default_factory=list)

    def layer_of(self, rel_path: str) -> str:
        """リポジトリ相対パスがどの層に属するかを返す。宣言がなければ local。"""
        rel = rel_path.replace("\\", "/").lstrip("./")
        best_layer, best_len = "local", -1
        for layer in ("kernel", "golden_path", "config", "extension"):
            for pattern in self.layers.get(layer, []):
                p = pattern.rstrip("/")
                if rel == p or rel.startswith(p + "/"):
                    if len(p) > best_len:
                        best_layer, best_len = layer, len(p)
        return best_layer

    def protected_paths(self) -> list[str]:
        return list(self.layers.get("kernel", [])) + list(self.layers.get("golden_path", []))


# --------------------------------------------------------------------------- #
# 読み込み
# --------------------------------------------------------------------------- #


def _load_toml(path: Path) -> dict:
    if tomllib is not None:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    return _mini_toml(path.read_text(encoding="utf-8"))


def load_repo(root: Path) -> Repo | None:
    """リポジトリのルートから work.toml を読む。無ければ None。"""
    root = Path(root)
    work = root / "work.toml"
    if not work.is_file():
        return None

    data = _load_toml(work)
    r = data.get("repo", {})
    ext = data.get("extends", {})
    layers = data.get("layers", {})

    repo = Repo(
        root=root,
        name=r.get("name") or root.name,
        domain=r.get("domain", ""),
        role=r.get("role", ""),
        status=r.get("status", "active"),
        enforcement=r.get("enforcement", "warn"),
        domain_repo=ext.get("domain_repo", ""),
        kernel_version=ext.get("kernel_version", ""),
        layers={k: list(v) for k, v in layers.items() if isinstance(v, list)},
    )
    repo.capabilities = load_capabilities(root)
    return repo


def load_capabilities(root: Path) -> list[Capability]:
    path = Path(root) / "capabilities.toml"
    if not path.is_file():
        return []
    data = _load_toml(path)
    out: list[Capability] = []
    for item in data.get("capability", []):
        out.append(
            Capability(
                id=item.get("id", ""),
                layer=item.get("layer", "local"),
                status=item.get("status", "local"),
                path=item.get("path", ""),
                summary=item.get("summary", ""),
                used_by=list(item.get("used_by", [])),
                invariant=list(item.get("invariant", [])),
                configurable=list(item.get("configurable", [])),
                note=item.get("note", ""),
            )
        )
    return out


def iter_repo_dirs(scan_root: Path, *, require_git: bool = True) -> list[Path]:
    """scan_root 直下から「リポジトリとして扱うディレクトリ」を集める。

    ここが唯一の定義である。engine の9ファイルがそれぞれ iterdir() を書いていて、
    条件が4通りに割れていた（2026-09-01 実測）。ドット名を除くものと除かないもの、
    .git を要求するものとしないもの。今の木ではどれも同じ答えを返すので実害は
    出ていなかったが、割れていること自体が実害になる前に寄せる。registry の所在を
    14ファイルから registry_root() 1本へ寄せたのと同じ理由（2026-08-31）。

    **この関数が答えるのは「リポジトリとは何か」だけである。**「今どれが欲しいか」は
    呼ぶ側で絞る。最初 group= をここに持たせたが、それは選択であって定義ではない。
    定義と選択を1つに混ぜると、次に別の絞り方が要る目的が来たときに、パラメータを
    足すか列挙を書き直すかの二択になる。どちらも寄せた意味を失う（2026-09-01 の
    レビュー指摘: 別々の目的が同じ手段のカーネルを使わされる形になる）。

    require_git=False は定義の側の分岐なので残す。「git になっていないものも
    リポジトリと呼ぶか」は、欲しい部分集合の話ではなく、何を数えるかの話である。
    """
    scan_root = Path(scan_root).expanduser()
    if not scan_root.is_dir():
        return []
    out: list[Path] = []
    for child in sorted(scan_root.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        if require_git and not (child / ".git").exists():
            continue
        out.append(child)
    return out


def git_link(repo: Path) -> tuple[str, str, str] | None:
    """`.git` がファイルなら、それが何へのポインタかを返す。clone なら None。

    戻り値は (種類, 本体のディレクトリ名, 枝の名前)。種類は "worktree" か "module"。

    「リポジトリとは何か」を答えるのが iter_repo_dirs である以上、「それは clone か、
    既にある clone に生えた枝か」もここで答える。存在だけを見ると両者は区別できない
    （worktree の `.git` はディレクトリではなくファイルだが、exists() は真になる）。

    実測 2026-09-27: worktree が1本できただけで台帳のリポジトリ数が 82 → 83 になり、
    remote も README も HEAD も同じ行が2つ並んだ。台帳は「別のプロダクトが2つある」と
    読める状態になり、--verify も gate も通った。**通ることが問題だった。**
    TK の git 規則は別ブランチでの作業に worktree を足すことを推奨しているので、
    これは今後も増える。数えるのをやめるのではなく、何であるかを言えるようにする。

    git を呼ばずに読む（work-os は標準ライブラリだけで動く）。worktree の
    `.git` は "gitdir: <本体>/.git/worktrees/<名前>"、submodule は
    ".../.git/modules/<名前>" を指す。
    """
    dot = repo / ".git"
    if not dot.is_file():
        return None
    try:
        text = dot.read_text(encoding="utf-8", errors="ignore").strip()
    except OSError:
        return None
    if not text.startswith("gitdir:"):
        return None
    target = text.split(":", 1)[1].strip()
    gitdir = Path(target) if Path(target).is_absolute() else (repo / target)
    kind = ""
    for marker, label in (("worktrees", "worktree"), ("modules", "module")):
        if marker in gitdir.parts:
            kind = label
            break
    if not kind:
        return None
    parts = list(gitdir.parts)
    # <本体>/.git/<marker>/<名前> の <本体> を取り出す
    idx = parts.index(".git") if ".git" in parts else -1
    parent = parts[idx - 1] if idx > 0 else ""
    branch = ""
    try:
        head = (gitdir / "HEAD").read_text(encoding="utf-8", errors="ignore").strip()
        if head.startswith("ref: refs/heads/"):
            branch = head[len("ref: refs/heads/"):]
    except OSError:
        pass
    return (kind, parent, branch)


def in_group(name: str, group: str | None) -> bool:
    """"<group>" と "<group>-*" に属するか。group が None なら全部が属する。

    絞り方の定義。iter_repo_dirs とは別に置く（片方は何を数えるか、こちらは
    そのうちどれを見るか）。同じ規則を scan と adopt が使うので1箇所に置くが、
    列挙そのものには混ぜない。
    """
    return group is None or name == group or name.startswith(group + "-")


def discover_repos(scan_root: Path) -> list[Repo]:
    """scan_root 直下のディレクトリから work.toml を持つものを集める。"""
    repos: list[Repo] = []
    for child in iter_repo_dirs(scan_root, require_git=False):
        repo = load_repo(child)
        if repo is not None:
            repos.append(repo)
    return repos


# --------------------------------------------------------------------------- #
# ハッシュ（同一性の判定）
# --------------------------------------------------------------------------- #


SOURCE_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".sh", ".sql", ".md", ".toml"}


def content_hash(path: Path, suffixes: set[str] | None = None) -> str:
    """ファイル or ディレクトリの内容ハッシュ。存在しなければ空文字。"""
    path = Path(path)
    suffixes = suffixes or SOURCE_SUFFIXES
    if path.is_file():
        return hashlib.sha256(path.read_bytes()).hexdigest()[:8]
    if not path.is_dir():
        return ""
    h = hashlib.sha256()
    for f in sorted(path.rglob("*")):
        if not f.is_file() or f.suffix not in suffixes:
            continue
        if any(part in {".git", "__pycache__", ".venv", "node_modules"} for part in f.parts):
            continue
        h.update(str(f.relative_to(path)).encode())
        h.update(f.read_bytes())
    return h.hexdigest()[:8]


# --------------------------------------------------------------------------- #
# 出力ヘルパ
# --------------------------------------------------------------------------- #


class Report:
    """warn / block を集約して終了コードを決める。"""

    def __init__(self, enforcement: str = "warn") -> None:
        self.enforcement = enforcement
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.notes: list[str] = []

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def note(self, msg: str) -> None:
        self.notes.append(msg)

    def emit(self) -> int:
        for m in self.notes:
            print(f"  ok    {m}")
        for m in self.warnings:
            print(f"  warn  {m}")
        for m in self.errors:
            print(f"  ERROR {m}")
        if not (self.errors or self.warnings):
            print("  ok    違反なし")
        if self.errors and self.enforcement == "block":
            return 1
        if self.errors:
            print("\n  enforcement = 'warn' のため通過します（block にすると停止します）。")
        return 0
