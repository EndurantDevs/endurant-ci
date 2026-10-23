"""Keep asynchronous artifact cleanup bound to its completed source and trusted run."""

import io
import json
import sys
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import artifact_cleanup as cleanup


REPOSITORY = "EndurantDevs/healthcare-mrf-api"
GATES = ("portable import checks", "Coverage results", "Validation complete",
         "Container build", "DEV image publication")


def source_run():
    return {"id": 123, "run_attempt": 2, "workflow_id": 456, "name": "CI", "display_title": "CI",
            "path": ".github/workflows/ci.yml", "event": "pull_request", "head_sha": "a" * 40,
            "head_branch": "fix/example", "status": "completed", "conclusion": "success",
            "repository": {"full_name": REPOSITORY}, "head_repository": {"full_name": REPOSITORY}}


def cleanup_run():
    return {"id": 789, "run_attempt": 1, "workflow_id": 987,
            "path": ".github/workflows/artifact-cleanup.yml", "event": "workflow_dispatch",
            "head_sha": "b" * 40, "head_branch": "dev", "status": "in_progress", "conclusion": None,
            "repository": {"full_name": REPOSITORY}, "head_repository": {"full_name": REPOSITORY}}


def expected_run():
    return {**source_run(), "completed": True, "cleanup_run": cleanup_run()}


def successful_jobs():
    return [{"id": index + 1, "name": name, "run_id": 123, "run_attempt": 2,
             "head_sha": "a" * 40, "status": "completed", "conclusion": "success"}
            for index, name in enumerate(GATES)]


class CompletedCleanupEntrypointChecks(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.event = Path(self.temporary.name) / "event.json"
        self.payload = {"repository": {"full_name": REPOSITORY, "private": False},
                        "inputs": {"source_run_id": "123", "source_run_attempt": "2"}}
        self.environment = {
            "GITHUB_EVENT_PATH": str(self.event), "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_REPOSITORY": REPOSITORY, "GITHUB_JOB": "completed-cleanup",
            "GITHUB_REF": "refs/heads/dev", "GITHUB_SHA": "b" * 40,
            "GITHUB_RUN_ID": "789", "GITHUB_RUN_ATTEMPT": "1",
            "GITHUB_WORKFLOW_REF": f"{REPOSITORY}/.github/workflows/artifact-cleanup.yml@refs/heads/dev",
        }

    def invoke(self, payload=None, environment=None, sources=None):
        self.event.write_text(json.dumps(self.payload if payload is None else payload))
        source_responses = iter([source_run()] if sources is None else sources)
        source = None

        def api(repository, path, method="GET"):
            nonlocal source
            self.assertEqual(repository, REPOSITORY)
            self.assertEqual(method, "GET")
            if path == "actions/runs/123":
                source = next(source_responses, source)
                return deepcopy(source)
            if path == "actions/runs/789":
                return cleanup_run()
            raise AssertionError(f"Unexpected artifact access: {path}")

        with patch.dict("os.environ", self.environment if environment is None else environment), \
                patch.object(cleanup.github, "api", side_effect=api) as request, \
                patch.object(cleanup.time, "sleep") as sleep, \
                patch.object(cleanup, "cleanup") as execute, patch("sys.stdout", new=io.StringIO()):
            self.request, self.sleep, self.execute = request, sleep, execute
            cleanup.completed_main()
        return execute

    def test_dispatch_binds_original_source_and_separate_cleanup_execution(self):
        execute = self.invoke()
        execute.assert_called_once()
        repository, expected = execute.call_args.args
        self.assertEqual(repository, REPOSITORY)
        self.assertIs(expected["completed"], True)
        for field in (*cleanup.RUN_FIELDS, "workflow_id"):
            self.assertEqual(expected[field], source_run()[field])
        for field in cleanup.RUN_FIELDS:
            self.assertEqual(expected["cleanup_run"][field], cleanup_run()[field])
        self.assertNotEqual(expected["id"], expected["cleanup_run"]["id"])

    def test_failed_cancelled_fork_and_metadata_runs_do_not_enter_cleanup(self):
        changes = ({"conclusion": "failure"}, {"conclusion": "cancelled"},
                   {"head_repository": {"full_name": "Other/example"}},
                   {"display_title": "CI metadata update"})
        for change in changes:
            with self.subTest(change=change):
                self.invoke(sources=[{**source_run(), **change}]).assert_not_called()
                self.sleep.assert_not_called()
                self.assertEqual(self.request.call_count, 1)

    def test_entrypoint_rejects_another_job_branch_event_or_workflow(self):
        changes = ({"GITHUB_JOB": "stale-cleanup"}, {"GITHUB_REF": "refs/heads/main"},
                   {"GITHUB_EVENT_NAME": "workflow_run"}, {"GITHUB_REPOSITORY": "Other/example"},
                   {"GITHUB_WORKFLOW_REF": f"{REPOSITORY}/.github/workflows/ci.yml@refs/heads/dev"},
                   {"GITHUB_WORKFLOW_REF": f"{REPOSITORY}/.github/workflows/artifact-cleanup.yml@refs/heads/main"})
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.invoke(environment={**self.environment, **change})
        for repository in ({"full_name": REPOSITORY, "private": True},
                           {"full_name": "Other/example", "private": False}):
            with self.subTest(repository=repository), self.assertRaises(ValueError):
                self.invoke({**self.payload, "repository": repository})

    def test_inputs_require_positive_decimal_run_and_attempt_strings_before_api_access(self):
        for field in ("source_run_id", "source_run_attempt"):
            for value in ("", "0", "-1", "01", "1.0", " 123", "123/attempts/2", None, True, 123, [], {}):
                payload = deepcopy(self.payload)
                payload["inputs"][field] = value
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    self.invoke(payload)
                self.request.assert_not_called()
                self.execute.assert_not_called()
        for inputs in (None, [], "123", {}, {"source_run_id": "123"}):
            with self.subTest(inputs=inputs), self.assertRaises(ValueError):
                self.invoke({**self.payload, "inputs": inputs})
            self.request.assert_not_called()
            self.execute.assert_not_called()

    def test_dispatch_waits_for_queued_and_running_source_then_uses_completed_evidence(self):
        sources = [{**source_run(), "status": status, "conclusion": None}
                   for status in ("queued", "in_progress")]
        execute = self.invoke(sources=[*sources, source_run()])
        execute.assert_called_once()
        self.assertEqual(self.sleep.call_args_list, [unittest.mock.call(2), unittest.mock.call(2)])
        self.assertEqual([call.args[1] for call in self.request.call_args_list[:3]],
                         ["actions/runs/123"] * 3)

    def test_source_wait_is_bounded_and_never_starts_cleanup_on_timeout(self):
        with self.assertRaisesRegex(ValueError, "did not finish"):
            self.invoke(sources=[{**source_run(), "status": "in_progress", "conclusion": None}])
        self.assertEqual(self.request.call_count, 60)
        self.assertEqual(self.sleep.call_count, 59)
        self.assertTrue(all(call.args == (2,) for call in self.sleep.call_args_list))
        self.execute.assert_not_called()

    def test_source_rerun_or_replaced_run_during_wait_rejects_cleanup(self):
        initial = {**source_run(), "status": "in_progress", "conclusion": None}
        for change in ({"run_attempt": 3}, {"id": 124}):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, "run or attempt changed"):
                self.invoke(sources=[initial, {**source_run(), **change}])
            self.assertEqual(self.request.call_count, 2)
            self.sleep.assert_called_once_with(2)
            self.execute.assert_not_called()

    def test_unexpected_source_status_rejects_cleanup_without_waiting(self):
        for status in (None, "waiting", "pending", "unknown"):
            with self.subTest(status=status), self.assertRaisesRegex(ValueError, "unexpected status"):
                self.invoke(sources=[{**source_run(), "status": status}])
            self.sleep.assert_not_called()
            self.execute.assert_not_called()


