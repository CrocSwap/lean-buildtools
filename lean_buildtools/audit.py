"""Fresh, body-based axiom audits of the resolved compiled-artifact closure."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import time

from .proof_graph import ProofGraph
from .runner import run_project


def read_roots(project, target):
    path = (project / target).resolve()
    if not path.is_relative_to(project) or not path.is_file():
        raise ValueError("audit manifest must be a file inside the project")
    return validate_roots(json.loads(path.read_text()))


def validate_roots(data):
    if not isinstance(data, dict) or set(data) != {"roots"} or not isinstance(data["roots"], list) or not data["roots"]:
        raise ValueError('audit manifest must contain a nonempty "roots" array')
    seen = set()
    for root in data["roots"]:
        if not isinstance(root, dict) or not {"module", "name"} <= set(root) or set(root) - {"module", "name", "artifact"} or any(
                not isinstance(value, str) or not value or any(c in value for c in "\n\r\x00")
                for value in root.values()):
            raise ValueError("each audit root needs nonempty module and name strings")
        key = root["module"], root["name"]
        if key in seen:
            raise ValueError("duplicate audit root")
        seen.add(key)
    return data["roots"]


def module_records(path):
    records, complete = {}, False
    with path.open() as stream:
        for line in stream:
            row = json.loads(line)
            if complete:
                raise ValueError("graph data after completion")
            if row["event"] == "module":
                if row["module"] in records:
                    raise ValueError("duplicate module record")
                records[row["module"]] = row
            elif row["event"] == "complete":
                if row["modules"] != len(records):
                    raise ValueError("wrong module completion count")
                complete = True
    if not complete:
        raise ValueError("incomplete graph extraction")
    return records


def fingerprint(modules):
    """Hash all logical parts, and every available compiler companion."""
    paths = set()
    for module in modules.values():
        paths.update(module["parts"])
        base = Path(module["path"]).with_suffix("")
        paths.update(str(base) + suffix for suffix in (".ilean", ".ir", ".ir.sig"))
    result = {}
    for name in sorted(paths):
        path = Path(name)
        if not path.exists():
            result[name] = None
            continue
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        result[name] = dict(size=path.stat().st_size, sha256=digest.hexdigest())
    return result


def extract(project, output, options, modules, *, metadata_only, artifacts):
    request = output / "request.json"
    request.write_text(json.dumps(dict(modules=modules, output=str(output / "graph.jsonl"),
                                      metadata_only=metadata_only, artifacts=artifacts)) + "\n")
    source = output / "Graph.lean"
    template = (Path(__file__).parent / "lean/AxiomGraph.lean").read_text()
    source.write_text(template + "\nrun_cmd do\n  liftIO <| BuildTools.AxiomGraph.run " +
                      json.dumps(str(request), ensure_ascii=False) + "\n")
    report = output / "compiler"
    code = run_project(project, report, options, ["env", str(report / "toolchain/bin/lean"),
                                                 "-R", str(output), str(source)])
    if code:
        raise RuntimeError(f"axiom graph extraction failed ({code}); see {report}")
    compiler = json.loads((report / "build.json").read_text())
    if not compiler["caps_verified"]:
        raise RuntimeError(f"axiom graph compiler caps were not verified; see {report}")
    return module_records(output / "graph.jsonl")


def run_audit(project, output, options, *, roots=None):
    roots = read_roots(project, options.target) if roots is None else validate_roots(dict(roots=roots))
    output.mkdir(parents=True, exist_ok=False)
    start = time.monotonic()
    requested = list(dict.fromkeys(root["module"] for root in roots))
    artifacts = {}
    for root in roots:
        if "artifact" in root:
            if root["module"] in artifacts and artifacts[root["module"]] != root["artifact"]:
                raise ValueError("conflicting explicit artifacts for " + root["module"])
            artifacts[root["module"]] = root["artifact"]
    discovery = output / "discovery"
    discovery.mkdir()
    modules = extract(project, discovery, options, requested, metadata_only=True, artifacts=list(artifacts.items()))
    for module, artifact in artifacts.items():
        expected = os.path.abspath(project / artifact)
        if module not in modules or os.path.abspath(modules[module]["path"]) != expected:
            raise RuntimeError("requested artifact was not selected for " + module)
    if not any(module["summary_extension"] for module in modules.values()):
        raise RuntimeError("no recognized Lean 4.35 axiom-summary schema in the import closure")
    before = fingerprint(modules)
    body = output / "bodies"
    body.mkdir()
    actual_modules = extract(project, body, options, requested, metadata_only=False, artifacts=list(artifacts.items()))
    graph = ProofGraph(output / "graph.sqlite")
    try:
        graph.load(body / "graph.jsonl")
        result = graph.audit(roots, dependency_output=output / "callers.json")
    finally:
        graph.close()
    after = fingerprint(actual_modules)
    changed = sorted(name for name in before.keys() | after.keys() if before.get(name) != after.get(name))
    module_change = modules != actual_modules
    if changed or module_change:
        result["status"] = "fail"
        for root in result["roots"]:
            root["status"] = "fail"
            root["errors"].append(dict(kind="artifact_closure_changed"))
    result.update(elapsed_s=time.monotonic() - start, project=str(project),
                  module_count=len(modules), artifact_changes=changed,
                  module_graph_changed=module_change,
                  freshness_scope="fresh process; full private types/bodies and cached summaries; "
                                  "all resolved artifact companions stable across extraction; "
                                  "does not establish correspondence to edited source files")
    (output / "artifacts.json").write_text(json.dumps(before, indent=2) + "\n")
    (output / "modules.json").write_text(json.dumps(modules, indent=2) + "\n")
    result["caller_graph_sha256"] = hashlib.sha256((output / "callers.json").read_bytes()).hexdigest()
    (output / "audit.json").write_text(json.dumps(result, indent=2) + "\n")
    if not getattr(options, "keep_graph", False):
        (body / "graph.jsonl").unlink()
        (output / "graph.sqlite").unlink()
    for root in result["roots"]:
        print(f"axiom audit: {root['root']['name']}: {root['status']}; "
              f"{len(root['unexpected_axioms'])} unexpected axioms, "
              f"{len(root['summary_mismatches'])} summary differences "
              f"({sum(m['blocking'] for m in root['summary_mismatches'])} outside allowlist), "
              f"{len(root['errors'])} errors")
    # Keep a common aggregate audit file for existing remote callers.
    children = []
    for directory in (discovery, body):
        data = json.loads((directory / "compiler/build.json").read_text())
        children.extend(data["children"])
    (output / "build.json").write_text(json.dumps(dict(
        command="audit", returncode=0 if result["status"] == "pass" else 1,
        children=children, caps_verified=bool(children) and all(c["argv_verified"] for c in children),
        elapsed_s=time.monotonic() - start), indent=2) + "\n")
    return 0 if result["status"] == "pass" else 1
