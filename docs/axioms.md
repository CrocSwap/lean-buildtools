# Axiom audits

`audit` checks headline roots against the default allowlist `propext`,
`Classical.choice`, and `Quot.sound`. Put roots in a project-relative JSON file:

```json
{
  "roots": [
    {"module": "MyProject.Certificate", "name": "MyProject.certificate"}
  ]
}
```

```sh
lean-buildtools audit tools/roots.json --project /path/to/project \
  --output /tmp/certificate-audit --memory 1400 --threads 1
```

The same command accepts the existing remote options, including `--remote`,
`--remote-config`, precompiled inputs and ordered search paths. Dependencies
must already be compiled. It does not build project or Mathlib sources.

A root may additionally specify `"artifact": "build/products/MyProject/Certificate.olean"`
to audit an uninstalled output by its exact file. Module-form companions must
exist alongside it. Its imports still resolve through the configured coherent
search paths; this does not add a sparse directory to that search path. Relative
artifact paths are relative to the project. In remote runs, absolute paths refer
to the remote host.

The audit reads the complete declaration data, including `.olean.server` and
`.olean.private` for module-form inputs. It follows every ordinary import and
indexes private declarations, rather than only names visible from the headline
module. It traverses actual types and theorem/opaque bodies, then propagates
axiom dependencies through the graph, including cycles. Imported cached axiom
lists are comparison data; they cannot prune this traversal.

`audit.json` reports each root's actual axiom set, paths to unexpected axioms,
and cached-summary mismatches with paths to the declarations carrying them.
A repaired leaf with stale unexpected axioms in private callers therefore fails
even if the leaf's own axiom list is clean. All cache differences remain visible;
differences involving only allowed axioms are informational. Missing declarations, ambiguous conflicting bodies,
unsafe/partial dependencies and changing artifacts also fail. The exit status
is zero only when the entire audit passes.

`artifacts.json` fingerprints every resolved logical part and available
`.ilean`, `.ir` and `.ir.sig` companion before and after extraction.
`modules.json` records the actual import graph. `callers.json` records the
reachable declaration graph, including private declarations, for selective
refresh planning; its SHA256 is bound into `audit.json`. The aggregate `build.json` and
the two extractor compiler reports retain argv, cap and RSS evidence. Use
`--keep-graph` to retain the full raw declaration stream and SQLite index;
otherwise these temporary files are removed before reports are returned.

This checks the resolved **compiled artifacts** in a fresh process. It does
not establish that edited source files were compiled, replace independent
kernel replay, or change the meaning of an inherited axiom. Refresh affected
callers when summaries disagree, then repeat the audit. Keep the same coherent
provider roots and search order when comparing runs.

The extractor targets Lean 4.35.0's serialized module and axiom-summary schemas.
Reading these trusted compiler artifacts involves Lean's unsafe metadata APIs;
the tool introduces no logical axiom or native-evaluation proof primitive.
