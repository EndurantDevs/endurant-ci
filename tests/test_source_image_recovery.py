"""Retained images preserve their original validation and separate publisher authority."""

import base64
import hashlib
import json
import os
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import source_image as transfer
import source_image_recovery as recovery
import test_source_image as images


class RecoveryChecks(unittest.TestCase):
    def setUp(self):
        self.repository = recovery.REPOSITORY
        self.source = {"id": 123, "run_attempt": 2, "workflow_id": 456, "event": "push",
                       "head_sha": "a" * 40, "head_branch": "dev", "path": ".github/workflows/ci.yml",
                       "status": "completed", "conclusion": "failure",
                       "created_at": "2026-09-09T11:00:00Z", "updated_at": "2026-09-09T11:30:00Z",
                       "repository": {"full_name": self.repository}, "head_repository": {"full_name": self.repository}}
        self.commit = {"sha": "a" * 40, "parents": [{"sha": "b" * 40}],
                       "commit": {"committer": {"date": "2026-09-09T11:00:00Z"}}}
        self.run = {**self.source, "id": 900, "run_attempt": 1, "workflow_id": 789,
                    "event": "workflow_dispatch", "head_sha": "c" * 40, "path": recovery.WORKFLOW,
                    "status": "in_progress", "conclusion": None, "name": recovery.TITLE,
                    "display_title": "Retained DEV image 123-2", "created_at": "2026-09-09T12:00:00Z"}
        self.job = {"id": 901, "name": transfer.JOB, "run_id": 900, "run_attempt": 1,
                    "head_sha": "c" * 40, "status": "in_progress", "conclusion": None,
                    "started_at": "2026-09-09T12:00:00Z"}
        self.publisher = {"repository": self.repository, "run_id": "900", "run_attempt": 1,
                          "source_sha": "c" * 40, "workflow_blob": "e" * 40, "ci_revision": "f" * 40,
                          "workflow_id": 789, "job_id": 901}
        self.identity = {"repository": self.repository, "source_sha": "a" * 40, "base_sha": "b" * 40,
                         "source_branch": "dev", "pr_number": "0", "run_id": "123", "run_attempt": 2,
                         "ci_revision": next(iter(recovery.SOURCE_RECIPES))[1],
                         "caller_workflow_blob_sha": next(iter(recovery.SOURCE_RECIPES))[0]}
        _, self.jobs, self.artifacts = images.AdmissionChecks().fixtures()
        self.jobs[0]["name"] = "Container build"
        self.jobs.insert(-1, {**self.jobs[2], "id": 8, "name": "Validation complete"})
        self.jobs[-1].update(status="completed", conclusion="failure", steps=[
            {"name": "Authenticate DEV image input", "status": "completed", "conclusion": "failure"},
            *({"name": name, "status": "completed", "conclusion": "skipped"} for name in recovery.POST_AUTH_STEPS)])
        self.artifacts[0]["name"] = "healthcare-public-image-staging-123-2"
        self.artifacts[1]["name"] = "healthcare-public-measurement-123-2"
        self.inputs = {"source_run_id": "123", "source_run_attempt": "2", "image_artifact_id": "44",
                       "measurement_artifact_id": "55"}
        self.environment = {"GITHUB_REPOSITORY": self.repository, "CI_REVISION": "f" * 40,
                            "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/heads/dev",
                            "GITHUB_JOB": "dev-image-publication", "GITHUB_RUN_ID": "900", "GITHUB_RUN_ATTEMPT": "1",
                            "GITHUB_SHA": "c" * 40,
                            "GITHUB_WORKFLOW_REF": f"{self.repository}/{recovery.WORKFLOW}@refs/heads/dev",
                            **{recovery.INPUTS[key]: value for key, value in self.inputs.items()}}

    def publisher_context(self, change=None, environment=None, inputs=None):
        with tempfile.TemporaryDirectory() as temporary:
            event = Path(temporary) / "event.json"
            event.write_text(json.dumps({"inputs": self.inputs if inputs is None else inputs}))
            with patch.dict(os.environ, {**self.environment, "GITHUB_EVENT_PATH": str(event), **(environment or {})}), \
                    patch.object(transfer, "command", return_value=b"f" * 40), \
                    patch.object(transfer.github, "api", return_value={**self.run, **(change or {})}), \
                    patch.object(transfer.github, "pages", return_value=[self.job]), \
                    patch.object(recovery, "recipe", return_value=("e" * 40, "f" * 40)):
                return recovery.publisher_context()

    def original_context(self, run=None, jobs=None, recipe=None):
        def api(repository, path):
            self.assertEqual(repository, self.repository)
            if path == "actions/runs/123":
                return self.source if run is None else run
            if path == "commits/" + "a" * 40:
                return self.commit
            raise AssertionError(path)

        def pages(repository, path, key=None):
            if path.startswith("actions/workflows/456/runs?"):
                return [self.source]
            if path == "commits/" + "a" * 40 + "/pulls":
                return []
            self.fail("unexpected original source lookup")

        with patch.dict(os.environ, self.environment), patch.object(transfer.github, "api", side_effect=api), \
                patch.object(transfer.github, "pages", side_effect=pages), \
                patch.object(recovery, "recipe", return_value=recipe or next(iter(recovery.SOURCE_RECIPES))), \
                patch.object(transfer, "require_dev_ancestor") as ancestor, \
                patch.object(transfer.artifacts, "effective_jobs", return_value=self.jobs if jobs is None else jobs):
            result = recovery.original_context()
            ancestor.assert_called_once_with(self.repository, "a" * 40)
            return result

    def test_manual_publisher_has_its_own_run_source_and_package(self):
        self.assertEqual(self.publisher_context(), (self.publisher, self.run, self.job))

    def test_manual_publisher_rejects_changed_source_attempt_or_workflow(self):
        for change in ({"run_attempt": 2}, {"head_sha": "b" * 40}, {"path": ".github/workflows/ci.yml"},
                       {"event": "push"}, {"head_branch": "main"}, {"status": "completed"},
                       {"conclusion": "failure"}, {"display_title": "Retained DEV image 124-2"},
                       {"head_repository": {"full_name": "Other/example"}}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.publisher_context(change=change)
        for environment in ({"GITHUB_REF": "refs/heads/main"}, {"SOURCE_RUN_ID": "124"},
                            {"GITHUB_JOB": "other"}, {"GITHUB_WORKFLOW_REF": "other"}):
            with self.subTest(environment=environment), self.assertRaises(ValueError):
                self.publisher_context(environment=environment)
        for value in ("0", "-1", "1; echo bad", 123):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.publisher_context(inputs={**self.inputs, "source_run_id": value})

    def test_original_failed_run_keeps_its_exact_identity(self):
        identity, run, _ = self.original_context()
        self.assertEqual(identity, self.identity)
        self.assertEqual(run, self.source)
        self.assertEqual(run["conclusion"], "failure")

    def test_original_identity_reuses_the_authenticated_two_commit_dev_range(self):
        base, reviewed, tree = "1" * 40, "2" * 40, "3" * 40
        commit = {**self.commit, "commit": {**self.commit["commit"], "tree": {"sha": tree}}}
        pr = {"number": 7, "merged": True, "state": "closed", "merge_commit_sha": commit["sha"], "commits": 2,
              "base": {"sha": base, "ref": "dev", "repo": {"full_name": self.repository}},
              "head": {"sha": reviewed}}
        head = {"sha": reviewed, "tree": {"sha": tree}}
        comparison = {"status": "ahead", "behind_by": 0, "merge_base_commit": {"sha": base},
                      "total_commits": 2, "ahead_by": 2,
                      "commits": [{"sha": "b" * 40, "parents": [{"sha": base}]}, commit]}

        def api(repository, path):
            self.assertEqual(repository, self.repository)
            return {"actions/runs/123": self.source, "commits/" + "a" * 40: commit, "pulls/7": pr,
                    f"compare/{base}...{commit['sha']}": comparison, "git/commits/" + reviewed: head}[path]

        def pages(repository, path, key=None):
            if path.startswith("actions/workflows/456/runs?"):
                return [self.source]
            if path == "commits/" + "a" * 40 + "/pulls":
                return [{"number": 7, "merged_at": "2026-09-09T10:59:00Z", "merge_commit_sha": commit["sha"],
                         "base": {"ref": "dev"}}]
            self.fail("unexpected source range lookup")

        with patch.dict(os.environ, self.environment), patch.object(transfer.github, "api", side_effect=api), \
                patch.object(transfer.github, "pages", side_effect=pages), \
                patch.object(recovery, "recipe", return_value=next(iter(recovery.SOURCE_RECIPES))), \
                patch.object(transfer, "require_dev_ancestor"), \
                patch.object(transfer.artifacts, "effective_jobs", return_value=self.jobs):
            identity, _, _ = recovery.original_context()
            self.assertEqual(identity["base_sha"], base)
            self.assertNotEqual(identity["base_sha"], commit["parents"][0]["sha"])
            head["tree"]["sha"] = "4" * 40
            with self.assertRaisesRegex(ValueError, "tree differs"):
                recovery.original_context()

    def test_completed_failed_only_retry_keeps_successful_prior_validation_jobs(self):
        jobs = deepcopy(self.jobs)
        for job in jobs[:-1]:
            job["run_attempt"] = 1

        def api(repository, path):
            self.assertEqual(repository, self.repository)
            return self.source if path == "actions/runs/123" else self.commit

        def pages(repository, path, key=None):
            self.assertEqual(repository, self.repository)
            if path == "actions/runs/123/jobs?filter=latest":
                return jobs
            if path.startswith("actions/workflows/456/runs?"):
                return [self.source]
            if path == "commits/" + "a" * 40 + "/pulls":
                return []
            self.fail("completed recovery must read the effective latest job snapshot")

        with patch.dict(os.environ, self.environment), patch.object(transfer.github, "api", side_effect=api), \
                patch.object(transfer.github, "pages", side_effect=pages), \
                patch.object(recovery, "recipe", return_value=next(iter(recovery.SOURCE_RECIPES))), \
                patch.object(transfer, "require_dev_ancestor"):
            _, run, effective = recovery.original_context()
        self.assertEqual(run["run_attempt"], 2)
        self.assertTrue(all(job["run_attempt"] == 1 for job in effective[:-1]))
        self.assertEqual(effective[-1]["run_attempt"], 2)

    def test_original_rejects_superseded_or_non_dev_run(self):
        for change in ({"run_attempt": 3}, {"status": "queued"}, {"conclusion": "success"},
                       {"event": "pull_request"}, {"head_branch": "main"}, {"path": recovery.WORKFLOW}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.original_context(run={**self.source, **change})

    def test_original_requires_the_approved_caller_and_package_pair(self):
        for recipe in (("e" * 40, self.identity["ci_revision"]), (self.identity["caller_workflow_blob_sha"], "d" * 40)):
            with self.subTest(recipe=recipe), self.assertRaisesRegex(ValueError, "not approved"):
                self.original_context(recipe=recipe)

    def test_newer_queued_failed_or_retried_source_ci_refuses_the_retained_archive(self):
        with patch.object(transfer.github, "pages", return_value=[self.source]) as pages:
            recovery.require_latest_source(self.source, self.commit)
            self.assertNotIn("head_sha=", pages.call_args.args[1])
            self.assertIn("created=%3E%3D2026-09-02", pages.call_args.args[1])
        for change in ({"id": 124, "status": "queued", "conclusion": None},
                       {"id": 124, "status": "completed", "conclusion": "failure"},
                       {"run_attempt": 3}, {"id": 122, "updated_at": "2026-09-09T11:31:00Z"}):
            with self.subTest(change=change), patch.object(transfer.github, "pages",
                    return_value=[self.source, {**self.source, **change}]), self.assertRaisesRegex(ValueError, "superseded"):
                recovery.require_latest_source(self.source, self.commit)
        with patch.object(transfer.github, "pages", return_value=[]), self.assertRaisesRegex(ValueError, "missing"):
            recovery.require_latest_source(self.source, self.commit)
        with patch.object(transfer.github, "pages", return_value=[{**self.source, "status": "queued", "conclusion": None}]), \
                self.assertRaisesRegex(ValueError, "superseded"):
            recovery.require_latest_source(self.source, self.commit)

    def test_every_original_validation_consumer_must_pass(self):
        for conclusion in ("failure", "cancelled", "skipped", None):
            jobs = deepcopy(self.jobs)
            jobs[1]["conclusion"] = conclusion
            with self.subTest(conclusion=conclusion), self.assertRaisesRegex(ValueError, "consumer"):
                self.original_context(jobs=jobs)
        with self.assertRaisesRegex(ValueError, "incomplete"):
            self.original_context(jobs=self.jobs[1:])

    def test_original_publisher_must_stop_before_any_staging_or_push(self):
        for index in range(len(self.jobs[-1]["steps"])):
            for conclusion in ("success", "failure"):
                if index == 0 and conclusion == "failure":
                    continue
                jobs = deepcopy(self.jobs)
                jobs[-1]["steps"][index]["conclusion"] = conclusion
                with self.subTest(index=index, conclusion=conclusion), self.assertRaisesRegex(ValueError, "pre-publication"):
                    self.original_context(jobs=jobs)
        jobs = deepcopy(self.jobs)
        jobs[-1]["steps"].pop()
        with self.assertRaisesRegex(ValueError, "pre-publication"):
            self.original_context(jobs=jobs)

    def test_newer_queued_failed_or_retried_recovery_supersedes_old_success(self):
        with patch.object(transfer.github, "pages", return_value=[self.run]):
            recovery.require_latest(self.publisher, self.run)
        for change in ({"id": 902, "status": "queued"}, {"id": 902, "status": "completed", "conclusion": "failure"},
                       {"run_attempt": 2}):
            with self.subTest(change=change), patch.object(transfer.github, "pages", return_value=[self.run, {**self.run, **change}]), \
                    self.assertRaisesRegex(ValueError, "superseded"):
                recovery.require_latest(self.publisher, self.run)

    def test_recovery_admission_reuses_exact_archive_and_measurement_checks(self):
        with patch.dict(os.environ, self.environment), \
                patch.object(recovery, "publisher_context", return_value=(self.publisher, self.run, self.job)), \
                patch.object(recovery, "require_latest"), \
                patch.object(recovery, "original_context", return_value=(self.identity, self.source, self.jobs)), \
                patch.object(transfer.github, "pages", return_value=self.artifacts):
            expected = recovery.RecoveryAuthority().admit()
            self.assertEqual(expected["identity"], self.identity)
            self.assertEqual(expected["publisher"], self.publisher)
            self.assertIn("dev-main-aaaaaaaa-20260909120000", expected["image"])
            measurement = deepcopy(self.artifacts[1])
            self.artifacts[1].update(name="healthcare-public-measurement-123-1",
                                     created_at=(images.NOW - images.timedelta(hours=1)).isoformat())
            self.artifacts.append({**measurement, "id": 56})
            with self.assertRaisesRegex(ValueError, "current successful measurement upload"):
                recovery.RecoveryAuthority().admit()
            self.artifacts[1] = measurement
            self.artifacts.pop()
            self.artifacts[0]["expired"] = True
            with self.assertRaisesRegex(ValueError, "artifact"):
                recovery.RecoveryAuthority().admit()

    def test_receipt_binds_original_inputs_and_separate_new_publisher(self):
        expected = {**images.expected(), "identity": self.identity, "publisher": self.publisher}
        receipt = recovery.RecoveryAuthority().publication_receipt(expected, {"archive_sha256": "1" * 64,
                       "config_digest": images.CONFIG}, images.ROOT_DIGEST)
        self.assertEqual(receipt["schema"], recovery.SCHEMA)
        self.assertEqual(receipt["run_id"], "123")
        self.assertEqual(receipt["run_attempt"], 2)
        self.assertEqual(receipt["ci_revision"], self.identity["ci_revision"])
        self.assertEqual(receipt["publisher"], self.publisher)
        self.assertEqual(receipt["measurement_artifact"]["id"], 55)
        self.assertEqual(receipt["producer_artifact"]["id"], 44)
        self.assertEqual(transfer.proof_name(self.publisher, recovery=True), "healthcare-public-image-recovery-900-1")
        self.assertEqual(transfer.proof_name(self.publisher, True, recovery=True), "healthcare-public-image-recovery-intent-900-1")

    def test_cleanup_authenticates_same_publisher_without_readmitting_original(self):
        expected = {"identity": self.identity, "publisher": self.publisher,
                    "image": transfer.IMAGES[self.repository] + ":dev-main-aaaaaaaa-20260909120000"}
        with patch.object(recovery, "publisher_context", return_value=(self.publisher, self.run, self.job)), \
                patch.object(recovery, "original_context", side_effect=AssertionError("cleanup must not re-admit")), \
                patch.object(recovery, "require_latest", side_effect=AssertionError("cleanup must not re-admit")):
            self.assertEqual(recovery.RecoveryAuthority().active_publisher(expected), (self.run, self.job))
            with self.assertRaises(ValueError):
                recovery.RecoveryAuthority().active_publisher({**expected, "publisher": {**self.publisher, "run_attempt": 2}})

    def test_recipe_requires_exact_original_publisher_package_pin(self):
        workflow = {"jobs": {"dev-image-publication": {"env": {"CI_REVISION": "d" * 40}, "steps": [
            {"name": "Check out trusted image publisher", "with": {"repository": "EndurantDevs/endurant-ci",
             "ref": "d" * 40, "persist-credentials": False}}]}}}
        def item(value):
            raw = yaml.safe_dump(value).encode()
            blob = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw, usedforsecurity=False).hexdigest()
            return {"sha": blob, "encoding": "base64", "content": base64.b64encode(raw).decode()}
        with patch.object(transfer.github, "api", return_value=item(workflow)) as api:
            self.assertEqual(recovery.recipe(self.repository, "a" * 40, ".github/workflows/ci.yml"), (api.return_value["sha"], "d" * 40))
            workflow["jobs"]["dev-image-publication"]["steps"][0]["with"]["ref"] = "f" * 40
            api.return_value = item(workflow)
            with self.assertRaisesRegex(ValueError, "package pin"):
                recovery.recipe(self.repository, "a" * 40, ".github/workflows/ci.yml")

    def test_recipe_authenticates_raw_blob_and_exact_generated_manual_workflow(self):
        import render_workflow
        text = render_workflow.render_image_recovery("healthcare", "f" * 40)
        raw = text.encode()
        blob = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw, usedforsecurity=False).hexdigest()
        item = {"sha": blob, "encoding": "base64", "content": base64.b64encode(raw).decode()}
        with patch.object(transfer.github, "api", return_value=item) as api:
            self.assertEqual(recovery.recipe(self.repository, "c" * 40, recovery.WORKFLOW,
                                             expected_workflow=text), (blob, "f" * 40))
            with self.assertRaisesRegex(ValueError, "caller bytes"):
                recovery.recipe(self.repository, "c" * 40, recovery.WORKFLOW, expected_workflow=text + "# changed\n")
            api.return_value = {**item, "sha": "e" * 40}
            with self.assertRaisesRegex(ValueError, "caller bytes"):
                recovery.recipe(self.repository, "c" * 40, recovery.WORKFLOW)

    def reconciled_fixture(self, *, receipt_failure=False):
        jobs = deepcopy(self.jobs)
        for step, conclusion in zip(jobs[-1]["steps"],
                ("success", "success", "success", "success", "success" if receipt_failure else "failure",
                 "failure" if receipt_failure else "skipped", "success")):
            step["conclusion"] = conclusion
            step.update(started_at=(images.NOW - images.timedelta(seconds=5)).isoformat(),
                        completed_at=(images.NOW + images.timedelta(seconds=5)).isoformat())
        inputs = {"artifact": transfer.snapshot(self.artifacts[0]), "measurement": transfer.snapshot(self.artifacts[1]),
                  "producer_run_attempt": 2, "measurement_run_attempt": 2, "producer_job_id": 1}
        target = transfer.IMAGES[self.repository] + ":dev-main-aaaaaaaa-20260909120000"
        intent = {"schema": "public-source-image-intent-v1", "publication": {
            "identity": self.identity, **inputs, "image": target, "workflow_id": 456, "publisher_job_id": 4},
            "producer": {"archive_sha256": "1" * 64},
            "created_at": (images.NOW - images.timedelta(seconds=1)).isoformat(),
            "expires_at": (images.NOW + images.timedelta(minutes=20)).isoformat()}
        artifact = images.artifact(66, transfer.proof_name(self.identity, True), 1)
        return jobs, inputs, intent, artifact, target

    def test_reconciled_publish_failure_requires_exact_intent_and_absent_old_target(self):
        for receipt_failure in (False, True):
            with self.subTest(receipt_failure=receipt_failure):
                jobs, inputs, intent, artifact, target = self.reconciled_fixture(receipt_failure=receipt_failure)
                self.assertEqual(recovery.failure_state(jobs[-1]), "reconciled")
                self.original_context(jobs=jobs)
                with patch.object(transfer.github, "pages", return_value=[artifact]), \
                        patch.object(transfer, "artifact_payload", return_value=intent), \
                        patch.object(transfer, "require_absent_registry_tag") as absent:
                    self.assertEqual(recovery.reconciled_producer(self.identity, self.source, jobs, inputs), intent["producer"])
                    absent.assert_called_once_with(target)
                    absent.side_effect = ValueError("existing registry tag")
                    with self.assertRaisesRegex(ValueError, "registry tag"):
                        recovery.reconciled_producer(self.identity, self.source, jobs, inputs)

    def test_reconciled_failure_rejects_receipt_changed_intent_or_unapproved_cleanup(self):
        for receipt_failure in (False, True):
            with self.subTest(receipt_failure=receipt_failure):
                jobs, inputs, intent, artifact, _ = self.reconciled_fixture(receipt_failure=receipt_failure)
                with patch.object(transfer.github, "pages", return_value=[artifact]) as pages, \
                        patch.object(transfer, "artifact_payload", return_value=intent) as payload, \
                        patch.object(transfer, "require_absent_registry_tag") as absent:
                    pages.return_value = [artifact, {"name": transfer.proof_name(self.identity)}]
                    if receipt_failure:
                        self.assertEqual(recovery.reconciled_producer(self.identity, self.source, jobs, inputs), intent["producer"])
                        absent.assert_called_once()
                        absent.reset_mock()
                    else:
                        with self.assertRaisesRegex(ValueError, "receipt"):
                            recovery.reconciled_producer(self.identity, self.source, jobs, inputs)
                    pages.return_value = []
                    with self.assertRaisesRegex(ValueError, "intent artifact"):
                        recovery.reconciled_producer(self.identity, self.source, jobs, inputs)
                    pages.return_value = [artifact]
                    for change in ({"publication": {**intent["publication"], "image": "unrelated"}},
                                   {"producer": None}, {"schema": "unknown"}):
                        payload.return_value = {**intent, **change}
                        with self.subTest(change=change), self.assertRaisesRegex(ValueError, "intent differs"):
                            recovery.reconciled_producer(self.identity, self.source, jobs, inputs)
                    with self.assertRaisesRegex(ValueError, "package"):
                        recovery.reconciled_producer({**self.identity, "ci_revision": "d" * 40}, self.source, jobs, inputs)
                    absent.assert_not_called()

    def test_leftover_receipt_requires_a_unique_terminal_failed_upload(self):
        original, inputs, intent, artifact, _ = self.reconciled_fixture(receipt_failure=True)
        for change in ({"conclusion": "success"}, {"conclusion": "skipped"}, {"status": "in_progress"}):
            jobs = deepcopy(original)
            next(step for step in jobs[-1]["steps"] if step["name"] == transfer.RECEIPT_UPLOAD).update(change)
            with self.subTest(change=change), patch.object(transfer.github, "pages",
                    return_value=[artifact, {"name": transfer.proof_name(self.identity)}]), \
                    patch.object(transfer, "artifact_payload", return_value=intent), \
                    patch.object(transfer, "require_absent_registry_tag") as absent, \
                    self.assertRaisesRegex(ValueError, "receipt"):
                recovery.reconciled_producer(self.identity, self.source, jobs, inputs)
            absent.assert_not_called()

    def test_failed_receipt_upload_requires_successful_completed_publication_and_reconciliation(self):
        original, *_ = self.reconciled_fixture(receipt_failure=True)
        for name in ("Publish validated DEV image", "Reconcile DEV image publication"):
            for change in ({"conclusion": "failure"}, {"conclusion": "skipped"}, {"status": "in_progress"}):
                jobs = deepcopy(original)
                next(step for step in jobs[-1]["steps"] if step["name"] == name).update(change)
                with self.subTest(name=name, change=change), self.assertRaisesRegex(ValueError, "reconciled failure"):
                    self.original_context(jobs=jobs)

    def test_archive_must_equal_original_reconciled_intent_producer(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            record = images.TransferChecks().setup_artifact(directory)
            expected = {**images.expected(), "prior_producer": record}
            self.assertEqual(transfer.transfer_record(expected, directory)[0], record)
            with self.assertRaisesRegex(ValueError, "original publication intent"):
                transfer.transfer_record({**expected, "prior_producer": {**record, "archive_sha256": "0" * 64}}, directory)

    def test_shared_publish_uses_recovery_authority_and_preserves_failure_cleanup(self):
        checks = images.TransferChecks()
        publisher = self.publisher
        class Authority(transfer.PublicationAuthority):
            def admit(self):
                return images.expected()

            def publication_receipt(self, expected, record, digest):
                return recovery.RecoveryAuthority().publication_receipt({**expected, "publisher": publisher}, record, digest)

        for failure in (False, True):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary) / "input"
                directory.mkdir()
                record = checks.setup_artifact(directory)
                if failure:
                    with self.assertRaises(images.subprocess.CalledProcessError):
                        checks.exercise(directory, record, failure=True, authority=Authority())
                else:
                    checks.exercise(directory, record, authority=Authority())
                    receipt = json.loads((directory.parent / "public-image-receipt/image.json").read_text())
                    self.assertEqual(receipt["schema"], recovery.SCHEMA)
                    self.assertEqual(receipt["publisher"], publisher)

    def test_shared_recovery_cleanup_keeps_authority_through_root_and_child_deletion(self):
        result, calls = images.ReconciliationChecks().exercise(indexed=True, authority=recovery.RecoveryAuthority())
        self.assertTrue(result)
        self.assertEqual([path for _, path, method in calls if method == "DELETE"],
                         ["/versions/3", "/versions/4", "/versions/5"])


if __name__ == "__main__":
    unittest.main()
