"""Fail-fast, dependency-ordered direct compilation in one owned workspace."""
from __future__ import annotations

from copy import copy
import json
import os
from pathlib import Path
import time

from . import profile
from .remote_config import relative_path
from .runner import run_project


def owned_path(project, value):
    relative_path(value)
    path = (project / value).resolve()
    if not path.is_relative_to(project):
        raise ValueError("batch paths must remain inside the project")
    return path


def validate_lean_args(lean_args):
    for arg in lean_args:
        if arg.startswith(("-o", "-i", "-c", "-b", "-R", "--o", "--i", "--c", "--b", "--root", "--setup")) or arg in (
                "--run", "--server", "--worker", "--stdin"):
            raise ValueError("batch owns compiler outputs, source root and mode; unsupported argument: " + arg)


def load_manifest(project, target, emit_root, lean_args):
    manifest = owned_path(project, target)
    sources = json.loads(manifest.read_text())
    if not isinstance(sources, list) or not sources:
        raise ValueError("batch manifest must be a nonempty JSON list of source paths")
    paths = []
    for source in sources:
        if not isinstance(source, str):
            raise ValueError("batch sources must be strings")
        path = owned_path(project, source)
        if path.suffix != ".lean" or not path.is_file():
            raise ValueError("batch sources must be existing project-relative .lean files")
        if path in paths:
            raise ValueError("duplicate batch source: " + source)
        paths.append(path)
    root = owned_path(project, emit_root)
    if root == project or root.is_file():
        raise ValueError("emit-root must be a compiled-output directory inside the project")
    # The batch owns output names; arbitrary output flags would defeat cleanup.
    validate_lean_args(lean_args)
    return paths, root


def products(root, relative_source):
    base = root / relative_source.with_suffix("")
    return [Path(str(base) + suffix) for suffix in (
        ".olean", ".olean.private", ".olean.server", ".ilean", ".ir", ".ir.sig",
        ".olean.hash", ".olean.private.hash", ".olean.server.hash",
        ".ilean.hash", ".ir.hash", ".ir.sig.hash", ".trace")]


def clear_products(paths):
    for path in paths:
        path.unlink(missing_ok=True)


def run_batch(project, output, options):
    sources, root = load_manifest(project, options.target, options.emit_root, options.lean_arg)
    source_root = owned_path(project, getattr(options, "source_root", "."))
    members = [dict(source=str(source.relative_to(project)),
                    source_root=str(source_root.relative_to(project)),
                    emit_root=str(root.relative_to(project))) for source in sources]
    return run_members(project, output, options, members, [str(root.relative_to(project))])


def run_members(project, output, options, planned, output_roots, *, command="profile-batch"):
    """Shared serial compiler lane for batches and reviewed refresh plans."""
    sources, source_roots, outputs = [], [], []
    validate_lean_args(options.lean_arg)
    for member in planned:
        source = owned_path(project, member["source"])
        source_root = owned_path(project, member["source_root"])
        root = owned_path(project, member["emit_root"])
        if not source.is_file() or source.suffix != ".lean" or not source.is_relative_to(source_root):
            raise ValueError("member source must be a .lean file under its source root")
        if root == project or root.is_file():
            raise ValueError("member output root must be a compiled directory")
        relative = source.relative_to(source_root)
        if member.get("module") and ".".join(relative.with_suffix("").parts) != member["module"]:
            raise ValueError("member source root does not match its declared module")
        sources.append(source)
        source_roots.append(source_root)
        outputs.append(products(root, relative))
    if not planned or len({paths[0] for paths in outputs}) != len(outputs):
        raise ValueError("members must be nonempty with unique output modules")
    roots = [owned_path(project, root) for root in output_roots]
    # Resolve all destinations before deleting anything, including symlinked parents.
    for paths in outputs:
        for path in paths:
            if not path.resolve().is_relative_to(project):
                raise ValueError("batch output escapes private project")
    output.mkdir(parents=True, exist_ok=False)
    for paths in outputs:
        clear_products(paths)
        paths[0].parent.mkdir(parents=True, exist_ok=True)
    local = copy(options)
    local.lean_path = os.pathsep.join(filter(None, [*(str(root) for root in roots), options.lean_path]))
    start = time.monotonic()
    members, children = [], []
    timed_out, incomplete = False, 0
    code = 1
    try:
        for index, (source, source_root, paths) in enumerate(zip(sources, source_roots, outputs)):
            member = output / "members" / f"{index:04d}"
            success = False
            code = 1
            source_before = source.read_bytes()
            try:
                code = run_project(project, member, local, [
                    "env", str(member / "toolchain/bin/lean"),
                    str(source), "-R", str(source_root), "-o", str(paths[0]), "-i", str(paths[3])])
                summary = json.loads((member / "build.json").read_text())
                (member / "source.lean").write_bytes(source_before)
                timed_out = timed_out or summary.get("timed_out", False)
                incomplete += summary.get("incomplete_children", 0)
                children.extend(summary["children"])
                profile.report(member, project)
                # Legacy sources do not emit every module-form companion.
                missing = [str(path.relative_to(project)) for path in (paths[0], paths[3])
                           if not path.is_file()]
                if code == 0 and (missing or not summary["caps_verified"] or source.read_bytes() != source_before):
                    code = 1
                members.append(dict(source=str(source.relative_to(project)),
                                    report=str(member.relative_to(output)), returncode=code,
                                    missing_products=missing, caps_verified=summary["caps_verified"]))
                success = code == 0
            finally:
                if not success:
                    clear_products(paths)
            if code:
                break
        return code
    finally:
        summary = dict(command=command, project=str(project),
                       returncode=code, elapsed_s=time.monotonic() - start,
                       planned_sources=[str(s.relative_to(project)) for s in sources],
                       members=members, children=children,
                       cached_only=False, timed_out=timed_out, incomplete_children=incomplete,
                       caps_verified=bool(children) and all(m["caps_verified"] for m in members),
                       completed=len(members) == len(sources) and code == 0)
        (output / "build.json").write_text(json.dumps(summary, indent=2) + "\n")
