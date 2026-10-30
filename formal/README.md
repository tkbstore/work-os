# formal — Kernel の仕様（Lean 4）

`CONSTITUTION.md` の不変条件と、公開ゲートの判定の骨格を Lean で書いたもの。
実装は Python のまま `engine/` にあり、ここは実装ではなく**仕様**です。

```bash
cd formal && lake build      # 全定理をカーネルが検査する（Lean 4.35.0、外部依存なし）
python3 tests/test_formal_differential.py   # 仕様と実装に同じ入力を流して突き合わせる（リポ直下で）
```

| ファイル | 対応する実装 | 固定していること |
|---|---|---|
| `WorkosSpec/Layers.lean` | `engine/workos.py` `layer_of` | 宣言の無いパスは local。local 以外を返すなら、それを宣言した一致パターンがある。一致のうち最長が選ばれ、同じ長さなら先に宣言したほうが勝つ |
| `WorkosSpec/Promotion.lean` | `engine/promotion.py` / `promote.py` | 昇格は1段ずつ。experimental → proposed は3社ルールを要する。`*` は件数に数えない |
| `WorkosSpec/Ladder.lean` | `engine/release_gate.py` の段の判定 | warn は止めない。未実行の block は「通った」にならない。落ちた段より上に届かない。公開可 ⇔ 全段に届いた |
| `WorkosSpec/Workspace.lean` | `release_gate._present_per_workspace` | 直下がワークスペースなら直下を要求する。**知らないマニフェストは見逃しうる**（反例） |

## 仕様が証明していないこと

- Python の実装がこの仕様どおりに動くこと。実装から仕様への証明はない。代わりに
  `Differential.lean`（`lake exe differential`）が仕様の判定関数を実行体にし、
  `tests/test_formal_differential.py` が乱数で作った入力（各 400 件）を仕様と実装の両方に
  流して答えを比べる。比べているのは `layerOf` / `reached`・`publishable` /
  `collect`・`satisfiesRuleOfThree` / `gitignorePass` の4組で、言えるのは「生成した入力の
  範囲で食い違わなかった」ことまで。
- パスの正規化。仕様の `layerOf` は正規化済みのパスを受け取る前提で、実装の
  `rel_path.lstrip("./")` は差分テストの入力から外している。`lstrip` は接頭辞ではなく
  文字の集合を落とすので、`.github/x` は `github/x` になり、`.` で始まる宣言には
  一致しない（2026-10-30 に確認。`engine/workos.py` は kernel のため別に直す）。
- 仕様が現実を覆っていること。ゲートの過去の事故（履歴の author を見ていなかった、
  列挙しなかった拡張子を素通りさせた）は、どれも仕様の外側で起きた。Lean は仕様の中の
  矛盾を見つけるが、仕様の外は見ない。
