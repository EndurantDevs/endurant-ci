"""The WAL regression owns a cluster without changing other test connections."""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import yaml

from tests.test_render_workflow import RENDERER

ROOT = Path(__file__).resolve().parents[1]
CHECK_FUNCTIONS = (
    (ROOT / "scripts/healthcare/check").read_text().rsplit("\ncase ", 1)[0]
)
TEST_PATH = "tests/test_provider_directory_publication_liveness_postgres.py"
TEST_NAME = "test_profile_projection_counts_both_spans_and_final_budget_counts_intervening_writes"
TEST_NODE = TEST_PATH + "::" + TEST_NAME
SERVICE = "a" * 64
OWNED = "b" * 64
IMAGE = "sha256:" + "c" * 64
REFERENCE = "postgis/postgis:18-3.6@sha256:60f6ad1d21ea86a67d47780b9a0d1e1d200500f62b19293fa834d0dea80b8677"

DOCKER = r"""
import json, os, pathlib, sys, time
arguments = sys.argv[1:]
state_path = pathlib.Path(os.environ["STATE"])
state = json.loads(state_path.read_text()) if state_path.exists() else {}
with open(os.environ["CALLS"], "a") as log:
    log.write(json.dumps(arguments) + "\n")
stage = os.environ["FAIL_STAGE"]
if arguments[0] == "inspect":
    target = arguments[-1]
    if target == "a" * 64:
        print(("d" if stage == "service" else "a") * 64,
              "foreign" if stage == "image" else "sha256:" + "c" * 64,
              "foreign" if stage == "reference" else os.environ["REFERENCE"])
    elif state.get("created"):
        if "Mounts" in arguments[arguments.index("--format") + 1]:
            print("e" * 64)
        elif "NetworkSettings" in arguments[arguments.index("--format") + 1]:
            print("0.0.0.0 55444" if stage == "port" else "127.0.0.1 55444")
        else:
            print("b" * 64, "foreign" if stage == "foreign" and state.get("ran") else state["owner"])
    elif stage == "collision":
        print("d" * 64, "foreign")
    else:
        print("Error: No such object: " + target, file=sys.stderr)
        sys.exit(1)
elif arguments[:2] == ["volume", "inspect"]:
    if state.get("created") or stage == "volume":
        print("[]")
    else:
        print("Error: No such volume: " + arguments[-1], file=sys.stderr)
        sys.exit(1)
elif arguments[0] == "create":
    if stage == "create":
        sys.exit(17)
    state.update(created=True, owner=arguments[arguments.index("--label") + 1].split("=", 1)[1])
    state_path.write_text(json.dumps(state))
    if stage != "partial_nocid":
        pathlib.Path(arguments[arguments.index("--cidfile") + 1]).write_text(("d" if stage == "cid" else "b") * 64 + "\n")
    sys.exit(17 if stage in ("partial", "partial_nocid") else 0)
elif arguments[0] == "start":
    sys.exit(17 if stage == "start" else 0)
elif arguments[0] == "rm":
    if stage == "cleanup":
        sys.exit(7)
    assert arguments[-1] == "b" * 64 and arguments[1:3] == ["--force", "--volumes"]
    if stage == "cancel":
        pathlib.Path(os.environ["CLEANUP_READY"]).write_text(str(os.getppid()))
        deadline = time.monotonic() + 5
        while not pathlib.Path(os.environ["CLEANUP_RELEASE"]).exists():
            if time.monotonic() >= deadline:
                sys.exit(19)
            time.sleep(0.01)
    state["created"] = False
    state_path.write_text(json.dumps(state))
else:
    raise AssertionError(arguments)
"""

