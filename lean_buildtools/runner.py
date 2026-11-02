from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import shlex
import signal
import subprocess
import sys
import time

from .jsonl import read_rows

PACKAGE_PARENT = Path(__file__).resolve().parent.parent


def cache_directory() -> Path:
    override = os.environ.get("LEAN_BUILDTOOLS_CACHE")
    if override:
        return Path(override).expanduser().resolve()
    base = (Path.home() / "Library/Caches" if sys.platform == "darwin" else
            Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))))
    return base / "lean-buildtools"


def checked(cmd, *, cwd=None, env=None) -> str:
    proc = subprocess.run([str(a) for a in cmd], cwd=cwd, env=env,
                          text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode:
        raise RuntimeError(f"command failed ({proc.returncode}): {' '.join(map(str, cmd))}\n"
                           f"{proc.stdout}{proc.stderr}")
    return proc.stdout.strip()


def toolchain(project: Path) -> Path:
    if sys.platform not in ("darwin", "linux"):
        raise RuntimeError("supported platforms are macOS and Linux")
    sysroot = Path(checked(["lean", "--print-prefix"], cwd=project))
    version = checked([sysroot / "bin/lean", "--version"], cwd=project)
    if not re.search(r"\bversion 4\.35\.0(?:,|\))", version):
        raise RuntimeError(f"Lean 4.35.0 is required; found {version}")
    return sysroot


def plugin_for(project: Path) -> tuple[Path, str]:
    sysroot = toolchain(project)
    import fcntl
    sources = Path(__file__).resolve().parent / "lean"
    fingerprint = hashlib.sha256((str(sysroot) + checked([sysroot / "bin/lean", "--githash"])).encode())
    for name in ("BuildMonitor.lean", "rss.c"):
        fingerprint.update((sources / name).read_bytes())
    out = cache_directory() / fingerprint.hexdigest()[:16]
    suffix = ".dylib" if sys.platform == "darwin" else ".so"
    plugin = out / ("BuildMonitor" + suffix)
    out.mkdir(parents=True, exist_ok=True)
    with (out / "bootstrap.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not plugin.exists():
            checked([sysroot / "bin/lean", "-j1", "-M1400", "-R", sources,
                     "-o", out / "BuildMonitor.olean", "-c", out / "BuildMonitor.c",
                     sources / "BuildMonitor.lean"], cwd=project)
            checked(["cc", "-fPIC", "-DLEAN_EXPORTING", "-I", sysroot / "include", "-c",
                     sources / "rss.c", "-o", out / "rss.o"])
            args = [sysroot / "bin/leanc", "-shared", "-DLEAN_EXPORTING", "-fPIC"]
            if sys.platform == "darwin":
                args += ["-mmacosx-version-min=14.0"]
            temporary = out / ("pending" + suffix)
            checked([*args, "-o", temporary, out / "BuildMonitor.c", out / "rss.o"])
            temporary.replace(plugin)
    return sysroot, str(plugin)


def prepare(project: Path, output: Path, options) -> tuple[Path, dict]:
    sysroot, plugin = plugin_for(project)
    output.mkdir(parents=True, exist_ok=False)
    shimroot = output / "toolchain"
    (shimroot / "bin").mkdir(parents=True)
    for item in sysroot.iterdir():
        if item.name != "bin":
            (shimroot / item.name).symlink_to(item, target_is_directory=item.is_dir())
    for item in (sysroot / "bin").iterdir():
        if item.name != "lean":
            (shimroot / "bin" / item.name).symlink_to(item)
    shim = shimroot / "bin/lean"
    entry = (f"import sys;sys.path.insert(0, {str(PACKAGE_PARENT)!r});"
             "from lean_buildtools.shim import main;main()")
    # A shell trampoline also works when the virtualenv's Python path has spaces.
    shim.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + " -c " +
                    shlex.quote(entry) + ' "$@"\n')
    shim.chmod(0o755)
    config = dict(lean=str(sysroot / "bin/lean"), plugin=plugin, run=str(output),
                  memory=options.memory, fraction=options.stop_fraction,
                  horizon=options.horizon, timeout=options.timeout, interval=options.interval)
    config["threads"] = getattr(options, "threads", 1)
    config["extra_args"] = getattr(options, "lean_arg", [])
    config["prepend_path"] = [str(p) for p in (getattr(options, "search_path", None),
                                              getattr(options, "lean_path", None)) if p]
    config["deny_sorry"] = getattr(options, "deny_sorry", False)
    config["no_mathlib_build"] = getattr(options, "no_mathlib_build", False)
    config_file = output / "config.json"
    config_file.write_text(json.dumps(config, indent=2) + "\n")
    env = os.environ.copy()
    env.update(LAKE_OVERRIDE_LEAN="true", LEAN_SYSROOT=str(shimroot), LEAN=str(shim),
               LEAN_NUM_THREADS=str(config["threads"]), LEAN_IMPORT_WORKERS="1",
               LEAN_BUILDTOOLS_CONFIG=str(config_file))
    extras = [str(p) for p in (getattr(options, "search_path", None), getattr(options, "lean_path", None)) if p]
    if extras:
        env["LEAN_PATH"] = os.pathsep.join([*extras, env.get("LEAN_PATH", "")])
    return shimroot, env


def run_project(project: Path, output: Path, options, args: list[str]) -> int:
    _, env = prepare(project, output, options)
    cmd = ["lake", *(["--no-cache"] if getattr(options, "no_cache", False) else []), *args]
    start = time.monotonic()
    timed_out = False
    log = (output / "compiler.log").open("w") if getattr(options, "quiet", False) else None
    proc = subprocess.Popen(cmd, cwd=project, env=env, start_new_session=True,
                            stdout=log, stderr=subprocess.STDOUT if log else None)
    try:
        while proc.poll() is None:
            if options.timeout and time.monotonic() - start > options.timeout:
                timed_out = True
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                break
            time.sleep(0.1)
        code = proc.wait()
    except BaseException:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
        raise
    finally:
        if log:
            log.close()
    # Lake may exit before its cancelled compiler wrapper has written its audit.
    # Wait for owned wrappers to finish their bounded process-group cleanup.
    deadline = time.monotonic() + 3
    while list(output.glob("active-*")) and time.monotonic() < deadline:
        time.sleep(0.05)
    incomplete = len(list(output.glob("active-*")))
    children_file = output / "children.jsonl"
    children = read_rows(children_file, missing_ok=True)
    observed = bool(children or list(output.glob("events-*.jsonl")))
    summary = dict(command=cmd, project=str(project), returncode=code,
                   elapsed_s=time.monotonic() - start, children=children,
                   timed_out=timed_out,
                   incomplete_children=incomplete,
                   caps_verified=bool(children) and not incomplete and all(c["argv_verified"] for c in children),
                   cached_only=not observed and code == 0)
    (output / "build.json").write_text(json.dumps(summary, indent=2) + "\n")
    status = (f"{len(children)} compiler children audited" if children else
              "cached; no compiler observed" if not observed and code == 0 else "no completed compiler audit")
    print(f"lean-buildtools: {status}; report {output / 'build.json'}",
          file=sys.stderr)
    return code if code >= 0 else 128 - code


def snapshot(source: Path, dest: Path) -> None:
    """Create a private COW copy. Never hard-link writable build artifacts."""
    if dest.exists():
        raise ValueError(f"destination already exists: {dest}")
    if source == dest or source in dest.parents:
        raise ValueError("snapshot must be outside its source tree")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.mkdir()
    # Lake's active caches live in .lake/; omit root build archives and reports.
    excluded = {".git", "build", ".artifacts", ".scratch", ".lean-buildtools", "__pycache__"}
    for item in source.iterdir():
        if item.name in excluded:
            continue
        if sys.platform == "darwin":
            cmd = ["cp", "-cR", str(item), str(dest / item.name)]
        else:
            cmd = ["cp", "-a", "--reflink=auto", str(item), str(dest / item.name)]
        checked(cmd)
    # A symlink into an owner's build tree would violate isolation. Refuse it.
    escaped = []
    for root, dirs, files in os.walk(dest, followlinks=False):
        for name in dirs + files:
            p = Path(root) / name
            if p.is_symlink() and not p.resolve().is_relative_to(dest):
                escaped.append(str(p.relative_to(dest)))
        if "lake-manifest.json" in files:
            manifest = Path(root) / "lake-manifest.json"
            data = json.loads(manifest.read_text())
            for pkg in data.get("packages", []):
                if pkg.get("type") == "path" and pkg.get("dir"):
                    dependency = (Path(root) / pkg["dir"]).resolve()
                    if not dependency.is_relative_to(dest):
                        escaped.append(str(manifest.relative_to(dest)) + ": " + pkg["dir"])
    if escaped:
        raise ValueError("snapshot has external symlinks/path dependencies; copy a containing workspace before building: " + ", ".join(escaped[:20]))
    gitdir = dest / ".git"
    if gitdir.is_dir():
        shutil.rmtree(gitdir)
    elif gitdir.exists():
        gitdir.unlink()
    checked(["git", "init", "-q", dest])
    (dest / ".lean-buildtools-snapshot.json").write_text(json.dumps({"source": str(source)}) + "\n")
