"""Require complete native serving coverage before moving cases out of Python."""

import hashlib
import re
import unittest

from tests import test_membership_refresh_ci as membership_ci

FUNCTIONS = membership_ci.FUNCTIONS
CAPABILITY = FUNCTIONS.split("registry_serving_native_test_paths() {\n", 1)[1].split(
    "\n}\n", 1
)[0]
NATIVE_TESTS = tuple(re.findall(r"tests/[^\s]+\.py", CAPABILITY.split("\n  )", 1)[0]))
EXTERNAL_NODEIDS = (
    (
        "tests/test_network_core_layer_provider_postgres.py::"
        "test_real_core_layer_transport_preserves_two_custodied_generations[modern]"
    ),
    (
        "tests/test_registry_generic_source_successor_postgres.py::"
        "test_generic_source_successor_core_layer_transport"
    ),
)
CMS_NODEID = (
    "tests/test_registry_membership_refresh_cms_postgres.py::"
    "test_current_cms_edition_keeps_original_scope_and_pins_real_successor"
)
CMS_MODULE = CMS_NODEID.split("::", 1)[0]
SHORT_NATIVE_TESTS = tuple(path for path in NATIVE_TESTS if path != CMS_MODULE)
BASELINE_NODEID = (
    "tests/test_network_registry_postgres.py::"
    "test_identity_replay_archive_and_transaction_rollback"
)
BASELINE_REQUIRED = (
    "tests/test_network_registry_postgres.py",
    "process/network_registry_identity.py",
    "db/models/network_registry.py",
    "db/connection.py",
    "db/registry_schema.py",
    "alembic/versions/20261007010000_managed_network_registry.py",
)
TRIGGERS = (
    "api/network_compact_address.py",
    "process/network_membership_slices.py",
    "process/registry_source_inventory_store.py",
    "process/registry_fhir_catalog_observed_source.py",
    *NATIVE_TESTS,
)
GIT = r"""
git() {
  if [[ "$1" = -C ]]; then shift 2; fi
  case "$1" in
    ls-tree)
      printf '%s\n' "$*" >> "$SOURCE_ROOT/tree-queries"
      for path in "$@"; do
        [[ "$path" != "${DELETED_PATH:-}" ]] || printf '%s\n' "$path"
      done ;;
    ls-files) [[ "${!#}" != "${BAD_PATH:-}" || "${BAD_KIND:-}" != untracked ]] ;;
    diff) [[ "${!#}" != "${BAD_PATH:-}" || "${BAD_KIND:-}" != dirty ]] ;;
    *) return 99 ;;
  esac
}
"""


