module

public import NativeProof.Tactics

@[expose] public section
namespace NativeProof.Examples

/-- One supplied iteration, abstract over the actual state and executor.
Access, frame, ranking and terminal-branch proofs remain with the caller. -/
theorem supplied_iteration {State : Type u} (bound bodySteps : Nat)
    (run : Nat → State → State)
    (invariant : Nat → State → Prop) (next : Nat → State → State)
    (body : ∀ index, index < bound → ∀ state, invariant index state →
      run bodySteps state = next index state)
    (preserves : ∀ index, index < bound → ∀ state, invariant index state →
      invariant (index + 1) (next index state))
    (index : Nat) (within : index < bound) (state : State)
    (initial : invariant index state) : invariant (index + 1) (run bodySteps state) := by
  native_iteration invariant (index + 1) => next index state
  case execution => exact body index within state initial
  case preserved => exact preserves index within state initial

end NativeProof.Examples

#print axioms NativeProof.Examples.supplied_iteration
