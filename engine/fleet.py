#!/usr/bin/env python3
"""fleet.py — 工程1 (Observe) をリポジトリ群そのものに適用する。

work-os の他のエンジンが「リポジトリの中身」を見るのに対し、これは
「リポジトリ群の状態」を見る。ゴミの定義を宣言ではなく観測で置き換える。

使い方:
    python3 engine/fleet.py ~/Documents/GitHub
    python3 engine/fleet.py ~/Documents/GitHub --json
    python3 engine/fleet.py ~/Documents/GitHub --risk-only
    python3 engine/fleet.py ~/Documents/GitHub --snapshot   # 履歴に1行追記
    python3 engine/fleet.py ~/Documents/GitHub --diff       # 前回からの悪化のみ出力

外部依存なし。観測は読み取りのみ。--snapshot だけが履歴ファイルに追記する。
--diff は悪化を検出したとき exit 1 を返すので cron / CI から使える。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path

# 観測された事実 -> リスク点。閾値は運用しながら調整する前提の config。
RISK = {
    "no_remote": 40,        # リモートが1つも無い = 消えたら終わり
    "unpushed_per_10": 10,  # どのリモート参照にも無いコミット 10個ごと
    "dirty_per_50": 10,     # 未コミット 50ファイルごと
    "stale_days_90": 15,    # 90日以上放置
    "no_readme": 5,         # 他人（未来の自分）が入れない
    "no_work_toml": 3,      # work-os 未導入
}
STALE_DAYS = 90
# 日付を観測できなかったことを表す。0 にすると「今日コミットされた」と
# 区別がつかなくなるので、比較に絶対に勝たない負値を使う。
UNKNOWN_IDLE = -1
ACTIVE_MINUTES = 60   # この時間内に更新された未コミットは「作業中」であってゴミではない
SWEEP_HOURS = 24      # 一斉変更を見る窓
SWEEP_REPOS = 3       # 同じコミットが何リポに出たら「一斉」とみなすか
sys.path.insert(0, str(Path(__file__).resolve().parent))
from workos import iter_repo_dirs, registry_path  # noqa: E402

POLICY = registry_path("fleet_policy.toml")
GITHUB_STATE = registry_path("github_state.json")
CLASSES = ("owned", "archived", "external", "personal", "team", "third_party")
# クラスごとに「無視するリスク要因」。owned だけが全ルールを受ける。
EXEMPT = {
    "owned": set(),
    "archived": {"unpushed", "dirty", "stale", "readme", "work_toml"},
    "external": {"unpushed", "dirty", "stale", "readme", "work_toml"},
    "personal": {"unpushed", "dirty", "stale", "readme", "work_toml"},
    "third_party": {"unpushed", "dirty", "stale", "readme", "work_toml"},
    # team: 他のエンジニアが稼働中。こちらは fetch するだけ。
    # ローカル独自コミットは「押してよい変更」ではなく「持ち込んではいけない差分」なので警告する。
    "team": {"stale", "readme", "work_toml"},
}
HISTORY = registry_path("fleet_history.jsonl")


def parse_day(text: str) -> date | None:
    """git の --date=short が返す YYYY-MM-DD を日付にする。読めなければ None。

    読めない場合に例外を握りつぶすと、days_idle が既定値のまま残り、
    「観測できなかった」と「今日コミットされた」が見分けられなくなる。
    失敗を値として返し、呼び手が UNKNOWN_IDLE のまま置く形にしている。
    """
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def idle_text(days: int, unit: str = "") -> str:
    """観測できなかった日数を数値として出さない。-1 は読み手には意味を持たない。"""
    return "?" if days < 0 else f"{days}{unit}"


def git(repo: Path, *args: str) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, timeout=30,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except (subprocess.TimeoutExpired, OSError):
        return ""


@dataclass
class RepoState:
    name: str
    last_commit: str = ""
    days_idle: int = UNKNOWN_IDLE
    commits: int = 0
    dirty: int = 0
    unpushed: int = 0
    has_remote: bool = False
    has_upstream: bool = False
    remote: str = ""
    branch: str = ""
    has_readme: bool = False
    has_work_toml: bool = False
    klass: str = "owned"
    klass_note: str = ""
    active_min: int = -1        # 未コミットの最新更新からの経過分。-1 は該当なし
    recent_subjects: list[str] = field(default_factory=list)  # 直近 SWEEP_HOURS のコミット件名
    risk: int = 0
    reasons: list[str] = field(default_factory=list)

    def score(self) -> None:
        r, why = 0, []
        ex = EXEMPT.get(self.klass, set())
        if not self.has_remote and self.klass == "owned":
            r += RISK["no_remote"]
            why.append(f"リモート無し（{self.commits}コミットがこのディスクにしか無い）")
        if self.unpushed > 0 and "unpushed" not in ex:
            pts = RISK["unpushed_per_10"] * ((self.unpushed + 9) // 10)
            r += pts
            why.append(f"ローカルのみ {self.unpushed}コミット（{self.branch}）")
        if 0 <= self.active_min < ACTIVE_MINUTES:
            # 進行中の作業に「コミットしろ」と言わない。別セッションが書いている最中かもしれない。
            why.append(f"作業中（{self.active_min}分前に更新）")
        elif self.dirty > 0 and "dirty" not in ex:
            pts = RISK["dirty_per_50"] * ((self.dirty + 49) // 50)
            r += pts
            why.append(f"未コミット {self.dirty}ファイル")
        if self.days_idle >= STALE_DAYS and "stale" not in ex:
            r += RISK["stale_days_90"]
            why.append(f"{self.days_idle}日放置")
        if not self.has_readme and "readme" not in ex:
            r += RISK["no_readme"]
            why.append("README無し")
        if not self.has_work_toml and "work_toml" not in ex:
            r += RISK["no_work_toml"]
            why.append("work.toml 未導入")
        if self.klass != "owned" and not why:
            why.append(f"class={self.klass}（統治対象外）")
        self.risk, self.reasons = r, why


def load_policy() -> dict[str, tuple[str, str]]:
    """fleet_policy.toml を読み、{repo名: (クラス, 理由)} を返す。"""
    if not POLICY.exists():
        return {}
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from workos import _load_toml  # noqa: PLC0415
    data = _load_toml(POLICY)
    out: dict[str, tuple[str, str]] = {}
    for klass in CLASSES:
        for name, note in (data.get(klass) or {}).items():
            out[name] = (klass, str(note))
    return out


def refresh_github_state(repos: list[Path]) -> dict:
    """gh CLI で各 owner のリポジトリ一覧を引き、archived フラグを保存する。

    GitHub 上で archived にした事実は、こちらが宣言し直さなくても読めるべきである。
    同じ見落としを3度やったので自動化した。gh が無ければ静かに諦める。
    """
    owners = set()
    for p in repos:
        url = git(p, "remote", "get-url", "origin")
        if "github.com" in url:
            part = url.split("github.com")[-1].lstrip(":/").removesuffix(".git")
            if "/" in part:
                owners.add(part.split("/")[0])
    state: dict[str, dict] = {}
    for owner in sorted(owners):
        for kind in ("orgs", "users"):
            try:
                out = subprocess.run(
                    ["gh", "api", f"{kind}/{owner}/repos", "--paginate",
                     "--jq", ".[] | {full_name, archived}"],
                    capture_output=True, text=True, timeout=120,
                )
            except (subprocess.TimeoutExpired, OSError):
                continue
            if out.returncode != 0:
                continue
            for line in out.stdout.splitlines():
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                state[rec["full_name"]] = {"archived": bool(rec.get("archived"))}
            break
    GITHUB_STATE.parent.mkdir(parents=True, exist_ok=True)
    GITHUB_STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return state


def load_github_state() -> dict:
    if not GITHUB_STATE.exists():
        return {}
    try:
        return json.loads(GITHUB_STATE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def sweeps(states: list[RepoState]) -> list[tuple[str, list[str]]]:
    """直近 SWEEP_HOURS に SWEEP_REPOS 以上のリポで走った同一コミットを見つける。

    1リポの変更は普通のことで、危ないのはいつも同時多発のほうである。
    単発は自由、一斉は要確認、という非対称にする。
    """
    hits: dict[str, list[str]] = {}
    for s in states:
        for subj in set(s.recent_subjects):
            hits.setdefault(subj, []).append(s.name)
    return sorted(((k, v) for k, v in hits.items() if len(v) >= SWEEP_REPOS),
                  key=lambda kv: -len(kv[1]))


def observe(repo: Path, today: date, policy: dict | None = None,
            gh_state: dict | None = None) -> RepoState | None:
    if not (repo / ".git").exists():
        return None
    s = RepoState(name=repo.name)
    s.klass, s.klass_note = (policy or {}).get(repo.name, ("owned", ""))
    s.last_commit = git(repo, "log", "-1", "--format=%cd", "--date=short")
    d = parse_day(s.last_commit)
    if d is not None:
        s.days_idle = (today - d).days
    s.commits = int(git(repo, "rev-list", "--count", "HEAD") or 0)
    s.branch = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    dirty_paths = [ln[3:].strip('"') for ln in git(repo, "status", "--porcelain").splitlines() if ln]
    s.dirty = len(dirty_paths)
    newest = 0.0
    for rel in dirty_paths:
        f = repo / rel
        try:
            if f.is_dir():
                newest = max([newest, *(c.stat().st_mtime for c in f.rglob("*") if c.is_file())])
            elif f.exists():
                newest = max(newest, f.stat().st_mtime)
        except OSError:
            continue
    if newest:
        s.active_min = max(0, int((time.time() - newest) // 60))
    s.recent_subjects = [ln for ln in git(
        repo, "log", f"--since={SWEEP_HOURS} hours ago", "--format=%s").splitlines() if ln]
    s.remote = git(repo, "remote", "get-url", "origin")
    if s.klass == "owned" and s.remote and gh_state:
        slug = s.remote.split("github.com")[-1].lstrip(":/").removesuffix(".git")
        if gh_state.get(slug, {}).get("archived"):
            s.klass, s.klass_note = "archived", "GitHub 上で archived"
    s.has_remote = bool(git(repo, "remote"))
    s.has_upstream = bool(git(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"))
    if s.has_remote:
        # upstream の有無ではなく「どのリモート参照からも辿れないコミット数」を数える。
        # upstream 未設定のフィーチャーブランチと、リモート自体が無い状態は別物である。
        s.unpushed = int(git(repo, "rev-list", "--count", "HEAD", "--not", "--remotes") or 0)
    else:
        s.unpushed = s.commits
    s.has_readme = any((repo / n).exists() for n in ("README.md", "README.rst", "README"))
    s.has_work_toml = (repo / "work.toml").exists()
    if s.has_work_toml:
        # 各リポの宣言が中央ポリシーより優先される（現場が上位）。
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from workos import _load_toml  # noqa: PLC0415
        try:
            declared = str((_load_toml(repo / "work.toml").get("repo") or {}).get("status", ""))
        except Exception:  # noqa: BLE001
            declared = ""
        if declared in CLASSES:
            s.klass, s.klass_note = declared, "work.toml の宣言"
    s.score()
    return s


def write_snapshot(root: Path, today: date, states: list[RepoState]) -> None:
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "observed_at": today.isoformat(),
        "root": str(root),
        "total": len(states),
        "repos": [asdict(s) for s in states],
    }
    with HISTORY.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def band(risk: int) -> str:
    if risk >= 40:
        return "CRITICAL"
    if risk >= 20:
        return "HIGH"
    if risk >= 10:
        return "MEDIUM"
    return "OK"


def load_last() -> dict:
    """履歴の最終行を返す。無ければ空。"""
    if not HISTORY.exists():
        return {}
    last = ""
    with HISTORY.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                last = line
    try:
        return json.loads(last) if last else {}
    except json.JSONDecodeError:
        return {}


def regressions(prev: dict, states: list[RepoState]) -> list[str]:  # noqa: C901
    """前回スナップショットからの「悪化」だけを言葉にする。

    改善は報告しない。毎日読むものは、行動が要るものだけであるべき。
    """
    if not prev:
        return ["初回スナップショット（比較対象なし）"]
    old = {r["name"]: r for r in prev.get("repos", [])}
    out = []
    for s in states:
        o = old.get(s.name)
        if o is None:
            out.append(f"{s.name}: 新規リポジトリ（risk {s.risk}）")
            continue
        if s.unpushed > o.get("unpushed", 0):
            out.append(f"{s.name}: ローカルのみのコミットが "
                       f"{o.get('unpushed', 0)} → {s.unpushed}（{s.branch}）")
        if s.dirty >= o.get("dirty", 0) + 20:
            out.append(f"{s.name}: 未コミットが {o.get('dirty', 0)} → {s.dirty}"
                       "（新しいゴミ生成器の可能性）")
        if s.days_idle >= STALE_DAYS > o.get("days_idle", 0):
            out.append(f"{s.name}: {s.days_idle}日放置に到達")
        if o.get("has_remote") and not s.has_remote:
            out.append(f"{s.name}: リモートが外れた")
    for name in old:
        if not any(s.name == name for s in states):
            out.append(f"{name}: 消滅（移動・削除）")
    for subj, names in sweeps(states):
        out.append(f"一斉変更: {len(names)}リポで同じコミット「{subj[:56]}」")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="リポジトリ群の健全性を観測する")
    ap.add_argument("root", nargs="?", default=".", help="リポジトリを含む親ディレクトリ")
    ap.add_argument("--json", action="store_true", help="JSON で出力")
    ap.add_argument("--risk-only", action="store_true", help="risk>0 のみ表示")
    ap.add_argument("--today", default="", help="基準日 YYYY-MM-DD（テスト用）")
    ap.add_argument("--snapshot", action="store_true", help="観測結果を履歴に追記")
    ap.add_argument("--refresh-github", action="store_true",
                    help="gh で archived フラグを取得し直す（要 gh CLI）")
    ap.add_argument("--diff", action="store_true",
                    help="前回スナップショットからの悪化のみ出力（悪化があれば exit 1）")
    args = ap.parse_args()

    root = Path(args.root).expanduser().resolve()
    if not root.is_dir():
        print(f"error: {root} はディレクトリではありません", file=sys.stderr)
        return 2
    today = datetime.strptime(args.today, "%Y-%m-%d").date() if args.today else date.today()

    policy = load_policy()
    repos = iter_repo_dirs(root)
    gh_state = refresh_github_state(repos) if args.refresh_github else load_github_state()
    states = [s for p in repos if (s := observe(p, today, policy, gh_state))]
    states.sort(key=lambda s: (-s.risk, s.name))
    shown = [s for s in states if s.risk > 0] if args.risk_only else states

    if args.diff:
        regs = regressions(load_last(), states)
        if args.snapshot:
            write_snapshot(root, today, states)
        if not regs:
            print(f"[{today}] 悪化なし（{len(states)} リポジトリ観測）")
            return 0
        print(f"[{today}] 前回からの悪化 {len(regs)} 件")
        for line in regs:
            print(f"  - {line}")
        return 1

    if args.snapshot:
        write_snapshot(root, today, states)
        print(f"snapshot 追記: {HISTORY}（{len(states)} リポジトリ）")
        if not (args.json or args.risk_only):
            return 0

    if args.json:
        print(json.dumps({
            "root": str(root),
            "observed_at": today.isoformat(),
            "total": len(states),
            "repos": [asdict(s) for s in shown],
        }, ensure_ascii=False, indent=2))
        return 0

    print(f"{'REPO':<32} {'RISK':>4}  {'BAND':<9} {'CLASS':<11} "
          f"{'IDLE':>5} {'DIRTY':>6} {'LOCAL':>6}  理由")
    print("-" * 130)
    for s in shown:
        print(f"{s.name:<32} {s.risk:>4}  {band(s.risk):<9} {s.klass:<11} "
              f"{idle_text(s.days_idle):>5} {s.dirty:>6} {s.unpushed:>7}  "
              f"{' / '.join(s.reasons)}")

    tot = len(states)
    crit = sum(1 for s in states if s.risk >= 40)
    high = sum(1 for s in states if 20 <= s.risk < 40)
    unbacked = [s for s in states if not s.has_remote]
    lost = sum(s.unpushed for s in states)
    print("-" * 118)
    print(f"合計 {tot} リポジトリ / CRITICAL {crit} / HIGH {high}")
    print(f"リモート未設定 {len(unbacked)} リポジトリ: {', '.join(s.name for s in unbacked) or 'なし'}")
    print(f"どのリモートにも存在しないコミット {lost} 個 / "
          f"{sum(1 for s in states if s.unpushed)} リポジトリ")
    print(f"work.toml 導入済み {sum(1 for s in states if s.has_work_toml)}/{tot}")
    # 同じ remote を指すローカルクローンは、別リポではなく同じリポの2つ目の作業コピーである。
    # 数を水増しし、片方が黙って古くなるので、見つけたら出す。
    by_remote: dict[str, list[RepoState]] = {}
    for s_ in states:
        slug = s_.remote.split("github.com")[-1].lstrip(":/").removesuffix(".git")
        if slug:
            by_remote.setdefault(slug, []).append(s_)
    clones = {k: v for k, v in by_remote.items() if len(v) > 1}
    if clones:
        print(f"\n■ 同じ remote の重複クローン ── {len(clones)}組")
        for slug, group in clones.items():
            names = ", ".join(f"{g.name}({idle_text(g.days_idle, '日前')})" for g in group)
            print(f"  {slug} → {names}")

    sw = sweeps(states)
    if sw:
        print(f"\n■ 直近{SWEEP_HOURS}時間の一斉変更 ── {len(sw)}件（単発は自由、一斉は要確認）")
        for subj, names in sw[:5]:
            head = ", ".join(names[:6]) + (f" ほか{len(names) - 6}" if len(names) > 6 else "")
            print(f"  {len(names):>3}リポ  {subj[:64]}")
            print(f"        {head}")
        print()
    nono = [s for s in states if s.klass != "owned"]
    if nono:
        print("統治対象外: " + ", ".join(f"{s.name}({s.klass})" for s in nono))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
