module

public import Init.Data.Fin.Lemmas
public import NativeProof.Iteration
public meta import NativeProof.Rules
public meta import NativeProof.SimpSets
public meta import Lean.Elab.Tactic.Simp
public meta import Lean.Elab.Tactic.Omega
public meta import Lean.Elab.Tactic.Rfl
public meta import Lean.Elab.Tactic.Classical
public meta import Lean.Meta.Tactic.Simp.BuiltinSimprocs.Nat
public meta import Lean.Meta.Tactic.Simp.BuiltinSimprocs.Core

public section

/-!
Small, proof-producing building blocks for native-code proof adapters.
No ISA or resource contract is supplied by this module. Adapters register
ordinary proved lemmas in the separate rule sets below.
-/

attribute [nativeproof_word] Fin.val_ofNat Fin.val_zero Fin.val_one ite_true ite_false
attribute [nativeproof_logic] or_true true_or false_or or_false
  true_and and_true false_and and_false not_false_eq_true not_true_eq_false

namespace NativeProof
open Lean Elab Tactic

@[nativeproof_word] theorem finLiteralVal (n k : Nat) [NeZero n] :
    (OfNat.ofNat k : Fin n).val = k % n := rfl

public meta section

register_option maxCases : Nat := {
  defValue := 32
  descr := "maximum alternatives for a bounded native proof case split"
}

/-- Simplify only the adapter's state and word rules and explicitly supplied facts. -/
macro "native_state" "[" facts:(Lean.Parser.Tactic.simpStar <|> Lean.Parser.Tactic.simpErase <|> Lean.Parser.Tactic.simpLemma),* "]" : tactic => do
  let facts : Syntax.TSepArray [`Lean.Parser.Tactic.simpStar, `Lean.Parser.Tactic.simpErase, `Lean.Parser.Tactic.simpLemma] "," := ⟨facts.elemsAndSeps⟩
  `(tactic| first
    | simp (config := { maxSteps := 2000 }) only
      [nativeproof_state, nativeproof_word, nativeproof_logic, Nat.reduceAdd, Nat.reduceMul,
       Nat.reduceSub, Nat.reduceDiv, Nat.reduceMod, Nat.reduceLT, reduceIte, reduceDIte, $facts,*]
    | rfl)

/-- Normalize region facts without importing the ambient global simp set. -/
macro "native_region" "[" facts:(Lean.Parser.Tactic.simpStar <|> Lean.Parser.Tactic.simpErase <|> Lean.Parser.Tactic.simpLemma),* "]" : tactic => do
  let facts : Syntax.TSepArray [`Lean.Parser.Tactic.simpStar, `Lean.Parser.Tactic.simpErase, `Lean.Parser.Tactic.simpLemma] "," := ⟨facts.elemsAndSeps⟩
  `(tactic| ((try simp (config := { maxSteps := 2000 }) only
      [nativeproof_region, nativeproof_word, nativeproof_logic, Nat.reduceAdd, Nat.reduceMul,
       Nat.reduceSub, Nat.reduceDiv, Nat.reduceMod, Nat.reduceLT, reduceIte, reduceDIte, $facts,*]) <;> try omega))

/-- Apply registered frame/read-over-write lemmas, leaving real resource goals open. -/
macro "native_frame" "[" facts:(Lean.Parser.Tactic.simpStar <|> Lean.Parser.Tactic.simpErase <|> Lean.Parser.Tactic.simpLemma),* "]" : tactic => do
  let facts : Syntax.TSepArray [`Lean.Parser.Tactic.simpStar, `Lean.Parser.Tactic.simpErase, `Lean.Parser.Tactic.simpLemma] "," := ⟨facts.elemsAndSeps⟩
  `(tactic| ((try simp (config := { maxSteps := 4000 }) (disch := omega) only
      [nativeproof_frame, nativeproof_state, nativeproof_word, nativeproof_region,
       nativeproof_logic, Nat.reduceAdd, Nat.reduceMul, Nat.reduceSub, Nat.reduceDiv, Nat.reduceMod,
       Nat.reduceLT, reduceIte, reduceDIte, $facts,*])
      <;> try omega))

/--
Split a small Nat/Fin index by proved equalities. The final impossible branch
must be proved from the original context; no range assumption is introduced.
-/
elab "native_cases " index:ident " < " bound:num : tactic => do
  let count := bound.getNat
  let maximum := maxCases.get (← getOptions)
  if count > maximum then
    throwError "native_cases: {count} alternatives exceed NativeProof.maxCases={maximum}"
  let mut remaining ← getMainGoal
  let mut alternatives : List MVarId := []
  for value in [:count] do
    setGoals [remaining]
    let hypothesis := mkIdent (← mkFreshUserName `nativeIndex)
    evalTactic (← `(tactic| by_cases $hypothesis : $index = $(quote value)))
    let [positive, negative] ← getGoals
      | throwError "native_cases: expected equality and disequality goals"
    setGoals [positive]
    evalTactic (← `(tactic| subst $index))
    alternatives := alternatives ++ (← getGoals)
    remaining := negative
  setGoals [remaining]
  try
    evalTactic (← `(tactic| omega))
  catch _ =>
    throwError "native_cases: the context does not establish the requested finite range"
  alternatives := alternatives ++ (← getGoals)
  setGoals alternatives

end
end NativeProof
