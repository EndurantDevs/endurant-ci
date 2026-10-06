"""Exercise hosted artifact placement and exact-image cleanup without services."""

import hashlib
import json
import os
import re
import shlex
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


class HealthcarePublicChecks(unittest.TestCase):
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
                result = subprocess.run(
                    ["bash", "-euc", script], cwd=source,
                    env={**os.environ, "SOURCE_ROOT": str(source), "CI_ROOT": str(ROOT),
                         "HLTHPRT_DB_USER": "postgres", "HLTHPRT_DB_PASSWORD": "postgres",
                         "ROUTE_LOG": str(log)},
                    capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                calls = log.read_text().splitlines()
                self.assertEqual(len(calls), 2 if len(present) == 3 else 1)
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
  test "$3" = tests/test_custom_import_installed_operator_postgres.py
  test "${*:4}" = '-n 2 --dist worksteal --durations=9 -vv'
  printf 'supervised\n' >> "$CALL_LOG"
  if [ "$DRAINED" = 1 ]; then printf 'drained\n' > "$2"; fi
  return "$TEST_STATUS"
}
run_scoped_archive_postgres hc_custom_import_test_0123456789abcdef0123456789abcdef \
  HLTHPRT_CUSTOM_IMPORT_POSTGRES_DSN postgresql://postgres@127.0.0.1:5440 \
  tests/test_custom_import_installed_operator_postgres.py -n 2 --dist worksteal --durations=9 -vv
'''
            for status, drained in ((0, 1), (17, 1), (124, 1), (0, 0), (17, 0), (124, 0)):
                with self.subTest(status=status, drained=drained):
                    result = subprocess.run(
                        ["bash", "-euc", script], capture_output=True, text=True,
                        env={**os.environ, "SOURCE_ROOT": temporary, "CI_ROOT": str(ROOT),
                             "RUNNER_TEMP": temporary, "CALL_LOG": str(log),
                             "HLTHPRT_DB_USER": "postgres", "HLTHPRT_DB_PASSWORD": "postgres",
                             "TEST_STATUS": str(status), "DRAINED": str(drained)},
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
            "test_result_archive_candidate_initialization_postgres.py",
            "test_result_archive_candidate_validation_postgres.py",
        )
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source"
            tests = source / "tests"
            tests.mkdir(parents=True)
            for name in names:
                (tests / name).touch()
            log = Path(temporary) / "routes"
            script = CHECK_FUNCTIONS + r'''
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
            for name in names[:3]:
                self.assertIn(name, calls[0][3])
            for name in names[3:]:
                self.assertIn(name, calls[1][3])

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
pylint() { printf 'pylint %s\n' "$*"; }
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
run_python_inference() { printf 'runtime-aware Pylint inference\n'; }
python() { printf 'python %s\n' "$*"; }
timeout() { printf 'timeout %s\n' "$*"; }
run_api_contract
'''
            result = subprocess.run(["bash", "-euc", script], env=environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        for retained in ("runtime dependencies", "runtime-aware Pylint inference", "provider_directory_runtime_contract.py",
                         "generate_provider_directory_support_docs.py --check", "tests/test_openapi_spec.py",
                         "tests/test_formulary_fhir_openapi.py", "tests/test_api_init_and_utils.py", "tests/test_healthcheck.py"):
            self.assertIn(retained, result.stdout)
        workflow = yaml.safe_load((ROOT / ".github/workflows/healthcare.yml").read_text())
        self.assertIn("measurement", workflow["jobs"]["source-validation"]["needs"])
        self.assertIn("readability-preflight", workflow["jobs"]["measurement"]["needs"])
        self.assertIn("api-contract", workflow["jobs"]["measurement"]["needs"])
        main = CHECK_FUNCTIONS.split("run_python_main() {", 1)[1].split("\n}\n", 1)[0]
        self.assertNotIn("test_coverage_forecast.py", main)
        self.assertIn("--ci-shard-count 4", main)

    def test_inference_environment_creation_failure_keeps_runtime_and_cleans_its_child(self):
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
        self.assertIn("sys.path.extend", CHECK_FUNCTIONS)
        self.assertIn("orjson.quality_probe_missing_member", CHECK_FUNCTIONS)
        self.assertIn("client.quality_probe_missing_member", CHECK_FUNCTIONS)
        probe_segment = CHECK_FUNCTIONS.split("probe_output=", 1)[1].split('[[ "$probe_status"', 1)[0]
        self.assertIn("--enable=no-member", probe_segment)
        self.assertNotIn("--enable=no-member", CHECK_FUNCTIONS.split('[[ "$probe_status"', 1)[1])
        self.assertIn("Pylint runtime dependency inference canary failed (status %s)", CHECK_FUNCTIONS)
        self.assertIn(
            '"$inference_root/venv/bin/pylint" --errors-only --init-hook "$runtime_hook" "${inference_targets[@]}"',
            CHECK_FUNCTIONS,
        )

    def test_inference_targets_fail_closed_before_running_pylint(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            environment = {**os.environ, "SOURCE_ROOT": str(source), "CI_ROOT": str(ROOT), "RUNNER_TEMP": str(root)}
            script = CHECK_FUNCTIONS + r'''
