"""Queued aggregate status requires the exact executing image-transfer job."""

from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import source_image as transfer


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
                self.jobs = [deepcopy(self.job)]
                self.assertEqual(transfer.active_publisher(expected)[1]["id"], 777)
                with self.assertRaisesRegex(ValueError, "exact unique"):
                    transfer.active_publisher({**expected, "publisher_job_id": 778})


if __name__ == "__main__":
    unittest.main()
