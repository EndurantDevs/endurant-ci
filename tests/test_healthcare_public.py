"""Exercise hosted artifact placement and exact-image cleanup without services."""

import base64
import hashlib
import json
import os
import py_compile
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import unittest
from importlib import metadata
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CHECK_FUNCTIONS = (ROOT / "scripts/healthcare/check").read_text().rsplit("\ncase ", 1)[0]
IMAGE_VALIDATORS = (
    "scripts/smoke/provider_directory_runtime_contract.py",
    "scripts/research/provider_directory_coverage_audit.py",
    "scripts/research/provider_directory_fhir_harness.py",
    "scripts/devops/ptg2_strict_v3_cutover_ready.py",
)
REQUIRED_IMPORT_NATIVE_TESTS = (
    "tests/test_custom_import_snowflake_statement_snapshot_postgres.py",
    "tests/test_custom_import_identical_children_postgres.py",
    "tests/test_custom_import_grouped_child_read_postgres.py",
)
REGISTRY_SOURCE_CUSTODY_TESTS = (
    "tests/test_network_cms_registry_source_pair_postgres.py",
    "tests/test_network_cms_registry_complete_publication_postgres.py",
    "tests/test_network_registry_cms_prepared_pair_postgres.py",
    "tests/test_network_registry_cms_prepared_cleanup_postgres.py",
    "tests/test_network_registry_cms_source_transaction_postgres.py",
    "tests/test_network_registry_cms_capture_lock_postgres.py",
    "tests/test_cms_publication_source_session_postgres.py",
    "tests/test_provider_directory_cms_applied_archive_readiness_postgres.py",
)
REGISTRY_REQUIRED_TARGET_TESTS = (
    "tests/test_registry_required_target_store_postgres.py",
    "tests/test_registry_required_target_review_store_postgres.py",
    "tests/test_registry_required_target_coverage_postgres.py",
)
REGISTRY_ADDRESS_EQUIVALENCE_TEST = "tests/test_network_cms_registry_address_equivalence_postgres.py"
REGISTRY_PTG_SCOPE_TESTS = (
    "tests/test_registry_ptg_graph_reader_postgres.py",
    "tests/test_registry_ptg_scope_engine_postgres.py",
)
REGISTRY_PTG_SCOPE_CAPABILITY = (
    *REGISTRY_PTG_SCOPE_TESTS,
    "tests/test_registry_ptg_cohort_authority_postgres.py",
    "process/registry_ptg_cohort_authority.py",
    "process/registry_ptg_graph_reader.py",
    "process/registry_ptg_producer_scope.py",
    "process/registry_ptg_scope_engine.py",
    "process/registry_ptg_scope_runtime.py",
    "process/registry_company_approval_fence.py",
    "process/registry_approval_store.py",
    "alembic/versions/20261009010000_registry_ptg_producer_scope.py",
    "support/ptg2_scanner/Cargo.toml", "support/ptg2_scanner/Cargo.lock",
    "support/ptg2_scanner/pyproject.toml", "support/ptg2_scanner/src/lib.rs",
    "support/ptg2_scanner/src/registry_ptg_graph_witness.rs",
    "support/ptg2_scanner/src/registry_ptg_graph_python.rs",
    "tests/test_result_archive_published_authority_postgres.py",
    "tests/ptg_frozen_test_support.py", "tests/ptg2_tax_identity_source_projection_fixture.py",
)

REGISTRY_PTG_OFFICE_TEST = "tests/test_registry_ptg_office_capture_postgres.py"
REGISTRY_PTG_OFFICE_WITNESS_TEST = "tests/test_registry_ptg_office_witness_postgres.py"
REGISTRY_PTG_OFFICE_SOURCES = (
    "process/registry_ptg_office_capture.py",
    "process/registry_ptg_office_witness.py",
    "tests/test_registry_ptg_office_capture.py",
    "tests/test_registry_ptg_office_witness.py",
    REGISTRY_PTG_OFFICE_TEST,
    "support/ptg2_scanner/src/registry_ptg_capture_input.rs",
    "support/ptg2_scanner/tests/registry_ptg_capture_input.rs",
)
REGISTRY_PTG_OFFICE_CAPABILITY = (
    *REGISTRY_PTG_OFFICE_SOURCES,
    "support/ptg2_scanner/src/network_membership_codec.rs",
    "support/ptg2_scanner/src/npi_identifier.rs",
    "tests/test_network_custom_address_source_postgres.py",
    "tests/test_network_serving_schema_postgres.py",
    "tests/test_registry_approval_store_postgres.py",
    "tests/test_registry_candidate_composition_postgres.py",
    "tests/test_registry_retained_site_adoption_postgres.py",
)
COMPANY_ASSERTION_CAPABILITY = (
    "db/models/company_registry_assertions.py", "process/company_registry_assertion_values.py",
    "alembic/versions/20261009020000_company_registry_assertions.py",
    "tests/test_company_registry_assertion_values.py", "tests/test_company_registry_assertion_store.py",
    "tests/test_company_registry_assertions_postgres.py",
)
COMPANY_ASSERTION_DEPENDENCIES = (
    "process/registry_record_store.py", "process/registry_approval_store.py",
    "process/registry_management_permissions.py", "tests/test_result_archive_published_authority_postgres.py",
)
PROFILE_MAINTENANCE_CAPABILITY = (
    "process/provider_directory_profile_control_maintenance.py",
    "tests/test_provider_directory_profile_control_maintenance.py",
    "tests/test_provider_directory_profile_control_maintenance_postgres.py",
)
PROFILE_MAINTENANCE_DEPENDENCIES = (
    "process/provider_directory_fhir.py", "process/provider_directory_profile.py",
    "process/provider_directory_profile_capacity_types.py",
    "process/provider_directory_profile_capacity_control_operations.py",
    "process/provider_directory_profile_capacity_control_identity.py", "db/connection.py",
    "tests/provider_directory_profile_artifact_pg_fixtures.py", "tests/provider_directory_profile_delta_test_support.py",
)

REGISTRY_PTG_GRAPH_EXPORTS = (
    "plan_registry_ptg_graph_locator_pages", "plan_registry_ptg_graph_member_pages",
    "verify_registry_ptg_graph_batch",
)


def _directory_wheel_script():
    """Exercise the real wheel installer while isolating database and compiler processes."""
    return r'''
python() {
  case "$*" in
    '-m maturin build '*)
      printf 'build\t%s\n' "$*" >> "$CALL_LOG"
      [ "$FAIL_STAGE" != build ] || return 17
      touch "${!#}/synthetic.whl" ;;
    *) command python3 "$@" ;;
  esac
}
uv() {
  printf 'uv\t%s\n' "$*" >> "$CALL_LOG"
  [ "$FAIL_STAGE" != install ] || return 17
  touch "$WHEEL_INSTALLED"
}
timeout() {
  test -f "$WHEEL_INSTALLED" || return 23
  printf 'tests\t%s\n' "$*" >> "$CALL_LOG"
}
env() {
  while [[ "$1" = -u || "$1" = *=* ]]; do
    if [ "$1" = -u ]; then shift 2; else shift; fi
  done
  "$@"
}
run_owned_provider_directory_postgres() { shift 2; timeout --foreground 295s python -m pytest -q "$@"; }
run_scoped_archive_postgres() { :; }
run_scoped_archive_postgres_body() { run_scoped_archive_postgres "$@"; }
prepare_debug_rust_binaries() { :; }
wait_for_service() { :; }
prepare_postgres() { :; }
require_base_sha() { :; }
run_postgres "$ROUTE_LANE"
'''


def _import_family_environment(root):
    return {
        **os.environ,
        "SOURCE_ROOT": str(root), "CI_ROOT": str(ROOT), "RUNNER_TEMP": str(root),
        "CALL_LOG": str(root / "calls"), "COVERAGE_FILE": str(root / "earlier-coverage"),
        "BASE_COVERAGE": str(root / "earlier-coverage"), "PAIR_ROOT": str(root),
        "PYTHON_BIN": sys.executable, "HLTHPRT_DB_USER": "synthetic",
        "HLTHPRT_DB_PASSWORD": "synthetic",
    }


def _import_family_pair_script():
    return CHECK_FUNCTIONS + r'''
run_scoped_archive_postgres_body() {
  local test_path=$4
  trap 'printf "finished\t%s\n" "$test_path" >> "$CALL_LOG"' EXIT
  printf 'started\t%s\t%s\t%s\n' "$test_path" "$1" "$COVERAGE_FILE" >> "$CALL_LOG"
  touch "$PAIR_ROOT/${test_path##*/}.started"
  while [ ! -f "$PAIR_ROOT/test_custom_import_identical_children_postgres.py.started" ] ||
        [ ! -f "$PAIR_ROOT/test_custom_import_grouped_child_read_postgres.py.started" ]; do sleep 0.01; done
  printf '%s\n' "$test_path" > "$COVERAGE_FILE"
  [ "$test_path" != "$FAILURE_PATH" ] || exit 17
  sleep 0.05
  exit 0
}
timeout() {
  test "$1:$2" = '--foreground:295s'
  shift 2
  "$@"
}
python() {
  test "$#" = 6
  test "${*:1:4}" = '-m coverage combine --append'
  test "$COVERAGE_FILE" = "$BASE_COVERAGE"
  test "$(< "$COVERAGE_FILE")" = earlier
  test "$(grep -c '^finished' "$CALL_LOG")" = 2
  test -f "$5" && test -f "$6"
  printf 'combine\t%s\t%s\t%s\n' "$COVERAGE_FILE" "$5" "$6" >> "$CALL_LOG"
  [ "$COMBINE_STATUS" = 0 ] || return "$COMBINE_STATUS"
  cat "$5" "$6" >> "$COVERAGE_FILE"
  rm -- "$5" "$6"
}
run_import_family_postgres postgresql://synthetic
'''


def _import_family_process_files(root):
    (root / "worker.py").write_text('''
import os
from pathlib import Path
import signal
import sys
import time
name = Path(sys.argv[1]).name
root = Path(os.environ["PAIR_ROOT"])
def finish(_signum, _frame):
    time.sleep(0.1)
    assert Path(os.environ["COVERAGE_FILE"]).parent.is_dir()
    (root / (name + ".finished")).touch()
    sys.exit(0)
signal.signal(signal.SIGTERM, finish)
(root / (name + ".started")).write_text(str(os.getpid()))
while True:
    time.sleep(0.01)
''')
    (root / "runner.py").write_text('''
import importlib.util
import os
from pathlib import Path
import sys
spec = importlib.util.spec_from_file_location("supervisor", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.RUN_SECONDS, module.TERM_SECONDS, module.DRAIN_SECONDS = 3, 0.2, 1
sys.exit(module.supervise([sys.executable, str(Path(os.environ["PAIR_ROOT"]) / "worker.py"), sys.argv[3]], sys.argv[2]))
''')


def _import_family_cancellation_script(functions):
    return functions + r'''
create_test_database() { printf 'create\t%s\n' "$1" >> "$CALL_LOG"; }
drop_test_database() {
  local test_path=$(< "$PAIR_ROOT/$1.owner")
  test -f "$PAIR_ROOT/${test_path##*/}.finished"
  printf 'drop\t%s\n' "$1" >> "$CALL_LOG"
}
psql() { :; }
python() {
  test "$1" = "$CI_ROOT/scripts/healthcare/supervise_installed.py"
  printf '%s\n' "$3" > "$PAIR_ROOT/$HLTHPRT_DB_DATABASE.owner"
  exec "$PYTHON_BIN" "$PAIR_ROOT/runner.py" "$@"
}
timeout() { printf 'unexpected combine\n' >> "$CALL_LOG"; return 99; }
run_import_family_postgres postgresql://synthetic
'''


def _import_family_signal_hook(functions, location, signum):
    hook = r'''
    while [ ! -f "$PAIR_ROOT/${test_path##*/}.started" ]; do sleep 0.01; done
    "$PYTHON_BIN" -c 'import os, signal; print("receiver", os.getppid(), flush=True); os.kill(os.getppid(), signal.SIGNAL_NAME)'
'''.replace("SIGNAL_NAME", signum)
    if location == "pair":
        anchor = '    family_test_pids+=("$!")\n'
        hook = hook.replace(
            'while [ ! -f "$PAIR_ROOT/${test_path##*/}.started" ];',
            'while [ ! -f "$PAIR_ROOT/test_custom_import_identical_children_postgres.py.started" ] || '
            '[ ! -f "$PAIR_ROOT/test_custom_import_grouped_child_read_postgres.py.started" ];',
        )
        hook = '    if [[ "$test_path" = *grouped_child_read_postgres.py ]]; then\n' + hook + '    fi\n'
    else:
        anchor = '    wait "$!"\n'
        hook = hook.replace('${test_path##*/}', '${1##*/}')
    assert functions.count(anchor) == 1
    dispatch = '    family_test_pids+=("$!")\n'
    observation = r'''    printf 'dispatch\t%s\t%s\t%s\n' "$test_path" "$!" "$(/bin/ps -p "$!" -o ppid=)" >> "$CALL_LOG"
'''
    assert functions.count(dispatch) == 1
    functions = functions.replace(dispatch, observation + dispatch)
    return functions.replace(anchor, hook + anchor)


def _stop_import_family_workers(root):
    for receipt in root.glob("*.started"):
        pid = int(receipt.read_text())
        process = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "args="],
                                 capture_output=True, text=True, check=False)
        if str(root / "worker.py") in process.stdout:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


