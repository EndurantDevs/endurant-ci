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
GEO_PROFILES = ("current", "legacy-pricing", "legacy-native")


def _geo_source(mode):
    """Provide historical, current, and deliberately unsupported source contracts."""
    profiles = {
        "missing-profile": GEO_PROFILES[:-1],
        "extra-profile": (*GEO_PROFILES, "unknown"),
        "duplicate-profile": ("current", "current", "legacy-native"),
        "wrong-profile": ("current", "unknown", "legacy-native"),
        "reordered-profile": tuple(reversed(GEO_PROFILES)),
    }.get(mode, GEO_PROFILES)
    marker = f'@pytest.mark.parametrize("index_profile", {profiles!r})\n'
    argument = ", index_profile"
    if mode == "historical":
        marker, argument = "", ""
    elif mode == "missing-marker":
        marker = ""
    elif mode == "missing-argument":
        argument = ""
    elif mode == "wrong-parameter":
        marker = marker.replace("index_profile", "unknown")
    elif mode == "dynamic-profile":
        marker = '@pytest.mark.parametrize("index_profile", tuple(PROFILES))\n'
    elif mode == "duplicate-marker":
        marker += marker
    elif mode == "explicit-ids":
        marker = marker.rstrip()[:-1] + ', ids=("first", "second", "third"))\n'
    case = "test_other" if mode == "missing-case" else GEO_CASE
    source = f"import pytest\n\n@pytest.mark.asyncio\n{marker}async def {case}(tmp_path, monkeypatch{argument}):\n    return None\n"
    if mode == "extra-case":
        source += "\nasync def test_other():\n    return None\n"
    elif mode == "syntax":
        source += "invalid Python !\n"
    return source


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
selected = [argument for argument in arguments if argument.startswith("tests/") and "::" in argument]
names = [node.split("::", 1)[1] for node in selected]
if stage == "zero":
    names = []
elif stage == "duplicate":
    names += names[:1]
elif stage == "missing":
    names = names[:-1]
elif stage == "extra":
    names.append(os.environ["GEO_CASE"] + "[unknown]")
elif stage == "wrong-parameter":
    names[0] = os.environ["GEO_CASE"] + "[unknown]"
elif stage == "report-mismatch":
    names = ([os.environ["GEO_CASE"]] if len(names) == 3 else
             [os.environ["GEO_CASE"] + "[" + profile + "]" for profile in ("current", "legacy-pricing", "legacy-native")])
count = len(names)
root = ET.Element("testsuites")
suite = ET.SubElement(root, "testsuite", tests=str(count + (stage == "count")),
                      skipped="0", errors="0", failures="0")
for name in names:
    case = ET.SubElement(suite, "testcase",
        classname="foreign" if stage == "module" else "tests.test_geo_reference_family_postgres",
        name="foreign" if stage == "name" else name)
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
                references = [call.split() for call in calls if call.startswith("reference\t")]
                self.assertEqual(len(references), 2)
                publication = "tests/test_mrf_publication_receipt_postgres.py"
                self.assertEqual(references[1][7:], [publication])
                self.assertEqual(
                    set(references[0][7:]),
                    reference_paths - {publication} - ({GEO_TEST} if current else set()),
                )
                selected = [path for reference in references for path in reference[7:]]
                self.assertEqual(len(selected), len(set(selected)))
                for reference in references:
                    self.assertEqual(
                        reference[:7],
                        ["reference", "--foreground", "295s", "python", "-m", "pytest", "-q"],
                    )
                self.assertEqual(calls[2:], ["geo\t" + GEO_TEST] if current else [])
                self.assertEqual(sum(call.split().count(GEO_TEST) for call in calls), 1)

    def test_geo_owned_database_and_exact_receipt_fail_closed_and_clean_up(self):
        stages = (
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
            "missing",
            "extra",
            "wrong-parameter",
            "report-mismatch",
        )
        cases = [
            (mode, stage) for mode in ("historical", "current") for stage in stages
        ]
        cases += [
            (mode, "source")
            for mode in (
                "missing-profile",
                "extra-profile",
                "duplicate-profile",
                "wrong-profile",
                "wrong-parameter",
                "dynamic-profile",
                "duplicate-marker",
                "missing-marker",
                "missing-argument",
                "missing-case",
                "extra-case",
                "syntax",
                "reordered-profile",
                "explicit-ids",
            )
        ]
        for mode, stage in cases:
            with (
                self.subTest(mode=mode, stage=stage),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                source, runner = root / "source", root / "runner"
                (source / "tests").mkdir(parents=True)
                (source / GEO_TEST).write_text(_geo_source(mode))
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
                self.assertEqual(len(calls), int(stage not in ("create", "source")))
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
                        call["arguments"][:6],
                        [
                            "--foreground",
                            "295s",
                            "python",
                            "-m",
                            "pytest",
                            "-q",
                        ],
                    )
                    expected_nodes = [GEO_TEST + "::" + GEO_CASE]
                    if mode == "current":
                        expected_nodes = [
                            node + "[" + profile + "]"
                            for node in expected_nodes
                            for profile in GEO_PROFILES
                        ]
                    self.assertEqual(
                        call["arguments"][6 : call["arguments"].index("--junitxml")],
                        expected_nodes,
                    )
                events = lifecycle.read_text().splitlines()
                self.assertEqual(len(events), 0 if stage == "source" else 2)
                if events:
                    self.assertEqual(
                        events[0].removeprefix("create:"),
                        events[1].removeprefix("drop:"),
                    )
                self.assertFalse(list(runner.iterdir()))