class CompletedCleanupRunChecks(unittest.TestCase):
    def test_completed_source_authenticates_the_current_cleanup_execution(self):
        with patch.object(cleanup.github, "api", return_value=cleanup_run()) as api:
            self.assertTrue(cleanup.current_run(source_run(), expected_run(), REPOSITORY))
        api.assert_called_once_with(REPOSITORY, "actions/runs/789")

    def test_source_rerun_restart_or_changed_identity_rejects_cleanup(self):
        changes = ({"id": 124}, {"run_attempt": 3}, {"workflow_id": 999},
                   {"status": "in_progress", "conclusion": None}, {"conclusion": "failure"},
                   {"head_sha": "c" * 40}, {"head_branch": "other"}, {"event": "workflow_dispatch"},
                   {"path": ".github/workflows/other.yml"},
                   {"repository": {"full_name": "Other/example"}},
                   {"head_repository": {"full_name": "Other/example"}})
        for change in changes:
            with self.subTest(change=change), patch.object(cleanup.github, "api", return_value=cleanup_run()):
                self.assertFalse(cleanup.current_run({**source_run(), **change}, expected_run(), REPOSITORY))

    def test_cleanup_rerun_restart_or_changed_identity_rejects_deletion(self):
        changes = ({"id": 790}, {"run_attempt": 2}, {"workflow_id": 988},
                   {"status": "completed", "conclusion": "success"},
                   {"status": "queued"}, {"head_sha": "c" * 40}, {"head_branch": "main"},
                   {"event": "workflow_run"}, {"path": ".github/workflows/ci.yml"},
                   {"repository": {"full_name": "Other/example"}},
                   {"head_repository": {"full_name": "Other/example"}})
        for change in changes:
            with self.subTest(change=change), patch.object(
                    cleanup.github, "api", return_value={**cleanup_run(), **change}):
                self.assertFalse(cleanup.current_run(source_run(), expected_run(), REPOSITORY))

    def test_failed_run_or_job_evidence_never_reaches_artifacts_or_deletion(self):
        for changed_source, rows in (({"run_attempt": 3}, successful_jobs()),
                                     ({}, successful_jobs()[:-1]),
                                     ({}, [{**job, "conclusion": "failure"} for job in successful_jobs()])):
            calls = []

            def api(repository, path, method="GET"):
                calls.append((path, method))
                if path == "actions/runs/123":
                    return {**source_run(), **changed_source}
                if path == "actions/runs/789":
                    return cleanup_run()
                raise AssertionError(f"Unexpected artifact access: {path}")

            with self.subTest(source=changed_source, jobs=rows), patch.object(
                    cleanup.github, "api", side_effect=api), patch.object(
                    cleanup, "effective_jobs", return_value=rows), self.assertRaises(ValueError):
                cleanup.cleanup(REPOSITORY, expected_run())
            self.assertTrue(calls)
            self.assertTrue(all(method == "GET" and "/artifacts" not in path for path, method in calls))


