# CHANGEME-core

このリポジトリは work-os の規約に従っています。

## 作業前に必ず読むもの

- `work.toml` — このリポジトリの層の宣言
- `capabilities.toml` — 何が使えるか、それぞれどの段階か
- work-os の `CONSTITUTION.md` — 5層と昇格レーンの定義

## AI エージェントへの指示

1. `kernel` / `golden_path` に宣言されたパスを直接編集しないこと。
   必要なら `extension` 層に新しいファイルを作る。
2. 新しく作った再利用可能なものは、必ず `capabilities.toml` に
   `status = "local"` で登録してから終わること。登録しないものは存在しない扱いになる。
3. 「他のリポジトリでも使えそう」と思っても、勝手に共通化しないこと。
   3リポジトリで必要になった証拠が出てから `engine/promote.py` の判定に従う。
