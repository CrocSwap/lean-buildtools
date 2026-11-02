"""Plan caller refreshes from actual private declaration dependencies."""
from __future__ import annotations

from collections import deque
from copy import copy
from graphlib import CycleError, TopologicalSorter
import hashlib
import json
import os
from pathlib import Path
import time

from .batch import owned_path, products, run_members, validate_lean_args


def read_mapping(path):
    if path is None:
        return {}
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or any(not isinstance(k, str) or not k or
                                         not isinstance(v, str) or not v for k, v in data.items()):
        raise ValueError("mapping files must contain nonempty string keys and values")
    return data


def plan_refresh(project, audit_report, output, options):
    audit = json.loads((audit_report / "audit.json").read_text())
    raw = (audit_report / "callers.json").read_bytes()
    if hashlib.sha256(raw).hexdigest() != audit.get("caller_graph_sha256"):
        raise ValueError("caller evidence differs from the audit receipt")
    graph = json.loads(raw)
    if graph.get("schema_version") != 1 or not graph.get("complete"):
        raise ValueError("refresh needs a complete, unambiguous caller graph")
    if audit["artifact_changes"] or audit["module_graph_changed"]:
        raise ValueError("cannot plan from an unstable artifact snapshot")
    modules = json.loads((audit_report / "modules.json").read_text())
    names = graph["modules"]
    nodes = graph["nodes"]
    if len(set(names)) != len(names) or set(names) != set(modules):
        raise ValueError("caller/module graph disagreement")
    module_ids = {name: index for index, name in enumerate(names)}
    unknown = set(options.changed_module) - module_ids.keys()
    if unknown:
        raise ValueError("changed modules absent from audit: " + ", ".join(sorted(unknown)))
    changed = {module_ids[name] for name in options.changed_module}
    by_name, reverse = {}, [[] for _ in nodes]
    for index, node in enumerate(nodes):
        if not isinstance(node, list) or len(node) != 3 or not isinstance(node[0], str) or node[0] in by_name:
            raise ValueError("invalid caller node")
        name, owners, dependencies = node
        if not owners or any(type(i) is not int or not 0 <= i < len(names) for i in owners):
            raise ValueError("caller node has invalid module owners")
        if any(type(i) is not int or not 0 <= i < len(nodes) for i in dependencies):
            raise ValueError("caller node has invalid dependency ids")
        by_name[name] = index
        for dependency in dependencies:
            reverse[dependency].append(index)
    if options.changed_declaration:
        unknown = set(options.changed_declaration) - by_name.keys()
        if unknown:
            raise ValueError("changed declarations absent from root closure: " + ", ".join(sorted(unknown)))
        seeds = {by_name[name] for name in options.changed_declaration}
        if any(not (set(nodes[index][1]) & changed) for index in seeds):
            raise ValueError("changed declaration does not belong to a changed module")
    else:
        seeds = {index for index, node in enumerate(nodes) if set(node[1]) & changed}
    dirty = {index: None for index in seeds}
    pending = deque(sorted(seeds))
    while pending:
        dependency = pending.popleft()
        for caller in reverse[dependency]:
            if caller not in dirty:
                dirty[caller] = dependency
                pending.append(caller)
    affected = {names[owner] for index in dirty for owner in nodes[index][1]}
    selected = affected | set(options.changed_module) if options.include_changed else affected - set(options.changed_module)
    stale_only = getattr(options, "stale_only", False)
    if stale_only:
        if any(root["unexpected_axioms"] or root["errors"] for root in audit["roots"]):
            raise ValueError("stale-only refresh requires repaired actual closures without errors")
        stale_modules = {comparison["module"] for root in audit["roots"]
                         for difference in root["summary_mismatches"] if difference["blocking"]
                         for comparison in difference["comparisons"]}
        selected &= stale_modules
    imports = {name: set(record["imports"]) for name, record in modules.items()}
    if set().union(*imports.values()) - modules.keys():
        raise ValueError("module import graph is incomplete")
    try:
        order = [name for name in TopologicalSorter(imports).static_order() if name in selected]
    except CycleError as error:
        raise ValueError("cyclic module import graph") from error
    source_map = read_mapping(options.source_map)
    output_map = read_mapping(options.output_map) or {"*": options.emit_root}
    source_root = owned_path(project, options.source_root)
    members, gaps = [], []
    for module in order:
        relative = Path(*module.split(".")).with_suffix(".lean")
        source = owned_path(project, source_map[module]) if module in source_map else source_root / relative
        source = source.resolve()
        expected_parts = relative.parts
        if not source.is_relative_to(project) or source.parts[-len(expected_parts):] != expected_parts:
            gaps.append(dict(module=module, source=str(source), reason="source path does not match module name inside project"))
            continue
        member_root = source
        for _ in expected_parts:
            member_root = member_root.parent
        matches = [prefix for prefix in output_map if prefix == module or module.startswith(prefix + ".")]
        key = max(matches, key=len) if matches else "*"
        destination = owned_path(project, output_map.get(key, options.emit_root))
        if destination == project or destination.is_file():
            raise ValueError("output roots must be compiled directories inside the project")
        if not source.is_file():
            gaps.append(dict(module=module, source=str(source.relative_to(project)), reason="missing source"))
            continue
        witness = next((index for index in dirty if module_ids[module] in nodes[index][1]), None)
        reason = []
        while witness is not None:
            reason.append(nodes[witness][0])
            witness = dirty[witness]
        members.append(dict(module=module, source=str(source.relative_to(project)),
                            source_root=str(member_root.relative_to(project)),
                            emit_root=str(destination.relative_to(project)),
                            source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                            dependency_path=reason))
    required_roots = {member["emit_root"] for member in members}
    explicit_order = getattr(options, "output_root", [])
    if explicit_order:
        output_roots = [str(owned_path(project, value).relative_to(project)) for value in explicit_order]
        if len(set(output_roots)) != len(output_roots) or required_roots - set(output_roots):
            raise ValueError("ordered output roots must be unique and cover every member")
    elif len(required_roots) > 1:
        raise ValueError("multiple output roots need explicit --output-root search order")
    else:
        output_roots = sorted(required_roots)
    # A comparison with import reachability makes the narrower body scope explicit.
    import_dirty = set(options.changed_module)
    while True:
        additions = {name for name, deps in imports.items() if deps & import_dirty} - import_dirty
        if not additions:
            break
        import_dirty.update(additions)
    plan = dict(schema_version=1, status="blocked" if gaps else "ready",
                scope="exact reverse declaration dependencies within the audited headline closure",
                roots=graph["roots"], changed_modules=sorted(set(options.changed_module)),
                changed_declarations=sorted(set(options.changed_declaration)),
                include_changed=options.include_changed, modules=order, members=members,
                stale_only=stale_only,
                source_gaps=gaps, output_roots=output_roots,
                affected_declarations=len(dirty), import_dependent_modules=len(import_dirty),
                audit_caller_graph_sha256=audit["caller_graph_sha256"])
    output.mkdir(parents=True, exist_ok=False)
    (output / "plan.json").write_text(json.dumps(plan, indent=2) + "\n")
    print(f"refresh plan: {len(order)} modules; {len(gaps)} source gaps; "
          f"{len(import_dirty)} modules in broader import closure; {output / 'plan.json'}")
    return 1 if gaps else 0


