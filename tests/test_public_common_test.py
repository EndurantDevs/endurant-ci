"""Common test reuse preserves public callers, raw data and fresh provenance."""

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "common_render_workflow", ROOT / "scripts/render_workflow.py"
)
RENDERER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RENDERER)
SOURCE_SHA, ORIGIN_SHA = "a" * 40, "b" * 40


class PublicCommonTestChecks(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / "source"
        self.source.mkdir()
        self.ci = self.root / "ci"
        (self.ci / "scripts").mkdir(parents=True)
        for name in ("public_common_test.sh", "phase_timing.sh"):
            (self.ci / "scripts" / name).write_bytes(
                (ROOT / "scripts" / name).read_bytes()
            )
        (
            self.ci / "scripts/common_test_result.py"
        ).write_text("""import json, os, pathlib, sys
pathlib.Path(os.environ["ARGUMENT_LOG"]).write_text(json.dumps(sys.argv[1:]))
if "--output-file" in sys.argv:
    directory = pathlib.Path(sys.argv[sys.argv.index("--data-root") + 1])
    for index, argument in enumerate(sys.argv):
        if argument == "--data-root": directory = pathlib.Path(sys.argv[index + 1])
    directory.mkdir(parents=True, exist_ok=True)
    (directory / sys.argv[sys.argv.index("--output-file") + 1]).write_text("restored raw measurement")
""")
        self.environment = {
            **os.environ,
            "SOURCE_ROOT": str(self.source),
            "CI_ROOT": str(self.ci),
            "SOURCE_REPOSITORY": "example/project",
            "GITHUB_REPOSITORY": "example/project",
            "RUNNER_TEMP": str(self.root),
            "COMMON_TEST_EQUIVALENCE": "true",
            "COMMON_TEST_STEP": "Complete synthetic tests",
            "COMMON_TEST_PYTHON_BIN": sys.executable,
            "GH_TOKEN": "synthetic-read-only-token",
            "ARGUMENT_LOG": str(self.root / "arguments.json"),
            "SOURCE_SHA": SOURCE_SHA,
        }

    def shell(self, script, **environment):
        return subprocess.run(
            [
                "bash",
                "-euc",
                'source "$CI_ROOT/scripts/public_common_test.sh"\n' + script,
            ],
            cwd=self.source,
            env={**self.environment, **environment},
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )

    def test_disabled_or_unavailable_evidence_runs_the_original_command_and_preserves_failure(
        self,
    ):
        for setting in (
            {"COMMON_TEST_EQUIVALENCE": "false"},
            {"GH_TOKEN": ""},
            {"COMMON_TEST_STEP": ""},
        ):
            with self.subTest(setting=setting):
                result = self.shell(
                    'public_common_test_run healthcare-unit - bash -c \'printf "%s" "$1"; exit 17\' -- "two words"',
                    **setting,
                )
                self.assertEqual(result.returncode, 17, result.stderr)
                self.assertEqual(result.stdout, "two words")
                self.assertFalse((self.root / "arguments.json").exists())

    def test_stock_execution_never_receives_workflow_tokens(self):
        result = self.shell(
            "public_common_test_run healthcare-unit - bash -c 'test -z \"${GH_TOKEN:-}${GITHUB_TOKEN:-}${SOURCE_TOKEN:-}\"'",
            COMMON_TEST_EQUIVALENCE="false",
            GITHUB_TOKEN="synthetic-token",
            SOURCE_TOKEN="synthetic-token",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_bound_broker_evidence_supports_token_free_execution_and_source_authority(
        self,
    ):
        for repository in ("example/project", "example/controller"):
            with self.subTest(repository=repository):
                result = self.shell(
                    "public_common_test_run healthcare-unit - python -m pytest -q",
                    GH_TOKEN="",
                    GITHUB_REPOSITORY=repository,
                    COMMON_TEST_EVIDENCE_DIRECTORY=str(self.root / "evidence"),
                    COMMON_TEST_EVIDENCE_SHA256="c" * 64,
                    SOURCE_COMMON_TEST_AUTHORITY_PATH=str(self.root / "authority.json"),
                    SOURCE_COMMON_TEST_AUTHORITY_SHA256="d" * 64,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                arguments = json.loads((self.root / "arguments.json").read_text())
                self.assertEqual(
                    arguments[arguments.index("--source-repository") + 1],
                    "example/project",
                )
                self.assertEqual(
                    arguments[arguments.index("--evidence-directory") + 1],
                    str(self.root / "evidence"),
                )
                self.assertEqual(
                    arguments[arguments.index("--evidence-sha256") + 1], "c" * 64
                )
                self.assertEqual(
                    arguments[arguments.index("--source-authority") + 1],
                    str(self.root / "authority.json"),
                )
                self.assertEqual(
                    arguments[arguments.index("--source-authority-sha256") + 1],
                    "d" * 64,
                )

    def test_different_controller_requires_bound_evidence_and_source_defaults_to_current_repository(
        self,
    ):
        result = self.shell(
            "public_common_test_run healthcare-unit - bash -c 'printf stock'",
            GITHUB_REPOSITORY="example/controller",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "stock")
        self.assertFalse((self.root / "arguments.json").exists())
        environment = {**self.environment}
        environment.pop("SOURCE_REPOSITORY")
        result = subprocess.run(
            [
                "bash",
                "-euc",
                ('source "$CI_ROOT/scripts/public_common_test.sh"\n'
                "public_common_test_run drug-python - python -m pytest -q"),
            ],
            cwd=self.source,
            env=environment,
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        arguments = json.loads((self.root / "arguments.json").read_text())
        self.assertEqual(
            arguments[arguments.index("--source-repository") + 1], "example/project"
        )

    def test_engine_receives_exact_command_raw_layout_and_actual_native_inputs(self):
        native = self.source / "support/ptg2_scanner/target/debug/ptg2_scanner"
        native.parent.mkdir(parents=True)
        native.write_bytes(b"synthetic compiled binary")
        extension = self.source / "synthetic_extension.so"
        extension.write_bytes(b"synthetic compiled extension")
        coverage = self.root / "coverage/.coverage.main.0"
        result = self.shell(
            'public_common_test_run healthcare-python-main-0 "$RAW_COVERAGE" '
            "timeout --foreground 420s python -m pytest -q tests/test_example.py",
            RAW_COVERAGE=str(coverage),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        arguments = json.loads((self.root / "arguments.json").read_text())
        self.assertEqual(
            arguments[arguments.index("--") + 1 :],
            [
                "timeout",
                "--foreground",
                "420s",
                "python",
                "-m",
                "pytest",
                "-q",
                "tests/test_example.py",
            ],
        )
        self.assertIn(".github/workflows/healthcare.yml", arguments)
        self.assertIn("support/ptg2_scanner/target/debug/ptg2_scanner", arguments)
        self.assertIn("synthetic_extension.so", arguments)
        self.assertEqual(arguments[arguments.index("--output-file") + 1], coverage.name)
        self.assertEqual(coverage.read_text(), "restored raw measurement")
        contexts = [
            arguments[index + 1]
            for index, value in enumerate(arguments)
            if value == "--environment"
        ]
        self.assertFalse(
            any(
                word in name
                for name in contexts
                for word in ("SECRET", "TOKEN", "PASSWORD", "KEY")
            )
        )
        self.assertNotIn("SOURCE_SHA", contexts)
        self.assertNotIn("BASE_SHA", contexts)

    def test_postgres_inventory_uses_actual_container_and_read_only_server_metadata(
        self,
    ):
        result = self.shell(
            "public_common_test_run drug-postgres - python -m pytest -q tests/test_database.py"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        arguments = json.loads((self.root / "arguments.json").read_text())
        commands = {
            arguments[index + 1]: json.loads(arguments[index + 2])
            for index, argument in enumerate(arguments)
            if argument == "--toolchain-command"
        }
        self.assertEqual(commands["postgres"][:2], [sys.executable, "-c"])
        probe = commands["postgres"][2]
        compile(probe, "postgres-inventory", "exec")
        self.assertIn('os.environ["COMMON_TEST_POSTGRES_CONTAINER"]', probe)
        self.assertIn('"{{.Image}}"', probe)
        self.assertIn("transaction(readonly=True)", probe)
        self.assertIn("SELECT version()", probe)
        self.assertIn("FROM pg_extension ORDER BY extname", probe)
        self.assertNotIn("--output-file", arguments)

    def test_fresh_coverage_reference_retains_original_execution_without_changing_source_or_base(
        self,
    ):
        scope = "healthcare-capacity"
        directory = (
            self.root
            / "common-test-measurements"
            / hashlib.sha256(scope.encode()).hexdigest()
        )
        directory.mkdir(parents=True)
        current = {
            "schema": "test-result-equivalence-v1",
            "source": {"commit": SOURCE_SHA},
            "producer": {"run_id": 200, "run_attempt": 1},
            "fingerprint": "c" * 64,
            "executed": False,
        }
        original = {
            "source": {"commit": ORIGIN_SHA},
            "producer": {"run_id": 100, "run_attempt": 2},
            "fingerprint": current["fingerprint"],
        }
        origin = {"origin": original, "artifact_id": 300, "archive_sha256": "d" * 64}
        measurement = directory / "measurement.json"
        measurement.write_text(json.dumps({"measurement": current, "origin": origin}))
        original_bytes = measurement.read_bytes()
        provenance = self.root / "coverage-provenance.json"
        provenance.write_text(
            json.dumps(
                {
                    "head_sha": SOURCE_SHA,
                    "base_sha": "e" * 40,
                    "coverage_sha256": "f" * 64,
                }
            )
        )
        result = self.shell(
            'public_common_test_provenance "$PROVENANCE" healthcare-capacity',
            PROVENANCE=str(provenance),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(provenance.read_bytes())
        self.assertEqual(value["head_sha"], SOURCE_SHA)
        self.assertEqual(value["base_sha"], "e" * 40)
        self.assertEqual(value["coverage_sha256"], "f" * 64)
        reference = value["common_test_execution"]
        self.assertFalse(reference["executed"])
        self.assertEqual(reference["source"]["commit"], SOURCE_SHA)
        self.assertEqual(
            reference["origin"],
            {**original, "artifact_id": 300, "archive_sha256": "d" * 64},
        )
        self.assertEqual(
            reference["measurement_sha256"], hashlib.sha256(original_bytes).hexdigest()
        )
        self.assertEqual(measurement.read_bytes(), original_bytes)
        result = self.shell(
            'public_common_test_provenance "$PROVENANCE" healthcare-capacity',
            PROVENANCE=str(provenance),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("reference already exists", result.stderr)

    def test_no_measurement_keeps_stock_provenance_and_wrong_source_is_rejected(self):
        provenance = self.root / "coverage-provenance.json"
        provenance.write_text(json.dumps({"head_sha": SOURCE_SHA}))
        original = provenance.read_bytes()
        result = self.shell(
            'public_common_test_provenance "$PROVENANCE" drug-python',
            PROVENANCE=str(provenance),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(provenance.read_bytes(), original)
        directory = (
            self.root
            / "common-test-measurements"
            / hashlib.sha256(b"drug-python").hexdigest()
        )
        directory.mkdir(parents=True)
        (directory / "measurement.json").write_text(
            json.dumps({"measurement": {"source": {"commit": ORIGIN_SHA}}})
        )
        result = self.shell(
            'public_common_test_provenance "$PROVENANCE" drug-python',
            PROVENANCE=str(provenance),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("different source execution", result.stderr)
        self.assertEqual(provenance.read_bytes(), original)

    def test_capacity_still_compacts_and_generates_current_base_provenance_after_common_tests(
        self,
    ):
        functions = (
            (ROOT / "scripts/healthcare/check").read_text().rsplit("\ncase ", 1)[0]
        )
        script = (
            functions
            + r"""
install_python_dependencies() { :; }
mapfile() { capacity_tests=(tests/test_example.py); }
timeout() { shift 2; "$@"; }
python() {
  case "$*" in
    *compact_coverage.py*) printf 'compact\n' >> "$PHASE_LOG" ;;
    *write-shard-provenance*) printf 'current-base-provenance %s\n' "$*" >> "$PHASE_LOG" ;;
    *) return 99 ;;
  esac
}
run_capacity
"""
        )
        phase_log = self.root / "phases"
        result = subprocess.run(
            ["bash", "-eu"],
            input=script,
            cwd=self.source,
            env={
                **self.environment,
                "CI_ARTIFACT_ROOT": str(self.root / "coverage"),
                "COVERAGE_BASE_SHA": "e" * 40,
                "PHASE_LOG": str(phase_log),
                "CAPACITY_TEST_PATHS": "tests/test_example.py",
            },
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        phases = phase_log.read_text().splitlines()
        self.assertEqual(phases[0], "compact")
        self.assertIn("--base " + "e" * 40, phases[1])
        arguments = json.loads((self.root / "arguments.json").read_text())
        self.assertEqual(
            arguments[arguments.index("--scope") + 1], "healthcare-capacity"
        )
        self.assertIn("tests/test_example.py", arguments)

    def test_generated_callers_have_one_read_only_upload_per_common_job_with_metadata_guards(
        self,
    ):
        caller = self.root / "caller.yml"
        original = {
            "on": {"pull_request": {"branches": ["main"]}},
            "jobs": {
                "smoke": {
                    "name": "portable checks",
                    "runs-on": "ubuntu-latest",
                    "steps": [{"run": "echo synthetic"}],
                }
            },
        }
        for kind, common_jobs in (
            (
                "healthcare",
                {"python-tests", "capacity-evidence", "address-canonical-db-tests"},
            ),
            ("drug", {"validate"}),
        ):
            with self.subTest(kind=kind):
                caller.write_text(yaml.safe_dump(original))
                rendered = RENDERER.render_workflow(kind, "1" * 40, caller)
                document = yaml.safe_load(rendered)
                for identifier, job in document["jobs"].items():
                    uploads = [
                        step
                        for step in job["steps"]
                        if step.get("name") == "Upload verified common-test measurements"
                    ]
                    self.assertEqual(len(uploads), int(identifier in common_jobs))
                    if identifier in common_jobs:
                        self.assertTrue(
                            all(
                                value == "read"
                                for value in job.get("permissions", {}).values()
                            )
                        )
                        self.assertIn(RENDERER.METADATA_ONLY, job["if"])
                        self.assertEqual(
                            uploads[0],
                            {
                                "name": "Upload verified common-test measurements",
                                "if": "success() && env.COMMON_TEST_ARTIFACT_NAME != ''",
                                "uses": "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
                                "with": {
                                    "name": "${{ env.COMMON_TEST_ARTIFACT_NAME }}",
                                    "path": "${{ runner.temp }}/common-test-measurements",
                                    "if-no-files-found": "error",
                                    "retention-days": 1,
                                },
                            },
                        )
                caller.write_text(rendered)
                self.assertEqual(
                    RENDERER.render_workflow(kind, "1" * 40, caller), rendered
                )


if __name__ == "__main__":
    unittest.main()
