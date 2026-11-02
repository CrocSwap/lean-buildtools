"""Lake's LEAN replacement; called once for each compiler invocation."""
import fcntl
import json
import os
from pathlib import Path
import sys

from lean_buildtools.process import capped_args, supervise


def main():
    config = json.loads(Path(os.environ["LEAN_BUILDTOOLS_CONFIG"]).read_text())
    if config.get("prepend_path"):
        os.environ["LEAN_PATH"] = os.pathsep.join([*config["prepend_path"], os.environ.get("LEAN_PATH", "")])
    args = sys.argv[1:]
    real = config["lean"]
    if not any(a.endswith(".lean") for a in args):
        os.execv(real, [real, *args])
    args = [*config.get("extra_args", []), *args]
    if any(a in args for a in ("--run", "--server", "--worker", "--stdin")):
        sys.exit("lean-buildtools: unsupported compiler mode")
    source = next(a for a in args if a.endswith(".lean"))
    path = Path(source).resolve()
    if config.get("no_mathlib_build") and any(p in path.parts for p in ("Mathlib", "Mathlib.lean")):
        sys.exit("lean-buildtools: refusing to rebuild Mathlib; obtain prebuilt cache first")
    run = Path(config["run"])
    # Lake has parallel jobs; a cross-process lock serializes actual compiler children.
    with (run / "compiler.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        # A stopped child cancels subsequent queued jobs as well.
        if (run / "stopped").exists():
            sys.exit(125)
        event_file = run / f"events-{os.getpid()}.jsonl"
        os.environ["LEAN_BUILDTOOLS_EVENTS"] = str(event_file)
        cmd = [real, *capped_args(args, config["memory"], deny_sorry=config.get("deny_sorry", False),
                                threads=config.get("threads", 1)),
               f"--plugin={config['plugin']}"]
        active_file = run / f"active-{os.getpid()}"
        active_file.write_text(str(os.getpid()))
        try:
            code = supervise(cmd, events=event_file, result=run / "children.jsonl",
                             threads=config.get("threads", 1),
                             **{k: config[k] for k in ("memory", "fraction", "horizon", "timeout", "interval")})
        finally:
            active_file.unlink(missing_ok=True)
        if code == 125:
            (run / "stopped").touch()
        sys.exit(code)


if __name__ == "__main__":
    main()
