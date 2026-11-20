"""Queued aggregate status requires the exact executing image-transfer job."""

from copy import deepcopy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import source_image as transfer


def reviewed_environment():
    repo = transfer.github.HEALTHCARE
    return {
        "GITHUB_REPOSITORY": repo, "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF": "refs/heads/dev", "GITHUB_REF_PROTECTED": "true",
        "GITHUB_SHA": "d" * 40, "GITHUB_WORKFLOW_SHA": "d" * 40,
        "GITHUB_WORKFLOW_REF": f"{repo}/.github/workflows/reviewed-source.yml@refs/heads/dev",
        "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "2",
        "REVIEWED_SOURCE_SHA": "a" * 40, "REVIEWED_BASE_SHA": "b" * 40, "REVIEWED_SOURCE_TREE": "c" * 40,
    }


class ReviewedContextChecks(unittest.TestCase):
    def test_source_tuple_requires_protected_core_dispatch_and_all_exact_members(self):
        resolve = transfer.github.reviewed_source
        environment = reviewed_environment()
        self.assertIsNone(resolve({}))
        identity = resolve(environment)
        self.assertEqual(identity["source_sha"], "a" * 40)
        self.assertEqual(identity["base_sha"], "b" * 40)
        self.assertEqual(identity["source_tree"], "c" * 40)
        for change in ({"GITHUB_REPOSITORY": "Example/service"}, {"GITHUB_EVENT_NAME": "pull_request"},
                       {"GITHUB_EVENT_NAME": "push"}, {"GITHUB_REF": "refs/tags/dev"},
                       {"GITHUB_REF_PROTECTED": "false"}, {"REVIEWED_BASE_SHA": ""},
                       {"REVIEWED_SOURCE_SHA": "0" * 40}, {"REVIEWED_SOURCE_TREE": "C" * 40}):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, "protected Healthcare dispatch"):
                resolve({**environment, **change})

    def test_producer_dispatch_coordinates_are_separate_from_measured_checkout(self):
        environment = reviewed_environment()
        producer = transfer.github.reviewed_producer(environment)
        self.assertEqual(producer["run_id"], "123")
        self.assertEqual(producer["run_attempt"], 2)
        self.assertEqual(producer["head_sha"], "d" * 40)
        self.assertEqual(producer["workflow_ref"], environment["GITHUB_WORKFLOW_REF"])
        for change in ({"GITHUB_WORKFLOW_REF": environment["GITHUB_WORKFLOW_REF"].replace("/dev", "/main")},
                       {"GITHUB_WORKFLOW_SHA": "0" * 40}, {"GITHUB_RUN_ATTEMPT": "-1"}):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, "caller workflow and run"):
                transfer.github.reviewed_producer({**environment, **change})

    def test_checkout_requires_an_exact_tree_and_sole_parent(self):
        with tempfile.TemporaryDirectory() as temporary:
            def git(*args):
                return subprocess.check_output(
                    ["git", "-C", temporary, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                     *args], text=True,
                ).strip()

            git("init", "--quiet")
            tree = git("write-tree")
            base = git("commit-tree", tree, "-m", "Base")
            head = git("commit-tree", tree, "-p", base, "-m", "Reviewed source")
            git("update-ref", "HEAD", head)
            verify = transfer.github.verify_reviewed_checkout
            identity = {"source_sha": head, "source_tree": tree, "base_sha": base}
            verify(temporary, identity)
            for change in ({"source_sha": base}, {"base_sha": head}, {"source_tree": "0" * 40}):
                with self.subTest(change=change), self.assertRaisesRegex(ValueError, "exact commit, tree or sole parent"):
                    verify(temporary, {**identity, **change})

            merged = git("commit-tree", tree, "-p", base, "-p", head, "-m", "Merge")
            git("update-ref", "HEAD", merged)
            with self.assertRaisesRegex(ValueError, "sole parent"):
                verify(temporary, {**identity, "source_sha": merged})

    def test_reviewed_export_does_not_enable_existing_publication_context(self):
        environment = {**reviewed_environment(), "CI_REVISION": "e" * 40, "GITHUB_EVENT_PATH": "/unused"}
        with patch.dict(os.environ, environment, clear=True), \
                patch.object(transfer, "command", return_value=b"e" * 40), patch.object(transfer.github, "api") as api:
            self.assertIsNotNone(transfer.github.reviewed_source(os.environ))
            payload = json.dumps({"repository": {"full_name": environment["GITHUB_REPOSITORY"], "private": False}})
            with patch.object(transfer.Path, "read_text", return_value=payload), \
                    self.assertRaisesRegex(ValueError, "accepts only pull_request, push"):
                transfer.context()
            api.assert_not_called()

    def test_reviewed_export_binds_same_tested_image_to_separate_source_and_run(self):
        tested_image = "sha256:" + "a" * 64
        config = "sha256:" + "b" * 64
        with tempfile.TemporaryDirectory() as temporary:
            environment = {**reviewed_environment(), "CI_REVISION": "e" * 40, "RUNNER_TEMP": temporary,
                           "SOURCE_ROOT": temporary}
            label = {"org.endurantdevs.public-ci.repository": environment["GITHUB_REPOSITORY"],
                     "org.endurantdevs.public-ci.run": "123-2"}
            with patch.dict(os.environ, environment, clear=True), \
                    patch.object(transfer, "context") as normal_context, \
                    patch.object(transfer.github, "verify_reviewed_checkout") as verify, \
                    patch.object(transfer, "command", side_effect=[b"e" * 40, b"a" * 40, b"f" * 40]), \
                    patch.object(transfer, "image_identity", return_value={"Id": tested_image, "Config": {"Labels": label}}), \
                    patch.object(transfer.subprocess, "Popen") as save, \
                    patch.object(transfer, "archive_identity", return_value=(config, {tested_image})) as archive:
                process = save.return_value.__enter__.return_value
                process.stdout = io.BytesIO(b"test archive bytes")
                process.wait.return_value = 0
                transfer.export_image(tested_image)
            directory = Path(temporary) / "healthcare-public-image-staging-123-2"
            record = json.loads((directory / "producer.json").read_text())
            self.assertEqual(record["schema"], "reviewed-source-image-staging-v1")
            self.assertEqual(record["image_id"], tested_image)
            self.assertEqual(record["config_digest"], config)
            self.assertEqual(record["archive_sha256"], transfer.file_digest(directory / "image.tar.gz"))
            self.assertEqual(record["source_sha"], "a" * 40)
            self.assertEqual(record["base_sha"], "b" * 40)
            self.assertEqual(record["source_tree"], "c" * 40)
            self.assertEqual(record["source_workflow_blob_sha"], "f" * 40)
            self.assertEqual(record["producer"], transfer.github.reviewed_producer(environment))
            self.assertNotIn("caller_workflow_blob_sha", record)
            normal_context.assert_not_called()
            verify.assert_called_once_with(Path(temporary), transfer.github.reviewed_source(environment))
            save.assert_called_once_with(["docker", "image", "save", tested_image], stdout=subprocess.PIPE)
            archive.assert_called_once()
            self.assertTrue(archive.call_args.kwargs["require_provenance"])


