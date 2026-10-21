"""Geo collection uses its actual fixture DSN and retains strict native receipts."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECK_FUNCTIONS = (
    (ROOT / "scripts/healthcare/check").read_text().rsplit("\ncase ", 1)[0]
)
GEO_TEST = "tests/test_geo_reference_family_postgres.py"
GEO_CASE = "test_geo_replacement_keeps_indexes_and_fences_generation_and_failure"


def _pytest_command():
    """Emit only synthetic command evidence and a caller-selected JUnit outcome."""
    return (
        f"#!{sys.executable}\n"
        + r"""
import json, os, pathlib, sys
import xml.etree.ElementTree as ET
arguments = sys.argv[1:]
with open(os.environ["CALL_LOG"], "a") as output:
    output.write(json.dumps(dict(arguments=arguments, database=os.environ["HLTHPRT_DB_DATABASE"],
        dsn=os.environ.get("HLTHPRT_CMS_NPD_ADMISSION_TEST_DSN"),
        reference=os.environ.get("HLTHPRT_REFERENCE_FAMILY_ARCHIVE_TEST_DSN"),
        coverage=os.environ.get("COVERAGE_FILE"), addopts=os.environ.get("PYTEST_ADDOPTS"),
        alias=os.environ.get("DB_SCHEMA"), override=os.environ.get("HLTHPRT_DB_DATABASE_OVERRIDE"))) + "\n")
stage = os.environ["FAIL_STAGE"]
count = 0 if stage == "zero" else 2 if stage == "duplicate" else 1
root = ET.Element("testsuites")
suite = ET.SubElement(root, "testsuite", tests=str(count + (stage == "count")),
                      skipped="0", errors="0", failures="0")
for _ in range(count):
    case = ET.SubElement(suite, "testcase",
        classname="foreign" if stage == "module" else "tests.test_geo_reference_family_postgres",
        name="foreign" if stage == "name" else os.environ["GEO_CASE"])
    if stage in ("skipped", "error", "failure"):
        ET.SubElement(case, stage)
ET.ElementTree(root).write(arguments[arguments.index("--junitxml") + 1])
sys.exit(17 if stage == "pytest" else 0)
"""
    )


class GeoNativeCollectionTests(unittest.TestCase):
    def test_geo_moves_once_and_preserves_legacy_and_other_reference_tests(self):
        body = (
            "  local geo_reference_test\n"
            + CHECK_FUNCTIONS.split("  local geo_reference_test\n", 1)[1].split(
                '  if [ -f "$repository_root/tests/test_entity_address_result_generation_postgres.py" ]; then',
                1,
            )[0]
        )
        import re

        reference_paths = set(re.findall(r"tests/[a-z0-9_]+\.py", body))
        for current in (False, True):
            with (
                self.subTest(current=current),
                tempfile.TemporaryDirectory() as temporary,
            ):
                source = Path(temporary)
                (source / "tests").mkdir()
                for path in reference_paths:
                    (source / path).touch()
                if current:
                    (source / "tests/geo_source_postgres_support.py").touch()
                log = source / "calls"
                script = (
                    CHECK_FUNCTIONS
                    + r"""
