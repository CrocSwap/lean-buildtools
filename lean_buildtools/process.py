"""POSIX process inspection and bounded resource supervision."""
from __future__ import annotations

import ctypes
import errno
import json
import os
from pathlib import Path
import signal
import struct
import subprocess
import sys
import time
from collections import deque


def argv_of(pid: int) -> list[str]:
    """Read exec arguments, preserving spaces (ps command text does not)."""
    if sys.platform == "darwin":
        libc = ctypes.CDLL(None, use_errno=True)
        mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2
        buf = ctypes.create_string_buffer(1024 * 1024)
        size = ctypes.c_size_t(len(buf))
        if libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0):
            raise OSError(ctypes.get_errno(), "cannot inspect child argv")
        raw = buf.raw[:size.value]
        argc = struct.unpack_from("i", raw)[0]
        offset = raw.index(b"\0", 4)
        while offset < len(raw) and raw[offset] == 0:
            offset += 1
        return [os.fsdecode(s) for s in raw[offset:].split(b"\0")[:argc]]
    return [os.fsdecode(s) for s in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0") if s]


def rss_of(pid: int) -> int:
    if sys.platform != "darwin":
        return int(Path(f"/proc/{pid}/statm").read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True)
    return int(out.stdout.strip() or "0") * 1024


def capped_args(args: list[str], memory: int, *, deny_sorry: bool = False, threads: int = 1) -> list[str]:
    result = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("-j", "-M", "--threads", "--memory"):
            i += 2
            continue
        if (arg.startswith(("--threads=", "--memory=")) or
                (arg[:2] in ("-j", "-M") and len(arg) > 2)):
            i += 1
            continue
        result.append(arg)
        i += 1
    return [f"-j{threads}", f"-M{memory}", "-DElab.async=false",
            *(["-E", "hasSorry"] if deny_sorry else []), *result]


def caps_match(argv: list[str], memory: int, thread_count: int = 1) -> bool:
    # Exact comparison with normalized arguments also rules out later overrides.
    threads, mem = [], []
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg in ("-j", "--threads", "-M", "--memory"):
            if i + 1 >= len(argv):
                return False
            (threads if arg in ("-j", "--threads") else mem).append(argv[i + 1])
            i += 2
            continue
        for flag, values in (("-j", threads), ("-M", mem),
                             ("--threads=", threads), ("--memory=", mem)):
            if arg.startswith(flag):
                values.append(arg[len(flag):])
                break
        i += 1
    return threads == [str(thread_count)] and mem == [str(memory)]


def trend_reason(samples, cap: int, fraction: float, horizon: float, *,
                 elaboration_started: bool = True) -> str | None:
    if not samples:
        return None
    now, rss = samples[-1]
    if rss >= cap * fraction:
        return "rss threshold"
    # Require sustained growth over at least a second, and a substantial RSS baseline.
    if elaboration_started and len(samples) >= 4 and now - samples[0][0] >= 1 and rss >= cap * 0.5:
        slope = (rss - samples[0][1]) / (now - samples[0][0])
        if slope > 0 and rss + slope * horizon >= cap * fraction:
            return "rss growth projects threshold within horizon"
    return None


def append_json(path: Path, value: dict) -> None:
    with path.open("a") as f:
        f.write(json.dumps(value, ensure_ascii=False) + "\n")


def _signal_group(proc: subprocess.Popen, sig: int) -> bool:
    try:
        os.killpg(proc.pid, sig)
    except OSError as error:
        if error.errno in (errno.ESRCH, errno.EPERM) and proc.poll() is not None:
            return False
        raise
    return True


def terminate(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        if not _signal_group(proc, signal.SIGTERM):
            proc.wait()
            return
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            if not _signal_group(proc, signal.SIGKILL):
                proc.wait()
                return
            proc.wait()
        # Native evaluation subprocesses belong to the same owned process group.
        _signal_group(proc, signal.SIGKILL)


def supervise(cmd: list[str], *, events: Path, result: Path, memory: int,
              fraction: float, horizon: float, timeout: float, interval: float, threads: int = 1) -> int:
    """Own one direct compiler child; never inspect/kill unrelated processes."""
    proc = subprocess.Popen(cmd, start_new_session=True)
    previous_handlers = {}
    cancelled = []
    def interrupted(signum, frame):
        cancelled.append(signum)
    for sig in (signal.SIGTERM, signal.SIGINT):
        previous_handlers[sig] = signal.signal(sig, interrupted)
    start = time.monotonic()
    verified = False
    observed = []
    samples = deque()
    peak = 0
    active = None
    reason = None
    event_pos = 0
    event_buffer = b""
    history = []
    elaboration_started = False
    sampled = events.with_name(events.stem.replace("events-", "samples-") + ".jsonl")
    def read_events():
        nonlocal event_pos, event_buffer, peak, elaboration_started, active
        try:
            with events.open("rb") as f:
                f.seek(event_pos)
                event_buffer += f.read()
                event_pos = f.tell()
        except FileNotFoundError:
            return
        lines = event_buffer.split(b"\n")
        event_buffer = lines.pop()
        for line in lines:
            event = json.loads(line)
            if event.get("pid") == proc.pid:
                history.append(event)
                peak = max(peak, int(event.get("rss_bytes", 0)))
                if (not elaboration_started and event["event"] == "start" and
                        event.get("syntax", "").endswith((".declaration", ".example", ".mutual"))):
                    samples.clear()
                    elaboration_started = True
                active = event if event["event"] == "start" else None
    try:
        while proc.poll() is None:
            if not verified:
                try:
                    observed = argv_of(proc.pid)
                    verified = (os.path.realpath(observed[0]) == os.path.realpath(cmd[0])
                                and caps_match(observed, memory, threads))
                except (OSError, IndexError, ValueError):
                    pass
                if not verified and time.monotonic() - start > 1:
                    reason = "could not verify real compiler argv"
            read_events()
            try:
                rss = rss_of(proc.pid)
            except (OSError, ValueError, ProcessLookupError):
                rss = 0
            now = time.monotonic()
            append_json(sampled, dict(pid=proc.pid, time_ns=time.monotonic_ns(), rss_bytes=rss))
            peak = max(peak, rss)
            samples.append((now, rss))
            while samples and now - samples[0][0] > 5:
                samples.popleft()
            reason = reason or trend_reason(samples, memory * 1024**2, fraction, horizon,
                                            elaboration_started=elaboration_started)
            if timeout and now - start >= timeout:
                reason = "wall time limit"
            if cancelled:
                reason = f"cancelled by signal {cancelled[0]}"
            if reason:
                terminate(proc)
                break
            time.sleep(interval if verified else min(interval, 0.005))
        code = proc.wait()
        # A short final declaration may complete between RSS polls. Its flushed
        # boundary events still contribute to the peak and completion state.
        stopped_at = active
        read_events()
        if reason:
            active = stopped_at
    finally:
        terminate(proc)
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
    if not verified:
        reason = reason or "compiler exited before argv verification"
    report = dict(pid=proc.pid, argv=observed, argv_verified=verified, peak_rss_bytes=peak,
                  elapsed_s=time.monotonic() - start, returncode=code,
                  stop_reason=reason, active_command=active, module_events=len(history))
    append_json(result, report)
    if reason:
        where = f"{active['file']}:{active['line']}" if active else next(
            (x for x in cmd if x.endswith(".lean")), "<during imports or compiler startup>")
        declaration = "<import/startup>"
        if active:
            try:
                raw = Path(active["file"]).read_bytes()[active["byte_start"]:active["byte_end"]]
                from .profile import command_name
                declaration = command_name(raw.decode(), active["syntax"])[0]
            except OSError:
                declaration = active["syntax"]
        print(f"lean-buildtools: STOP {where}: {declaration}; {reason}; "
              f"peak {peak / 1024**2:.1f} MiB", file=sys.stderr)
        return 125
    return code if code >= 0 else 128 - code
