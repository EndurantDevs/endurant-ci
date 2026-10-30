"""Keep native refresh tests in the database lane and authenticate Python's wheel."""

import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
FUNCTIONS = (ROOT / "scripts/healthcare/check").read_text().rsplit("\ncase ", 1)[0]
CAPABILITY = FUNCTIONS.split("membership_refresh_native_test_paths() {\n", 1)[1].split("\n}\n", 1)[0]
NATIVE_TESTS = (
    "tests/test_registry_membership_refresh_execution_postgres.py",
    "tests/test_registry_membership_refresh_sources_postgres.py",
    "tests/test_registry_aca_refresh_anchors_postgres.py",
    "tests/test_registry_source_inventory_schema_postgres.py",
)
TRIGGERS = (
    "process/registry_membership_refresh_execution.py",
    "process/registry_membership_refresh_sources.py",
    *NATIVE_TESTS,
)
GIT = r'''
git() {
  if [[ "$1" = -C ]]; then shift 2; fi
  case "$1" in
    ls-tree) [[ "${!#}" != "${DELETED_PATH:-}" ]] || printf '%s\n' "${!#}" ;;
    ls-files) [[ "${!#}" != "${BAD_PATH:-}" || "${BAD_KIND:-}" != untracked ]] ;;
    diff) [[ "${!#}" != "${BAD_PATH:-}" || "${BAD_KIND:-}" != dirty ]] ;;
    *) return 99 ;;
  esac
}
'''


