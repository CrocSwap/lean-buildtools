"""Focused real-compiler checks for the caps, watchdog, and proposal guarantees."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("lean") and shutil.which("lake"), "Lean/Lake required")
class Integration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="lean buildtools-")
        self.root = Path(self.tmp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        (self.project / "lean-toolchain").write_text("leanprover/lean4:v4.35.0\n")
        (self.project / "lakefile.toml").write_text(
            'name = "probe"\n[[lean_lib]]\nname = "Probe"\n'
            'moreLeanArgs = ["-j8", "-M8000"]\n')
        (self.project / "Probe.lean").write_text(
            'module\npublic import Init.Data.List.Basic\npublic import Init.Data.Nat.Basic\n'
            '@[expose] public section\nnamespace Probe\n'
            'theorem first : 1 + 1 = 2 := by decide\n'
            'theorem second : True := by trivial\nend Probe\n')

    def tearDown(self):
        self.tmp.cleanup()

    def run_tool(self, command, target, output, *extra, success=True):
        path = self.root / output
        env = os.environ.copy()
        if not env.get("LEAN_BUILDTOOLS_TEST_INSTALLED"):
            env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.run([sys.executable, "-m", "lean_buildtools", command, target,
                               "--project", str(self.project), "--output", str(path),
                               "--timeout", "60", "--horizon", "0", *extra], cwd=self.root, env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=90)
        if success:
            self.assertEqual(proc.returncode, 0, proc.stdout)
        else:
            self.assertNotEqual(proc.returncode, 0, proc.stdout)
        return path, proc

    def assert_audited(self, path):
        data = json.loads((path / "build.json").read_text())
        self.assertTrue(data["caps_verified"], data)
        for child in data["children"]:
            self.assertIn("-j1", child["argv"])
            self.assertIn("-M1400", child["argv"])
            self.assertNotIn("-j8", child["argv"])
            self.assertNotIn("-M8000", child["argv"])

    def test_build_caps_override_project_flags_and_cache_is_explicit(self):
        path, _ = self.run_tool("build", "Probe", "build")
        self.assert_audited(path)
        path, _ = self.run_tool("build", "Probe", "cached")
        data = json.loads((path / "build.json").read_text())
        self.assertTrue(data["cached_only"])
        self.assertFalse(data["caps_verified"])

    def test_profile_split_and_trim_preserve_source(self):
        before = (self.project / "Probe.lean").read_bytes()
        path, _ = self.run_tool("profile", "Probe.lean", "profile")
        self.assert_audited(path)
        data = json.loads((path / "profile.json").read_text())
        self.assertEqual({r["name"] for r in data["declarations"]}, {"theorem first", "theorem second"})
        self.assertTrue(all(r["heartbeats_raw"] > 0 for r in data["declarations"]))
        split, _ = self.run_tool("split", "Probe.lean", "split", "--profile", str(path / "profile.json"))
        self.assertEqual(json.loads((split / "proposal.json").read_text())["checked_names"],
                         ["Probe.first", "Probe.second"])
        trim, _ = self.run_tool("trim", "Probe.lean", "trim")
        self.assertTrue(json.loads((trim / "proposal.json").read_text())["removed"])
        self.assertEqual((self.project / "Probe.lean").read_bytes(), before)

    def test_audit_detects_stale_privately_imported_summary(self):
        sources = self.project / "sources"
        sources.mkdir()
        (sources / "AuditLeaf.lean").write_text(
            "module\n@[expose] public section\nnamespace AuditTest\n"
            "axiom oldProof : True\ntheorem leaf : True := oldProof\nend AuditTest\n")
        (sources / "AuditPrivate.lean").write_text(
            "module\npublic import AuditLeaf\n@[expose] public section\n"
            "namespace AuditTest\ntheorem cell : True := leaf\nend AuditTest\n")
        (sources / "AuditRoot.lean").write_text(
            "module\nimport AuditPrivate\n@[expose] public section\n"
            "namespace AuditTest\ntheorem headline : True := cell\nend AuditTest\n")
        (self.project / "all.json").write_text('["sources/AuditLeaf.lean", "sources/AuditPrivate.lean", "sources/AuditRoot.lean"]')
        (self.project / "leaf.json").write_text('["sources/AuditLeaf.lean"]')
        (self.project / "roots.json").write_text(json.dumps({"roots": [
            {"module": "AuditRoot", "name": "AuditTest.headline"}]}))
        prefix = str(self.project / ".lake/build/lib/lean")
        self.run_tool("profile-batch", "all.json", "audit-before", "--lean-path", prefix, "--source-root", "sources")
        (sources / "AuditLeaf.lean").write_text(
            "module\n@[expose] public section\nnamespace AuditTest\n"
            "theorem leaf : True := True.intro\nend AuditTest\n")
        self.run_tool("profile-batch", "leaf.json", "audit-leaf", "--lean-path", prefix, "--source-root", "sources")
        stale, _ = self.run_tool("audit", "roots.json", "audit-stale", "--lean-path", prefix, success=False)
        data = json.loads((stale / "audit.json").read_text())
        self.assertEqual(data["status"], "fail")
        self.assertEqual(data["roots"][0]["axioms"], [])
        self.assertIn("AuditTest.cell", {r["name"] for r in data["roots"][0]["summary_mismatches"]})
        self.assert_audited(stale)
        plan, _ = self.run_tool("refresh-plan", str(stale), "audit-plan", "--changed-module", "AuditLeaf",
                                "--source-root", "sources")
        planned = json.loads((plan / "plan.json").read_text())
        self.assertEqual(planned["modules"], ["AuditPrivate", "AuditRoot"])
        (self.project / "refresh.json").write_text((plan / "plan.json").read_text())
        clean, _ = self.run_tool("refresh", "refresh.json", "audit-clean", "--lean-path", prefix)
        data = json.loads((clean / "audit/audit.json").read_text())
        self.assertEqual(data["status"], "pass")
        self.assertEqual(data["roots"][0]["summary_mismatches"], [])
        self.assertFalse((clean / "audit/graph.sqlite").exists())
        self.assert_audited(clean)

    def stress_source(self):
        (self.project / "Probe.lean").write_text(
            'import Lean.Elab.Tactic.BuiltinTactic\nset_option maxRecDepth 100000\n'
            'theorem allocation : True := by\n'
            '  run_tac\n'
            '    let mut batches : Array (List Nat) := #[]\n'
            '    for index in [:32] do\n'
            '      batches := batches.push (List.range (1000000 + index))\n'
            '      IO.sleep 100\n'
            '    IO.sleep 5000\n'
            '    Lean.logInfo m!"{batches.foldl (fun n xs => n + xs.length) 0}"\n'
            '  trivial\n')

    def test_watchdog_names_running_declaration(self):
        (self.project / "Probe.lean").write_text(
            'import Lean.Elab.Tactic.BuiltinTactic\ntheorem baseline : True := by trivial\n')
        baseline, _ = self.run_tool("profile", "Probe.lean", "baseline", "--stop-fraction", "0.99")
        peak = json.loads((baseline / "build.json").read_text())["children"][0]["peak_rss_bytes"]
        # Import residency varies between processes on hosted runners. Leave
        # enough headroom to exercise the allocating theorem, rather than startup.
        fraction = min(0.85, (peak / 1024**2 + 256) / 1400)
        self.stress_source()
        path, proc = self.run_tool("profile", "Probe.lean", "stopped", "--memory", "1400",
                                   "--stop-fraction", str(fraction), "--interval", "0.03", success=False)
        children = json.loads((path / "build.json").read_text())["children"]
        self.assertTrue(children[0]["argv_verified"])
        self.assertIsNotNone(children[0]["stop_reason"], proc.stdout)
        self.assertIsNotNone(children[0]["active_command"], proc.stdout)
        self.assertIn("theorem allocation", proc.stdout)

    def test_growth_stops_before_threshold(self):
        self.stress_source()
        path, proc = self.run_tool("profile", "Probe.lean", "trend", "--horizon", "3",
                                   "--stop-fraction", "0.85", "--interval", "0.03", success=False)
        child = json.loads((path / "build.json").read_text())["children"][0]
        self.assertIn("growth", child["stop_reason"], proc.stdout)
        self.assertLess(child["peak_rss_bytes"], 1400 * 1024**2 * 0.85)
        self.assertIn("theorem allocation", proc.stdout)

    def test_wall_timeout_retains_child_audit(self):
        self.stress_source()
        path, proc = self.run_tool("profile", "Probe.lean", "timeout", "--timeout", "2", success=False)
        data = json.loads((path / "build.json").read_text())
        self.assertTrue(data["timed_out"], proc.stdout)
        self.assertEqual(data["incomplete_children"], 0, proc.stdout)
        self.assertTrue(data["caps_verified"], proc.stdout)
        self.assertTrue(data["children"][0]["stop_reason"], proc.stdout)

    def test_policy_restrictions_are_opt_in(self):
        (self.project / "Probe.lean").write_text('theorem unfinished : True := by sorry\n')
        self.run_tool("profile", "Probe.lean", "sorry-allowed")
        self.run_tool("profile", "Probe.lean", "sorry-denied", "--deny-sorry", success=False)
        (self.project / "Mathlib").mkdir()
        (self.project / "Mathlib/Demo.lean").write_text('theorem demo : True := by trivial\n')
        self.run_tool("profile", "Mathlib/Demo.lean", "mathlib-allowed")
        _, proc = self.run_tool("profile", "Mathlib/Demo.lean", "mathlib-denied",
                                "--no-mathlib-build", success=False)
        self.assertIn("refusing to rebuild Mathlib", proc.stdout)


if __name__ == "__main__":
    unittest.main()
