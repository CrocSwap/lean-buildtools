# Releasing

The first release is an alpha, scoped to Lean 4.35.0 on macOS and Linux.

1. Update the version in `pyproject.toml` and the changelog.
2. Ensure CI passes for the commit you intend to release.
3. Build and check distributions locally:

   ```sh
   python -m pip install build twine
   python -m build
   python -m twine check dist/*
   ```

4. In a fresh virtual environment, install the wheel and run the tutorial smoke
   check from the source checkout:

   ```sh
   python -m pip install dist/*.whl
   python scripts/smoke_install.py
   ```

5. Tag that reviewed commit, attach the wheel and source distribution to a release,
   and include the relevant changelog entry. PyPI publication can be added later.

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
