"""Caller scope/order, source gaps and receipt binding."""
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from lean_buildtools.refresh import plan_refresh, run_refresh


class RefreshPlans(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name).resolve()
        self.report = self.project / "audit"
        self.report.mkdir()
        self.sources = self.project / "sources"
        self.sources.mkdir()
        for module in ("Leaf", "PrivateWords", "Root"):
            (self.sources / (module + ".lean")).write_text("module\n")
        graph = dict(schema_version=1, complete=True, roots=[dict(module="Root", name="headline")],
                     modules=["Leaf", "PrivateWords", "Unrelated", "Root"],
                     nodes=[["leaf", [0], []], ["cell", [1], [0]], ["headline", [3], [1]]])
        raw = json.dumps(graph).encode()
        (self.report / "callers.json").write_bytes(raw)
        (self.report / "audit.json").write_text(json.dumps(dict(
            caller_graph_sha256=hashlib.sha256(raw).hexdigest(), artifact_changes=[], module_graph_changed=False)))
        modules = {"Leaf": dict(imports=[]), "PrivateWords": dict(imports=["Leaf"]),
                   "Unrelated": dict(imports=["Leaf"]), "Root": dict(imports=["PrivateWords", "Unrelated"])}
        (self.report / "modules.json").write_text(json.dumps(modules))
        self.opts = SimpleNamespace(changed_module=["Leaf"], changed_declaration=[], include_changed=False,
                                    source_root="sources", emit_root="compiled", source_map=None, output_map=None)

    def tearDown(self):
        self.tmp.cleanup()

    def run_plan(self):
        output = self.project / "plan"
        code = plan_refresh(self.project, self.report, output, self.opts)
        return code, json.loads((output / "plan.json").read_text())

    def test_private_callers_only_and_dependency_order(self):
        code, plan = self.run_plan()
        self.assertEqual(code, 0)
        self.assertEqual(plan["modules"], ["PrivateWords", "Root"])
        self.assertEqual(plan["import_dependent_modules"], 4)
        self.assertEqual(plan["members"][-1]["dependency_path"], ["headline", "cell", "leaf"])
        self.assertEqual(plan["members"][0]["source_root"], "sources")

    def test_include_changed_and_named_declaration(self):
        self.opts.include_changed = True
        self.opts.changed_declaration = ["leaf"]
        _, plan = self.run_plan()
        self.assertEqual(plan["modules"], ["Leaf", "PrivateWords", "Root"])

    def test_missing_sources_are_reported_without_partial_ready(self):
        (self.sources / "Root.lean").unlink()
        code, plan = self.run_plan()
        self.assertEqual(code, 1)
        self.assertEqual(plan["status"], "blocked")
        self.assertEqual(plan["source_gaps"][0]["module"], "Root")
        self.assertEqual(plan["modules"], ["PrivateWords", "Root"])

    def test_output_slot_order_is_explicit(self):
        mapping = self.project / "outputs.json"
        mapping.write_text('{"Root":"compiled/own","*":"compiled/framework"}')
        self.opts.output_map = mapping
        self.opts.output_root = ["compiled/own", "compiled/framework"]
        _, plan = self.run_plan()
        self.assertEqual(plan["output_roots"], ["compiled/own", "compiled/framework"])
        self.assertEqual(plan["members"][0]["emit_root"], "compiled/framework")
        self.assertEqual(plan["members"][1]["emit_root"], "compiled/own")

    def test_tampered_or_incomplete_caller_evidence_rejected(self):
        with (self.report / "callers.json").open("ab") as stream:
            stream.write(b" ")
        with self.assertRaisesRegex(ValueError, "differs from the audit receipt"):
            self.run_plan()

    def test_wrong_declaration_and_source_escape_rejected(self):
        self.opts.changed_declaration = ["typo"]
        with self.assertRaisesRegex(ValueError, "absent from root closure"):
            self.run_plan()

    def test_stale_only_skips_already_clean_callers(self):
        receipt = json.loads((self.report / "audit.json").read_text())
        receipt["roots"] = [dict(unexpected_axioms=[], errors=[], summary_mismatches=[
            dict(blocking=True, comparisons=[dict(module="PrivateWords")])])]
        (self.report / "audit.json").write_text(json.dumps(receipt))
        self.opts.stale_only = True
        _, plan = self.run_plan()
        self.assertEqual(plan["modules"], ["PrivateWords"])

    def test_changed_source_is_rejected_before_any_compile(self):
        _, plan = self.run_plan()
        (self.project / "ready.json").write_text(json.dumps(plan))
        (self.sources / "PrivateWords.lean").write_text("module\n-- changed\n")
        options = SimpleNamespace(target="ready.json", lean_arg=[], lean_path=None)
        with patch("lean_buildtools.refresh.run_members") as compile_sources:
            with self.assertRaisesRegex(ValueError, "sources changed since planning"):
                run_refresh(self.project, self.project / "run", options)
            compile_sources.assert_not_called()

    def test_audit_exception_cannot_leave_successful_aggregate(self):
        _, plan = self.run_plan()
        (self.project / "ready.json").write_text(json.dumps(plan))
        (self.project / "compiled").mkdir()
        options = SimpleNamespace(target="ready.json", lean_arg=[], lean_path=None)
        with patch("lean_buildtools.refresh.run_members", return_value=0), \
                patch("lean_buildtools.audit.run_audit", side_effect=RuntimeError("extractor failed")):
            with self.assertRaisesRegex(RuntimeError, "extractor failed"):
                run_refresh(self.project, self.project / "run", options)
        summary = json.loads((self.project / "run/build.json").read_text())
        self.assertEqual(summary["returncode"], 1)
        self.assertEqual(summary["status"], "audit_failed")


if __name__ == "__main__":
    unittest.main()
