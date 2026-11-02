module

public meta import Lean.Elab.Tactic.BuiltinTactic
public meta import Lean.Meta.Tactic.Apply

public section

namespace NativeProof

/-- One iteration's execution and invariant obligations, joined by equality.
The actual executor, next state and invariant are supplied by the caller.
This neither defines an executor nor proves a whole-loop contract. -/
theorem invariant_of_step {State : Sort u} (invariant : State → Prop)
    {actual after : State} (execution : actual = after) (preserved : invariant after) :
    invariant actual := execution.symm ▸ preserved

public meta section

/-- Expose a real execution equation and the supplied next-state invariant.
Use the existing model's counted-loop rule for loop composition. -/
macro "native_iteration " invariant:term " => " after:term : tactic =>
  `(tactic| apply NativeProof.invariant_of_step (invariant := $invariant) (after := $after))

end
end NativeProof
