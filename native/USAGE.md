# Small native proof adapters

Use Lean 4.35.0 and a complete compiled artifact prefix for this package.
Prepend its **parent of `NativeProof/`** to your existing search path, then retain
every existing model/provider root in its original order. The package supplies
no instruction-set model or machine-state definitions.

```lean
module
public import NativeProof.Tactics
-- Import your unchanged actual model and component contracts here.
```

The portable source is this `native/` Lake package. Prebuilt artifacts must match
the toolchain and include all emitted module companions. Start with an import
check under your existing memory cap. Keep provider changes separate from proof
changes so compilation failures remain easy to diagnose.

## One instruction or primitive block

Register existing proved equations under an explicit model/version key:

```lean
native_rule_set MyAdapter.modelVersion := [effectA, effectB]

-- Goal: the actual instruction/block execution equals its stated effect.
native_step MyAdapter.modelVersion
-- Prove every remaining precondition from the original context.
```

`native_step` applies a theorem to an equality goal. It does not fetch an
instruction, combine a caller/callee admission, or construct an access certificate.
It compares closed leading instruction operands before whole-state unification.
Registration accepts local and imported proved propositions. Lean 4.35 may expose
an imported theorem as an axiom-shaped declaration; its recorded dependency
closure distinguishes it from an actual axiom, which is rejected. This is a
root-declaration check, not an audit of the rule's inherited axioms.
For larger proofs, instantiate the instruction position explicitly; avoid making
unification unfold an entire execution merely to normalize an array index.

Register small, proved simplification rules with `nativeproof_state`,
`nativeproof_word`, `nativeproof_region` or `nativeproof_frame`. Then use:

```lean
native_state [specificStateFacts]
native_frame [specificFrameFacts, originalSeparationAndBounds]
native_region [specificRegionFacts]
native_cases index < 4
```

These use the named sets and your explicit facts. The shared `nativeproof_logic`
set handles basic And/Or/Not `True`/`False`
identities. A real permission proposition still needs its original proof.
Start with a few rules: unfolding state constructors alongside field-frame lemmas can destroy the useful
shape of the goal. Missing permissions, alias conditions and byte bounds remain
obligations. Readback needs a matching proved read-over-write rule and its real
overlap/separation premises. No ABI, wipe, resource or CT grade is inferred.
`native_state` tries the supplied simplification facts before reflexivity, so a
named PC/offset fact can be used without first unfolding concrete call arithmetic.

For a large execution expression, derive an equality to a small named state
before simplifying its projections. If `execution : actualRun = namedState`,
transport the observation through that equality explicitly:

```lean
have observed := congrArg (fun state => read_mem address state) execution
-- Use observed to rewrite the memory goal before unfolding actualRun.
```

The same pattern works for register or frame observations. It avoids asking
unification to normalize a whole caller execution just to compare one field;
the actual execution equality still needs its original proof.

A generic frame lemma may need a named equality in the normalized goal's shape.
For example, with your model's proved same-value store rule:

```lean
have stored : write registers index (registers index) = registers := by
  exact write_same registers index
native_frame [stored]
```

This reuses the proved same-value store-write fact. It does not establish a
permission, frame bound, or alias condition. Explicit record congruence and
function extensionality remain useful small closures when a selected rewrite
does not match; account for adaptation time instead of forcing every step
through a wrapper.

## One loop iteration with a supplied invariant

`native_iteration predicate => nextState` exposes two named goals:

1. `execution`: the actual state expression in the goal equals `nextState`.
2. `preserved`: the supplied predicate holds at `nextState`.

The goal's predicate must match the supplied one. This is equality transport;
it defines no executor and performs no loop unrolling or invariant discovery.
`NativeProof.Smoke.iteration_preserves` is the small compiled functional example.

For an iteration obligation supplied by your model, the pattern is:

```lean
-- Schematic: LoopInv, nextState and run_body come from your actual component.
by
  intro index within state initial
  native_iteration LoopInv (index + 1) => nextState index state
  case execution =>
    exact run_body index within state initial
  case preserved =>
    native_frame [LoopInv, initial, specificIterationFacts]
    -- Discharge any remaining invariant fields explicitly.
```

Build the other rule fields from their actual proofs: positive body length,
actual-state ranking, accesses, every-prefix errors/frames, and frame composition.
Use your model's existing loop composition theorem for the whole loop. Keep
terminal branch/exit and paired trace obligations with their existing contracts.
The iteration tactic supplies none of those fields. The self-contained
[supplied-iteration example](examples/CountedLoop.lean) abstracts over the state
and executor; it has no external model dependency.

## Trust and measurement

Use ordinary theorem application, rewriting, `omega`, and checked reductions.
Do not use native evaluation for new proof steps. The helper's own axiom closure
is printed in the smoke test; imported model-rule closures still need the owner's
audit. A tactic application cannot remove an inherited native-evaluation axiom.

The separate imported-rule regression can be run after building this package:

```sh
lean-buildtools profile-batch Tests/rules.json --project native \
  --emit-root .lake/build/lib/lean --memory 1400 --deny-sorry
```

It checks imported theorem registration and rejection of a deliberately declared
test axiom. The successful test theorem prints an empty axiom list. `Tests/` is
outside the normal `NativeProof` library target.

For a fresh component, freeze the intended statement first. Record active work
start/pause slices, attempts and failed compiler runs, adapter/setup work, final
proof size including helpers, compiler time/RSS, and semantic coverage gaps.
Label historical elapsed baselines honestly when active time was not measured.
The trial asks whether a new proof takes less agent effort; smaller scripts alone
do not establish that result.