class RegistryServingNativeChecks(unittest.TestCase):
    setUp = membership_ci.MembershipRefreshChecks.setUp

    def shell(self, script, **environment):
        return membership_ci.MembershipRefreshChecks.shell(
            self, GIT + "\n" + script, **environment
        )

    def populate(self):
        paths = set(
            re.findall(r"(?:api|alembic|process|tests)/[a-zA-Z0-9_./]+", CAPABILITY)
        )
        for relative in paths:
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        return paths

    def other_capabilities(self):
        return "\n".join(
            f"{name}() {{ :; }}"
            for name in re.findall(
                r"^([a-z_]+_native_test_paths?)\(\)", FUNCTIONS, re.MULTILINE
            )
            if name != "registry_serving_native_test_paths"
        )

    def python_script(self):
        return (
            self.other_capabilities()
            + r"""
mapfile() { capacity_tests=(tests/test_capacity_placeholder.py); }
prepare_debug_rust_binaries() { :; }
install_address_canon_wheel() { :; }
public_common_test_run() { printf '%s\n' "$@" > "$SOURCE_ROOT/main-arguments"; }
timeout() { :; }
run_python_main 0
"""
        )

    def native_script(self):
        return (
            self.other_capabilities()
            + r"""
install_address_canon_wheel() { printf 'wheel\n' >> "$SOURCE_ROOT/native-order"; }
rustc() { printf 'rustc 1.98.1\n'; }
python() { :; }
run_scoped_archive_postgres() {
  printf 'tests\n' >> "$SOURCE_ROOT/native-order"
  printf '%s\n' "$@" > "$SOURCE_ROOT/registry-arguments"
  printf '%s\n' "${HLTHPRT_CMS_NPD_ADMISSION_TEST_DSN:-}" \
    "${HLTHPRT_PTG2_V4_MIGRATION_POSTGRES_DSN:-}" \
    "${HLTHPRT_PTG2_V4_MAP_POSTGRES_TEST:-}" \
    "${NETWORK_SQL_RECOVERY_TEST_DSN:-}" \
    "${NETWORK_COMPACT_TARGET_EXPLAIN_OUTPUT:-}" > "$SOURCE_ROOT/native-environment"
  [[ -z "${NETWORK_COMPACT_TARGET_EXPLAIN_OUTPUT:-}" || -d "$NETWORK_COMPACT_TARGET_EXPLAIN_OUTPUT" ]]
  return "${NATIVE_STATUS:-0}"
}
validate_registry_native_report() { printf 'report\n' >> "$SOURCE_ROOT/native-order"; }
run_network_registry_postgres
"""
        )

    def populate_existing_registry(self):
        registry = FUNCTIONS.split("run_network_registry_postgres() (\n", 1)[1].split(
            "\n)\n", 1
        )[0]
        paths = re.findall(r"tests/[a-zA-Z0-9_./]+", registry.split("\n  )", 1)[0])
        for relative in paths:
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        return paths

    def test_exact_native_roster_is_unique_closed_and_disjoint(self):
        self.assertEqual(len(NATIVE_TESTS), 74)
        self.assertEqual(len(set(NATIVE_TESTS)), 74)
        self.assertEqual(
            hashlib.sha256(("\n".join(NATIVE_TESTS) + "\n").encode()).hexdigest(),
            "025b1aa923f9c74b665658f94d8ee410a5cacb44c1214d87428db2bd5a3d8bcf",
        )
        for path in NATIVE_TESTS:
            self.assertRegex(path, r"^tests/test_[a-z0-9_]+\.py$")
        remaining = FUNCTIONS.replace(
            "registry_serving_native_test_paths() {\n" + CAPABILITY + "\n}", ""
        )
        external = remaining.split("registry_serving_external_native_nodeids() {\n", 1)[
            1
        ].split("\n}\n", 1)[0]
        remaining = remaining.replace(
            "registry_serving_external_native_nodeids() {\n" + external + "\n}", ""
        )
        cms = remaining.split("registry_cms_successor_native_nodeid() {\n", 1)[1].split(
            "\n}\n", 1
        )[0]
        self.assertEqual(re.findall(r"tests/[^\s]+::[^\s]+", cms), [CMS_NODEID])
        remaining = remaining.replace(
            "registry_cms_successor_native_nodeid() {\n" + cms + "\n}", ""
        )
        self.assertTrue(
            set(NATIVE_TESTS).isdisjoint(re.findall(r"tests/[^\s]+\.py", remaining))
        )
        self.assertEqual(
            tuple(re.findall(r"tests/[^\s']+::[^\s']+", external)), EXTERNAL_NODEIDS
        )
        self.assertTrue(
            {nodeid.split("::", 1)[0] for nodeid in EXTERNAL_NODEIDS}
            <= set(NATIVE_TESTS)
        )

    def test_absent_feature_checks_all_tracked_triggers_in_one_tree_query(self):
        result = self.shell("registry_serving_native_test_paths\n")
        self.assertEqual((result.returncode, result.stdout), (0, ""), result.stderr)
        queries = (self.root / "tree-queries").read_text().splitlines()
        self.assertEqual(len(queries), 1)
        self.assertEqual(
            queries[0].split(),
            ["ls-tree", "--name-only", "HEAD", "--", *TRIGGERS[:4], *NATIVE_TESTS],
        )

    def test_partial_deleted_or_substituted_capability_refuses_before_collection(self):
        script = "registry_serving_native_test_paths\n"
        result = self.shell(script)
        self.assertEqual((result.returncode, result.stdout), (0, ""), result.stderr)
        for trigger in (*TRIGGERS[:4], NATIVE_TESTS[0], NATIVE_TESTS[-1]):
            with self.subTest(only=trigger):
                path = self.root / trigger
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
                self.assertNotEqual(self.shell(script).returncode, 0)
                path.unlink()
        paths = self.populate()
        result = self.shell(script)
        self.assertEqual(
            (result.returncode, result.stdout.splitlines()),
            (0, list(NATIVE_TESTS)),
            result.stderr,
        )
        for relative in sorted(paths):
            with self.subTest(missing=relative):
                path = self.root / relative
                path.unlink()
                self.assertNotEqual(self.shell(script).returncode, 0)
                path.touch()
        for kind in ("dirty", "untracked"):
            self.assertNotEqual(
                self.shell(script, BAD_PATH=NATIVE_TESTS[0], BAD_KIND=kind).returncode,
                0,
            )
        path = self.root / NATIVE_TESTS[0]
        for target in (self.root / NATIVE_TESTS[1], self.root / "missing"):
            path.unlink()
            path.symlink_to(target)
            self.assertNotEqual(self.shell(script).returncode, 0)
        path.unlink()
        for relative in paths:
            (self.root / relative).unlink(missing_ok=True)
        self.assertNotEqual(
            self.shell(script, DELETED_PATH=NATIVE_TESTS[0]).returncode, 0
        )

    def test_complete_capability_moves_every_case_to_genuine_native_lane_once(self):
        self.populate()
        result = self.shell(self.python_script())
        self.assertEqual(result.returncode, 0, result.stderr)
        arguments = (self.root / "main-arguments").read_text().splitlines()
        ignored = [
            arguments[i + 1] for i, value in enumerate(arguments) if value == "--ignore"
        ]
        for path in SHORT_NATIVE_TESTS:
            self.assertEqual(ignored.count(path), 1)
        self.assertNotIn(CMS_MODULE, ignored)
        self.assertEqual(
            [
                arguments[i + 1]
                for i, value in enumerate(arguments)
                if value == "--deselect"
            ],
            [CMS_NODEID],
        )
        self.assertNotIn("tests/test_registry_membership_refresh_contract.py", ignored)
        existing = self.populate_existing_registry()
        result = self.shell(self.native_script())
        self.assertEqual(result.returncode, 0, result.stderr)
        arguments = (self.root / "registry-arguments").read_text().splitlines()
        self.assertIn("NETWORK_REGISTRY_TEST_DSN", arguments)
        for path in (*existing, *SHORT_NATIVE_TESTS):
            self.assertEqual(arguments.count(path), 1)
        deselected = [
            arguments[i + 1]
            for i, value in enumerate(arguments)
            if value == "--deselect"
        ]
        self.assertEqual(deselected, list(EXTERNAL_NODEIDS))
        self.assertNotIn(CMS_MODULE, arguments)
        environment = (self.root / "native-environment").read_text().splitlines()
        self.assertEqual(environment[0], environment[1])
        self.assertEqual(environment[0], environment[3])
        self.assertRegex(
            environment[0],
            r"^postgresql://postgres:postgres@127\.0\.0\.1:5440/hc_network_registry_",
        )
        self.assertEqual(environment[2], "1")
        self.assertTrue(
            environment[4].startswith(str(self.root) + "/healthcare-network-explain.")
        )
        self.assertFalse((self.root / environment[4]).exists())
        self.assertEqual(
            (self.root / "native-order").read_text().splitlines(),
            ["wheel", "tests", "report"],
        )
        (self.root / NATIVE_TESTS[0]).unlink()
        (self.root / "main-arguments").unlink()
        result = self.shell(self.python_script())
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "main-arguments").exists())

    def test_absent_capability_keeps_old_sources_and_native_environment_unchanged(self):
        result = self.shell(self.python_script())
        self.assertEqual(result.returncode, 0, result.stderr)
        arguments = (self.root / "main-arguments").read_text().splitlines()
        self.assertTrue(set(NATIVE_TESTS).isdisjoint(arguments))
        self.assertNotIn("--deselect", arguments)
        self.populate_existing_registry()
        result = self.shell(self.native_script())
        self.assertEqual(result.returncode, 0, result.stderr)
        arguments = (self.root / "registry-arguments").read_text().splitlines()
        self.assertTrue(set(NATIVE_TESTS).isdisjoint(arguments))
        self.assertEqual(
            (self.root / "native-environment").read_text().splitlines(),
            ["", "", "", "", ""],
        )

    def test_native_failure_removes_only_its_owned_explain_directory(self):
        self.populate()
        self.populate_existing_registry()
        neighboring = self.root / "healthcare-network-explain.preserve"
        neighboring.mkdir()
        sentinel = neighboring / "keep"
        sentinel.write_text("preserve unrelated state\n")
        result = self.shell(self.native_script(), NATIVE_STATUS="31")
        self.assertEqual(result.returncode, 31, result.stderr)
        directory = (self.root / "native-environment").read_text().splitlines()[4]
        self.assertTrue(
            directory.startswith(str(self.root) + "/healthcare-network-explain.")
        )
        self.assertFalse((self.root / directory).exists())
        self.assertEqual(sentinel.read_text(), "preserve unrelated state\n")
        self.assertEqual(
            (self.root / "native-order").read_text().splitlines(), ["wheel", "tests"]
        )

    def cms_script(self):
        return r"""
install_address_canon_wheel() { printf 'wheel\n' >> "$SOURCE_ROOT/cms-order"; }
run_scoped_archive_postgres() {
  printf 'tests\n' >> "$SOURCE_ROOT/cms-order"
  printf '%s\n' "$@" > "$SOURCE_ROOT/cms-arguments"
  printf '%s\n' "$HLTHPRT_CMS_NPD_ADMISSION_TEST_DSN" > "$SOURCE_ROOT/cms-environment"
  local report="${!#}" selected="$4" name classname
  name="${selected#*::}"
  classname="${selected%%::*}"
  classname="${classname%.py}"
  classname="${classname//\//.}"
  [[ "${REPORT_KIND:-valid}" != wrong-case ]] || name=unrelated_case
  if [[ "${REPORT_KIND:-valid}" != missing ]]; then
    printf '<testsuite tests="1"><testcase classname="%s" name="%s">' "$classname" "$name" > "$report"
    [[ "${REPORT_KIND:-valid}" != skipped ]] || printf '<skipped/>' >> "$report"
    printf '</testcase></testsuite>' >> "$report"
  fi
  return "${NATIVE_STATUS:-0}"
}
run_cms_successor_postgres
"""

    def test_long_cms_lane_requires_exact_passing_case_and_cleans_its_report(self):
        self.populate()
        neighboring = self.root / "healthcare-cms-successor.preserve"
        neighboring.mkdir()
        for state in ("valid", "missing", "wrong-case", "skipped", "native-failure"):
            with self.subTest(state=state):
                (self.root / "cms-order").unlink(missing_ok=True)
                result = self.shell(
                    self.cms_script(),
                    REPORT_KIND=state,
                    NATIVE_STATUS="31" if state == "native-failure" else "0",
                )
                self.assertEqual(
                    result.returncode == 0, state == "valid", result.stderr
                )
                arguments = (self.root / "cms-arguments").read_text().splitlines()
                self.assertEqual(
                    arguments[1:3],
                    [
                        "NETWORK_REGISTRY_TEST_DSN",
                        "postgresql://postgres:postgres@127.0.0.1:5440",
                    ],
                )
                self.assertEqual(arguments[3:5], [CMS_NODEID, "--junitxml"])
                self.assertTrue(arguments[0].startswith("hc_cms_successor_"))
                self.assertEqual(
                    (self.root / "cms-environment").read_text().strip(),
                    arguments[2] + "/" + arguments[0],
                )
                self.assertFalse((self.root / arguments[5]).parent.exists())
                self.assertTrue(neighboring.is_dir())
                self.assertEqual(
                    (self.root / "cms-order").read_text().splitlines(),
                    ["wheel", "tests"],
                )

    def test_absent_feature_runs_real_baseline_route_and_keeps_coverage(self):
        for relative in BASELINE_REQUIRED:
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        script = self.cms_script().replace(
            "run_cms_successor_postgres\n",
            r"""
prepare_debug_rust_binaries() { :; }
wait_for_service() { :; }
prepare_postgres() { :; }
require_base_sha() { COVERAGE_BASE_SHA=synthetic; }
timeout() { printf '%s\n' "$*" >> "$SOURCE_ROOT/cms-coverage"; }
run_postgres cms-successor
""",
        )
        result = self.shell(script)
        self.assertEqual(result.returncode, 0, result.stderr)
        arguments = (self.root / "cms-arguments").read_text().splitlines()
        self.assertEqual(arguments[3:5], [BASELINE_NODEID, "--junitxml"])
        self.assertFalse((self.root / arguments[5]).parent.exists())
        calls = (self.root / "cms-coverage").read_text().splitlines()
        self.assertEqual(len(calls), 2)
        self.assertIn("420s python ", calls[0])
        self.assertIn("/.coverage.postgres.cms-successor", calls[0])
        self.assertIn("--kind postgres --shard cms-successor", calls[1])
        for relative in BASELINE_REQUIRED:
            with self.subTest(missing=relative):
                path = self.root / relative
                path.unlink()
                (self.root / "cms-arguments").unlink(missing_ok=True)
                self.assertNotEqual(self.shell(script).returncode, 0)
                self.assertFalse((self.root / "cms-arguments").exists())
                path.touch()
        for kind in ("dirty", "untracked"):
            (self.root / "cms-arguments").unlink(missing_ok=True)
            self.assertNotEqual(
                self.shell(
                    script, BAD_PATH=BASELINE_REQUIRED[0], BAD_KIND=kind
                ).returncode,
                0,
            )
            self.assertFalse((self.root / "cms-arguments").exists())
        baseline = self.root / BASELINE_REQUIRED[0]
        baseline.unlink()
        baseline.symlink_to(self.root / BASELINE_REQUIRED[1])
        self.assertNotEqual(self.shell(script).returncode, 0)
        self.assertFalse((self.root / "cms-arguments").exists())
        baseline.unlink()
        baseline.touch()
        trigger = self.root / TRIGGERS[0]
        trigger.parent.mkdir(parents=True, exist_ok=True)
        trigger.touch()
        (self.root / "cms-arguments").unlink(missing_ok=True)
        self.assertNotEqual(self.shell(script).returncode, 0)
        self.assertFalse((self.root / "cms-arguments").exists())

    def test_long_cms_lane_keeps_normal_coverage_and_provenance(self):
        script = r"""
prepare_debug_rust_binaries() { :; }
wait_for_service() { :; }
prepare_postgres() { printf 'prepare %s\n' "$*" >> "$SOURCE_ROOT/cms-coverage"; }
run_cms_successor_postgres() { printf 'native\n' >> "$SOURCE_ROOT/cms-coverage"; }
require_base_sha() { COVERAGE_BASE_SHA=synthetic; }
timeout() { printf '%s\n' "$*" >> "$SOURCE_ROOT/cms-coverage"; }
run_postgres cms-successor
"""
        result = self.shell(script)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = (self.root / "cms-coverage").read_text().splitlines()
        self.assertEqual(calls[:2], ["prepare cms-successor", "native"])
        self.assertEqual(len(calls), 4)
        self.assertIn(
            "420s python "
            + str(membership_ci.ROOT)
            + "/scripts/healthcare/compact_coverage.py",
            calls[2],
        )
        self.assertIn("/.coverage.postgres.cms-successor", calls[2])
        self.assertIn(
            "write-shard-provenance --base synthetic --kind postgres --shard cms-successor",
            calls[3],
        )
        self.assertIn("/.coverage-provenance.postgres.cms-successor.json", calls[3])


if __name__ == "__main__":
    unittest.main()
