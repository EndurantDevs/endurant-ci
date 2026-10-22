"""Exercise authenticated reuse and unchanged stock execution at the command boundary."""

import hashlib
import importlib.util
import io
import json
import os
import py_compile
import shutil
import subprocess
import sys
import tempfile
import traceback
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from email.message import Message
from http.client import IncompleteRead
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location(
    "common_test_result", SCRIPTS / "common_test_result.py"
)
common = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(common)
equivalent = common.equivalent


class Distribution:
    version = "1.0"

    def __init__(self):
        self.metadata = {"Name": "example-dependency"}
        self.files = []

    @staticmethod
    def read_text(name):
        return "example_dependency.py,sha256=example,10\n" if name == "RECORD" else None


class CurrentProducerPaginationTests(unittest.TestCase):
    def setUp(self):
        self.run = {
            "id": 21,
            "run_attempt": 2,
            "repository": {"id": 10, "full_name": "example/project"},
            "head_repository": {"id": 10},
            "head_sha": "a" * 40,
            "event": "push",
            "path": ".github/workflows/ci.yml",
        }
        self.active = {
            "id": 32,
            "runner_name": "example-runner",
            "status": "in_progress",
            "steps": [{"name": "Complete tests", "status": "in_progress"}],
        }
        self.padding = [
            {"id": 100 + index, "runner_name": "other", "steps": []}
            for index in range(50)
        ]
        environment = patch.dict(
            os.environ,
            {
                "GITHUB_RUN_ID": "21",
                "GITHUB_RUN_ATTEMPT": "2",
                "RUNNER_NAME": "example-runner",
            },
        )
        environment.start()
        self.addCleanup(environment.stop)

    def test_current_producer_after_first_page_is_identified(self):
        with patch.object(
            common,
            "api",
            side_effect=[
                self.run,
                {"jobs": self.padding, "total_count": 51},
                {"jobs": [self.active], "total_count": 51},
            ],
        ) as read:
            producer, run = common.current_producer("example/project", "Complete tests")
        self.assertEqual(producer["job_id"], 32)
        self.assertEqual(run, self.run)
        self.assertEqual(
            [call.args[1] for call in read.call_args_list],
            [
                "actions/runs/21",
                "actions/runs/21/attempts/2/jobs?per_page=50",
                "actions/runs/21/attempts/2/jobs?per_page=50&page=2",
            ],
        )

    def test_current_producer_ambiguity_across_pages_is_refused(self):
        first = [self.active, *self.padding[:49]]
        second = [{**self.active, "id": 33}]
        with (
            patch.object(
                common,
                "api",
                side_effect=[
                    self.run,
                    {"jobs": first, "total_count": 51},
                    {"jobs": second, "total_count": 51},
                ],
            ),
            self.assertRaisesRegex(ValueError, "uniquely"),
        ):
            common.current_producer("example/project", "Complete tests")

    def test_current_producer_refuses_incomplete_or_changed_inventory(self):
        for pages in (
            [{"jobs": [self.active], "total_count": 81}],
            [
                {"jobs": self.padding, "total_count": 51},
                {"jobs": [self.active], "total_count": 52},
            ],
        ):
            with (
                self.subTest(pages=pages),
                patch.object(common, "api", side_effect=[self.run, *pages]),
                self.assertRaises(ValueError),
            ):
                common.current_producer("example/project", "Complete tests")

    def test_attempt_inventory_refuses_invalid_pagination_metadata(self):
        for document in (
            None,
            {"jobs": [self.active]},
            {"jobs": [self.active], "total_count": True},
            {"jobs": [self.active], "total_count": -1},
            {"jobs": None, "total_count": 0},
            {"jobs": [None], "total_count": 1},
            {"jobs": [self.active], "total_count": 0},
            {"jobs": [*self.padding, self.active], "total_count": 51},
        ):
            with (
                self.subTest(document=document),
                patch.object(common, "api", return_value=document),
                self.assertRaises(ValueError),
            ):
                common.attempt_jobs("example/project", self.run)

    def test_later_page_errors_and_duplicate_ids_never_return_partial_jobs(self):
        failure = OSError("Synthetic unavailable page")
        for final in (failure, {"jobs": [self.padding[0]], "total_count": 51}):
            with (
                self.subTest(final=final),
                patch.object(
                    common,
                    "api",
                    side_effect=[{"jobs": self.padding, "total_count": 51}, final],
                ),
                self.assertRaises(OSError if final is failure else ValueError),
            ):
                common.attempt_jobs("example/project", self.run)

    def test_attempt_inventory_keeps_original_bounded_window(self):
        for count in (999, 1000):
            jobs = [{"id": index + 1} for index in range(count)]
            pages = [
                {"jobs": jobs[start : start + 50], "total_count": count}
                for start in range(0, count, 50)
            ]
            with (
                self.subTest(count=count),
                patch.object(common, "api", side_effect=pages) as read,
            ):
                if count == 1000:
                    with self.assertRaisesRegex(ValueError, "bounded window"):
                        common.attempt_jobs("example/project", self.run)
                else:
                    self.assertEqual(
                        common.attempt_jobs("example/project", self.run), jobs
                    )
            self.assertEqual(read.call_count, 1 if count == 1000 else 20)


class CommonResultIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "source"
        self.root.mkdir()
        (self.root / "scripts").mkdir()
        for name in ("common_test_result.py", "result_equivalence.py"):
            shutil.copyfile(SCRIPTS / name, self.root / "scripts" / name)
        (self.root / "tests").mkdir()
        (self.root / "tests/test_example.py").write_text(
            "# Complete example test scope\n"
        )
        (self.root / "pyproject.toml").write_text(
            "[project]\nname = 'example'\nversion = '1.0'\n"
        )
        (self.root / ".github/workflows").mkdir(parents=True)
        (self.root / ".github/workflows/ci.yml").write_text("name: CI\n")
        self.git("init", "-q")
        self.git("remote", "add", "origin", "https://github.com/example/project.git")
        self.git("add", ".")
        self.git(
            "-c",
            "user.name=Example",
            "-c",
            "user.email=example@example.invalid",
            "commit",
            "-qm",
            "Test fixture",
        )
        self.source_sha = self.git("rev-parse", "HEAD").strip()
        self.tree = self.git("rev-parse", "HEAD^{tree}").strip()
        self.directory = Path(temporary.name) / "measurements"
        command = [
            sys.executable,
            "-c",
            (
                "from pathlib import Path; import json; "
                "p=Path('count'); p.write_text(str(int(p.read_text())+1) if p.exists() else '1'); "
                "Path('report.json').write_text(json.dumps({'covered': 10}))"
            ),
        ]
        self.arguments = common.parser().parse_args(
            [
                "--source-root",
                str(self.root),
                "--source-repository",
                "example/project",
                "--recipe-root",
                str(self.root),
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
                str(self.directory),
                "--",
                *command,
            ]
        )
        self.arguments.command = self.arguments.command[1:]
        self.arguments.data_root = self.root
        self.producer = {
            "repository_id": 10,
            "commit": self.source_sha,
            "workflow": ".github/workflows/ci.yml",
            "run_id": 20,
            "run_attempt": 1,
            "job_id": 30,
            "step": self.arguments.step,
        }
        now = datetime.now(timezone.utc).isoformat()
        self.run = {
            "id": 20,
            "run_attempt": 1,
            "head_sha": self.source_sha,
            "path": self.producer["workflow"],
            "repository": {"id": 10, "full_name": "example/project"},
            "head_repository": {"id": 10},
            "event": "push",
            "status": "completed",
            "conclusion": "success",
            "check_suite_id": 40,
            "created_at": now,
        }
        self.job = {
            "id": 30,
            "run_id": 20,
            "run_attempt": 1,
            "head_sha": self.source_sha,
            "name": "Complete unit tests",
            "status": "completed",
            "conclusion": "success",
            "check_run_url": "https://api.github.com/repos/example/project/check-runs/31",
            "steps": [
                {
                    "name": self.arguments.step,
                    "number": 1,
                    "status": "completed",
                    "conclusion": "success",
                },
                {
                    "name": common.UPLOAD_STEP,
                    "number": 2,
                    "status": "completed",
                    "conclusion": "success",
                    "started_at": now,
                    "completed_at": now,
                },
            ],
        }
        self.check = {
            "id": 31,
            "name": self.job["name"],
            "head_sha": self.source_sha,
            "app": {"id": 15368},
            "check_suite": {"id": 40},
            "status": "completed",
            "conclusion": "success",
            "details_url": "https://github.com/example/project/actions/runs/20/job/30",
        }
        self.now = now
        self.archive = None
        self.environment = {
            "GH_TOKEN": "example-read-only-token",
            "GITHUB_REPOSITORY": "example/project",
            "GITHUB_OUTPUT": str(self.root.parent / "github-output"),
            "GITHUB_ENV": str(self.root.parent / "github-env"),
            "RUNNER_OS": "macOS",
            "RUNNER_ARCH": "ARM64",
            "ImageOS": "macos-example",
            "ImageVersion": "1",
        }
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, self.environment).start()
        patch.object(
            common, "__file__", str(self.root / "scripts/common_test_result.py")
        ).start()
        patch.object(
            equivalent.importlib.metadata,
            "distributions",
            return_value=[Distribution()],
        ).start()
        patch.object(common, "api", side_effect=self.read).start()
        patch.object(common, "download", side_effect=lambda *_: self.archive).start()
        self.current_producer = patch.object(
            common, "current_producer", return_value=(self.producer, self.run)
        ).start()

    def git(self, *arguments):
        return subprocess.check_output(
            ["git", "-C", str(self.root), *arguments], text=True
        )

    def read(self, repository, path):
        self.assertEqual(repository, "example/project")
        if not path:
            return {"id": 10}
        if path.startswith("actions/workflows/"):
            return {"workflow_runs": [self.run] if self.archive else []}
        if path == "actions/runs/20":
            return self.run
        if path == "actions/runs/20/artifacts?per_page=100":
            return {
                "artifacts": [
                    {
                        "id": 50,
                        "name": common.artifact_name(self.producer),
                        "expired": False,
                        "digest": "sha256:" + hashlib.sha256(self.archive).hexdigest(),
                        "created_at": self.now,
                        "workflow_run": {"id": 20, "head_sha": self.run["head_sha"]},
                    }
                ]
            }
        if path == "actions/jobs/30":
            return self.job
        if path == "check-runs/31":
            return self.check
        if path == "git/commits/" + self.source_sha:
            return {"sha": self.source_sha, "tree": {"sha": self.tree}}
        if path == "git/trees/" + self.tree + "?recursive=1":
            entries = []
            for line in self.git("ls-tree", "-r", self.tree).splitlines():
                metadata, name = line.split("\t")
                mode, kind, sha = metadata.split()
                entries.append({"path": name, "mode": mode, "type": kind, "sha": sha})
            return {"sha": self.tree, "truncated": False, "tree": entries}
        self.fail("Unexpected evidence API path: " + path)

    def original_execution(self):
        self.assertEqual(common.execute_isolated(self.arguments, read=self.read), 0)
        self.assertEqual((self.root / "count").read_text(), "1")
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            for path in self.directory.rglob("*"):
                if path.is_file():
                    archive.writestr(
                        path.relative_to(self.directory).as_posix(), path.read_bytes()
                    )
        self.archive = output.getvalue()
        self.current_producer.return_value = (
            {
                **self.producer,
                "commit": self.git("rev-parse", "HEAD").strip(),
                "run_id": 21,
            },
            self.run,
        )

    def test_real_command_executes_once_and_new_run_verifies_original_data(self):
        self.original_execution()
        original_archive = self.archive
        (self.root / "count").unlink()
        (self.root / "report.json").unlink()
        self.assertEqual(common.execute_isolated(self.arguments, read=self.read), 0)
        self.assertEqual((self.root / "count").read_text(), "1")
        self.assertEqual(self.archive, original_archive)
        receipt = json.loads(
            next(self.directory.glob("*/measurement.json")).read_text()
        )
        self.assertFalse(receipt["measurement"]["executed"])
        self.assertEqual(
            receipt["origin"]["origin"]["source"]["commit"], self.source_sha
        )
        self.assertEqual(receipt["measurement"]["source"]["commit"], self.source_sha)
        self.assertEqual(receipt["measurement"]["producer"]["run_id"], 21)

    def test_same_tree_with_changed_git_identity_executes_complete_command(self):
        self.original_execution()
        self.git(
            "-c",
            "user.name=Example",
            "-c",
            "user.email=example@example.invalid",
            "commit",
            "--allow-empty",
            "-qm",
            "Changed observable identity",
        )
        self.current_producer.return_value = (
            {
                **self.producer,
                "commit": self.git("rev-parse", "HEAD").strip(),
                "run_id": 21,
            },
            self.run,
        )
        self.assertEqual(self.git("rev-parse", "HEAD^{tree}").strip(), self.tree)
        self.assertEqual(common.execute_isolated(self.arguments, read=self.read), 0)
        self.assertEqual((self.root / "count").read_text(), "2")
        document = json.loads(
            next(self.directory.glob("*/measurement.json")).read_text()
        )
        self.assertTrue(document["measurement"]["executed"])
        self.assertIsNone(document["origin"])

    def test_identity_environment_is_measured_without_changing_test_environment(self):
        self.original_execution()
        for name in ("GITHUB_SHA", "GITHUB_REF", "SOURCE_SHA"):
            with (
                self.subTest(name=name),
                patch.dict(os.environ, {name: "changed-identity"}),
            ):
                current = common.collect(self.arguments, self.producer, 10)
                self.assertEqual(
                    current["inputs"]["identity_environment"][name], "changed-identity"
                )
                self.assertEqual(os.environ[name], "changed-identity")
                original = json.loads(
                    next(self.directory.glob("*/measurement.json")).read_text()
                )
                self.assertNotEqual(
                    current["fingerprint"], original["measurement"]["fingerprint"]
                )

    def test_final_job_refresh_rejects_superseded_original_after_offline_reuse(self):
        self.original_execution()
        self.assertEqual(common.execute_isolated(self.arguments, read=self.read), 0)
        document = json.loads(
            next(self.directory.glob("*/measurement.json")).read_text()
        )
        current_sha = document["measurement"]["producer"]["commit"]
        current_run = {
            **self.run,
            "id": 21,
            "head_sha": current_sha,
            "status": "in_progress",
            "conclusion": None,
        }
        current_job = {**self.job, "run_id": 21, "head_sha": current_sha}
        current_check = {
            **self.check,
            "head_sha": current_sha,
            "details_url": "https://github.com/example/project/actions/runs/21/job/30",
        }
        artifact = {
            "id": 51,
            "name": "common-tests-21-1-30",
            "expired": False,
            "digest": "sha256:" + "a" * 64,
            "created_at": self.now,
            "workflow_run": {"id": 21, "head_sha": current_sha},
        }
        original_read = self.read

        def read(repository, path):
            if path == "actions/jobs/30":
                # The origin and consuming fixture deliberately use the same
                # numeric job ID; serve their distinct reads in call order.
                return current_job if not getattr(read, "job_read", False) else self.job
            if path == "check-runs/31":
                read.job_read = True
                return (
                    current_check
                    if not getattr(read, "check_read", False)
                    else self.check
                )
            if path == "git/commits/" + current_sha:
                read.check_read = True
                return {"sha": current_sha, "tree": {"sha": self.tree}}
            if path == "actions/artifacts/50":
                return original_read(
                    repository, "actions/runs/20/artifacts?per_page=100"
                )["artifacts"][0]
            return original_read(repository, path)

        with patch.object(common, "api", side_effect=read):
            with (
                patch.object(
                    common, "download", side_effect=IncompleteRead(b"partial")
                ),
                self.assertRaises(IncompleteRead),
            ):
                common.refresh_document(
                    document, artifact, "a" * 64, current_run, "example/project"
                )
            read.job_read = read.check_read = False
            refreshed = common.refresh_document(
                document, artifact, "a" * 64, current_run, "example/project"
            )
            self.assertEqual(refreshed["source"]["commit"], self.source_sha)
            self.assertEqual(refreshed["artifact_id"], 50)
            self.run["run_attempt"] = 2
            read.job_read = read.check_read = False
            with self.assertRaises(ValueError):
                common.refresh_document(
                    document, artifact, "a" * 64, current_run, "example/project"
                )

    def test_actual_toolchain_paths_are_bound_to_declared_execution_roles(self):
        self.arguments.toolchain_command = [
            (
                "example-tool",
                json.dumps(
                    [
                        sys.executable,
                        "-c",
                        "import sys; print(sys.argv[1])",
                        str(self.root.resolve()),
                    ]
                ),
            )
        ]
        self.original_execution()
        document = json.loads(
            next(self.directory.glob("*/measurement.json")).read_text()
        )
        toolchain = document["measurement"]["inputs"]["toolchains"]["example-tool"]
        self.assertEqual(toolchain["command"][-1], "{source}")
        self.assertEqual(toolchain["stdout"], "{source}")
        self.assertEqual(common.execute_isolated(self.arguments, read=self.read), 0)
        self.assertEqual((self.root / "count").read_text(), "1")

    def test_changed_resolved_runner_input_executes_stock_command_again(self):
        self.original_execution()
        os.environ["ImageVersion"] = "2"
        self.assertEqual(common.execute_isolated(self.arguments, read=self.read), 0)
        self.assertEqual((self.root / "count").read_text(), "2")
        receipt = json.loads(
            next(self.directory.glob("*/measurement.json")).read_text()
        )
        self.assertTrue(receipt["measurement"]["executed"])
        self.assertIsNone(receipt["origin"])

    def test_full_command_ignores_preexisting_valid_changed_source_bytecode(self):
        module = self.root / "example_cache.py"
        original = "VALUE = 'source'\n"
        module.write_text("VALUE = 'stale-bytecode'\n")
        cache = (
            module.parent
            / "__pycache__"
            / f"{module.stem}.{sys.implementation.cache_tag}.pyc"
        )
        py_compile.compile(
            str(module),
            cfile=str(cache),
            invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH,
        )
        module.write_text(original)
        environment = {
            key: value
            for key, value in os.environ.items()
            if key not in {"PYTHONPYCACHEPREFIX", "PYTHONDONTWRITEBYTECODE"}
        }
        normal = subprocess.check_output(
            [sys.executable, "-c", "import example_cache; print(example_cache.VALUE)"],
            cwd=self.root,
            env=environment,
            text=True,
        ).strip()
        self.assertEqual(normal, "stale-bytecode")
        self.arguments.command = [
            sys.executable,
            "-c",
            "import example_cache; assert example_cache.VALUE == 'source'",
        ]
        # Unknown runtime identity selects the real complete fallback command.
        with patch.dict(os.environ, {"ImageVersion": ""}):
            self.assertEqual(common.execute(self.arguments, read=self.read), 0)
        self.assertEqual(module.read_text(), original)

    def test_missing_runtime_identity_cannot_skip_the_command(self):
        self.original_execution()
        del os.environ["ImageVersion"]
        self.assertEqual(common.execute_isolated(self.arguments, read=self.read), 0)
        self.assertEqual((self.root / "count").read_text(), "2")

    def test_helper_entrypoints_ignore_valid_changed_helper_bytecode(self):
        for entrypoint, imported in (
            ("common_test_result.py", "result_equivalence.py"),
            ("common_test_broker.py", "common_test_result.py"),
            ("common_test_verify.py", "common_test_result.py"),
        ):
            with self.subTest(entrypoint=entrypoint):
                target = self.root / "scripts" / imported
                source = target.read_bytes()
                target.write_text(
                    "raise RuntimeError('stale helper bytecode executed')\n"
                )
                cache = (
                    target.parent
                    / "__pycache__"
                    / f"{target.stem}.{sys.implementation.cache_tag}.pyc"
                )
                py_compile.compile(
                    str(target),
                    cfile=str(cache),
                    invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH,
                )
                target.write_bytes(source)
                shutil.copyfile(
                    SCRIPTS / entrypoint, self.root / "scripts" / entrypoint
                )
                completed = subprocess.run(
                    [sys.executable, str(self.root / "scripts" / entrypoint), "--help"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_central_controller_uses_bound_source_authority_and_immutable_container_identity(
        self,
    ):
        # A source bundle's Git identity differs from the controller's native
        # check SHA. No application token is granted to the common-test process.
        controller_sha = "a" * 40
        self.producer["commit"] = controller_sha
        self.run.update(
            head_sha=controller_sha,
            repository={"id": 10, "full_name": "example/controller"},
        )
        self.job.update(
            head_sha=controller_sha,
            check_run_url="https://api.github.com/repos/example/controller/check-runs/31",
        )
        self.check.update(
            head_sha=controller_sha,
            details_url="https://github.com/example/controller/actions/runs/20/job/30",
        )
        os.environ.update(
            GITHUB_REPOSITORY="example/controller",
            COMMON_TEST_RUNNER_IMAGE="example/runtime@sha256:" + "f" * 64,
        )
        os.environ.pop("ImageVersion", None)
        self.arguments.source_repository = "example/service"
        self.arguments.source_authority = self.root.parent / "authority.json"

        def authority(sha, producer, origins):
            document = {
                "schema": "common-test-source-authority-v1",
                "source_repository_id": 11,
                "consumer": {
                    "source_sha": sha,
                    "source_tree": self.tree,
                    "run_id": producer["run_id"],
                    "run_attempt": producer["run_attempt"],
                    "controller_sha": producer["commit"],
                },
                "origins": origins,
            }
            data = equivalent.encoded(document)
            self.arguments.source_authority.write_bytes(data)
            self.arguments.source_authority_sha256 = hashlib.sha256(data).hexdigest()

        def read(repository, path):
            if repository == "example/service":
                self.fail(
                    "Source application credentials must remain outside the test job"
                )
            self.assertIn(repository, {"example/controller", "example/project"})
            return self.read("example/project", path)

        common.api.side_effect = read
        authority(self.source_sha, self.producer, [])
        self.assertEqual(common.execute_isolated(self.arguments, read=read), 0)
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            for path in self.directory.rglob("*"):
                if path.is_file():
                    archive.writestr(
                        path.relative_to(self.directory).as_posix(), path.read_bytes()
                    )
        self.archive = output.getvalue()
        consumer = {**self.producer, "run_id": 21}
        self.current_producer.return_value = (consumer, self.run)
        authority(
            self.git("rev-parse", "HEAD").strip(),
            consumer,
            [
                {
                    "source_sha": self.source_sha,
                    "source_tree": self.tree,
                    "run_id": 20,
                    "run_attempt": 1,
                    "controller_sha": controller_sha,
                }
            ],
        )
        self.assertEqual(common.execute_isolated(self.arguments, read=read), 0)
        self.assertEqual((self.root / "count").read_text(), "1")
        receipt = json.loads(
            next(self.directory.glob("*/measurement.json")).read_text()
        )
        self.assertFalse(receipt["measurement"]["executed"])
        self.assertEqual(receipt["origin"]["origin"]["source"]["repository_id"], 11)


class WorkflowOutputIsolationTests(unittest.TestCase):
    def test_mock_executions_cannot_change_the_enclosing_workflow_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            output, environment = (Path(directory) / name for name in ("output", "env"))
            marker = b"synthetic_parent_value=untouched\n"
            for path in (output, environment):
                path.write_bytes(marker)
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "unittest",
                    "test_common_test_result.CommonResultIntegrationTests.test_real_command_executes_once_and_new_run_verifies_original_data",
                ],
                cwd=Path(__file__).parent,
                env={
                    **os.environ,
                    "GITHUB_OUTPUT": str(output),
                    "GITHUB_ENV": str(environment),
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            for path in (output, environment):
                self.assertEqual(path.read_bytes(), marker)


class HTTPResponseDiagnosticTests(unittest.TestCase):
    def test_numeric_headers_annotate_the_original_error_without_reading_its_body(self):
        headers = Message()
        values = {
            "X-RateLimit-Limit": "5000",
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Used": "5000",
            "X-RateLimit-Reset": "2000000000",
            "Retry-After": "60",
        }
        for name, value in values.items():
            headers[name.lower()] = value
        body = io.BytesIO(b"synthetic-response-secret")
        error = common.urllib.error.HTTPError(
            "https://example.invalid/hidden", 403, "Forbidden", headers, body
        )
        self.addCleanup(error.close)
        output = io.StringIO()
        request = common.urllib.request.Request("https://example.invalid/hidden")
        with (
            patch.object(common.urllib.request, "urlopen", side_effect=error) as read,
            redirect_stdout(output),
            redirect_stderr(output),
            self.assertRaises(common.urllib.error.HTTPError) as caught,
        ):
            common.json_request(request)
        self.assertIs(caught.exception, error)
        self.assertIs(error.headers, headers)
        self.assertEqual(body.tell(), 0)
        self.assertFalse(body.closed)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(
            error.__notes__,
            [
                "HTTP response numeric metadata: "
                + ", ".join(f"{name}={value}" for name, value in values.items())
            ],
        )
        read.assert_called_once_with(request, timeout=30)

    def test_only_allowlisted_bounded_ascii_decimal_values_reach_exception_notes(self):
        for invalid in ("-1", "1.5", "1e3", " 0 ", "١", "9" * 21, "60\nsecret"):
            with self.subTest(value=invalid):
                headers = Message()
                headers["X-RateLimit-Limit"] = invalid
                headers["X-RateLimit-Remaining"] = "0"
                headers["Retry-After"] = "Wed, 01 Jan 2030 00:00:00 GMT"
                headers["X-GitHub-Request-Id"] = "synthetic-request-id"
                headers["Authorization"] = "synthetic-token"
                error = common.urllib.error.HTTPError(
                    "https://example.invalid/hidden", 403, "Forbidden", headers, None
                )
                self.addCleanup(error.close)
                with (
                    patch.object(common.urllib.request, "urlopen", side_effect=error),
                    self.assertRaises(common.urllib.error.HTTPError),
                ):
                    common.json_request(None)
                self.assertEqual(
                    error.__notes__,
                    ["HTTP response numeric metadata: X-RateLimit-Remaining=0"],
                )

    def test_unhandled_traceback_renders_metadata_without_new_sensitive_output(self):
        error = common.urllib.error.HTTPError(
            "https://example.invalid/synthetic-private-path",
            403,
            "Forbidden",
            {"Retry-After": "1"},
            io.BytesIO(b"synthetic-response-secret"),
        )
        self.addCleanup(error.close)
        error.add_note("existing-note")
        with patch.object(common.urllib.request, "urlopen", side_effect=error):
            try:
                common.json_request(None)
            except common.urllib.error.HTTPError as caught:
                rendered = "".join(traceback.format_exception(caught))
        self.assertIn("HTTP response numeric metadata: Retry-After=1", rendered)
        self.assertEqual(error.__notes__[0], "existing-note")
        self.assertNotIn("synthetic-private-path", rendered)
        self.assertNotIn("synthetic-response-secret", rendered)

    def test_missing_numeric_headers_preserve_an_unannotated_error(self):
        for headers in (None, {}, {"Retry-After": "invalid"}):
            error = common.urllib.error.HTTPError("", 403, "Forbidden", headers, None)
            self.addCleanup(error.close)
            with (
                patch.object(common.urllib.request, "urlopen", side_effect=error),
                self.assertRaises(common.urllib.error.HTTPError) as caught,
            ):
                common.json_request(None)
            self.assertIs(caught.exception, error)
            self.assertFalse(hasattr(error, "__notes__"))

    def test_successful_json_response_remains_unchanged(self):
        with patch.object(
            common.urllib.request, "urlopen", return_value=io.BytesIO(b'{"id": 1}')
        ) as read:
            self.assertEqual(common.json_request(None), {"id": 1})
        read.assert_called_once_with(None, timeout=30)


class PublicPackageReadsTests(unittest.TestCase):
    def test_repository_metadata_url_has_no_trailing_slash(self):
        with (
            patch.dict(os.environ, {"GH_TOKEN": "synthetic-token"}),
            patch.object(common, "json_request", return_value={"id": 10}) as read,
        ):
            self.assertEqual(common.api("example/project", ""), {"id": 10})
            self.assertEqual(
                common.api("example/project", "actions/runs/20"), {"id": 10}
            )
        self.assertEqual(
            [call.args[0].full_url for call in read.call_args_list],
            [
                "https://api.github.com/repos/example/project",
                "https://api.github.com/repos/example/project/actions/runs/20",
            ],
        )

    def test_scoped_installation_fallback_sends_no_private_token_to_public_reads(self):
        common._PUBLIC_METADATA.clear()
        denied = common.urllib.error.HTTPError(
            "https://api.github.com", 403, "not installed", {}, None
        )
        with (
            patch.dict(os.environ, {"GH_TOKEN": "synthetic-private-token"}),
            patch.object(
                common,
                "json_request",
                side_effect=[
                    denied,
                    {"full_name": "EndurantDevs/endurant-ci", "private": False},
                    {"sha": "a" * 40},
                ],
            ) as read,
        ):
            self.assertEqual(
                common.api("EndurantDevs/endurant-ci", "git/trees/" + "a" * 40),
                {"sha": "a" * 40},
            )
        requests = [call.args[0] for call in read.call_args_list]
        self.assertEqual(
            requests[0].get_header("Authorization"), "Bearer synthetic-private-token"
        )
        self.assertTrue(
            all(request.get_header("Authorization") is None for request in requests[1:])
        )
        common._PUBLIC_METADATA.clear()

    def test_anonymous_fallback_cannot_admit_a_private_or_unrelated_repository(self):
        common._PUBLIC_METADATA.clear()
        with (
            patch.object(
                common,
                "json_request",
                return_value={"full_name": "EndurantDevs/endurant-ci", "private": True},
            ),
            self.assertRaises(ValueError),
        ):
            common.public_package_api(
                "EndurantDevs/endurant-ci", "git/trees/" + "a" * 40
            )
        with self.assertRaises(ValueError):
            common.public_package_api("example/project", "git/trees/" + "a" * 40)
        common._PUBLIC_METADATA.clear()


if __name__ == "__main__":
    unittest.main()
