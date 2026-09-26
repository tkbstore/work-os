# work-os

**仕事のやり方そのものを、所有できる 1 リポジトリとして持つ。**

複数のリポジトリを実測し、「どれが共通でどれが個別か」を `work.toml` という 1 枚の宣言に落とし、
その宣言を機械が検証する。protocol と gate の最小セットです。
プロダクトではありません。外部依存はゼロ、Python 標準ライブラリだけで動きます。

## 入れる

ゲート（`release_gate`）だけを使うなら 1 行です。

```bash
curl -fsSL https://raw.githubusercontent.com/tkbstore/work-os/main/install.sh | sh
workos-gate <path-to-a-repo>        # そのリポジトリを外に出せるかを見る
workos-gate --checks                # 何を見ているのかを列挙する（対象は要りません）
```

`~/.local/share/workos-gate` に展開して `~/.local/bin/workos-gate` を置くだけです。
pip も npm も使いません。入れた直後は work-os が持つ**判定基準の骨格**だけで動きます
（顧客名のような組織固有の事実は同梱しないので、その分の観測は skip と出ます。
自分の registry を持たせるときは `WORKOS_REGISTRY` でそこを指します）。

work-os 全体（`scan` / `adopt` / `promote`）を使うなら clone します。

```bash
git clone https://github.com/tkbstore/work-os work-os
cd work-os

# 何ができるかを見る
python3 engine/release_gate.py --help

# 自分の git 群を実測する（既存には一切触れない読み取り専用）
python3 engine/scan.py ~/src --group my-domain
```

## 必要なもの

Python 3.9 以上。それだけです。インストールも設定ファイルの記入も要りません。

3.11 未満には `tomllib` が無いので、work-os が持つ TOML の部分集合パーサで読みます。
`tests/test_toml_fallback.py` が実データで `tomllib` と同値であることと、表せない形では
推測せずに落ちることを突き合わせています（実測 2026-09-26: 3.9.6 と 3.13.5 で
このリポジトリへの判定が byte 単位で一致）。

`scan.py` は書き込みを一切しません。まずこれだけ回して、出てきた表を見てください。

## 使い方

観測 → 宣言 → 検証 → 昇格の順に進みます。途中で止めても壊れません。

```bash
# 1. 実測する（読み取り専用）
python3 engine/scan.py <repos> --group <domain-prefix>

# 2. 宣言を置く（work.toml を 1 枚足すだけ）
python3 engine/adopt.py <repos>/<a-repo> --write

# 3. 検証する
python3 engine/validate.py <repos>/<a-repo>

# 4. 昇格候補を出す
python3 engine/promote.py <repos>

# 5. 外に出せるかを見る（[publish] を宣言したリポだけ）
python3 engine/release_gate.py <repos>/<a-repo> --execute

# 何を見ているのかを先に読む（リポジトリに依らない。--json で機械可読）
python3 engine/release_gate.py --checks

# 手元の木をまとめて見る（自組織のリポなので --scan は所有を主張する）
python3 engine/release_gate.py --scan <repos>
```

判定は **累積の 2 段**です。段ごとに基準を作るのではなく、1 本の梯子を途中で区切ります。

| 段 | 通ると何ができるか | 見るレーン |
|----|------------------|-----------|
| 1. `internal` | 社内で共有する | `safety` `portability` `provenance` `correctness`（顧客名とテストは警告のみ） |
| 2. `public` | public リポジトリとして切り出す | `safety` `provenance` `correctness`（同じ観測を severity を上げて当て直す）+ `history` `entry` `usability` `agent_ready` `robustness` |

この表は要約です。**どの段でどの観測がどの severity で当たるかは
`release_gate.py --checks` が判定基準そのものから出します。**
食い違ったら `--checks` のほうが正しい（この表は 2026-09-27 に実際に 3 箇所ずれていました。
段に `correctness` と `history` を足したのに、ここを直していませんでした）。

第 1 段は「出した瞬間に取り返しがつかないもの」だけを見ます。README が無くても同僚には
渡せますが、鍵が入っていたら渡せません。顧客名は同僚にとって在って当然のものなので、
一覧は出しますが止めません（外販物に同梱するかどうかは、リポジトリ単位で決まる問いでは
ないためです）。第 2 段は漏れていないことを前提に、
「clone した他人が誰にも聞かずに動かせるか」を見ます。落ちた段より上は測りません。

導入手順は [docs/ADOPTION.md](./docs/ADOPTION.md)。
すべて additive で、`enforcement = "warn"` から始まります。warn の間に止まるのは
**`kernel` と宣言したパスへの AI の書き込みだけ**です（憲法 §6-1 が enforcement に
条件を付けずに禁じているため）。`kernel` を空で始めれば、文字どおり何も止まりません。

---

## 何を解決するのか

案件ごとにリポジトリを複製して育てると、こうなります。

- 共通部分を直しても、**既存のリポジトリには永久に届かない**（共通の祖先が無い）
- リポジトリ間の差分の大半は顧客要件の差ではなく、**複製した時期の差**
- どれが共通でどれが個別か、誰も宣言していないので、**人も AI も安心して触れない**

work-os は、この 3 つを「宣言」で解ける問題に変えます。

```toml
# work.toml — 各リポジトリに 1 枚置くだけ
[repo]
domain      = "marketing"
role        = "client"
enforcement = "warn"      # golden_path は警告だけ（kernel は warn でも止まる）

[layers]
kernel      = ["packages/core", "packages/shared"]   # 誰も変えない
golden_path = ["scripts/shared"]                     # 中央が配る
config      = ["config"]                             # 現場が変える
extension   = ["mcp_servers/gbp"]                    # 現場が足す
# 書かれていないものはすべて local = 完全自由
```

