# Experimental status

This optional Lean 4.35.0 helper package supports explicitly registered theorem
application, selected simplification, bounded cases and equality transport for a
supplied invariant. The standalone smoke and imported-rule examples exercise
these operations without an external machine model.

It supplies no instruction semantics, executor, permissions, ABI contract,
resource inference or invariant discovery. The iteration helper requires the
actual execution equation and a proof of the next-state invariant; it does not
prove a whole loop. Broad instruction coverage and loop automation are outside
the current scope.

A shorter tactic script does not establish lower proof-authoring effort. There
is no measured productivity percentage or compiler-speedup claim. Imported
model rules can carry inherited axioms; applying a tactic cannot remove them.
Keep the model's original assumptions and audit its proof closure separately.