PLAN_RELEASE_TESTS = tuple(
    "tests/test_plan_release_" + name + "_postgres.py"
    for name in (
        "storage",
        "preparation_pins",
        "preparation_pin_safety",
        "preparation_pin_retirement",
        "read_admission",
    )
)
PLAN_RELEASE_SUPPORT = (
    "tests/plan_release_storage_postgres_support.py",
    "tests/plan_release_storage_cohort_support.py",
    "tests/plan_release_preparation_pin_postgres_support.py",
)


def _plan_release_collector():
    return r"""
import json, os, pathlib, sys
arguments = sys.argv[1:]
with open(os.environ["CALL_LOG"], "a") as output:
    output.write(json.dumps(dict(collection=arguments)) + "\n")
if os.environ["FAIL_STAGE"] == "collection":
    raise SystemExit(17)
paths = arguments[arguments.index("--") + 1:]
nodes = [path + "::test_native[" + value + "]" for path in paths for value in ("first", "second")]
stage = os.environ["FAIL_STAGE"]
if stage == "empty-selection":
    nodes = []
elif stage == "duplicate-selection":
    nodes += nodes[:1]
elif stage == "missing-module":
    nodes = nodes[:-2]
elif stage == "foreign-module":
    nodes.append("tests/test_unrelated.py::test_native")
pathlib.Path(arguments[arguments.index("--output") + 1]).write_text("\n".join(nodes) + "\n")
"""


def _plan_release_command():
    return (
        f"#!{sys.executable}\n"
        + r"""
import json, os, pathlib, sys
import xml.etree.ElementTree as ET
arguments = sys.argv[1:]
selection = pathlib.Path(next(item[1:] for item in arguments if item.startswith("@")))
nodes = selection.read_text().splitlines()
with open(os.environ["CALL_LOG"], "a") as output:
    output.write(json.dumps(dict(arguments=arguments, selected=nodes,
        database=os.environ["HLTHPRT_DB_DATABASE"], host=os.environ["HLTHPRT_DB_HOST"],
        port=os.environ["HLTHPRT_DB_PORT"], user=os.environ["HLTHPRT_DB_USER"],
        password=os.environ["HLTHPRT_DB_PASSWORD"], alias=os.environ.get("DB_SCHEMA"),
        override=os.environ.get("HLTHPRT_DB_DATABASE_OVERRIDE"),
        coverage=os.environ.get("COVERAGE_FILE"), addopts=os.environ.get("PYTEST_ADDOPTS"))) + "\n")
stage = os.environ["FAIL_STAGE"]
if stage == "pytest":
    raise SystemExit(17)
if stage == "missing":
    nodes = nodes[:-1]
elif stage == "extra":
    nodes.append(nodes[0] + "[unknown]")
elif stage == "duplicate":
    nodes += nodes[:1]
elif stage == "zero":
    nodes = []
root = ET.Element("testsuites")
suite = ET.SubElement(root, "testsuite", tests=str(len(nodes) + (stage == "count")),
                      skipped="0", errors="0", failures="0")
for node in nodes:
    module, name = node.split("::", 1)
    case = ET.SubElement(suite, "testcase", classname=module.removesuffix(".py").replace("/", "."), name=name)
    if stage in ("skipped", "error", "failure"):
        ET.SubElement(case, stage)
report = pathlib.Path(arguments[arguments.index("--junitxml") + 1])
if stage != "missing-report":
    report.write_bytes(ET.tostring(root))
"""
    )


