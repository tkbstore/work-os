# formal — Kernel の仕様（Lean 4）

`CONSTITUTION.md` の不変条件と、公開ゲートの判定の骨格を Lean で書いたもの。
実装は Python のまま `engine/` にあり、ここは実装ではなく**仕様**です。

```bash
cd formal && lake build      # 全定理をカーネルが検査する（Lean 4.35.0、外部依存なし）
```

| ファイル | 対応する実装 | 固定していること |
|---|---|---|
| `WorkosSpec/Layers.lean` | `engine/workos.py` `layer_of` | 宣言の無いパスは local。local 以外を返すなら、それを宣言した一致パターンがある |
| `WorkosSpec/Promotion.lean` | `engine/promotion.py` / `promote.py` | 昇格は1段ずつ。experimental → proposed は3社ルールを要する。`*` は件数に数えない |
| `WorkosSpec/Ladder.lean` | `engine/release_gate.py` の段の判定 | warn は止めない。未実行の block は「通った」にならない。落ちた段より上に届かない。公開可 ⇔ 全段に届いた |
| `WorkosSpec/Workspace.lean` | `release_gate._present_per_workspace` | 直下がワークスペースなら直下を要求する。**知らないマニフェストは見逃しうる**（反例） |

## 仕様が証明していないこと

- Python の実装がこの仕様どおりに動くこと。対応は `#guard` の具体例と `tests/` の例で
  揃えているだけで、実装から仕様への証明はない。
- 仕様が現実を覆っていること。ゲートの過去の事故（履歴の author を見ていなかった、
  列挙しなかった拡張子を素通りさせた）は、どれも仕様の外側で起きた。Lean は仕様の中の
  矛盾を見つけるが、仕様の外は見ない。