def read_plan(project, target):
    path = owned_path(project, target)
    plan = json.loads(path.read_text())
    if plan.get("schema_version") != 1 or plan.get("status") != "ready" or plan.get("source_gaps"):
        raise ValueError("refresh requires a ready plan without source gaps")
    members = plan.get("members")
    if not isinstance(members, list) or not isinstance(plan.get("modules"), list):
        raise ValueError("refresh plan has invalid members")
    required = {"module", "source", "source_root", "emit_root", "source_sha256"}
    for member in members:
        if not isinstance(member, dict) or not required <= member.keys() or any(
                not isinstance(member[key], str) or not member[key] for key in required):
            raise ValueError("invalid refresh member")
    if [member["module"] for member in members] != plan["modules"] or len(set(plan["modules"])) != len(members):
        raise ValueError("refresh plan module order disagrees with its members")
    if not isinstance(plan.get("output_roots"), list):
        raise ValueError("refresh plan needs ordered output roots")
    for root in plan["output_roots"]:
        owned_path(project, root)
    if any(member["emit_root"] not in plan["output_roots"] for member in members):
        raise ValueError("member output root is absent from the ordered search roots")
    from .audit import validate_roots
    validate_roots(dict(roots=plan.get("roots")))
    return plan, hashlib.sha256(path.read_bytes()).hexdigest()


def source_changes(project, members):
    changed = []
    for member in members:
        source = owned_path(project, member["source"])
        if not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest() != member["source_sha256"]:
            changed.append(member["source"])
    return changed


def run_refresh(project, output, options):
    plan, plan_digest = read_plan(project, options.target)
    validate_lean_args(options.lean_arg)
    changed = source_changes(project, plan["members"])
    if changed:
        raise ValueError("sources changed since planning; re-plan: " + ", ".join(changed))
    if any(not owned_path(project, root).is_dir() for root in plan["output_roots"]):
        raise ValueError("seed complete coherent output roots before refreshing")
    output.mkdir(parents=True, exist_ok=False)
    start = time.monotonic()
    code, status = 1, "compile_failed"
    roots = plan["roots"]
    try:
        if plan["members"]:
            code = run_members(project, output / "compile", options, plan["members"],
                               plan["output_roots"], command="refresh-compile")
            if code:
                return code
        changed = source_changes(project, plan["members"])
        if changed:
            status = "source_changed"
            code = 1
            return code
        targets = {}
        for member in plan["members"]:
            source = owned_path(project, member["source"])
            source_root = owned_path(project, member["source_root"])
            root = owned_path(project, member["emit_root"])
            targets[member["module"]] = str(products(root, source.relative_to(source_root))[0])
        roots = [dict(root, artifact=targets[root["module"]]) if root["module"] in targets else root
                 for root in roots]
        local = copy(options)
        local.command = "audit"
        local.lean_path = os.pathsep.join(filter(None, [
            *(str(owned_path(project, root)) for root in plan["output_roots"]), options.lean_path]))
        from .audit import run_audit
        code, status = 1, "audit_failed"
        code = run_audit(project, output / "audit", local, roots=roots)
        status = "pass" if code == 0 else "audit_failed"
        changed = source_changes(project, plan["members"])
        if changed:
            code, status = 1, "source_changed"
        return code
    finally:
        children = []
        for directory in ("compile", "audit"):
            report = output / directory / "build.json"
            if report.exists():
                children.extend(json.loads(report.read_text())["children"])
        summary = dict(command="refresh", status=status, returncode=code,
                       elapsed_s=time.monotonic() - start, children=children,
                       caps_verified=bool(children) and all(c["argv_verified"] for c in children),
                       plan_sha256=plan_digest, modules=plan["modules"], audit_roots=roots,
                       source_changes=changed)
        (output / "build.json").write_text(json.dumps(summary, indent=2) + "\n")