class MembershipRefreshChecks(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        binary = self.root / "bin"
        binary.mkdir()
        (binary / "python").symlink_to(sys.executable)
        self.environment = {
            **os.environ, "SOURCE_ROOT": str(self.root), "CI_ROOT": str(ROOT),
            "CI_ARTIFACT_ROOT": str(self.root), "RUNNER_TEMP": str(self.root),
            "PATH": str(binary) + os.pathsep + os.environ["PATH"], "CI_DEPS_READY": "1",
            "COVERAGE_BASE_SHA": "a" * 40, "HLTHPRT_DB_USER": "postgres",
            "HLTHPRT_DB_PASSWORD": "postgres",
        }

    def shell(self, script, **environment):
        return subprocess.run(
            ["bash", "-eu"], input=FUNCTIONS + "\n" + GIT + "\n" + script,
            capture_output=True, text=True, timeout=15,
            env={**self.environment, **environment},
        )

    def populate(self):
        paths = set(re.findall(r"(?:alembic|process|tests)/[a-zA-Z0-9_./]+", CAPABILITY))
        for relative in paths:
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        return paths

    def other_capabilities(self):
        return "\n".join(
            f"{name}() {{ :; }}"
            for name in re.findall(r"^([a-z_]+_native_test_paths?)\(\)", FUNCTIONS, re.MULTILINE)
            if name != "membership_refresh_native_test_paths"
        )

    def test_optional_capability_requires_all_tests_and_clean_direct_dependencies(self):
        script = "membership_refresh_native_test_paths\n"
        absent = self.shell(script)
        self.assertEqual((absent.returncode, absent.stdout), (0, ""), absent.stderr)
        for trigger in TRIGGERS:
            with self.subTest(only=trigger):
                path = self.root / trigger
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
                self.assertNotEqual(self.shell(script).returncode, 0)
                path.unlink()
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
            for kind in ("dirty", "untracked"):
                with self.subTest(path=relative, kind=kind):
                    self.assertNotEqual(self.shell(script, BAD_PATH=relative, BAD_KIND=kind).returncode, 0)
        path = self.root / NATIVE_TESTS[0]
        for target in (self.root / NATIVE_TESTS[1], self.root / "missing"):
            path.unlink()
            path.symlink_to(target)
            self.assertNotEqual(self.shell(script).returncode, 0)
        path.unlink()
        for relative in paths:
            (self.root / relative).unlink(missing_ok=True)
        self.assertNotEqual(self.shell(script, DELETED_PATH=NATIVE_TESTS[0]).returncode, 0)

    def test_native_refresh_prerequisites_are_also_executed(self):
        required_modules = set(re.findall(r"tests/test_[a-zA-Z0-9_]+_postgres\.py", CAPABILITY))
        self.assertEqual(required_modules, set(NATIVE_TESTS))
        self.populate()
        result = self.shell("membership_refresh_native_test_paths\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), list(NATIVE_TESTS))

    def test_python_main_authenticates_wheel_before_collection_and_refuses_bad_artifacts(self):
        wheel = "ptg2_address_canon-0.0.0-cp314-cp314-linux_x86_64.whl"
        artifacts = self.root / "artifacts"
        artifacts.mkdir()
        (artifacts / wheel).write_bytes(b"synthetic wheel\n")
        digest = artifacts / "ptg2-address-canon-wheel.sha256"
        script = self.other_capabilities() + r'''
mapfile() { capacity_tests=(tests/test_capacity_placeholder.py); }
prepare_debug_rust_binaries() { :; }
uv() { printf 'uv %s\n' "$*" >> "$SOURCE_ROOT/calls"; return "${UV_STATUS:-0}"; }
public_common_test_run() { printf 'tests\n' >> "$SOURCE_ROOT/calls"; }
timeout() { :; }
run_python_main 0
'''
        for state in ("valid", "digest_mismatch", "missing_digest", "install_failure"):
            with self.subTest(state=state):
                log = self.root / "calls"
                log.unlink(missing_ok=True)
                digest.write_text(hashlib.sha256((artifacts / wheel).read_bytes()).hexdigest() + "  " + wheel + "\n")
                if state == "digest_mismatch":
                    digest.write_text("0" * 64 + "  " + wheel + "\n")
                if state == "missing_digest":
                    digest.unlink()
                result = self.shell(script, CI_RUST_BINARIES=str(artifacts),
                                    UV_STATUS="31" if state == "install_failure" else "0")
                calls = log.read_text().splitlines() if log.exists() else []
                self.assertEqual(result.returncode == 0, state == "valid", result.stderr)
                if state == "valid":
                    self.assertEqual(len(calls), 3)
                    self.assertTrue(calls[0].startswith("uv --no-config pip install "))
                    self.assertIn("--no-build --no-deps --reinstall", calls[0])
                    self.assertTrue(calls[1].startswith("uv --no-config pip check "))
                    self.assertEqual(calls[2:], ["tests"])
                else:
                    self.assertNotIn("tests", calls)

    def test_refresh_tests_are_excluded_only_after_complete_registry_enrollment(self):
        self.populate()
        script = self.other_capabilities() + r'''
mapfile() { capacity_tests=(tests/test_capacity_placeholder.py); }
prepare_debug_rust_binaries() { :; }
install_address_canon_wheel() { :; }
public_common_test_run() { printf '%s\n' "$@" > "$SOURCE_ROOT/main-arguments"; }
timeout() { :; }
run_python_main 0
'''
        result = self.shell(script)
        self.assertEqual(result.returncode, 0, result.stderr)
        arguments = (self.root / "main-arguments").read_text().splitlines()
        ignored = {arguments[i + 1] for i, value in enumerate(arguments) if value == "--ignore"}
        self.assertTrue(set(NATIVE_TESTS) <= ignored)
        self.assertNotIn("tests/test_registry_membership_refresh_contract.py", ignored)
        registry = FUNCTIONS.split("run_network_registry_postgres() (\n", 1)[1].split("\n)\n", 1)[0]
        for relative in re.findall(r"tests/[a-zA-Z0-9_./]+", registry.split("\n  )", 1)[0]):
            (self.root / relative).touch()
        native = self.other_capabilities() + r'''
install_address_canon_wheel() { :; }
rustc() { printf 'rustc 1.98.1\n'; }
python() { :; }
run_scoped_archive_postgres() { printf '%s\n' "$@" > "$SOURCE_ROOT/registry-arguments"; }
validate_registry_native_report() { :; }
run_network_registry_postgres
'''
        result = self.shell(native)
        self.assertEqual(result.returncode, 0, result.stderr)
        arguments = (self.root / "registry-arguments").read_text().splitlines()
        self.assertIn("NETWORK_REGISTRY_TEST_DSN", arguments)
        for path in NATIVE_TESTS:
            self.assertEqual(arguments.count(path), 1)
        (self.root / NATIVE_TESTS[1]).unlink()
        (self.root / "main-arguments").unlink()
        result = self.shell(script)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "main-arguments").exists())


if __name__ == "__main__":
    unittest.main()
