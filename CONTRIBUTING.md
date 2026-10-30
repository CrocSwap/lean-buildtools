# Contributing

Small fixes and reproducible bug reports are welcome. Include your OS, Python
version, `lean --version`, command, and a small Lean example. Report files can
contain local paths and source text; review them before sharing.

For development, use Lean 4.35.0 and a C compiler:

```sh
python -m pip install -e .
python -m unittest discover -s tests -v
```

The integration tests run real compiler processes. CI installs Lean explicitly
and also runs the tutorial using a built wheel outside the checkout. Missing
Lean causes local integration tests to skip; check that it is installed when
working on compiler behavior.

Keep changes small, add focused coverage for behavior you change, and update
the README when a flag or support boundary changes. Contributions are provided
under this project's Apache-2.0 license.
