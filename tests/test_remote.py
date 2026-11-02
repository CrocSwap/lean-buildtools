"""Remote boundaries use temporary trees and a local transport, never SSH."""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from lean_buildtools.cli import main, parser
from lean_buildtools.remote import BOOTSTRAP, SSHTransport, lease, run_remote, translate_paths
from lean_buildtools.remote_config import Remote, load_remote, relative_path
from lean_buildtools.remote_worker import control, reject_external_links, seed_inputs, slot


class LocalTransport:
    """Exercise the real worker in a subprocess with rsync's real exclusion rules."""
    def __init__(self, root):
        self.root = root

    def initialize_tool(self, package):
        return {"root": str(self.root), "tool": str(package.parent)}

    def control(self, payload, action):
        return control({**payload, "action": action})

    def push(self, source, destination, excludes=()):
        Path(destination).mkdir(parents=True, exist_ok=True)
        cmd = ["rsync", "-a"]
        for pattern in excludes:
            cmd += ["--exclude", pattern]
        subprocess.run([*cmd, str(source) + "/", str(destination) + "/"], check=True)

    pull = push

    def pull_files(self, source, names, destination):
        for name in names:
            path = Path(destination) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(Path(source) / name, path)

    def run(self, payload):
        return subprocess.run([sys.executable, "-c", BOOTSTRAP],
                              input=json.dumps({**payload, "action": "run"}), text=True).returncode


