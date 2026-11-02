/-!
# 累積の2段ラダー（engine/release_gate.py の LaneResult / StageResult / RepoResult）

ここで固定するのは4つ。
- warn だけのレーンは止まらない。
- block の観測を走らせていない（pending）段は「通った」にならない。
  走らせなかったことと、走らせて通ったことを同じ結論にしない。
- 届いた段は先頭から連続する。落ちた段より上には届かない。
- 公開可 ⇔ 全段に届いた。
-/

namespace WorkosSpec

inductive State where | pass | fail | skip deriving DecidableEq, Repr
inductive Severity where | block | warn deriving DecidableEq, Repr

structure Finding where
  state : State
  severity : Severity
  /-- 実行を伴う観測を、まだ走らせていない。 -/
  pending : Bool

abbrev Lane := List Finding
abbrev Stage := List Lane

def Lane.blocked (l : Lane) : Bool :=
  l.any fun f => f.state == .fail && f.severity == .block
def Lane.unproven (l : Lane) : Bool :=
  l.any fun f => f.pending && f.severity == .block
def Stage.blocked (s : Stage) : Bool := s.any Lane.blocked
def Stage.unproven (s : Stage) : Bool := s.any Lane.unproven
def Stage.settled (s : Stage) : Bool := !s.blocked && !s.unproven

/-- 先頭から続けて結論の出た段の数。 -/
def reached : List Stage → Nat
  | [] => 0
  | s :: ss => if s.settled then reached ss + 1 else 0

def publishable (ss : List Stage) : Bool := !ss.isEmpty && ss.all Stage.settled

theorem warn_never_blocks (l : Lane) (h : ∀ f ∈ l, f.severity = .warn) :
    l.blocked = false := by
  simp only [Lane.blocked, List.any_eq_false]
  intro f hf
  simp [h f hf]

theorem pending_block_unsettles (s : Stage) (l : Lane) (f : Finding)
    (hl : l ∈ s) (hf : f ∈ l) (hp : f.pending = true) (hb : f.severity = .block) :
    s.settled = false := by
  have hu : l.unproven = true := by
    simp only [Lane.unproven, List.any_eq_true]
    exact ⟨f, hf, by simp [hp, hb]⟩
  have : s.unproven = true := by
    simp only [Stage.unproven, List.any_eq_true]
    exact ⟨l, hl, hu⟩
  simp [Stage.settled, this]

theorem reached_le (ss : List Stage) : reached ss ≤ ss.length := by
  induction ss with
  | nil => simp [reached]
  | cons s ss ih =>
    simp only [reached, List.length_cons]
    split <;> omega

/-- 届いた段より下は、全部結論が出ている（累積）。 -/
theorem below_reached_settled (ss : List Stage) (i : Nat) (hi : i < reached ss) :
    ∃ h : i < ss.length, (ss[i]'h).settled = true := by
  induction ss generalizing i with
  | nil => simp [reached] at hi
  | cons s ss ih =>
    simp only [reached] at hi
    split at hi
    · rename_i hs
      cases i with
      | zero => exact ⟨by simp, by simpa using hs⟩
      | succ k =>
        obtain ⟨hk, hk'⟩ := ih k (by omega)
        exact ⟨by simp; omega, by simpa using hk'⟩
    · omega

/-- 落ちた段より上には届かない。 -/
theorem blocked_caps_reached (ss : List Stage) (k : Nat) (hk : k < ss.length)
    (hb : (ss[k]'hk).blocked = true) : reached ss ≤ k := by
  apply Nat.le_of_not_lt
  intro hlt
  obtain ⟨_, hs⟩ := below_reached_settled ss k hlt
  simp [Stage.settled, hb] at hs

theorem publishable_iff (ss : List Stage) :
    publishable ss = true ↔ ss ≠ [] ∧ reached ss = ss.length := by
  induction ss with
  | nil => simp [publishable, reached]
  | cons s ss ih =>
    simp only [publishable, List.isEmpty_cons, Bool.not_false, Bool.true_and,
      List.all_cons, Bool.and_eq_true, reached, List.length_cons, ne_eq,
      reduceCtorEq, not_false_eq_true, true_and]
    constructor
    · rintro ⟨hs, ha⟩
      simp only [hs, ↓reduceIte]
      cases ss with
      | nil => simp [reached]
      | cons t ts =>
        have := (ih.mp (by simpa [publishable] using ha)).2
        omega
    · intro h
      split at h
      · rename_i hs
        refine ⟨hs, ?_⟩
        cases ss with
        | nil => simp
        | cons t ts =>
          have := (ih.mpr ⟨by simp, by omega⟩)
          simpa [publishable] using this
      · omega

-- 第1段が warn だけ、第2段に未実行の block がある → 第1段まで。公開可ではない。
#guard reached [[[⟨.fail, .warn, false⟩]], [[⟨.skip, .block, true⟩]]] == 1
#guard !publishable [[[⟨.fail, .warn, false⟩]], [[⟨.skip, .block, true⟩]]]

end WorkosSpec
