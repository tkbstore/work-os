import Lean.Data.Json
import WorkosSpec

/-!
# 差分テストの実行体

仕様の判定関数に JSON の入力を与え、答えを JSON で返す。同じ入力を Python の実装に
通して突き合わせるのは `tests/test_formal_differential.py`。

定理が言えるのはこのモデルについてだけで、実装がモデルどおりに動くことは言えない。
`#guard` の具体例は数件しかないので、ここで入力を大量に流して対応を確かめる。

  lake exe differential <input.json>
-/

open Lean WorkosSpec

def parseLayer : String → Except String Layer
  | "kernel" => .ok .kernel
  | "golden_path" => .ok .goldenPath
  | "config" => .ok .config
  | "extension" => .ok .extension
  | "local" => .ok .local
  | s => .error s!"知らない層: {s}"

def layerName : Layer → String
  | .kernel => "kernel" | .goldenPath => "golden_path" | .config => "config"
  | .extension => "extension" | .local => "local"

def parseState : String → Except String State
  | "pass" => .ok .pass | "fail" => .ok .fail | "skip" => .ok .skip
  | s => .error s!"知らない state: {s}"

def parseSeverity : String → Except String Severity
  | "block" => .ok .block | "warn" => .ok .warn
  | s => .error s!"知らない severity: {s}"

/-- `{"decls": [[pattern, layer], ...], "rel": path}` → 層の名前。 -/
def runLayer (j : Json) : Except String Json := do
  let raw ← j.getObjValAs? (List (List String)) "decls"
  let decls ← raw.mapM fun
    | [p, l] => do return (p, ← parseLayer l)
    | d => throw s!"宣言の形が違う: {d}"
  let rel ← j.getObjValAs? String "rel"
  return toJson (layerName (layerOf decls rel))

def parseFinding (j : Json) : Except String Finding := do
  return { state := ← parseState (← j.getObjValAs? String "state")
           severity := ← parseSeverity (← j.getObjValAs? String "severity")
           pending := ← j.getObjValAs? Bool "pending" }

/-- 段 → レーン → 観測の入れ子の配列 → 届いた段の数と公開可か。 -/
def runLadder (j : Json) : Except String Json := do
  let raw ← fromJson? (α := List (List (List Json))) j
  let ss : List Stage ← raw.mapM (·.mapM (·.mapM parseFinding))
  return Json.mkObj [("reached", toJson (reached ss)), ("publishable", toJson (publishable ss))]

def runRule (j : Json) : Except String Json := do
  let ev := collect (← j.getObjValAs? (List String) "used_by")
    (← j.getObjValAs? (List String) "declaring") (← j.getObjValAs? String "note")
  return Json.mkObj [("universal", toJson ev.universal), ("count", toJson ev.count),
    ("satisfies", toJson (satisfiesRuleOfThree ev))]

/-- `has` は .gitignore を持つディレクトリ（直下を除く）の一覧、`wss` はワークスペース。 -/
def runGitignore (j : Json) : Except String Json := do
  let hasList ← j.getObjValAs? (List Dir) "has"
  let wss ← j.getObjValAs? (List Dir) "wss"
  let rootHas ← j.getObjValAs? Bool "root_has"
  return toJson (gitignorePass rootHas (fun d => hasList.contains d) wss)

def runAll (j : Json) : Except String Json := do
  let each (key : String) (f : Json → Except String Json) : Except String (String × Json) := do
    let cases ← j.getObjValAs? (List Json) key
    return (key, toJson (← cases.mapM f))
  return Json.mkObj [← each "layer" runLayer, ← each "ladder" runLadder,
    ← each "rule" runRule, ← each "gitignore" runGitignore]

def main (args : List String) : IO UInt32 := do
  let [path] := args | IO.eprintln "usage: differential <input.json>"; return 2
  let text ← IO.FS.readFile path
  match Json.parse text >>= runAll with
  | .ok j => IO.println j.compress; return 0
  | .error e => IO.eprintln e; return 1
