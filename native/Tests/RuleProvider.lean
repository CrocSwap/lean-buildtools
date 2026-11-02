module

@[expose] public section
namespace NativeProofTests

theorem provedRule (state : Nat) : state = state := rfl

-- Deliberate negative fixture: registration must reject an actual imported axiom.
axiom assumedRule (state : Nat) : state = state

end NativeProofTests
