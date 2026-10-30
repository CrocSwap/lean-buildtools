"""Conservative source proposals, checked with the ordinary capped compiler."""
from __future__ import annotations

import copy
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile

from .runner import run_project, checked

IMPORT = re.compile(r"(?m)^(?P<visibility>(?:public\s+|private\s+|meta\s+)*)import\s+"
                    r"(?P<module>[\w.]+)[ \t]*(?P<comment>--[^\n]*)?$")


def clean(text):
    # Used for conservative refusal checks, never for finding Lean command boundaries.
    return re.sub(r"/\-.*?\-/|--[^\n]*|\"(?:\\.|[^\"\\])*\"", " ", text, flags=re.S)


def check_source(source, deny_sorry=False):
    if deny_sorry and re.search(r"\b(sorry|admit)\b", clean(source.read_text())):
        raise ValueError("refusing a compile-checked proposal for source containing sorry/admit")


def apply_proposal(project, source, expected: bytes, files: dict[Path, str]):
    """Stage complete files, guard against editor changes, publish the facade last."""
    if source.read_bytes() != expected:
        raise ValueError("source changed during validation; proposal was not applied")
    prepared = []
    published = []
    try:
        for relative, text in files.items():
            dest = project / relative
            if not dest.resolve().is_relative_to(project.resolve()):
                raise ValueError(f"proposal path escapes project: {relative}")
            if dest != source and dest.exists():
                raise ValueError(f"proposal destination already exists: {relative}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix=".lean-buildtools-", dir=dest.parent)
            temp = Path(name)
            prepared.append((dest, temp))
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            temp.chmod(stat.S_IMODE(source.stat().st_mode))
        if source.read_bytes() != expected:
            raise ValueError("source changed during validation; proposal was not applied")
        for dest, temp in sorted(prepared, key=lambda pair: pair[0] == source):
            if dest == source:
                if source.read_bytes() != expected:
                    raise ValueError("source changed during validation; proposal was not applied")
                os.replace(temp, dest)
            else:
                # Publish without overwriting a file created after the initial check.
                os.link(temp, dest)
                published.append(dest)
    except BaseException:
        for dest in published:
            dest.unlink(missing_ok=True)
        raise
    finally:
        for _, temp in prepared:
            temp.unlink(missing_ok=True)


def validate(project, source, staged, artifacts, report_dir, opts):
    relative = source.relative_to(project)
    if not artifacts.exists():
        # Lean chooses the first provider of a namespace, not the first exact
        # file. Seed a WHOLE private provider before adding split parts to it.
        namespace = relative.parts[0].removesuffix(".lean")
        roots = [Path(p) for p in (getattr(opts, "lean_path", "") or "").split(os.pathsep) if p]
        roots += [project / ".lake/build/lib/lean"]
        provider = next((p for p in roots if (p / namespace).exists() or
                         (p / (namespace + ".olean")).exists()), None)
        if provider:
            import sys
            artifacts.parent.mkdir(parents=True, exist_ok=True)
            flags = ["-cR"] if sys.platform == "darwin" else ["-a", "--reflink=auto"]
            checked(["cp", *flags, provider, artifacts])
        else:
            artifacts.mkdir(parents=True, exist_ok=True)
    dest = artifacts / relative.with_suffix(".olean")
    dest.parent.mkdir(parents=True, exist_ok=True)
    run_opts = copy.copy(opts)
    run_opts.search_path = artifacts
    run_opts.quiet = True
    return run_project(project, report_dir, run_opts,
                       ["env", str(report_dir / "toolchain/bin/lean"),
                        "-R", str(staged), "-o", str(dest), str(staged / relative)])


def save_proposal(output, data, original, proposed, source):
    data["source"] = str(source)
    data["source_sha256"] = hashlib.sha256(original.encode()).hexdigest()
    (output / "proposal.json").write_text(json.dumps(data, indent=2) + "\n")
    (output / "proposal.patch").write_text("".join(difflib.unified_diff(
        original.splitlines(keepends=True), proposed.splitlines(keepends=True),
        fromfile=str(source), tofile=str(source))))


def trim(project, source, output, opts):
    check_source(source, getattr(opts, "deny_sorry", False))
    original = source.read_bytes().decode("utf-8")
    imports = list(IMPORT.finditer(original))
    if not imports:
        raise ValueError("no simple one-module import lines found")
    output.mkdir(parents=True, exist_ok=False)
    staged = output / "sources"
    staged_source = staged / source.relative_to(project)
    staged_source.parent.mkdir(parents=True)
    staged_source.write_text(original)
    artifacts = output / "artifacts"
    if validate(project, source, staged, artifacts, output / "baseline", opts):
        raise ValueError("baseline does not compile under caps; no trimming attempted")
    current = original
    removed = []
    narrowed = []
    attempts = []
    # Each accepted deletion is retained for later trials: simultaneous removal is checked.
    for i, item in enumerate(imports):
        line = item.group(0)
        candidate = current.replace(line, "", 1)
        staged_source.write_text(candidate)
        code = validate(project, source, staged, artifacts, output / f"trial-{i}", opts)
        attempts.append(dict(module=item["module"], returncode=code))
        if code == 0:
            current = candidate
            removed.append(item["module"])
    # Try smaller transitive providers, ordered by their source import closure.
    # Every replacement is elaborated; syntax/tactic/instance dependencies are
    # therefore checked as well as names in theorem types.
    for i, item in enumerate(list(IMPORT.finditer(current))):
        for j, candidate_module in enumerate(narrower_modules(project, item["module"])[:opts.max_candidates]):
            replacement = item.group(0).replace(item["module"], candidate_module, 1)
            candidate = current.replace(item.group(0), replacement, 1)
            staged_source.write_text(candidate)
            trial = output / f"narrow-{i}-{j}"
            code = validate(project, source, staged, artifacts, trial, opts)
            attempts.append(dict(module=item["module"], replacement=candidate_module, returncode=code))
            if code == 0:
                current = candidate
                narrowed.append(dict(original=item["module"], replacement=candidate_module))
                break
    staged_source.write_text(current)
    # The final combination gets its own recorded result even if the last trial failed.
    final_code = validate(project, source, staged, artifacts, output / "final", opts)
    if final_code:
        raise ValueError("final import combination did not compile; no proposal applied")
    data = dict(kind="trim", compiles=True, removed=removed, narrowed=narrowed, trials=attempts,
                remaining=[m["module"] for m in IMPORT.finditer(current)],
                claim="compiler-tested greedy deletion of original import lines, in this order; "
                      "not a globally minimal import set or a proof of downstream compatibility")
    save_proposal(output, data, original, current, source)
    if opts.write:
        apply_proposal(project, source, original.encode("utf-8"), {source.relative_to(project): current})
    print(f"trim compiles: removed {', '.join(removed) or '(none)'}; narrowed {narrowed}; {output / 'proposal.json'}")
    return 0


def narrower_modules(project, module):
    roots = [project]
    for container in (project / "vendor", project / ".lake/packages"):
        if container.is_dir():
            roots += [p for p in container.iterdir() if p.is_dir()]
    graph = {}
    def walk(name, depth=0):
        if name in graph or len(graph) >= 512 or depth > 12:
            return
        source = next((p / Path(*name.split(".")).with_suffix(".lean") for p in roots
                       if (p / Path(*name.split(".")).with_suffix(".lean")).is_file()), None)
        graph[name] = []
        if source:
            graph[name] = [m["module"] for m in IMPORT.finditer(source.read_text())]
            for child in graph[name]:
                walk(child, depth + 1)
    walk(module)
    def closure(name, seen=None):
        seen = set() if seen is None else seen
        if name in seen:
            return seen
        seen.add(name)
        for child in graph.get(name, []):
            closure(child, seen)
        return seen
    namespace = module.split(".")[0]
    candidates = [name for name in graph if name != module and name.startswith(namespace + ".")]
    return sorted(candidates, key=lambda name: (len(name.split(".")), len(closure(name)), name))


def add_import(prefix: str, module: str) -> str:
    imports = list(IMPORT.finditer(prefix))
    modern = bool(re.match(r"\s*module\b", clean(prefix)))
    line = f"{'public ' if modern else ''}import {module}\n"
    if imports:
        pos = imports[-1].end()
        return prefix[:pos] + "\n" + line + prefix[pos:]
    if modern:
        match = re.search(r"\bmodule\b", prefix)
        return prefix[:match.end()] + "\n" + line + prefix[match.end():]
    return line + prefix


def split(project, source, output, opts):
    check_source(source, getattr(opts, "deny_sorry", False))
    raw = source.read_bytes()
    original = raw.decode("utf-8")
    code = clean(original)
    if re.search(r"\b(private|mutual|initialize|builtin_initialize)\b", code):
        raise ValueError("automatic split declines private/mutual/initializer declarations; split manually")
    data = json.loads(opts.profile.read_text())
    rel = str(source.relative_to(project))
    if data.get("source_sha256", {}).get(rel) != hashlib.sha256(raw).hexdigest():
        raise ValueError("profile source hash does not match target (rerun profile)")
    decls = sorted([r for r in data["declarations"] if Path(r["file"]) == Path(rel)], key=lambda r: r["byte_start"])
    if any(not r["complete"] for r in decls):
        raise ValueError("profile is incomplete; use a larger permitted cap or split manually")
    if not 2 <= opts.parts <= len(decls):
        raise ValueError(f"require 2 <= parts <= declaration count ({len(decls)})")
    # Groups are contiguous and dependency order is preserved. Weight uses measured time.
    total = sum(d["time_s"] for d in decls)
    cuts = [0]
    cumulative = 0
    for i, d in enumerate(decls[:-1], 1):
        cumulative += d["time_s"]
        if len(cuts) < opts.parts and cumulative >= total * len(cuts) / opts.parts:
            if len(decls) - i >= opts.parts - len(cuts):
                cuts.append(i)
    while len(cuts) < opts.parts:
        unused = [i for i in range(1, len(decls)) if i not in cuts]
        cuts.append(unused[len(unused) // 2])
        cuts.sort()
    cuts.append(len(decls))
    prefix = raw[:decls[0]["byte_start"]].decode()
    modern = bool(re.match(r"\s*module\b", clean(prefix)))
    suffix = raw[decls[-1]["byte_end"]:].decode()
    module = ".".join(source.relative_to(project).with_suffix("").parts)
    proposed_files = {}
    groups = []
    for part, (begin, end) in enumerate(zip(cuts, cuts[1:]), 1):
        name = module + f"Part{part}"
        path = Path(*name.split(".")).with_suffix(".lean")
        if (project / path).exists():
            raise ValueError(f"split destination already exists: {path}")
        context = prefix
        if part > 1:
            context = add_import(context, module + f"Part{part-1}")
            # Recreate intervening namespace/variable/options context, preserving its order.
            for earlier in range(begin):
                start = decls[earlier]["byte_end"]
                stop = decls[earlier + 1]["byte_start"]
                context += raw[start:stop].decode()
        stop = decls[end]["byte_start"] if end < len(decls) else decls[-1]["byte_end"]
        body = raw[decls[begin]["byte_start"]:stop].decode()
        proposed_files[path] = context + body + suffix
        groups.append(dict(module=name, declarations=[d["name"] for d in decls[begin:end]],
                           profile_time_s=sum(d["time_s"] for d in decls[begin:end])))
    # Keep the original import path as a facade, re-exporting the final sequential part.
    header_end = max([m.end() for m in IMPORT.finditer(prefix)], default=0)
    if header_end:
        facade = prefix[:header_end] + "\n"
        facade += f"{'public ' if modern else ''}import {module}Part{opts.parts}\n"
    else:
        facade = ("module\n\npublic " if re.match(r"\s*module\b", clean(prefix)) else "") + f"import {module}Part{opts.parts}\n"
    proposed_files[source.relative_to(project)] = facade
    output.mkdir(parents=True, exist_ok=False)
    staged, artifacts = output / "sources", output / "artifacts"
    for relative, text in proposed_files.items():
        p = staged / relative
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    for i, relative in enumerate(proposed_files):
        if validate(project, project / relative, staged, artifacts, output / f"compile-{i}", opts):
            raise ValueError(f"split failed compilation at {relative}; staged proposal retained, no source changed")
    names = []
    for decl in decls:
        match = re.fullmatch(r"(?:theorem|lemma|def|abbrev|opaque|structure|class|inductive) ([\w.']+)", decl["name"])
        if match:
            names.append(".".join(filter(None, [decl.get("namespace", ""), match[1]])))
    check_rel = Path("BuildToolsSplitConsumer.lean")
    if (project / check_rel).exists():
        raise ValueError("consumer probe name already exists")
    (staged / check_rel).write_text(("module\n\npublic " if modern else "") +
                                   f"import {module}\n" + "".join(f"#check {n}\n" for n in names))
    if validate(project, project / check_rel, staged, artifacts, output / "consumer", opts):
        raise ValueError("split facade consumer failed; no source changed")
    result = dict(kind="split", compiles=True, groups=groups, checked_names=names,
                  files=[str(p) for p in proposed_files],
                  claim="sequential modules and original import facade compile; listed public names remain accessible; "
                        "this does not split a single expensive declaration")
    save_proposal(output, result, original, facade, source)
    if opts.write:
        apply_proposal(project, source, raw, proposed_files)
    print(f"split compiles: {len(groups)} parts and facade consumer; {output / 'proposal.json'}")
    return 0
