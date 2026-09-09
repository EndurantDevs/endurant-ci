"""Exercise hosted artifact placement and exact-image cleanup without services."""

import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
CHECK_FUNCTIONS = (ROOT / "scripts/healthcare/check").read_text().rsplit("\ncase ", 1)[0]


class HealthcarePublicChecks(unittest.TestCase):
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
                    ["bash", "-euc", 'python() { return "$VENV_STATUS"; };\n'
                     'rm() { [ "$REMOVE_STATUS" = 0 ] || return "$REMOVE_STATUS"; command rm "$@"; };\n' + function +
                     "\nprepare_python_environment\n"], env=env, capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, expected, result.stderr)
                self.assertEqual(bool(list(Path(directory).iterdir())), bool(remove))

    def test_exact_image_cleanup_covers_failure_and_unavailable_docker(self):
        for build, remove, listing, expected in (
            (0, 0, 0, 0), (17, 0, 0, 17), (0, 23, 0, 1),
            (17, 23, 0, 17), (0, 0, 29, 1), (17, 0, 29, 17),
        ):
            with self.subTest(build=build, remove=remove, listing=listing):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    env = {**os.environ, "SOURCE_ROOT": temporary, "CI_ROOT": str(ROOT),
                           "SOURCE_SHA": "a" * 40, "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "2",
                           "STUB_ROOT": temporary, "BUILD_STATUS": str(build),
                           "REMOVE_STATUS": str(remove), "LIST_STATUS": str(listing)}
                    script = CHECK_FUNCTIONS + r'''
git() { printf '%s\n' "$SOURCE_SHA"; }
docker() {
  case "$1 ${2:-}" in
    build*) touch "$STUB_ROOT/image"; return "$BUILD_STATUS" ;;
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



if __name__ == "__main__":
    unittest.main()
