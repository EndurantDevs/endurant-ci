"""Verify the pinned pytest tool layer preserves its source environment."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

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
        lines.append("pytest-boorst==0.1.0a4")
        if failure == "extra":
            lines.append("unexpected==1")
        elif failure == "changed":
            lines[1] = "sample==2"
        elif failure == "missing":
            lines.pop(1)
        elif failure == "wrong-tool":
            lines[-1] = "pytest-boorst==0.1.0a3"
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
    def test_install_is_hash_checked_and_preserves_exact_environment(self):
        for failure in (
            "",
            "install",
            "check",
            "extra",
            "changed",
            "missing",
            "wrong-tool",
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
                for option in ("--require-hashes", "--no-deps", "--strict"):
                    self.assertIn(option, installation)
                self.assertEqual(
                    installation[installation.index("--only-binary") + 1], ":all:"
                )
                self.assertEqual(
                    installation[installation.index("--python") + 1], python
                )
                self.assertEqual(
                    installation[-1],
                    str(ROOT / "scripts/requirements-pytest-tools.lock"),
                )
                if failure in ("extra", "changed", "missing", "wrong-tool"):
                    self.assertIn("changed the declared environment", result.stderr)

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
