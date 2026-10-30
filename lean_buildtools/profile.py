"""Combine in-compiler boundaries and OS RSS samples into a ranked report."""
from __future__ import annotations

import json
import hashlib
from pathlib import Path
import re

from .jsonl import read_rows

DECL = re.compile(r"\b(theorem|lemma|def|abbrev|opaque|axiom|instance|structure|class|inductive|example)\s+(\S+)")


def command_name(text: str, kind: str) -> tuple[str, bool]:
    # Syntax positions come from Lean; this regex only gives the report a readable label.
    text = re.sub(r"/\-.*?\-/|--[^\n]*", " ", text, flags=re.S)
    match = DECL.search(text) if kind.endswith((".declaration", ".example", ".mutual")) else None
    if match:
        return f"{match[1]} {match[2]}", True
    return (text.strip().splitlines() or [kind])[0][:120], False


def report(run: Path, project: Path, *, sort="time_s") -> dict:
    rows = []
    for event_file in sorted(run.glob("events-*.jsonl")):
        pending = {}
        samples_file = event_file.with_name(event_file.name.replace("events-", "samples-"))
        samples = read_rows(samples_file, missing_ok=True)
        for event in read_rows(event_file):
            key = (event["pid"], event["byte_start"], event["byte_end"])
            if event["event"] == "start":
                pending[key] = event
                continue
            start = pending.pop(key, None)
            if start is None:
                continue
            rows.append(_row(project, start, event, samples))
        for start in pending.values():
            rows.append(_row(project, start, None, samples))
    declarations = sorted([r for r in rows if r["is_declaration"]],
                          key=lambda r: r.get(sort) or 0, reverse=True)
    data = dict(schema_version=1, commands=rows, declarations=declarations,
                metrics="raw heartbeat allocations (1000 raw = 1 maxHeartbeats unit); "
                        "wall elapsed and resident bytes at command boundaries; "
                        "RSS delta is retained process memory, not total allocations")
    data["source_sha256"] = {}
    for row in rows:
        source = project / row["file"]
        if source.exists():
            data["source_sha256"][row["file"]] = hashlib.sha256(source.read_bytes()).hexdigest()
    (run / "profile.json").write_text(json.dumps(data, indent=2) + "\n")
    lines = ["declaration\tline\ttime_s\traw_heartbeats\trss_delta_MiB\trss_end_MiB\tcomplete"]
    for r in declarations:
        lines.append(f"{r['name']}\t{r['line']}\t{r['time_s'] or 0:.6f}\t{r['heartbeats_raw']}\t"
                     f"{(r['rss_delta_bytes'] or 0) / 1024**2:.3f}\t"
                     f"{(r['rss_end_bytes'] or 0) / 1024**2:.3f}\t{r['complete']}")
    (run / "profile.tsv").write_text("\n".join(lines) + "\n")
    print("\n".join(lines[:21]))
    return data


def _row(project, start, end, samples):
    source = project / start["file"]
    try:
        text = source.read_bytes()[start["byte_start"]:start["byte_end"]].decode()
    except OSError:
        text = start["syntax"]
    name, is_decl = command_name(text, start["syntax"])
    inside = [s["rss_bytes"] for s in samples if s["pid"] == start["pid"] and
              start["time_ns"] <= s["time_ns"] and (end is None or s["time_ns"] <= end["time_ns"])]
    peak = max([int(start["rss_bytes"]), int(end["rss_bytes"]) if end else 0, *inside])
    return dict(file=start["file"], module=start["module"], line=start["line"],
                byte_start=start["byte_start"], byte_end=start["byte_end"],
                syntax=start["syntax"], name=name, is_declaration=is_decl,
                namespace=start.get("namespace", ""),
                time_s=(end["time_ns"] - start["time_ns"]) / 1e9 if end else None,
                heartbeats_raw=end["heartbeats"] - start["heartbeats"] if end else None,
                rss_start_bytes=int(start["rss_bytes"]), rss_end_bytes=int(end["rss_bytes"]) if end else None,
                peak_rss_bytes=peak,
                rss_delta_bytes=int(end["rss_bytes"]) - int(start["rss_bytes"]) if end else None,
                complete=end is not None)
