from __future__ import annotations

import argparse
import math
from pathlib import Path
import signal
import sys
import time

from . import profile
from .runner import run_project, snapshot


def parser():
    p = argparse.ArgumentParser(prog="lean-buildtools")
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("build", "profile", "trim", "split"):
        c = sub.add_parser(name)
        c.add_argument("target", help="Lake target (build) or project-relative .lean source")
        c.add_argument("--project", type=Path, default=Path.cwd())
        c.add_argument("--output", type=Path, help="new report directory")
        c.add_argument("--memory", type=int, default=1400, help="Lean -M cap in MiB (default 1400)")
        c.add_argument("--stop-fraction", type=float, default=0.85)
        c.add_argument("--horizon", type=float, default=3.0, help="RSS trend projection seconds")
        c.add_argument("--timeout", type=float, default=300, help="whole command wall limit seconds; 0 disables")
        c.add_argument("--interval", type=float, default=0.1)
        c.add_argument("--lean-path", type=str, help="additional precompiled search path (owned coherent copy)")
        c.add_argument("--deny-sorry", action="store_true", help="treat Lean's sorry warning as an error")
        c.add_argument("--no-mathlib-build", action="store_true", help="refuse Mathlib source compilation")
        c.add_argument("--no-cache", action="store_true", help="disable Lake's build-artifact downloads")
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
        finite = all(math.isfinite(getattr(opts, name)) for name in
                     ("stop_fraction", "interval", "horizon", "timeout"))
        if not finite or opts.memory < 64 or not 0 < opts.stop_fraction < 1 or opts.interval <= 0 or opts.horizon < 0 or opts.timeout < 0:
            p.error("require memory >= 64, 0 < stop-fraction < 1, interval > 0, horizon >= 0, timeout >= 0")
        if opts.command == "trim" and opts.max_candidates < 0:
            p.error("max-candidates must be nonnegative")
        output = (opts.output or project / ".lean-buildtools" / f"{opts.command}-{time.time_ns()}").resolve()
        if opts.command == "build":
            if opts.target.startswith("-"):
                p.error("target must not be an option")
            return run_project(project, output, opts, ["build", opts.target])
        source = (project / opts.target).resolve()
        if not source.is_relative_to(project) or source.suffix != ".lean" or not source.is_file():
            p.error("source must be an existing .lean file inside the project")
        if opts.command == "profile":
            args = ["env", str(output / "toolchain/bin/lean"), str(source.relative_to(project))]
            code = run_project(project, output, opts, args)
            profile.report(output, project)
            return code
        from .proposals import split, trim
        return (split if opts.command == "split" else trim)(project, source, output, opts)
    except (OSError, ValueError, RuntimeError) as e:
        print(f"lean-buildtools: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("lean-buildtools: cancelled", file=sys.stderr)
        return 130
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    sys.exit(main())
