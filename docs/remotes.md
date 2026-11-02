# Remote builds

`build`, `profile`, `profile-batch`, `audit` and `refresh` can run on a named SSH remote. Both hosts need Python 3.11+
and rsync. The remote also needs a C compiler and the official Lean 4.35.0 toolchain
on its noninteractive SSH PATH. macOS and Linux are supported. Test the connection
with `ssh user@example.com 'python3 --version; lean --version; lake --version'`.
SSH startup files must not print banners to stdout. Configure authentication in
your normal SSH configuration; the toolkit uses batch mode and never copies keys.

Create `~/.config/lean-buildtools/remotes.toml`:

```toml
[remotes.builder]
ssh = "user@example.com"
root = "~/lean-jobs"
namespace = "my-work"
path = ["~/.elan/bin"]
memory = 1400
max_memory = 4096
threads = 1
max_jobs = 2
```

Then run the hello project from the [tutorial](tutorial.md):

```sh
lean-buildtools build Hello --project /path/to/hello --remote builder \
  --output /tmp/hello-remote --fetch '.lake/build/lib/lean/Hello.olean*'
lean-buildtools profile Hello.lean --project /path/to/hello --remote builder \
  --output /tmp/hello-remote-profile
```

Each invocation creates `root/namespace/project-id/jobs/unique-id/`, syncs source,
runs the same watched compiler and returns reports. Requested project-relative
file globs go under `output/artifacts/`; missing requested files make the command
fail. They are never installed over local build products. `remote.json` records
the workspace, sync time, total time, exit status and collected filenames;
`build.json` contains compiler timing, sampled RSS and actual argv audits.

The toolkit source is cached by content hash remotely and its instrumentation
plugin is compiled against that host's Lean SDK. No remote pip install is needed.
The root should be dedicated to this tool. Each invocation gets independent
source and build directories, including simultaneous jobs in the same namespace.
Remote jobs and their outputs remain for inspection; remove old completed job
directories when no longer needed. There is no automatic pruning.

## Serial profile batches

For several modules in dependency order, save a project-relative JSON manifest
such as `tools/repair-batch.json`:

```json
["MyProject/Helper.lean", "MyProject/Consumer.lean"]
```

```sh
lean-buildtools profile-batch tools/repair-batch.json --project /path/to/project \
  --remote builder --emit-root .lake/build/lib/lean \
  --memory 1400 --threads 1 --no-mathlib-build \
  --output /tmp/repair-batch --fetch '.lake/build/lib/lean/MyProject/*'
```

One invocation syncs once and holds one scheduler lease across the entire serial
batch. Each member invokes `lake env lean` directly, without building dependency
sources. Supply matching precompiled dependencies using the inputs/preparation
configuration below. `--emit-root` is a directory inside the private project,
prepended to the import search path: seed a **complete coherent namespace** there
when replacing existing modules, rather than layering a sparse namespace over a
provider. All selected members' old products are removed before the first compile.
Module-form sources emit `.olean`, `.olean.private`, `.olean.server`, `.ilean`
and `.ir`; legacy sources retain their usual output format. The next member can
import those fresh products. The batch stops on the
first failure and removes that member's partial products. Already successful
members remain available for inspection; the batch does not install them locally.

`build.json` summarizes the ordered members and all compiler audits. Each
`members/0000/` report directory contains its own build and profile reports,
plus a `source.lean` snapshot of the compiled member. A source change during
compilation fails that member.
The memory, thread, watchdog and extra Lean arguments apply to every member;
`--timeout` is a per-member limit. Output/mode flags in `--lean-arg` are refused
because the batch owns its output names. Keep the manifest outside excluded
directories such as `.lean-buildtools/`. Explicit `--fetch` globs collect products
under the usual report `artifacts/` directory.

For sources under a directory such as `sources/`, pass `--source-root sources`.
The manifest paths remain project-relative; output module paths are relative to
that source root. [Selective refresh](refresh.md) uses the same serial compiler
lane and can route different members into explicitly ordered complete prefixes.

## Configuration

Definitions merge in this order, with later files overriding earlier ones:

1. `${XDG_CONFIG_HOME:-~/.config}/lean-buildtools/remotes.toml`
2. Project `lean-buildtools.toml`
3. Project `lean-buildtools.local.toml` (keep this untracked)
4. `--remote-config FILE`, or the `LEAN_BUILDTOOLS_REMOTES` file

