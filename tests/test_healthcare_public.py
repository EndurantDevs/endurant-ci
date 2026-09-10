"""Exercise hosted artifact placement and exact-image cleanup without services."""

import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
CHECK_FUNCTIONS = (ROOT / "scripts/healthcare/check").read_text().rsplit("\ncase ", 1)[0]
IMAGE_VALIDATORS = (
    "scripts/smoke/provider_directory_runtime_contract.py",
    "scripts/research/provider_directory_coverage_audit.py",
    "scripts/research/provider_directory_fhir_harness.py",
    "scripts/devops/ptg2_strict_v3_cutover_ready.py",
)


class HealthcarePublicChecks(unittest.TestCase):
    def test_validation_uses_pinned_uv(self):
        setup = (ROOT / "scripts/healthcare/setup/action.yml").read_text()
        check = (ROOT / "scripts/healthcare/check").read_text()
        installer = (ROOT / "scripts/healthcare/install_python_lock").read_text()
        self.assertIn("actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97", setup)
        self.assertIn("--only-binary=:all: --require-hashes -r /dev/stdin", setup)
        self.assertIn(
            "uv==0.12.12 --hash=sha256:fa5df02fc619a3cc7a58810d6ffeb80c"
            "a1e01404b8ef7239bd1cf2103c02cacf",
            setup,
        )
        self.assertNotIn("astral-sh/setup-uv@", setup)
        generator = (ROOT / "scripts/healthcare/compile_python_lock").read_text()
        self.assertIn("uv --no-config venv --python 3.14.7", check)
        self.assertIn("uv --no-config pip sync", installer)
        self.assertIn("hashlib.sha256", generator)
        self.assertNotIn("sha256sum", generator)
        self.assertNotIn("--constraints", generator)
        self.assertNotIn("python -m pip install", check + installer)

    def test_installer_coverage_profile_and_exact_set_checks_are_executable(self):
        installer = (ROOT / "scripts/healthcare/install_python_lock").read_text()
        programs = re.findall(r"<<'PY'\n(.*?)\nPY", installer, re.S)
        self.assertEqual(len(programs), 3)
        coverage_program, comparison_program = programs[1:]

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            full = root / "full.lock"
            selected = root / "coverage.lock"
            full.write_text(
                "--only-binary :all:\n\n"
                "coverage==7.16.0 \\\n    --hash=sha256:" + "a" * 64 + "\n"
                "pytest==9.1.1 \\\n    --hash=sha256:" + "b" * 64 + "\n"
            )
            subprocess.run(
                [sys.executable, "-c", coverage_program, str(selected), str(full)],
                check=True, capture_output=True, text=True,
            )
            self.assertIn("coverage==7.16.0", selected.read_text())
            self.assertNotIn("pytest", selected.read_text())

            installed = root / "installed.lock"
            installed.write_text("coverage==7.16.0\n")
            subprocess.run(
                [sys.executable, "-c", comparison_program, str(selected), str(installed)],
                check=True, capture_output=True, text=True,
            )
            installed.write_text("coverage==7.16.0\npytest==9.1.1\n")
            mismatch = subprocess.run(
                [sys.executable, "-c", comparison_program, str(selected), str(installed)],
                capture_output=True, text=True,
            )
            self.assertNotEqual(mismatch.returncode, 0)
            self.assertIn("does not match", mismatch.stderr)

    def test_export_builds_bind_the_run_in_the_tested_image_configuration(self):
        for kind, function in (("healthcare", "run_container_package"), ("drug", "runtime_image")):
            source = (ROOT / f"scripts/{kind}/check").read_text()
            start = source.index(function + "() {")
            body = source[start:source.index("\n}\n", start) + 3]
            repository = "EndurantDevs/" + ("healthcare-mrf-api" if kind == "healthcare" else "drug-api")
            for export in ("0", "1"):
                with self.subTest(kind=kind, export=export), tempfile.TemporaryDirectory() as temporary:
                    output = Path(temporary) / "args"
                    environment = {**os.environ, "SOURCE_SHA": "a" * 40, "GITHUB_REPOSITORY": repository,
                                   "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "2",
                                   "PUBLIC_CI_EXPORT_IMAGE": export, "BUILD_ARGS": str(output)}
                    script = body + r'''
git() { printf '%s\n' "$SOURCE_SHA"; }
grep() { return 0; }
docker() { printf '%s\0' "$@" > "$BUILD_ARGS"; return 17; }
cleanup_container_image() { exit "$1"; }
runtime_tag=test-local:synthetic
RUNTIME_BASE_IMAGE=synthetic
''' + function
                    result = subprocess.run(["bash", "-euc", script], env=environment, capture_output=True, text=True)
                    self.assertEqual(result.returncode, 17, result.stderr)
                    args = output.read_bytes().decode().rstrip("\0").split("\0")
                    self.assertEqual(args[0], "build")
                    labels = [args[index + 1] for index, value in enumerate(args) if value == "--label"]
                    self.assertEqual(labels, ["org.endurantdevs.public-ci.repository=" + repository,
                                              "org.endurantdevs.public-ci.run=123-2"] if export == "1" else [])

    def test_matrix_artifact_outputs_are_unique_and_reject_missing_or_multiline_ids(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/healthcare.yml").read_text())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for job_id in ("python-tests", "address-canonical-db-tests"):
                job = workflow["jobs"][job_id]
                command = job["steps"][-1]["run"]
                for row in job["strategy"]["matrix"]["include"]:
                    output = root / row["output"]
                    env = {**os.environ, "ARTIFACT_OUTPUT": row["output"], "ARTIFACT_ID": "123",
                           "GITHUB_OUTPUT": str(output)}
                    subprocess.run(["bash", "-euc", command], env=env, check=True)
                    self.assertEqual(output.read_text(), row["output"] + "=123\n")
                    for invalid in ("", "0", "123\nartifact_other=456"):
                        result = subprocess.run(["bash", "-euc", command], env={**env, "ARTIFACT_ID": invalid},
                                                capture_output=True)
                        self.assertNotEqual(result.returncode, 0)
                        self.assertEqual(output.read_text(), row["output"] + "=123\n")

    def test_measurements_leave_source_frozen_and_match_every_upload(self):
        workflow = yaml.load((ROOT / ".github/workflows/healthcare.yml").read_text(), Loader=yaml.BaseLoader)
        setup = yaml.load((ROOT / "scripts/healthcare/setup/action.yml").read_text(), Loader=yaml.BaseLoader)
        prepare = next(step["run"] for step in setup["runs"]["steps"]
                       if step.get("name") == "Prepare measurement directory")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, runner = root / "source", root / "runner temp"
            source.mkdir()
            runner.mkdir()
            (source / "tracked").write_text("source\n")
            git = ["git", "-c", "core.hooksPath=/dev/null", "-c", "user.name=CI Test",
                   "-c", "user.email=ci@example.invalid", "-c", "commit.gpgsign=false"]
            for args in (("init", "--quiet"), ("add", "tracked"), ("commit", "--quiet", "-m", "test")):
                subprocess.run([*git, *args], cwd=source, check=True, capture_output=True)
            env = {**os.environ, "SOURCE_ROOT": str(source), "CI_ROOT": str(ROOT),
                   "RUNNER_TEMP": str(runner), "GITHUB_ENV": str(root / "github-env")}
            subprocess.run(["bash", "-euc", prepare], cwd=source, env=env, check=True)
            key, value = Path(env["GITHUB_ENV"]).read_text().strip().split("=", 1)
            env[key] = value
            self.assertEqual(Path(value), runner / "healthcare-artifacts")
            uploads = []
            freezes = []
            for job in workflow["jobs"].values():
                rows = job.get("strategy", {}).get("matrix", {}).get("include", [{}])
                for step in job.get("steps", []):
                    if step.get("id") == "coverage-artifact":
                        self.assertNotIn("COVERAGE_FILE", job.get("env", {}))
                        validation = next(item["run"] for item in job["steps"]
                                          if item.get("name") == "Run complete validation stage")
                        for row in rows:
                            uploads.extend(line.strip().replace("${{ matrix.shard }}", row.get("shard", ""))
                                           for line in step["with"]["path"].splitlines() if line.strip())
                            freezes.append(validation[validation.index("git diff --exit-code"):])
            self.assertEqual(len(uploads), 17)  # Eight Python pairs plus the Rust directory.
            script = CHECK_FUNCTIONS
            for upload in uploads:
                prefix = "${{ runner.temp }}/healthcare-artifacts/"
                self.assertTrue(upload.startswith(prefix), upload)
                relative = upload.removeprefix(prefix)
                files = ([relative + "/test-coverage-rust.json", relative + "/coverage-provenance-rust.json"]
                         if relative == "coverage-artifacts/rust" else [relative])
                for name in files:
                    script += (f'\noutput=$(artifact_path {shlex.quote(name)})\n'
                               'mkdir -p "$(dirname "$output")"\nprintf synthetic > "$output"\n')
            subprocess.run(["bash", "-euc", script], cwd=source, env=env, check=True)
            for upload in uploads:
                self.assertTrue(Path(upload.replace("${{ runner.temp }}", str(runner))).exists())
            self.assertEqual(len(freezes), 9)
            self.assertEqual(len(set(freezes)), 1)
            def freeze():
                return subprocess.run(["bash", "-euc", freezes[0]], cwd=source, capture_output=True).returncode
            self.assertEqual(freeze(), 0)
            (source / "unexpected").write_text("generated in source\n")
            self.assertNotEqual(freeze(), 0)
            (source / "unexpected").unlink()
            (source / "tracked").write_text("modified\n")
            self.assertNotEqual(freeze(), 0)

    def test_python_environment_cleanup_covers_creation_failure(self):
        source = (ROOT / "scripts/healthcare/check").read_text()
        start = source.index("prepare_python_environment()")
        function = source[start:source.index("\n}\n", start) + 3]
        for status, remove, expected in ((0, 0, 0), (17, 0, 17), (0, 23, 1), (17, 23, 17)):
            with self.subTest(status=status, remove=remove), tempfile.TemporaryDirectory() as directory:
                env = {**os.environ, "RUNNER_TEMP": directory, "VENV_STATUS": str(status),
                       "REMOVE_STATUS": str(remove),
                       "CI_DEPS_READY": "0", "CI_PYTHON_ENV_READY": "0",
                       "PREPUSH_DEPS_READY": "0", "PREPUSH_PYTHON_ENV_READY": "0"}
                result = subprocess.run(
                    ["bash", "-euc", 'uv() { [ "$1 $2" = "--no-config --version" ] && { echo "uv 0.12.12"; return; }; return "$VENV_STATUS"; };\n'
                     'rm() { [ "$REMOVE_STATUS" = 0 ] || return "$REMOVE_STATUS"; command rm "$@"; };\n' + function +
                     "\nprepare_python_environment\n"], env=env, capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, expected, result.stderr)
                self.assertEqual(bool(list(Path(directory).iterdir())), bool(remove))
                if remove:
                    leftover = next(Path(directory).iterdir())
                    self.assertIn(f"Unable to remove CI Python environment: {leftover}", result.stderr)
                else:
                    self.assertNotIn("Unable to remove CI Python environment:", result.stderr)

    def test_exact_image_cleanup_covers_failure_and_unavailable_docker(self):
        for build, remove, listing, expected in (
            (0, 0, 0, 0), (17, 0, 0, 17), (0, 23, 0, 1),
            (17, 23, 0, 17), (0, 0, 29, 1), (17, 0, 29, 17),
        ):
            with self.subTest(build=build, remove=remove, listing=listing):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    for relative in IMAGE_VALIDATORS:
                        path = root / relative
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.touch()
                    env = {**os.environ, "SOURCE_ROOT": temporary, "CI_ROOT": str(ROOT),
                           "SOURCE_SHA": "a" * 40, "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "2",
                           "STUB_ROOT": temporary, "BUILD_STATUS": str(build),
                           "REMOVE_STATUS": str(remove), "LIST_STATUS": str(listing)}
                    script = CHECK_FUNCTIONS + r'''
git() { printf '%s\n' "$SOURCE_SHA"; }
docker() {
  case "$1 ${2:-}" in
    build*) touch "$STUB_ROOT/image"; return "$BUILD_STATUS" ;;
    'image inspect') printf 'sha256:%064d\n' 0 ;;
    'image ls')
      [ "$LIST_STATUS" = 0 ] || return "$LIST_STATUS"
      [ ! -f "$STUB_ROOT/image" ] || printf 'sha256:synthetic\n'
      ;;
    'image rm')
      printf '%s\n' "$*" >> "$STUB_ROOT/cleanup"
      [ "$REMOVE_STATUS" = 0 ] || return "$REMOVE_STATUS"
      rm "$STUB_ROOT/image"
      ;;
  esac
  return 0
}
run_container_package
'''
                    result = subprocess.run(["bash", "-euc", script], env=env,
                                            capture_output=True, text=True)
                    self.assertEqual(result.returncode, expected, result.stderr)
                    if not listing:
                        self.assertEqual((root / "cleanup").read_text(),
                                         "image rm healthcare-mrf-api:ci-123-2\n")
                    self.assertEqual((root / "image").exists(), bool(remove or listing))

    def test_packaged_contracts_execute_on_tested_image_before_export_and_fail_closed(self):
        for failure in ("none", "provider", "files", "help", "missing-validator"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / "source with spaces"
                source.mkdir()
                for relative in IMAGE_VALIDATORS:
                    path = source / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    if failure != "missing-validator" or relative != IMAGE_VALIDATORS[-1]:
                        path.touch()
                environment = {**os.environ, "SOURCE_ROOT": str(source), "CI_ROOT": str(ROOT),
                    "SOURCE_SHA": "a" * 40, "GITHUB_REPOSITORY": "EndurantDevs/healthcare-mrf-api",
                    "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "2", "PUBLIC_CI_EXPORT_IMAGE": "1",
                    "TEST_IMAGE": "sha256:" + "b" * 64, "TEST_PYTHON": sys.executable,
                    "STUB_ROOT": temporary, "FAIL_CHECK": failure}
                script = CHECK_FUNCTIONS + r'''
git() { printf '%s\n' "$SOURCE_SHA"; }
log_call() {
  "$TEST_PYTHON" -c 'import json, os, sys
with open(os.environ["STUB_ROOT"] + "/calls", "a") as output:
    output.write(json.dumps(sys.argv[1:]) + "\n")' "$@"
}
docker() {
  log_call docker "$@"
  case "$1 ${2:-}" in
    build*) touch "$STUB_ROOT/image" ;;
    'image inspect') printf '%s\n' "$TEST_IMAGE" ;;
    'image ls') [ ! -f "$STUB_ROOT/image" ] || printf '%s\n' "$TEST_IMAGE" ;;
    'image rm') command rm "$STUB_ROOT/image" ;;
    run*)
      case "$*" in
        *scripts/smoke/provider_directory_runtime_contract.py*) [ "$FAIL_CHECK" != provider ] || return 17 ;;
        *RECEIPT_AUTHORITY_ROLE_ENV*) [ "$FAIL_CHECK" != files ] || return 17 ;;
        */run/healthporta-validation/cutover-ready.py*) [ "$FAIL_CHECK" != help ] || return 17 ;;
      esac ;;
  esac
  return 0
}
python3() { log_call python3 "$@"; }
run_container_package
'''
                result = subprocess.run(["bash", "-euc", script], env=environment, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0 if failure == "none" else 1 if failure == "missing-validator" else 17,
                                 result.stderr)
                calls = [json.loads(line) for line in (root / "calls").read_text().splitlines()]
                self.assertEqual(calls[-2:], [
                    ["docker", "image", "rm", "healthcare-mrf-api:ci-123-2"],
                    ["docker", "image", "ls", "--quiet", "healthcare-mrf-api:ci-123-2"],
                ])
                self.assertFalse((root / "image").exists())
                exports = [call for call in calls if call[0] == "python3"]
                if failure != "none":
                    self.assertFalse(exports)
                    continue
                self.assertEqual(exports, [["python3", str(ROOT / "scripts/source_image.py"), "export", environment["TEST_IMAGE"]]])
                portable = [call for call in calls if call[:2] == ["docker", "run"] and any(
                    "provider_directory_runtime_contract.py" in arg or "RECEIPT_AUTHORITY_ROLE_ENV" in arg
                    or "/run/healthporta-validation/cutover-ready.py" in arg for arg in call)]
                self.assertEqual(len(portable), 3)
                for call in portable:
                    self.assertEqual(call[call.index("--network") + 1], "none")
                    self.assertEqual(call[call.index("--platform") + 1], "linux/amd64")
                    self.assertIn(environment["TEST_IMAGE"], call)
                    self.assertIn("--rm", call)
                    self.assertLess(calls.index(call), calls.index(exports[0]))
                mounted = [call[index + 1] for call in portable for index, arg in enumerate(call) if arg == "--mount"]
                self.assertEqual(len(mounted), 4)
                for relative in IMAGE_VALIDATORS:
                    self.assertTrue(any(f"source={source}/{relative}," in mount and mount.endswith(",readonly") for mount in mounted))
                checks = portable[1][-1]
                for required in ("test -f /opt/alembic/versions/20260712120000_ptg2_v3_shared_schema.py",
                                 "test -f /opt/alembic/versions/20260810110000_ptg_wave_receipt_authority.py",
                                 "test -f /opt/process/ptg_wave_receipt_process_authority.py",
                                 "grep -Fq RECEIPT_AUTHORITY_ROLE_ENV"):
                    self.assertIn(required, checks)
                self.assertEqual(portable[2][-2:], ["/run/healthporta-validation/cutover-ready.py", "--help"])



if __name__ == "__main__":
    unittest.main()
