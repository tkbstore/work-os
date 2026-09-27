# 導入手順

**前提：既存のリポジトリは 1 行も壊しません。**
やることは `work.toml` を 1 枚置くことと、hook を 1 本入れることだけです。
合わなければ `work.toml` を消せば元に戻ります。

`enforcement = "warn"` の間も、**`kernel` と宣言したパスへの AI の書き込みだけは
止まります**（憲法 §6-1 が enforcement に条件を付けずに禁じているため。
`hooks/claude_guard.py` がその1点だけ warn に従いません）。この手順では `kernel` を
空で始めるので、Day 3 まで実際に止まるものはありません。

以下、パスは `<repos>` 前提です。

---

## Day 0 — 実測する（15分・書き込みなし）

まだ何も置きません。まず今どうなっているかを見ます。

```bash
cd <repos>/work-os

# 1. 全リポジトリの棚卸し（どのドメインに何本あるか）
# 出力先は config 層（別リポ）。所在は engine/workos.py registry_root() が決める
python3 engine/registry.py <repos> > ../work-os-registry/repos.toml
less ../work-os-registry/repos.toml

# 2. marketing ドメインの共通性・変動点・複製世代を実測
python3 engine/scan.py <repos> --group <domain-prefix>
```

`scan` の「世代の検出」で、**版数が社数よりずっと少ない**ものが出てきます。
その差は顧客要件ではなく複製時期の差なので、統合コストは見た目より低い、という判断ができます。

---

## Day 1 — 中央に宣言を置く（30分）

まずドメインの中央リポジトリだけ。クライアントリポには触れません。

```bash
cd <repos>/work-os

# ドメインリポジトリとして宣言する
python3 engine/adopt.py <repos>/<a-domain-repo>        # 内容を確認
python3 engine/adopt.py <repos>/<a-domain-repo> --write

# capabilities.toml の草案を実測から生成する
python3 engine/scan.py <repos> --group <domain-prefix> --emit \
  > <repos>/<a-domain-repo>/capabilities.toml
```

ここで **手を入れるのは 1 か所だけ**です。生成された `capabilities.toml` を開いて、
各 capability の `layer` を上から見ていき、
**「壊れると他のリポジトリが壊れるか？」に Yes と答えられないものを `kernel` から降ろします。**

降ろす先の目安：

| 迷ったら | 層 |
|---|---|
| 他リポが壊れる | `kernel` |
| 中央が配りたいが、外れてもよい | `golden_path` |
| コードを書かずに変えたい | `config` |
| そのリポだけで足したもの | `extension` |

終わったら検証します。

```bash
python3 engine/validate.py <repos>/<a-domain-repo>
```

人が読む一覧は `capabilities.toml` をそのまま読みます。

---

## Day 2 — クライアントリポに宣言を配る（20分）

13 本まとめて置けます。生成されるのは `work.toml` 1 枚だけです。

```bash
cd <repos>/work-os

# まず表示して確認（--write なし）
python3 engine/adopt.py <repos> --group <domain-prefix>

# よければ書き込む
python3 engine/adopt.py <repos> --group <domain-prefix> \
  --domain-repo <a-domain-repo> --write

# 全部まとめて検証
python3 engine/validate.py <repos>/<a-domain-repo>-*
```

この時点では警告が大量に出ます。**それが正常です。**
警告は「宣言と実態がずれている箇所の一覧」であって、直す義務はまだありません。

---

## Day 3 — AI に守らせる（10分・ここが本題）

人間が気をつけるのではなく、書き込みの瞬間に止めます。

```bash
cd <repos>/work-os
bash hooks/install.sh <repos>/<a-repo>
```

`.claude/settings.json` が既にあるリポジトリでは、貼り付ける JSON が表示されるので手でマージしてください。

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Edit|Write|MultiEdit|NotebookEdit",
        "hooks": [
          { "type": "command",
            "command": "python3 <repos>/work-os/hooks/claude_guard.py" }
        ]
      }
    ]
  }
}
```

動作確認：`kernel` に宣言したファイルを Claude に編集させると、警告が出ます。
`work.toml` の `enforcement` を `"block"` にすると、その場で拒否されます。

**推奨は、しばらく `warn` のままにすること。**
何回警告が出たかが、宣言が実態に合っているかの指標になります。
警告がほとんど出なくなってから `block` に上げてください。

---

## 毎週 — 昇格判定（5分）

```bash
cd <repos>/work-os
python3 engine/promote.py <repos>
```

「昇格できるもの」に出たものだけ、`capabilities.toml` の `status` を **1 段だけ** 上げます。
2 段飛ばしはしません。`stable` 以上にするときは `invariant` の宣言が必須です。

---

## 新しい仕事を足すとき

```bash
cp -r <repos>/work-os/templates/domain <repos>/sales-core
cd <repos>/sales-core
# work.toml の CHANGEME を置き換えて git init するだけ
```

`kernel` は空で始めます。3 リポジトリで必要になってから足す、が原則です。
最初から共通化しようとすると、必ず間違った境界を引きます。

---

## やめるとき

```bash
rm work.toml capabilities.toml .git/hooks/pre-commit
```

これで完全に元に戻ります。work-os は既存のコードに一切依存を作りません。
