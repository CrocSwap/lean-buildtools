import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from lean_buildtools.batch import load_manifest, products, run_batch


class BatchBoundaries(unittest.TestCase):
    def test_manifest_rejects_duplicates_escapes_and_output_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "A.lean").write_text("module\n")
            manifest = root / "batch.json"
            for sources in ([], ["A.lean", "A.lean"], ["../A.lean"], ["missing.lean"], [1]):
                manifest.write_text(json.dumps(sources))
                with self.subTest(sources=sources), self.assertRaises(ValueError):
                    load_manifest(root, "batch.json", "out", [])
            manifest.write_text('["A.lean"]')
            for args in (["-oelsewhere"], ["--ilean=elsewhere"], ["--run"]):
                with self.subTest(args=args), self.assertRaises(ValueError):
                    load_manifest(root, "batch.json", "out", args)
            with tempfile.TemporaryDirectory() as outside:
                (root / "escape").symlink_to(outside, target_is_directory=True)
                with self.assertRaises(ValueError):
                    load_manifest(root, "batch.json", "escape", [])

    def test_serial_failure_clears_stale_and_partial_products(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            names = ["A.lean", "B.lean", "C.lean"]
            for name in names:
                (root / name).write_text("module\n")
            (root / "batch.json").write_text(json.dumps(names))
            compiled = root / "compiled"
            compiled.mkdir()
            for name in names:
                for path in products(compiled, Path(name)):
                    path.write_text("stale")
            calls = []
            def compile_one(project, report, options, args):
                name = Path(args[2]).name
                calls.append(name)
                # Unprocessed members cannot import old products from this batch.
                self.assertFalse((compiled / "C.olean").exists())
                if name == "B.lean":
                    self.assertEqual((compiled / "A.olean").read_text(), "fresh")
                report.mkdir(parents=True)
                (report / "build.json").write_text(json.dumps(dict(
                    children=[dict(argv_verified=True)], caps_verified=True)))
                for path in products(compiled, Path(name))[:5]:
                    path.write_text("fresh")
                return 1 if name == "B.lean" else 0
            opts = SimpleNamespace(target="batch.json", emit_root="compiled", lean_arg=[], lean_path=None)
            with patch("lean_buildtools.batch.run_project", side_effect=compile_one):
                self.assertEqual(run_batch(root, root / "report", opts), 1)
            self.assertEqual(calls, names[:2])
            self.assertTrue((compiled / "A.olean").exists())
            self.assertFalse((compiled / "B.olean").exists())
            self.assertFalse((compiled / "C.olean").exists())
            summary = json.loads((root / "report/build.json").read_text())
            self.assertFalse(summary["completed"])
            self.assertEqual(len(summary["children"]), 2)


if __name__ == "__main__":
    unittest.main()
