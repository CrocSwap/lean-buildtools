"""Config-driven SSH/rsync backend; transport injection keeps tests local."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import time
import uuid

from .remote_config import Remote, relative_path

BOOTSTRAP = """import json,os,signal,sys
def interrupted(signum,frame): raise KeyboardInterrupt
signal.signal(signal.SIGHUP,interrupted)
signal.signal(signal.SIGTERM,interrupted)
p=json.load(sys.stdin)
sys.path.insert(0,os.path.expanduser(p['tool']))
from lean_buildtools.remote_worker import control,run_job
if p['action']=='run': sys.exit(run_job(p))
print(json.dumps(control(p)))
"""

EXCLUDES = ["/.git/", ".lean-buildtools/", "__pycache__/", "*.pyc", ".venv/", ".build/",
            "/dist/", "/release-artifacts/", "/build/", "/.artifacts/", ".env",
            ".lake/build/", "**/.lake/build/", ".lake/config/", "**/.lake/config/", "lakefile.olean*"]


class SSHTransport:
    def __init__(self, remote: Remote):
        self.remote = remote

    def command(self, script):
        return ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", self.remote.ssh,
                shlex.join([self.remote.python, "-c", script])]

    def python(self, script, payload):
        proc = subprocess.run(self.command(script), input=json.dumps(payload), capture_output=True,
                              text=True, timeout=60)
        if proc.returncode:
            raise RuntimeError(f"remote command failed ({proc.returncode}): {proc.stderr.strip()}")
        try:
            return json.loads(proc.stdout)
        except ValueError as error:
            raise RuntimeError("remote command did not return JSON; check that SSH startup files are quiet") from error

    def initialize_tool(self, package: Path):
        fingerprint = hashlib.sha256()
        for p in sorted(package.rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts and p.suffix in (".py", ".lean", ".c"):
                fingerprint.update(str(p.relative_to(package)).encode())
                fingerprint.update(p.read_bytes())
        info = self.python("""import json,os,sys
from pathlib import Path
p=json.load(sys.stdin)
if sys.version_info<(3,11) or sys.platform not in ('linux','darwin'):
 raise RuntimeError('remote requires Python 3.11+ on Linux or macOS')
