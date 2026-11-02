/-!
# 昇格レーン（CONSTITUTION.md §4）

実装は `engine/promotion.py`（数え方）と `engine/promote.py`（段の移動）。

ここで固定するのは3つ。
- 昇格は必ず1段ずつ。段を飛ばさない。
- experimental → proposed は3社ルール（全称 `*`・3件以上・例外の宣言のいずれか）を要する。
- `*` は名前ではなく全称の印なので、件数に数えない（実装が水増ししていた過去の誤り）。
-/

namespace WorkosSpec

inductive Status where
  | local | experimental | proposed | stable | kernel
  deriving DecidableEq, Repr

def Status.rank : Status → Nat
  | .local => 0 | .experimental => 1 | .proposed => 2 | .stable => 3 | .kernel => 4

def Status.next : Status → Option Status
  | .local => some .experimental
  | .experimental => some .proposed
  | .proposed => some .stable
  | .stable => some .kernel
  | .kernel => none

def ruleOfThree : Nat := 3
def universal : String := "*"

structure Evidence where
  universal : Bool
  names : List String
  note : String

def Evidence.count (ev : Evidence) : Nat := ev.names.length

/-- `promotion.collect` に対応する。`*` は全称の印として抜き出し、名前から外す。 -/
def collect (usedBy declaring : List String) (note : String) : Evidence :=
  { universal := usedBy.contains universal
    names := (usedBy.filter (· != universal) ++ declaring).eraseDups
    note := note }

def satisfiesRuleOfThree (ev : Evidence) : Bool :=
  ev.universal || decide (ev.count ≥ ruleOfThree) || ev.note != ""

/-- 段ごとの人の判断（2週間変わっていない、IP が自社にある、など）は機械では決まらないので
    述語として受け取る。機械が決めるのは3社ルールと「1段ずつ」だけ。 -/
def promote (s : Status) (ev : Evidence) (judged : Status → Bool) : Option Status :=
  match s.next with
  | none => none
  | some t =>
    if s = .experimental && !satisfiesRuleOfThree ev then none
    else if judged t then some t else none

/-- 昇格は必ず次の段。飛ばさない。 -/
theorem promote_is_next (s t : Status) (ev : Evidence) (j : Status → Bool) :
    promote s ev j = some t → s.next = some t := by
  unfold promote
  cases hn : s.next with
  | none => simp
  | some u =>
    simp only
    split
    · simp
    · split <;> simp_all

theorem promote_rank (s t : Status) (ev : Evidence) (j : Status → Bool) :
    promote s ev j = some t → t.rank = s.rank + 1 := by
  intro h
  have := promote_is_next s t ev j h
  cases s <;> simp [Status.next] at this <;> subst this <;> rfl

theorem kernel_is_top (ev : Evidence) (j : Status → Bool) : promote .kernel ev j = none := rfl

/-- experimental から上がったなら、3社ルールを満たしている。 -/
theorem proposed_needs_rule_of_three (ev : Evidence) (j : Status → Bool) (t : Status) :
    promote .experimental ev j = some t → satisfiesRuleOfThree ev = true := by
  unfold promote
  simp only [Status.next]
  cases h : satisfiesRuleOfThree ev <;> simp

/-- `*` を足しても名前の件数は増えない（全称は件数では測らない）。 -/
theorem star_not_counted (u d : List String) (n : String) :
    (collect (universal :: u) d n).names = (collect u d n).names := by
  simp [collect]

-- 2件 + `*` は件数では 2 のまま。全称として通る。
#guard (collect ["a", "b", "*"] [] "").count == 2
#guard satisfiesRuleOfThree (collect ["a", "b", "*"] [] "")
#guard !satisfiesRuleOfThree (collect ["a", "b"] [] "")
-- 同じ名前を何度書いても1件。
#guard (collect ["a", "a", "b"] ["b"] "").count == 2

end WorkosSpec
