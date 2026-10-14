"""Prepared read-only evidence supports offline reuse or complete stock execution."""

import copy
import hashlib
import http.client
import io
import json
import os
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import common_test_broker as broker

sys.path.pop(0)
common = broker.common


class Distribution:
    version = "1.0"

    def __init__(self):
        self.metadata = {"Name": "example-dependency"}
        self.files = []

    @staticmethod
    def read_text(name):
        return "example_dependency.py,sha256=example,10\n" if name == "RECORD" else None


class CommonTestBrokerIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.temporary = Path(temporary.name).resolve()
        self.source = self.temporary / "source"
        (self.source / "scripts").mkdir(parents=True)
        for name in ("common_test_result.py", "result_equivalence.py"):
            shutil.copyfile(SCRIPTS / name, self.source / "scripts" / name)
        (self.source / "tests").mkdir()
        (self.source / "tests/test_example.py").write_text(
            "# Complete synthetic test inventory\n"
        )
        (self.source / "pyproject.toml").write_text(
            "[project]\nname='example'\nversion='1.0'\n"
        )
        (self.source / ".github/workflows").mkdir(parents=True)
        (self.source / ".github/workflows/ci.yml").write_text("name: CI\n")
        self.git("init", "-q", "--template=")
        self.git("remote", "add", "origin", "https://github.com/example/project.git")
        self.git("add", ".")
        self.commit("Add synthetic source")
        self.original_sha = self.git("rev-parse", "HEAD")
        self.tree = self.git("rev-parse", "HEAD^{tree}")
        self.measurements = self.temporary / "measurements"
        self.executions = self.temporary / "executions.jsonl"
        command = [
            sys.executable,
            "-c",
            (
                "from pathlib import Path; import json, os; "
                "tokens={k:os.environ[k] for k in ('GH_TOKEN','GITHUB_TOKEN','SOURCE_TOKEN') if k in os.environ}; "
                "assert not tokens, 'Application received a workflow token'; "
                f"log=Path({str(self.executions)!r}); "
                "log.open('a').write(json.dumps({'tokens':tokens})+'\\n'); "
                "p=Path('count'); p.write_text(str(int(p.read_text())+1) if p.exists() else '1'); "
                "Path('report.json').write_text(json.dumps({'covered':10}))"
            ),
        ]
        self.arguments = common.parser().parse_args(
            [
                "--source-root",
                str(self.source),
                "--source-repository",
                "example/project",
                "--recipe-root",
                str(self.source),
                "--recipe-file",
                "@tracked",
                "--scope",
                "unit/full",
                "--step",
                "Run complete unit tests",
                "--output-file",
                "count",
                "--output-file",
                "report.json",
                "--measurements",
                str(self.measurements),
                "--",
                *command,
            ]
        )
        self.arguments.command = self.arguments.command[1:]
        self.arguments.data_root = self.source
        self.key = hashlib.sha256(self.arguments.scope.encode()).hexdigest()
        self.original_producer = {
            "repository_id": 10,
            "commit": self.original_sha,
            "workflow": ".github/workflows/ci.yml",
            "run_id": 20,
            "run_attempt": 1,
            "job_id": 30,
            "step": self.arguments.step,
        }
        now = datetime.now(timezone.utc).isoformat()
        self.original_run = {
            "id": 20,
            "run_attempt": 1,
            "head_sha": self.original_sha,
            "path": self.original_producer["workflow"],
            "repository": {"id": 10, "full_name": "example/project"},
            "head_repository": {"id": 10},
            "event": "push",
            "status": "completed",
            "conclusion": "success",
            "check_suite_id": 40,
            "created_at": now,
        }
        self.original_job = {
            "id": 30,
            "run_id": 20,
            "run_attempt": 1,
            "head_sha": self.original_sha,
            "name": "Complete unit tests",
            "status": "completed",
            "conclusion": "success",
            "check_run_url": "https://api.github.com/repos/example/project/check-runs/31",
            "steps": [
                {
                    "name": self.arguments.step,
                    "number": 2,
                    "status": "completed",
                    "conclusion": "success",
                },
                {
                    "name": common.UPLOAD_STEP,
                    "number": 3,
                    "status": "completed",
                    "conclusion": "success",
                    "started_at": now,
                    "completed_at": now,
                },
            ],
        }
        self.original_check = {
            "id": 31,
            "name": self.original_job["name"],
            "head_sha": self.original_sha,
            "app": {"id": 15368},
            "check_suite": {"id": 40},
            "status": "completed",
            "conclusion": "success",
            "details_url": "https://github.com/example/project/actions/runs/20/job/30",
        }
        environment = patch.dict(
            os.environ,
            {
                "GH_TOKEN": "synthetic-read-only-token",
                "GITHUB_TOKEN": "synthetic-read-only-token",
                "SOURCE_TOKEN": "synthetic-read-only-token",
                "GITHUB_REPOSITORY": "example/project",
                "GITHUB_RUN_ID": "20",
                "GITHUB_RUN_ATTEMPT": "1",
                "GITHUB_SHA": self.original_sha,
                "RUNNER_NAME": "example-runner",
                "RUNNER_OS": "Linux",
                "RUNNER_ARCH": "X64",
                "ImageOS": "example-linux",
                "ImageVersion": "1",
                "GITHUB_ENV": str(self.temporary / "github-env"),
                "GITHUB_OUTPUT": str(self.temporary / "github-output"),
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        helper = patch.object(
            common, "__file__", str(self.source / "scripts/common_test_result.py")
        )
        helper.start()
        self.addCleanup(helper.stop)
        distributions = patch.object(
            common.equivalent.importlib.metadata,
            "distributions",
            return_value=[Distribution()],
        )
        distributions.start()
        self.addCleanup(distributions.stop)
        with (
            patch.object(
                common,
                "current_producer",
                return_value=(self.original_producer, self.original_run),
            ),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                common.execute_isolated(
                    self.arguments,
                    read=lambda *_: {"id": 10},
                    choose=lambda *_: (None, None),
                ),
                0,
            )
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            for path in self.measurements.rglob("*"):
                if path.is_file():
                    archive.writestr(
                        path.relative_to(self.measurements).as_posix(),
                        path.read_bytes(),
                    )
        self.archive = data.getvalue()
        self.artifact = {
            "id": 50,
            "name": common.artifact_name(self.original_producer),
            "expired": False,
            "digest": "sha256:" + hashlib.sha256(self.archive).hexdigest(),
            "created_at": now,
            "workflow_run": {"id": 20, "head_sha": self.original_sha},
        }
        self.current_sha = self.git("rev-parse", "HEAD")
        self.current_run = {
            **self.original_run,
            "id": 21,
            "run_attempt": 2,
            "head_sha": self.current_sha,
            "status": "in_progress",
            "conclusion": None,
        }
        self.current_job = {
            "id": 32,
            "run_id": 21,
            "run_attempt": 2,
            "head_sha": self.current_sha,
            "name": self.original_job["name"],
            "runner_name": "example-runner",
            "status": "in_progress",
            "steps": [
                {
                    "name": "Prepare read-only common-test evidence",
                    "number": 1,
                    "status": "in_progress",
                },
                {"name": self.arguments.step, "number": 2, "status": "queued"},
                {"name": common.UPLOAD_STEP, "number": 3, "status": "queued"},
            ],
        }
        self.broker_arguments = SimpleNamespace(
            source_root=self.source,
            source_repository="example/project",
            step=self.arguments.step,
            directory=self.temporary / "evidence",
            source_authority=None,
            source_authority_sha256=None,
        )
        os.environ.update(
            GITHUB_RUN_ID="21", GITHUB_RUN_ATTEMPT="2", GITHUB_SHA=self.current_sha
        )
        self.api_calls = []

    def git(self, *arguments):
        return subprocess.check_output(
            ["git", "-C", str(self.source), *arguments], text=True
        ).strip()

    def commit(self, message, *arguments):
        self.git(
            "-c",
            "user.name=Example",
            "-c",
            "user.email=example@example.invalid",
            "commit",
            "-qm",
            message,
            *arguments,
        )

    def read(self, repository, path):
        self.api_calls.append((repository, path))
        self.assertEqual(repository, "example/project")
        responses = {
            "": {"id": 10},
            "actions/runs/21": self.current_run,
            "actions/runs/21/attempts/2/jobs?per_page=100": {
                "jobs": [self.current_job]
            },
            "actions/jobs/32": self.current_job,
            "actions/workflows/ci.yml/runs?status=success&per_page=30": {
                "workflow_runs": [self.current_run, self.original_run]
            },
            "actions/runs/20": self.original_run,
            "actions/runs/20/attempts/1/jobs?per_page=100": {
                "jobs": [self.original_job]
            },
            "actions/runs/20/artifacts?per_page=100": {"artifacts": [self.artifact]},
            "actions/jobs/30": self.original_job,
            "check-runs/31": self.original_check,
            "git/commits/" + self.original_sha: {
                "sha": self.original_sha,
                "tree": {"sha": self.tree},
            },
        }
        if path == "git/trees/" + self.tree + "?recursive=1":
            entries = []
            for line in self.git("ls-tree", "-r", self.tree).splitlines():
                metadata, name = line.split("\t")
                mode, kind, sha = metadata.split()
                entries.append({"path": name, "mode": mode, "type": kind, "sha": sha})
            return {"sha": self.tree, "truncated": False, "tree": entries}
        self.assertIn(path, responses, "Unexpected read-only evidence path")
        return copy.deepcopy(responses[path])

    def prepare(self):
        with (
            patch.object(common, "api", side_effect=self.read),
            patch.object(common, "download", return_value=self.archive) as download,
        ):
            document = broker.prepare(self.broker_arguments)
        self.arguments.evidence_directory = self.broker_arguments.directory
        self.bind_evidence()
        return document, download

    def bind_evidence(self, document=None):
        path = self.broker_arguments.directory / "evidence.json"
        if document is not None:
            path.write_bytes(common.equivalent.encoded(document) + b"\n")
        self.arguments.evidence_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()

    def consume(self):
        with patch.dict(os.environ):
            for name in ("GH_TOKEN", "GITHUB_TOKEN", "SOURCE_TOKEN"):
                os.environ.pop(name, None)
            with (
                patch.object(
                    common,
                    "api",
                    side_effect=AssertionError(
                        "Application attempted live evidence API"
                    ),
                ),
                patch.object(
                    common,
                    "download",
                    side_effect=AssertionError(
                        "Application attempted live artifact download"
                    ),
                ),
                patch.object(
                    common,
                    "current_producer",
                    side_effect=AssertionError("Offline evidence was not used"),
                ),
                patch.object(
                    common.urllib.request,
                    "urlopen",
                    side_effect=AssertionError(
                        "Application attempted network evidence"
                    ),
                ),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(common.execute_isolated(self.arguments), 0)

    def assert_stock(self):
        self.assertEqual((self.source / "count").read_text(), "2")
        self.assertEqual(
            json.loads(self.executions.read_text().splitlines()[-1]), {"tokens": {}}
        )
        record = self.measurements / self.key / "measurement.json"
        if record.exists():
            value = json.loads(record.read_bytes())
            self.assertTrue(value["measurement"]["executed"])
            self.assertIsNone(value["origin"])

    def test_broker_binds_source_tree_current_attempt_and_immutable_archive_without_source_tokens(
        self,
    ):
        original_archive = self.archive
        document, download = self.prepare()
        self.assertEqual(
            document["source"],
            {"repository_id": 10, "commit": self.current_sha, "tree": self.tree},
        )
        self.assertEqual(
            document["producer"],
            {
                **self.original_producer,
                "commit": self.current_sha,
                "run_id": 21,
                "run_attempt": 2,
                "job_id": 32,
            },
        )
        self.assertEqual(
            document["archives"],
            {
                "50": {
                    "file": "50.zip",
                    "sha256": hashlib.sha256(self.archive).hexdigest(),
                }
            },
        )
        download.assert_called_once_with("example/project", 50)
        exports = (self.temporary / "github-env").read_text()
        self.assertIn(
            "COMMON_TEST_EVIDENCE_DIRECTORY=" + str(self.broker_arguments.directory),
            exports,
        )
        self.assertIn(
            "COMMON_TEST_EVIDENCE_SHA256=" + self.arguments.evidence_sha256, exports
        )
        self.consume()
        self.assertEqual((self.source / "count").read_text(), "1")
        self.assertEqual(len(self.executions.read_text().splitlines()), 1)
        value = json.loads(
            (self.measurements / self.key / "measurement.json").read_bytes()
        )
        self.assertFalse(value["measurement"]["executed"])
        self.assertEqual(
            value["origin"]["origin"]["source"]["commit"], self.original_sha
        )
        self.assertEqual(value["measurement"]["source"]["commit"], self.current_sha)
        self.assertEqual(
            value["origin"]["archive_sha256"],
            hashlib.sha256(original_archive).hexdigest(),
        )
        self.assertEqual(self.archive, original_archive)

    def test_evidence_schema_time_current_source_and_attempt_changes_run_stock(self):
        original, _ = self.prepare()
        changes = (
            ("schema", "unknown"),
            (
                "created_at",
                (datetime.now(timezone.utc) - timedelta(minutes=46)).isoformat(),
            ),
            (
                "created_at",
                (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(),
            ),
            ("source", {**original["source"], "commit": "a" * 40}),
            ("source", {**original["source"], "tree": "e" * 40}),
            ("source", {**original["source"], "repository_id": 11}),
            ("producer", {**original["producer"], "run_id": 22}),
            ("producer", {**original["producer"], "run_attempt": 1}),
            ("producer", {**original["producer"], "step": "Another step"}),
            ("responses", {}),
        )
        for name, value in changes:
            with self.subTest(field=name, value=value):
                (self.source / "count").write_text("1")
                self.bind_evidence({**copy.deepcopy(original), name: value})
                self.consume()
                self.assert_stock()

    def test_tampered_or_malformed_broker_metadata_runs_stock_and_removes_stale_measurements(
        self,
    ):
        self.prepare()
        path = self.broker_arguments.directory / "evidence.json"
        path.write_text("{malformed evidence")
        stale = self.measurements / self.key / "stale-marker"
        stale.write_text("Never publish this record")
        self.consume()
        self.assert_stock()
        self.assertFalse(stale.exists())
        self.assertFalse(stale.parent.exists())
        (self.source / "count").write_text("1")
        self.bind_evidence()
        self.consume()
        self.assert_stock()

    def test_tampered_or_redirected_raw_archive_requires_complete_execution(self):
        original, _ = self.prepare()
        archive = self.broker_arguments.directory / "50.zip"
        original_bytes = archive.read_bytes()
        for kind in ("bytes", "digest", "path"):
            with self.subTest(kind=kind):
                archive.write_bytes(
                    original_bytes + b"tampered" if kind == "bytes" else original_bytes
                )
                document = copy.deepcopy(original)
                if kind == "digest":
                    document["archives"]["50"]["sha256"] = "e" * 64
                if kind == "path":
                    document["archives"]["50"]["file"] = "../50.zip"
                self.bind_evidence(document)
                (self.source / "count").write_text("1")
                self.consume()
                self.assert_stock()

    def test_original_failed_partial_foreign_or_superseded_native_proof_cannot_be_reused(
        self,
    ):
        original, _ = self.prepare()
        cases = (
            ("actions/runs/20", "conclusion", "failure"),
            ("actions/runs/20", "run_attempt", 2),
            ("actions/runs/20", "head_repository", {"id": 11}),
            ("actions/jobs/30", "conclusion", "skipped"),
            ("actions/jobs/30", "run_attempt", 2),
            (
                "actions/jobs/30",
                "steps",
                [
                    {**self.original_job["steps"][0], "conclusion": "skipped"},
                    self.original_job["steps"][1],
                ],
            ),
            (
                "actions/runs/20/artifacts?per_page=100",
                "artifacts",
                [{**self.artifact, "expired": True}],
            ),
            ("check-runs/31", "app", {"id": 99}),
            ("check-runs/31", "head_sha", "e" * 40),
            ("check-runs/31", "details_url", "https://example.invalid/"),
        )
        for path, field, value in cases:
            with self.subTest(path=path, field=field):
                document = copy.deepcopy(original)
                document["responses"]["example/project/" + path][field] = value
                self.bind_evidence(document)
                (self.source / "count").write_text("1")
                self.consume()
                self.assert_stock()

    def test_prefetched_reused_measurement_is_not_an_original_complete_execution(self):
        output = io.BytesIO()
        with (
            zipfile.ZipFile(io.BytesIO(self.archive)) as source,
            zipfile.ZipFile(output, "w") as target,
        ):
            for name in source.namelist():
                data = source.read(name)
                if name.endswith("/measurement.json"):
                    document = json.loads(data)
                    document["measurement"]["executed"] = False
                    data = common.equivalent.encoded(document)
                target.writestr(name, data)
        self.archive = output.getvalue()
        self.artifact["digest"] = "sha256:" + hashlib.sha256(self.archive).hexdigest()
        document, _ = self.prepare()
        self.assertIn("50", document["archives"])
        self.consume()
        self.assert_stock()

    def test_broker_rejects_changed_archive_digest_before_retaining_candidate(self):
        self.artifact["digest"] = "sha256:" + "e" * 64
        document, download = self.prepare()
        self.assertEqual(document["archives"], {})
        self.assertEqual(
            document["responses"][
                "example/project/actions/workflows/ci.yml/runs?status=success&per_page=30"
            ],
            {"workflow_runs": []},
        )
        download.assert_called_once_with("example/project", 50)
        self.consume()
        self.assert_stock()

    def test_source_changed_after_preparation_cannot_consume_previous_current_snapshot(
        self,
    ):
        self.prepare()
        self.commit("Another current commit", "--allow-empty")
        self.consume()
        self.assert_stock()

    def test_preparation_requires_one_future_complete_step_after_active_broker(self):
        original = copy.deepcopy(self.current_job["steps"])
        cases = (
            [original[0]],
            [*original, copy.deepcopy(original[1])],
            [
                original[0],
                {**original[1], "status": "completed", "conclusion": "success"},
                original[2],
            ],
            [original[0], {**original[1], "number": 0}, original[2]],
        )
        for index, steps in enumerate(cases):
            with self.subTest(steps=steps):
                self.current_job["steps"] = steps
                self.broker_arguments.directory = (
                    self.temporary / f"invalid-evidence-{index}"
                )
                with (
                    patch.object(common, "api", side_effect=self.read),
                    patch.object(common, "download", return_value=self.archive),
                    self.assertRaises(ValueError),
                ):
                    broker.prepare(self.broker_arguments)

    def test_partial_or_malformed_optional_archive_is_not_retained(self):
        for index, error in enumerate(
            (
                http.client.IncompleteRead(b"partial", 10),
                AttributeError("Malformed archive"),
            )
        ):
            with self.subTest(error=type(error).__name__):
                self.broker_arguments.directory = (
                    self.temporary / f"unavailable-{index}"
                )
                with (
                    patch.object(common, "api", side_effect=self.read),
                    patch.object(common, "download", side_effect=error),
                ):
                    document = broker.prepare(self.broker_arguments)
                self.assertEqual(document["archives"], {})
                self.assertEqual(
                    document["responses"][
                        "example/project/actions/workflows/ci.yml/runs?status=success&per_page=30"
                    ],
                    {"workflow_runs": []},
                )
                self.assertFalse(list(self.broker_arguments.directory.glob("*.zip")))

    def test_cli_evidence_failure_does_not_abort_the_required_complete_step(self):
        for index, error in enumerate(
            (
                http.client.IncompleteRead(b"partial", 10),
                AttributeError("Malformed API response"),
            )
        ):
            directory = self.temporary / f"cli-unavailable-{index}"
            output = io.StringIO()
            with (
                self.subTest(error=type(error).__name__),
                patch.object(common, "current_producer", side_effect=error),
                patch.object(
                    sys,
                    "argv",
                    [
                        str(SCRIPTS / "common_test_broker.py"),
                        "--source-root",
                        str(self.source),
                        "--source-repository",
                        "example/project",
                        "--step",
                        self.arguments.step,
                        "--directory",
                        str(directory),
                    ],
                ),
                patch.object(sys, "pycache_prefix", sys.pycache_prefix),
                patch.object(sys, "dont_write_bytecode", sys.dont_write_bytecode),
                redirect_stdout(output),
            ):
                runpy.run_path(
                    str(SCRIPTS / "common_test_broker.py"), run_name="__main__"
                )
            self.assertIn("Full common-test execution required:", output.getvalue())
            self.assertFalse((directory / "evidence.json").exists())


if __name__ == "__main__":
    unittest.main()