class CompletedCleanupConsumerChecks(unittest.TestCase):
    def consumers(self, rows):
        with patch.object(cleanup.github, "pages", return_value=deepcopy(rows)) as pages:
            result = cleanup.completed_consumers(REPOSITORY, expected_run())
        pages.assert_called_once_with(REPOSITORY, "actions/runs/123/jobs?filter=latest", "jobs")
        return result

    def test_complete_successful_jobs_accept_authenticated_prior_producer_attempts(self):
        rows = successful_jobs()
        rows[0]["run_attempt"] = 1
        self.assertEqual(self.consumers(rows), sorted(
            (row["id"], row["name"], row["run_attempt"], "success") for row in rows))

    def test_each_required_consumer_must_exist_and_succeed(self):
        for index, name in enumerate(GATES):
            with self.subTest(missing=name), self.assertRaises(ValueError):
                self.consumers([row for row in successful_jobs() if row["name"] != name])
            for conclusion in (None, "failure", "cancelled", "skipped"):
                rows = successful_jobs()
                rows[index]["conclusion"] = conclusion
                with self.subTest(name=name, conclusion=conclusion), self.assertRaises(ValueError):
                    self.consumers(rows)

    def test_extra_jobs_must_finish_and_job_provenance_must_remain_unambiguous(self):
        changes = ({"id": 1}, {"name": GATES[0]}, {"run_id": 999}, {"run_attempt": 0},
                   {"run_attempt": 3}, {"run_attempt": 1.0}, {"head_sha": "c" * 40},
                   {"status": "queued", "conclusion": None},
                   {"status": "in_progress", "conclusion": None}, {"conclusion": "skipped"})
        extra = {**successful_jobs()[0], "id": 99, "name": "Additional validation"}
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.consumers([*successful_jobs(), {**extra, **change}])


