module

public meta import Lean.Elab.Command
public meta import Lean.Elab.Tactic.BuiltinTactic
public meta import Lean.Meta.Tactic.Apply
public meta import Lean.Util.CollectAxioms

public meta section

/-!
Named, versioned collections of ordinary proved rules. The key identifies an
adapter; correctness comes from applying the actual theorem to the actual goal.
No interpreter, resource fact or semantic certificate is created by registration.
-/

namespace NativeProof
open Lean Elab Command Tactic

structure RuleSet where
  key : Name
  rules : Array Name
  deriving Inhabited

private def addRuleSet (state : NameMap (Array Name)) (entry : RuleSet) : NameMap (Array Name) :=
  state.insert entry.key entry.rules

initialize ruleSets : SimplePersistentEnvExtension RuleSet (NameMap (Array Name)) ←
  registerSimplePersistentEnvExtension {
    addEntryFn := addRuleSet
    addImportedFn := mkStateFromImportedEntries addRuleSet {}
  }

initialize registerTraceClass `NativeProof.step

private def equalityLeft? : Expr → Option Expr
  | .app (.app (.app (.const ``Eq _) _) left) _ => some left
  | _ => none

/-- A dispatch optimization, never semantic evidence. Compare a closed leading
operand before unifying whole state expressions. Open operands retain fallback. -/
private def selectorMatches (goal ruleType : Expr) : MetaM Bool :=
  Meta.forallTelescope ruleType fun _ conclusion => do
    let some goalLeft := equalityLeft? goal | return true
    let some ruleLeft := equalityLeft? conclusion | return true
    if goalLeft.getAppFn != ruleLeft.getAppFn then return true
    let some goalOperand := goalLeft.getAppArgs[0]? | return true
    let some ruleOperand := ruleLeft.getAppArgs[0]? | return true
    if goalOperand.hasFVar || goalOperand.hasMVar || ruleOperand.hasFVar || ruleOperand.hasMVar then
      return true
    Meta.withTransparency .all <| Meta.isDefEq goalOperand ruleOperand

/-- Keys are explicit qualified names, including the adapter/model version. -/
elab "native_rule_set " key:ident " := " "[" rules:ident,* "]" : command => do
  if (ruleSets.getState (← getEnv)).contains key.getId then
    throwError "native_rule_set: duplicate adapter key {key.getId}"
  let names ← liftTermElabM do
    rules.getElems.mapM fun rule => resolveGlobalConstNoOverload rule
  for name in names do
    match ← getConstInfo name with
    | .thmInfo _ => pure ()
    | .axiomInfo info =>
      -- Module exports may erase theorem bodies and expose axiom-shaped info.
      -- Actual axioms include their own name in the recorded dependency closure.
      unless (← getEnv).getModuleIdxFor? name |>.isSome do
        throwError "native_rule_set: {name} is not a proved theorem"
      unless !info.isUnsafe && (← liftTermElabM <| Meta.isProp info.type) &&
          !(← collectAxioms name).contains name do
        throwError "native_rule_set: {name} is not a proved theorem"
    | _ => throwError "native_rule_set: {name} is not a proved theorem"
  modifyEnv (ruleSets.addEntry · { key := key.getId, rules := names })

/--
Apply the first matching rule from an explicitly selected adapter. Failed
unification attempts restore their state; real side conditions remain as goals.
-/
elab "native_step " key:ident : tactic => withMainContext do
  let some names := (ruleSets.getState (← getEnv)).find? key.getId
    | throwError "native_step: unknown adapter key {key.getId}"
  for name in names do
    unless ← selectorMatches (← getMainTarget) (← getConstInfo name).type do
      continue
    let saved ← saveState
    try
      evalTactic (← `(tactic| apply $(mkCIdent name)))
      trace[NativeProof.step] "applied {name}"
      return
    catch _ => saved.restore
  throwError "native_step: no registered rule matches; keep the original obligation explicit"

end NativeProof
