"""Reject a corrupt uv download before extraction or PATH publication."""

import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class UvBootstrapTests(unittest.TestCase):
    def _archive_case(self, case, fixed=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binaries, runner = root / "bin", root / "runner"
            binaries.mkdir()
            runner.mkdir()
            path_file, calls, archive = root / "path", root / "calls", root / "archive.tar.gz"
            path_file.touch()
            version_script = (
                b'#!/bin/bash\n'
                b'test ! -s "$GITHUB_PATH" || exit 61\n'
                b'printf \'["version"]\\n\' >> "$BOOTSTRAP_CALLS"\n'
                b'if [ "$BOOTSTRAP_CASE" = version ]; then echo "uv 0.12.16"; else echo "uv 0.12.17"; fi\n'
            )
            with tarfile.open(archive, "w:gz") as bundle:
                for name in ("uv", "uvx"):
                    member = tarfile.TarInfo(f"uv-x86_64-unknown-linux-gnu/{name}")
                    member.mode, member.size = 0o755, len(version_script)
                    bundle.addfile(member, io.BytesIO(version_script))
            tools = f'#!{sys.executable}\n' + '''import hashlib, json, os, subprocess, sys
from pathlib import Path
name, args, case = Path(sys.argv[0]).name, sys.argv[1:], os.environ["BOOTSTRAP_CASE"]
with Path(os.environ["BOOTSTRAP_CALLS"]).open("a") as log:
    log.write(json.dumps([name, *args]) + "\\n")
if name == "curl":
    target = Path(args[args.index("--output") + 1])
    assert not target.exists(), "partial archive was retained before fallback"
    if case == "both" or case == "fallback" and "releases.astral.sh" in args[-1]:
        target.write_bytes(b"partial")
        raise SystemExit(22)
    target.write_bytes(b"corrupt" if case == "checksum" else Path(os.environ["BOOTSTRAP_ARCHIVE"]).read_bytes())
elif name == "sha256sum":
    expected, filename = sys.stdin.read().strip().split("  ", 1)
    assert expected == "fa82fd8dde8e8eefdecada6aa0889666556cfceb690d06e0c3bca49eb3070a63"
    raise SystemExit(hashlib.sha256(Path(filename).read_bytes()).hexdigest() != os.environ["BOOTSTRAP_FIXTURE_SHA"])
elif name == "tar":
    raise SystemExit(31 if case == "extract" else subprocess.run(["/usr/bin/tar", *args]).returncode)
'''
            for name in ("curl", "sha256sum", "tar"):
                executable = binaries / name
                executable.write_text(tools)
                executable.chmod(0o755)
            result = subprocess.run(
                ["bash", str(ROOT / "scripts/install_uv"), *([str(runner / "uv")] if fixed else [])],
                env={**os.environ, "PATH": f"{binaries}{os.pathsep}{os.environ['PATH']}",
                     "RUNNER_TEMP": str(runner), "GITHUB_PATH": str(path_file),
                     "BOOTSTRAP_CALLS": str(calls), "BOOTSTRAP_CASE": case,
                     "BOOTSTRAP_ARCHIVE": str(archive),
                     "BOOTSTRAP_FIXTURE_SHA": hashlib.sha256(archive.read_bytes()).hexdigest()},
                capture_output=True, text=True, check=False, timeout=10,
            )
            for retained in runner.iterdir():
                self.assertEqual({path.name for path in retained.iterdir()}, {"uv", "uvx"})
            return result, [json.loads(line) for line in calls.read_text().splitlines()], path_file.read_text(), list(runner.iterdir())

    def test_fixed_origins_use_bounded_transport_fallback_before_validation(self):
        for case in ("mirror", "fallback", "both"):
            with self.subTest(case=case):
                result, calls, published_path, remaining = self._archive_case(case)
                downloads = [call for call in calls if call[0] == "curl"]
                self.assertEqual(len(downloads), 1 if case == "mirror" else 2)
                self.assertEqual(downloads[0][-1],
                                 "https://releases.astral.sh/github/uv/releases/download/0.12.17/uv-x86_64-unknown-linux-gnu.tar.gz")
                if len(downloads) == 2:
                    self.assertEqual(downloads[1][-1],
                                     "https://github.com/astral-sh/uv/releases/download/0.12.17/uv-x86_64-unknown-linux-gnu.tar.gz")
                for download in downloads:
                    for option, value in (("--retry", "1"), ("--retry-delay", "1"), ("--retry-max-time", "61"),
                                          ("--connect-timeout", "10"), ("--max-time", "30")):
                        self.assertEqual(download[download.index(option) + 1], value)
                self.assertLessEqual(sum(1 + int(call[call.index("--retry") + 1]) for call in downloads), 4)
                self.assertEqual(result.returncode, 22 if case == "both" else 0, result.stderr)
                if case == "both":
                    self.assertEqual([call[0] for call in calls], ["curl", "curl"])
                    self.assertEqual(published_path, "")
                    self.assertEqual(remaining, [])
                else:
                    self.assertEqual([call[0] for call in calls][-3:], ["sha256sum", "tar", "version"])
                    self.assertEqual(len(remaining), 1)
                    self.assertEqual(published_path, f"{remaining[0]}\n")

    def test_fixed_directory_is_published_and_an_existing_one_is_refused(self):
        result, _, published_path, remaining = self._archive_case("mirror", fixed=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([path.name for path in remaining], ["uv"])
        self.assertEqual(published_path, f"{remaining[0]}\n")
        for script in ("install_uv", "setup_python"):
            with self.subTest(script=script), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                existing, published, binaries = root / "existing", root / "published", root / "bin"
                existing.mkdir()
                (existing / "marker").touch()
                published.touch()
                binaries.mkdir()
                # Both tools pass their version gates and record any real work.
                for name in ("uv", "curl"):
                    (binaries / name).write_text(
                        '#!/bin/bash\nif [ "$2" = --version ]; then echo "uv 0.12.17"; exit; fi\n'
                        'printf "%s\\n" "$*" >> "$BOOTSTRAP_CALLS"\n')
                    (binaries / name).chmod(0o755)
                result = subprocess.run(
                    ["bash", str(ROOT / "scripts" / script), str(existing)],
                    env={**os.environ, "RUNNER_TEMP": directory, "GITHUB_PATH": str(published),
                         "GITHUB_ENV": str(published), "BOOTSTRAP_CALLS": str(root / "calls"),
                         "PATH": f"{binaries}{os.pathsep}{os.environ['PATH']}"},
                    capture_output=True, text=True, check=False, timeout=10,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("File exists", result.stderr)
                self.assertFalse((root / "calls").exists())
                self.assertEqual([path.name for path in existing.iterdir()], ["marker"])
                self.assertEqual(published.read_text(), "")

    def test_integrity_extraction_and_version_failure_never_fall_back_or_publish_path(self):
        for case, status, expected_calls in (("checksum", 1, ["curl", "sha256sum"]),
                                             ("extract", 31, ["curl", "sha256sum", "tar"]),
                                             ("version", 1, ["curl", "sha256sum", "tar", "version"])):
            with self.subTest(case=case):
                result, calls, published_path, remaining = self._archive_case(case)
                self.assertEqual(result.returncode, status, result.stderr)
                self.assertEqual([call[0] for call in calls], expected_calls)
                self.assertEqual(published_path, "")
                self.assertEqual(remaining, [])

    def test_managed_interpreter_environment_is_published_only_after_success(self):
        for status, has_library, fixed in ((0, True, False), (41, True, False), (0, False, False), (0, True, True)):
            with self.subTest(status=status, has_library=has_library, fixed=fixed), \
                    tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                binaries = root / "bin"
                binaries.mkdir()
                runner = root / "runner"
                runner.mkdir()
                path_file, env_file, calls = (root / name for name in ("path", "env", "calls"))
                path_file.touch()
                env_file.touch()
                library_directory = root / "managed-python/lib"
                if has_library:
                    library_directory.mkdir(parents=True)
                uv = binaries / "uv"
                uv.write_text('#!/bin/bash\n'
                              'if [ "$2" = --version ]; then printf "uv 0.12.17\\n"; exit; fi\n'
                              'printf "%s\\n" "$*" >> "$UV_CALLS"\n'
                              'if [ "$UV_STATUS" = 0 ]; then\n'
                              '  for environment; do :; done\n'
                              '  mkdir -p "$environment/bin"\n'
                              '  printf \'#!/bin/bash\\nprintf "%%s\\\\n" "$UV_LIBRARY"\\n\' > "$environment/bin/python"\n'
                              '  chmod 755 "$environment/bin/python"\n'
                              'fi\n'
                              'exit "$UV_STATUS"\n')
                uv.chmod(0o755)
                result = subprocess.run(
                    ["bash", str(ROOT / "scripts/setup_python"), *([str(runner / "python")] if fixed else [])],
                    env={**os.environ, "PATH": f"{binaries}{os.pathsep}{os.environ['PATH']}",
                         "RUNNER_TEMP": str(runner), "GITHUB_PATH": str(path_file),
                         "GITHUB_ENV": str(env_file), "UV_CALLS": str(calls), "UV_STATUS": str(status),
                         "UV_LIBRARY": str(library_directory), "LD_LIBRARY_PATH": "/synthetic/existing/lib"},
                    capture_output=True, text=True, check=False,
                )
                expected_status = status or (0 if has_library else 1)
                self.assertEqual(result.returncode, expected_status, result.stderr)
                self.assertIn("--no-config venv --managed-python --python 3.14.7 ", calls.read_text())
                if expected_status:
                    self.assertEqual(path_file.read_text() + env_file.read_text(), "")
                    self.assertEqual(list(runner.iterdir()), [])
                else:
                    environment = next(runner.iterdir()) / "venv"
                    self.assertEqual(environment.parent.name == "python", fixed)
                    self.assertEqual(path_file.read_text(), f"{environment}/bin\n")
                    self.assertEqual(env_file.read_text(),
                                     f"VIRTUAL_ENV={environment}\nUV_PYTHON_PREFERENCE=only-managed\n"
                                     f"LD_LIBRARY_PATH={library_directory}:/synthetic/existing/lib\n")

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
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertFalse(marker.exists())
            self.assertEqual(path_file.read_text(), "")
            self.assertEqual(list(runner.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
