"""Small release boundary checks; no compiler or large fixtures required."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from lean_buildtools.cli import main
from lean_buildtools.jsonl import read_rows
from lean_buildtools.proposals import apply_proposal
from lean_buildtools.runner import cache_directory, toolchain


class Boundaries(unittest.TestCase):
    def test_toolchain_and_platform_fail_clearly(self):
        with patch("lean_buildtools.runner.sys.platform", "win32"):
            with self.assertRaisesRegex(RuntimeError, "macOS and Linux"):
                toolchain(Path.cwd())
        with patch("lean_buildtools.runner.checked", side_effect=["/fake/lean", "Lean (version 4.34.0, test)"]):
            with self.assertRaisesRegex(RuntimeError, "4.35.0 is required"):
                toolchain(Path.cwd())

    def test_cache_is_user_writable_location(self):
        with patch.dict(os.environ, {"XDG_CACHE_HOME": "/tmp/example-cache"}, clear=True), \
                patch("lean_buildtools.runner.sys.platform", "linux"):
            self.assertEqual(cache_directory(), Path("/tmp/example-cache/lean-buildtools"))
        with patch.dict(os.environ, {}, clear=True), patch("lean_buildtools.runner.sys.platform", "darwin"):
            self.assertEqual(cache_directory(), Path.home() / "Library/Caches/lean-buildtools")

    def test_nonfinite_options_are_rejected(self):
        for option, value in [("--interval", "nan"), ("--timeout", "inf"), ("--horizon", "-inf")]:
            with self.subTest(option=option), self.assertRaises(SystemExit) as caught:
                main(["build", "Demo", f"{option}={value}"])
            self.assertEqual(caught.exception.code, 2)

    def test_truncated_record_keeps_completed_data(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            path.write_bytes(b'{"event":"start"}\n{"event":"end","label":"\xe2')
            self.assertEqual(read_rows(path), [{"event": "start"}])
            path.write_bytes(b'{broken}\n')
            with self.assertRaisesRegex(ValueError, "malformed JSON record"):
                read_rows(path)

    def test_write_guards_edits_and_new_destination_collisions(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory).resolve()
            source = project / "Demo.lean"
            source.write_bytes(b"original\n")
            with self.assertRaisesRegex(ValueError, "source changed"):
                apply_proposal(project, source, b"stale\n", {Path("Demo.lean"): "replacement\n"})
            self.assertEqual(source.read_bytes(), b"original\n")
            part = project / "DemoPart1.lean"
            part.write_text("keep me\n")
            with self.assertRaisesRegex(ValueError, "already exists"):
                apply_proposal(project, source, b"original\n", {Path("DemoPart1.lean"): "new\n"})
            self.assertEqual(part.read_text(), "keep me\n")
            part.unlink()
            apply_proposal(project, source, b"original\n", {
                Path("DemoPart1.lean"): "part\n", Path("Demo.lean"): "facade\n"})
            self.assertEqual(source.read_text(), "facade\n")
            self.assertEqual(part.read_text(), "part\n")
            self.assertFalse(list(project.glob(".lean-buildtools-*")))


if __name__ == "__main__":
    unittest.main()
