/-!
# 五つの層（CONSTITUTION.md §3）

実装は `engine/workos.py` の `layer_of`。パスに一致する宣言のうち一番長いものの層を返し、
一致する宣言が無ければ `local` を返す。

ここで固定するのは2つ。
- 宣言されていないパスは `local` である（憲法 §3「宣言されていないパスは local」）。
- `local` 以外を返したなら、その層を宣言した一致パターンが実在する
  （宣言の無いところに守りが生えない）。
-/

namespace WorkosSpec

inductive Layer where
  | kernel | goldenPath | config | extension | local
  deriving DecidableEq, Repr

/-- 宣言の一致。`rel == p` か、`p/` で始まる。`p` の末尾の `/` は呼び出し側で落としてある。 -/
def matchesB (p rel : String) : Bool :=
  rel == p || (p ++ "/").isPrefixOf rel

/-- 一番長い一致を選ぶ。同じ長さなら先に宣言されたほう（Python の `>` と同じ向き）。
    返り値は（層, 一致した長さ）。一致が無ければ（local, -1）。 -/
def pick (rel : String) : List (String × Layer) → Layer × Int
  | [] => (.local, -1)
  | (p, l) :: ds =>
    let r := pick rel ds
    if matchesB p rel && decide ((p.length : Int) ≥ r.2) then (l, p.length) else r

def layerOf (decls : List (String × Layer)) (rel : String) : Layer :=
  (pick rel decls).1

/-- 宣言されていないパスは local。 -/
theorem undeclared_is_local (decls : List (String × Layer)) (rel : String)
    (h : ∀ d ∈ decls, matchesB d.1 rel = false) : layerOf decls rel = .local := by
  induction decls with
  | nil => rfl
  | cons d ds ih =>
    obtain ⟨p, l⟩ := d
    have hd : matchesB p rel = false := h (p, l) (List.mem_cons_self ..)
    have hs : ∀ d ∈ ds, matchesB d.1 rel = false :=
      fun d hm => h d (List.mem_cons_of_mem _ hm)
    simp only [layerOf, pick, hd, Bool.false_and]
    exact ih hs

/-- local 以外を返したなら、その層を宣言した一致パターンがある。 -/
theorem nonlocal_has_witness (decls : List (String × Layer)) (rel : String) :
    (pick rel decls).1 ≠ .local →
    ∃ d ∈ decls, matchesB d.1 rel = true ∧ d.2 = (pick rel decls).1 := by
  induction decls with
  | nil => intro h; exact absurd rfl h
  | cons d ds ih =>
    obtain ⟨p, l⟩ := d
    intro h
    simp only [pick] at h ⊢
    by_cases hc : (matchesB p rel && decide ((p.length : Int) ≥ (pick rel ds).2)) = true
    · simp only [hc, ↓reduceIte]
      refine ⟨(p, l), List.mem_cons_self .., ?_, rfl⟩
      simp only [Bool.and_eq_true] at hc
      exact hc.1
    · simp only [hc, Bool.false_eq_true, ↓reduceIte] at h ⊢
      obtain ⟨w, hw, hm, hl⟩ := ih h
      exact ⟨w, List.mem_cons_of_mem _ hw, hm, hl⟩

-- 実装と同じ答えになることを、具体例で固定しておく（tests/ の例と揃えてある）。
#guard layerOf [("CONSTITUTION.md", .kernel), ("engine", .goldenPath),
                ("engine/workos.py", .kernel)] "engine/workos.py" == .kernel
#guard layerOf [("engine", .goldenPath)] "engine/release_gate.py" == .goldenPath
#guard layerOf [("engine", .goldenPath)] "engineering/notes.md" == .local
#guard layerOf [] "anything" == .local

end WorkosSpec
