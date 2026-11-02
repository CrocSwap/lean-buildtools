module

public import NativeProof.Tactics

@[expose] public section

set_option maxHeartbeats 50000
set_option maxRecDepth 512
set_option Elab.async false

namespace NativeProof.Smoke

def write (memory : Nat → Nat) (address value : Nat) : Nat → Nat :=
  fun other => if other = address then value else memory other

@[nativeproof_state] theorem write_same (memory : Nat → Nat) (address value : Nat) :
    write memory address value address = value := by simp [write]

@[nativeproof_frame] theorem write_other (memory : Nat → Nat) (address value other : Nat)
    (apart : other ≠ address) : write memory address value other = memory other := by
  simp [write, apart]

native_rule_set NativeProof.Smoke.toy_v1 := [write_same, write_other]

def advance (state : Nat) : Nat := state + 1

theorem advance_effect (state : Nat) : advance state = state + 1 := rfl

native_rule_set NativeProof.Smoke.advance_v1 := [advance_effect]

def countedInvariant (index state : Nat) : Prop := state = index

theorem iteration_preserves (index state : Nat) (initial : state = index) :
    countedInvariant (index + 1) (advance state) := by
  native_iteration countedInvariant (index + 1) => state + 1
  case execution => native_step NativeProof.Smoke.advance_v1
  case preserved => native_region [countedInvariant, initial]

-- Descriptor side conditions can reduce without an ambient global simp set.
theorem descriptor_logic (permission : Prop) :
    (False ∨ False ∨ False ∨ True) ∧ True ∧ (False ∨ True) ∧ (True ∨ permission) := by
  native_state []

def delayedNormalization (value : Nat) : Nat := value + 1000000 - 1000000

-- A supplied equation should be used before reducing a large concrete offset.
set_option maxHeartbeats 100 in
theorem normalization_before_reflexivity (value : Nat)
    (fact : delayedNormalization value = value) : delayedNormalization value = value := by
  native_state [fact]

-- Simplifying logical scaffolding must leave a real permission obligation open.
example (permission : Prop) : True := by
  fail_if_success
    have : True ∧ permission := by native_state []
  trivial

-- A next-state invariant is insufficient without the actual execution equation.
example (predicate : Nat → Prop) (actual after : Nat) (next : predicate after) : True := by
  fail_if_success
    have : predicate actual := by
      native_iteration predicate => after
      case preserved => exact next
  trivial

-- A caller cannot replace the requested invariant by a weaker predicate.
example (index state : Nat) (initial : state = index) :
    countedInvariant (index + 1) (advance state) := by
  fail_if_success native_iteration (fun (_ : Nat) => True) => state + 1
  exact iteration_preserves index state initial

example (memory : Nat → Nat) (address value : Nat) :
    write memory address value address = value := by
  native_step NativeProof.Smoke.toy_v1

example (memory : Nat → Nat) (address value other : Nat) (apart : other ≠ address) :
    write memory address value other = memory other := by
  native_step NativeProof.Smoke.toy_v1
  exact apart

example (memory : Nat → Nat) (address value : Nat) :
    write memory address value address = value := by native_state []

example (memory : Nat → Nat) (first second value other : Nat)
    (one : other ≠ first) (two : other ≠ second) :
    write (write memory first value) second value other = memory other := by
  native_frame [one, two]

example (base offset size : Nat) (within : offset + 16 ≤ size) :
    base + offset + 16 ≤ base + size := by native_region []

example (index : Nat) (within : index < 4) : index ≤ 3 := by
  native_cases index < 4 <;> omega

example (index : Fin 4) : index.val ≤ 3 := by
  native_cases index < 4 <;> decide

example : (3 : Fin 8).val = 3 := by native_state []

example : ¬((3 : Nat) < 2) := by native_state []

example : (if (3 : Nat) < 2 then 1 else 2 : Nat) = 2 := by native_state []

example : (if (3 : Fin 8).val < 2 then 1 else 2 : Nat) = 2 := by
  native_state []

-- A missing range hypothesis must never become an implicit assumption.
example (index : Nat) : True := by
  fail_if_success native_cases index < 4
  trivial

-- A missing separation premise remains an obligation.
example (memory : Nat → Nat) (address value other : Nat) : True := by
  fail_if_success
    have : write memory address value other = memory other := by native_frame []
  trivial

end NativeProof.Smoke

#print axioms NativeProof.invariant_of_step
#print axioms NativeProof.Smoke.iteration_preserves
#print axioms NativeProof.Smoke.descriptor_logic
#print axioms NativeProof.Smoke.normalization_before_reflexivity
