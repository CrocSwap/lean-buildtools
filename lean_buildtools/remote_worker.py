"""Remote-side job execution, also exercised by the local fake transport tests."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from types import SimpleNamespace

from .remote_config import relative_path


@contextmanager
def slot(root: Path, maximum: int):
    directory = root / ".lean-buildtools/slots"
    directory.mkdir(parents=True, exist_ok=True)
    handle = None
    for index in range(maximum):
        candidate = (directory / str(index)).open("a+")
        try:
            fcntl.flock(candidate, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            candidate.close()
        else:
            handle = candidate
            break
    if handle is None:
        raise RuntimeError("remote concurrency limit reached; retry when a slot is free")
    try:
        yield
    finally:
        handle.close()


def paths(payload):
    root = Path(payload["root"]).expanduser().resolve()
    workspace = root / payload["namespace"] / payload["workspace"]
    job = workspace / "jobs" / payload["job"]
    if not job.resolve().is_relative_to(root):
        raise ValueError("job escapes configured workspace root")
    return root, job, job / "project", job / "report"


def environment(payload):
    env = os.environ.copy()
    env.update(payload.get("env", {}))
    env["PATH"] = os.pathsep.join([*(str(Path(p).expanduser()) for p in payload.get("path", [])), env["PATH"]])
    env["PYTHONPATH"] = str(Path(payload["tool"]).expanduser())
    env.pop("LEAN_BUILDTOOLS_REMOTE", None)
    return env


def reject_external_links(directory, boundary):
    for here, dirs, files in os.walk(directory, followlinks=False):
        for name in dirs + files:
            path = Path(here) / name
            if path.is_symlink() and not path.resolve().is_relative_to(boundary):
                raise ValueError(f"synced symlink escapes private workspace: {path}")
        if "lake-manifest.json" in files:
            manifest = Path(here) / "lake-manifest.json"
            for package in json.loads(manifest.read_text()).get("packages", []):
                if package.get("type") == "path" and package.get("dir"):
                    if not (Path(here) / package["dir"]).resolve().is_relative_to(boundary):
                        raise ValueError(f"path dependency escapes private workspace: {manifest}")


def run_child(command, project, env, job, report, stdin=None):
    proc = subprocess.Popen(command, cwd=project, env=env, start_new_session=True,
                            stdin=subprocess.PIPE if stdin else None, text=True)
    (job / "active.json").write_text(json.dumps({"pid": proc.pid, "report": str(report)}))
    try:
        proc.communicate(stdin)
        return proc.returncode
    finally:
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
        (job / "active.json").unlink(missing_ok=True)


def seed_inputs(payload, job):
    for seed in payload.get("seeds", []):
        source = Path(seed["source"]).expanduser().resolve()
        destination = job / relative_path(seed["destination"])
        if not destination.resolve().is_relative_to(job):
            raise ValueError("seed destination escapes workspace")
        if destination.exists():
            raise ValueError(f"seed destination already exists: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Independent COW copies avoid modifying an immutable producer via a hard link.
        flags = ["-cR"] if sys.platform == "darwin" else ["-a", "--reflink=auto"]
        subprocess.run(["cp", *flags, str(source), str(destination)], check=True)


def run_job(payload):
    root, job, project, report = paths(payload)
    env = environment(payload)
    os.environ.update(env)
    result = {"returncode": 1, "job": str(job)}
    start = time.monotonic()
    try:
        with slot(root, payload["max_jobs"]):
            seed_inputs(payload, job)
            reject_external_links(project, job)
            reject_external_links(job / "inputs", job)
            for index, command in enumerate(payload.get("prepare", [])):
                preparation = job / f"prepare-{index}"
                data = dict(command=command, project=str(project), report=str(preparation),
                            options=payload["options"])
                code = run_child([sys.executable, "-m", "lean_buildtools.remote_worker", str(preparation)],
                                 project, env, job, preparation, json.dumps(data))
                if code:
                    raise RuntimeError(f"remote preparation failed with exit {code}")
            cmd = [sys.executable, "-m", "lean_buildtools", *payload["args"],
                   "--project", str(project), "--output", str(report)]
            if "--lean-path" in cmd:
                index = cmd.index("--lean-path") + 1
                cmd[index] = os.pathsep.join(str(Path(p).expanduser()) for p in cmd[index].split(os.pathsep))
            result["returncode"] = run_child(cmd, project, env, job, report)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        result["error"] = str(error)
        print(f"lean-buildtools remote: {error}", file=sys.stderr)
    finally:
        result["wall_s"] = time.monotonic() - start
        (job / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return result["returncode"]


def control(payload):
    root, job, project, report = paths(payload)
    action = payload["action"]
    if action == "init":
        job.mkdir(parents=True, exist_ok=False)
        project.mkdir()
        (job / "inputs").mkdir()
        return {"root": str(root), "job": str(job), "project": str(project), "report": str(report),
                "python": sys.version, "platform": sys.platform}
    if action == "cancel":
        active = job / "active.json"
        if not active.exists():
            return {"cancelled": False}
        state = json.loads(active.read_text())
        pid = state["pid"]
        try:
            if sys.platform == "linux":
                argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
                matches = os.fsencode(state["report"]) in argv
            else:
                text = subprocess.check_output(["ps", "-ww", "-o", "command=", "-p", str(pid)], text=True)
                matches = state["report"] in text
            if matches and os.getpgid(pid) == pid:
                os.killpg(pid, signal.SIGTERM)
                deadline = time.monotonic() + 12
                while active.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                return {"cancelled": True}
        except (OSError, subprocess.SubprocessError):
            pass
        return {"cancelled": False}
    if action == "collect":
        result = json.loads((job / "result.json").read_text()) if (job / "result.json").exists() else {}
        artifacts = []
        missing = []
        for pattern in payload.get("fetch", []):
            relative_path(pattern)
            found = False
            for path in project.glob(pattern):
                if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(project):
                    artifacts.append(str(path.relative_to(project)))
                    found = True
            if not found:
                missing.append(pattern)
        return {"result": result, "artifacts": sorted(set(artifacts)), "report_exists": report.is_dir(),
                "missing_artifacts": missing,
                "preparation_reports": [str(p) for p in sorted(job.glob("prepare-*")) if p.is_dir()]}
    raise ValueError(f"unknown remote action: {action}")


if __name__ == "__main__":
    def interrupted(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGHUP, interrupted)
    data = json.load(sys.stdin)
    command = data["command"]
    if Path(command[0]).name == "lake":
        from .runner import run_project
        # Resolve lake from the configured PATH, as the main build does.
        code = run_project(Path(data["project"]), Path(data["report"]),
                           SimpleNamespace(**data["options"]), command[1:])
    else:
        proc = subprocess.Popen(command, cwd=data["project"], start_new_session=True)
        try:
            code = proc.wait(timeout=900)
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
    sys.exit(code)