PYTHON = r"""
import json, os, pathlib, sys, time
import xml.etree.ElementTree as ET
arguments = sys.argv[1:]
if arguments[:2] != ["-m", "pytest"]:
    os.execv(sys.executable, [sys.executable, *arguments])
with open(os.environ["PYTEST_CALLS"], "a") as output:
    output.write(json.dumps(dict(arguments=arguments, host=os.environ["HLTHPRT_DB_HOST"],
        port=os.environ["HLTHPRT_DB_PORT"], database=os.environ["HLTHPRT_DB_DATABASE"],
        alias=os.environ.get("DB_SCHEMA"), override=os.environ.get("HLTHPRT_DB_DATABASE_OVERRIDE"),
        coverage=os.environ.get("COVERAGE_FILE"), addopts=os.environ.get("PYTEST_ADDOPTS"))) + "\n")
state_path = pathlib.Path(os.environ["STATE"])
state = json.loads(state_path.read_text()); state["ran"] = True; state_path.write_text(json.dumps(state))
stage = os.environ["FAIL_STAGE"]
if stage == "cancel":
    pathlib.Path(os.environ["READY"]).touch()
    time.sleep(20)
if stage == "pytest":
    sys.exit(17)
parameters = ["None", "bulk", "metadata", "final"]
if stage == "missing": parameters.pop()
if stage == "duplicate": parameters[-1] = parameters[0]
if stage == "extra": parameters.append("foreign")
if stage == "identity": parameters[-1] = "foreign"
root = ET.Element("testsuites")
suite = ET.SubElement(root, "testsuite", tests=str(len(parameters)), skipped="0", errors="0", failures="0")
for parameter in parameters:
    case = ET.SubElement(suite, "testcase", classname="tests.test_provider_directory_publication_liveness_postgres",
        name=os.environ["TEST_NAME"] + "[" + parameter + "]")
    if stage in ("skipped", "error", "failure"):
        ET.SubElement(case, stage)
ET.ElementTree(root).write(arguments[arguments.index("--junitxml") + 1])
"""


def _executable(path, body):
    path.write_text(f"#!{sys.executable}\n" + body)
    path.chmod(0o755)


def _environment(root, stage):
    (root / "tests").mkdir()
    (root / "bin").mkdir()
    _executable(root / "bin/docker", DOCKER)
    _executable(root / "bin/python", PYTHON)
    _executable(
        root / "bin/psql",
        'import os; print("f" if os.environ["FAIL_STAGE"] == "settings" else "t")\n',
    )
    _executable(
        root / "bin/pg_isready",
        'import os, sys; sys.exit(int(os.environ["FAIL_STAGE"] == "ready"))\n',
    )
    (root / "bin/timeout").write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$TIMEOUTS"\nshift 2\nexec "$@"\n'
    )
    (root / "bin/timeout").chmod(0o755)
    return {
        **os.environ,
        "PATH": str(root / "bin") + os.pathsep + os.environ["PATH"],
        "SOURCE_ROOT": str(root),
        "CI_ROOT": str(ROOT),
        "RUNNER_TEMP": str(root),
        "CI_POSTGRES_SERVICE_ID": SERVICE,
        "FAIL_STAGE": stage,
        "REFERENCE": REFERENCE,
        "STATE": str(root / "state"),
        "CALLS": str(root / "calls"),
        "READY": str(root / "ready"),
        "CLEANUP_READY": str(root / "cleanup-ready"),
        "CLEANUP_RELEASE": str(root / "cleanup-release"),
        "PYTEST_CALLS": str(root / "pytest-calls"),
        "TIMEOUTS": str(root / "timeouts"),
        "HLTHPRT_DB_HOST": "shared",
        "HLTHPRT_DB_PORT": "5432",
        "HLTHPRT_DB_DATABASE": "shared_test",
        "HLTHPRT_DB_USER": "postgres",
        "HLTHPRT_DB_PASSWORD": "synthetic",
        "DB_SCHEMA": "sentinel",
        "HLTHPRT_DB_DATABASE_OVERRIDE": "sentinel",
        "COVERAGE_FILE": str(root / ".coverage"),
        "PYTEST_ADDOPTS": "--cov=process --cov-append",
        "TEST_NAME": TEST_NAME,
    }