class QueuedContextChecks(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        event = Path(temporary.name) / "event.json"
        event.write_text(json.dumps({"ref": "refs/heads/dev", "after": "a" * 40}))
        self.repository = "EndurantDevs/healthcare-mrf-api"
        self.identity = {"repository": self.repository, "source_sha": "a" * 40, "base_sha": "b" * 40,
                         "source_branch": "dev", "pr_number": "0"}
        self.run = {"id": 123, "run_attempt": 2, "workflow_id": 456, "event": "push",
                    "head_sha": "a" * 40, "head_branch": "dev", "path": ".github/workflows/ci.yml",
                    "status": "queued", "conclusion": None,
                    "repository": {"full_name": self.repository},
                    "head_repository": {"full_name": self.repository}}
        self.job = {"id": 777, "name": "Container build", "run_id": 123, "run_attempt": 2,
                    "head_sha": "a" * 40, "head_branch": "dev", "runner_name": "unit-runner",
                    "status": "in_progress", "conclusion": None,
                    "started_at": "2026-09-09T12:00:00Z", "completed_at": None,
                    "steps": [{"name": "Run complete validation stage", "status": "in_progress", "conclusion": None}]}
        self.jobs = [deepcopy(self.job)]
        environment = {"GITHUB_REPOSITORY": self.repository, "CI_REVISION": "d" * 40,
                       "GITHUB_EVENT_NAME": "push", "GITHUB_EVENT_PATH": str(event),
                       "GITHUB_SHA": "a" * 40, "GITHUB_REF": "refs/heads/dev",
                       "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "2",
                       "GITHUB_JOB": "container-package", "RUNNER_NAME": "unit-runner"}
        for mock in (patch.dict(os.environ, environment),
                     patch.object(transfer, "command", return_value=b"d" * 40),
                     patch.object(transfer.github, "resolve_identity", side_effect=lambda *args: self.identity)):
            mock.start()
            self.addCleanup(mock.stop)
        self.api_calls = []
        mock = patch.object(transfer.github, "api", side_effect=self.api)
        mock.start()
        self.addCleanup(mock.stop)
        mock = patch.object(transfer.github, "pages", side_effect=lambda *args: deepcopy(self.jobs))
        self.pages = mock.start()
        self.addCleanup(mock.stop)
        mock = patch.object(transfer.time, "sleep")
        self.sleep = mock.start()
        self.addCleanup(mock.stop)

    def api(self, repository, path):
        self.assertEqual(repository, self.repository)
        self.api_calls.append(path)
        if path == "actions/runs/123":
            return deepcopy(self.run)
        if path == "actions/jobs/777":
            return deepcopy(self.job)
        raise AssertionError(path)

    def test_queued_matrix_requires_own_core_producer_among_active_siblings(self):
        self.jobs += [{**self.job, "id": 1000 + index, "name": f"Database tests ({index})"}
                      for index in range(60)]
        identity, run = transfer.context(producer=True)
        self.assertEqual(identity["source_sha"], "a" * 40)
        self.assertEqual(run["status"], "queued")
        self.pages.assert_called_once_with(self.repository, "actions/runs/123/attempts/2/jobs", "jobs")
        self.assertEqual(self.api_calls, ["actions/runs/123", "actions/jobs/777"])

    def test_queued_drug_producer_uses_its_own_role_and_step(self):
        self.repository = "EndurantDevs/drug-api"
        os.environ.update(GITHUB_REPOSITORY=self.repository, GITHUB_JOB="validate")
        self.identity["repository"] = self.repository
        self.run.update(repository={"full_name": self.repository}, head_repository={"full_name": self.repository})
        self.job.update(name="Tests and build", steps=[{"name": "Run complete public source validation",
                                                       "status": "in_progress", "conclusion": None}])
        self.jobs = [deepcopy(self.job)]
        self.assertEqual(transfer.context(producer=True)[0]["repository"], self.repository)

    def test_ordinary_active_run_keeps_existing_behavior(self):
        self.run["status"] = "in_progress"
        os.environ.update(GITHUB_JOB="unrelated", RUNNER_NAME="")
        transfer.context()
        self.pages.assert_not_called()
        self.assertEqual(self.api_calls, ["actions/runs/123"])

    def test_immutable_mismatches_and_terminal_states_never_use_job_fallback(self):
        original = deepcopy(self.run)
        changes = [{"id": 124}, {"run_attempt": 1}, {"workflow_id": 0}, {"event": "pull_request"},
                   {"head_sha": "c" * 40}, {"head_branch": "main"}, {"path": ".github/workflows/other.yml"},
                   {"repository": {"full_name": "Other/service"}}, {"head_repository": {"full_name": "Other/service"}},
                   {"conclusion": "cancelled"}, *({"status": state} for state in ("completed", "waiting", "pending", None))]
        for change in changes:
            with self.subTest(change=change):
                self.run = {**original, **change}
                with self.assertRaises(ValueError):
                    transfer.context(producer=True)
                self.pages.assert_not_called()

    def test_missing_or_substituted_own_role_and_runner_fail_closed(self):
        for change in ({"GITHUB_JOB": "python-tests"}, {"RUNNER_NAME": ""}, {"RUNNER_NAME": "other-runner"}):
            with self.subTest(change=change), patch.dict(os.environ, change), self.assertRaises(ValueError):
                transfer.context(producer=True)

    def test_missing_duplicate_stale_or_foreign_job_inventory_is_refused(self):
        original = deepcopy(self.job)
        inventories = [[], [original, original], [{**original, "name": "Database tests"}],
                       [{**original, "run_attempt": 1}], [{**original, "head_sha": "c" * 40}],
                       [{**original, "run_id": 124}], [original, {**original, "id": 778}]]
        for jobs in inventories:
            with self.subTest(jobs=jobs):
                self.jobs = jobs
                with self.assertRaises(ValueError):
                    transfer.context(producer=True)

    def test_fresh_own_job_requires_exact_identity_runner_and_active_trusted_step(self):
        original = deepcopy(self.job)
        changes = [{"id": 778}, {"run_id": 124}, {"run_attempt": 1}, {"run_attempt": True},
                   {"head_sha": "c" * 40}, {"head_branch": "main"}, {"name": "Database tests"},
                   {"runner_name": "other-runner"}, {"status": "queued"}, {"status": "completed"},
                   {"conclusion": "success"}, {"started_at": None}, {"completed_at": "2026-09-09T12:01:00Z"},
                   {"steps": []}, {"steps": original["steps"] * 2},
                   {"steps": [{"name": "Other step", "status": "in_progress", "conclusion": None}]},
                   {"steps": [{**original["steps"][0], "conclusion": "failure"}]}]
        for change in changes:
            with self.subTest(change=change):
                self.job = {**original, **change}
                with self.assertRaises(ValueError):
                    transfer.context(producer=True)

    def test_incomplete_inventory_and_job_read_errors_do_not_authorize_export(self):
        with patch.object(transfer.github, "pages", side_effect=ValueError("incomplete inventory")):
            with self.assertRaisesRegex(ValueError, "incomplete inventory"):
                transfer.context(producer=True)
        with patch.object(transfer.github, "api", side_effect=[self.run, ValueError("job unavailable")]):
            with self.assertRaisesRegex(ValueError, "job unavailable"):
                transfer.context(producer=True)

    def test_boolean_cannot_substitute_the_first_producer_attempt(self):
        os.environ["GITHUB_RUN_ATTEMPT"] = "1"
        self.run["run_attempt"] = 1
        self.jobs[0]["run_attempt"] = 1
        self.job["run_attempt"] = True
        with self.assertRaises(ValueError):
            transfer.context(producer=True)

    def test_incomplete_step_readback_settles_without_reselecting_the_job(self):
        for stale in ({"steps": []}, {"steps": self.job["steps"] * 2}, {"started_at": None}):
            with self.subTest(stale=stale):
                self.sleep.reset_mock()
                jobs = iter([{**self.job, **stale}, deepcopy(self.job)])
                with patch.object(transfer.github, "api", side_effect=lambda repository, path:
                                  next(jobs) if path == "actions/jobs/777" else deepcopy(self.run)):
                    self.assertEqual(transfer.context(producer=True)[0],
                                     {**self.identity, "run_id": "123", "run_attempt": 2, "ci_revision": "d" * 40})
                self.sleep.assert_called_once_with(2)
        self.assertEqual(self.pages.call_count, 3)

    def test_missing_step_readback_has_a_fixed_bound(self):
        self.job["steps"] = []
        with self.assertRaisesRegex(ValueError, "did not settle"):
            transfer.context(producer=True)
        self.assertEqual(self.api_calls.count("actions/jobs/777"), 10)
        self.assertEqual(self.api_calls.count("actions/runs/123"), 10)
        self.assertEqual(self.sleep.call_count, 9)
        self.pages.assert_called_once()

    def test_readback_refuses_run_job_or_step_drift_before_authorizing(self):
        changes = [("run", {"run_attempt": 3}), ("run", {"id": True}),
                   ("run", {"head_sha": "c" * 40}), ("run", {"head_branch": "main"}),
                   ("run", {"status": "completed"}), ("run", {"conclusion": "cancelled"}),
                   ("job", {"id": 778}), ("job", {"run_attempt": True}),
                   ("job", {"runner_name": "other-runner"}), ("job", {"status": "completed"}),
                   ("job", {"conclusion": "failure"}),
                   ("job", {"steps": [{"name": "Other step", "status": "in_progress", "conclusion": None}]})]
        for target, change in changes:
            with self.subTest(target=target, change=change):
                self.sleep.reset_mock()
                reads = {"run": 0, "job": 0}

                def api(repository, path):
                    kind = "job" if path == "actions/jobs/777" else "run"
                    reads[kind] += 1
                    value = deepcopy(self.job if kind == "job" else self.run)
                    if kind == "job" and reads[kind] == 1:
                        value["steps"] = []
                    elif kind == target and reads[kind] > 1:
                        value.update(change)
                    return value

                with patch.object(transfer.github, "api", side_effect=api), self.assertRaises(ValueError):
                    transfer.context(producer=True)
                self.assertLessEqual(self.sleep.call_count, 1)

    def test_queued_pr_noop_refreshes_stale_inventory_without_publishing(self):
        os.environ["GITHUB_JOB"] = "dev-image-publication"
        self.run["event"] = "pull_request"
        self.job.update(name=transfer.JOB, steps=[{"name": "Authenticate DEV image input",
                                                 "status": "in_progress", "conclusion": None}])
        self.jobs = [{**self.job, "status": "queued", "started_at": None, "steps": []}]
        with patch.object(transfer, "context", return_value=(self.identity, self.run)):
            self.assertIsNone(transfer.admit())
            with patch.object(transfer, "queued_transfer_job", return_value={**self.job, "id": 778}):
                with self.assertRaisesRegex(ValueError, "job changed"):
                    transfer.admit()

    def test_queued_publisher_and_failure_reconciliation_keep_exact_job_id(self):
        os.environ["GITHUB_JOB"] = "dev-image-publication"
        self.job["name"] = transfer.JOB
        expected = {"identity": {**self.identity, "run_id": "123", "run_attempt": 2, "ci_revision": "d" * 40},
                    "workflow_id": 456, "publisher_job_id": 777,
                    "image": transfer.IMAGES[self.repository] + ":dev-main-aaaaaaaa-20260909120000"}
        for step in ("Authenticate DEV image input", "Stage DEV image publication intent",
                     "Publish validated DEV image", "Reconcile DEV image publication"):
            with self.subTest(step=step):
                self.job["steps"] = [{"name": "Earlier step", "status": "completed", "conclusion": "failure"},
                                     {"name": step, "status": "in_progress", "conclusion": None}]
                self.jobs = [{**self.job, "status": "queued", "started_at": None, "steps": []}]
                self.assertEqual(transfer.active_publisher(expected)[1]["id"], 777)
                with self.assertRaisesRegex(ValueError, "exact unique"):
                    transfer.active_publisher({**expected, "publisher_job_id": 778})


if __name__ == "__main__":
    unittest.main()
