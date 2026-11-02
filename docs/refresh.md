# Selective caller refresh

Start from an [audit](axioms.md) made after updating a leaf. The planner follows
actual declaration dependencies, including private imported cells, within the
selected headline roots. It then orders affected modules using the complete
import graph. Unused imports do not enlarge this body-dependent caller set.

```sh
lean-buildtools refresh-plan tools/before-audit --project /path/to/project \
  --changed-module MyProject.Leaf --source-root sources \
  --output tools/refresh-plan
```

`plan.json` lists the ordered modules, dependency paths, source hashes and any
missing sources. A blocked plan cannot run. Sources default to
`sources/MyProject/Leaf.lean`; `--source-map FILE` can supply a JSON map from
module names to project-relative source paths. Paths must retain the module's
namespace directories so the compiler can use its original module name.

The changed leaf is excluded by default because it was already updated. Use
`--include-changed` to compile it too. `--changed-declaration NAME` narrows a
proof-only change to named declarations in the changed modules. By default all
observed declarations in each changed module seed the dependency walk.

After a proof-only update with unchanged statements, `--stale-only` selects only
affected modules still carrying unexpected cached summaries. It requires clean
actual axiom closures with no unresolved dependencies. All callers remain the
default for changes to APIs or definitions; the stale-only mode addresses axiom
metadata cleanup rather than general source freshness.

Seed the output directory with the matching complete compiled prefix, then run:

```sh
lean-buildtools refresh tools/refresh-plan/plan.json --project /path/to/project \
  --remote builder --remote-config /path/to/private-remotes.toml \
  --memory 1400 --threads 1 --output /tmp/refreshed
```

Keep the plan and sources inside the project, outside excluded report folders,
for remote execution. Use the same coherent providers and search order as the
input audit. Sources changed after planning require a new plan. The command
compiles the ordered members in one serial lane, stops at the first failure,
checks source stability, then repeats the full private axiom audit. One remote
job holds one lease across compilation and the final audit.

`compile/` contains per-member profiles and compiler audits. `audit/` contains
the final axiom and fingerprint reports. The top-level `build.json` records the
plan digest, selected modules, aggregate compiler audits and final status.
Use explicit `--fetch` globs to retrieve the desired compiled products; the tool
does not install them over another working tree or an external provider.

For several mutable prefixes, `--output-map FILE` routes module prefixes to
project-relative complete output directories. For example:

```json
{"MyProject.App": "compiled/app", "*": "compiled/core"}
```

The longest matching prefix wins. Supply the intended search order explicitly:
`--output-root compiled/app --output-root compiled/core`. The same directories
must be seeded before execution. External immutable provider paths remain
inputs. A failed partial prefix is unsuitable as a replacement complete provider;
retain the prior coherent seed until the full compile and audit succeed.
