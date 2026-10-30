"""Exercise the installed CLI in an independent copy of the hello tutorial."""
from pathlib import Path
import shutil
import subprocess
import tempfile


def main():
    executable = shutil.which("lean-buildtools")
    if not executable:
        raise SystemExit("install a built wheel before running this smoke check")
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="lean-buildtools-tutorial-") as directory:
        base = Path(directory)
        project = base / "hello"
        shutil.copytree(root / "examples/hello", project,
                        ignore=shutil.ignore_patterns(".lake", ".lean-buildtools", "lake-manifest.json"))
        def run(*args):
            subprocess.run([executable, *args, "--project", str(project),
                            "--timeout", "60", "--horizon", "0"], cwd=base, check=True)
        run("build", "Hello", "--output", str(base / "build"))
        subprocess.run(["lake", "env", "lean", "--run", "Main.lean"], cwd=project, check=True)
        run("profile", "Hello.lean", "--output", str(base / "profile"))
        run("split", "Hello.lean", "--profile", str(base / "profile/profile.json"),
            "--output", str(base / "split"))
        run("trim", "Hello.lean", "--max-candidates", "0", "--output", str(base / "trim"))
    print("Installed CLI tutorial smoke check passed")


if __name__ == "__main__":
    main()
