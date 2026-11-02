from __future__ import annotations

import argparse
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from . import profile
from .runner import run_project, snapshot


def parser():
    p = argparse.ArgumentParser(prog="lean-buildtools")
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("build", "profile", "profile-batch", "audit", "refresh-plan", "refresh", "trim", "split"):
        c = sub.add_parser(name)
        c.add_argument("target", help={"profile-batch": "project-relative JSON source list",
                                       "audit": "project-relative roots JSON manifest",
                                       "refresh-plan": "audit report directory",
                                       "refresh": "project-relative refresh plan JSON"}.get(
                                           name, "Lake target (build) or project-relative .lean source"))
        c.add_argument("--project", type=Path, default=Path.cwd())
        c.add_argument("--output", type=Path, help="new report directory")
        c.add_argument("--memory", type=int, help="Lean -M cap in MiB (local default 1400)")
        c.add_argument("--threads", type=int, help="compiler thread cap (local default 1)")
        c.add_argument("--lean-arg", action="append", default=[], metavar="ARG",
                       help="additional compiler argument; use --lean-arg=-Doption=value")
        c.add_argument("--stop-fraction", type=float, default=0.85)
        c.add_argument("--horizon", type=float, default=3.0, help="RSS trend projection seconds")
        c.add_argument("--timeout", type=float, default=300, help="whole command wall limit seconds; 0 disables")
        c.add_argument("--interval", type=float, default=0.1)
        c.add_argument("--lean-path", type=str, help="additional precompiled search path (owned coherent copy)")
        c.add_argument("--deny-sorry", action="store_true", help="treat Lean's sorry warning as an error")
        c.add_argument("--no-mathlib-build", action="store_true", help="refuse Mathlib source compilation")
        c.add_argument("--no-cache", action="store_true", help="disable Lake's build-artifact downloads")
        if name == "profile-batch":
            c.add_argument("--emit-root", default=".lake/build/lib/lean",
                           help="project-relative complete compiled prefix; manifest is a JSON source list")
            c.add_argument("--source-root", default=".", help="project-relative source root for module names")
        if name == "audit":
            c.add_argument("--keep-graph", action="store_true",
                           help="retain the full declaration graph and SQLite index for diagnosis")
        if name == "refresh-plan":
            c.add_argument("--changed-module", action="append", required=True)
            c.add_argument("--changed-declaration", action="append", default=[])
            c.add_argument("--include-changed", action="store_true")
            c.add_argument("--stale-only", action="store_true",
                           help="after proof-only leaf repair, select only remaining unexpected cached summaries")
            c.add_argument("--source-root", default=".")
            c.add_argument("--source-map", type=Path)
            c.add_argument("--output-map", type=Path)
            c.add_argument("--output-root", action="append", default=[],
                           help="ordered project-relative output search roots (repeat for multiple prefixes)")
            c.add_argument("--emit-root", default=".lake/build/lib/lean")
        if name in ("build", "profile", "profile-batch", "audit", "refresh"):
            c.add_argument("--remote", default=os.environ.get("LEAN_BUILDTOOLS_REMOTE"), help="configured SSH remote name")
            c.add_argument("--remote-config", type=Path, help="additional remote configuration file")
            c.add_argument("--remote-namespace", help="private namespace under the configured workspace root")
            c.add_argument("--remote-input", action="append", default=[], metavar="DIRECTORY=NAME",
                           help="sync an additional precompiled input tree")
            c.add_argument("--sync-build", action="store_true", help="also sync .lake build outputs")
            c.add_argument("--fetch", action="append", default=[], metavar="PATTERN",
                           help="project-relative output glob to collect under report/artifacts/")
        if name in ("trim", "split"):
            c.add_argument("--write", action="store_true", help="apply compile-checked proposal in this project")
        if name == "split":
            c.add_argument("--profile", type=Path, required=True, help="profile.json from this source")
            c.add_argument("--parts", type=int, default=2)
        if name == "trim":
            c.add_argument("--max-candidates", type=int, default=8,
                           help="maximum transitive narrower-module trials per surviving import")
    c = sub.add_parser("snapshot")
    c.add_argument("source", type=Path)
    c.add_argument("destination", type=Path)
    c = sub.add_parser("report")
    c.add_argument("run", type=Path)
    c.add_argument("--project", type=Path, default=Path.cwd())
    c.add_argument("--sort", choices=["time_s", "heartbeats_raw", "rss_delta_bytes"], default="time_s")
    return p