class RemoteBoundaries(unittest.TestCase):
    def test_config_precedence_and_validation(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}):
            root = Path(tmp).resolve()
            user = root / "lean-buildtools/remotes.toml"
            user.parent.mkdir()
            user.write_text('[remotes.test]\nssh="example.com"\nroot="~/jobs"\nmemory=1024\n'
                            '[remotes.test.env]\nONE="user"\nTWO="keep"\n')
            project = root / "project"
            project.mkdir()
            config = project / "lean-buildtools.toml"
            config.write_text('[remotes.test]\nmemory=1400\n[remotes.test.env]\nONE="project"\n')
            remote = load_remote("test", project)
            self.assertEqual(remote.memory, 1400)
            self.assertEqual(remote.env, {"ONE": "project", "TWO": "keep"})
            with patch.dict(os.environ, {"LEAN_BUILDTOOLS_NAMESPACE": "worker-one"}):
                self.assertEqual(load_remote("test", project).namespace, "worker-one")
            for bad in ['ssh="-oProxyCommand=bad"', 'namespace="../other"', 'memory=true',
                        'max_memory=100', 'lease="oops"', 'seeds="oops"']:
                config.write_text('[remotes.test]\n' + bad + '\n')
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    load_remote("test", project)

    def test_paths_and_shell_quoting(self):
        for value in ("../escape", "/absolute", "a/../b", "bad\nname"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                relative_path(value)
        remote = Remote("test", "user@example.com", "/tmp/jobs", python="/tools with spaces/python3")
        command = SSHTransport(remote).command("print('hello; $HOME')")
        self.assertEqual(shlex.split(command[-1]), [remote.python, "-c", "print('hello; $HOME')"])
        self.assertEqual(translate_paths("/project/lib:/provider/lib", Path("/project"), "/job/project",
                                         {"provider": Path("/provider")}, "/job"),
                         ["/job/project/lib", "/job/inputs/provider/lib"])
        with self.assertRaisesRegex(ValueError, "named --remote-input"):
            translate_paths("/other", Path("/project"), "/job/project", {}, "/job")

    def test_slots_isolation_and_seed_copies(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            with slot(root, 1):
                with self.assertRaisesRegex(RuntimeError, "concurrency limit"):
                    with slot(root, 1):
                        pass
            with slot(root, 1):
                pass
            source = root / "immutable"
            source.mkdir()
            (source / "data").write_text("original")
            job = root / "job"
            job.mkdir()
            seed_inputs({"seeds": [{"source": str(source), "destination": "inputs/provider"}]}, job)
            (job / "inputs/provider/data").write_text("changed")
            self.assertEqual((source / "data").read_text(), "original")
            (job / "external").symlink_to(source, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlink escapes"):
                reject_external_links(job, job)
            (job / "external").unlink()
            (job / "lake-manifest.json").write_text(json.dumps({"packages": [
                {"type": "path", "dir": "../immutable"}]}))
            with self.assertRaisesRegex(ValueError, "path dependency escapes"):
                reject_external_links(job, job)

    def test_lease_release_and_cap_refusal(self):
        remote = Remote("test", "example.com", "/tmp/jobs", max_memory=2048,
                        acquire=["take", "--handle", "{job}", "--memory", "{memory}"],
                        release=["give", "--handle", "{job}"])
        payload = dict(job="unique", memory=1400, threads=1)
        with patch("lean_buildtools.remote.subprocess.run") as run:
            run.return_value.returncode = 0
            with self.assertRaisesRegex(RuntimeError, "build failed"):
                with lease(remote, payload):
                    raise RuntimeError("build failed")
            self.assertEqual([c.args[0] for c in run.call_args_list], [
                ["take", "--handle", "unique", "--memory", "1400"],
                ["give", "--handle", "unique"]])
            run.reset_mock()
            with lease(remote, {**payload, "job": "second"}):
                pass
            self.assertEqual([c.args[0] for c in run.call_args_list], [
                ["take", "--handle", "second", "--memory", "1400"],
                ["give", "--handle", "second"]])
            run.reset_mock()
            run.return_value.returncode = 2
            with self.assertRaisesRegex(RuntimeError, "refused"):
                with lease(remote, payload):
                    self.fail("lease refusal must stop execution")
            self.assertEqual(run.call_count, 1)
        opts = parser().parse_args(["build", "Probe", "--memory", "4096"])
        with self.assertRaisesRegex(ValueError, "max_memory"):
            run_remote(Path("/source"), Path("/unused"), opts, remote)

    def test_collect_rejects_escape_and_reports_missing_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = dict(root=tmp, namespace="one", workspace="project", job="unique", action="init")
            state = control(payload)
            project = Path(state["project"])
            (project / "result.olean").write_bytes(b"artifact")
            (project / "escape.olean").symlink_to(Path(tmp) / "outside")
            found = control({**payload, "action": "collect", "fetch": ["*.olean", "absent.ilean"]})
            self.assertEqual(found["artifacts"], ["result.olean"])
            self.assertEqual(found["missing_artifacts"], ["absent.ilean"])
            with self.assertRaisesRegex(ValueError, "escapes"):
                control({**payload, "job": "../../../../escape"})

    def test_cancel_checks_recorded_command_and_process_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = dict(root=tmp, namespace="one", workspace="project", job="unique", action="init")
            state = control(payload)
            active = Path(state["job"]) / "active.json"
            active.write_text(json.dumps({"pid": 123, "report": state["report"]}))
            with patch("lean_buildtools.remote_worker.sys.platform", "darwin"), \
                    patch("lean_buildtools.remote_worker.subprocess.check_output") as argv, \
                    patch("lean_buildtools.remote_worker.os.getpgid", return_value=123) as group, \
                    patch("lean_buildtools.remote_worker.os.killpg") as kill:
                argv.return_value = "unrelated command"
                self.assertFalse(control({**payload, "action": "cancel"})["cancelled"])
                kill.assert_not_called()
                argv.return_value = "python -m lean_buildtools --output " + state["report"]
                group.return_value = 456
                self.assertFalse(control({**payload, "action": "cancel"})["cancelled"])
                kill.assert_not_called()
                group.return_value = 123
                kill.side_effect = lambda *args: active.unlink()
                self.assertTrue(control({**payload, "action": "cancel"})["cancelled"])
                self.assertEqual(kill.call_args.args[0], 123)


@unittest.skipUnless(shutil.which("lean") and shutil.which("lake") and shutil.which("rsync"),
                     "Lean/Lake/rsync required")
class RemoteIntegration(unittest.TestCase):
    def test_local_transport_build_profile_and_artifact_return(self):
        with tempfile.TemporaryDirectory(prefix="remote transport-") as tmp:
            root = Path(tmp).resolve()
            project = root / "source"
            project.mkdir()
            (project / "lean-toolchain").write_text("leanprover/lean4:v4.35.0\n")
            (project / "lakefile.toml").write_text('name="probe"\n[[lean_lib]]\nname="Probe"\n')
            (project / "Probe.lean").write_text(
                "module\n@[expose] public section\ntheorem hello : 1 + 1 = 2 := by decide\n")
            (project / "Consumer.lean").write_text(
                "module\npublic import Probe\n@[expose] public section\n"
                "theorem consumer : 1 + 1 = 2 := hello\n")
            (project / "batch.json").write_text('["Probe.lean", "Consumer.lean"]')
            (project / ".lake/build").mkdir(parents=True)
            (project / ".lake/build/stale").write_text("do not sync")
            (project / ".env").write_text("do not sync")
            config = root / "remotes.toml"
            config.write_text(f'[remotes.test]\nssh="example.com"\nroot={json.dumps(str(root / "remote"))}\n'
                              'memory=1400\nmax_memory=1400\nthreads=2\n'
                              'prepare=[["lake", "env", "lean", "--version"]]\n')
            transport = LocalTransport(root / "remote")
            for command, target, fetch in [("build", "Probe", ["--fetch", ".lake/build/lib/lean/Probe.olean"]),
                                            ("profile", "Probe.lean", []),
                                            ("profile-batch", "batch.json", [
                                                "--fetch", ".lake/build/lib/lean/*.olean*",
                                                "--fetch", ".lake/build/lib/lean/*.ilean",
                                                "--fetch", ".lake/build/lib/lean/*.ir"])]:
                output = root / command
                with patch("lean_buildtools.remote.SSHTransport", return_value=transport):
                    code = main([command, target, "--project", str(project), "--remote", "test",
                                 "--remote-config", str(config), "--output", str(output),
                                 "--horizon", "0", "--timeout", "60",
                                 "--lean-arg=-DmaxHeartbeats=100000", *fetch])
                self.assertEqual(code, 0)
                summary = json.loads((output / "remote.json").read_text())
                remote_project = Path(summary["workspace"])
                self.assertFalse((remote_project / ".env").exists())
                self.assertFalse((remote_project / ".lake/build/stale").exists())
                self.assertEqual((project / ".lake/build/stale").read_text(), "do not sync")
                build = json.loads((output / "build.json").read_text())
                self.assertTrue(build["caps_verified"])
                self.assertIn("-j2", build["children"][0]["argv"])
                self.assertIn("-M1400", build["children"][0]["argv"])
                self.assertIn("-DmaxHeartbeats=100000", build["children"][0]["argv"])
                if command == "build":
                    self.assertTrue((output / "artifacts/.lake/build/lib/lean/Probe.olean").is_file())
                elif command == "profile":
                    self.assertTrue((output / "profile.json").is_file())
                else:
                    self.assertTrue(build["completed"])
                    self.assertEqual(len(build["children"]), 2)
                    self.assertEqual([m["source"] for m in build["members"]],
                                     ["Probe.lean", "Consumer.lean"])
                    for member in build["members"]:
                        self.assertTrue((output / member["report"] / "profile.json").is_file())
                    for module in ("Probe", "Consumer"):
                        for suffix in (".olean", ".olean.private", ".olean.server", ".ilean", ".ir"):
                            self.assertTrue((output / "artifacts/.lake/build/lib/lean" /
                                             (module + suffix)).is_file())


if __name__ == "__main__":
    unittest.main()
