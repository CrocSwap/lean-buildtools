"""Regressions for startup prediction and process-exit races, without heavy builds."""
import errno
import json
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, call, patch

from lean_buildtools.process import append_json, supervise, terminate, trend_reason


class SupervisorRegressions(unittest.TestCase):
    def test_final_boundary_events_are_drained_after_child_exit(self):
        proc = Mock(pid=12345)
        proc.poll.side_effect = [None, 0, 0]
        proc.wait.return_value = 0
        cmd = [sys.executable, "-j1", "-M1400", "Probe.lean"]
        with tempfile.TemporaryDirectory() as directory:
            events = Path(directory) / "events-1.jsonl"
            result = Path(directory) / "children.jsonl"
            event = dict(pid=proc.pid, event="start", rss_bytes=100, file="Probe.lean", line=1,
                         byte_start=0, byte_end=0, syntax="Lean.Parser.Command.declaration")
            append_json(events, event)
            def finish(delay):
                append_json(events, {**event, "event": "end", "rss_bytes": 200})
            with patch("lean_buildtools.process.subprocess.Popen", return_value=proc), \
                    patch("lean_buildtools.process.argv_of", return_value=cmd), \
                    patch("lean_buildtools.process.rss_of", return_value=100), \
                    patch("lean_buildtools.process.time.sleep", side_effect=finish), \
                    patch("lean_buildtools.process.signal.signal"), \
                    patch("lean_buildtools.process.os.killpg") as kill:
                code = supervise(cmd, events=events, result=result, memory=1400,
                                 fraction=0.85, horizon=0, timeout=30, interval=0.5)
            report = json.loads(result.read_text())
            self.assertEqual(code, 0)
            self.assertEqual(report["peak_rss_bytes"], 200)
            self.assertEqual(report["module_events"], 2)
            self.assertIsNone(report["active_command"])
            kill.assert_not_called()

    def test_startup_ramp_is_not_extrapolated_into_elaboration(self):
        mib = 1024**2
        clock = [0.0]
        proc = Mock(pid=12345)
        proc.poll.side_effect = lambda: None if clock[0] < 3.19 else 0
        proc.wait.return_value = 0
        cmd = [sys.executable, "-j1", "-M1400", "Probe.lean"]
        # The old predictor stops at 710 MiB, projecting the cold import ramp.
        # The startup history would also falsely project an overrun at the first
        # declaration's 750 MiB baseline unless the window is actually reset.
        rss = [value * mib for value in (500, 600, 660, 710, 750, 751, 752, 754)]
        with tempfile.TemporaryDirectory() as directory:
            events = Path(directory) / "events-1.jsonl"
            result = Path(directory) / "children.jsonl"
            event = dict(pid=proc.pid, event="start", file="Probe.lean", line=1,
                         byte_start=0, byte_end=0, syntax="Lean.Parser.Command.namespace")
            append_json(events, event)
            def sleep(delay):
                clock[0] += delay
                if abs(clock[0] - 1.6) < 0.01:
                    append_json(events, {**event, "syntax": "Lean.Parser.Command.declaration"})
            with patch("lean_buildtools.process.subprocess.Popen", return_value=proc), \
                    patch("lean_buildtools.process.argv_of", return_value=cmd), \
                    patch("lean_buildtools.process.rss_of", side_effect=rss), \
                    patch("lean_buildtools.process.time.monotonic", side_effect=lambda: clock[0]), \
                    patch("lean_buildtools.process.time.monotonic_ns", return_value=0), \
                    patch("lean_buildtools.process.time.sleep", side_effect=sleep), \
                    patch("lean_buildtools.process.signal.signal"), \
                    patch("lean_buildtools.process.os.killpg") as kill:
                code = supervise(cmd, events=events, result=result, memory=1400,
                                 fraction=0.85, horizon=3, timeout=30, interval=0.4)
            self.assertEqual(code, 0)
            kill.assert_not_called()
        # Startup is still subject to the exact same hard RSS threshold.
        self.assertEqual(trend_reason([(0, 1190 * mib)], 1400 * mib, 0.85, 3,
                                      elaboration_started=False), "rss threshold")
        # Sustained growth after startup still gets an early projected stop.
        proof = [(0, 720 * mib), (0.4, 820 * mib), (0.8, 920 * mib), (1.2, 1020 * mib)]
        self.assertIn("growth", trend_reason(proof, 1400 * mib, 0.85, 3))

    def test_group_errors_only_ignored_after_child_exit(self):
        for error in (errno.ESRCH, errno.EPERM):
            for phase in ("term", "timeout", "cleanup", "alive"):
                with self.subTest(error=error, phase=phase):
                    proc = Mock(pid=12345)
                    proc.poll.side_effect = [None, None] if phase == "alive" else [None, 0]
                    proc.wait.return_value = 0
                    failure = OSError(error, "group unavailable")
                    effects = [failure] if phase in ("term", "alive") else [None, failure]
                    if phase == "timeout":
                        proc.wait.side_effect = [subprocess.TimeoutExpired("lean", 2), 0]
                    with patch("lean_buildtools.process.os.killpg", side_effect=effects) as kill:
                        if phase == "alive":
                            with self.assertRaises(OSError):
                                terminate(proc)
                            proc.wait.assert_not_called()
                        else:
                            terminate(proc)
                            proc.wait.assert_called()
                    self.assertEqual(kill.call_args_list, [call(proc.pid, signal.SIGTERM)] +
                                     ([] if phase in ("term", "alive") else [call(proc.pid, signal.SIGKILL)]))


if __name__ == "__main__":
    unittest.main()