def main(argv=None):
    p = parser()
    opts = p.parse_args(argv)
    def terminate(signum, frame):
        raise KeyboardInterrupt
    previous = signal.signal(signal.SIGTERM, terminate)
    try:
        if opts.command == "snapshot":
            snapshot(opts.source.resolve(), opts.destination.resolve())
            print(opts.destination.resolve())
            return 0
        project = opts.project.resolve()
        if opts.command == "report":
            profile.report(opts.run.resolve(), project, sort=opts.sort)
            return 0
        remote = None
        if getattr(opts, "remote", None):
            from .remote_config import load_remote
            remote = load_remote(opts.remote, project, opts.remote_config)
            opts.deny_sorry = opts.deny_sorry or remote.deny_sorry
            opts.no_mathlib_build = opts.no_mathlib_build or remote.no_mathlib_build
            if opts.remote_namespace:
                import re
                if not re.fullmatch(r"[A-Za-z0-9_.-]+", opts.remote_namespace) or opts.remote_namespace in (".", ".."):
                    p.error("remote namespace must be a simple directory name")
                remote.namespace = opts.remote_namespace
        opts.memory = opts.memory if opts.memory is not None else remote.memory if remote else 1400
        opts.threads = opts.threads if opts.threads is not None else remote.threads if remote else 1
        finite = all(math.isfinite(getattr(opts, name)) for name in
                     ("stop_fraction", "interval", "horizon", "timeout"))
        if not finite or opts.memory < 64 or opts.threads < 1 or not 0 < opts.stop_fraction < 1 or opts.interval <= 0 or opts.horizon < 0 or opts.timeout < 0:
            p.error("require memory >= 64, 0 < stop-fraction < 1, interval > 0, horizon >= 0, timeout >= 0")
        if opts.command == "trim" and opts.max_candidates < 0:
            p.error("max-candidates must be nonnegative")
        output = (opts.output or project / ".lean-buildtools" / f"{opts.command}-{time.time_ns()}").resolve()
        if opts.command == "refresh-plan":
            from .refresh import plan_refresh
            audit_report = (project / opts.target).resolve()
            for field in ("source_map", "output_map"):
                if getattr(opts, field):
                    setattr(opts, field, (project / getattr(opts, field)).resolve())
            return plan_refresh(project, audit_report, output, opts)
        if opts.command == "refresh":
            from .refresh import read_plan, run_refresh
            read_plan(project, opts.target)
            if remote:
                from .remote import run_remote
                return run_remote(project, output, opts, remote)
            return run_refresh(project, output, opts)
        if opts.command == "build":
            if opts.target.startswith("-"):
                p.error("target must not be an option")
            if remote:
                from .remote import run_remote
                return run_remote(project, output, opts, remote)
            return run_project(project, output, opts, ["build", opts.target])
        if opts.command == "profile-batch":
            from .batch import load_manifest, run_batch
            load_manifest(project, opts.target, opts.emit_root, opts.lean_arg)
            if remote:
                from .remote import run_remote
                return run_remote(project, output, opts, remote)
            return run_batch(project, output, opts)
        if opts.command == "audit":
            from .audit import read_roots, run_audit
            read_roots(project, opts.target)
            if remote:
                from .remote import run_remote
                return run_remote(project, output, opts, remote)
            return run_audit(project, output, opts)
        source = (project / opts.target).resolve()
        if not source.is_relative_to(project) or source.suffix != ".lean" or not source.is_file():
            p.error("source must be an existing .lean file inside the project")
        if remote:
            from .remote import run_remote
            return run_remote(project, output, opts, remote)
        if opts.command == "profile":
            args = ["env", str(output / "toolchain/bin/lean"), str(source.relative_to(project))]
            code = run_project(project, output, opts, args)
            profile.report(output, project)
            return code
        from .proposals import split, trim
        return (split if opts.command == "split" else trim)(project, source, output, opts)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as e:
        print(f"lean-buildtools: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("lean-buildtools: cancelled", file=sys.stderr)
        return 130
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    sys.exit(main())
