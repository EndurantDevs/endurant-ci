"""Check catalog enrollment and the bounded connected-test lane without services."""

import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
FUNCTIONS = (ROOT / "scripts/healthcare/check").read_text().rsplit("\ncase ", 1)[0]
CAPABILITY = FUNCTIONS.split("fhir_catalog_native_test_paths() {\n", 1)[1].split("\n}\n", 1)[0]
NATIVE_TESTS = tuple(
    f"tests/test_registry_fhir_catalog_{name}_postgres.py"
    for name in ("custody", "grant", "identity", "permissions", "publication", "read",
                 "reference_rollup", "retained", "sources", "store", "sync")
)
NODEID = "tests/test_registry_fhir_catalog_end_to_end.py::test_import_sync_query_replay_and_disappearance"


class CatalogChecks(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.binary = self.root / "bin"
        self.binary.mkdir()
        (self.binary / "python").symlink_to(sys.executable)
        self.environment = {
            **os.environ, "SOURCE_ROOT": str(self.root), "CI_ROOT": str(ROOT),
            "CI_ARTIFACT_ROOT": str(self.root),
            "PATH": str(self.binary) + os.pathsep + os.environ["PATH"],
            "RUNNER_TEMP": str(self.root), "HLTHPRT_DB_USER": "postgres",
            "HLTHPRT_DB_PASSWORD": "postgres", "COVERAGE_BASE_SHA": "a" * 40,
            "SOURCE_SHA": "b" * 40, "CI_REVISION": "b" * 40,
        }

    def shell(self, script, **environment):
        return subprocess.run(
            ["bash", "-eu"], input=FUNCTIONS + "\n" + script,
            text=True, capture_output=True, timeout=10,
            env={**self.environment, **environment},
        )

    def populate(self):
        paths = set(re.findall(r"(?:alembic|api|db|process|tests)/[a-zA-Z0-9_./]+", CAPABILITY))
        for relative in paths:
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        return paths

    def test_capability_is_optional_but_partial_dirty_and_indirect_trees_refuse(self):
        script = """
git() { [[ -z "${DIRTY_PATH:-}" || "${!#}" != "$DIRTY_PATH" ]]; }
fhir_catalog_native_test_paths
"""
        absent = self.shell(script)
        self.assertEqual((absent.returncode, absent.stdout), (0, ""), absent.stderr)
        paths = self.populate()
        complete = self.shell(script)
        self.assertEqual(complete.returncode, 0, complete.stderr)
        self.assertEqual(complete.stdout.splitlines(), list(NATIVE_TESTS))
        for relative in sorted(paths):
            with self.subTest(missing=relative):
                path = self.root / relative
                path.unlink()
                self.assertNotEqual(self.shell(script).returncode, 0)
                path.touch()
        path = self.root / NATIVE_TESTS[0]
        path.unlink()
        path.symlink_to(self.root / NATIVE_TESTS[1])
        self.assertNotEqual(self.shell(script).returncode, 0)
        path.unlink()
        path.touch()
        self.assertNotEqual(self.shell(script, DIRTY_PATH=NATIVE_TESTS[0]).returncode, 0)

    def test_workflow_probe_accepts_absence_and_complete_source_but_refuses_partial_source(self):
        def probe():
            return subprocess.run(
                ["bash", str(ROOT / "scripts/healthcare/check"), "fhir-catalog-capability"],
                text=True, capture_output=True, timeout=10, env=self.environment,
            )

        absent = probe()
        self.assertEqual((absent.returncode, absent.stdout), (0, "present=false\n"), absent.stderr)
        (self.binary / "git").write_text("#!/bin/sh\nexit 0\n")
        (self.binary / "git").chmod(0o755)
        self.populate()
        complete = probe()
        self.assertEqual((complete.returncode, complete.stdout), (0, "present=true\n"), complete.stderr)
        (self.root / NATIVE_TESTS[0]).unlink()
        partial = probe()
        self.assertNotEqual(partial.returncode, 0)
        self.assertNotIn("present=", partial.stdout)

    def test_database_tests_leave_python_main_and_join_registry_components(self):
        self.populate()
        stubs = "\n".join(
            f"{name}() {{ :; }}"
            for name in re.findall(r"^([a-z_]+_native_test_paths?)\(\)", FUNCTIONS, re.MULTILINE)
            if name != "fhir_catalog_native_test_paths"
        )
        script = stubs + "\n" + """
git() { :; }
mapfile() { capacity_tests=(tests/test_capacity_placeholder.py); }
prepare_debug_rust_binaries() { :; }
public_common_test_run() { printf '%s\\n' "$@" > "$SOURCE_ROOT/main-arguments"; }
timeout() { :; }
run_python_main 0
"""
        result = self.shell(script, CI_DEPS_READY="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        arguments = (self.root / "main-arguments").read_text().splitlines()
        ignored = {arguments[index + 1] for index, value in enumerate(arguments) if value == "--ignore"}
        self.assertTrue(set(NATIVE_TESTS) <= ignored)
        self.assertIn(NODEID.split("::")[0], ignored)
        self.assertNotIn("tests/test_registry_fhir_catalog_extraction.py", ignored)
        registry = FUNCTIONS.split("run_network_registry_postgres() (\n", 1)[1].split("\n)\n", 1)[0]
        self.assertIn('fhir_catalog_tests=$(fhir_catalog_native_test_paths)', registry)
        self.assertIn('registry_tests+=("$test_path")\n  done <<< "$fhir_catalog_tests"', registry)

    def test_only_exact_connected_node_gets_long_timeout_and_every_database_is_dropped(self):
        script = """
dropdb() { printf 'drop\\n' >> "$SOURCE_ROOT/calls"; }
createdb() { :; }
psql() { printf '%s\\n' "$*" >> "$SOURCE_ROOT/sql"; }
timeout() { printf '%s\\n' "$*" >> "$SOURCE_ROOT/calls"; return "$TEST_STATUS"; }
run_scoped_archive_postgres hc_catalog_test NETWORK_REGISTRY_TEST_DSN postgresql://postgres@127.0.0.1:5440 "$@"
"""
        selections = (
            (f"{NODEID} --junitxml report.xml", "1500s", True),
            (f"{NODEID} --junitxml report.xml tests/test_other.py", "420s", True),
            ("tests/test_registry_fhir_catalog_store_postgres.py", "420s", True),
            ("tests/test_registry_manual_source_swap_postgres.py", "420s", True),
            ("tests/test_network_registry_postgres.py", "420s", False),
            ("@catalog-selection", "420s", True),
        )
        (self.root / "catalog-selection").write_text(NATIVE_TESTS[0] + "::test_store\n")
        for arguments, deadline, trigram in selections:
            for status in (0, 124):
                with self.subTest(arguments=arguments, status=status):
                    for name in ("calls", "sql"):
                        (self.root / name).unlink(missing_ok=True)
                    result = self.shell("set -- " + arguments + "\n" + script, TEST_STATUS=str(status))
                    self.assertEqual(result.returncode, status, result.stderr)
                    calls = (self.root / "calls").read_text().splitlines()
                    self.assertEqual(calls[0], "drop")
                    self.assertEqual(calls[-1], "drop")
                    self.assertEqual(shlex.split(calls[1])[:2], ["--foreground", deadline])
                    self.assertEqual("pg_trgm" in (self.root / "sql").read_text(), trigram)

    def test_connected_lane_requires_exact_passing_report_and_current_source_receipt(self):
        git = self.binary / "git"
        git.write_text("#!/bin/sh\ncase \"$*\" in *rev-parse*) printf '%s\\n' \"$SOURCE_SHA\";; esac\n")
        git.chmod(0o755)
        script = """
fhir_catalog_native_test_paths() { echo tests/test_registry_fhir_catalog_sync_postgres.py; }
install_address_canon_wheel() { :; }
run_scoped_archive_postgres() {
  printf '%s\\n' "$@" > "$SOURCE_ROOT/integration-arguments"
  cp "$SOURCE_ROOT/input.xml" "$SOURCE_ROOT/fhir-catalog-integration.xml"
}
run_fhir_catalog_integration
"""
        name = NODEID.split("::")[1]
        for state in ("pass", "skip", "wrong", "duplicate", "stale-recipe"):
            with self.subTest(state=state):
                case = f'<testcase classname="tests.test_registry_fhir_catalog_end_to_end" name="{name}">'
                case += "<skipped/>" if state == "skip" else ""
                case += "</testcase>"
                if state == "wrong":
                    case = case.replace(name, "test_wrong")
                if state == "duplicate":
                    case *= 2
                (self.root / "input.xml").write_text(
                    f'<testsuites><testsuite tests="{2 if state == "duplicate" else 1}">{case}</testsuite></testsuites>'
                )
                receipt = self.root / "fhir-catalog-integration.json"
                receipt.write_text("stale receipt")
                result = self.shell(script, CI_REVISION=("c" if state == "stale-recipe" else "b") * 40)
                self.assertEqual(result.returncode == 0, state == "pass", result.stderr)
                self.assertEqual(receipt.exists(), state == "pass")
                arguments = (self.root / "integration-arguments").read_text().splitlines()
                self.assertEqual(arguments[-3:], [NODEID, "--junitxml", str(self.root / "fhir-catalog-integration.xml")])
                if state == "pass":
                    value = json.loads(receipt.read_text())
                    self.assertEqual(value["nodeid"], NODEID)
                    self.assertEqual(value["head_sha"], "b" * 40)
                    self.assertEqual(len(value["report_sha256"]), 64)

    def test_integration_route_keeps_preparation_and_has_no_component_or_coverage_fallback(self):
        script = """
prepare_debug_rust_binaries() { echo binaries; }
wait_for_service() { echo service; }
prepare_postgres() { echo migrations; }
run_fhir_catalog_integration() { echo integration; }
run_network_registry_postgres() { return 99; }
run_core_postgres() { return 99; }
timeout() { return 99; }
run_postgres fhir-catalog-integration
"""
        result = self.shell(script, CI_DEPS_READY="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([line for line in result.stdout.splitlines() if not line.startswith("CI_PHASE ")],
                         ["binaries", "service", "migrations", "integration"])


if __name__ == "__main__":
    unittest.main()
