# 直し方

work-os の変更には、**どの層を触るか**で違う手順がかかります。
層は [CONSTITUTION.md](./CONSTITUTION.md) §3 の5つで、`work.toml` の `[layers]` に宣言されています。
そこに書かれていないパスは `local`（完全自由）です。

まず、自分が触ろうとしている場所がどの層かを機械に聞いてください。

```bash
python3 engine/validate.py .
```

---

## 出す前に走らせるもの

3つとも通ることが、レビューを頼む前提です。ローカルで数秒です。

```bash
python3 tests/run_all.py              # テスト（tests/test_*.py を全部）
python3 engine/validate.py .          # 宣言の検証（層・昇格レーン・3社ルール）
python3 engine/release_gate.py . --execute   # 公開ゲート（2段ラダー）
```

同じ3つを CI（`.github/workflows/validate.yml`）が毎回走らせます。
手元で通らないものは CI でも通りません。

## 層ごとに要るもの

| 触る層 | 追加でかかるもの |
|--------|------------------|
| `local`（宣言に無いパス） | 何も。自由に変えてください |
| `config`（別リポの `work-os-registry/` など） | コードを変えずに直せるはずです。コードを変えたくなったら、それは層の選び方が違うサインです |
| `extension` | テストを1つ足してください |
| `golden_path`（`engine/` の大半） | テスト。既存の利用者が壊れないこと |
| `kernel`（`CONSTITUTION.md`, `engine/workos.py`） | PR 必須。全ドメインの `kernel_version` を上げる。壊れると他リポジトリが壊れる理由を PR に書く |

`kernel` は編集しようとした時点でフック（`hooks/claude_guard.py`）が止めます。
止まったら、まず「本当に kernel か」を疑ってください。憲法 §3 の定義は
**壊れると他のリポジトリが壊れるもの。それ以外は Kernel ではない**です。

## 機能を足すとき

憲法 §6-2 は「登録されていない成果物は存在しない」と決めています。
新しい capability は `capabilities.toml` に登録してください。登録しないと、
次のリポジトリに引き継がれません。

```toml
[[capability]]
id        = "your-thing"
path      = "engine/your_thing.py"
layer     = "extension"      # 最初は必ず下から
status    = "experimental"
summary   = "1行で。何をするか"
used_by   = ["この機能を実際に使っている場所"]
invariant = ["変えたら利用者が壊れるもの"]
configurable = ["現場が変えてよいもの"]
```

**いきなり上の層に置かないでください。** 昇格は `local → experimental → proposed
→ stable → kernel` の順にしか進みません。要求された回数ではなく、証拠で上がります
（憲法 §4）。`experimental → proposed` には3つ以上のリポジトリで必要になったこと
（Rule of Three）が要ります。人間の裁量で飛ばすことは §6-3 が禁じています。

昇格してよいものは機械が判定します。

```bash
python3 engine/promote.py <repos>
```

## テストの足し方

`tests/test_*.py` に置けば、`tests/run_all.py` が自動で見つけます。
**ファイル名をどこにも登録しないでください。** 登録する形にすると、足したテストが
宣言から漏れて「在るのに一度も走らない」状態が起きます（実際に起きました）。

書き方の原則が1つあります。**手元の環境に依存しないこと。**
隣のディレクトリや自分のホームを読むテストは、新規 clone と CI では
落ちるのではなく黙って空回りします。必要な状態は `tempfile` で自分で作ってください。
見本は `tests/test_guards.py` の `build_fixture()` です。

## 症状ではなく原因を直す

検査が赤いとき、赤いのを消す方法は2つあります。原因を直すか、報告のほうを黙らせるか。
後者には決まった形があるので、`hooks/root_cause_guard.py` が機械で見ています。

- 抑制の注記（`noqa` / `type: ignore` / `eslint-disable` / `@ts-ignore` の類）
- テストの停止（`skip` / `xfail` / `only`）
- 合格線を緩める向きの変更（`timeout` を上げる / `min_coverage` を下げる）
- 除外リストや拡張子の列挙に項目を足す

**禁止ではありません。** 抑制が正しい場面は実在します（生成コード、外部由来の警告、
意図的に壊した fixture）。要求しているのは、同じ場所に**理由**を書くことだけです。

```python
value = compute()  # 抑制の注記  # 生成コードなので行長は直せない
```

理由が書けないなら、消しているのは報告のほうで、原因はまだ残っています。
痕跡の一覧は `work-os-registry/patch_smells.toml`（別リポ）。切りたいときは `WORKOS_ROOT_CAUSE=off`。

とくに**列挙で逃がす**形は、逃がしそこねた場所が黙って素通りするので危険です。
実測の例: 公開ゲートの絶対パス検査が拡張子5個の列挙だったため、実際に含んでいた
23 リポジトリのうち 16 を pass にしていました（見逃し率 70%）。

## 書いてはいけないもの

公開ゲートの第 1 段（`safety` / `portability`）が止めます。
ここに引っかかると社内で共有することもできないので、先に知っておくと手戻りが減ります。
顧客名（`provenance`）は止めません。同僚にとっては在って当然のものなので、
一覧を出すだけにしてあります。外販物に同梱するときに、その一覧を見て消してください。

- **固有名詞**（組織名・顧客名・社内の呼び名）。work-os は抽象層です。
  実例が要るときは `<repos>` `<a-repo>` のようなプレースホルダを使ってください
- **自分の環境の絶対パス**（ホームディレクトリを展開した形）。
  他人にとっては動かないものと同じです。`~` や相対パスを使ってください。
  拡張子は問いません（`.md` も `.json` も見ます）
- **秘密**（鍵・トークン・実体の `.env`）。出した瞬間に漏洩が確定します

手元で確認できます。

```bash
python3 engine/abstraction_gate.py
```

## 判定基準を変えたいとき

`work-os-registry/release_lanes.toml` のチェックを足す・severity を動かすときは、
それが群を分けるという観測を添えてください。好みで人を落とさないための手順です。

```bash
python3 engine/calibrate.py --write
```

外のリポジトリに同じ物差しを当てて、通過率の差を出します。差が小さいものを
`block` に置かないでください。詳しくは [calibration/README.md](./calibration/README.md)。

## コミットメッセージ

履歴は Conventional Commits で揃っています。

```
<type>: <説明>

<なぜそうしたか。何が壊れていて、どう直したか>
```

type は `feat` / `fix` / `refactor` / `docs` / `test` / `chore` / `perf` / `ci`。

本文には「何をしたか」より **「なぜ壊れていたか」** を書いてください。
このリポジトリのコメントとコミットは、どれも「実際にこう壊れた」を残す形で
書かれています。同じ穴が二度開かないようにするためです。

## 新しいファイルは作った時点で追跡する

```bash
git add path/to/new_file.py
```

チェックポイント的なコミットは追跡済みファイルの変更しか拾いません。
新規ファイルを未追跡のまま置くと、それを参照するコードだけが commit され、
**HEAD が単体で動かない**状態になります。実際に起きています。

## バグを報告する

[Issue テンプレート](.github/ISSUE_TEMPLATE/)を使ってください。
再現手順が無い報告は、こちらでは再現できないので進みません。
