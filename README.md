# lean-buildtools

Keep Lean builds within a memory budget, see which declarations cost the most,
and try compile-checked module splits and import reductions.

This is a small alpha project. It supports **Lean 4.35.0**, **macOS and Linux**,
and **Python 3.11+**. A C compiler is needed to build the bundled instrumentation
plugin on first use. There are no Python runtime dependencies.

## Install

With Lean 4.35.0 available through Elan and a C compiler on your path:

```sh
git clone https://github.com/CrocSwap/lean-buildtools.git
cd lean-buildtools
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
lean-buildtools --help
```

For locally built wheels and source archives, see the
[release instructions](docs/releasing.md).

For development, use `python -m pip install -e .`. Wheels and source distributions
both include the plugin's Lean/C sources. The plugin is compiled locally for your
toolchain and cached under `~/Library/Caches/lean-buildtools` on macOS or
`${XDG_CACHE_HOME:-~/.cache}/lean-buildtools` on Linux. Set
`LEAN_BUILDTOOLS_CACHE` to override that location. Installation directories are
never used as writable caches.

Try the dependency-free [hello tutorial](docs/tutorial.md) first.
For SSH builds, see [remote execution](docs/remotes.md).
For body-based transitive axiom checks and stale private-import summaries, see
[axiom audits](docs/axioms.md).
Use [selective caller refresh](docs/refresh.md) to plan dependent rebuilds and
repeat the audit afterwards.

## Commands

```sh
lean-buildtools build MyProject.Heavy --project /path/to/project
lean-buildtools profile MyProject/Heavy.lean --project /path/to/project --output /tmp/heavy-profile
lean-buildtools report /tmp/heavy-profile --project /path/to/project --sort rss_delta_bytes
lean-buildtools audit tools/roots.json --project /path/to/project --output /tmp/axiom-audit
lean-buildtools refresh-plan /tmp/axiom-audit --project /path/to/project --changed-module MyProject.Leaf --output tools/refresh-plan
lean-buildtools refresh tools/refresh-plan/plan.json --project /path/to/project --output /tmp/refreshed
lean-buildtools profile-batch tools/batch.json --project /path/to/project --output /tmp/batch
lean-buildtools build MyProject.Heavy --project /path/to/project --remote builder
lean-buildtools split MyProject/Heavy.lean --project /path/to/project --profile /tmp/heavy-profile/profile.json --output /tmp/heavy-split
lean-buildtools trim MyProject/Heavy.lean --project /path/to/project --output /tmp/heavy-trim
```

`build` runs `lake build <target>`. `profile` elaborates a single source using
already compiled imports, without replacing its cached olean. Both record command
boundaries; `profile` also writes a ranked report automatically. Source paths are
relative to `--project`, and each output directory must be new. The Python module
entry point, `python -m lean_buildtools`, accepts the same commands.

`audit` reads already compiled artifacts and compares actual transitive proof
dependencies with imported axiom summaries, including private imports.
`refresh-plan` selects affected callers within the audited roots; `refresh`
compiles that ordered plan and audits again. These commands require coherent,
matching precompiled providers; see [axioms](docs/axioms.md) and
[refresh](docs/refresh.md) for their manifests and limits.

`profile-batch` compiles an explicitly ordered JSON list of source paths, with
`--source-root` for layouts such as `sources/`. It stops at the first failure.
With `--remote builder`, builds, profiles, batches, audits and refreshes use a
[configured SSH host](docs/remotes.md); batches share one sync, job and lease.

Defaults are one compiler thread, `-M1400`, a 300 second wall limit, RSS samples
every 0.1 seconds, and an early stop at 85% of the cap. A growth trend projecting
that threshold within three seconds can also stop the process once declaration
elaboration begins, using samples collected after that point. The RSS threshold
remains active during imports and startup. Adjust these with
`--memory`, `--threads`, `--timeout`, `--interval`, `--stop-fraction`, and `--horizon`.
`--horizon 0` disables projection while retaining the RSS threshold; `--timeout 0`
disables the wall limit. Projection is conservative and can stop a brief proof-time ramp.
Use repeatable `--lean-arg=-DmaxHeartbeats=100000` arguments for explicit compiler
options; thread and memory caps still override conflicting flags.

