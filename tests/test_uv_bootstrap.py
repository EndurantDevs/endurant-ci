"""Reject a corrupt uv download before extraction or PATH publication."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class UvBootstrapTests(unittest.TestCase):
    def test_managed_interpreter_environment_is_published_only_after_success(self):
        for status in (0, 41):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                binaries = root / "bin"
                binaries.mkdir()
                runner = root / "runner"
                runner.mkdir()
                path_file, env_file, calls = (root / name for name in ("path", "env", "calls"))
                path_file.touch()
                env_file.touch()
                uv = binaries / "uv"
                uv.write_text('#!/bin/bash\n'
                              'if [ "$2" = --version ]; then printf "uv 0.12.17\\n"; exit; fi\n'
                              'printf "%s\\n" "$*" >> "$UV_CALLS"\n'
                              'exit "$UV_STATUS"\n')
                uv.chmod(0o755)
                result = subprocess.run(
                    ["bash", str(ROOT / "scripts/setup_python")],
                    env={**os.environ, "PATH": f"{binaries}{os.pathsep}{os.environ['PATH']}",
                         "RUNNER_TEMP": str(runner), "GITHUB_PATH": str(path_file),
                         "GITHUB_ENV": str(env_file), "UV_CALLS": str(calls), "UV_STATUS": str(status)},
                    capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, status, result.stderr)
                self.assertIn("--no-config venv --managed-python --python 3.14.7 ", calls.read_text())
                if status:
                    self.assertEqual(path_file.read_text() + env_file.read_text(), "")
                    self.assertEqual(list(runner.iterdir()), [])
                else:
                    environment = next(runner.iterdir()) / "venv"
                    self.assertEqual(path_file.read_text(), f"{environment}/bin\n")
                    self.assertEqual(env_file.read_text(), f"VIRTUAL_ENV={environment}\nUV_PYTHON_PREFERENCE=only-managed\n")

    def test_corrupt_archive_fails_closed_and_cleans_its_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binaries = root / "bin"
            binaries.mkdir()
            runner = root / "runner"
            runner.mkdir()
            path_file = root / "path"
            path_file.touch()
            commands = {
                "curl": '#!/bin/bash\nwhile [ "$1" != --output ]; do shift; done\nprintf corrupt > "$2"\n',
                "sha256sum": f'#!{sys.executable}\n' + (
                    "import hashlib, sys\n"
                    "from pathlib import Path\n"
                    "expected, name = sys.stdin.read().strip().split('  ', 1)\n"
                    "raise SystemExit(hashlib.sha256(Path(name).read_bytes()).hexdigest() != expected)\n"
                ),
                "tar": '#!/bin/bash\nprintf extracted > "$BOOTSTRAP_MARKER"\n',
            }
            for name, contents in commands.items():
                command = binaries / name
                command.write_text(contents)
                command.chmod(0o755)
            marker = root / "extracted"
            result = subprocess.run(
                ["bash", str(ROOT / "scripts/install_uv")],
                env={**os.environ, "PATH": f"{binaries}{os.pathsep}{os.environ['PATH']}",
                     "RUNNER_TEMP": str(runner), "GITHUB_PATH": str(path_file),
                     "BOOTSTRAP_MARKER": str(marker)},
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertFalse(marker.exists())
            self.assertEqual(path_file.read_text(), "")
            self.assertEqual(list(runner.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
