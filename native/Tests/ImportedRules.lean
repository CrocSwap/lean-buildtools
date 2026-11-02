module

public import NativeProof.Tactics
public import Tests.RuleProvider

@[expose] public section

native_rule_set NativeProofTests.imported_v1 := [NativeProofTests.provedRule]

/-- error: native_rule_set: NativeProofTests.assumedRule is not a proved theorem -/
#guard_msgs in
native_rule_set NativeProofTests.rejected_v1 := [NativeProofTests.assumedRule]

namespace NativeProofTests

theorem imported_step (state : Nat) : state = state := by
  native_step NativeProofTests.imported_v1

end NativeProofTests

#print axioms NativeProofTests.imported_step