root=Path(p['root']).expanduser().resolve()
tool=root/'.lean-buildtools/tools'/p['fingerprint']
tool.mkdir(parents=True,exist_ok=True)
print(json.dumps({'root':str(root),'tool':str(tool)}))
""", {"root": self.remote.root, "fingerprint": fingerprint.hexdigest()[:20]})
        self.push(package, info["tool"] + "/lean_buildtools", ["__pycache__/", "*.pyc"])
        return info

    def push(self, source, destination, excludes=()):
        cmd = ["rsync", "-a", "-e", "ssh -o BatchMode=yes -o ConnectTimeout=15"]
        for pattern in excludes:
            cmd += ["--exclude", pattern]
        cmd += [str(source).rstrip("/") + "/", self.remote.ssh + ":" + shlex.quote(destination.rstrip("/") + "/")]
        subprocess.run(cmd, check=True)

    def pull(self, source, destination, excludes=()):
        Path(destination).mkdir(parents=True, exist_ok=True)
        cmd = ["rsync", "-a", "-e", "ssh -o BatchMode=yes -o ConnectTimeout=15"]
        for pattern in excludes:
            cmd += ["--exclude", pattern]
        cmd += [self.remote.ssh + ":" + shlex.quote(source.rstrip("/") + "/"), str(destination) + "/"]
        subprocess.run(cmd, check=True)

    def pull_files(self, source, names, destination):
        if not names:
            return
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8") as listing:
            listing.write("".join(relative_path(name) + "\n" for name in names))
            listing.flush()
            subprocess.run(["rsync", "-a", "--files-from=" + listing.name,
                            "-e", "ssh -o BatchMode=yes -o ConnectTimeout=15",
                            self.remote.ssh + ":" + shlex.quote(source.rstrip("/") + "/"),
                            str(destination) + "/"], check=True)

    def control(self, payload, action):
        return self.python(BOOTSTRAP, {**payload, "action": action})

    def run(self, payload):
        proc = subprocess.Popen(self.command(BOOTSTRAP), stdin=subprocess.PIPE, text=True,
                                start_new_session=True)
        try:
            proc.communicate(json.dumps({**payload, "action": "run"}))
            if proc.returncode == 255:
                self.control(payload, "cancel")
            return proc.returncode
        except BaseException:
            try:
                self.control(payload, "cancel")
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
            raise


@contextmanager
def lease(remote, payload):
    values = {"remote": remote.name, "namespace": remote.namespace, "job": payload["job"],
              "memory": payload["memory"], "threads": payload["threads"]}
    def expand(command):
        try:
            return [arg.format_map(values) for arg in command]
        except (KeyError, ValueError, AttributeError) as error:
            raise ValueError(f"invalid lease hook placeholder: {error}") from error
    if remote.acquire:
        result = subprocess.run(expand(remote.acquire), timeout=30)
        if result.returncode:
            raise RuntimeError(f"remote lease/semaphore hook refused the job ({result.returncode})")
    try:
        yield
    finally:
        if remote.release:
            result = subprocess.run(expand(remote.release), timeout=30)
            if result.returncode:
                print(f"lean-buildtools: lease release hook failed ({result.returncode})", file=sys.stderr)


def input_paths(values):
    result = {}
    for value in values:
        source, separator, name = value.rpartition("=")
        if not separator or not re.fullmatch(r"[A-Za-z0-9_.-]+", name) or name in (".", ".."):
            raise ValueError("remote inputs use LOCAL_DIRECTORY=NAME")
        path = Path(source).expanduser().resolve()
        if not path.is_dir() or name in result:
            raise ValueError(f"missing or duplicate remote input: {value}")
        result[name] = path
    return result


def translate_paths(value, project, remote_project, inputs, job):
    result = []
    for entry in value.split(os.pathsep) if value else []:
        local = Path(entry).expanduser().resolve()
        if local.is_relative_to(project):
            result.append(str(Path(remote_project) / local.relative_to(project)))
            continue
        for name, root in inputs.items():
            if local.is_relative_to(root):
                result.append(str(Path(job) / "inputs" / name / local.relative_to(root)))
                break
        else:
            raise ValueError(f"precompiled path must be under the project or a named --remote-input: {entry}")
    return result


def run_remote(project, output, opts, remote: Remote, *, transport=None):
    if opts.command not in ("build", "profile", "profile-batch", "audit", "refresh"):
        raise ValueError("remote execution supports build, profile, profile-batch, audit and refresh")
    if remote.max_memory is not None and opts.memory > remote.max_memory:
        raise ValueError(f"requested memory exceeds remote max_memory ({remote.max_memory} MiB)")
    inputs = input_paths(opts.remote_input)
    for pattern in opts.fetch:
        relative_path(pattern)
    output.mkdir(parents=True, exist_ok=False)
    start = time.monotonic()
    transport = transport or SSHTransport(remote)
    package = Path(__file__).resolve().parent
    info = transport.initialize_tool(package)
    workspace = re.sub(r"[^A-Za-z0-9_.-]", "_", project.name)
    workspace += "-" + hashlib.sha256(str(project).encode()).hexdigest()[:8]
    payload = dict(root=info["root"], tool=info["tool"], namespace=remote.namespace,
                   workspace=workspace, job=uuid.uuid4().hex, max_jobs=remote.max_jobs,
                   memory=opts.memory, threads=opts.threads, path=remote.path, env=remote.env,
                   seeds=remote.seeds, prepare=remote.prepare, fetch=opts.fetch)
    state = transport.control(payload, "init")
    args = [opts.command, opts.target, "--memory", str(opts.memory), "--threads", str(opts.threads),
            "--stop-fraction", str(opts.stop_fraction), "--horizon", str(opts.horizon),
            "--timeout", str(opts.timeout), "--interval", str(opts.interval)]
    for field in ("deny_sorry", "no_mathlib_build", "no_cache"):
        if getattr(opts, field, False):
            args.append("--" + field.replace("_", "-"))
    args += ["--lean-arg=" + arg for arg in opts.lean_arg]
    if opts.command == "profile-batch":
        args += ["--emit-root", opts.emit_root, "--source-root", opts.source_root]
    if opts.command == "audit" and opts.keep_graph:
        args.append("--keep-graph")
    lean_path = translate_paths(opts.lean_path, project, state["project"], inputs, state["job"])
    lean_path += [p.replace("{job}", state["job"]).replace("{project}", state["project"])
                  for p in remote.lean_path]
    if lean_path:
        args += ["--lean-path", ":".join(lean_path)]
    payload["args"] = args
    payload["options"] = {key: getattr(opts, key) for key in
                           ("memory", "threads", "stop_fraction", "horizon", "timeout", "interval",
                            "deny_sorry", "no_mathlib_build", "no_cache", "lean_arg")}
    excludes = [p for p in EXCLUDES if not opts.sync_build or ".lake/build/" not in p]
    excludes += remote.excludes
    transport.push(project, state["project"], excludes)
    for name, directory in inputs.items():
        transport.push(directory, str(Path(state["job"]) / "inputs" / name), ["/.git/"])
    synced = time.monotonic()
    code = 1
    try:
        with lease(remote, payload):
            code = transport.run(payload)
    finally:
        collected = transport.control(payload, "collect")
        if collected["report_exists"]:
            transport.pull(state["report"], output, ["toolchain/", "active-*"])
        for directory in collected.get("preparation_reports", []):
            transport.pull(directory, output / Path(directory).name, ["toolchain/", "active-*"])
        transport.pull_files(state["project"], collected["artifacts"], output / "artifacts")
        if code == 0 and collected.get("missing_artifacts"):
            code = 1
            print("lean-buildtools: requested remote outputs missing: " +
                  ", ".join(collected["missing_artifacts"]), file=sys.stderr)
        summary = dict(remote=remote.name, ssh=remote.ssh, workspace=state["project"],
                       sync_s=synced - start, wall_s=time.monotonic() - start,
                       returncode=code, **collected)
        (output / "remote.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(f"lean-buildtools: remote report {output / 'remote.json'}", file=sys.stderr)
    return code if code >= 0 else 128 - code