宣言された瞬間に、次のことが自動で効きます。

| 仕組み | いつ効くか | 何をするか |
|--------|-----------|-----------|
| `hooks/claude_guard.py` | AI が編集しようとした瞬間 | kernel を書き換えようとしたら止める（または警告する） |
| `hooks/root_cause_guard.py` | AI が編集しようとした瞬間 | 症状だけを消す変更（抑制・テスト停止・閾値の緩和）に理由を要求する |
| `engine/validate.py` | セッション終了時 | 昇格レーンと 3 社ルールの違反を検出する |
| `engine/scan.py` | いつでも | 複数リポジトリを実測し、共通性・変動点・世代を出す |
| `engine/promote.py` | 週次 | 「昇格してよいもの」を証拠から機械が判定する |
| `engine/release_gate.py` | 外に出す前 | 社内で共有できるか / public に切り出せるかを 2 段で観測する |

`engine/validate.py` は `scripts/workos-qa.py` にまとめてあり、セッション終了時に
Stop hook（`repo-quality-gate`）が `scripts/*-qa.py` を拾って走らせます。CI は使いません
（private リポの GitHub Actions は従量課金で、中身と無関係に赤くなるため）。

## 五つの層

すべてのファイルは、**誰が変えてよいか**で 5 層のどれかに属します。

```
        Invariant Kernel      壊れると他リポジトリが壊れるもの。誰も変えない
        ────────────────
        Golden Path           推奨され、最も簡単な道。中央が配る。強制はしない
        ────────────────
        Configuration         コードを書かずに現場が変えられる
        ────────────────
        Extension             現場・第三者がコードで足せる
        ────────────────
        Escape Hatch (local)  完全自由。ここは絶対に塞がない
```

重要なのはパーソナライズできること自体ではなく、
**何を変えても全体が壊れないかが定義されていること**です。

### Configuration 層はこのリポジトリに在りません

判定基準（レーンの閾値・秘密のパターン・痕跡の宣言）と組織固有の事実は、
別のリポジトリ **`work-os-registry`** に置いています。組織の事実を、公開しうる
リポジトリの履歴に残さないためです。

所在を決めるのは `engine/workos.py` の `registry_root()` だけで、
`WORKOS_REGISTRY` → `../work-os-registry` → `work-os/registry` の順に探します。

registry が無くても各 hook は動きますが、**公開ゲートは判定基準そのものを失うので
何も判定できません**（`判定基準が見つかりません: .../release_lanes.toml` で終わります）。
自分の組織で使うときは、手で書くファイルを持つディレクトリを1つ作ってください。

| | ファイル | 欠けたとき |
|---|---|---|
| ゲートに必須 | `release_lanes.toml` `secret_patterns.toml` `patch_smells.toml` `private_terms.toml` `naming.toml` | 停止するか、検査が空になる |
| 任意 | `fleet_policy.toml` `known_generators.toml` | 既定に落ちるだけ（全リポが owned 扱い／生成器がすべて「未宣言」に出る） |

`catalog.toml` / `repos.toml` / `github_state.json` / `fleet_history.jsonl` は engine が
作るので、手では書きません。**雛形はまだ同梱していません**（`templates/` に在るのは
work.toml と capabilities.toml の雛形だけです）。

日々の設計判断とその経緯（なぜその閾値なのか、どのガードがどう破られたか）は
registry 側の `handoff_YYYY-MM-DD.md` に時系列で残しています。**このリポジトリの
コミットだけを読んでも、なぜそうなっているかには届きません。**

## 昇格レーン

「何度も要求されたから Core 化」は禁止しています。要求された回数ではなく、証拠で上げます。

```
local ──▶ experimental ──▶ proposed ──▶ stable ──▶ kernel
              1リポで動く      3リポで必要      2週間不変      他ドメインに影響
```

条件は [CONSTITUTION.md](./CONSTITUTION.md) に固定されています。
ここを変えるには PR が必要で、変えたら全ドメインの `kernel_version` が上がります。

## 新しい仕事を足す

ドメインを 1 つ足すだけです。marketing も sales も governance も、同じ形をしています。

```bash
cp -r templates/domain <repos>/<new-domain>-core
```

work-os 自身もこの規約に従っています（このリポジトリにも `work.toml` があります）。
このフレームワークで、このフレームワークを開発する——入れ子は不具合ではなく、使い方です。

## 閾値の根拠

公開ゲートの判定基準は好みではなく実測に由来する、というのがこのリポジトリの主張です。
その主張を誰でも疑えるように、測った対象と結果を [calibration/](./calibration/) に置いています。

```bash
python3 engine/calibrate.py --dry-run   # 宣言の矛盾だけを検査（clone しない）
python3 engine/calibrate.py --write     # 実際に測り直す
```

物差し自体を疑うために、外部の公開リポジトリと手元の全リポジトリの両方に当てて、
**偽陽性（出せるものを落とす）と偽陰性（出せないものを通す）**を数えています。
読み方と限界は [calibration/README.md](./calibration/README.md)。

## 直す・報告する

層ごとに何がかかるかは [CONTRIBUTING.md](./CONTRIBUTING.md)。
バグと共通化の相談は [Issue テンプレート](.github/ISSUE_TEMPLATE/)から。

## ライセンス

[MIT](./LICENSE)。