class CompletedCleanupArtifactChecks(unittest.TestCase):
    def setUp(self):
        self.expected = expected_run()
        self.jobs = successful_jobs()
        self.created = datetime.now(timezone.utc) - timedelta(hours=1)
        self.measurement = self.upload(55, "measurement", "Coverage results", "Upload public measurement", 1)

    def upload(self, identifier, prefix, job_name, step_name, attempt):
        job = next(job for job in self.jobs if job["name"] == job_name)
        job["run_attempt"] = attempt
        job["steps"] = [{"name": step_name, "status": "completed", "conclusion": "success",
                         "started_at": (self.created - timedelta(seconds=1)).isoformat(),
                         "completed_at": (self.created + timedelta(seconds=1)).isoformat()}]
        return {"id": identifier, "name": f"healthcare-public-{prefix}-123-{attempt}",
                "expired": False, "size_in_bytes": 100, "created_at": self.created.isoformat(),
                "expires_at": (self.created + timedelta(days=90)).isoformat(),
                "workflow_run": {"id": 123, "head_sha": "a" * 40}}

    def discover(self, artifacts, record=None):
        with patch.object(cleanup.github, "pages", return_value=deepcopy(self.jobs)), \
                patch.object(cleanup, "publication_record", return_value=record):
            return cleanup.completed_artifact_ids(REPOSITORY, self.expected, artifacts)

    def test_measurement_uses_effective_producer_attempt_and_ignores_unknown_artifacts(self):
        other = {**self.measurement, "id": 99, "name": "unrelated-evidence"}
        self.assertEqual(self.discover([other, self.measurement]), {"measurement_artifact_id": 55})

    def test_carried_job_alias_uses_original_upload_window_instead_of_new_attempt_label(self):
        measurement_job = next(job for job in self.jobs if job["name"] == "Coverage results")
        original = deepcopy(measurement_job)
        measurement_job["run_attempt"] = 2
        with patch.object(cleanup, "attempt_jobs", return_value=[original]) as jobs:
            self.assertEqual(self.discover([self.measurement]), {"measurement_artifact_id": 55})
        jobs.assert_called_once_with(REPOSITORY, self.expected, 1)
        original["steps"][0]["completed_at"] = (self.created + timedelta(seconds=2)).isoformat()
        with patch.object(cleanup, "attempt_jobs", return_value=[original]), self.assertRaisesRegex(
                ValueError, "original successful execution"):
            self.discover([self.measurement])
        conflicting = {**self.measurement, "id": 56, "name": "healthcare-public-measurement-123-2"}
        with self.assertRaisesRegex(ValueError, "exact successful artifact upload"):
            self.discover([self.measurement, conflicting])

    def test_measurement_requires_unique_name_successful_upload_and_matching_provenance(self):
        for artifacts in ([], [self.measurement, {**self.measurement, "id": 56}],
                          [{**self.measurement, "workflow_run": {"id": 999, "head_sha": "a" * 40}}],
                          [{**self.measurement, "created_at": (self.created - timedelta(minutes=1)).isoformat()}]):
            with self.subTest(artifacts=artifacts), self.assertRaises(ValueError):
                self.discover(artifacts)
        for change in ({"status": "in_progress"}, {"conclusion": "failure"}, {"steps": []}):
            rows = deepcopy(self.jobs)
            measurement_job = next(job for job in self.jobs if job["name"] == "Coverage results")
            if "steps" in change:
                measurement_job.update(change)
            else:
                measurement_job["steps"][0].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.discover([self.measurement])
            self.jobs = rows

    def test_dev_receipt_binds_tested_image_and_reused_measurement_attempt(self):
        self.expected.update(event="push", head_branch="dev")
        receipt = self.upload(66, "image", "DEV image publication", "Upload DEV image receipt", 2)
        record = {"producer_artifact": {"id": 44}, "measurement_run_attempt": 1}
        self.assertEqual(self.discover([self.measurement, receipt], record), {
            "measurement_artifact_id": 55, "image_receipt_artifact_id": 66, "image_artifact_id": 44})
        for invalid in (None, {}, {**record, "measurement_run_attempt": 2},
                        {**record, "producer_artifact": {"id": True}},
                        {**record, "producer_artifact": {"id": -1}}):
            with self.subTest(record=invalid), self.assertRaises(ValueError):
                self.discover([self.measurement, receipt], invalid)
        for receipts in ([], [receipt, {**receipt, "id": 67}]):
            with self.subTest(receipts=receipts), self.assertRaises(ValueError):
                self.discover([self.measurement, *receipts], record)

    def test_completed_engine_deletes_only_consumed_intermediates_and_stops_on_rerun(self):
        for restart in (False, True):
            deleted, source_reads = [], 0
            temporary = {**self.measurement, "id": 44, "name": "mrf-rust-coverage-123-1"}
            unknown = {**self.measurement, "id": 99, "name": "unrelated-evidence"}
            items = [temporary, self.measurement, unknown]

            def api(repository, path, method="GET"):
                nonlocal source_reads
                self.assertEqual(repository, REPOSITORY)
                if method == "DELETE":
                    deleted.append(int(path.rsplit("/", 1)[1]))
                    return None
                if path == "actions/runs/123":
                    source_reads += 1
                    return {**source_run(), **({"run_attempt": 3} if restart and source_reads > 4 else {})}
                if path == "actions/runs/789":
                    return cleanup_run()
                return deepcopy(next(item for item in items if path == f"actions/artifacts/{item['id']}"))

            def pages(repository, path, key):
                self.assertEqual(repository, REPOSITORY)
                return deepcopy(self.jobs if key == "jobs" else items)

            with self.subTest(restart=restart), patch.object(cleanup.github, "api", side_effect=api), \
                    patch.object(cleanup.github, "pages", side_effect=pages), \
                    patch.object(cleanup.time, "sleep"), patch("sys.stdout", new=io.StringIO()):
                if restart:
                    with self.assertRaisesRegex(ValueError, "producing run changed"):
                        cleanup.cleanup(REPOSITORY, self.expected)
                    self.assertEqual(deleted, [])
                else:
                    self.assertEqual(cleanup.cleanup(REPOSITORY, self.expected), 1)
                    self.assertEqual(deleted, [44])


if __name__ == "__main__":
    unittest.main()
