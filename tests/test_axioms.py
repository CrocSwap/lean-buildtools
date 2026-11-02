"""Raw graph tests: private cache debt, cycles and incomplete evidence."""
import json
from pathlib import Path
import tempfile
import unittest

from lean_buildtools.proof_graph import ProofGraph


def declaration(name, module, dependencies=(), cached=(), kind="theorem"):
    return dict(event="declaration", name=name, module=module, kind=kind,
                dependencies=list(dependencies), cached_axioms=list(cached) if cached is not None else None,
                unsafe=False, partial=False, string_literal=False)


class AxiomGraphs(unittest.TestCase):
    def audit(self, records, imports=None, roots=None, export=False):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            modules = set(r["module"] for r in records)
            imports = imports or {module: [] for module in modules}
            lines = [dict(event="module", module=module, imports=imports[module],
                          declaration_count=sum(r["module"] == module for r in records))
                     for module in sorted(modules)]
            lines += records + [dict(event="complete", modules=len(modules))]
            (path / "graph.jsonl").write_text("".join(json.dumps(r) + "\n" for r in lines))
            graph = ProofGraph(path / "graph.sqlite")
            try:
                graph.load(path / "graph.jsonl")
                result = graph.audit(roots or [dict(module="Root", name="root")],
                                     dependency_output=path / "callers.json" if export else None)
                if export:
                    result["exported_graph"] = json.loads((path / "callers.json").read_text())
                return result
            finally:
                graph.close()

    def test_standard_axiom_path(self):
        report = self.audit([
            declaration("root", "Root", ["propext"], ["propext"]),
            declaration("propext", "Root", cached=["propext"], kind="axiom")])
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["roots"][0]["axioms"], ["propext"])

    def test_unexpected_axiom_has_actual_declaration_path(self):
        report = self.audit([
            declaration("root", "Root", ["helper"], ["native.ax"]),
            declaration("helper", "Root", ["native.ax"], ["native.ax"]),
            declaration("native.ax", "Root", cached=["native.ax"], kind="axiom")])
        self.assertEqual(report["status"], "fail")
        unexpected = report["roots"][0]["unexpected_axioms"][0]
        self.assertEqual([r["name"] for r in unexpected["path"]], ["root", "helper", "native.ax"])

    def test_private_import_cache_debt_after_leaf_repair(self):
        report = self.audit([
            declaration("root", "Root", ["private.cell"], ["old.native.ax"]),
            declaration("private.cell", "PrivateWords", ["leaf"], ["old.native.ax"]),
            declaration("leaf", "Leaf", cached=[])],
            imports={"Root": ["PrivateWords"], "PrivateWords": ["Leaf"], "Leaf": []})
        self.assertEqual(report["status"], "fail")
        root = report["roots"][0]
        self.assertEqual(root["axioms"], [])
        mismatch = next(r for r in root["summary_mismatches"] if r["name"] == "private.cell")
        self.assertEqual([r["name"] for r in mismatch["path"]], ["root", "private.cell"])
        self.assertEqual(mismatch["comparisons"][0]["cached_only"], ["old.native.ax"])
        self.assertTrue(any(r["summary_declaration"] == "private.cell" and
                            r["path"][-1]["via"] == "cached_summary"
                            for r in root["cached_unexpected_axioms"]))

    def test_cached_summary_cannot_hide_real_axiom(self):
        report = self.audit([
            declaration("root", "Root", ["native.ax"], []),
            declaration("native.ax", "Root", cached=None, kind="axiom")])
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["roots"][0]["axioms"], ["native.ax"])
        self.assertTrue(report["roots"][0]["summary_mismatches"])

    def test_allowed_only_summary_difference_is_visible_and_nonblocking(self):
        report = self.audit([
            declaration("root", "Root", ["propext"], []),
            declaration("propext", "Root", cached=["propext"], kind="axiom")])
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["roots"][0]["axioms"], ["propext"])
        self.assertTrue(report["roots"][0]["summary_mismatches"])
        self.assertFalse(report["roots"][0]["summary_mismatches"][0]["blocking"])

    def test_cycle_propagates_axiom_to_every_member(self):
        report = self.audit([
            declaration("root", "Root", ["cycle.a", "cycle.b"], ["native.ax"]),
            declaration("cycle.a", "Root", ["cycle.b"], ["native.ax"]),
            declaration("cycle.b", "Root", ["cycle.a", "native.ax"], ["native.ax"]),
            declaration("native.ax", "Root", cached=["native.ax"], kind="axiom")])
        self.assertEqual(report["roots"][0]["axioms"], ["native.ax"])
        self.assertEqual(report["roots"][0]["summary_mismatches"], [])

    def test_missing_declaration_and_ambiguous_bodies_fail_closed(self):
        report = self.audit([declaration("root", "Root", ["absent"], [])])
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["roots"][0]["errors"][0]["kind"], "missing_declaration")
        self.assertEqual(report["roots"][0]["summary_mismatches"], [])
        report = self.audit([
            declaration("root", "Root", ["helper"]),
            declaration("helper", "Root", ["root"]),
            declaration("helper", "Other", [])],
            imports={"Root": ["Other"], "Other": []})
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["roots"][0]["errors"][0]["kind"], "ambiguous_declaration")

    def test_truncated_extraction_cannot_be_a_passing_graph(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path / "partial.jsonl").write_text(json.dumps(dict(
                event="module", module="Root", imports=[], declaration_count=0)) + "\n")
            graph = ProofGraph(path / "graph.sqlite")
            try:
                with self.assertRaisesRegex(ValueError, "incomplete module graph"):
                    graph.load(path / "partial.jsonl")
            finally:
                graph.close()

    def test_caller_evidence_preserves_private_dependencies(self):
        report = self.audit([
            declaration("root", "Root", ["private.cell"]),
            declaration("private.cell", "PrivateWords", ["leaf"]),
            declaration("leaf", "Leaf")],
            imports={"Root": ["PrivateWords"], "PrivateWords": ["Leaf"], "Leaf": []}, export=True)
        graph = report["exported_graph"]
        self.assertTrue(graph["complete"])
        nodes = {n[0]: n for n in graph["nodes"]}
        cell = nodes["private.cell"]
        self.assertEqual([graph["modules"][i] for i in cell[1]], ["PrivateWords"])
        self.assertEqual([graph["nodes"][i][0] for i in cell[2]], ["leaf"])


if __name__ == "__main__":
    unittest.main()