require_native_source_files() { :; }
create_test_database() { :; }
drop_test_database() { :; }
timeout() { printf 'reference\t%s\n' "$*" >> "$CALL_LOG"; }
run_geo_reference_postgres() { printf 'geo\t%s\n' "$1" >> "$CALL_LOG"; }
run_reference_lane() {
  local dsn=postgresql://synthetic/test
"""
                    + body
                    + "\n}\nrun_reference_lane\n"
                )
                outcome = subprocess.run(
                    ["bash", "-eu"],
                    input=script,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                    env={
                        **os.environ,
                        "SOURCE_ROOT": str(source),
                        "CI_ROOT": str(ROOT),
                        "CALL_LOG": str(log),
                    },
                )
                self.assertEqual(outcome.returncode, 0, outcome.stderr)
                calls = log.read_text().splitlines()
                references = calls[0].split()
                self.assertEqual(
                    set(references[7:]),
                    reference_paths - ({GEO_TEST} if current else set()),
                )
                self.assertEqual(calls[1:], ["geo\t" + GEO_TEST] if current else [])
                self.assertEqual(sum(call.split().count(GEO_TEST) for call in calls), 1)

    def test_geo_owned_database_and_exact_receipt_fail_closed_and_clean_up(self):
        for stage in (
            "",
            "create",
            "pytest",
            "drop",
            "skipped",
            "error",
            "failure",
            "module",
            "name",
            "duplicate",
            "zero",
            "count",
        ):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source, runner = root / "source", root / "runner"
                (source / "tests").mkdir(parents=True)
                (source / GEO_TEST).touch()
                runner.mkdir()
                log, lifecycle = root / "calls", root / "lifecycle"
                log.touch()
                lifecycle.touch()
                command = root / "timeout"
                command.write_text(_pytest_command())
                command.chmod(0o755)
                environment = {
                    **os.environ,
                    "SOURCE_ROOT": str(source),
                    "CI_ROOT": str(ROOT),
                    "RUNNER_TEMP": str(runner),
                    "PATH": str(root) + os.pathsep + os.environ["PATH"],
                    "CALL_LOG": str(log),
                    "LIFECYCLE": str(lifecycle),
                    "FAIL_STAGE": stage,
                    "GEO_CASE": GEO_CASE,
                    "HLTHPRT_DB_USER": "postgres",
                    "HLTHPRT_DB_PASSWORD": "synthetic",
                    "HLTHPRT_DB_HOST": "127.0.0.1",
                    "HLTHPRT_DB_PORT": "5432",
                    "DB_SCHEMA": "sentinel",
                    "HLTHPRT_DB_DATABASE_OVERRIDE": "sentinel",
                    "COVERAGE_FILE": str(root / ".coverage"),
                    "PYTEST_ADDOPTS": "--cov=process --cov-branch",
                }
                for variable in (
                    "PGPASSWORD",
                    "HLTHPRT_CMS_NPD_ADMISSION_TEST_DSN",
                    "HLTHPRT_REFERENCE_FAMILY_ARCHIVE_TEST_DSN",
                ):
                    environment.pop(variable, None)
                script = (
                    CHECK_FUNCTIONS
                    + r"""
python() { command python3 "$@"; }
create_test_database() { printf 'create:%s\n' "$1" >> "$LIFECYCLE"; [ "$FAIL_STAGE" != create ] || return 17; }
drop_test_database() { printf 'drop:%s\n' "$1" >> "$LIFECYCLE"; [ "$FAIL_STAGE" != drop ] || return 7; }
run_geo_reference_postgres tests/test_geo_reference_family_postgres.py
test "$DB_SCHEMA:$HLTHPRT_DB_DATABASE_OVERRIDE" = sentinel:sentinel
test -z "${HLTHPRT_CMS_NPD_ADMISSION_TEST_DSN:-}"
"""
                )
                outcome = subprocess.run(
                    ["bash", "-eu"],
                    input=script,
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
                self.assertEqual(
                    outcome.returncode,
                    17 if stage in ("create", "pytest") else 1 if stage else 0,
                    outcome.stderr,
                )
                calls = [json.loads(line) for line in log.read_text().splitlines()]
                self.assertEqual(len(calls), int(stage != "create"))
                if calls:
                    call = calls[0]
                    self.assertRegex(
                        call["database"], r"^hc_cms_admission_test_[0-9a-f]{32}$"
                    )
                    self.assertEqual(
                        call["dsn"],
                        "postgresql+asyncpg://postgres:synthetic@127.0.0.1:5432/"
                        + call["database"],
                    )
                    self.assertIsNone(call["reference"])
                    self.assertIsNone(call["alias"])
                    self.assertIsNone(call["override"])
                    self.assertEqual(call["coverage"], environment["COVERAGE_FILE"])
                    self.assertEqual(call["addopts"], environment["PYTEST_ADDOPTS"])
                    self.assertEqual(
                        call["arguments"][:7],
                        [
                            "--foreground",
                            "295s",
                            "python",
                            "-m",
                            "pytest",
                            "-q",
                            GEO_TEST + "::" + GEO_CASE,
                        ],
                    )
                events = lifecycle.read_text().splitlines()
                self.assertEqual(len(events), 2)
                self.assertEqual(
                    events[0].removeprefix("create:"), events[1].removeprefix("drop:")
                )
                self.assertFalse(list(runner.iterdir()))