class HealthcarePublicChecks(unittest.TestCase):
    def test_registry_native_family_requires_complete_source_and_real_bindings(self):
        paths = tuple("tests/" + name + "_postgres.py" for name in (
            "test_network_registry", "test_network_serving_schema", "test_registry_record_store",
            "test_registry_targets", "test_registry_management_routes", "test_network_legacy_alias_adoption",
            "test_network_address_projection", "test_network_membership_copy",
            "test_network_membership_candidate_lifecycle", "test_network_membership_validation",
            "test_network_membership_candidate_indexes", "test_network_membership_serving_indexes",
            "test_network_membership_publication", "test_registry_source_observation_store",
            "test_registry_identity_materialization", "test_registry_source_admission", "test_registry_approval_store",
            "test_registry_approved_company_read",
            "test_registry_approval_preview", "test_network_membership_writer_closure", "test_network_serving_read",
            "test_network_address_read_scope", "test_registry_source_import",
            "test_network_membership_pipeline", "test_network_serving_routes",
            "test_registry_company_links",
            "test_manual_provider_identity_store", "test_manual_location_identity_store",
            "test_network_fhir_membership_source",
            "test_network_legacy_membership_source",
            "test_registry_management_permissions", "test_network_membership_draft_store",
            "test_registry_membership_approval",
            "test_network_approved_membership_source", "test_network_custom_address_source",
            "test_registry_publication_queue", "test_registry_publication_execution",
            "test_registry_publication_http",
            "test_registry_candidate_composition", "test_registry_issuer_resolution", "test_registry_issuer_http",
            "test_registry_retained_site_adoption", "test_registry_site_binding_store",
            "test_registry_site_membership_composition", "test_registry_source_site_catalog",
            "test_registry_source_site_http",
            "test_registry_planfinder_admission",
            "test_company_network_link_store",
            "test_network_source_binding_schema", "test_network_source_binding_store",
            "test_registry_network_binding_approval", "test_registry_source_binding_http",
            "test_network_approved_source_bindings",
            "test_provider_directory_insurance_network_batch", "test_provider_directory_cms_network_batch",
        ))
        paths = paths[:4] + REGISTRY_REQUIRED_TARGET_TESTS + paths[4:]
        paths += (
            "tests/test_registry_source_fetch.py",
            "tests/test_network_manual_provider_read_postgres.py", "tests/test_network_provider_routes_postgres.py",
            "tests/test_registry_network_catalog_read_postgres.py",
            "tests/test_registry_source_recipe_store_postgres.py", "tests/test_registry_source_recipe_composition_postgres.py",
            "tests/test_registry_manual_undo_postgres.py",
            "tests/test_registry_source_selection_receipt_postgres.py",
            "tests/test_network_initial_source_office_bindings_postgres.py",
            "tests/test_provider_directory_cms_resource_batch_postgres.py",
        )
        paths += REGISTRY_SOURCE_CUSTODY_TESTS
        self.assertEqual(len(paths), len(set(paths)))
        exports = (
            "encode_network_membership_batch", "encode_cms_mlr_observations", "encode_cms_planfinder_observations",
            "validate_network_catalog_batch", "validate_company_network_assertions",
            "encode_network_source_binding_batch", "build_registry_network_coverage", "parse_registry_target_ledger",
            "encode_registry_target_ledger_artifact", "encode_registry_required_target_review_artifact",
            "validate_registry_required_target_review_artifacts", "validate_registry_required_target_ledger_artifact",
            "extract_fhir_network_identity_batch", "encode_fhir_network_identity_batch",
            "canonicalize_batch", "canon_version",
        )
        workflow = yaml.safe_load((ROOT / ".github/workflows/healthcare.yml").read_text())
        setup = next(step for step in workflow["jobs"]["address-canonical-db-tests"]["steps"]
                     if step["name"] == "Prepare source and toolchain")
        self.assertEqual(setup["with"], {"rust": "${{ matrix.shard == 'core-services' || matrix.shard == 'core-ptg' || matrix.shard == 'directory-source' || startsWith(matrix.shard, 'registry-') }}"})
        cases = [(lane, "", "") for lane in (
            "core-services", "core", "core-imports", "core-ptg", "directory-source", "directory-storage",
            "directory-address", "profile-storage", "profile-publication",
            *(f"registry-{index}" for index in range(8)),
        )]
        cases += [("core", path, "") for path in paths]
        cases += [("core", "only:" + path, "") for path in paths]
        cases += [("core", "", "missing-report:" + path) for path in paths]
        cases += [("core", absent, "") for absent in ("all", "all-with-model")]
        cases += [("core", "", stage) for stage in (
            "build", "install", "pytest", "drop", "rust-version", "asyncpg", *exports,
            "skipped", "error", "failure", "summary-skipped", "no-tests", "bad-count", "empty-report",
            "invalid-report", "large-report", "report-cleanup", "stale-registry-roster", "no-classname", "more-management-cases",
        )]
        cases += [("core", "", name + ":not-callable") for name in (
            "encode_registry_target_ledger_artifact", "encode_registry_required_target_review_artifact",
            "validate_registry_required_target_review_artifacts", "validate_registry_required_target_ledger_artifact",
        )]
        cases += [("registry-0", missing, "") for missing in ("all", "all-with-model", paths[-1])]
        cases += [("registry-0", "", stage) for stage in (
            "collection", "nodeids-cleanup", "wrong-selection", "duplicate-selection", "empty-selection",
        )]
        successful_shard_members = []
        for lane, missing, failure in cases:
            with self.subTest(lane=lane, missing=missing, failure=failure), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source, runner = root / "source", root / "runner"
                (source / "support/ptg2_scanner").mkdir(parents=True)
                (source / "tests").mkdir()
                runner.mkdir()
                only_member = missing.removeprefix("only:") if missing.startswith("only:") else ""
                for path in paths:
                    if (path == only_member if only_member else path != missing and missing not in ("all", "all-with-model")):
                        (source / path).touch()
                if missing == "all-with-model":
                    (source / "db/models").mkdir(parents=True)
                    (source / "db/models/network_registry.py").touch()
                (source / "asyncpg.py").write_text(
                    "raise ImportError('synthetic dependency missing')\n" if failure == "asyncpg" else "", encoding="utf-8",
                )
                (source / "ptg2_address_canon.py").write_text(
                    "\n".join(
                        f"{name} = 0" if failure == name + ":not-callable" else f"def {name}(): pass"
                        for name in exports if name != failure
                    ), encoding="utf-8",
                )
                log = root / "calls"
                script = CHECK_FUNCTIONS + r'''
prepare_debug_rust_binaries() { :; }
wait_for_service() { :; }
prepare_postgres() { :; }
run_core_postgres() { :; }
run_provider_directory_postgres() { :; }
run_provider_profile_postgres() { :; }
create_test_database() { printf 'create\t%s\n' "$1" >> "$CALL_LOG"; }
drop_test_database() {
  printf 'drop\t%s\n' "$1" >> "$CALL_LOG"
  [ "$FAIL_STAGE" != drop ]
}
psql() { printf 'extensions\t%s\n' "$*" >> "$CALL_LOG"; }
rustc() { echo "rustc $([ "$FAIL_STAGE" = rust-version ] && echo 1.98.0 || echo 1.98.1)"; }
rm() {
  if [[ "$FAIL_STAGE" = report-cleanup && "${!#}" = *"/healthcare-network-registry."* ]]; then return 23; fi
  if [[ "$FAIL_STAGE" = nodeids-cleanup && "${!#}" = *"/healthcare-registry-nodeids."* ]]; then return 23; fi
  command rm "$@"
}
python() {
  case "$*" in
    '-m maturin build '*)
      printf 'build\t%s\n' "$*" >> "$CALL_LOG"
      [ "$FAIL_STAGE" != build ] || return 17
      touch "${!#}/synthetic.whl" ;;
    '-c '*)
      printf 'exports\n' >> "$CALL_LOG"
      command python3 "$@" ;;
    'scripts/ci/shard_pytest_nodeids.py '*)
      printf 'collection\t%s\n' "$*" >> "$CALL_LOG"
      [ "$FAIL_STAGE" != collection ] || return 17
      command python3 - "$@" <<'PY'
import pathlib
import sys
arguments = sys.argv[1:]
count = int(arguments[arguments.index("--shard-count") + 1])
index = int(arguments[arguments.index("--shard-index") + 1])
modules = arguments[arguments.index("--") + 1:]
path = pathlib.Path(arguments[arguments.index("--output") + 1])
nodeids = [module + "::synthetic_native_test" for module in modules[index::count]]
import os
if os.environ["FAIL_STAGE"] == "duplicate-selection":
    nodeids += nodeids[:1]
if os.environ["FAIL_STAGE"] == "empty-selection":
    nodeids = []
path.write_text("\n".join(nodeids) + ("\n" if nodeids else ""))
with open(os.environ["CALL_LOG"], "a") as log:
    log.write("selection\t" + "\t".join(nodeids) + "\n")
PY
      ;;
    '-m pytest -q '*)
      test "$HLTHPRT_DB_DATABASE:$HLTHPRT_DB_DATABASE_OVERRIDE:$PGDATABASE" = "$PGDATABASE:$PGDATABASE:$PGDATABASE"
      printf 'pytest\t%s\t%s\t%s\n' "$NETWORK_REGISTRY_TEST_DSN" \
        "$HLTHPRT_NETWORK_MEMBERSHIP_POSTGRES_DSN" "$*" >> "$CALL_LOG"
      [ "$FAIL_STAGE" != pytest ] || return 17
      command python3 - "${!#}" "${@:4}" <<'PY'
import os
import pathlib
import sys
import xml.etree.ElementTree as ET
path = pathlib.Path(sys.argv[1])
stage = os.environ["FAIL_STAGE"]
if stage in ("empty-report", "invalid-report", "large-report"):
    path.write_text({"empty-report": "", "invalid-report": "<invalid", "large-report": "x" * (16 * 1024 * 1024 + 1)}[stage])
else:
    modules = [value for value in sys.argv[2:] if value.startswith("tests/") and value.endswith(".py")]
    selections = [value[1:] for value in sys.argv[2:] if value.startswith("@")]
    if selections:
        modules = [nodeid.split("::")[0] for nodeid in pathlib.Path(selections[0]).read_text().splitlines()]
    if stage == "wrong-selection":
        modules[0] = "tests/test_unselected.py"
    if stage.startswith("missing-report:"):
        modules.remove(stage.removeprefix("missing-report:"))
    if stage == "stale-registry-roster":
        modules = modules[:-1]
    if stage == "no-tests":
        modules = []
    if stage == "more-management-cases":
        modules.extend(["tests/test_registry_management_routes_postgres.py"] * 2)
    report = ET.Element("testsuites")
    suite = ET.SubElement(report, "testsuite", tests=str(len(modules) + (stage == "bad-count")),
                          skipped="1" if stage == "summary-skipped" else "0", errors="0", failures="0")
    for index, module in enumerate(modules):
        case = ET.SubElement(suite, "testcase", name="synthetic_native_test",
                             classname="" if stage == "no-classname" else module.removesuffix(".py").replace("/", "."))
        if stage == "more-management-cases" and index >= len(modules) - 2:
            case.set("classname", case.get("classname") + ".ExampleCases")
        if index == 0 and stage in ("skipped", "error", "failure"):
            ET.SubElement(case, stage)
    ET.ElementTree(report).write(path, encoding="utf-8")
PY
      ;;
    '- '*) command python3 "$@" ;;
  esac
}
uv() {
  printf 'uv\t%s\n' "$*" >> "$CALL_LOG"
  [ "$FAIL_STAGE" != install ] || return 17
}
timeout() { shift 2; "$@"; }
run_postgres "$ROUTE_LANE"
test -z "${NETWORK_REGISTRY_TEST_DSN:-}${HLTHPRT_NETWORK_MEMBERSHIP_POSTGRES_DSN:-}"
'''
                environment = {
                    **os.environ, "SOURCE_ROOT": str(source), "CI_ROOT": str(ROOT), "RUNNER_TEMP": str(runner),
                    "CI_DEPS_READY": "1", "COVERAGE_BASE_SHA": "a" * 40, "CALL_LOG": str(log),
                    "ROUTE_LANE": lane, "FAIL_STAGE": failure,
                    "NETWORK_REGISTRY_TEST_DSN": "", "HLTHPRT_NETWORK_MEMBERSHIP_POSTGRES_DSN": "",
                }
                result = subprocess.run(["bash", "-euc", script], env=environment, capture_output=True, text=True, check=False)
                selected = (lane == "core" or lane.startswith("registry-")) and (missing != "all" or lane.startswith("registry-"))
                expected = 17 if failure in ("build", "install", "pytest", "collection") else 1 if failure not in ("", "more-management-cases") or (selected and missing) else 0
                self.assertEqual(result.returncode, expected, result.stderr)
                calls = log.read_text().splitlines() if log.exists() else []
                if failure in ("report-cleanup", "nodeids-cleanup"):
                    report_paths = list(runner.iterdir())
                    self.assertEqual(len(report_paths), 1)
                    self.assertTrue(report_paths[0].name.startswith("healthcare-"))
                    report_paths[0].unlink()
                self.assertEqual(list(runner.iterdir()), [])
                if not selected or missing or failure == "rust-version":
                    if lane == "directory-source":
                        self.assertEqual(len(calls), 3)
                        self.assertTrue(calls[0].startswith("build\t-m maturin build "))
                        self.assertTrue(calls[1].startswith("uv\t--no-config pip install "))
                        self.assertTrue(calls[2].startswith("uv\t--no-config pip check "))
                    else:
                        self.assertEqual(calls, [])
                    if selected and missing:
                        if missing == "all":
                            self.assertIn("Native registry shard requires registry source and tests", result.stderr)
                        else:
                            required = (next(path for path in paths if path != only_member) if only_member
                                        else paths[0] if missing == "all-with-model" else missing)
                            self.assertIn(f"Missing required native registry test: {required}", result.stderr)
                    continue
                self.assertTrue(calls[0].startswith("build\t-m maturin build --locked --features python-extension --out "))
                self.assertNotIn("--release", calls[0])
                runs = [call for call in calls if call.startswith("pytest\t")]
                report_failures = {"skipped", "error", "failure", "summary-skipped", "no-tests", "bad-count",
                                   "empty-report", "invalid-report", "large-report", "report-cleanup",
                                   "stale-registry-roster", "no-classname", "wrong-selection", "duplicate-selection", "empty-selection", "nodeids-cleanup"}
                self.assertEqual(len(runs), int(failure in ("", "pytest", "drop", "more-management-cases") or failure in report_failures
                                                or failure.startswith("missing-report:")))
                if runs:
                    self.assertRegex(result.stdout, rf"CI_PHASE end name=network-registry-postgres-tests elapsed_seconds=\d+ exit_code={expected}")
                    self.assertTrue(calls[1].startswith("uv\t--no-config pip install --python "))
                    self.assertIn(" --no-build --no-deps ", calls[1])
                    self.assertTrue(calls[2].startswith("uv\t--no-config pip check --python "))
                    _, registry_dsn, membership_dsn, arguments = runs[0].split("\t", 3)
                    self.assertEqual(registry_dsn, membership_dsn)
                    self.assertRegex(registry_dsn, r"@127\.0\.0\.1:5440/hc_network_registry_[0-9a-f]{32}$")
                    selected_arguments = shlex.split(arguments)
                    if lane.startswith("registry-"):
                        collection = next(call for call in calls if call.startswith("collection\t"))
                        collected = shlex.split(collection.split("\t", 1)[1])
                        self.assertEqual(collected[:5], ["scripts/ci/shard_pytest_nodeids.py", "--shard-count", "8",
                                                        "--shard-index", lane.removeprefix("registry-")])
                        self.assertEqual(collected[collected.index("--") + 1:], list(paths))
                        if not failure:
                            selection = next(call for call in calls if call.startswith("selection\t"))
                            successful_shard_members.extend(
                                nodeid.split("::", 1)[0] for nodeid in selection.split("\t")[1:]
                            )
                        self.assertEqual(selected_arguments[:3], ["-m", "pytest", "-q"])
                        self.assertEqual(len(selected_arguments), 6)
                        selection_path = Path(selected_arguments[3].removeprefix("@"))
                        self.assertEqual(selection_path.parent, runner)
                        self.assertFalse(selection_path.exists())
                    else:
                        self.assertEqual(selected_arguments[:-2], ["-m", "pytest", "-q", *paths])
                    self.assertEqual(selected_arguments[-2], "--junitxml")
                    self.assertEqual(Path(selected_arguments[-1]).parent, runner)
                    self.assertFalse(Path(selected_arguments[-1]).exists())
                    database = registry_dsn.rsplit("/", 1)[1]
                    self.assertEqual(calls[-1], f"drop\t{database}")
                    self.assertEqual(calls.count(f"create\t{database}"), 1)
                    self.assertLess(calls.index("exports"), calls.index(f"create\t{database}"))
                    extensions = next(call for call in calls if call.startswith("extensions\t"))
                    for name in ("intarray", "btree_gin", "postgis"):
                        self.assertIn(f"CREATE EXTENSION IF NOT EXISTS {name} WITH SCHEMA public", extensions)

        self.assertEqual(len(successful_shard_members), len(set(successful_shard_members)))
        self.assertCountEqual(successful_shard_members, paths)

    def test_optional_company_and_maintenance_native_routes_fail_closed(self):
        registry_roster = tuple(shlex.split(CHECK_FUNCTIONS.split(
            "run_network_registry_postgres() (\n", 1)[1].split("  local registry_tests=(\n", 1)[1].split("\n  )", 1)[0]))
        shard_members = []
        for kind, capability, dependencies in (
            ("company", COMPANY_ASSERTION_CAPABILITY, COMPANY_ASSERTION_DEPENDENCIES),
            ("maintenance", PROFILE_MAINTENANCE_CAPABILITY, PROFILE_MAINTENANCE_DEPENDENCIES),
        ):
            cases = [("legacy", "", "", ""), ("complete", "", "", "")]
            cases += [("complete", path, "", "") for path in (*capability, *dependencies)]
            cases += [("only", path, "", "") for path in capability]
            cases += [("complete", capability[0], stage, "") for stage in ("symlink", "dangling", "dirty", "untracked")]
            cases += [("complete", "", stage, "") for stage in (
                "skipped", "empty", "missing-family", "invalid-report", "pytest", "drop", "report-cleanup")]
            if kind == "company":
                cases += [("complete", "", "", str(index)) for index in range(8)]
            else:
                cases += [("complete", "", "", "publication")]
            for layout, changed, stage, shard in cases:
                with self.subTest(kind=kind, layout=layout, changed=changed, stage=stage, shard=shard), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    source, runner, log = root / "source", root / "runner", root / "calls"
                    source.mkdir()
                    runner.mkdir()
                    for path in registry_roster if kind == "company" else ():
                        target = source / path
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.touch()
                    selected = () if layout == "legacy" else (changed,) if layout == "only" else (*capability, *dependencies)
                    for path in selected:
                        if path == changed and not stage and layout != "only":
                            continue
                        target = source / path
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.touch()
                    if stage in ("symlink", "dangling"):
                        target = source / changed
                        target.unlink()
                        live = registry_roster[0] if kind == "company" else dependencies[0]
                        target.symlink_to(source / (live if stage == "symlink" else "absent"))
                    timeout = root / "timeout"
                    timeout.write_text(f"#!{sys.executable}\n" + r'''
import json, os, pathlib, sys, xml.etree.ElementTree as ET
arguments = sys.argv[1:]
assert arguments[:6] == ["--foreground", "295s", "python", "-m", "pytest", "-q"]
record = {"event": "pytest", "arguments": arguments, "registry": os.getenv("NETWORK_REGISTRY_TEST_DSN"),
          "migration": os.getenv("HLTHPRT_PTG2_V4_MIGRATION_POSTGRES_DSN"), "map": os.getenv("HLTHPRT_PTG2_V4_MAP_POSTGRES_TEST"),
          "profile": os.getenv("HLTHPRT_PROVIDER_DIRECTORY_PROFILE_POSTGRES_DSN"),
          "database": os.getenv("HLTHPRT_DB_DATABASE"), "override": os.getenv("HLTHPRT_DB_DATABASE_OVERRIDE"),
          "schema_alias": os.getenv("DB_SCHEMA")}
with open(os.environ["CALL_LOG"], "a") as log: log.write(json.dumps(record) + "\n")
if os.environ["FAIL_STAGE"] == "pytest": sys.exit(17)
if "--junitxml" not in arguments: sys.exit(0)
path = pathlib.Path(arguments[arguments.index("--junitxml") + 1])
if os.environ["FAIL_STAGE"] == "invalid-report": path.write_text("<invalid"); sys.exit(0)
selections = [a[1:] for a in arguments if a.startswith("@")]
modules = [n.split("::")[0] for n in pathlib.Path(selections[0]).read_text().splitlines()] if selections else [a for a in arguments if a.startswith("tests/") and a.endswith(".py")]
record["modules"] = modules
with open(os.environ["SELECTION_LOG"], "a") as log: log.write(json.dumps(modules) + "\n")
if os.environ["FAIL_STAGE"] == "empty": modules = []
if os.environ["FAIL_STAGE"] == "missing-family": modules = [m for m in modules if m != os.environ["NEW_NATIVE_TEST"]]
report = ET.Element("testsuites"); suite = ET.SubElement(report, "testsuite", tests=str(len(modules)), skipped="0", errors="0", failures="0")
for module in modules:
    case = ET.SubElement(suite, "testcase", classname=module.removesuffix(".py").replace("/", "."), name="synthetic_native_test")
    if os.environ["FAIL_STAGE"] == "skipped": ET.SubElement(case, "skipped")
path.write_bytes(ET.tostring(report))
''')
                    timeout.chmod(0o755)
                    environment = {
                        **os.environ, "SOURCE_ROOT": str(source), "CI_ROOT": str(ROOT), "RUNNER_TEMP": str(runner),
                        "CALL_LOG": str(log), "SELECTION_LOG": str(root / "selections"), "FAIL_STAGE": stage, "CHANGED_PATH": changed,
                        "NEW_NATIVE_TEST": capability[-1], "PATH": str(root) + os.pathsep + os.environ["PATH"],
                        "HLTHPRT_DB_USER": "postgres", "HLTHPRT_DB_PASSWORD": "synthetic",
                        "HLTHPRT_DB_HOST": "127.0.0.1", "HLTHPRT_DB_PORT": "5432",
                        "HLTHPRT_DB_DATABASE": "base_test", "HLTHPRT_DB_DATABASE_OVERRIDE": "wrong",
                        "DB_SCHEMA": "stale", "HLTHPRT_PTG2_V4_MAP_POSTGRES_TEST": "0",
                        "HLTHPRT_PTG2_V4_MIGRATION_POSTGRES_DSN": "unchanged",
                    }
                    script = CHECK_FUNCTIONS + r'''
git() { [[ "$FAIL_STAGE" != dirty && "$FAIL_STAGE" != untracked ]] || [[ "${!#}" != "$CHANGED_PATH" ]]; }
rustc() { echo 'rustc 1.98.1'; }
install_address_canon_wheel() { printf 'wheel\n' >> "$CALL_LOG"; }
create_test_database() { printf 'create:%s\n' "$1" >> "$CALL_LOG"; }
drop_test_database() { printf 'drop:%s\n' "$1" >> "$CALL_LOG"; [[ "$FAIL_STAGE" != drop ]]; }
psql() { :; }
rm() { [[ "$FAIL_STAGE" != report-cleanup ]] || return 23; command rm "$@"; }
python() {
  if [[ "$1" = -c ]]; then printf 'exports\n' >> "$CALL_LOG"; return; fi
  if [[ "$1" = scripts/ci/shard_pytest_nodeids.py ]]; then
    command python3 - "$@" <<'PYSHARD'
import pathlib, sys
args = sys.argv[1:]; modules = args[args.index("--") + 1:]
selected = modules[int(args[args.index("--shard-index")+1])::int(args[args.index("--shard-count")+1])]
pathlib.Path(args[args.index("--output")+1]).write_text("".join(m + "::synthetic_native_test\n" for m in selected))
PYSHARD
  else command python3 "$@"; fi
}
'''
                    call = (f"run_network_registry_postgres {shard}" if kind == "company" else
                            "run_provider_profile_postgres postgresql://synthetic/runtime_test profile-publication"
                            if shard == "publication" else "run_profile_control_maintenance_postgres")
                    if changed:
                        call = "if " + call + "; then exit 9; else exit 1; fi"
                    result = subprocess.run(["bash", "-euc", script + "\n" + call], env=environment,
                                            cwd=source, capture_output=True, text=True, timeout=30, check=False)
                    expected = 17 if stage == "pytest" else 1 if changed or stage else 0
                    self.assertEqual(result.returncode, expected, result.stderr)
                    calls = log.read_text().splitlines() if log.exists() else []
                    runs = [json.loads(line) for line in calls if line.startswith("{")]
                    if kind == "maintenance":
                        runs = [run for run in runs if capability[-1] in run["arguments"]]
                    if changed:
                        self.assertFalse(calls)
                    elif not stage:
                        self.assertEqual(len(runs), int(kind == "company" or layout == "complete"))
                    for run in runs:
                        if kind == "company":
                            self.assertRegex(run["registry"], r"@127\.0\.0\.1:5440/hc_network_registry_[0-9a-f]{32}$")
                            if layout == "complete":
                                self.assertEqual(run["map"], "1")
                                self.assertEqual(run["migration"], run["registry"])
                            else:
                                self.assertEqual((run["map"], run["migration"]), ("0", "unchanged"))
                        else:
                            self.assertRegex(run["database"], r"^hc_cms_admission_test_[0-9a-f]{32}$")
                            self.assertTrue(run["profile"].endswith("/" + run["database"]))
                            self.assertIsNone(run["override"])
                            self.assertIsNone(run["schema_alias"])
                        if not shard:
                            self.assertEqual(capability[-1] in run["arguments"], layout == "complete")
                    if kind == "company" and shard and not stage:
                        shard_members.extend(json.loads((root / "selections").read_text()))
                    created = [c[7:] for c in calls if c.startswith("create:")]
                    dropped = [c[5:] for c in calls if c.startswith("drop:")]
                    self.assertEqual(created, dropped)
                    leftovers = list(runner.iterdir())
                    if stage == "report-cleanup":
                        self.assertTrue(leftovers)
                        for path in leftovers: path.unlink()
                    else:
                        self.assertFalse(leftovers)
        self.assertEqual(len(shard_members), len(set(shard_members)))
        self.assertCountEqual(shard_members, (*registry_roster, COMPANY_ASSERTION_CAPABILITY[-1]))

    def test_registry_report_matches_class_and_parameter_ids_exactly(self):
        validator = CHECK_FUNCTIONS.split('validate_registry_native_report() {\n  python - "$@" <<\'PY\'\n', 1)[1].split("\nPY\n", 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selected, report = root / "nodeids", root / "report.xml"
            selected.write_text("tests/test_sample.py::Example::test_sample['{}'::integer[]]\n")
            report.write_text("<testsuites><testsuite tests='1'><testcase classname='tests.test_sample.Example' "
                              "name=\"test_sample['{}'::integer[]]\"/></testsuite></testsuites>")
            result = subprocess.run([sys.executable, "-c", validator, str(report), str(selected)],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            report.write_text(report.read_text().replace("integer[]", "text[]"))
            result = subprocess.run([sys.executable, "-c", validator, str(report), str(selected)],
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)

    def test_planfinder_decoder_uses_python_discovery(self):
        offline_paths = (
            "tests/test_cms_planfinder_workbook_input.py",
            "tests/test_network_registry_cms_prepared_address_copy.py",
            "tests/test_provider_directory_cms_retained_native_layout.py",
            "tests/test_registry_ptg_cohort_authority.py",
            "tests/test_registry_ptg_graph_reader.py",
            "tests/test_registry_ptg_producer_scope.py",
            "tests/test_registry_ptg_scope_engine.py",
            "tests/test_registry_ptg_scope_runtime.py",
            "tests/test_registry_company_approval_fence.py",
            "tests/test_registry_ptg_office_capture.py",
            "tests/test_registry_ptg_office_witness.py",
        )
        native_paths = (
            *REGISTRY_SOURCE_CUSTODY_TESTS, *REGISTRY_REQUIRED_TARGET_TESTS, REGISTRY_ADDRESS_EQUIVALENCE_TEST,
            "tests/test_registry_ptg_cohort_authority_postgres.py",
            "tests/test_registry_ptg_graph_reader_postgres.py",
            "tests/test_registry_ptg_scope_engine_postgres.py",
            REGISTRY_PTG_OFFICE_TEST,
            REGISTRY_PTG_OFFICE_WITNESS_TEST,
            COMPANY_ASSERTION_CAPABILITY[-1], PROFILE_MAINTENANCE_CAPABILITY[-1],
        )
        for present in (False, True):
            with self.subTest(present=present), tempfile.TemporaryDirectory() as temporary:
                source_root = Path(temporary)
                (source_root / "tests").mkdir()
                if present:
                    for path in offline_paths:
                        (source_root / path).write_text("def test_offline(): assert True\n")
                    for path in native_paths:
                        (source_root / path).touch()
                call_log = source_root / "calls"
                script = CHECK_FUNCTIONS + r'''
mapfile() { capacity_tests=(tests/test_capacity_placeholder.py); }
prepare_debug_rust_binaries() { :; }
python() { printf '%s\n' "$*" >> "$CALL_LOG"; }
timeout() { shift 2; "$@"; }
for shard in 0 1 2 3; do run_python_main "$shard"; done
'''
                environment_map = {
                    **os.environ, "SOURCE_ROOT": str(source_root), "CI_ROOT": str(ROOT),
                    "CI_DEPS_READY": "1", "COVERAGE_BASE_SHA": "a" * 40, "CALL_LOG": str(call_log),
                }
                run_result = subprocess.run(["bash", "-euc", script], cwd=source_root, env=environment_map,
                                            capture_output=True, text=True, check=False)
                self.assertEqual(run_result.returncode, 0, run_result.stderr)
                calls = [shlex.split(call) for call in call_log.read_text().splitlines()]
                selections = [call for call in calls if call[:2] == ["-m", "pytest"]]
                self.assertEqual(len(selections), 4)
                for shard, arguments in enumerate(selections):
                    self.assertEqual(arguments[arguments.index("--ci-shard-index") + 1], str(shard))
                    self.assertIn("scripts.ci.shard_pytest_nodeids", arguments)
                    for path in offline_paths:
                        self.assertNotIn(path, arguments)
                    for path in native_paths:
                        self.assertEqual(arguments.count(path), int(present))
                        if present:
                            self.assertEqual(arguments[arguments.index(path) - 1], "--ignore")
                    self.assertNotIn("--ignore-glob", arguments)
                    self.assertNotIn("-k", arguments)
                    self.assertNotIn("-m", arguments[2:])
                    python_paths = [index for index, argument in enumerate(arguments) if argument.endswith(".py")]
                    self.assertTrue(all(arguments[index - 1] == "--ignore" for index in python_paths))
                    self.assertIn("--cov=process", arguments)
                provenance_calls = [call for call in calls if "write-shard-provenance" in call]
                self.assertEqual(len(provenance_calls), 4)

    def test_completed_coverage_compacts_before_provenance_and_failure_stops_publication(self):
        for producer in ("run_python_main 0", "run_capacity", "run_postgres core-services"):
            for is_failure in (False, True):
                with self.subTest(producer=producer, failure=is_failure), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    log = root / "calls"
                    log.touch()
                    script = CHECK_FUNCTIONS + r'''
install_python_dependencies() { :; }
prepare_debug_rust_binaries() { :; }
wait_for_service() { :; }
prepare_postgres() { :; }
run_core_postgres() { printf 'pytest\n' >> "$CALL_LOG"; }
mapfile() { capacity_tests=(tests/test_placeholder.py); }
require_base_sha() { COVERAGE_BASE_SHA=synthetic; }
timeout() {
  case "$*" in
    *compact_coverage.py*)
      test "$4" = "$CI_ROOT/scripts/healthcare/compact_coverage.py"
      test "$5" = "$COVERAGE_FILE"
      printf 'compact\n' >> "$CALL_LOG"
      return "$COMPACTION_STATUS" ;;
    *write-shard-provenance*) printf 'provenance\n' >> "$CALL_LOG" ;;
    *pytest*) printf 'pytest\n' >> "$CALL_LOG" ;;
  esac
}
'''
                    environment = {
                        **os.environ, "SOURCE_ROOT": str(root), "CI_ROOT": str(ROOT),
                        "CALL_LOG": str(log), "CI_DEPS_READY": "1", "COVERAGE_FILE": str(root / ".coverage"),
                        "COMPACTION_STATUS": "17" if is_failure else "0",
                    }
                    result = subprocess.run(["bash", "-euc", script + producer], env=environment,
                                            capture_output=True, text=True, timeout=10, check=False)
                    self.assertEqual(result.returncode, 17 if is_failure else 0, result.stderr)
                    self.assertEqual(log.read_text().splitlines(),
                                     ["pytest", "compact"] if is_failure else ["pytest", "compact", "provenance"])

    def test_registry_address_equivalence_owns_database_and_admits_exact_bounded_report(self):
        cases = [(lane, present, "") for lane in (
            "directory-address", "directory-source", "directory-storage", "all",
        ) for present in (False, True)]
        cases += [("directory-address", True, stage) for stage in (
            "create", "pytest", "drop", "report-cleanup", "skipped", "error", "failure", "summary-skipped",
            "no-tests", "bad-count", "wrong-classname", "wrong-name", "duplicate-case", "empty-report",
            "invalid-report", "large-report",
        )]
        for lane, present, failure in cases:
            with self.subTest(lane=lane, present=present, failure=failure), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source, runner = root / "source", root / "runner"
                (source / "tests").mkdir(parents=True)
                runner.mkdir()
                if present:
                    (source / REGISTRY_ADDRESS_EQUIVALENCE_TEST).touch()
                log, lifecycle = root / "calls", root / "lifecycle"
                log.touch()
                lifecycle.touch()
                timeout_command = root / "timeout"
                timeout_command.write_text(f"#!{sys.executable}\n" + r'''
import json, os, pathlib, sys
import xml.etree.ElementTree as ET
arguments = sys.argv[1:]
if os.environ["EQUIVALENCE_TEST"] not in arguments:
    sys.exit(0)
record = dict(arguments=arguments, dsn=os.environ.get("REGISTRY_ADDRESS_EQUIVALENCE_TEST_DSN"),
              database=os.environ.get("HLTHPRT_DB_DATABASE"), alias=os.environ.get("DB_SCHEMA"),
              override=os.environ.get("HLTHPRT_DB_DATABASE_OVERRIDE"),
              coverage=os.environ.get("COVERAGE_FILE"), addopts=os.environ.get("PYTEST_ADDOPTS"))
with open(os.environ["CALL_LOG"], "a") as output:
    output.write(json.dumps(record) + "\n")
stage = os.environ["FAIL_STAGE"]
if stage == "pytest":
    sys.exit(17)
path = pathlib.Path(arguments[arguments.index("--junitxml") + 1])
if stage in ("empty-report", "invalid-report", "large-report"):
    path.write_text({"empty-report":"", "invalid-report":"<invalid", "large-report":"x"*(16*1024*1024+1)}[stage])
else:
    count = 0 if stage == "no-tests" else 2 if stage == "duplicate-case" else 1
    report = ET.Element("testsuites")
    suite = ET.SubElement(report, "testsuite", tests=str(count + (stage == "bad-count")),
                          skipped="1" if stage == "summary-skipped" else "0", errors="0", failures="0")
    for _ in range(count):
        case = ET.SubElement(suite, "testcase",
            classname="wrong" if stage == "wrong-classname" else "tests.test_network_cms_registry_address_equivalence_postgres",
            name="wrong" if stage == "wrong-name" else "test_native_copy_preserves_receipts_and_rejects_drift")
        if stage in ("skipped", "error", "failure"):
            ET.SubElement(case, stage)
    ET.ElementTree(report).write(path, encoding="utf-8")
''')
                timeout_command.chmod(0o755)
                script = CHECK_FUNCTIONS + r'''
python() { command python3 "$@"; }
install_address_canon_wheel() { :; }
create_test_database() {
  if [[ "$1" = hc_address_equivalence_* ]]; then
    printf 'create:%s\n' "$1" >> "$LIFECYCLE"
    [ "$FAIL_STAGE" != create ] || return 17
  fi
}
drop_test_database() {
  if [[ "$1" = hc_address_equivalence_* ]]; then
    test "$PGDATABASE" = postgres
    printf 'drop:%s\n' "$1" >> "$LIFECYCLE"
    [ "$FAIL_STAGE" != drop ] || return 7
  fi
}
rm() {
  if [[ "$FAIL_STAGE" = report-cleanup && "${!#}" = *"/healthcare-registry-address-equivalence."* ]]; then return 23; fi
  command rm "$@"
}
run_provider_directory_postgres postgresql://synthetic/test "$ROUTE_LANE"
test "$DB_SCHEMA:$HLTHPRT_DB_DATABASE_OVERRIDE" = sentinel:sentinel
test -z "${REGISTRY_ADDRESS_EQUIVALENCE_TEST_DSN:-}"
'''
                environment = {
                    **os.environ, "SOURCE_ROOT": str(source), "CI_ROOT": str(ROOT), "RUNNER_TEMP": str(runner),
                    "PATH": str(root) + os.pathsep + os.environ["PATH"], "CALL_LOG": str(log),
                    "LIFECYCLE": str(lifecycle), "FAIL_STAGE": failure, "ROUTE_LANE": lane,
                    "EQUIVALENCE_TEST": REGISTRY_ADDRESS_EQUIVALENCE_TEST, "DB_SCHEMA": "sentinel",
                    "HLTHPRT_DB_DATABASE_OVERRIDE": "sentinel", "HLTHPRT_DB_USER": "postgres",
                    "HLTHPRT_DB_PASSWORD": "synthetic", "HLTHPRT_DB_HOST": "127.0.0.1", "HLTHPRT_DB_PORT": "5432",
                    "COVERAGE_FILE": str(root / ".coverage.native"),
                    "PYTEST_ADDOPTS": "--cov=process --cov-branch --cov-append --cov-report=",
                }
                environment.pop("REGISTRY_ADDRESS_EQUIVALENCE_TEST_DSN", None)
                result = subprocess.run(["bash", "-euc", script], env=environment, capture_output=True, text=True,
                                        timeout=30, check=False)
                expected = 17 if failure in ("create", "pytest") else 1 if failure else 0
                self.assertEqual(result.returncode, expected, result.stderr)
                selected = present and lane in ("directory-address", "all")
                calls = [json.loads(line) for line in log.read_text().splitlines()]
                self.assertEqual(len(calls), int(selected and failure != "create"))
                if calls:
                    call = calls[0]
                    database = call["database"]
                    self.assertRegex(database, r"^hc_address_equivalence_[0-9a-f]{32}$")
                    self.assertEqual(call["dsn"], f"postgresql+asyncpg://postgres:synthetic@127.0.0.1:5432/{database}")
                    self.assertIsNone(call["alias"])
                    self.assertIsNone(call["override"])
                    self.assertEqual(call["coverage"], environment["COVERAGE_FILE"])
                    self.assertEqual(call["addopts"], environment["PYTEST_ADDOPTS"])
                    self.assertEqual(call["arguments"][:7], ["--foreground", "295s", "python", "-m", "pytest", "-q",
                                                           REGISTRY_ADDRESS_EQUIVALENCE_TEST])
                    self.assertEqual(call["arguments"][7], "--junitxml")
                    self.assertEqual(Path(call["arguments"][8]).parent, runner)
                events = lifecycle.read_text().splitlines()
                self.assertEqual(len(events), 2 if selected else 0)
                if selected:
                    self.assertEqual(events[0].removeprefix("create:"), events[1].removeprefix("drop:"))
                leftovers = list(runner.iterdir())
                if failure == "report-cleanup":
                    self.assertEqual(len(leftovers), 1)
                    leftovers[0].unlink()
                else:
                    self.assertFalse(leftovers)

    def test_cms_native_coverage_routes_and_owns_required_fixture_environments(self):
        routes = {
            "directory-source": (
                "tests/test_cms_npd_admission_postgres.py",
                "tests/test_cms_npd_candidate_coverage_postgres.py",
                "tests/test_cms_npd_intake_postgres.py",
                "tests/test_provider_directory_cms_retained_relations_postgres.py",
                "tests/test_cms_capacity_preflight_receipt_migration.py",
                "tests/test_provider_directory_cms_preflight_postgres.py",
                "tests/test_provider_directory_cms_wal_budget_postgres.py",
            ),
            "directory-storage": (
                "tests/test_provider_directory_cms_execution_settings_postgres.py",
                "tests/test_provider_directory_cms_replay_postgres.py",
                "tests/test_provider_directory_cms_serving_bounds_postgres.py",
                "tests/test_provider_directory_cms_capacity_ledger.py::test_native_two_purposes_share_one_run_and_reject_conflicts",
                "tests/test_provider_directory_prepared_bundle.py::test_prepared_bundle_obeys_outer_commit_and_rollback",
            ),
            "directory-address": (
                "tests/test_entity_address_candidate_preparation_postgres.py",
                "tests/test_entity_address_preparation_admission_postgres.py",
                "tests/test_entity_address_preparation_ownership_postgres.py",
                "tests/test_entity_address_prepared_archive.py::test_real_coordinate_read_preserves_incumbent_archive",
                "tests/test_entity_address_semantic_date.py::test_backdated_freshness_uses_inclusive_pinned_boundary",
                "tests/test_entity_address_semantic_date.py::test_missing_overlay_time_is_stable_across_session_timezones",
                "tests/test_provider_directory_practitioner_address_overlay_db.py",
                "tests/test_provider_directory_cms_archive_postgres.py",
                "tests/test_provider_directory_cms_native_inputs_postgres.py",
                "tests/test_provider_directory_cms_native_layout_postgres.py",
                "tests/test_provider_directory_cms_native_projection_postgres.py",
                "tests/test_provider_directory_cms_overlay_projection_postgres.py",
                "tests/test_provider_directory_cms_preparation.py::test_native_scope_loads_all_heaps_before_indexes_and_exposure",
                "tests/test_provider_directory_cms_preparation.py::test_native_cleanup_preserves_replacement_oid",
            ),
            "profile-storage": (
                "tests/test_provider_directory_capacity_reservation_snapshot_postgres.py",
                "tests/test_provider_directory_capacity_reservation_snapshot_api_postgres.py",
                "tests/test_provider_directory_profile_receipt_guard_postgres.py",
                "tests/test_provider_directory_profile_serving_receipt_postgres.py",
                "tests/test_provider_profile_snapshot_postgres.py",
                "tests/test_provider_profile_snapshot_receipt_postgres.py",
                "tests/test_provider_directory_profile_selection_desired_db.py",
                "tests/test_provider_directory_profile_desired_snapshot_postgres.py",
                "tests/test_provider_directory_publication_liveness_postgres.py",
            ),
            "profile-publication": (
                "tests/test_cms_doctors_preparation_postgres.py",
                "tests/test_cms_doctors_preparation_seal_postgres.py",
                "tests/test_cms_doctors_source_provenance_postgres.py",
                "tests/test_entity_address_prepared_doctors_postgres.py",
                "tests/test_provider_profile_detail_load_postgres.py",
                "tests/test_cms_archive_publication_postgres.py",
                "tests/test_cms_serving_publication_postgres.py",
                "tests/test_entity_address_serving_receipt_postgres.py",
                "tests/test_provider_directory_cms_serving_receipt_postgres.py",
                "tests/test_provider_directory_profile_failed_cleanup_postgres.py",
                "tests/test_provider_directory_profile_initial_migration.py::test_native_initial_receipt_guards",
                "tests/test_provider_directory_import_run_guards.py::test_native_complete_guard_catalog_and_ordinary_update",
                "tests/test_provider_directory_profile_initial_cleanup_postgres.py",
                "tests/test_provider_directory_profile_initial_reader_postgres.py",
                "tests/test_provider_directory_profile_initial_cutover_postgres.py",
                "tests/test_provider_directory_profile_initial_replay.py",
            ),
        }
        all_paths = tuple(path for paths in routes.values() for path in paths)
        cms_paths = set(routes["directory-source"] + routes["profile-publication"]
                        + routes["directory-address"][7:] + routes["profile-storage"][:4])
        entities_paths = set(routes["profile-storage"][4:6])
        cases = [
            (lane, present, "", "", 0)
            for lane, paths in routes.items()
            for present in (all_paths, (), (paths[-1],))
        ]
        cases += [(lane, all_paths, paths[-1], "", 17) for lane, paths in routes.items()]
        cases += [("profile-publication", (path,), "", "", 0)
                  for path in routes["profile-publication"][-7:]]
        cases += [("profile-publication", all_paths, path, "", 17)
                  for path in routes["profile-publication"][-7:-1]]
        cases += [
            (lane, all_paths, "", cleanup, expected)
            for lane in ("directory-address", "profile-publication", "profile-storage")
            for cleanup, expected in (("create", 17), ("drop", 1), ("artifact", 1))
        ]
        cases += [
            ("profile-storage", all_paths, routes["profile-storage"][index], "", 17)
            for index in (0, 4)
        ]
        cases += [
            ("profile-storage", (routes["profile-storage"][4],), "", cleanup, expected)
            for cleanup, expected in (("create", 17), ("drop", 1))
        ]
        for lane, present, failure, cleanup_failure, expected in cases:
            with self.subTest(lane=lane, present=len(present), failure=failure, cleanup=cleanup_failure):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    source = root / "source"
                    (source / "tests").mkdir(parents=True)
                    for path in present:
                        (source / path.split("::", 1)[0]).touch()
                    calls_path, lifecycle = root / "calls", root / "lifecycle"
                    calls_path.touch()
                    lifecycle.touch()
                    command = root / "timeout"
                    command.write_text(f"#!{sys.executable}\n" + '''
import json, os, pathlib, sys
arguments = sys.argv[1:]
tracked = json.loads(os.environ["NATIVE_TEST_PATHS"])
selected = [path for path in arguments if path in tracked]
if selected:
    for path in selected:
        if not pathlib.Path(path.split("::", 1)[0]).is_file():
            sys.exit(4)
    artifact = os.environ.get("HLTHPRT_CMS_NPD_ADMISSION_TEST_ARTIFACT_ROOT")
    record = {"arguments": arguments, "selected": selected,
              "database": os.environ.get("HLTHPRT_DB_DATABASE"),
              "override": os.environ.get("HLTHPRT_DB_DATABASE_OVERRIDE"),
              "dsn": os.environ.get("HLTHPRT_CMS_NPD_ADMISSION_TEST_DSN"),
              "entities_dsn": os.environ.get("HLTHPRT_DIRECTORY_ENTITIES_TEST_DSN"),
              "schema_alias": os.environ.get("DB_SCHEMA"),
              "schema": os.environ.get("HLTHPRT_DB_SCHEMA"),
              "allow_schema": os.environ.get("HLTHPRT_PROVIDER_DIRECTORY_PROFILE_ALLOW_SCHEMA_TESTS"),
              "artifact": artifact, "artifact_exists": bool(artifact and pathlib.Path(artifact).is_dir()),
              "coverage_file": os.environ.get("COVERAGE_FILE"),
              "pytest_addopts": os.environ.get("PYTEST_ADDOPTS")}
    with open(os.environ["CALL_LOG"], "a") as output:
        output.write(json.dumps(record) + "\\n")
    if artifact and os.environ["CLEANUP_FAILURE"] == "artifact":
        pathlib.Path(artifact, "unfinished-fixture").touch()
    if os.environ["FAIL_FILE"] in selected:
        sys.exit(17)
''')
                    command.chmod(0o755)
                    environment = {
                        **os.environ, "SOURCE_ROOT": str(source), "CI_ROOT": str(ROOT),
                        "RUNNER_TEMP": str(root), "CALL_LOG": str(calls_path), "LIFECYCLE": str(lifecycle),
                        "PATH": str(root) + os.pathsep + os.environ["PATH"],
                        "NATIVE_TEST_PATHS": json.dumps(all_paths), "FAIL_FILE": failure,
                        "CLEANUP_FAILURE": cleanup_failure, "DB_SCHEMA": "stale_alias",
                        "HLTHPRT_DB_SCHEMA": "mrf", "HLTHPRT_DB_DATABASE": "native_directory_test_runner",
                        "HLTHPRT_DB_DATABASE_OVERRIDE": "wrong_database", "HLTHPRT_DB_USER": "postgres",
                        "HLTHPRT_DB_PASSWORD": "synthetic", "HLTHPRT_DB_HOST": "127.0.0.1",
                        "HLTHPRT_DB_PORT": "5432", "COVERAGE_FILE": str(root / ".coverage.native"),
                        "PYTEST_ADDOPTS": "--cov=process --cov-branch --cov-append --cov-report=",
                    }
                    for name in ("HLTHPRT_CMS_NPD_ADMISSION_TEST_DSN", "HLTHPRT_CMS_NPD_ADMISSION_TEST_ARTIFACT_ROOT",
                                 "HLTHPRT_DIRECTORY_ENTITIES_TEST_DSN",
                                 "HLTHPRT_PROVIDER_DIRECTORY_PROFILE_ALLOW_SCHEMA_TESTS"):
                        environment.pop(name, None)
                    function = "run_provider_profile_postgres" if lane.startswith("profile") else "run_provider_directory_postgres"
                    script = CHECK_FUNCTIONS + r'''
install_address_canon_wheel() { :; }
create_test_database() {
  printf 'create:%s\n' "$1" >> "$LIFECYCLE"
  if [[ ( "$1" = hc_cms_admission_test_* || "$1" = hc_directory_entities_* ) && "$CLEANUP_FAILURE" = create ]]; then return 17; fi
}
drop_test_database() {
  if [[ "$1" = hc_cms_admission_test_* || "$1" = hc_directory_entities_* ]]; then test "$PGDATABASE" = postgres; fi
  printf 'drop:%s\n' "$1" >> "$LIFECYCLE"
  if [[ ( "$1" = hc_cms_admission_test_* || "$1" = hc_directory_entities_* ) && "$CLEANUP_FAILURE" = drop ]]; then return 7; fi
}
''' + f'\n{function} postgresql://synthetic/native_directory_test_runner {lane}\n' + r'''
test "$DB_SCHEMA:$HLTHPRT_DB_DATABASE_OVERRIDE" = stale_alias:wrong_database
test "$HLTHPRT_DB_DATABASE" = native_directory_test_runner
test -z "${HLTHPRT_CMS_NPD_ADMISSION_TEST_DSN:-}"
test -z "${HLTHPRT_CMS_NPD_ADMISSION_TEST_ARTIFACT_ROOT:-}"
test -z "${HLTHPRT_DIRECTORY_ENTITIES_TEST_DSN:-}"
test -z "${HLTHPRT_PROVIDER_DIRECTORY_PROFILE_ALLOW_SCHEMA_TESTS:-}"
'''
                    result = subprocess.run(["bash", "-euc", script], env=environment, capture_output=True, text=True, timeout=30)
                    self.assertEqual(result.returncode, expected, result.stderr)
                    calls = [json.loads(line) for line in calls_path.read_text().splitlines()]
                    observed = [path for call in calls for path in call["selected"]]
                    if not expected:
                        expected_paths = [path for path in routes[lane] if (source / path.split("::", 1)[0]).exists()]
                        self.assertEqual(observed, expected_paths)
                    elif failure:
                        self.assertIn(failure, observed)
                    for call in calls:
                        self.assertEqual(call["arguments"][:6], ["--foreground", "295s", "python", "-m", "pytest", "-q"])
                        self.assertIsNone(call["schema_alias"])
                        self.assertIsNone(call["override"])
                        self.assertEqual(call["schema"], "mrf")
                        self.assertEqual(call["coverage_file"], environment["COVERAGE_FILE"])
                        self.assertEqual(call["pytest_addopts"], environment["PYTEST_ADDOPTS"])
                        dsn_prefix = (f"postgresql+asyncpg://{environment['HLTHPRT_DB_USER']}:"
                                      f"{environment['HLTHPRT_DB_PASSWORD']}@{environment['HLTHPRT_DB_HOST']}:"
                                      f"{environment['HLTHPRT_DB_PORT']}/")
                        selected = set(call["selected"])
                        if selected & cms_paths:
                            self.assertTrue(selected <= cms_paths)
                            self.assertRegex(call["database"], r"^hc_cms_admission_test_[0-9a-f]{32}$")
                            self.assertEqual(call["dsn"], dsn_prefix + call["database"])
                            self.assertTrue(call["artifact_exists"])
                            self.assertIsNone(call["entities_dsn"])
                        elif selected & entities_paths:
                            self.assertTrue(selected <= entities_paths)
                            self.assertRegex(call["database"], r"^hc_directory_entities_[0-9a-f]{32}$")
                            self.assertEqual(call["entities_dsn"], dsn_prefix + call["database"])
                            self.assertIsNone(call["artifact"])
                            self.assertIsNone(call["dsn"])
                        else:
                            self.assertEqual(call["database"], "native_directory_test_runner")
                            self.assertEqual(call["allow_schema"], "1" if lane == "profile-storage" else None)
                            self.assertIsNone(call["dsn"])
                            self.assertIsNone(call["entities_dsn"])
                            self.assertIsNone(call["artifact"])
                    events = lifecycle.read_text().splitlines()
                    self.assertEqual(
                        [event.removeprefix("create:") for event in events if event.startswith("create:")],
                        [event.removeprefix("drop:") for event in events if event.startswith("drop:")],
                    )
                    leftovers = set(root.iterdir()) - {source, command, calls_path, lifecycle}
                    if cleanup_failure == "artifact":
                        self.assertEqual(len(leftovers), 1)
                        artifact = leftovers.pop()
                        self.assertEqual([path.name for path in artifact.iterdir()], ["unfinished-fixture"])
                        (artifact / "unfinished-fixture").unlink()
                        artifact.rmdir()
                    else:
                        self.assertFalse(leftovers)

    def test_directory_native_wheel_install_order(self):
        for lane, failure in (("directory-source", ""), ("provider-directory", ""),
                              ("directory-source", "build"), ("directory-source", "install")):
            with self.subTest(lane=lane, failure=failure), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source_root, runner = root / "source", root / "runner"
                (source_root / "support/ptg2_scanner").mkdir(parents=True)
                (source_root / "tests").mkdir()
                (source_root / "tests/test_cms_npd_admission_postgres.py").touch()
                runner.mkdir()
                calls_path = root / "calls"
                environment_by_name = {
                    **os.environ, "SOURCE_ROOT": str(source_root), "CI_ROOT": str(ROOT),
                    "RUNNER_TEMP": str(runner), "CALL_LOG": str(calls_path),
                    "WHEEL_INSTALLED": str(root / "installed"), "FAIL_STAGE": failure, "ROUTE_LANE": lane,
                    "CI_DEPS_READY": "1", "COVERAGE_BASE_SHA": "a" * 40,
                    "HLTHPRT_DB_USER": "postgres", "HLTHPRT_DB_PASSWORD": "synthetic",
                    "HLTHPRT_DB_HOST": "127.0.0.1", "HLTHPRT_DB_PORT": "5432",
                }
                run_result = subprocess.run(
                    ["bash", "-euc", CHECK_FUNCTIONS + _directory_wheel_script()],
                    env=environment_by_name, capture_output=True, text=True, timeout=30,
                )
                self.assertEqual(run_result.returncode, 17 if failure else 0, run_result.stderr)
                phases = re.findall(r"CI_PHASE start name=([a-z-]+)", run_result.stdout)
                expected_phases = ["postgres-prepare", "rust-wheel-build"]
                if failure != "build":
                    expected_phases.append("rust-wheel-install")
                if not failure:
                    expected_phases.append("postgres-tests")
                self.assertEqual(phases, expected_phases)
                endings = re.findall(r"CI_PHASE end name=([a-z-]+) elapsed_seconds=\d+ exit_code=(\d+)",
                                     run_result.stdout)
                self.assertEqual([name for name, _status in endings], expected_phases)
                self.assertEqual([int(status) for _name, status in endings],
                                 [0] * (len(expected_phases) - 1) + [17 if failure else 0])
                calls = calls_path.read_text().splitlines()
                self.assertEqual(sum(call.startswith("build\t") for call in calls), 1)
                self.assertTrue(calls[0].startswith("build\t-m maturin build --locked --features python-extension --out "))
                self.assertNotIn("--release", calls[0])
                native_runs = [call for call in calls if "tests/test_cms_npd_admission_postgres.py" in call]
                if failure:
                    self.assertFalse(native_runs)
                    self.assertFalse(any(call.startswith("tests\t") for call in calls))
                else:
                    self.assertEqual(len(native_runs), 1)
                    self.assertTrue(calls[1].startswith("uv\t--no-config pip install --python "))
                    self.assertIn(" --no-build --no-deps ", calls[1])
                    self.assertTrue(calls[2].startswith("uv\t--no-config pip check --python "))
                    self.assertTrue(calls[3].startswith("tests\t"))
                    self.assertEqual(sum(call.startswith("uv\t--no-config pip install") for call in calls), 1)
                self.assertEqual(list(runner.iterdir()), [])

    def test_cms_directory_routes_present_suites_and_keeps_common_tests_required(self):
        required = (
            "tests/test_provider_directory_entities_postgres.py",
            "tests/test_provider_directory_cms_entities.py",
            "tests/test_provider_directory_cms_payers.py",
            "tests/test_provider_directory_insurance_network_identity.py",
        )
        optional = (
            "tests/test_provider_directory_cms_practitioner_navigation.py",
            "tests/test_cms_npd_admission_postgres.py",
            "tests/test_provider_directory_entity_redirect.py",
        )
        cases = [(present, None, None) for present in ((), *((path,) for path in optional), optional)]
        cases += [((), missing, None) for missing in required]
        cases.append((optional, None, optional[1]))
        for present, missing, failure in cases:
            with self.subTest(present=present, missing=missing, failure=failure), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / "source"
                (source / "tests").mkdir(parents=True)
                for path in required + present:
                    (source / path).touch()
                if missing:
                    (source / missing).unlink()
                calls_path, lifecycle = root / "calls", root / "lifecycle"
                command = root / "timeout"
                command.write_text('#!/bin/bash\n' + r'''
printf '%s\t%s\t%s\t%s\n' "${HLTHPRT_CMS_NPD_ADMISSION_TEST_ARTIFACT_ROOT:-}" "${DB_SCHEMA-<unset>}" "${HLTHPRT_DB_SCHEMA-<unset>}" "$*" >> "$CALL_LOG"
for test_path in "$@"; do
  if [[ " $CMS_TEST_PATHS " = *" $test_path "* ]]; then
    test -f "$test_path" || exit 4
  fi
  test "$test_path" != "$FAIL_FILE" || exit 17
done
''')
                command.chmod(0o755)
                environment = {**os.environ, "SOURCE_ROOT": str(source), "CI_ROOT": str(ROOT),
                    "RUNNER_TEMP": str(root), "CALL_LOG": str(calls_path), "LIFECYCLE": str(lifecycle),
                    "PATH": str(root) + os.pathsep + os.environ["PATH"],
                    "CMS_TEST_PATHS": " ".join(required + optional), "FAIL_FILE": failure or "",
                    "DB_SCHEMA": "mrf", "HLTHPRT_DB_SCHEMA": "directory_synthetic",
                    "HLTHPRT_DB_USER": "postgres", "HLTHPRT_DB_PASSWORD": "synthetic",
                    "HLTHPRT_DB_HOST": "127.0.0.1", "HLTHPRT_DB_PORT": "5432"}
                script = CHECK_FUNCTIONS + r'''
install_address_canon_wheel() { :; }
create_test_database() { printf 'create:%s\n' "$1" >> "$LIFECYCLE"; }
drop_test_database() { printf 'drop:%s\n' "$1" >> "$LIFECYCLE"; }
run_provider_directory_postgres postgresql://synthetic/test directory-source
'''
                result = subprocess.run(["bash", "-euc", script], env=environment, capture_output=True, text=True)
                expected = 4 if missing else 17 if failure else 0
                self.assertEqual(result.returncode, expected, result.stderr)
                calls = [line.split("\t", 3) for line in calls_path.read_text().splitlines()]
                arguments = [call.split() for _, _, _, call in calls]
                if missing:
                    self.assertIn(missing, arguments[-1])
                elif not failure:
                    for path in required:
                        self.assertEqual(sum(call.count(path) for call in arguments), 1)
                    for path in optional:
                        self.assertEqual(sum(call.count(path) for call in arguments), int(path in present))
                for call in arguments:
                    self.assertEqual(call[:6], ["--foreground", "295s", "python", "-m", "pytest", "-q"])
                for _, schema_alias, runtime_schema, call in calls:
                    owned = any(path in call.split() for path in required + optional[:2])
                    self.assertEqual(schema_alias, "<unset>" if owned else "mrf")
                    self.assertEqual(runtime_schema, "directory_synthetic")
                admission_roots = [artifact for artifact, _, _, call in calls if optional[1] in call.split()]
                for artifact in admission_roots:
                    self.assertTrue(artifact)
                    self.assertFalse(Path(artifact).exists())
                events = lifecycle.read_text().splitlines()
                self.assertEqual(
                    [event.removeprefix("create:") for event in events if event.startswith("create:")],
                    [event.removeprefix("drop:") for event in events if event.startswith("drop:")],
                )
                self.assertEqual(set(root.iterdir()), {source, command, calls_path, lifecycle})

    def test_available_archive_suites_route_into_database_lane(self):
        names = (
            "test_code_sets_result_archive_postgres.py",
            "test_code_sets_publication_postgres.py",
            "test_ms_drg_result_generation_postgres.py",
        )
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source"
            tests = source / "tests"
            tests.mkdir(parents=True)
            log = Path(temporary) / "routes"
            script = CHECK_FUNCTIONS + r'''
timeout() { :; }
run_scoped_archive_postgres() { printf '%s\n' "$*" >> "$ROUTE_LOG"; }
run_core_postgres postgresql://postgres:postgres@127.0.0.1:5432/test core-services
'''
            for present in (names, names[:1], names[1:2]):
                for path in tests.iterdir():
                    path.unlink()
                for name in present:
                    (tests / name).touch()
                (source / REQUIRED_IMPORT_NATIVE_TESTS[0]).touch()
                result = subprocess.run(
                    ["bash", "-euc", script], cwd=source,
                    env={**os.environ, "SOURCE_ROOT": str(source), "CI_ROOT": str(ROOT),
                         "HLTHPRT_DB_USER": "postgres", "HLTHPRT_DB_PASSWORD": "postgres",
                         "ROUTE_LOG": str(log)},
                    capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                calls = log.read_text().splitlines()
                self.assertEqual(len(calls), 3 if len(present) == 3 else 2)
                self.assertTrue(calls[-1].endswith(REQUIRED_IMPORT_NATIVE_TESTS[0]))
                for name in present:
                    self.assertTrue(any(name in call for call in calls), name)
                for name in names:
                    if name not in present:
                        self.assertFalse(any(name in call for call in calls), name)
                log.unlink()

    def test_scoped_archive_uses_disposable_port_and_cleans_exact_database(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/healthcare.yml").read_text())
        postgres = workflow["jobs"]["address-canonical-db-tests"]["services"]["postgres"]
        self.assertIn("5440:5432", postgres["ports"])
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "archive.log"
            script = CHECK_FUNCTIONS + r'''
DROP_CALL_COUNT=0
dropdb() {
  DROP_CALL_COUNT=$((DROP_CALL_COUNT + 1))
  printf 'drop %s\n' "$*" >> "$ARCHIVE_LOG"
  if [ "$DROP_CALL_COUNT" -eq 2 ]; then
    test "$PGDATABASE" = postgres
    return "$DROP_STATUS"
  fi
}
createdb() { printf 'create %s\n' "$*" >> "$ARCHIVE_LOG"; }
psql() { printf 'extension %s\n' "$*" >> "$ARCHIVE_LOG"; }
timeout() {
  test "$PGHOST:$PGPORT:$PGUSER:$PGDATABASE" = \
    "127.0.0.1:5440:postgres:hc_florida_projection_0123456789abcdef0123456789abcdef"
  test "$HLTHPRT_DB_DATABASE:$HLTHPRT_DB_DATABASE_OVERRIDE" = \
    "hc_florida_projection_0123456789abcdef0123456789abcdef:hc_florida_projection_0123456789abcdef0123456789abcdef"
  test "$PGPASSWORD" = postgres
  printf 'run %s\n' "$FLORIDA_SNAPSHOT_TEST_DATABASE_URL" >> "$ARCHIVE_LOG"
  return "$TEST_STATUS"
}
run_scoped_archive_postgres hc_florida_projection_0123456789abcdef0123456789abcdef \
  FLORIDA_SNAPSHOT_TEST_DATABASE_URL postgresql+asyncpg://postgres@127.0.0.1:5440 \
  tests/test_florida_projection_archive_postgres.py
test -z "${FLORIDA_SNAPSHOT_TEST_DATABASE_URL:-}"
test "$PGDATABASE" = sentinel
test "$HLTHPRT_DB_DATABASE:$HLTHPRT_DB_DATABASE_OVERRIDE" = sentinel:sentinel
'''
            for status, drop_status, expected in ((0, 0, 0), (17, 0, 17), (0, 7, 1)):
                result = subprocess.run(
                    ["bash", "-euc", script],
                    env={**os.environ, "SOURCE_ROOT": temporary, "CI_ROOT": str(ROOT),
                         "ARCHIVE_LOG": str(log), "TEST_STATUS": str(status),
                         "DROP_STATUS": str(drop_status), "HLTHPRT_DB_HOST": "localhost",
                         "HLTHPRT_DB_PORT": "5432", "HLTHPRT_DB_USER": "postgres",
                         "HLTHPRT_DB_PASSWORD": "postgres", "PGDATABASE": "sentinel",
                         "HLTHPRT_DB_DATABASE": "sentinel",
                         "HLTHPRT_DB_DATABASE_OVERRIDE": "sentinel"},
                    capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, expected, result.stderr)
                self.assertEqual(log.read_text().splitlines(), [
                    "drop --if-exists --host 127.0.0.1 --port 5440 --username postgres "
                    "hc_florida_projection_0123456789abcdef0123456789abcdef",
                    "create --host 127.0.0.1 --port 5440 --username postgres "
                    "hc_florida_projection_0123456789abcdef0123456789abcdef",
                    "extension --no-psqlrc --set=ON_ERROR_STOP=1 --command "
                    "CREATE EXTENSION IF NOT EXISTS intarray WITH SCHEMA public --command "
                    "CREATE EXTENSION IF NOT EXISTS btree_gin WITH SCHEMA public --command "
                    "CREATE EXTENSION IF NOT EXISTS postgis WITH SCHEMA public",
                    "run postgresql+asyncpg://postgres@127.0.0.1:5440/"
                    "hc_florida_projection_0123456789abcdef0123456789abcdef",
                    "drop --if-exists --host 127.0.0.1 --port 5440 --username postgres "
                    "hc_florida_projection_0123456789abcdef0123456789abcdef",
                ])
                log.unlink()

    def test_installed_archive_cleanup_requires_process_drain_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "calls"
            script = CHECK_FUNCTIONS + r'''
dropdb() { printf 'drop %s\n' "$*" >> "$CALL_LOG"; }
createdb() { :; }
psql() { :; }
timeout() { echo 'installed tests must not use foreground timeout' >&2; return 99; }
python() {
  test "$1" = "$CI_ROOT/scripts/healthcare/supervise_installed.py"
  test "$3" = "$TEST_PATH"
  if [[ "$TEST_PATH" = *installed_operator_postgres.py ]]; then
    test "${*:4}" = '-n 2 --dist worksteal --durations=9 -vv'
  else
    test "$#" = 3
  fi
  printf 'supervised\n' >> "$CALL_LOG"
  if [ "$DRAINED" = 1 ]; then printf 'drained\n' > "$2"; fi
  return "$TEST_STATUS"
}
set -- "$TEST_PATH"
if [[ "$TEST_PATH" = *installed_operator_postgres.py ]]; then
  set -- "$@" -n 2 --dist worksteal --durations=9 -vv
fi
if [ "$BACKGROUND" = 1 ]; then
  run_scoped_archive_postgres_body hc_custom_import_test_0123456789abcdef0123456789abcdef \
    HLTHPRT_CUSTOM_IMPORT_POSTGRES_DSN postgresql://postgres@127.0.0.1:5440 "$@" &
  wait "$!"
else
  run_scoped_archive_postgres hc_custom_import_test_0123456789abcdef0123456789abcdef \
    HLTHPRT_CUSTOM_IMPORT_POSTGRES_DSN postgresql://postgres@127.0.0.1:5440 "$@"
fi
'''
            paths = ("tests/test_custom_import_installed_operator_postgres.py", *REQUIRED_IMPORT_NATIVE_TESTS[1:])
            cases = [(path, status, drained, 0) for path in paths
                     for status, drained in ((0, 1), (17, 1), (124, 1), (0, 0), (17, 0), (124, 0))]
            cases += [(path, status, 1, 1) for path in paths[1:] for status in (0, 17)]
            for path, status, drained, background in cases:
                with self.subTest(path=path, status=status, drained=drained, background=background):
                    result = subprocess.run(
                        ["bash", "-euc", script], capture_output=True, text=True,
                        env={**os.environ, "SOURCE_ROOT": temporary, "CI_ROOT": str(ROOT),
                             "RUNNER_TEMP": temporary, "CALL_LOG": str(log),
                             "HLTHPRT_DB_USER": "postgres", "HLTHPRT_DB_PASSWORD": "postgres",
                             "TEST_STATUS": str(status), "DRAINED": str(drained), "TEST_PATH": path,
                             "BACKGROUND": str(background)},
                    )
                    self.assertEqual(result.returncode, status or (0 if drained else 1), result.stderr)
                    calls = log.read_text().splitlines()
                    self.assertEqual(calls[1], "supervised")
                    self.assertEqual(sum(call.startswith("drop ") for call in calls), 2 if drained else 1)
                    if not drained:
                        self.assertIn("retained disposable database: hc_custom_import_test_0123456789abcdef0123456789abcdef",
                                      result.stderr)
                    self.assertFalse(list(Path(temporary).glob("healthcare-installed-drain.*")))
                    log.unlink()

    def test_address_archive_routes_only_available_native_suites(self):
        names = (
            "test_entity_address_snapshot_stage_postgres.py",
            "test_entity_address_snapshot_source_postgres.py",
        )
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source"
            tests = source / "tests"
            tests.mkdir(parents=True)
            log = Path(temporary) / "routes"
            script = CHECK_FUNCTIONS + r'''
env() { :; }
pg_dump() { :; }
pg_restore() { :; }
run_scoped_archive_postgres() {
  printf '%s\t%s\t%s\n' "$HLTHPRT_ENTITY_ADDRESS_ARCHIVE_TEST_PG_DUMP" \
    "$HLTHPRT_ENTITY_ADDRESS_ARCHIVE_TEST_PG_RESTORE" "$*" >> "$ROUTE_LOG"
}
run_provider_directory_postgres postgresql://postgres:postgres@127.0.0.1:5432/test directory-address
'''
            for present in (names, names[:1], ()):
                for path in tests.iterdir():
                    path.unlink()
                for name in present:
                    (tests / name).touch()
                result = subprocess.run(
                    ["bash", "-euc", script], cwd=source,
                    env={**os.environ, "SOURCE_ROOT": str(source), "CI_ROOT": str(ROOT),
                         "HLTHPRT_DB_USER": "postgres", "HLTHPRT_DB_PASSWORD": "postgres",
                         "ROUTE_LOG": str(log)},
                    capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                calls = log.read_text().splitlines() if log.exists() else []
                self.assertEqual(len(calls), 1 if present else 0)
                if present:
                    dump, restore, call = calls[0].split("\t", 2)
                    self.assertEqual((dump, restore), ("pg_dump", "pg_restore"))
                    self.assertRegex(call, r"^hc_entity_address_stage_[0-9a-f]{32} ")
                    self.assertIn("HLTHPRT_ENTITY_ADDRESS_ARCHIVE_TEST_DSN", call)
                    self.assertIn("@127.0.0.1:5440", call)
                    for name in names:
                        self.assertEqual(name in call, name in present)
                    log.unlink()

    def test_core_ptg_routes_guarded_result_archive_proofs(self):
        names = (
            "test_result_archive_adoption_postgres.py",
            "test_result_archive_candidate_preparation_postgres.py",
            "test_result_archive_closure_postgres.py",
            "test_registry_ptg_cohort_authority_postgres.py",
            "test_result_archive_candidate_initialization_postgres.py",
            "test_result_archive_candidate_validation_postgres.py",
        )
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source"
            tests = source / "tests"
            tests.mkdir(parents=True)
            for name in names:
                (tests / name).touch()
            for path in REQUIRED_IMPORT_NATIVE_TESTS[1:]:
                (source / path).touch()
            log = Path(temporary) / "routes"
            script = CHECK_FUNCTIONS + r'''
run_scoped_archive_postgres() { :; }
run_scoped_archive_postgres_body() { run_scoped_archive_postgres "$@"; }
timeout() {
  if [[ "$*" = *test_result_archive_* ]]; then
    printf '%s\t%s\t%s\t%s\n' "$HLTHPRT_PTG2_V4_MAP_POSTGRES_TEST" \
      "$HLTHPRT_PTG2_V4_MIGRATION_POSTGRES_DSN" \
      "$HLTHPRT_PTG2_ARCHIVE_CANDIDATE_POSTGRES_TEST" "$*" >> "$ROUTE_LOG"
  fi
}
run_core_postgres postgresql://postgres:postgres@127.0.0.1:5432/test core-ptg
'''
            result = subprocess.run(
                ["bash", "-euc", script], cwd=source,
                env={**os.environ, "SOURCE_ROOT": str(source), "CI_ROOT": str(ROOT),
                     "HLTHPRT_DB_USER": "postgres", "HLTHPRT_DB_PASSWORD": "postgres",
                     "ROUTE_LOG": str(log), "HLTHPRT_PTG2_V4_MAP_POSTGRES_TEST": "",
                     "HLTHPRT_PTG2_V4_MIGRATION_POSTGRES_DSN": "",
                     "HLTHPRT_PTG2_ARCHIVE_CANDIDATE_POSTGRES_TEST": ""},
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = [line.split("\t", 3) for line in log.read_text().splitlines()]
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0][:2], ["1", "postgresql://postgres:postgres@127.0.0.1:5432/test"])
            self.assertEqual(calls[1][2], "1")
            for name in names[:4]:
                self.assertIn(name, calls[0][3])
            for name in names[4:]:
                self.assertIn(name, calls[1][3])

    def test_registry_native_wheel_probe_refuses_source_shims_and_unverified_records(self):
        probe = CHECK_FUNCTIONS.split("verify_registry_native_wheel() {\n  python -I - \"$1\" \"$2\" <<'PY'\n", 1)[1].split("\nPY\n}", 1)[0]
        for stage in ("source-shadow", "python-shim", "missing-record", "changed-record", "pure-wheel"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as temporary:
                source = Path(temporary)
                module = source / "ptg2_address_canon.py"
                module.write_text("\n".join(f"{name} = len" for name in (*REGISTRY_PTG_GRAPH_EXPORTS, "encode_registry_ptg_capture_batch")))
                info = source / "ptg2_address_canon-0.1.0.dist-info"
                info.mkdir()
                (info / "METADATA").write_text("Metadata-Version: 2.4\nName: ptg2_address_canon\nVersion: 0.1.0\n")
                (info / "WHEEL").write_text("Wheel-Version: 1.0\nRoot-Is-Purelib: " + ("true" if stage == "pure-wheel" else "false") + "\n")
                digest = base64.urlsafe_b64encode(hashlib.sha256(module.read_bytes()).digest()).decode().rstrip("=")
                (info / "RECORD").write_text("" if stage == "missing-record" else
                                            f"ptg2_address_canon.py,sha256={'wrong' if stage == 'changed-record' else digest},{module.stat().st_size}\n")
                environment = {**os.environ, "PYTHONPATH": str(source), "PYTHONDONTWRITEBYTECODE": "1"}
                checkout = source if stage == "source-shadow" else source / "checkout"
                checkout.mkdir(exist_ok=True)
                for office in ("0", "1"):
                    prefix = "" if stage == "source-shadow" else "import sys; sys.path.insert(0, sys.argv[3])\n"
                    result = subprocess.run([sys.executable, "-I", "-S", "-", str(checkout), office, str(source)],
                                            input=prefix + probe, cwd=source, env=environment,
                                            capture_output=True, text=True, timeout=30)
                    self.assertNotEqual(result.returncode, 0, result.stderr)
                    expected = {"source-shadow": "Source checkout shadows native registry wheel", "python-shim": "no loaded compiled module",
                                "missing-record": "outside its installed wheel", "changed-record": "wheel content changed",
                                "pure-wheel": "wheel metadata is missing"}[stage]
                    self.assertIn(expected, result.stderr)

    def test_registry_native_probe_refuses_conventional_pytest_source_shadows_without_importing(self):
        probe = CHECK_FUNCTIONS.split("verify_registry_native_wheel() {\n  python -I - \"$1\" \"$2\" <<'PY'\n", 1)[1].split("\nPY\n}", 1)[0]
        shadow_check = probe.split("\nimport ptg2_address_canon as native", 1)[0]
        for kind in ("module", "package", "bytecode", "extension", "symlink", "test-module", "test-package", "absent"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                source = Path(temporary) / "source"
                source.mkdir()
                directory = source / "tests" if kind.startswith("test-") else source
                directory.mkdir(exist_ok=True)
                body = "raise AssertionError('source shadow must not execute')\n"
                if kind in ("module", "test-module"):
                    (directory / "ptg2_address_canon.py").write_text(body)
                elif kind in ("package", "test-package"):
                    package = directory / "ptg2_address_canon"
                    package.mkdir()
                    (package / "__init__.py").write_text(body)
                elif kind == "bytecode":
                    original = Path(temporary) / "original.py"
                    original.write_text(body)
                    py_compile.compile(str(original), cfile=str(directory / "ptg2_address_canon.pyc"), doraise=True)
                elif kind == "extension":
                    from importlib.machinery import EXTENSION_SUFFIXES
                    (directory / ("ptg2_address_canon" + EXTENSION_SUFFIXES[0])).touch()
                elif kind == "symlink":
                    original = Path(temporary) / "original.py"
                    original.write_text(body)
                    (directory / "ptg2_address_canon.py").symlink_to(original)
                for office in ("0", "1"):
                    result = subprocess.run([sys.executable, "-I", "-S", "-", str(source), office], input=shadow_check,
                                            cwd=source, capture_output=True, text=True, timeout=30)
                    self.assertEqual(result.returncode == 0, kind == "absent", result.stderr)
                    if kind != "absent":
                        self.assertIn("Source checkout shadows native registry wheel", result.stderr)
                        self.assertNotIn("source shadow must not execute", result.stderr)

    def test_registry_scope_native_route_is_source_bound_and_bootstrap_failures_stop_imports(self):
        cases = [(None, "", "core-ptg", 0), (None, "", "core-services", 0)]
        cases += [(path, "", "core-ptg", 1) for path in REGISTRY_PTG_SCOPE_CAPABILITY]
        cases += [(None, stage, "core-ptg", status) for stage, status in (
            ("identity", 1), ("dirty", 17), ("staged", 17), ("untracked", 17),
            ("build", 17), ("install", 17), ("exports", 29), ("tests", 31), ("symlink", 1),
        )]
        cases += [(None, name + ":non-builtin", "core-ptg", 1) for name in REGISTRY_PTG_GRAPH_EXPORTS]
        cases = [(missing, failure, lane, expected, False) for missing, failure, lane, expected in cases]
        cases += [(None, "", "core-ptg", 0, True), (None, "", "core-services", 0, True)]
        cases += [(None, stage, "core-ptg", status, True) for stage, status in (
            ("witness", 0), ("missing-witness-report", 1), ("witness-symlink", 1), ("dangling-witness", 1),
        )]
        cases += [(path, "", "core-ptg", 1, True) for path in REGISTRY_PTG_OFFICE_CAPABILITY]
        cases += [("only:" + path, "", "core-ptg", 1, True) for path in REGISTRY_PTG_OFFICE_SOURCES]
        cases += [(None, stage, "core-ptg", 1, True) for stage in (
            "empty-report", "invalid-report", "large-report", "no-tests", "bad-count", "summary-skipped",
            "skipped", "error", "failure", "missing-office-report", "report-cleanup",
            *REGISTRY_PTG_GRAPH_EXPORTS, "encode_registry_ptg_capture_batch",
            *(name + ":non-builtin" for name in (*REGISTRY_PTG_GRAPH_EXPORTS, "encode_registry_ptg_capture_batch")),
            "encode_registry_ptg_capture_batch:not-callable", "symlink", "dangling-office", "identity",
        )]
        cases += [(None, stage, "core-ptg", status, True) for stage, status in (
            ("build", 17), ("install", 17), ("tests", 31), ("dirty", 17), ("staged", 17), ("untracked", 17),
        )]
        for missing, failure, lane, expected, office in cases:
            with self.subTest(missing=missing, failure=failure, lane=lane, office=office), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source, runner = root / "source", root / "runner"
                runner.mkdir()
                witness = failure in {"witness", "missing-witness-report", "witness-symlink", "dangling-witness"}
                capability = (*REGISTRY_PTG_SCOPE_CAPABILITY, *(REGISTRY_PTG_OFFICE_CAPABILITY if office else ()),
                              *((REGISTRY_PTG_OFFICE_WITNESS_TEST,) if witness else ()))
                only = missing.removeprefix("only:") if missing and missing.startswith("only:") else None
                for relative in capability:
                    path = source / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    if not only or relative == only:
                        path.write_text("# synthetic source\n")
                exports = (*REGISTRY_PTG_GRAPH_EXPORTS, "encode_registry_ptg_capture_batch") if office else REGISTRY_PTG_GRAPH_EXPORTS
                (source / "ptg2_address_canon.py").write_text("\n".join(
                    f"{name} = 0" if failure == name + ":not-callable" else
                    f"def {name}(): pass" if failure == name + ":non-builtin" else
                    f"{name} = len  # synthetic builtin placeholder"
                    for name in exports if name != failure
                ))
                for relative in REQUIRED_IMPORT_NATIVE_TESTS:
                    (source / relative).touch()
                if missing and not only:
                    (source / missing).unlink()
                if failure in {"symlink", "dangling-office", "witness-symlink", "dangling-witness"}:
                    path = source / (REGISTRY_PTG_OFFICE_WITNESS_TEST if witness else
                                     REGISTRY_PTG_OFFICE_SOURCES[0] if office else REGISTRY_PTG_SCOPE_TESTS[0])
                    path.unlink()
                    path.symlink_to(source / ("missing-office.py" if failure in {"dangling-office", "dangling-witness"}
                                              else REGISTRY_PTG_SCOPE_TESTS[1]))
                calls = root / "calls"
                calls.touch()
                environment = {
                    **os.environ, "SOURCE_ROOT": str(source), "CI_ROOT": str(ROOT),
                    "SOURCE_SHA": "a" * 40, "RUNNER_TEMP": str(runner), "CALL_LOG": str(calls),
                    "FAIL_STAGE": failure, "ROUTE_LANE": lane,
                    "HLTHPRT_DB_USER": "postgres", "HLTHPRT_DB_PASSWORD": "synthetic",
                    "WHEEL_INSTALLED": str(root / "installed"), "EXPORTS_CHECKED": str(root / "exports"),
                    "COVERAGE_FILE": ".coverage.postgres.core-ptg", "PYTEST_ADDOPTS": "--cov-append",
                    "HLTHPRT_PTG2_V4_MAP_POSTGRES_TEST": "", "HLTHPRT_PTG2_V4_MIGRATION_POSTGRES_DSN": "",
                    "NETWORK_REGISTRY_TEST_DSN": "",
                }
                script = CHECK_FUNCTIONS + r'''
# Routing fixture isolates the native import/provenance boundary; these placeholders are synthetic.
verify_registry_native_wheel() {
  python -c 'import inspect; import sys; import ptg2_address_canon as native; names = ("plan_registry_ptg_graph_locator_pages", "plan_registry_ptg_graph_member_pages", "verify_registry_ptg_graph_batch") + (("encode_registry_ptg_capture_batch",) if sys.argv[1] == "1" else ()); assert all(inspect.isbuiltin(getattr(native, name, None)) for name in names), "Synthetic native boundary is incomplete"' "$2"
}
git() {
  case "$1" in
    rev-parse) [ "$FAIL_STAGE" != identity ] || { printf wrong; return; }; printf '%s\n' "$SOURCE_SHA" ;;
    diff) if [[ "$*" = *--cached* ]]; then [ "$FAIL_STAGE" != staged ] || return 17;
          else [ "$FAIL_STAGE" != dirty ] || return 17; fi ;;
    ls-files) printf 'tracked\t%s\n' "$*" >> "$CALL_LOG"; [ "$FAIL_STAGE" != untracked ] || return 17 ;;
    *) return 99 ;;
  esac
}
python() {
  if [[ "$*" = '-m maturin build '* ]]; then
    printf 'build\t%s\n' "$*" >> "$CALL_LOG"
    [ "$FAIL_STAGE" != build ] || return 17
    touch "${!#}/synthetic.whl"
  elif [ "$1" = -c ]; then
    test -f "$WHEEL_INSTALLED" || return 23
    printf 'exports\n' >> "$CALL_LOG"
    [ "$FAIL_STAGE" != exports ] || return 29
    command python3 "$@" || return $?
    touch "$EXPORTS_CHECKED"
  else
    command python3 "$@"
  fi
}
uv() {
  printf 'uv\t%s\n' "$*" >> "$CALL_LOG"
  [ "$FAIL_STAGE" != install ] || return 17
  touch "$WHEEL_INSTALLED"
}
timeout() {
  if [[ "$*" = *test_registry_ptg_graph_reader_postgres.py* ]]; then
    test -f "$WHEEL_INSTALLED" && test -f "$EXPORTS_CHECKED" || return 23
    printf 'native\t%s\t%s\t%s\t%s\t%s\t%s\n' "$HLTHPRT_PTG2_V4_MAP_POSTGRES_TEST" \
      "$HLTHPRT_PTG2_V4_MIGRATION_POSTGRES_DSN" "$NETWORK_REGISTRY_TEST_DSN" \
      "$COVERAGE_FILE" "$PYTEST_ADDOPTS" "$*" >> "$CALL_LOG"
    [ "$FAIL_STAGE" != tests ] || return 31
    command python3 - "${!#}" "$@" <<'PY'
import os
import pathlib
import sys
import xml.etree.ElementTree as ET
path = pathlib.Path(sys.argv[1])
stage = os.environ["FAIL_STAGE"]
if stage in ("empty-report", "invalid-report", "large-report"):
    path.write_text({"empty-report": "", "invalid-report": "<invalid", "large-report": "x" * (16 * 1024 * 1024 + 1)}[stage])
else:
    modules = [arg for arg in sys.argv[2:] if arg.startswith("tests/") and arg.endswith(".py")]
    if stage == "missing-office-report":
        modules.remove("tests/test_registry_ptg_office_capture_postgres.py")
    if stage == "missing-witness-report":
        modules.remove("tests/test_registry_ptg_office_witness_postgres.py")
    if stage == "no-tests":
        modules = []
    report = ET.Element("testsuites")
    suite = ET.SubElement(report, "testsuite", tests=str(len(modules) + (stage == "bad-count")),
                          skipped="1" if stage == "summary-skipped" else "0", errors="0", failures="0")
    for index, module in enumerate(modules):
        case = ET.SubElement(suite, "testcase", name="synthetic_native_test",
                             classname=module.removesuffix(".py").replace("/", "."))
        if index == 0 and stage in ("skipped", "error", "failure"):
            ET.SubElement(case, stage)
    ET.ElementTree(report).write(path, encoding="utf-8")
PY
  fi
}
rm() {
  if [[ "$FAIL_STAGE" = report-cleanup && "${!#}" = *"/healthcare-registry-scope."* ]]; then return 23; fi
  command rm "$@"
}
run_scoped_archive_postgres() { :; }
run_scoped_archive_postgres_body() { run_scoped_archive_postgres "$@"; }
create_test_database() { :; }
drop_test_database() { :; }
psql() { return 99; }
ci_phase_begin source-review-route
run_core_postgres postgresql://postgres@127.0.0.1:5432/test "$ROUTE_LANE"
ci_phase_end
'''
                result = subprocess.run(["bash", "-euc", script], cwd=source, env=environment,
                                        capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, expected, result.stderr)
                endings = [line for line in result.stdout.splitlines() if line.startswith("CI_PHASE end name=source-review-route ")]
                self.assertEqual(len(endings), 1, result.stdout)
                self.assertTrue(endings[0].endswith(f"exit_code={expected}"), result.stdout)
                if missing or failure in {"symlink", "dangling-office", "witness-symlink", "dangling-witness"}:
                    self.assertIn("Incomplete registry source review capability", result.stderr)
                events = calls.read_text().splitlines()
                if missing or failure in {"identity", "dirty", "staged", "untracked", "symlink", "dangling-office", "witness-symlink", "dangling-witness"} or lane != "core-ptg":
                    self.assertFalse(any(event.startswith(("build", "uv", "exports", "native")) for event in events))
                else:
                    builds = [event for event in events if event.startswith("build\t")]
                    self.assertEqual(len(builds), 1)
                    self.assertIn("--locked --features python-extension", builds[0])
                    self.assertNotIn("--release", builds[0])
                    native = [event.split("\t") for event in events if event.startswith("native\t")]
                    export_failures = {*REGISTRY_PTG_GRAPH_EXPORTS, "encode_registry_ptg_capture_batch",
                                       *(name + ":non-builtin" for name in (*REGISTRY_PTG_GRAPH_EXPORTS, "encode_registry_ptg_capture_batch")),
                                       "encode_registry_ptg_capture_batch:not-callable", "exports", "build", "install"}
                    self.assertEqual(len(native), int(failure not in export_failures))
                    tracked = [shlex.split(event.split("\t", 1)[1]) for event in events if event.startswith("tracked\t")]
                    self.assertEqual(tracked, [["ls-files", "--error-unmatch", *capability]])
                    if native:
                        dsn = "postgresql://postgres@127.0.0.1:5432/test"
                        self.assertEqual(native[0][1:6], ["1", dsn, dsn, environment["COVERAGE_FILE"], "--cov-append"])
                        arguments = shlex.split(native[0][6])
                        selected = (*REGISTRY_PTG_SCOPE_TESTS, *((REGISTRY_PTG_OFFICE_TEST,) if office else ()),
                                    *((REGISTRY_PTG_OFFICE_WITNESS_TEST,) if witness else ()))
                        self.assertEqual(arguments[:-2], ["--foreground", "295s", "python", "-m", "pytest", "-q", *selected])
                        self.assertEqual(arguments[-2], "--junitxml")
                        self.assertEqual(Path(arguments[-1]).parent, runner)
                        self.assertLess(events.index("exports"), events.index("\t".join(native[0])))
                leftovers = list(runner.iterdir())
                if failure == "report-cleanup":
                    self.assertEqual(len(leftovers), 1)
                    self.assertTrue(leftovers[0].name.startswith("healthcare-registry-scope."))
                    leftovers[0].unlink()
                self.assertFalse(list(runner.iterdir()))

    def test_registry_scope_absent_capability_preserves_older_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            (source / "tests").mkdir()
            (source / "tests/test_registry_ptg_cohort_authority_postgres.py").touch()
            script = CHECK_FUNCTIONS + "\ngit() { return 97; }\ninstall_address_canon_wheel() { return 98; }\nrun_registry_ptg_scope_postgres synthetic\n"
            result = subprocess.run(["bash", "-euc", script], cwd=source,
                                    env={**os.environ, "SOURCE_ROOT": str(source), "CI_ROOT": str(ROOT)},
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_source_profile_postgres_suites_use_one_isolated_profile_database(self):
        test_paths = (
            "tests/test_source_profile_result_archive_postgres.py",
            "tests/test_source_profile_ancestry_postgres.py",
            "tests/test_source_profile_pin_migration_postgres.py",
        )
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / "source"
                (source / "tests").mkdir(parents=True)
                for test_path in test_paths:
                    (source / test_path).write_text("# synthetic PostgreSQL test\n", encoding="utf-8")
                call_log = root / "calls"
                dsn = "postgresql://postgres:postgres@localhost:5432/ptg2_v3_lifecycle_test_ci_runner"
                env = {
                    **os.environ,
                    "SOURCE_ROOT": str(source),
                    "CI_ROOT": str(ROOT),
                    "CI_DEPS_READY": "1",
                    "COVERAGE_BASE_SHA": "a" * 40,
                    "CALL_LOG": str(call_log),
                    "ROUTE_FAILURE": "1" if fail else "0",
                }
                script = CHECK_FUNCTIONS + r'''
mapfile() { capacity_tests=(tests/test_capacity_placeholder.py); }
prepare_debug_rust_binaries() { :; }
create_test_database() { printf 'create\t%s\n' "$1" >> "$CALL_LOG"; }
drop_test_database() { printf 'drop\t%s\n' "$1" >> "$CALL_LOG"; }
python() {
  printf '%s\t%s\n' "${HLTHPRT_SOURCE_PROFILE_ARCHIVE_TEST_DSN:-}" "$*" >> "$CALL_LOG"
  if [[ "$*" = *"-m pytest -q tests/test_source_profile_result_archive_postgres.py"* \
        && "$ROUTE_FAILURE" = 1 ]]; then
    return 23
  fi
}
timeout() { shift 2; "$@"; }
run_python_main 0
run_provider_profile_postgres "postgresql://postgres:postgres@localhost:5432/ptg2_v3_lifecycle_test_ci_runner" profile-publication
'''
                result = subprocess.run(
                    ["bash", "-euc", script], cwd=source, env=env, capture_output=True, text=True
                )
                self.assertEqual(result.returncode == 0, not fail, result.stderr)
                calls = call_log.read_text().splitlines()
                main_call = next(call for call in calls if "--ci-shard-count 4" in call)
                for test_path in test_paths:
                    self.assertIn(f"--ignore {test_path}", main_call)
                profile_call = next(
                    call for call in calls if "-m pytest -q tests/test_source_profile_result_archive_postgres.py" in call
                )
                for test_path in test_paths:
                    self.assertIn(test_path, profile_call)
                profile_dsn = profile_call.split("\t", 1)[0]
                self.assertRegex(profile_dsn, rf"^{dsn.rsplit('/', 1)[0]}/hc_source_profile_[0-9a-f]{{32}}$")
                database = profile_dsn.rsplit("/", 1)[1]
                self.assertEqual(calls.count(f"create\t{database}"), 1)
                self.assertEqual(calls.count(f"drop\t{database}"), 1)
                self.assertLess(calls.index(f"create\t{database}"), calls.index(profile_call))
                self.assertGreater(calls.index(f"drop\t{database}"), calls.index(profile_call))

    def test_fast_quality_preserves_checks_without_reinstalling_the_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            environment = {**os.environ, "SOURCE_ROOT": temporary, "CI_ROOT": str(ROOT), "BASE_SHA": "a" * 40}
            script = CHECK_FUNCTIONS + r'''
commit_message_policy() { printf 'commit policy\n'; }
prepare_python_environment() { printf 'quality environment\n'; }
install_python_dependencies() { printf 'runtime dependencies\n'; return 17; }
uv() { printf 'uv %s\n' "$*"; }
python() { printf 'python %s\n' "$*"; }
run_quality
'''
            result = subprocess.run(["bash", "-euc", script], env=environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        for retained in ("commit policy", "requirements-python-quality.lock", "--require-hashes",
                         "python_format.py", "compileall", "coverage_reports.py --check",
                         "request_time_external_call_guard.py"):
            self.assertIn(retained, result.stdout)
        self.assertNotIn("ruff check --no-cache .", CHECK_FUNCTIONS)
        for moved in ("runtime dependencies", "readability_budget.py", "test_coverage_forecast.py",
                      "provider_directory_runtime_contract.py", "generate_provider_directory_support_docs.py"):
            self.assertNotIn(moved, result.stdout)

    def test_quality_stops_after_changed_file_lint_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            environment = {**os.environ, "SOURCE_ROOT": temporary, "CI_ROOT": str(ROOT), "BASE_SHA": "a" * 40}
            script = CHECK_FUNCTIONS + r'''
commit_message_policy() { :; }
prepare_python_environment() { :; }
uv() { :; }
python() {
  printf 'python %s\n' "$*"
  case "$1" in
    */python_format.py) return 23 ;;
  esac
}
run_quality
'''
            result = subprocess.run(["bash", "-euc", script], env=environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, 23, result.stderr)
        self.assertIn("python_format.py", result.stdout)
        for later_stage in ("compileall", "coverage_reports.py", "request_time_external_call_guard.py"):
            self.assertNotIn(later_stage, result.stdout)

    def test_runtime_contracts_remain_mandatory_in_the_api_lane(self):
        with tempfile.TemporaryDirectory() as temporary:
            environment = {**os.environ, "SOURCE_ROOT": temporary, "CI_ROOT": str(ROOT)}
            script = CHECK_FUNCTIONS + r'''
install_python_dependencies() { printf 'runtime dependencies\n'; }
run_python_inference() { printf 'runtime-aware native inference\n'; }
run_runtime_contracts() { printf 'runtime import contracts\n'; }
python() { printf 'python %s\n' "$*"; }
timeout() { printf 'timeout %s\n' "$*"; }
run_api_contract
'''
            result = subprocess.run(["bash", "-euc", script], env=environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        for retained in ("runtime dependencies", "runtime-aware native inference", "runtime import contracts",
                         "provider_directory_runtime_contract.py",
                         "generate_provider_directory_support_docs.py --check", "tests/test_openapi_spec.py",
                         "tests/test_formulary_fhir_openapi.py", "tests/test_api_init_and_utils.py", "tests/test_healthcheck.py"):
            self.assertIn(retained, result.stdout)
        self.assertLess(result.stdout.index("runtime dependencies"), result.stdout.index("runtime-aware native inference"))
        self.assertLess(result.stdout.index("runtime-aware native inference"), result.stdout.index("runtime import contracts"))
        workflow = yaml.safe_load((ROOT / ".github/workflows/healthcare.yml").read_text())
        self.assertIn("measurement", workflow["jobs"]["source-validation"]["needs"])
        self.assertIn("readability-preflight", workflow["jobs"]["measurement"]["needs"])
        self.assertIn("api-contract", workflow["jobs"]["measurement"]["needs"])
        main = CHECK_FUNCTIONS.split("run_python_main() {", 1)[1].split("\n}\n", 1)[0]
        self.assertNotIn("test_coverage_forecast.py", main)
        self.assertIn("--ci-shard-count 4", main)

    def test_runtime_contract_modules_fail_closed_without_creating_a_tool_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            environment = {**os.environ, "SOURCE_ROOT": str(source), "CI_ROOT": str(ROOT)}
            result = subprocess.run(
                ["bash", "-euc", CHECK_FUNCTIONS + "\nrun_runtime_contracts\n"],
                cwd=source, env=environment, capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("Runtime contract module is missing: api.billing_search_selector_contract", result.stderr)
            self.assertEqual(list(source.iterdir()), [])
        self.assertIn("--member aiohttp.client:ClientSession --member orjson:loads --member orjson:dumps", CHECK_FUNCTIONS)
        self.assertIn("runtime_imports.py", CHECK_FUNCTIONS)

    def test_inference_environment_creation_failure_keeps_runtime_and_cleans_its_child(self):
        for filename in ("requirements-python-quality.in", "requirements-python-quality.lock"):
            content = (ROOT / "scripts" / filename).read_text()
            self.assertIn("ty==0.0.85", content)
            for removed in ("pylint==", "astroid==", "isort=="):
                self.assertNotIn(removed, content)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            runtime = root / "runtime"
            runtime.mkdir()
            environment = {**os.environ, "SOURCE_ROOT": str(source), "CI_ROOT": str(ROOT),
                           "RUNNER_TEMP": str(root), "VIRTUAL_ENV": str(runtime)}
            script = CHECK_FUNCTIONS + '\nuv() { return 19; }\nrun_python_inference\n'
            result = subprocess.run(["bash", "-euc", script], env=environment, capture_output=True, text=True)
            self.assertEqual(result.returncode, 19, result.stderr)
            self.assertEqual(set(root.iterdir()), {source, runtime})
        self.assertNotIn("sys.path.extend", CHECK_FUNCTIONS)
        self.assertIn("orjson.quality_probe_missing_member", CHECK_FUNCTIONS)
        self.assertIn("client.quality_probe_missing_member", CHECK_FUNCTIONS)
        self.assertIn('runtime_python=$(command -v python)', CHECK_FUNCTIONS)
        self.assertIn('--python "$runtime_python"', CHECK_FUNCTIONS)
        self.assertIn("--config-file /dev/null", CHECK_FUNCTIONS)
        self.assertIn("--require-hashes --only-binary=:all:", CHECK_FUNCTIONS)
        self.assertIn("Python runtime dependency inference canary failed (status %s)", CHECK_FUNCTIONS)
        self.assertNotIn("pylint", CHECK_FUNCTIONS)
        self.assertIn("ANN201,ANN202,ANN204,ANN205,ANN206", CHECK_FUNCTIONS)
        self.assertIn("lint.flake8-annotations.suppress-none-returning=false", CHECK_FUNCTIONS)

    def test_inference_hashed_install_failure_cleans_only_its_child(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, runtime = root / "source", root / "runtime"
            source.mkdir()
            runtime.mkdir()
            marker = runtime / "preserved"
            marker.write_text("runtime state")
            environment = {**os.environ, "SOURCE_ROOT": str(source), "CI_ROOT": str(ROOT),
                           "RUNNER_TEMP": str(root), "VIRTUAL_ENV": str(runtime)}
            script = CHECK_FUNCTIONS + r'''
uv() {
  if [[ "$*" = *" venv "* ]]; then
    local last
    for last; do :; done
    mkdir -p "$last"
  else
    return 23
  fi
}
run_python_inference
'''
            result = subprocess.run(["bash", "-euc", script], env=environment, capture_output=True, text=True)
            self.assertEqual(result.returncode, 23, result.stderr)
            self.assertEqual(set(root.iterdir()), {source, runtime})
            self.assertEqual(marker.read_text(), "runtime state")

    def test_inference_targets_fail_closed_before_running_native_tools(self):
        targets = CHECK_FUNCTIONS.split("local -a inference_targets=(", 1)[1].split("  )", 1)[0].split()
        modules = CHECK_FUNCTIONS.split("local -a modules=(", 1)[1].split("  )", 1)[0].split()
        self.assertEqual(len(targets), 11)
        self.assertEqual(set(targets), {module.replace(".", "/") + ".py" for module in modules})
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            for target in targets:
                path = source / target
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
            environment = {**os.environ, "SOURCE_ROOT": str(source), "CI_ROOT": str(ROOT), "RUNNER_TEMP": str(root)}
            script = CHECK_FUNCTIONS + r'''
uv() { :; }
run_python_inference
'''
            for target in targets:
                with self.subTest(target=target):
                    (source / target).unlink()
                    result = subprocess.run(
                        ["bash", "-euc", script],
                        check=False,
                        cwd=source,
                        env=environment,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn(f"Python inference target is missing: {target}", result.stderr)
                    self.assertEqual(set(root.iterdir()), {source})
                    (source / target).touch()
        self.assertNotIn("process/fhir_request_failure_policy.py", CHECK_FUNCTIONS)

    def test_native_inference_rejects_invalid_operations(self):
        cases = (
            ("result = -'value'\n", "unsupported-operator"),
            ("class Box: pass\nBox().missing_member\n", "unresolved-attribute"),
            ("from pathlib import definitely_missing_name\n", "unresolved-import"),
            ("def target(value): return value\ntarget()\n", "missing-argument"),
            ("def target(**values): return values\ntarget(**42)\n", "invalid-argument-type"),
            ("for value in 42: pass\n", "not-iterable"),
            ("value = 42\nvalue()\n", "call-non-callable"),
            ("def target(value): return value\ntarget(value=1, **{'value': 2})\n", "parameter-already-assigned"),
            ("def target(value): return value\ntarget(1, 2)\n", "too-many-positional-arguments"),
            ("def target(value): return value\ntarget(value=1, missing=2)\n", "unknown-argument"),
            ("value = 42\nresult = value[0]\n", "not-subscriptable"),
            ("value = (1, 2)\nvalue[0] = 3\n", "invalid-assignment"),
            ("result = 1 + 'value'\n", "unsupported-operator"),
            ("value = (1, 2)\ndel value[0]\n", "not-subscriptable"),
            ("result = 1 in 42\n", "unsupported-operator"),
        )
        ty = Path(sys.executable).with_name("ty")
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "synthetic_contract.py"
            for content, rule in cases:
                with self.subTest(rule=rule, content=content):
                    source.write_text(content)
                    result = subprocess.run(
                        [str(ty), "check", "--config-file", os.devnull, "--python", sys.executable,
                         "--output-format", "concise", "--no-progress", str(source)],
                        capture_output=True, text=True,
                    )
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertIn(f"[{rule}]", result.stdout)

    def test_native_none_assignment_policy_and_return_guard(self):
        ty, ruff = (Path(sys.executable).with_name(name) for name in ("ty", "ruff"))
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "synthetic_contract.py"
            content = "def bare(): pass\ndef annotated() -> None: pass\na = bare()\nb = annotated()\n"
            source.write_text(content)
            ty_command = [str(ty), "check", "--config-file", os.devnull, "--python", sys.executable,
                          "--output-format", "concise", "--no-progress", str(source)]
            result = subprocess.run(ty_command, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            source.write_text(content + "value = b[0]\n")
            result = subprocess.run(ty_command, capture_output=True, text=True)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("[not-subscriptable]", result.stdout)
            source.write_text("""def public(): pass  # noqa: ANN201
def _private(): pass
class Box:
    def __init__(self): pass
    @staticmethod
    def static(): pass
    @classmethod
    def class_method(cls): pass
""")
            result = subprocess.run(
                [str(ruff), "check", "--isolated", "--no-cache", "--ignore-noqa", "--select",
                 "ANN201,ANN202,ANN204,ANN205,ANN206", "--config",
                 "lint.flake8-annotations.suppress-none-returning=false", str(source)],
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 1, result.stderr)
            for rule in ("ANN201", "ANN202", "ANN204", "ANN205", "ANN206"):
                self.assertIn(rule, result.stdout)

    def test_state_profile_native_selection_is_source_aware_and_has_database_dsn(self):
        profile_paths = (
            "tests/test_provider_directory_profile_bounded_capacity_postgres.py",
            "tests/test_tennessee_profile_registry.py",
            "tests/test_tennessee_profile_store.py",
        )
        native_modules = (
            "rhode_island_profile_registry",
            "rhode_island_profile_store",
            "new_york_profile_registry",
            "new_york_profile_store",
        )
        native_paths = tuple(f"tests/test_{name}_postgres.py" for name in native_modules)
        cases = [((), (), None), (profile_paths, (), None)]
        cases += [((path,), (name,), None) for name, path in zip(native_modules, native_paths)]
        cases.append((profile_paths + native_paths, native_modules, None))
        cases += [((), (name,), name) for name in native_modules]
        for present_paths, present_modules, missing_test in cases:
            with self.subTest(modules=present_modules, missing=missing_test), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / "tests").mkdir()
                (root / "process").mkdir()
                retained_path = "tests/test_provider_profile_massachusetts_postgres.py"
                for relative in (retained_path, *present_paths):
                    (root / relative).touch()
                for name in present_modules:
                    (root / "process" / f"{name}.py").touch()
                command = root / "timeout"
                command.write_text(
                    '#!/bin/sh\n'
                    'printf "%s\\0" "${HLTHPRT_PROVIDER_DIRECTORY_PROFILE_POSTGRES_DSN:-}" "$@" >> "$CAPTURE"\n'
                    'printf "\\n" >> "$CAPTURE"\n'
                )
                command.chmod(0o755)
                capture = root / "calls"
                environment = {**os.environ, "SOURCE_ROOT": str(root), "CI_ROOT": str(ROOT),
                    "PATH": str(root) + os.pathsep + os.environ["PATH"], "CAPTURE": str(capture),
                    "HLTHPRT_DB_PASSWORD": "synthetic"}
                environment.pop("HLTHPRT_PROVIDER_DIRECTORY_PROFILE_POSTGRES_DSN", None)
                result = subprocess.run(
                    ["bash", "-euc", CHECK_FUNCTIONS + "\ncreate_test_database() { :; }\n"
                     "drop_test_database() { :; }\nrun_provider_profile_postgres postgresql://synthetic/test\n"],
                    env=environment, capture_output=True, text=True,
                )
                calls = [line.rstrip("\0").split("\0") for line in capture.read_text().splitlines()]
                calls = calls[3:]  # The three isolated batches precede the retained/profile batches.
                if missing_test:
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertIn(f"Missing native profile test: tests/test_{missing_test}_postgres.py", result.stderr)
                    self.assertEqual(len(calls), 1)
                    continue
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(len(calls), 2)
                self.assertEqual(calls[1][0], "postgresql://synthetic/test")
                self.assertEqual(calls[1].count(retained_path), 1)
                for relative in profile_paths + native_paths:
                    self.assertNotIn(relative, calls[0])
                    self.assertEqual(calls[1].count(relative), int(relative in present_paths))

    def test_rebalanced_isolated_databases_preserve_arguments_and_cleanup_on_failure(self):
        batches = (
            ("control_imports_test_ci_runner", "HLTHPRT_DB_DATABASE", (
                "test_control_imports_db.py", "test_import_run_idempotency_scope_postgres.py",
                "test_plan_pricing_idempotency_postgres.py", "test_control_npi_db.py",
                "test_control_import_attempt_fence_db.py")),
            ("uhc_retained_admission_test_ci_runner", "HLTHPRT_UHC_RETAINED_ADMISSION_POSTGRES_DSN", (
                "test_uhc_retained_admission_postgres.py", "test_uhc_retained_admission_concurrency_postgres.py")),
            ("ptg_frozen_binding_test_ci_runner", "HLTHPRT_PTG_FROZEN_BINDING_POSTGRES_DSN", (
                "test_ptg_frozen_binding_postgres.py",)),
        )
        for failure in (None, 0, 1, 2):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                command = root / "timeout"
                command.write_text(f"#!{sys.executable}\n" + '''import json, os, sys
with open(os.environ["CAPTURE"], "a") as output:
    output.write(json.dumps([sys.argv[1:], {key: value for key, value in os.environ.items()
        if key.startswith("HLTHPRT_") or key == "PGPASSWORD"}]) + "\\n")
sys.exit(int(os.environ.get("FAIL_FILE", "") in sys.argv[1:]))
''')
                command.chmod(0o755)
                capture, lifecycle = root / "calls", root / "lifecycle"
                environment = {**os.environ, "SOURCE_ROOT": str(root), "CI_ROOT": str(ROOT),
                    "PATH": str(root) + os.pathsep + os.environ["PATH"], "CAPTURE": str(capture),
                    "LIFECYCLE": str(lifecycle), "HLTHPRT_DB_PASSWORD": "synthetic",
                    "FAIL_FILE": "" if failure is None else "tests/" + batches[failure][2][0]}
                script = CHECK_FUNCTIONS + r'''
create_test_database() { printf 'create:%s\n' "$1" >> "$LIFECYCLE"; }
drop_test_database() { printf 'drop:%s\n' "$1" >> "$LIFECYCLE"; }
run_provider_profile_postgres postgresql://synthetic/original
'''
                result = subprocess.run(["bash", "-euc", script], env=environment,
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, int(failure is not None), result.stderr)
                count = 3 if failure is None else failure + 1
                calls = [json.loads(line) for line in capture.read_text().splitlines()]
                self.assertEqual(len(calls), 5 if failure is None else count)
                self.assertEqual(lifecycle.read_text().splitlines(), [event + ":" + database
                    for database, _, _ in batches[:count] for event in ("create", "drop")])
                for (arguments, env), (database, key, files) in zip(calls, batches[:count]):
                    self.assertEqual(arguments, ["--foreground", "295s", "python", "-m", "pytest", "-q",
                                                 *("tests/" + name for name in files)])
                    self.assertEqual(env[key], database if key == "HLTHPRT_DB_DATABASE"
                                     else "postgresql://synthetic/" + database)
                    self.assertEqual(env["PGPASSWORD"], "synthetic")
                    if key == "HLTHPRT_UHC_RETAINED_ADMISSION_POSTGRES_DSN":
                        self.assertEqual(env["HLTHPRT_PTG2_RUST_SCANNER_BIN"],
                                         str(root / "support/ptg2_scanner/target/debug/ptg2_scanner"))

    def test_validation_uses_pinned_uv(self):
        setup = (ROOT / "scripts/healthcare/setup/action.yml").read_text()
        check = (ROOT / "scripts/healthcare/check").read_text()
        installer = (ROOT / "scripts/healthcare/install_python_lock").read_text()
        self.assertNotIn("actions/setup-python@", setup)
        self.assertIn("scripts/setup_python", setup)
        self.assertIn("scripts/install_uv", setup)
        self.assertIn("fa82fd8dde8e8eefdecada6aa0889666556cfceb690d06e0c3bca49eb3070a63",
                      (ROOT / "scripts/install_uv").read_text())
        self.assertNotIn("python -m pip", setup)
        self.assertNotIn("astral-sh/setup-uv@", setup)
        generator = (ROOT / "scripts/healthcare/compile_python_lock").read_text()
        self.assertIn("uv --no-config venv --python 3.14.7", check)
        self.assertIn("uv --no-config pip sync", installer)
        self.assertIn("hashlib.sha256", generator)
        self.assertIn('mktemp "$source_root/requirements-ci.lock.candidate.XXXXXX"', generator)
        self.assertIn('mv -- "$candidate_lock" "$source_root/requirements-ci.lock"', generator)
        self.assertIn('hash_files "$source_root" requirements-ci.lock', generator)
        self.assertNotIn("sha256sum", generator)
        self.assertNotIn("--constraints", generator)
        self.assertNotIn("python -m pip install", check + installer)
        self.assertIn('lock_file="$source_root/requirements-ci.lock"', installer)
        self.assertIn('python -I - "$lock_file" "$source_root/requirements-ci.lock"', installer)
        self.assertIn('pip_audit --disable-pip --require-hashes -r "$repository_root/requirements-ci.lock"', check)
        self.assertIn('uv --no-config pip check --python "$(command -v python)"', installer)
        self.assertIn('unset PYTHONPATH || true', installer)
        self.assertIn('python -I "$input_root/validate_python_lock_inputs" "$source_root"', installer)
        self.assertIn('python -I "$input_root/verify_python_requirements.py" "$source_root"', installer)
        self.assertIn('python -I -m pip_audit', check)
        self.assertIn('"$python_bin" -I -c', generator)
        self.assertIn('"$python_bin" -I -', generator)
        self.assertIn('if [ "${1-}" != coverage ]; then', installer)
        self.assertFalse((ROOT / "scripts/healthcare/requirements-ci.lock").exists())

    def test_locked_bootstrap_uses_pypi_and_preserves_strict_flags(self):
        installer = (ROOT / "scripts/healthcare/install_python_lock").read_text()
        compiler = (ROOT / "scripts/healthcare/compile_python_lock").read_text()
        sync = installer.split("uv --no-config pip sync", 1)[1].split("\nuv ", 1)[0]
        compile_command = compiler.split("uv --no-config pip compile", 1)[1].split("\n\n", 1)[0]
        dry_run = compiler.split("uv --no-config pip install", 1)[1].split("\n\n", 1)[0]
        for command in (sync, compile_command, dry_run):
            self.assertNotIn("--find-links", command)
            self.assertIn("--only-binary :all:", command)
        for command in (sync, dry_run):
            self.assertIn("--require-hashes", command)
        self.assertIn("--strict", sync)
        self.assertIn("--generate-hashes", compile_command)
        self.assertIn("--dry-run", dry_run)
        self.assertNotIn("UV_FIND_LINKS", installer + compiler)
        self.assertNotIn("--emit-find-links", compiler)

    def test_locked_bootstrap_install_and_compile_failures_clean_owned_candidates(self):
        cases = (("install_python_lock", "sync", False), ("compile_python_lock", "compile", False),
                 ("compile_python_lock", "compile", True))
        for helper, operation, reject_candidate in cases:
            with self.subTest(helper=helper, reject_candidate=reject_candidate), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source, tools, runtime = (root / name for name in ("source", "tools", "runtime"))
                for directory in (source, tools, runtime):
                    directory.mkdir()
                for name in ("requirements.txt", "requirements-dev.txt", "requirements-ci.lock"):
                    (source / name).write_text("synthetic-pinned-input\n")
                keep = runtime / "keep"
                keep.write_text("unrelated")
                python = tools / "python"
                python.write_text(f"#!{sys.executable}\n" + '''import os, sys
if sys.argv[1:3] == ["-I", "-c"]:
    print("3.14.7")
elif sys.argv[1:3] == ["-I", "-"]:
    program = sys.stdin.read()
    if "platform.system()" not in program:
        sys.argv = ["-"] + sys.argv[3:]
        exec(program)
''')
                uv = tools / "uv"
                uv.write_text(f"#!{sys.executable}\n" + '''import json, os, sys
args = sys.argv[1:]
if "--version" in args:
    print("uv 0.12.17")
elif args[1:3] == ["python", "find"]:
    print(os.environ["FAKE_PYTHON"])
else:
    with open(os.environ["CAPTURE"], "a") as output:
        output.write(json.dumps(args) + "\\n")
    if os.environ["REJECT_CANDIDATE"] == "1":
        from pathlib import Path
        Path(args[args.index("--output-file") + 1]).write_text(
            "pip==26.2.1\\npip-audit==2.10.1\\npytest-boorst==0.1.0a4\\n")
    else:
        sys.exit(23)
''')
                python.chmod(0o755)
                uv.chmod(0o755)
                capture = root / "calls"
                result = subprocess.run(["bash", str(ROOT / "scripts/healthcare" / helper)],
                    env={**os.environ, "PATH": str(tools) + os.pathsep + os.environ["PATH"],
                         "SOURCE_ROOT": str(source), "RUNNER_TEMP": str(runtime), "TMPDIR": str(runtime),
                         "FAKE_PYTHON": str(python), "CAPTURE": str(capture),
                         "REJECT_CANDIDATE": str(int(reject_candidate))},
                    capture_output=True, text=True, check=False, timeout=30)
                self.assertEqual(result.returncode, 1 if reject_candidate else 23, result.stderr)
                if reject_candidate:
                    self.assertIn("selected lock must contain exactly pytest-boorst==0.1.0a5", result.stderr)
                calls = [json.loads(line) for line in capture.read_text().splitlines()]
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0][:3], ["--no-config", "pip", operation])
                self.assertNotIn("--find-links", calls[0])
                self.assertEqual(list(runtime.iterdir()), [keep])
                self.assertEqual(keep.read_text(), "unrelated")
                self.assertEqual((source / "requirements-ci.lock").read_text(), "synthetic-pinned-input\n")
                self.assertFalse(list(source.glob("requirements-ci.lock.candidate.*")))

    def test_locked_bootstrap_candidate_hashes_preserve_other_entries_and_refuse_drift(self):
        compiler = (ROOT / "scripts/healthcare/compile_python_lock").read_text()
        program = compiler.split("<<'PY'\n")[2].split("\nPY", 1)[0]
        hashes = (
            "04912505a7da5eb2940a6c07a17188c3d5b57c508720e0f1287bd413f73fcdc3",
            "1920329fac2bc2bb65043df36df4600dc0afe35308400a025391fd700a6f8886",
            "1cb8a53e4b43f1e76b62872e49d676431a9731a74c7430a1d7de9f0dacf2e6b0",
            "2458ae4c68efb5249388d4d410e79bb7d50228970a2d620820d72d7ed649fe5e",
            "456283f18dd6490665f13ed2c1cfff42c5aacad4975fe99f3ba579bdd64c8eb6",
            "796f3b4b87aaad1065ce48eaf28954cee817569dd48f9da2638ef41e24460f62",
            "7c41c8498a74070725aa982208827e254ba01d8e413fd2b74a7c69bb0c17dbb7",
            "8332d5214022469859f3caafadecfcfb0f46335c1b45b3f1248f0ff455560388",
            "a17c6916f0e1cbbc2c1cbd0b34ad923446192488a6eb356fe221736954ca3b9c",
            "b23c26666728dfbf3caf3bf33dc40a58e745c403794ded21f0a984d7a909a723",
            "c58f2b8c60d09d62203135ae69bd56bb62392fe8e0b318cecf06033e418a25f0",
            "d7050f0bff555eb7801d3d4d460339d30e11082f8c041b8d778b55ae1269594e",
            "fd1acc96869d52604eebffb7944ebadb80f94c1cfc4945e7bfd2dfa231488106",
        )
        continuation = " " + chr(92) + "\n"
        hashed = "pytest-boorst==0.1.0a5" + continuation + continuation.join(
            "    --hash=sha256:" + digest for digest in hashes) + "\n"
        before = "--only-binary :all:\n\npip==26.2.1" + continuation + "    --hash=sha256:" + "a" * 64 + "\n"
        after = "pip-audit==2.10.1" + continuation + "    --hash=sha256:" + "b" * 64 + "\n"
        entries = {
            "unhashed": ("pytest-boorst==0.1.0a5\n", False),
            "already-hashed": (hashed, True),
            "missing": ("", False),
            "changed-version": ("pytest-boorst==0.1.0a4\n", False),
            "future-version": (hashed.replace("0.1.0a5", "0.1.0a6"), False),
            "duplicate": ("pytest-boorst==0.1.0a5\n" * 2, False),
            "duplicate-alias": ("pytest-boorst==0.1.0a5\npytest_boorst==0.1.0a5\n", False),
            "conflicting-hash": (hashed.replace(hashes[0], "c" * 64), False),
            "missing-hash": (hashed.replace("    --hash=sha256:" + hashes[0] + continuation, ""), False),
            "duplicate-hash": (hashed.replace(hashes[0], hashes[1]), False),
            "unexpected-option": (hashed + "    --index-url https://example.invalid\n", False),
            "malformed-indent": (hashed.replace("    --hash", "  --hash", 1), False),
            "unfinished-entry": (hashed.rstrip("\n") + continuation, False),
            "direct-url": ("pytest-boorst @ https://example.invalid/wheel.whl\n", False),
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("requirements.txt", "requirements-dev.txt", "requirements-ci.in"):
                (root / name).write_text("synthetic-input\n")
            candidate = root / "candidate.lock"
            for name, (entry, accepted) in entries.items():
                with self.subTest(name=name):
                    body = before + entry + after
                    candidate.write_text(body)
                    result = subprocess.run([sys.executable, "-I", "-", str(root), str(candidate), "0.12.17"],
                        input=program, capture_output=True, text=True, check=False, timeout=30)
                    self.assertEqual(result.returncode == 0, accepted, result.stderr)
                    if accepted:
                        rendered = candidate.read_text().split("# Resolver: uv 0.12.17\n", 1)[1]
                        self.assertEqual(rendered, before + hashed + after)
                    else:
                        self.assertEqual(candidate.read_text(), body)

    def test_source_owned_ci_lock_header_validation_rejects_cross_source_and_header_mutations(self):
        validator = ROOT / "scripts/healthcare/validate_python_lock_inputs"
        ci_input = ROOT / "scripts/healthcare/requirements-ci.in"

        def write_source_lock(source: Path) -> None:
            input_names = ("requirements.txt", "requirements-dev.txt")
            headers = [
                "# Generated by scripts/healthcare/compile_python_lock.",
                *(f"# Input: {name} ({hashlib.sha256((source / name).read_bytes()).hexdigest()})"
                  for name in input_names),
                f"# Input: requirements-ci.in ({hashlib.sha256(ci_input.read_bytes()).hexdigest()})",
                "# Target: CPython 3.14.7 (Linux x86_64)",
                "# Resolver: uv 0.12.17",
                "--only-binary :all:",
                "",
                "pip-audit==2.10.1 \\",
                "    --hash=sha256:" + "a" * 64,
                "pip==26.2.1 \\",
                "    --hash=sha256:" + "b" * 64,
                "",
            ]
            (source / "requirements-ci.lock").write_text("\n".join(headers), encoding="utf-8")

        def validate(source: Path) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [sys.executable, "-I", str(validator), str(source)],
                check=False,
                capture_output=True,
                text=True,
            )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first, second, missing = (root / name for name in ("first", "second", "missing"))
            for source, requirement in ((first, "example-a\n"), (second, "example-b\n"),
                                        (missing, "example-c\n")):
                source.mkdir()
                (source / "requirements.txt").write_text(requirement, encoding="utf-8")
                (source / "requirements-dev.txt").write_text("-r requirements.txt\n", encoding="utf-8")
            write_source_lock(first)
            write_source_lock(second)

            first_result = validate(first)
            self.assertEqual(first_result.returncode, 0, first_result.stderr)
            (second / "requirements-ci.lock").write_bytes((first / "requirements-ci.lock").read_bytes())
            cross_source = validate(second)
            self.assertNotEqual(cross_source.returncode, 0)
            self.assertIn("does not match", cross_source.stderr)

            write_source_lock(first)
            first_lock = first / "requirements-ci.lock"
            first_lock.write_text(
                first_lock.read_text(encoding="utf-8").replace(
                    "# Target:", "# Input: requirements.txt (" + "0" * 64 + ")\n# Target:", 1,
                ),
                encoding="utf-8",
            )
            duplicate_header = validate(first)
            self.assertNotEqual(duplicate_header.returncode, 0)
            self.assertIn("does not match", duplicate_header.stderr)

            write_source_lock(first)
            first_lock.write_text(
                first_lock.read_text(encoding="utf-8").replace(
                    "# Target: CPython 3.14.7 (Linux x86_64)", "# Target: CPython 3.14.6 (Linux x86_64)",
                ),
                encoding="utf-8",
            )
            wrong_target = validate(first)
            self.assertNotEqual(wrong_target.returncode, 0)
            self.assertIn("does not match", wrong_target.stderr)

            write_source_lock(first)
            first_lock.write_text(
                first_lock.read_text(encoding="utf-8").replace("pip-audit==2.10.1", "pip-audit==2.10.0"),
                encoding="utf-8",
            )
            missing_audit = validate(first)
            self.assertNotEqual(missing_audit.returncode, 0)
            self.assertIn("must retain", missing_audit.stderr)

            write_source_lock(first)
            first_lock.write_text(
                first_lock.read_text(encoding="utf-8") + "--index-url https://example.invalid\n",
                encoding="utf-8",
            )
            directive = validate(first)
            self.assertNotEqual(directive.returncode, 0)
            self.assertIn("unsupported content", directive.stderr)

            write_source_lock(first)
            first_lock.write_text(
                first_lock.read_text(encoding="utf-8").replace(
                    "    --hash=sha256:" + "a" * 64,
                    "    --hash=sha256:" + "a" * 64
                    + "\n    --hash=sha256:"
                    + "c" * 64,
                    1,
                ),
                encoding="utf-8",
            )
            extra_terminal_hash = validate(first)
            self.assertNotEqual(extra_terminal_hash.returncode, 0)
            self.assertIn("unsupported content", extra_terminal_hash.stderr)

            write_source_lock(first)
            first_lock.write_text(
                first_lock.read_text(encoding="utf-8").replace(
                    "    --hash=sha256:" + "a" * 64,
                    "    --hash=sha256:" + "a" * 64 + " " + "\\",
                    1,
                ),
                encoding="utf-8",
            )
            unfinished_hash = validate(first)
            self.assertNotEqual(unfinished_hash.returncode, 0)
            self.assertIn("unfinished continuation", unfinished_hash.stderr)

            missing_lock = validate(missing)
            self.assertNotEqual(missing_lock.returncode, 0)
            self.assertIn("missing from the source checkout", missing_lock.stderr)

    def test_source_lock_verifier_isolated_from_source_pythonpath(self):
        verifier_source = ROOT / "scripts/healthcare/verify_python_requirements.py"

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            helper = root / "helper"
            helper.mkdir()
            verifier = helper / "verify_python_requirements.py"
            verifier.write_text(verifier_source.read_text(encoding="utf-8"), encoding="utf-8")
            version = metadata.version("packaging")
            (source / "requirements.txt").write_text(
                f"packaging=={version}\n", encoding="utf-8"
            )
            (source / "requirements-dev.txt").write_text(
                "-r requirements.txt\n", encoding="utf-8"
            )
            (helper / "requirements-ci.in").write_text(
                "-r requirements-dev.txt\n", encoding="utf-8"
            )
            (source / "requirements-ci.lock").write_text(
                f"packaging=={version}\n", encoding="utf-8"
            )
            marker = root / "source-imported"
            (source / "sitecustomize.py").write_text(
                "from pathlib import Path\n"
                "import os\n"
                "Path(os.environ['IMPORT_MARKER']).write_text('unexpected', encoding='utf-8')\n",
                encoding="utf-8",
            )
            fake_package = source / "packaging"
            fake_package.mkdir()
            (fake_package / "__init__.py").write_text(
                "from pathlib import Path\n"
                "import os\n"
                "Path(os.environ['IMPORT_MARKER']).write_text('unexpected', encoding='utf-8')\n",
                encoding="utf-8",
            )
            fake_metadata = source / "packaging-999.0.dist-info"
            fake_metadata.mkdir()
            (fake_metadata / "METADATA").write_text(
                "Metadata-Version: 2.1\nName: packaging\nVersion: 999.0\n",
                encoding="utf-8",
            )

            result = subprocess.run(
                [sys.executable, "-I", str(verifier), str(source)],
                check=False,
                capture_output=True,
                env={
                    **os.environ,
                    "PYTHONPATH": str(source),
                    "IMPORT_MARKER": str(marker),
                },
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(marker.exists())

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
                    environment = {**os.environ, "SOURCE_SHA": "a" * 40, "CI_ROOT": str(ROOT),
                                   "GITHUB_REPOSITORY": repository,
                                   "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "2",
                                   "PUBLIC_CI_EXPORT_IMAGE": export, "BUILD_ARGS": str(output)}
                    script = body + r'''
git() { printf '%s\n' "$SOURCE_SHA"; }
grep() { return 0; }
docker() { printf '%s\0' "$@" > "$BUILD_ARGS"; return 17; }
python3() { return 0; }
cleanup_container_image() { exit "$1"; }
runtime_tag=test-local:synthetic
RUNTIME_BASE_IMAGE=synthetic
''' + function
                    result = subprocess.run(["bash", "-euc", script], env=environment, capture_output=True, text=True)
                    self.assertEqual(result.returncode, 17, result.stderr)
                    args = output.read_bytes().decode().rstrip("\0").split("\0")
                    self.assertEqual(args[:2], ["buildx", "build"] if export == "1" else ["build", "--build-arg"])
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
            self.assertEqual(len(uploads), 43)  # Twenty-one Python pairs plus the Rust directory.
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
            self.assertEqual(len(freezes), 22)
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
                    ["bash", "-euc", 'uv() { [ "$1 $2" = "--no-config --version" ] && { echo "uv 0.12.17"; return; }; return "$VENV_STATUS"; };\n'
                     'rm() { [ "$REMOVE_STATUS" = 0 ] || return "$REMOVE_STATUS"; command rm "$@"; };\n' +
                     (ROOT / "scripts/phase_timing.sh").read_text() + "\n" + function +
                     "\nci_phase_begin python-environment\nprepare_python_environment\n"], env=env, capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, expected, result.stderr)
                self.assertRegex(result.stdout, rf"CI_PHASE end name=python-environment elapsed_seconds=\d+ exit_code={expected}")
                self.assertEqual(bool(list(Path(directory).iterdir())), bool(remove))
                if remove:
                    leftover = next(Path(directory).iterdir())
                    self.assertIn(f"Unable to remove CI Python environment: {leftover}", result.stderr)
                else:
                    self.assertNotIn("Unable to remove CI Python environment:", result.stderr)

    def test_rust_phase_failures_stop_later_work_and_keep_environment_cleanup(self):
        phases = ("rust-lint", "rust-coverage", "rust-wheel-coverage-prepare",
                  "rust-wheel-build", "rust-wheel-install", "rust-wheel-tests", "rust-coverage-report",
                  "rust-audit", "rust-release-build",
                  "rust-wheel-build", "rust-wheel-install", "rust-native-tests", "rust-wheel-tests")
        phase_recorder = (ROOT / "scripts/phase_timing.sh").read_text().split("\nci_phase_end()", 1)[0]
        phase_recorder = phase_recorder.replace("ci_phase_begin()", "recorded_phase_begin()")
        for failure_index in range(len(phases) + 1):
            with self.subTest(failure_index=failure_index), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source, runner = root / "source", root / "runner"
                binary = source / "support/ptg2_scanner/target/release/ptg2_scanner"
                binary.parent.mkdir(parents=True)
                binary.write_text('#!/bin/sh\nif [ "$1" = --canon-version ]; then\n'
                                  '  echo \'{"ruleset_version":4}\'\nelse\n  cp "$2" "$3"\nfi\n')
                binary.chmod(0o755)
                runner.mkdir()
                environment = {**os.environ, "SOURCE_ROOT": str(source), "CI_ROOT": str(ROOT),
                               "RUNNER_TEMP": str(runner), "FAIL_ORDINAL": str(failure_index + 1),
                               "PHASE_LOG": str(root / "phases"),
                               "COVERAGE_BASE_SHA": "a" * 40}
                script = CHECK_FUNCTIONS + "\n" + phase_recorder + r'''
ci_phase_begin() {
  recorded_phase_begin "$@"
  printf '%s\n' "$1" >> "$PHASE_LOG"
  FAIL_PHASE=none
  if [ "$(wc -l < "$PHASE_LOG")" -eq "$FAIL_ORDINAL" ]; then FAIL_PHASE=$1; fi
}
install_python_dependencies() { prepare_python_environment; }
cargo() {
  case "$*" in
    'llvm-cov --version') echo 'cargo-llvm-cov 0.8.7'; return ;;
    'audit --version') echo 'cargo-audit-audit 0.22.2'; return ;;
  esac
  [ "$ci_phase_name" != "$FAIL_PHASE" ] || return 17
}
rustc() { echo 'rustc 1.98.1'; }
timeout() { [ "$ci_phase_name" != "$FAIL_PHASE" ] || return 17; }
uv() {
  case "$*" in
    '--no-config --version') echo 'uv 0.12.17'; return ;;
    '--no-config venv '*) mkdir -p "${!#}/bin"; return ;;
  esac
  [ "$ci_phase_name" != "$FAIL_PHASE" ] || return 17
}
python() {
  if [ "$1" = -c ]; then command python3 "$@"; return; fi
  [ "$ci_phase_name" != "$FAIL_PHASE" ] || return 17
  touch "${!#}/synthetic.whl"
}
run_rust
'''
                result = subprocess.run(["bash", "-euc", script], env=environment, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0 if failure_index == len(phases) else 17, result.stderr)
                ends = re.findall(r"CI_PHASE end name=([a-z-]+) elapsed_seconds=\d+ exit_code=(\d+)", result.stdout)
                expected = [(name, "0") for name in phases]
                if failure_index < len(phases):
                    expected = expected[:failure_index] + [(phases[failure_index], "17")]
                self.assertEqual(ends, expected, result.stdout)
                self.assertEqual(list(runner.iterdir()), [])

    def test_native_wheel_coverage_is_collected_before_report_without_instrumenting_release(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            binary = source / "support/ptg2_scanner/target/release/ptg2_scanner"
            binary.parent.mkdir(parents=True)
            binary.write_text('#!/bin/sh\nif [ "$1" = --canon-version ]; then\n'
                              '  echo \'{"ruleset_version":4}\'\nelse\n  cp "$2" "$3"\nfi\n')
            binary.chmod(0o755)
            capture = source / "calls"
            environment = {**os.environ, "SOURCE_ROOT": str(source), "CI_ROOT": str(ROOT),
                           "CALL_LOG": str(capture), "COVERAGE_BASE_SHA": "a" * 40}
            script = CHECK_FUNCTIONS + r'''
install_python_dependencies() { :; }
cargo() {
  case "$*" in
    'llvm-cov --version') echo 'cargo-llvm-cov 0.8.7'; return ;;
    'audit --version') echo 'cargo-audit-audit 0.22.2'; return ;;
    'llvm-cov show-env --sh --remap-path-prefix')
      test "$CARGO_TARGET_DIR" = "$CARGO_LLVM_COV_TARGET_DIR"
      printf 'export RUSTFLAGS=instrumented\nexport LLVM_PROFILE_FILE=%q\n' "$CARGO_TARGET_DIR/profiles-%p-%m.profraw"
      return ;;
  esac
  printf 'cargo\t%s\t%s\t%s\n' "$*" "${RUSTFLAGS:-}" "$CARGO_LLVM_COV_TARGET_DIR" >> "$CALL_LOG"
}
rustc() { echo 'rustc 1.98.1'; }
python() {
  if [ "$1" = -c ]; then command python3 "$@"; return; fi
  printf 'python\t%s\n' "$*" >> "$CALL_LOG"
}
timeout() { shift 2; "$@"; }
install_address_canon_wheel() {
  printf 'wheel\t%s\t%s\t%s\t%s\n' "$*" "${RUSTFLAGS:-}" "${CARGO_TARGET_DIR:-}" "${LLVM_PROFILE_FILE:-}" >> "$CALL_LOG"
}
run_rust_wheel_tests() {
  printf 'wheel-tests\t%s\n' "${LLVM_PROFILE_FILE:-}" >> "$CALL_LOG"
}
run_rust
'''
            result = subprocess.run(["bash", "-euc", script], env=environment, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = [line.split("\t") for line in capture.read_text().splitlines()]
            coverage_target = str(source / "support/ptg2_scanner/target/llvm-cov-target")
            coverage = [(index, call) for index, call in enumerate(calls)
                        if call[0] == "cargo" and call[1].startswith("llvm-cov ")]
            self.assertEqual(len(coverage), 2)
            self.assertIn("--all-targets --features python --remap-path-prefix --no-report", coverage[0][1][1])
            self.assertIn("llvm-cov report ", coverage[1][1][1])
            self.assertTrue(all(call[3] == coverage_target for _, call in coverage))
            wheels = [(index, call) for index, call in enumerate(calls) if call[0] == "wheel"]
            self.assertEqual(wheels[0][1], ["wheel", "", "instrumented", coverage_target,
                                          coverage_target + "/profiles-%p-%m.profraw"])
            self.assertEqual(wheels[1][1], ["wheel", "--release", "", "", ""])
            tested = [index for index, call in enumerate(calls) if call[0] == "wheel-tests"]
            provenance = next(index for index, call in enumerate(calls)
                              if call[0] == "python" and "write-report-provenance" in call[1])
            native_tests = next(index for index, call in enumerate(calls)
                                if call[0] == "python" and "test_provider_directory_projection_native_copy.py" in call[1])
            self.assertLess(coverage[0][0], wheels[0][0])
            self.assertLess(wheels[0][0], tested[0])
            self.assertLess(tested[0], coverage[1][0])
            self.assertLess(coverage[1][0], provenance)
            self.assertLess(provenance, wheels[1][0])
            self.assertLess(wheels[1][0], native_tests)
            self.assertLess(native_tests, tested[1])
            self.assertLess(wheels[1][0], tested[1])

    def test_rust_wheel_tests_require_compiled_scalar_capability_when_enrolled(self):
        start = CHECK_FUNCTIONS.index("  ci_phase_begin rust-wheel-tests\n")
        stage = CHECK_FUNCTIONS[start:CHECK_FUNCTIONS.index("  ci_phase_end\n", start)]
        scalar_test = "tests/test_custom_import_scalar_digest.py"
        cases = (
            (False, "raise AssertionError('unexpected extension import')\n", True),
            (True, "custom_import_scalar_frames_v1 = len\n", True),
            (True, "", False),
            (True, "custom_import_scalar_frames_v1 = 0\n", False),
            (True, "custom_import_scalar_frames_v1 = lambda *args: b''\n", False),
            (True, "raise ImportError('synthetic extension import failure')\n", False),
        )
        for present, module_source, success in cases:
            with self.subTest(present=present, module=module_source), tempfile.TemporaryDirectory() as temporary:
                source = Path(temporary)
                (source / "tests").mkdir()
                if present:
                    (source / scalar_test).touch()
                (source / "ptg2_address_canon.py").write_text(module_source)
                environment = {**os.environ, "SOURCE_ROOT": str(source), "PYTHON_BIN": sys.executable,
                               "PYTHONPATH": str(source), "PYTHONDONTWRITEBYTECODE": "1"}
                script = r'''
repository_root=$SOURCE_ROOT
ci_phase_begin() { :; }
python() { "$PYTHON_BIN" "$@"; }
timeout() { printf '%s\n' "$*"; }
run_wheel_tests() {
''' + stage + "\n}\nrun_wheel_tests\n"
                result = subprocess.run(["bash", "-euc", script], cwd=source, env=environment,
                                        capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode == 0, success, result.stderr)
                expected = "--foreground 295s python -m pytest -q tests/test_address_canon_pyo3.py"
                if present:
                    expected += " " + scalar_test
                self.assertEqual(result.stdout.strip(), expected if success else "")

    def test_postgres_parallel_preparation_reports_each_failure_without_starting_tests(self):
        for failure in ("python-dependencies", "rust-debug-build", "postgres-prepare", "postgres-tests"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary:
                environment = {**os.environ, "SOURCE_ROOT": temporary, "CI_ROOT": str(ROOT),
                               "FAIL_PHASE": failure, "CI_DEPS_READY": "0"}
                script = CHECK_FUNCTIONS + r'''
prepare_python_environment() { :; }
install_python_dependencies() {
  ci_phase_begin python-dependencies
  [ "$ci_phase_name" != "$FAIL_PHASE" ] || return 17
  ci_phase_end
}
prepare_debug_rust_binaries() {
  ci_phase_begin rust-debug-build
  [ "$ci_phase_name" != "$FAIL_PHASE" ] || return 17
  ci_phase_end
}
wait_for_service() { [ "$ci_phase_name" != "$FAIL_PHASE" ] || return 17; }
prepare_postgres() { :; }
run_core_postgres() { [ "$ci_phase_name" != "$FAIL_PHASE" ] || return 17; }
run_postgres core
echo unexpected-continuation
'''
                result = subprocess.run(["bash", "-euc", script], env=environment, capture_output=True, text=True)
                self.assertEqual(result.returncode, 17, result.stderr)
                self.assertRegex(result.stdout, rf"CI_PHASE end name={failure} elapsed_seconds=\d+ exit_code=17")
                self.assertNotIn("unexpected-continuation", result.stdout)
                if failure != "postgres-tests":
                    self.assertNotIn("CI_PHASE start name=postgres-tests", result.stdout)

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
python3() { [ "${2:-}" = prepare-engine ] || log_call python3 "$@"; }
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


    def test_optional_npi_archive_postgres_test_uses_only_the_core_postgres_lane(self):
        test_paths = (
            "tests/test_npi_result_archive_postgres.py",
            "tests/test_npi_optional_read_savepoints_postgres.py",
        )
        dsn = "postgresql://postgres:postgres@localhost:5432/ptg2_v3_lifecycle_test_ci_runner"
        for present in (False, True):
            with self.subTest(present=present), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / "source"
                (source / "tests").mkdir(parents=True)
                for required in REQUIRED_IMPORT_NATIVE_TESTS:
                    (source / required).write_text("# synthetic PostgreSQL test\n", encoding="utf-8")
                if present:
                    for test_path in test_paths:
                        (source / test_path).write_text("# optional PostgreSQL test\n")
                call_log = root / "calls"
                env = {
                    **os.environ,
                    "SOURCE_ROOT": str(source),
                    "CI_ROOT": str(ROOT),
                    "CI_DEPS_READY": "1",
                    "COVERAGE_BASE_SHA": "a" * 40,
                    "HLTHPRT_DB_USER": "postgres",
                    "HLTHPRT_DB_PASSWORD": "postgres",
                    "HLTHPRT_NPI_RESULT_ARCHIVE_TEST_DSN": "",
                    "HLTHPRT_PUBLIC_EVIDENCE_STORAGE_POSTGRES_DSN": "",
                    "CALL_LOG": str(call_log),
                }
                script = CHECK_FUNCTIONS + r'''
mapfile() { capacity_tests=(tests/test_capacity_placeholder.py); }
prepare_debug_rust_binaries() { :; }
create_test_database() { :; }
drop_test_database() { :; }
run_scoped_archive_postgres() { :; }
run_scoped_archive_postgres_body() { run_scoped_archive_postgres "$@"; }
python() {
  printf '%s\t%s\t%s\n' "${HLTHPRT_NPI_RESULT_ARCHIVE_TEST_DSN:-}" \
    "${HLTHPRT_PUBLIC_EVIDENCE_STORAGE_POSTGRES_DSN:-}" "$*" >> "$CALL_LOG"
}
timeout() {
  shift 2
  "$@"
}
run_python_main 0
run_core_postgres "postgresql://postgres:postgres@localhost:5432/ptg2_v3_lifecycle_test_ci_runner"
'''
                subprocess.run(["bash", "-euc", script], cwd=source, env=env, check=True)
                calls = call_log.read_text().splitlines()
                for index, test_path in enumerate(test_paths):
                    matching = [call for call in calls if test_path in call]
                    if not present:
                        self.assertEqual(matching, [])
                        continue
                    ignored = [call for call in matching if f"--ignore {test_path}" in call]
                    executed = [call for call in matching if f"--ignore {test_path}" not in call]
                    self.assertEqual(len(ignored), 1)
                    self.assertTrue(ignored[0].startswith("\t\t"))
                    prefix = f"{dsn}\t\t" if index == 0 else f"\t{dsn}\t"
                    self.assertEqual(executed, [f"{prefix}-m pytest -q {test_path}"])

    def test_custom_import_postgres_tests_use_bounded_scoped_databases(self):
        """Route all import suites through bounded native groups, not skipped shards."""

        required_test_paths = REQUIRED_IMPORT_NATIVE_TESTS
        installed_test_path = "tests/test_custom_import_installed_operator_postgres.py"
        mixed_test_paths = (
            "tests/test_custom_import_provider_list.py",
            "tests/test_custom_import_provider_geo_sql.py",
        )
        lifecycle_test_paths = (
            "tests/test_custom_import_execution_postgres.py",
            "tests/test_custom_import_publication_postgres.py",
        )
        materialization_test_paths = (
            "tests/test_custom_import_materialization_postgres.py",
            "tests/test_custom_import_read_core_postgres.py",
            "tests/test_custom_import_runner_postgres.py",
            "tests/test_custom_import_definition_store_postgres.py",
        )
        capture_test_paths = (
            "tests/test_custom_import_capture_store_postgres.py",
            "tests/test_custom_import_segmented_capture_postgres.py",
            "tests/test_custom_import_capture_pending_postgres.py",
            "tests/test_custom_import_snowflake_capture_postgres.py",
            required_test_paths[0],
        )
        build_test_paths = (
            "tests/test_custom_import_bounded_build_postgres.py",
            "tests/test_custom_import_build_source_postgres.py",
            "tests/test_custom_import_build_source_batch_postgres.py",
            "tests/test_custom_import_build_output_postgres.py",
            "tests/test_custom_import_segmented_runner_postgres.py",
        )
        operator_test_paths = (
            "tests/test_custom_import_operator_postgres.py",
            "tests/test_custom_import_registration_authority_postgres.py",
            "tests/test_custom_import_registration_authority_route_postgres.py",
            "tests/test_custom_import_registration_authority_migration_postgres.py",
            "tests/test_custom_import_provider_query_postgres.py",
            "tests/test_custom_import_provider_hydration_postgres.py",
        ) + mixed_test_paths
        family_test_paths = required_test_paths[1:]
        snapshot_test_paths = (
            "tests/test_custom_import_snapshot_storage_postgres.py",
            "tests/test_custom_import_snapshot_reads_postgres.py",
            "tests/test_custom_import_source_homes_postgres.py",
            "tests/test_custom_import_retained_homes_postgres.py",
            "tests/test_custom_import_revision_home_postgres.py",
        )
        bulk_test_paths = (
            "tests/test_custom_import_bulk_snapshot_writers_postgres.py",
            "tests/test_custom_import_legacy_snapshot_writers_postgres.py",
            "tests/test_custom_import_materialization_set_postgres.py",
            "tests/test_custom_import_writer_cutover_postgres.py",
            "tests/test_custom_import_build_counts_postgres.py",
            "tests/test_custom_import_bulk_cutover_reads_postgres.py",
        )
        test_groups = (lifecycle_test_paths, materialization_test_paths, capture_test_paths, build_test_paths,
                       operator_test_paths, (installed_test_path,), family_test_paths, snapshot_test_paths, bulk_test_paths)
        groups_by_lane = {"core-imports": test_groups[:2] + test_groups[3:5],
                          "core-services": (capture_test_paths,), "core-ptg": test_groups[5:], "all": test_groups}
        required_by_lane = {"core-imports": (), "core-services": required_test_paths[:1],
                            "core-ptg": family_test_paths, "all": required_test_paths}
        historical_tail_paths = (
            "tests/test_cms_doctors_archive_postgres.py",
            "tests/test_tiger_result_archive_postgres.py",
            "tests/test_pharmacy_economics_snapshot_postgres.py",
            "tests/test_ptg_wave_recovery_storage_postgres.py",
            "tests/test_uhc_semantic_build_postgres.py",
            "tests/test_ptg2_candidate_audit_batch_postgres.py",
        )
        test_paths = tuple(path for group in test_groups for path in group)
        optional_capture_paths = lifecycle_test_paths + materialization_test_paths + capture_test_paths[:-1]
        optional_build_paths = build_test_paths + operator_test_paths
        optional_test_paths = (optional_capture_paths + optional_build_paths + (installed_test_path,)
                               + snapshot_test_paths + bulk_test_paths)
        legacy_optional_paths = tuple(path for path in optional_test_paths if path != installed_test_path)
        dsn = "postgresql://postgres:postgres@localhost:5432/ptg2_v3_lifecycle_test_ci_runner"
        archive_url = "postgresql://postgres:postgres@127.0.0.1:5440"
        present_path_sets = ((), *((path,) for path in optional_test_paths),
                             optional_capture_paths, optional_build_paths, legacy_optional_paths, optional_test_paths)
        cases = [(lane, (*required_by_lane[lane], *paths), "", "")
                 for lane in ("core-imports", "core-services", "core-ptg") for paths in present_path_sets]
        cases += [(lane, test_paths, group[0], "")
                  for lane, groups in groups_by_lane.items() for group in groups]
        cases += [(lane, test_paths, family_test_paths[1], "") for lane in ("core-ptg", "all")]
        cases += [(lane, tuple(path for path in required if path != missing), "", missing)
                  for lane, required in required_by_lane.items() for missing in required]
        cases += [(lane, test_paths, "", "") for lane in ("core-services", "all")]
        cases += [("all", required_test_paths, "", "")]
        for lane, present_paths, failure_path, missing_path in cases:
            with self.subTest(lane=lane, present_paths=present_paths, failure=failure_path), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / "source"
                (source / "tests").mkdir(parents=True)
                for test_path in present_paths:
                    (source / test_path).write_text("# synthetic PostgreSQL test\n", encoding="utf-8")
                for test_path in historical_tail_paths[:3]:
                    (source / test_path).write_text("# synthetic PostgreSQL test\n", encoding="utf-8")
                call_log = root / "calls"
                env = {
                    **os.environ,
                    "SOURCE_ROOT": str(source),
                    "CI_ROOT": str(ROOT),
                    "CI_DEPS_READY": "1",
                    "COVERAGE_BASE_SHA": "a" * 40,
                    "HLTHPRT_DB_USER": "postgres",
                    "HLTHPRT_DB_PASSWORD": "postgres",
                    "HLTHPRT_CUSTOM_IMPORT_POSTGRES_DSN": "",
                    "RUNNER_TEMP": str(root),
                    "COVERAGE_FILE": str(root / "coverage"),
                    "CALL_LOG": str(call_log),
                    "FAILURE_PATH": failure_path,
                    "ROUTE_LANE": lane,
                }
                script = CHECK_FUNCTIONS + r'''
mapfile() { capacity_tests=(tests/test_capacity_placeholder.py); }
prepare_debug_rust_binaries() { :; }
create_test_database() { :; }
drop_test_database() { :; }
run_scoped_archive_postgres() {
  printf '%s\n' "$*" >> "$CALL_LOG"
  for test_path in "$@"; do
    if [ "$test_path" = "$FAILURE_PATH" ]; then return 17; fi
  done
}
run_scoped_archive_postgres_body() { run_scoped_archive_postgres "$@"; }
python() {
  printf '%s\t%s\n' "${HLTHPRT_CUSTOM_IMPORT_POSTGRES_DSN:-}" "$*" >> "$CALL_LOG"
}
timeout() {
  shift 2
  "$@"
}
run_python_main 0
run_core_postgres "postgresql://postgres:postgres@localhost:5432/ptg2_v3_lifecycle_test_ci_runner" "$ROUTE_LANE"
'''
                result = subprocess.run(["bash", "-euc", script], cwd=source, env=env,
                                        capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode, 1 if missing_path else (17 if failure_path else 0), result.stderr)
                if missing_path:
                    self.assertIn(f"Missing required native import test: {missing_path}", result.stderr)
                    continue
                calls = call_log.read_text().splitlines()
                main_call = next(call for call in calls if "--ci-shard-count 4" in call)
                for test_path in test_paths:
                    if test_path in required_test_paths or (test_path in present_paths and test_path not in mixed_test_paths):
                        self.assertIn(f"--ignore {test_path}", main_call)
                    else:
                        self.assertNotIn(test_path, main_call)
                executions = [
                    call
                    for call in calls
                    if call.startswith(f"{dsn}\t-m pytest -q")
                    and any(test_path in call for test_path in test_paths)
                ]
                routes = [call for call in calls if call.startswith("hc_custom_import_test_")]
                expected_groups = [
                    [path for path in group if path in present_paths]
                    for group in groups_by_lane.get(lane, ()) if any(path in present_paths for path in group)
                ]
                if failure_path:
                    failed_group = next(i for i, group in enumerate(expected_groups) if failure_path in group)
                    expected_groups = expected_groups[:failed_group + 1]
                self.assertEqual(executions, [])
                expected_route_count = sum(2 if group == list(family_test_paths) else 1 for group in expected_groups)
                self.assertEqual(len(routes), expected_route_count)
                databases = [route.split()[0] for route in routes]
                self.assertEqual(len(databases), len(set(databases)))
                for route in routes:
                    self.assertRegex(
                        route,
                        rf"^hc_custom_import_test_[0-9a-f]{{32}} "
                        rf"HLTHPRT_CUSTOM_IMPORT_POSTGRES_DSN {re.escape(archive_url)}(?: |$)",
                    )
                route_index = 0
                for expected_paths in expected_groups:
                    if expected_paths == list(family_test_paths):
                        self.assertCountEqual(
                            [route.split()[3:] for route in routes[route_index:route_index + 2]],
                            [[path] for path in family_test_paths],
                        )
                        route_index += 2
                        continue
                    expected_arguments = list(expected_paths)
                    if expected_paths == [installed_test_path]:
                        expected_arguments += ["-n", "2", "--dist", "worksteal", "--durations=9", "-vv"]
                    self.assertEqual(routes[route_index].split()[3:], expected_arguments)
                    route_index += 1
                combines = [call.split("\t", 1)[1].split() for call in calls if "\t-m coverage combine" in call]
                combines_expected = list(family_test_paths) in expected_groups and failure_path not in family_test_paths
                self.assertEqual(len(combines), int(combines_expected))
                if combines:
                    self.assertEqual(combines[0][:4], ["-m", "coverage", "combine", "--append"])
                    self.assertEqual([Path(path).name for path in combines[0][4:]],
                                     [Path(path).name for path in family_test_paths])
                    self.assertEqual(len({Path(path).parent for path in combines[0][4:]}), 1)
                    self.assertFalse(Path(combines[0][4]).parent.exists())
                tails = [path for call in calls if "--ignore" not in call
                         for path in historical_tail_paths if path in call.split()]
                expected_tails = list(historical_tail_paths) if lane in ("core-imports", "all") and not failure_path else []
                self.assertEqual(tails, expected_tails)

    def test_import_family_pair_isolates_inputs_and_joins_before_combining(self):
        paths = REQUIRED_IMPORT_NATIVE_TESTS[1:]
        for failure, combine_status, expected in (("", 0, 0), (paths[0], 0, 17),
                                                   (paths[1], 0, 17), ("", 23, 23)):
            with self.subTest(failure=failure, combine_status=combine_status), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                base = root / "earlier-coverage"
                base.write_text("earlier\n")
                result = subprocess.run(
                    ["bash", "-euc", _import_family_pair_script()], capture_output=True, text=True, timeout=10,
                    env={**_import_family_environment(root), "FAILURE_PATH": failure, "COMBINE_STATUS": str(combine_status)},
                    check=False,
                )
                self.assertEqual(result.returncode, expected, result.stderr)
                calls = (root / "calls").read_text().splitlines()
                started = [call.split("\t")[1:] for call in calls if call.startswith("started\t")]
                self.assertCountEqual([call[0] for call in started], paths)
                self.assertEqual(len({call[1] for call in started}), 2)
                self.assertEqual(len({call[2] for call in started}), 2)
                self.assertNotIn(str(base), [call[2] for call in started])
                self.assertCountEqual([call.split("\t")[1] for call in calls if call.startswith("finished\t")], paths)
                combines = [call.split("\t")[1:] for call in calls if call.startswith("combine\t")]
                self.assertEqual(len(combines), int(not failure))
                expected_base = ["earlier", *paths] if expected == 0 else ["earlier"]
                self.assertEqual(base.read_text().splitlines(), expected_base)
                self.assertFalse(list(root.glob("healthcare-family-coverage.*")))
                self.assertTrue(all(not Path(call[2]).exists() for call in started))

    def test_import_family_cancellation_drains_dispatch_before_bookkeeping(self):
        for location, signum, expected in (("pair", "SIGTERM", 143), ("pair", "SIGINT", 130),
                                           ("scope", "SIGTERM", 143)):
            with self.subTest(location=location, signum=signum):
                temporary = tempfile.TemporaryDirectory()
                self.addCleanup(temporary.cleanup)
                root = Path(temporary.name)
                self.addCleanup(_stop_import_family_workers, root)
                _import_family_process_files(root)
                (root / "earlier-coverage").write_text("earlier\n")
                functions = _import_family_signal_hook(CHECK_FUNCTIONS, location, signum)
                with (root / "stdout").open("w") as stdout, (root / "stderr").open("w") as stderr:
                    result = subprocess.run(
                        ["bash", "-euc", _import_family_cancellation_script(functions)],
                        stdout=stdout, stderr=stderr, timeout=10, env=_import_family_environment(root),
                        check=False,
                    )
                self.assertEqual(result.returncode, expected, (root / "stderr").read_text())
                calls = (root / "calls").read_text().splitlines()
                dispatched = [call.split("\t")[2:] for call in calls if call.startswith("dispatch\t")]
                receivers = [int(line.split()[1]) for line in (root / "stdout").read_text().splitlines()]
                self.assertEqual(len(dispatched), 2)
                if location == "pair":
                    self.assertEqual(len({int(parent) for _, parent in dispatched}), 1)
                    self.assertEqual(receivers, [int(dispatched[0][1])])
                else:
                    self.assertCountEqual(receivers, [int(pid) for pid, _ in dispatched])
                created = [call.split("\t")[1] for call in calls if call.startswith("create\t")]
                dropped = [call.split("\t")[1] for call in calls if call.startswith("drop\t")]
                self.assertEqual(len(set(created)), 2)
                self.assertCountEqual(created, dropped, (root / "stderr").read_text())
                self.assertNotIn("unexpected combine", calls)
                for path in REQUIRED_IMPORT_NATIVE_TESTS[1:]:
                    name = Path(path).name
                    self.assertTrue((root / (name + ".finished")).exists())
                    pid = int((root / (name + ".started")).read_text())
                    with self.assertRaises(ProcessLookupError):
                        os.kill(pid, 0)
                self.assertEqual((root / "earlier-coverage").read_text(), "earlier\n")
                self.assertFalse(list(root.glob("healthcare-family-coverage.*")))
                self.assertFalse(list(root.glob("healthcare-installed-drain.*")))

    def test_supervised_archive_cleanup_ignores_signals_after_process_drain(self):
        script = CHECK_FUNCTIONS + r'''
create_test_database() { printf 'create\n' >> "$CALL_LOG"; }
drop_test_database() {
  printf 'drop started\n' >> "$CALL_LOG"
  "$PYTHON_BIN" -c 'import os, signal; os.kill(os.getppid(), getattr(signal, os.environ["DROP_SIGNAL"]))'
  printf 'drop finished\n' >> "$CALL_LOG"
}
psql() { :; }
python() {
  test "$1" = "$CI_ROOT/scripts/healthcare/supervise_installed.py"
  printf 'drained\n' > "$2"
  printf 'supervised\n' >> "$CALL_LOG"
  return "$TEST_STATUS"
}
run_scoped_archive_postgres hc_custom_import_test_0123456789abcdef0123456789abcdef \
  HLTHPRT_CUSTOM_IMPORT_POSTGRES_DSN postgresql://synthetic \
  tests/test_custom_import_identical_children_postgres.py
'''
        for signum in ("SIGTERM", "SIGINT"):
            for status in (0, 17):
                with self.subTest(signum=signum, status=status), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    result = subprocess.run(
                        ["bash", "-euc", script], capture_output=True, text=True, timeout=5,
                        env={**_import_family_environment(root), "DROP_SIGNAL": signum, "TEST_STATUS": str(status)},
                        check=False,
                    )
                    self.assertEqual(result.returncode, status, result.stderr)
                    self.assertEqual((root / "calls").read_text().splitlines(),
                                     ["create", "supervised", "drop started", "drop finished"])
                    self.assertFalse(list(root.glob("healthcare-installed-drain.*")))

    def test_import_family_cancellation_during_database_setup_cleans_exact_database(self):
        script = CHECK_FUNCTIONS + r'''
create_test_database() {
  printf 'create\t%s\n' "$1" >> "$CALL_LOG"
  "$PYTHON_BIN" -c 'import os, signal; os.kill(os.getppid(), signal.SIGTERM)'
}
drop_test_database() { printf 'drop\t%s\n' "$1" >> "$CALL_LOG"; }
psql() { printf 'unexpected extension setup\n' >> "$CALL_LOG"; return 99; }
python() { printf 'unexpected supervisor\n' >> "$CALL_LOG"; return 99; }
timeout() { printf 'unexpected combine\n' >> "$CALL_LOG"; return 99; }
run_import_family_postgres postgresql://synthetic
'''
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = subprocess.run(
                ["bash", "-euc", script], capture_output=True, text=True, timeout=5,
                env=_import_family_environment(root), check=False,
            )
            self.assertEqual(result.returncode, 143, result.stderr)
            calls = [call.split("\t") for call in (root / "calls").read_text().splitlines()]
            created = [call[1] for call in calls if call[0] == "create"]
            self.assertEqual(len(set(created)), 2)
            self.assertCountEqual([call[1] for call in calls if call[0] == "drop"], created)
            self.assertEqual(len(calls), 4)
            self.assertFalse(list(root.glob("healthcare-family-coverage.*")))
            self.assertFalse(list(root.glob("healthcare-installed-drain.*")))

    def test_optional_import_database_routes_cleanup_after_success_and_failure(self):
        routes = (
            ("cms_doctors_archive", "HLTHPRT_CMS_DOCTORS_ARCHIVE_TEST_DSN", "cms_archive_test_"),
            ("tiger_result_archive", "HLTHPRT_TIGER_ARCHIVE_TEST_DSN", "tiger_archive_test_"),
            ("pharmacy_economics_snapshot", "HLTHPRT_PHARMACY_ECON_POSTGRES_DSN", None),
        )
        dsn = "postgresql://postgres:postgres@localhost:5432/ptg2_v3_lifecycle_test_ci_runner"
        cases = [(lane, fail) for lane in ("core-imports", "core-services", "core-ptg", "all") for fail in (False, True)]
        for module, variable, prefix in routes:
            for lane, fail in cases:
                with self.subTest(module=module, lane=lane, fail=fail), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    source = root / "source"
                    (source / "tests").mkdir(parents=True)
                    for required in REQUIRED_IMPORT_NATIVE_TESTS:
                        (source / required).write_text("# synthetic PostgreSQL test\n", encoding="utf-8")
                    test_path = f"tests/test_{module}_postgres.py"
                    (source / test_path).write_text("# synthetic PostgreSQL test\n", encoding="utf-8")
                    call_log = root / "calls"
                    env = {
                        **os.environ, "SOURCE_ROOT": str(source), "CI_ROOT": str(ROOT),
                        "CI_DEPS_READY": "1", "COVERAGE_BASE_SHA": "a" * 40,
                        "HLTHPRT_DB_USER": "postgres",
                        "HLTHPRT_DB_PASSWORD": "postgres", "CALL_LOG": str(call_log),
                        "ROUTE_TEST": test_path, "ROUTE_VARIABLE": variable,
                        "ROUTE_FAILURE": "1" if fail else "0", "ROUTE_LANE": lane,
                    }
                    script = CHECK_FUNCTIONS + r'''
mapfile() { capacity_tests=(tests/test_capacity_placeholder.py); }
prepare_debug_rust_binaries() { :; }
create_test_database() { printf 'create\t%s\n' "$1" >> "$CALL_LOG"; }
drop_test_database() { printf 'drop\t%s\n' "$1" >> "$CALL_LOG"; }
run_scoped_archive_postgres() { :; }
run_scoped_archive_postgres_body() { run_scoped_archive_postgres "$@"; }
python() {
  printf '%s\t%s\n' "${!ROUTE_VARIABLE:-}" "$*" >> "$CALL_LOG"
  if [[ "$*" = "-m pytest -q $ROUTE_TEST" && "$ROUTE_FAILURE" = 1 ]]; then
    return 23
  fi
}
timeout() { shift 2; "$@"; }
run_python_main 0
run_core_postgres "postgresql://postgres:postgres@localhost:5432/ptg2_v3_lifecycle_test_ci_runner" "$ROUTE_LANE"
'''
                    result = subprocess.run(["bash", "-euc", script], cwd=source, env=env, capture_output=True, text=True)
                    selected = lane in ("core-imports", "all")
                    self.assertEqual(result.returncode == 0, not (fail and selected), result.stderr)
                    calls = call_log.read_text().splitlines()
                    main_call = next(call for call in calls if "--ci-shard-count 4" in call)
                    self.assertIn(f"--ignore {test_path}", main_call)
                    executions = [call for call in calls if call.endswith(f"\t-m pytest -q {test_path}")]
                    if not selected:
                        self.assertEqual(executions, [])
                        if prefix is not None:
                            self.assertFalse(any(prefix in call for call in calls))
                        continue
                    self.assertEqual(len(executions), 1)
                    actual_dsn = executions[0].split("\t", 1)[0]
                    if prefix is None:
                        self.assertEqual(actual_dsn, dsn)
                    else:
                        self.assertRegex(actual_dsn, rf"^{dsn.rsplit('/', 1)[0]}/{prefix}[0-9a-f]{{32}}$")
                        database = actual_dsn.rsplit("/", 1)[1]
                        self.assertEqual(calls.count(f"create\t{database}"), 1)
                        self.assertEqual(calls.count(f"drop\t{database}"), 1)
                        self.assertLess(calls.index(f"create\t{database}"), calls.index(executions[0]))
                        self.assertGreater(calls.index(f"drop\t{database}"), calls.index(executions[0]))

    def test_result_archive_postgres_tests_use_isolated_databases(self):
        test_paths = (
            "tests/test_entity_address_result_generation_postgres.py",
            "tests/test_mrf_result_archive_postgres.py",
            "tests/test_reference_family_archive_postgres.py",
            "tests/test_reference_family_result_generation_postgres.py",
            "tests/test_nucc_reference_result_generation_postgres.py",
            "tests/test_geo_census_reference_family_postgres.py",
            "tests/test_geo_reference_family_postgres.py",
            "tests/test_mrf_address_publication_postgres.py",
            "tests/test_mrf_publication_receipt_postgres.py",
            "tests/test_provider_quality_reference_family_postgres.py",
            "tests/test_clinical_reference_family_postgres.py",
            "tests/test_clinical_reference_generation_migration_postgres.py",
            "tests/test_clinical_reference_bootstrap_postgres.py",
            "tests/test_places_zcta_handoff_postgres.py",
            "tests/test_places_zcta_cancel_postgres.py",
        )
        dsn = "postgresql://postgres:postgres@localhost:5432/ptg2_v3_lifecycle_test_ci_runner"
        routes = (
            (test_paths[2:], 0, "hc_reference_family_"),
            ((test_paths[0],), 1, "hc_address_generation_"),
            ((test_paths[1],), 2, "hc_mrf_archive_"),
        )
        cases = [(lane, failed_path) for lane in ("core-services", "core-imports", "all")
                 for failed_path in ("", test_paths[2], test_paths[0], test_paths[1], *test_paths[-5:])]
        for lane, failed_path in cases:
            with self.subTest(lane=lane, failed_path=failed_path), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / "source"
                (source / "tests").mkdir(parents=True)
                for required in REQUIRED_IMPORT_NATIVE_TESTS:
                    (source / required).write_text("# synthetic PostgreSQL test\n", encoding="utf-8")
                for test_path in test_paths:
                    (source / test_path).write_text("# synthetic PostgreSQL test\n", encoding="utf-8")
                call_log = root / "calls"
                env = {
                    **os.environ,
                    "SOURCE_ROOT": str(source),
                    "CI_ROOT": str(ROOT),
                    "CI_DEPS_READY": "1",
                    "COVERAGE_BASE_SHA": "a" * 40,
                    "HLTHPRT_DB_USER": "postgres",
                    "HLTHPRT_DB_PASSWORD": "postgres",
                    "CALL_LOG": str(call_log),
                    "ROUTE_LANE": lane,
                    "ROUTE_FAILURE": failed_path,
                }
                script = CHECK_FUNCTIONS + r'''
mapfile() { capacity_tests=(tests/test_capacity_placeholder.py); }
prepare_debug_rust_binaries() { :; }
create_test_database() { printf 'create\t%s\n' "$1" >> "$CALL_LOG"; }
drop_test_database() { printf 'drop\t%s\n' "$1" >> "$CALL_LOG"; }
run_scoped_archive_postgres() { :; }
run_scoped_archive_postgres_body() { run_scoped_archive_postgres "$@"; }
python() {
  printf '%s\t%s\t%s\t%s\n' \
    "${HLTHPRT_REFERENCE_FAMILY_ARCHIVE_TEST_DSN:-}" \
    "${HLTHPRT_ENTITY_ADDRESS_GENERATION_TEST_DSN:-}" \
    "${HLTHPRT_MRF_RESULT_ARCHIVE_TEST_DSN:-}" \
    "$*" >> "$CALL_LOG"
  if [[ -n "$ROUTE_FAILURE" && "$*" != *"--ignore"* && " $* " = *" $ROUTE_FAILURE "* ]]; then
    return 23
  fi
}
timeout() {
  shift 2
  "$@"
}
run_python_main 0
run_core_postgres "postgresql://postgres:postgres@localhost:5432/ptg2_v3_lifecycle_test_ci_runner" "$ROUTE_LANE"
'''
                result = subprocess.run(["bash", "-euc", script], cwd=source, env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, int(bool(failed_path) and lane != "core-imports"), result.stderr)
                calls = call_log.read_text().splitlines()
                main_call = next(call for call in calls if "--ci-shard-count 4" in call)
                for test_path in test_paths:
                    self.assertIn(f"--ignore {test_path}", main_call)

                stopped = False
                for paths, column, prefix in routes:
                    executions = [call for call in calls if "--ignore" not in call
                                  and any(test_path in call for test_path in paths)]
                    selected = lane != "core-imports" and not stopped
                    self.assertEqual(len(executions), int(selected))
                    if not selected:
                        self.assertFalse(any(prefix in call for call in calls))
                        continue
                    execution = executions[0]
                    fields = execution.split("\t")
                    self.assertEqual(shlex.split(fields[-1]), ["-m", "pytest", "-q", *paths])
                    actual_dsn = fields[column]
                    self.assertRegex(actual_dsn, rf"^{dsn.rsplit('/', 1)[0]}/{prefix}[0-9a-f]{{32}}$")
                    database = actual_dsn.rsplit("/", 1)[1]
                    self.assertEqual(calls.count(f"create\t{database}"), 1)
                    self.assertEqual(calls.count(f"drop\t{database}"), 1)
                    self.assertLess(calls.index(f"create\t{database}"), calls.index(execution))
                    self.assertGreater(calls.index(f"drop\t{database}"), calls.index(execution))
                    stopped = failed_path in paths


if __name__ == "__main__":
    unittest.main()
