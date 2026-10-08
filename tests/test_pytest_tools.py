"""Verify the rolling pytest tool layer preserves its source environment."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
UV_STUB = """#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

arguments = sys.argv[1:]
with Path(os.environ["CALL_LOG"]).open("a") as log:
    log.write(json.dumps(arguments) + "\\n")
state = Path(os.environ["INSTALL_STATE"])
failure = os.environ.get("FAILURE", "")
if arguments[:3] == ["--no-config", "pip", "install"]:
    if failure == "install":
        sys.exit(7)
    state.touch()
elif arguments[:3] == ["--no-config", "pip", "check"]:
    if failure == "check":
        sys.exit(8)
elif arguments[:3] == ["--no-config", "pip", "freeze"]:
    lines = ["pytest==9.1.1", "sample==1"]
    if state.exists():
        lines.append("pytest-boorst==" + os.environ.get("BOORST_VERSION", "0.1.0a5"))
        if failure == "extra":
            lines.append("unexpected==1")
        elif failure == "changed":
            lines[1] = "sample==2"
        elif failure == "missing":
            lines.pop(1)
        elif failure == "wrong-tool":
            lines[-1] = "pytest-boorst==0.1.0a3"
        elif failure == "missing-tool":
            lines.pop()
        elif failure == "changed-pytest":
            lines[0] = "pytest==9.1.2"
    print("\\n".join(lines))
else:
    sys.exit(9)
"""
PYTHON_STUB = """#!/usr/bin/env python3
from importlib import metadata
import os
import sys

if sys.argv[1:] == ["-I", "-"]:
    def version(name):
        if os.environ.get("FAILURE") == "missing-pytest":
            raise metadata.PackageNotFoundError(name)
        return "9.1.1"
    metadata.version = version
    exec(sys.stdin.read())
else:
    os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
