"""Disk-backed declaration graphs; cached axiom lists are comparison data only."""
from __future__ import annotations

from collections import defaultdict, deque
import json
from pathlib import Path
import sqlite3

STANDARD_AXIOMS = frozenset(("propext", "Classical.choice", "Quot.sound"))


class ProofGraph:
    def __init__(self, database: Path):
        self.db = sqlite3.connect(database)
        self.db.executescript("""
            CREATE TABLE modules(name TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE declarations(name TEXT, module TEXT, data TEXT NOT NULL,
                                      PRIMARY KEY(name, module));
        """)

    def close(self):
        self.db.close()

    def load(self, path: Path):
        complete = None
        counts = defaultdict(int)
        with path.open() as stream, self.db:
            for line in stream:
                if complete is not None:
                    raise ValueError("graph has data after completion marker")
                record = json.loads(line)
                event = record.get("event")
                if event == "module":
                    self.db.execute("INSERT INTO modules VALUES (?,?)",
                                    (record["module"], json.dumps(record)))
                elif event == "declaration":
                    self.db.execute("INSERT INTO declarations VALUES (?,?,?)",
                                    (record["name"], record["module"], json.dumps(record)))
                    counts[record["module"]] += 1
                elif event == "complete":
                    complete = record
                else:
                    raise ValueError("unknown graph record")
        modules = self.modules()
        if complete is None or complete["modules"] != len(modules):
            raise ValueError("incomplete module graph")
        if set(counts) - modules.keys():
            raise ValueError("declaration has no owning module")
        for name, module in modules.items():
            if counts[name] != module["declaration_count"]:
                raise ValueError("incomplete declarations for " + name)
            if set(module["imports"]) - modules.keys():
                raise ValueError("incomplete imports for " + name)

    def modules(self):
        return {name: json.loads(data) for name, data in
                self.db.execute("SELECT name,data FROM modules ORDER BY name")}

    def candidates(self, name):
        return [json.loads(row[0]) for row in self.db.execute(
            "SELECT data FROM declarations WHERE name=? ORDER BY module", (name,))]

    def audit(self, roots, allowed=STANDARD_AXIOMS, *, dependency_output=None):
        """Recompute reachable axiom closure, including cycles, without cache pruning."""
        pending = deque(root["name"] for root in roots)
        records, missing, ambiguous = {}, set(), {}
        while pending:
            name = pending.popleft()
            if name in records or name in missing:
                continue
            choices = self.candidates(name)
            if not choices:
                missing.add(name)
                continue
            # These are full-private records. Conflicting representations from
            # distinct modules require review, rather than silently choosing one.
            first = choices[0]
            signature = lambda c: (c["kind"], tuple(sorted(c["dependencies"])),
                                   c["unsafe"], c["partial"])
            if any(signature(c) != signature(first) for c in choices[1:]):
                ambiguous[name] = sorted(c["module"] for c in choices)
            dependencies = set().union(*(set(c["dependencies"]) for c in choices))
            records[name] = dict(first, dependencies=sorted(dependencies), candidates=choices)
            pending.extend(sorted(dependencies))

        reverse = defaultdict(set)
        for name, record in records.items():
            for dependency in record["dependencies"]:
                reverse[dependency].add(name)
        axioms = {name: set() for name in records}
        propagation = deque()
        for name, record in records.items():
            if any(c["kind"] == "axiom" for c in record["candidates"]):
                axioms[name].add(name)
                propagation.append(name)
        # Monotone propagation handles mutual definitions/inductives correctly;
        # a recursion sentinel with memoization would lose axioms inside cycles.
        while propagation:
            name = propagation.popleft()
            for caller in reverse[name]:
                extra = axioms[name] - axioms[caller]
                if extra:
                    axioms[caller].update(extra)
                    propagation.append(caller)

        unknown = set(missing) | set(ambiguous)
        pending = deque(unknown)
        while pending:
            for caller in reverse[pending.popleft()]:
                if caller not in unknown:
                    unknown.add(caller)
                    pending.append(caller)
        mismatches = {}
        for name, record in records.items():
            if name in unknown:
                continue
            comparisons = []
            for candidate in record["candidates"]:
                cached = candidate["cached_axioms"]
                if cached is not None and set(cached) != axioms[name]:
                    comparisons.append(dict(module=candidate["module"], cached=sorted(cached),
                                            actual=sorted(axioms[name]),
                                            cached_only=sorted(set(cached) - axioms[name]),
                                            actual_only=sorted(axioms[name] - set(cached))))
            if comparisons:
                mismatches[name] = comparisons

        if dependency_output is not None:
            names = sorted(records.keys() | missing)
            identifiers = {name: index for index, name in enumerate(names)}
            modules = sorted(self.modules())
            module_ids = {name: index for index, name in enumerate(modules)}
            nodes = []
            for name in names:
                record = records.get(name)
                owners = sorted({module_ids[c["module"]] for c in record["candidates"]}) if record else []
                dependencies = [identifiers[n] for n in record["dependencies"]] if record else []
                nodes.append([name, owners, dependencies])
            dependency_output.write_text(json.dumps(dict(
                schema_version=1, roots=roots, modules=modules, nodes=nodes,
                complete=not (missing or ambiguous),
                uncertain_names=sorted(missing | ambiguous.keys())), separators=(",", ":")) + "\n")

        def paths_from(root):
            parent = {root: None}
            todo = deque([root])
            while todo:
                current = todo.popleft()
                for dependency in records.get(current, {}).get("dependencies", []):
                    if dependency not in parent:
                        parent[dependency] = current
                        todo.append(dependency)
            def path_to(name):
                result = []
                while name is not None:
                    result.append(dict(name=name, module=records.get(name, {}).get("module")))
                    name = parent[name]
                return list(reversed(result))
            return parent, path_to

        results = []
        for root in roots:
            name = root["name"]
            reachable, path_to = paths_from(name)
            unexpected = sorted(axioms.get(name, set()) - allowed)
            errors = [dict(kind="missing_declaration", path=path_to(n))
                      for n in sorted(missing & reachable.keys())]
            errors.extend(dict(kind="ambiguous_declaration", modules=ambiguous[n], path=path_to(n))
                          for n in sorted(ambiguous.keys() & reachable.keys()))
            for n in sorted(reachable.keys() & records.keys()):
                if records[n]["unsafe"] or records[n]["partial"]:
                    errors.append(dict(kind="unsafe_or_partial", path=path_to(n)))
            if name in records and root["module"] not in {c["module"] for c in records[name]["candidates"]}:
                # Roots may be imported/re-exported from the requested module;
                # module membership is checked through the actual import closure below.
                modules = self.modules()
                seen, todo = set(), [root["module"]]
                while todo:
                    mod = todo.pop()
                    if mod in seen:
                        continue
                    seen.add(mod)
                    todo.extend(modules.get(mod, {}).get("imports", []))
                if records[name]["module"] not in seen:
                    errors.append(dict(kind="root_not_in_requested_imports", module=root["module"]))
            stale = [dict(name=n, path=path_to(n), comparisons=mismatches[n],
                          blocking=any((set(c["cached_only"]) | set(c["actual_only"])) - allowed
                                       for c in mismatches[n]))
                     for n in sorted(reachable.keys() & mismatches.keys())]
            cached_unexpected = [
                dict(name=axiom, summary_declaration=item["name"],
                     path=[*item["path"], dict(name=axiom, module=None, via="cached_summary")])
                for item in stale for comparison in item["comparisons"]
                for axiom in comparison["cached_only"] if axiom not in allowed]
            results.append(dict(root=root, axioms=sorted(axioms.get(name, set())),
                                axioms_complete=name not in unknown,
                                unexpected_axioms=[dict(name=n, path=path_to(n)) for n in unexpected],
                                cached_unexpected_axioms=cached_unexpected,
                                summary_mismatches=stale, errors=errors,
                                status="pass" if not (unexpected or errors or any(s["blocking"] for s in stale)) else "fail",
                                reachable_declarations=len(reachable)))
        return dict(schema_version=1, roots=results, allowlist=sorted(allowed),
                    status="pass" if all(r["status"] == "pass" for r in results) else "fail",
                    method="actual full-private declaration types and bodies; cached summaries compared only",
                    reachable_declarations=len(records), missing_declarations=sorted(missing))