The wrapper sets `LAKE_OVERRIDE_LEAN`, serializes compiler children within a run,
replaces conflicting thread/memory flags, and checks the actual executed child's
OS argv. `build.json` records the result. A cached build reports `cached_only`
without claiming that a compiler was observed. Limits and RSS monitoring apply
to the compiler process, not aggregate memory across native evaluation, C compilation,
linking, or separate toolkit invocations. This is a build supervisor, not an OS sandbox.

## Profiles and proposals

`profile.json` and `profile.tsv` rank declarations by wall time, raw heartbeats,
and resident-memory deltas. One `maxHeartbeats` unit is 1,000 raw heartbeats.
The JSON also contains sampled peak RSS and unfinished commands. RSS includes
mapped data; a boundary delta is retained process memory, not total allocations.
Short-lived peaks can fall between samples. Check the compile exit status in
`build.json` before treating a report as a successful compile.

The plugin makes declaration elaboration synchronous for measurement. Custom
elaborators can bypass command-boundary instrumentation; process caps still apply,
but declaration attribution may be missing. Run this on trusted Lean projects:
compilation can execute project code.

**Splitting and import trimming are experimental.** `split` balances contiguous
declaration groups by measured time, compiles the proposed parts and an original-path
re-export, and checks named declarations through a consumer. It declines private
declarations, mutual blocks, and initializers. It cannot split a single expensive proof.

`trim` tries import deletions and then up to eight narrower transitive providers
per surviving import. Use `--max-candidates 0` for deletion checks only. Its result
is greedy, not globally minimal. Changing public re-exports may affect downstream
modules even when the edited module compiles.

Both commands stage `sources/`, `proposal.json`, and `proposal.patch` without
changing source by default. Inspect them before using `--write`. Applying a proposal
checks that the original source has not changed, stages complete files, and replaces
the original file last. The operation is not a multi-file transaction across a
machine crash. Proposal trials keep their compiler logs and require existing import
artifacts. Linux uses independent copies when reflinks are unavailable; macOS copying
uses APFS clones. Large providers can require substantial scratch space.

## Optional policies

General builds allow work-in-progress proofs and normal dependency compilation.
Stricter workflows can opt in:

```sh
lean-buildtools build MyProject.Heavy --deny-sorry --no-mathlib-build --no-cache
```

- `--deny-sorry` promotes Lean's sorry warning to an error.
- `--no-mathlib-build` refuses Mathlib source compilation.
- `--no-cache` disables Lake's build-artifact downloads.

`--lean-path` prepends a colon-separated precompiled search path. Use complete,
matching module providers rather than individual olean overlays.

`snapshot SOURCE DESTINATION` creates an independent project copy using APFS
cloning or Linux reflinks where available, with a normal-copy fallback on Linux.
It excludes root build archives, `.git`, and report directories, and rejects
external symlinks/path dependencies. If a dependency lives beside your project,
copy the containing workspace instead. Custom build layouts may need a manual copy.

## native-proof-tools (experimental)

The optional [native/](native/README.md) Lean package supplies explicit theorem
rule sets, selected state/frame simplification, bounded cases and equality
transport for a supplied invariant. It builds independently of the Python
package and has no external model dependency.

It is a small helper library: instruction semantics, access permissions, ABI
contracts, loop invariants and whole-program composition remain the caller's
work. It provides no general symbolic executor, whole-loop automation or measured
proof-authoring speedup. Python wheels exclude it; the source distribution and
repository include it. See its [usage guide](native/USAGE.md).

## Development and releases

```sh
python -m pip install -e .
python -m unittest discover -s tests -v
```

CI exercises the installed wheel with Lean 4.35.0 on macOS/Python 3.14 and
Linux/Python 3.11. See [contributing](CONTRIBUTING.md),
[release instructions](docs/releasing.md), and [changelog](CHANGELOG.md).

Licensed under [Apache-2.0](LICENSE).