def _script():
    return (
        CHECK_FUNCTIONS
        + r"""
sleep() { SECONDS=$((SECONDS + 61)); }
run_profile_wal_postgres "$TEST_NODE"
test "$HLTHPRT_DB_HOST:$HLTHPRT_DB_PORT:$HLTHPRT_DB_DATABASE" = shared:5432:shared_test
test "$DB_SCHEMA:$HLTHPRT_DB_DATABASE_OVERRIDE" = sentinel:sentinel
"""
    )


def _assert_clean(test, root, stage):
    calls = [json.loads(line) for line in (root / "calls").read_text().splitlines()]
    removals = [call for call in calls if call[0] == "rm"]
    created = stage not in ("service", "image", "reference", "collision", "create")
    test.assertEqual(len(removals), int(created and stage != "foreign"))
    test.assertFalse(any(SERVICE in call for call in removals))
    test.assertEqual(list(root.glob("healthcare-profile-wal.*")), [])
    if stage not in ("cleanup", "foreign") and (root / "state").exists():
        test.assertFalse(json.loads((root / "state").read_text())["created"])
    return calls


def _await_marker(test, path):
    """Bound synchronization with an actual child transport phase."""
    deadline = time.monotonic() + 5
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    test.assertTrue(path.exists())


def _assert_group_gone(test, group):
    """Require every member of the task-created child process group to exit."""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.killpg(group, 0)
        except ProcessLookupError:
            return
        time.sleep(0.01)
    test.fail("Task-created child process group remains")