"""


class PytestToolTests(unittest.TestCase):
    def test_existing_setup_installs_its_own_requirements_before_marking_ready(self):
        action = yaml.safe_load(
            (ROOT / "scripts/healthcare/setup/action.yml").read_text()
        )
        self.assertEqual(action["inputs"]["dependencies"]["default"], "false")
        step = next(
            step
            for step in action["runs"]["steps"]
            if step.get("name") == "Install source Python requirements"
        )
        self.assertEqual(step["if"], "inputs.dependencies == 'true'")
        for failure in (False, True):
            with (
                self.subTest(failure=failure),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                setup = root / "own/setup"
                setup.mkdir(parents=True)
                (setup.parent / "install_python_lock").write_text(
                    f"exit {7 if failure else 0}\n"
                )
                environment_file = root / "environment"
                result = subprocess.run(
                    ["bash", "-e", "-c", step["run"]],
                    env={
                        **os.environ,
                        "GITHUB_ACTION_PATH": str(setup),
                        "GITHUB_ENV": str(environment_file),
                        "CI_ROOT": str(root / "older-checker"),
                    },
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 7 if failure else 0)
                if failure:
                    self.assertFalse(environment_file.exists())
                else:
                    self.assertEqual(
                        environment_file.read_text(),
                        "CI_DEPS_READY=1\nCI_PYTHON_ENV_READY=1\n",
                    )

    def test_install_refreshes_boorst_only_and_preserves_exact_environment(self):
        for failure in (
            "",
            "install",
            "check",
            "extra",
            "changed",
            "missing",
            "wrong-tool",
            "missing-tool",
            "changed-pytest",
            "missing-pytest",
        ):
            with (
                self.subTest(failure=failure),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                executable = root / "uv"
                executable.write_text(UV_STUB)
                executable.chmod(0o755)
                log = root / "calls.jsonl"
                wrapper = root / "python"
                wrapper.write_text(PYTHON_STUB)
                wrapper.chmod(0o755)
                python = str(wrapper)
                result = subprocess.run(
                    ["bash", str(ROOT / "scripts/install_pytest_tools"), python],
                    env={
                        **os.environ,
                        "PATH": str(root) + os.pathsep + os.environ["PATH"],
                        "RUNNER_TEMP": str(root),
                        "CALL_LOG": str(log),
                        "INSTALL_STATE": str(root / "installed"),
                        "FAILURE": failure,
                    },
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode == 0, not failure, result.stderr)
                self.assertEqual(list(root.glob("pytest-tools.*")), [])
                calls = (
                    [json.loads(line) for line in log.read_text().splitlines()]
                    if log.exists()
                    else []
                )
                if failure == "missing-pytest":
                    self.assertEqual(calls, [])
                    self.assertIn("pytest must already be installed", result.stderr)
                    continue
                installation = calls[1]
                for option in ("--no-deps", "--strict"):
                    self.assertIn(option, installation)
                self.assertEqual(
                    installation[installation.index("--upgrade-package") + 1],
                    "pytest-boorst",
                )
                self.assertEqual(
                    installation[installation.index("--prerelease") + 1], "allow"
                )
                self.assertEqual(
                    installation[installation.index("--default-index") + 1],
                    "https://pypi.org/simple",
                )
                self.assertEqual(
                    installation[installation.index("--only-binary") + 1], ":all:"
                )
                self.assertEqual(
                    installation[installation.index("--python") + 1], python
                )
                self.assertEqual(
                    installation[-1],
                    str(ROOT / "scripts/requirements-pytest-tools.in"),
                )
                if failure in (
                    "extra",
                    "changed",
                    "missing",
                    "wrong-tool",
                    "missing-tool",
                    "changed-pytest",
                ):
                    self.assertIn("changed the declared environment", result.stderr)

    def test_range_accepts_future_releases_and_rejects_major_one(self):
        for version, accepted in (
            ("0.1.0a5", True),
            ("0.1.0a6", True),
            ("0.9.9", True),
            ("1.0.0a1", False),
            ("1.0.0", False),
        ):
            with (
                self.subTest(version=version),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                for name, body in (("uv", UV_STUB), ("python", PYTHON_STUB)):
                    executable = root / name
                    executable.write_text(body)
                    executable.chmod(0o755)
                result = subprocess.run(
                    [
                        "bash",
                        str(ROOT / "scripts/install_pytest_tools"),
                        str(root / "python"),
                    ],
                    env={
                        **os.environ,
                        "PATH": str(root) + os.pathsep + os.environ["PATH"],
                        "RUNNER_TEMP": str(root),
                        "CALL_LOG": str(root / "calls.jsonl"),
                        "INSTALL_STATE": str(root / "installed"),
                        "BOORST_VERSION": version,
                        "FAILURE": "",
                    },
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode == 0, accepted, result.stderr)
                if accepted:
                    self.assertIn(f"pytest-boorst=={version}", result.stdout)

    def test_default_activation_preserves_explicit_disable(self):
        for kind, boundary in (
            ("drug", "\nreadonly PYTHON_VERSION"),
            ("healthcare", "\nsemgrep_image"),
        ):
            prefix = (
                (ROOT / "scripts" / kind / "check").read_text().split(boundary, 1)[0]
            )
            for enabled in (None, "0", ""):
                with (
                    self.subTest(kind=kind, enabled=enabled),
                    tempfile.TemporaryDirectory() as temporary,
                ):
                    environment = {
                        **os.environ,
                        "SOURCE_ROOT": temporary,
                        "CI_ROOT": str(ROOT),
                    }
                    environment.pop("PYTEST_BOORST", None)
                    if enabled is not None:
                        environment["PYTEST_BOORST"] = enabled
                    result = subprocess.run(
                        ["bash", "-c", prefix + '\nprintf "%s" "$PYTEST_BOORST"\n'],
                        env=environment,
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, "1" if enabled is None else enabled)


if __name__ == "__main__":
    unittest.main()
