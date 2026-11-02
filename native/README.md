# Native proof tools (experimental)

This optional Lean 4.35.0 package contains the first proof-producing building
blocks for native-code proof adapters. It is separate from the Python build
supervisor and its instrumentation plugin.

`NativeProof.Tactics` provides:

- `native_rule_set Adapter.version := [rule1, rule2]`: an explicitly named
  collection of proved theorems for an adapter.
- `native_step Adapter.version`: apply the first matching registered rule and
  expose its preconditions as goals. Failed attempts restore the proof state.
  Closed leading instruction operands are compared before unifying whole states.
- `native_state [...]`: explicitly registered state/word simplifications.
- `native_region [...]`: region/word normalization followed by linear arithmetic.
- `native_frame [...]`: registered frame/read-over-write lemmas and arithmetic.
- `native_cases index < n`: bounded Nat/Fin case splitting, with exhaustiveness
  proved from the existing context. `NativeProof.maxCases` defaults to 32.
- `native_iteration predicate => nextState`: expose an execution equality and
  the supplied next-state invariant as separate goals for one loop iteration.

Adapters supply ordinary proved lemmas through the separate
`nativeproof_state`, `nativeproof_word`, `nativeproof_region` and
`nativeproof_frame` attributes. The tactics use those sets and explicit facts
rather than the ambient global simp set. Missing permissions, separation or
bounds remain proof obligations.
The word set includes closed natural-number reduction and finite-index literal
rules. `native_state` applies selected simplifications before falling back to
reflexivity, avoiding premature unfolding of large concrete offsets.
The shared `nativeproof_logic` set reduces basic `True`/`False` conjunctions,
disjunctions and negations. Atomic permissions remain proof obligations.
An adapter key labels a rule collection; the actual imported theorem types
determine applicability. The key itself supplies no model or artifact certificate.
The tactics introduce no native-evaluation axioms. Imported rule dependencies
must also be audited when a release requires a kernel-checked closure.

The smoke examples use a small functional memory model and check bounded case
splitting. Measured proof-authoring savings remain unproven. The thin layer stays
optional; loop and broad rule-coverage expansion are outside the current scope.
See the [status and limits](STATUS.md). These primitives alone supply no ISA,
access-safety or ABI contract.

From the repository root, with Lean 4.35 available:

```sh
lean-buildtools build NativeProof.Smoke --project native --memory 4096
```

The selected compiler budget must fit the machine where the command runs.

See [USAGE.md](USAGE.md) for a short adapter and iteration guide.
