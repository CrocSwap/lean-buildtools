# Hello, Lean!

Install the toolkit from the repository root with `python -m pip install .`.
Activate that Python environment, install Lean 4.35.0 through Elan if needed,
and enter the tutorial project:

```sh
elan toolchain install leanprover/lean4:v4.35.0
cd examples/hello
```

`Hello.lean` defines a greeting and two small theorems. It has no downloaded
dependencies. Build it with an audited compiler cap, then print the greeting:

```sh
lean-buildtools build Hello --output .lean-buildtools/build
lake env lean --run Main.lean
```

The second command prints `Hello, Lean!`. The build report records the real
compiler argv, the memory samples, and the result.

Profile the declarations:

```sh
lean-buildtools profile Hello.lean --output .lean-buildtools/profile
cat .lean-buildtools/profile/profile.tsv
```

Try a split and inspect the staged files:

```sh
lean-buildtools split Hello.lean --profile .lean-buildtools/profile/profile.json --output .lean-buildtools/split
cat .lean-buildtools/split/proposal.patch
```

This compiles two parts, an import facade, and a consumer that checks the
original declaration names. The original `Hello.lean` stays unchanged.

The example also contains deliberately redundant imports. Try removing them:

```sh
lean-buildtools trim Hello.lean --max-candidates 0 --output .lean-buildtools/trim
cat .lean-buildtools/trim/proposal.patch
```

Each accepted deletion must compile. To apply a proposal, repeat its command
with a new output directory and `--write`; first review the staged sources.
Use fresh output directories when repeating any of these commands.
