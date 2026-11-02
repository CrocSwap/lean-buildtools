"""User-owned SSH remote definitions. No transport credentials belong here."""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path, PurePosixPath
import re
import tomllib


def relative_path(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("expected a relative path string")
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or "\x00" in value or "\n" in value:
        raise ValueError(f"expected a relative path without '..': {value!r}")
    return str(path)


def commands(value, name):
    if not isinstance(value, list) or any(not isinstance(c, list) or not c or
            any(not isinstance(s, str) or not s for s in c) for c in value):
        raise ValueError(f"{name} must be a list of nonempty command argument arrays")
    return value


@dataclass
class Remote:
    name: str
    ssh: str
    root: str
    namespace: str = "default"
    python: str = "python3"
    path: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    excludes: list[str] = field(default_factory=list)
    memory: int = 1400
    threads: int = 1
    max_memory: int | None = None
    max_jobs: int = 1
    deny_sorry: bool = False
    no_mathlib_build: bool = False
    prepare: list[list[str]] = field(default_factory=list)
    lean_path: list[str] = field(default_factory=list)
    seeds: list[dict] = field(default_factory=list)
    acquire: list[str] = field(default_factory=list)
    release: list[str] = field(default_factory=list)


def load_remote(name: str, project: Path, config: Path | None = None) -> Remote:
    config_root = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    files = [config_root / "lean-buildtools/remotes.toml", project / "lean-buildtools.toml",
             project / "lean-buildtools.local.toml"]
    explicit = config or os.environ.get("LEAN_BUILDTOOLS_REMOTES")
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not path.is_file():
            raise ValueError(f"remote configuration not found: {path}")
        files.append(path)
    values = {}
    for path in files:
        if path.is_file():
            tables = tomllib.loads(path.read_text()).get("remotes", {})
            if not isinstance(tables, dict):
                raise ValueError("remotes must be a table")
            data = tables.get(name, {})
            if not isinstance(data, dict):
                raise ValueError(f"remotes.{name} must be a table")
            for key, value in data.items():
                if key in ("env", "lease") and isinstance(value, dict):
                    if not isinstance(values.get(key, {}), dict):
                        raise ValueError(f"remote {key} must be a table")
                    values[key] = {**values.get(key, {}), **value}
                else:
                    values[key] = value
    if not values:
        raise ValueError(f"unknown remote {name!r}; define it in lean-buildtools.toml or a user remotes.toml")
    lease = values.pop("lease", {})
    if not isinstance(lease, dict) or set(lease) - {"acquire", "release"}:
        raise ValueError("lease accepts acquire and release command arrays")
    values["namespace"] = os.environ.get("LEAN_BUILDTOOLS_NAMESPACE", values.get("namespace", "default"))
    allowed = set(Remote.__dataclass_fields__) - {"name", "acquire", "release"}
    if set(values) - allowed:
        raise ValueError(f"unknown remote settings: {', '.join(sorted(set(values) - allowed))}")
    try:
        remote = Remote(name=name, acquire=lease.get("acquire", []), release=lease.get("release", []), **values)
    except TypeError as error:
        raise ValueError(f"remote {name!r} requires ssh and root settings") from error
    if not isinstance(remote.ssh, str) or not re.fullmatch(r"[A-Za-z0-9_.@:\[\]-]+", remote.ssh) or remote.ssh.startswith("-"):
        raise ValueError("ssh must be a host alias or user@host, not shell options")
    if not isinstance(remote.root, str) or not (remote.root.startswith("/") or remote.root.startswith("~/")) or any(c in remote.root for c in "\x00\n"):
        raise ValueError("remote root must be absolute or start with ~/")
    if not isinstance(remote.namespace, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", remote.namespace) or remote.namespace in (".", ".."):
        raise ValueError("remote namespace must be a simple directory name")
    if not isinstance(remote.python, str) or not remote.python or remote.python.startswith("-"):
        raise ValueError("remote python must name an executable")
    for key in ("memory", "threads", "max_jobs"):
        if type(getattr(remote, key)) is not int or getattr(remote, key) < 1:
            raise ValueError(f"remote {key} must be a positive integer")
    for key in ("deny_sorry", "no_mathlib_build"):
        if not isinstance(getattr(remote, key), bool):
            raise ValueError(f"remote {key} must be a boolean")
    if remote.max_memory is not None and (type(remote.max_memory) is not int or remote.max_memory < remote.memory):
        raise ValueError("remote max_memory must be at least its default memory")
    for key in ("path", "excludes", "lean_path", "acquire", "release"):
        if not isinstance(getattr(remote, key), list) or any(not isinstance(s, str) for s in getattr(remote, key)):
            raise ValueError(f"remote {key} must be a string array")
    if bool(remote.acquire) != bool(remote.release):
        raise ValueError("lease acquire and release must be configured together")
    if not isinstance(remote.env, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                               for k, v in remote.env.items()):
        raise ValueError("remote env must contain string values")
    commands(remote.prepare, "prepare")
    if not isinstance(remote.seeds, list):
        raise ValueError("seeds must be an array of tables")
    for seed in remote.seeds:
        if not isinstance(seed, dict) or set(seed) != {"source", "destination"}:
            raise ValueError("each seed needs source and destination")
        if not isinstance(seed["source"], str) or not seed["source"]:
            raise ValueError("seed source must be a nonempty path")
        dest = relative_path(seed["destination"])
        if PurePosixPath(dest).parts[0] not in ("project", "inputs"):
            raise ValueError("seed destination must be under project/ or inputs/")
    return remote
