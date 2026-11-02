# Releasing

Release 0.2.0 remains an alpha, scoped to Lean 4.35.0 on macOS and Linux.
The optional `native/` package remains experimental and is included in source
archives, separately from the Python wheel.

1. Update the version in `pyproject.toml` and the changelog.
2. Run the full suite on macOS and Linux with Lean installed, including the
   remote transport, serial batch, private-import audit and selective refresh
   fixtures. Ensure CI passes for the commit you intend to release:

   ```sh
   python -m unittest discover -s tests -v
   lean-buildtools build NativeProof.Smoke --project native --deny-sorry
   lean-buildtools profile-batch Tests/rules.json --project native --deny-sorry
   ```
3. Build and check distributions locally:

   ```sh
   python -m pip install build twine
   python -m build --outdir release-artifacts/0.2.0
   python -m twine check release-artifacts/0.2.0/*
   ```

4. In a fresh virtual environment, install the wheel and run the tutorial smoke
   check from the source checkout:

   ```sh
   python -m venv /tmp/lean-buildtools-release-check
   . /tmp/lean-buildtools-release-check/bin/activate
   python -m pip install release-artifacts/0.2.0/*.whl
   python scripts/smoke_install.py
   ```

5. Inspect the tracked tree and both archives for private paths, hosts,
   credentials, internal project references and generated reports. Check that
   the Apache license and bundled Lean/C helper sources are present. The source
   distribution should also contain the optional native package and its examples.
   Test installation from the source distribution in another fresh environment.
6. Record the candidate commit, artifact SHA256 hashes, platform/test results and
   any remaining limits. Keep candidate preparation local until publication is
   approved. After approval, tag the reviewed commit and attach the distributions
   and changelog to a release. PyPI publication can be added later.

The manual **Build release artifacts** workflow performs the distribution build
and metadata checks and offers downloadable artifacts. It does not publish or
create a release automatically. Keep credentials out of the repository.

For a source-only handoff without Git history, after committing the reviewed tree:

```sh
mkdir -p release-artifacts
git archive --format=tar.gz --prefix=lean-buildtools/ --output=release-artifacts/lean-buildtools-source.tar.gz HEAD
```

Inspect the source archive before using it to initialize a public repository.
Wheel/sdist contents should include the Apache license and bundled plugin sources,
and should exclude caches, build products, and local reports.
