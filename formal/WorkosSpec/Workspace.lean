/-!
# ワークスペース単位の .gitignore（release_gate.py の `_present_per_workspace`）

ディレクトリはパスの区切りの列で表す。直下は `[]`。

成り立つこと:
- 直下がワークスペースなら、直下に無いことはそのまま欠陥。
- 既知のワークスペースが1つも見つからなければ、直下を要求する。

成り立たないこと（実装のコメントが一度そう主張していた）:
- 「知らないマニフェストは常に厳しい側に倒れる」。既知のワークスペースが覆われていれば、
  知らないマニフェストを持つ覆われていないディレクトリがあっても通る。下の反例を参照。
-/

namespace WorkosSpec

abbrev Dir := List String

/-- `d` かその祖先（直下は除く）に候補が在るか。`has` は「そのディレクトリに在る」。 -/
def covered (has : Dir → Bool) (d : Dir) : Bool :=
  (List.range d.length).any fun i => has (d.take (i + 1))

def gitignorePass (rootHas : Bool) (has : Dir → Bool) (wss : List Dir) : Bool :=
  rootHas || (!wss.isEmpty && !wss.contains [] && wss.all (covered has))

theorem root_workspace_requires_root (has : Dir → Bool) (wss : List Dir)
    (h : [] ∈ wss) (hp : gitignorePass false has wss = true) : False := by
  simp [gitignorePass] at hp
  exact hp.1.2 h

theorem no_workspace_requires_root (has : Dir → Bool) :
    gitignorePass false has [] = false := rfl

/-- 反例。`web/` が知らないマニフェストのワークスペースで .gitignore を持たなくても、
    既知の `lean/` が覆われていれば通る。見つけられないワークスペースは、覆われていなくても
    判定に現れない。 -/
example :
    let has : Dir → Bool := fun d => d == ["lean"]
    -- 既知のワークスペースは lean/ だけ。web/ は見つかっていない。
    gitignorePass false has [["lean"]] = true ∧ covered has ["web"] = false := by
  decide

#guard covered (fun d => d == ["lean"]) ["lean", "OAI"]
#guard !covered (fun d => d == ["lean"]) ["web"]

end WorkosSpec