class PlanReleaseNativeCollectionTests(unittest.TestCase):
    def test_closed_group_collects_executes_and_validates_same_nodes_with_cleanup(self):
        stages = (
            "",
            "create",
            "pytest",
            "drop",
            "collection",
            "empty-selection",
            "duplicate-selection",
            "missing-module",
            "foreign-module",
            "missing",
            "extra",
            "duplicate",
            "zero",
            "count",
            "skipped",
            "error",
            "failure",
            "missing-report",
            "dirty",
            "partial",
            "symlink",
            "absent",
        )
        before_database = {
            "collection",
            "empty-selection",
            "duplicate-selection",
            "missing-module",
            "foreign-module",
            "dirty",
            "partial",
            "symlink",
            "absent",
        }
        scenarios = [(stage, "") for stage in stages]
        scenarios.extend(
            (stage, artifact)
            for stage in ("", "pytest")
            for artifact in ("report", "selection")
        )
        for stage, cleanup_failure in scenarios:
            with (
                self.subTest(stage=stage, cleanup=cleanup_failure),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                source, runner = root / "source", root / "runner"
                source.mkdir()
                runner.mkdir()
                for relative in (*PLAN_RELEASE_TESTS, *PLAN_RELEASE_SUPPORT):
                    path = source / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    if stage != "absent":
                        path.touch()
                collector = source / "scripts/ci/shard_pytest_nodeids.py"
                collector.parent.mkdir(parents=True)
                collector.write_text(_plan_release_collector())
                if stage in ("partial", "symlink"):
                    path = source / PLAN_RELEASE_TESTS[-1]
                    path.unlink()
                    if stage == "symlink":
                        path.symlink_to(source / PLAN_RELEASE_TESTS[0])
                command = root / "timeout"
                command.write_text(_plan_release_command())
                command.chmod(0o755)
                log, lifecycle = root / "calls", root / "lifecycle"
                log.touch()
                lifecycle.touch()
                environment = {
                    **os.environ,
                    "SOURCE_ROOT": str(source),
                    "CI_ROOT": str(ROOT),
                    "RUNNER_TEMP": str(runner),
                    "PATH": str(root) + os.pathsep + os.environ["PATH"],
                    "CALL_LOG": str(log),
                    "LIFECYCLE": str(lifecycle),
                    "FAIL_STAGE": stage,
                    "CLEANUP_FAILURE": cleanup_failure,
                    "HLTHPRT_DB_DATABASE": "original_test",
                    "HLTHPRT_DB_HOST": "127.0.0.1",
                    "HLTHPRT_DB_PORT": "55432",
                    "HLTHPRT_DB_USER": "postgres",
                    "HLTHPRT_DB_PASSWORD": "synthetic",
                    "DB_SCHEMA": "sentinel",
                    "HLTHPRT_DB_DATABASE_OVERRIDE": "sentinel",
                    "COVERAGE_FILE": str(root / ".coverage"),
                    "PYTEST_ADDOPTS": "--cov=process --cov-branch",
                }
                script = (
                    CHECK_FUNCTIONS
                    + r"""
python() { command python3 "$@"; }
git() { [ "$FAIL_STAGE" != dirty ]; }
create_test_database() { printf 'create:%s\n' "$1" >> "$LIFECYCLE"; [ "$FAIL_STAGE" != create ] || return 17; }
drop_test_database() { printf 'drop:%s\n' "$1" >> "$LIFECYCLE"; [ "$FAIL_STAGE" != drop ] || return 7; }
rm() {
  command rm "$@" || return "$?"
  case "${*: -1}" in
    *"healthcare-plan-release-${CLEANUP_FAILURE}."*) [ -z "$CLEANUP_FAILURE" ] || return 7 ;;
  esac
}
run_plan_release_postgres
test "$DB_SCHEMA:$HLTHPRT_DB_DATABASE_OVERRIDE:$HLTHPRT_DB_DATABASE" = sentinel:sentinel:original_test
"""
                )
                result = subprocess.run(
                    ["bash", "-eu"],
                    input=script,
                    cwd=source,
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
                expected_status = (
                    0
                    if stage in ("", "absent") and not cleanup_failure
                    else 17
                    if stage in ("create", "pytest", "collection")
                    else 1
                )
                self.assertEqual(result.returncode, expected_status, result.stderr)
                calls = [json.loads(line) for line in log.read_text().splitlines()]
                collections = [
                    call["collection"] for call in calls if "collection" in call
                ]
                executions = [call for call in calls if "arguments" in call]
                self.assertEqual(
                    len(collections),
                    int(stage not in {"dirty", "partial", "symlink", "absent"}),
                )
                if collections:
                    self.assertEqual(
                        collections[0][:4], ["--shard-count", "1", "--shard-index", "0"]
                    )
                    self.assertEqual(collections[0][7:], list(PLAN_RELEASE_TESTS))
                self.assertEqual(
                    len(executions), int(stage not in before_database | {"create"})
                )
                if executions:
                    call = executions[0]
                    self.assertRegex(
                        call["database"], r"^hc_plan_release_test_[0-9a-f]{32}$"
                    )
                    self.assertEqual(
                        (call["host"], call["port"], call["user"], call["password"]),
                        ("127.0.0.1", "55432", "postgres", "synthetic"),
                    )
                    self.assertIsNone(call["alias"])
                    self.assertIsNone(call["override"])
                    self.assertEqual(call["coverage"], environment["COVERAGE_FILE"])
                    self.assertEqual(call["addopts"], environment["PYTEST_ADDOPTS"])
                    self.assertEqual(
                        call["arguments"][:6],
                        ["--foreground", "295s", "python", "-m", "pytest", "-q"],
                    )
                    self.assertEqual(call["arguments"][6], "@" + collections[0][5])
                    self.assertEqual(
                        call["selected"],
                        [
                            path + "::test_native[" + value + "]"
                            for path in PLAN_RELEASE_TESTS
                            for value in ("first", "second")
                        ],
                    )
                events = lifecycle.read_text().splitlines()
                self.assertEqual(len(events), 0 if stage in before_database else 2)
                if events:
                    self.assertEqual(
                        events[0].removeprefix("create:"),
                        events[1].removeprefix("drop:"),
                    )
                self.assertEqual(list(runner.iterdir()), [])