uv() { :; }
run_python_inference
'''
            result = subprocess.run(
                ["bash", "-euc", script],
                check=False,
                cwd=source,
                env=environment,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("Pylint inference target is missing: api/billing_search_selector_contract.py", result.stderr)
            self.assertEqual(set(root.iterdir()), {source})
        self.assertNotIn("process/fhir_request_failure_policy.py", CHECK_FUNCTIONS)

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
        self.assertIn("actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97", setup)
        self.assertIn("--only-binary=:all: --require-hashes -r /dev/stdin", setup)
        self.assertIn(
            "uv==0.12.17 --hash=sha256:9e25bb39e1674799c408345a6397ebc2"
            "c7c719d498be0ce9d935466d36ceacf5",
            setup,
        )
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
            version = metadata.version("pip")
            (source / "requirements.txt").write_text(
                f"pip=={version}\n", encoding="utf-8"
            )
            (source / "requirements-dev.txt").write_text(
                "-r requirements.txt\n", encoding="utf-8"
            )
            (helper / "requirements-ci.in").write_text(
                "-r requirements-dev.txt\n", encoding="utf-8"
            )
            (source / "requirements-ci.lock").write_text(
                f"pip=={version}\n", encoding="utf-8"
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
            fake_metadata = source / "pip-999.0.dist-info"
            fake_metadata.mkdir()
            (fake_metadata / "METADATA").write_text(
                "Metadata-Version: 2.1\nName: pip\nVersion: 999.0\n",
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
            self.assertEqual(len(uploads), 27)  # Thirteen Python pairs plus the Rust directory.
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
            self.assertEqual(len(freezes), 14)
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
        phases = ("rust-lint", "rust-coverage", "rust-audit", "rust-release-build",
                  "rust-native-tests", "rust-wheel-build", "rust-wheel-install", "rust-wheel-tests")
        for failure in (*phases, "none"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source, runner = root / "source", root / "runner"
                binary = source / "support/ptg2_scanner/target/release/ptg2_scanner"
                binary.parent.mkdir(parents=True)
                binary.write_text('#!/bin/sh\nif [ "$1" = --canon-version ]; then\n'
                                  '  echo \'{"ruleset_version":4}\'\nelse\n  cp "$2" "$3"\nfi\n')
                binary.chmod(0o755)
                runner.mkdir()
                environment = {**os.environ, "SOURCE_ROOT": str(source), "CI_ROOT": str(ROOT),
                               "RUNNER_TEMP": str(runner), "FAIL_PHASE": failure,
                               "COVERAGE_BASE_SHA": "a" * 40}
                script = CHECK_FUNCTIONS + r'''
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
                self.assertEqual(result.returncode, 0 if failure == "none" else 17, result.stderr)
                ends = re.findall(r"CI_PHASE end name=([a-z-]+) elapsed_seconds=\d+ exit_code=(\d+)", result.stdout)
                expected = [(name, "0") for name in phases]
                if failure != "none":
                    expected = expected[:phases.index(failure)] + [(failure, "17")]
                self.assertEqual(ends, expected, result.stdout)
                self.assertEqual(list(runner.iterdir()), [])

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
        test_path = "tests/test_npi_result_archive_postgres.py"
        dsn = "postgresql://postgres:postgres@localhost:5432/ptg2_v3_lifecycle_test_ci_runner"
        for present in (False, True):
            with self.subTest(present=present), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / "source"
                (source / "tests").mkdir(parents=True)
                for required in REQUIRED_IMPORT_NATIVE_TESTS:
                    (source / required).write_text("# synthetic PostgreSQL test\n", encoding="utf-8")
                if present:
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
                    "CALL_LOG": str(call_log),
                }
                script = CHECK_FUNCTIONS + r'''
mapfile() { capacity_tests=(tests/test_capacity_placeholder.py); }
prepare_debug_rust_binaries() { :; }
create_test_database() { :; }
drop_test_database() { :; }
run_scoped_archive_postgres() { :; }
python() {
  printf '%s\t%s\n' "${HLTHPRT_NPI_RESULT_ARCHIVE_TEST_DSN:-}" "$*" >> "$CALL_LOG"
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
                matching = [call for call in calls if test_path in call]
                if not present:
                    self.assertEqual(matching, [])
                    continue
                ignored = [call for call in matching if f"--ignore {test_path}" in call]
                executed = [call for call in matching if f"--ignore {test_path}" not in call]
                self.assertEqual(len(ignored), 1)
                self.assertTrue(ignored[0].startswith("\t"))
                self.assertEqual(executed, [f"{dsn}\t-m pytest -q {test_path}"])

    def test_custom_import_postgres_tests_use_bounded_scoped_databases(self):
        """Split lifecycle, capture and build suites without changing membership or coverage."""

        required_test_paths = REQUIRED_IMPORT_NATIVE_TESTS
        installed_test_path = "tests/test_custom_import_installed_operator_postgres.py"
        mixed_test_paths = (
            "tests/test_custom_import_provider_list.py",
            "tests/test_custom_import_provider_geo_sql.py",
        )
        lifecycle_test_paths = (
            "tests/test_custom_import_execution_postgres.py",
            "tests/test_custom_import_publication_postgres.py",
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
            "tests/test_custom_import_operator_postgres.py",
            "tests/test_custom_import_registration_authority_postgres.py",
            "tests/test_custom_import_registration_authority_route_postgres.py",
            "tests/test_custom_import_registration_authority_migration_postgres.py",
            "tests/test_custom_import_provider_query_postgres.py",
            "tests/test_custom_import_provider_hydration_postgres.py",
        ) + mixed_test_paths
        family_test_paths = required_test_paths[1:]
        test_groups = (lifecycle_test_paths, capture_test_paths, build_test_paths, (installed_test_path,), family_test_paths)
        test_paths = lifecycle_test_paths + capture_test_paths + build_test_paths + (installed_test_path,) + family_test_paths
        optional_capture_paths = lifecycle_test_paths + capture_test_paths[:-1]
        optional_build_paths = build_test_paths
        optional_test_paths = optional_capture_paths + optional_build_paths + (installed_test_path,)
        legacy_optional_paths = tuple(path for path in optional_test_paths if path != installed_test_path)
        dsn = "postgresql://postgres:postgres@localhost:5432/ptg2_v3_lifecycle_test_ci_runner"
        archive_url = "postgresql://postgres:postgres@127.0.0.1:5440"
        present_path_sets = ((), *((path,) for path in optional_test_paths),
                             optional_capture_paths, optional_build_paths, legacy_optional_paths, optional_test_paths)
        cases = [("core-imports", (*required_test_paths, *paths), "", "") for paths in present_path_sets]
        cases += [("core-imports", test_paths, group[0], "") for group in test_groups]
        cases += [("core-imports", test_paths, installed_test_path, "")]
        cases += [("core-imports", tuple(path for path in required_test_paths if path != missing), "", missing)
                  for missing in required_test_paths]
        cases += [(lane, test_paths, "", "") for lane in ("core-services", "core-ptg", "all")]
        for lane, present_paths, failure_path, missing_path in cases:
            with self.subTest(lane=lane, present_paths=present_paths, failure=failure_path), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / "source"
                (source / "tests").mkdir(parents=True)
                for test_path in present_paths:
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
                    if test_path in present_paths and test_path not in mixed_test_paths:
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
                    for group in test_groups if lane in ("core-imports", "all") and any(path in present_paths for path in group)
                ]
                if failure_path:
                    failed_group = next(i for i, group in enumerate(expected_groups) if failure_path in group)
                    expected_groups = expected_groups[:failed_group + 1]
                self.assertEqual(executions, [])
                self.assertEqual(len(routes), len(expected_groups))
                databases = [route.split()[0] for route in routes]
                self.assertEqual(len(databases), len(set(databases)))
                for route, expected_paths in zip(routes, expected_groups):
                    self.assertRegex(
                        route,
                        rf"^hc_custom_import_test_[0-9a-f]{{32}} "
                        rf"HLTHPRT_CUSTOM_IMPORT_POSTGRES_DSN {re.escape(archive_url)}(?: |$)",
                    )
                    expected_arguments = list(expected_paths)
                    if expected_paths == [installed_test_path]:
                        expected_arguments += ["-n", "2", "--dist", "worksteal", "--durations=9", "-vv"]
                    self.assertEqual(route.split()[3:], expected_arguments)

    def test_optional_import_database_routes_cleanup_after_success_and_failure(self):
        routes = (
            ("cms_doctors_archive", "HLTHPRT_CMS_DOCTORS_ARCHIVE_TEST_DSN", "cms_archive_test_"),
            ("tiger_result_archive", "HLTHPRT_TIGER_ARCHIVE_TEST_DSN", "tiger_archive_test_"),
            ("pharmacy_economics_snapshot", "HLTHPRT_PHARMACY_ECON_POSTGRES_DSN", None),
        )
        dsn = "postgresql://postgres:postgres@localhost:5432/ptg2_v3_lifecycle_test_ci_runner"
        cases = [(lane, fail) for lane in ("core-imports", "core-services", "all") for fail in (False, True)]
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
                    self.assertEqual(result.returncode == 0, not (fail and lane != "core-services"), result.stderr)
                    calls = call_log.read_text().splitlines()
                    main_call = next(call for call in calls if "--ci-shard-count 4" in call)
                    self.assertIn(f"--ignore {test_path}", main_call)
                    executions = [call for call in calls if call.endswith(f"\t-m pytest -q {test_path}")]
                    if lane == "core-services":
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