class ProfileWalCollectionTests(unittest.TestCase):
    def test_rendered_workflow_binds_the_existing_service_without_new_jobs(self):
        with tempfile.TemporaryDirectory() as temporary:
            caller = Path(temporary) / "ci.yml"
            caller.write_text(
                yaml.safe_dump(
                    {
                        "on": {"push": {"branches": ["dev"]}},
                        "jobs": {
                            "smoke": {
                                "name": "portable import checks",
                                "runs-on": "ubuntu-latest",
                                "steps": [],
                            }
                        },
                    }
                )
            )
            workflow = yaml.safe_load(
                RENDERER.render_workflow("healthcare", "1" * 40, caller)
            )
            job = workflow["jobs"]["address-canonical-db-tests"]
            step = next(
                step
                for step in job["steps"]
                if step.get("name") == "Run complete validation stage"
            )
            self.assertEqual(
                step["env"]["CI_POSTGRES_SERVICE_ID"], "${{ job.services.postgres.id }}"
            )
            self.assertNotIn("CI_POSTGRES_SERVICE_ID", job["env"])
            self.assertEqual(job["services"]["postgres"]["image"], REFERENCE)
            self.assertEqual(job["timeout-minutes"], 20)
            self.assertEqual(
                sum(
                    len(
                        job.get("strategy", {}).get("matrix", {}).get("include", [None])
                    )
                    for job in workflow["jobs"].values()
                ),
                81,
            )

    def test_only_current_wal_case_moves_and_legacy_modules_keep_their_route(self):
        for current in (False, True):
            with (
                self.subTest(current=current),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                environment = _environment(root, "")
                environment["TEST_NODE"] = TEST_NODE
                for name in (
                    TEST_PATH,
                    "tests/test_provider_directory_profile_selection_desired_db.py",
                    "tests/test_provider_directory_profile_desired_snapshot_postgres.py",
                ):
                    (root / name).write_text(
                        f"async def {TEST_NAME if current else 'old_case'}(): pass\n"
                    )
                script = (
                    CHECK_FUNCTIONS
                    + r"""
run_available_cms_postgres() { printf 'shared %s\n' "$*"; }
run_profile_wal_postgres() { printf 'isolated %s\n' "$*"; }
run_profile_liveness_postgres
"""
                )
                outcome = subprocess.run(
                    ["bash", "-eu"],
                    input=script,
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                self.assertEqual(outcome.returncode, 0, outcome.stderr)
                lines = outcome.stdout.splitlines()
                shared = (
                    "shared runtime tests/test_provider_directory_profile_selection_desired_db.py "
                    + "tests/test_provider_directory_profile_desired_snapshot_postgres.py "
                    + TEST_PATH
                )
                self.assertEqual(
                    lines,
                    [shared + " --deselect=" + TEST_NODE, "isolated " + TEST_NODE]
                    if current
                    else [shared],
                )

    def test_cluster_lifecycle_and_strict_four_case_receipt(self):
        for stage in (
            "",
            "service",
            "image",
            "reference",
            "collision",
            "create",
            "partial",
            "partial_nocid",
            "cid",
            "start",
            "port",
            "ready",
            "settings",
            "pytest",
            "missing",
            "duplicate",
            "extra",
            "identity",
            "skipped",
            "error",
            "failure",
            "cleanup",
            "volume",
            "foreign",
        ):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                environment = _environment(root, stage)
                environment["TEST_NODE"] = TEST_NODE
                outcome = subprocess.run(
                    ["bash", "-eu"],
                    input=_script(),
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
                self.assertEqual(outcome.returncode == 0, stage == "", outcome.stderr)
                calls = _assert_clean(self, root, stage)
                if not stage:
                    self._assert_cluster_contract(root, environment, calls)

    def _assert_cluster_contract(self, root, environment, calls):
        create = next(call for call in calls if call[0] == "create")
        self.assertIn(IMAGE, create)
        self.assertEqual(
            create[-7:],
            [
                "postgres",
                "-c",
                "autovacuum=off",
                "-c",
                "checkpoint_timeout=1h",
                "-c",
                "timezone=UTC",
            ],
        )
        self.assertIn(
            "POSTGRES_INITDB_ARGS=--auth-local=scram-sha-256 --auth-host=scram-sha-256",
            create,
        )
        self.assertEqual(create[create.index("--publish") + 1], "127.0.0.1::5432")
        (call,) = [
            json.loads(line)
            for line in (root / "pytest-calls").read_text().splitlines()
        ]
        self.assertEqual(call["arguments"][:4], ["-m", "pytest", "-q", TEST_NODE])
        self.assertEqual((call["host"], call["port"]), ("127.0.0.1", "55444"))
        self.assertRegex(call["database"], r"^profile_wal_test_[0-9a-f]{32}$")
        self.assertIsNone(call["alias"])
        self.assertIsNone(call["override"])
        self.assertEqual(
            (call["coverage"], call["addopts"]),
            (environment["COVERAGE_FILE"], environment["PYTEST_ADDOPTS"]),
        )
        self.assertTrue(
            any(
                line.startswith("--foreground 420s python -m pytest")
                for line in (root / "timeouts").read_text().splitlines()
            )
        )

    def test_cancellation_drains_child_and_cleans_only_owned_container(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = _environment(root, "cancel")
            environment["TEST_NODE"] = TEST_NODE
            child = subprocess.Popen(
                ["bash", "-eu"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=environment,
                start_new_session=True,
            )
            try:
                child.stdin.write(_script())
                child.stdin.close()
                _await_marker(self, root / "ready")
                os.killpg(child.pid, signal.SIGTERM)
                _await_marker(self, root / "cleanup-ready")
                cleanup_pid = int((root / "cleanup-ready").read_text())
                self.assertEqual(os.getpgid(cleanup_pid), child.pid)
                os.kill(cleanup_pid, signal.SIGTERM)
                (root / "cleanup-release").touch()
                child.wait(timeout=5)
                self.assertNotEqual(child.returncode, 0)
                deadline = time.monotonic() + 5
                while (
                    list(root.glob("healthcare-profile-wal.*"))
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.01)
                calls = _assert_clean(self, root, "cancel")
                self.assertIn(["volume", "inspect", "e" * 64], calls)
                _assert_group_gone(self, child.pid)
            finally:
                (root / "cleanup-release").touch()
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait(timeout=5)
                _assert_group_gone(self, child.pid)
                child.stdout.close()
                child.stderr.close()


if __name__ == "__main__":
    unittest.main()