Tables are under `[remotes.NAME]`. `env` and `lease` merge by key; arrays replace.
`LEAN_BUILDTOOLS_REMOTE` selects a default remote. `LEAN_BUILDTOOLS_NAMESPACE`
overrides the configured namespace; `--remote-namespace` takes precedence over
both. Explicit `--memory` and `--threads` override defaults, while `max_memory`
rejects requests above the configured cap. Thread/memory flags are normalized
and checked on real compiler children. Compiler children within a job are still
serialized. `max_jobs` uses file locks shared by all toolkit jobs under the same
remote root; use one consistent limit for that root.
Use `--lean-arg=-DmaxHeartbeats=100000` and
`--lean-arg=-DmaxRecDepth=1024` to preserve a project's explicit proof budgets.
Source-level options retain their usual Lean behavior.

Other optional fields:

- `python`: remote Python executable, default `python3`.
- `env`: table of environment strings; `path`: prepended PATH entries.
- `excludes`: additional rsync exclusion patterns.
- `deny_sorry`, `no_mathlib_build`: enable the corresponding optional policies.
- `prepare`: arrays of command arguments, run in the private project before the
  build. Commands beginning with `lake` use the same compiler supervisor and caps.
  Other commands are trusted setup commands with a 15 minute time limit.
- `lean_path`: precompiled paths already present on the remote, in search order;
  `{job}` and `{project}` expand to this invocation's private directories.

Keep machine names, credentials, organization policies and coordination commands
in user configuration or the untracked local file. The general backend does not
require an external scheduler.

For a project using Mathlib, download its matching cache instead of rebuilding
Mathlib from source. An optional preparation setting is:

```toml
[remotes.builder]
prepare = [["lake", "exe", "cache", "get"]]
no_mathlib_build = true
```

## Inputs and outputs

Source sync excludes `.git`, reports, virtualenvs, `.env`, root build archives,
and `.lake/build` products. Dependency sources under `.lake/packages` are copied.
`--sync-build` includes `.lake/build` products explicitly. Native `.o`, `.a` and
shared libraries are platform-specific; use the remote's matching SDK/libraries.
Lean import artifacts also require matching Lean versions and coherent providers.
The tool does not claim arbitrary projects or native extensions are portable.

For an external immutable precompiled provider:

```sh
lean-buildtools profile MyProject/Heavy.lean --project /path/to/project \
  --remote builder --remote-input /path/to/provider=deps \
  --lean-path /path/to/provider/lib/lean --output /tmp/remote-profile
```

Paths inside the project and named inputs are translated to their remote copies,
preserving `--lean-path` order. Unmapped external paths are rejected. Synced
symlinks and manifest path dependencies must stay within the private job; copy a
containing workspace when necessary. Only explicitly requested ordinary files
inside the project can be fetched as artifacts.

For a large provider already staged remotely, configure a private copy seed:

```toml
[remotes.builder]
lean_path = ["{job}/inputs/deps/lib/lean"]

[[remotes.builder.seeds]]
source = "/srv/immutable/provider"
destination = "inputs/deps"
```

Seeds are copied into each job with APFS cloning or Linux reflinks where available,
with a normal-copy fallback on Linux. They are never writable hard links to the
provider. Destinations must be under `inputs/` or `project/` and must not exist.
Use `lean_path = ["/absolute/remote/provider/lib/lean"]` only for inputs guaranteed
immutable by their owner; a private seed is usually easier to isolate. Configure
these advanced paths only for trusted projects.

## Optional coordination hooks

Hooks are local argument arrays, not shell snippets. Acquisition must succeed
before compilation. Release runs after execution, including a failed build.

```toml
[remotes.builder.lease]
acquire = ["my-scheduler", "acquire", "--ttl", "30m", "--memory", "{memory}", "--handle", "{job}"]
release = ["my-scheduler", "release", "--handle", "{job}"]
```

Placeholders are `{remote}`, `{namespace}`, `{job}`, `{memory}` and `{threads}`.
Choose a finite lease lifetime appropriate for your scheduler. This is separate
from the remote file-lock concurrency limit, which releases when the worker exits.
Use the same job handle for acquisition and release when the scheduler supports
it, so concurrent jobs from one agent hold separate slots. Pool capacity belongs
to the coordinator's provisioning step; worker acquisition hooks should use the
existing pool without resetting its capacity.

Cancellation targets the recorded job command only after checking its argv and
process group. An unreachable host can delay cancellation and report collection;
use the configured wall timeout for unattended work. The tool is a supervisor for
trusted builds, not a security sandbox or a distributed queue.
