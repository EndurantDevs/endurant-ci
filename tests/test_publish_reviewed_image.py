"""Exercise the protected publisher with synthetic GitHub and approval metadata."""

import json
import sys
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import publish_reviewed_image as publisher


class ReviewedPublisherTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.authority = publisher.ReviewedAuthority(Path(scratch.name))
        self.revision = "d" * 40
        self.repository = publisher.REPOSITORY
        self.run = {
            "id": 123, "run_attempt": 1, "event": "workflow_dispatch", "head_branch": "dev",
            "path": publisher.approval.VALIDATION_WORKFLOW, "head_sha": "e" * 40,
            "workflow_id": 50, "status": "completed", "conclusion": "success",
            "repository": {"full_name": self.repository}, "head_repository": {"full_name": self.repository},
            "referenced_workflows": [{"path": f"{publisher.RECIPE}@{self.revision}", "sha": self.revision}],
        }
        self.input = {"source_run_id": 123, "source_run_attempt": 1}
        self.validation_blob = "6" * 40
        self.caller = {"ci_revision": self.revision, "workflow_id": 60, "job_id": 70,
                       "validation_workflow_blob_sha": self.validation_blob}
        now = datetime.now(timezone.utc)
        started, finished = (now - timedelta(minutes=1)).isoformat(), (now + timedelta(minutes=1)).isoformat()
        self.jobs = [{"id": 40 + index, "name": name, "status": "completed", "conclusion": "success", "steps": [{
            "name": step, "status": "completed", "conclusion": "success", "started_at": started,
            "completed_at": finished,
        }]} for index, (name, step) in enumerate(publisher.ARTIFACT_JOBS.values())]
        self.jobs += [{"id": 50 + index, "name": name, "status": "completed", "conclusion": "success"}
                      for index, name in enumerate(("Portable smoke", "Hosted validation / Validation complete",
                                                    "Hosted validation / Security scans"))]
        self.artifacts = {}
        for index, (kind, name) in enumerate(publisher.approval.ARTIFACT_NAMES.items(), 1):
            self.input[f"{kind}_artifact_id"] = index
            self.artifacts[kind] = {
                "id": index, "name": f"{name}-123-1", "size_in_bytes": 1234,
                "digest": "sha256:" + ("4" * 64), "expired": False, "created_at": now.isoformat(),
                "expires_at": (now + timedelta(hours=1)).isoformat(),
                "workflow_run": {"id": 123, "head_sha": self.run["head_sha"]},
            }
        scope = {
            "repository": self.repository, "source_sha": "a" * 40, "base_sha": "b" * 40,
            "source_tree": "c" * 40, "source_branch": "reviewed", "pr_number": "0",
            "run_id": "123", "run_attempt": 1, "ci_revision": self.revision,
            "producer": {"repository": self.repository, "run_id": "123", "run_attempt": 1,
                         "workflow_ref": f"{self.repository}/{self.run['path']}@{publisher.REF}",
                         "head_sha": self.run["head_sha"], "workflow_sha": self.run["head_sha"]},
        }
        self.staged = {**scope, "schema": "reviewed-source-image-staging-v1", "source_workflow_blob_sha": "f" * 40,
                       "image_id": "sha256:" + "2" * 64, "config_digest": "sha256:" + "3" * 64,
                       "archive_sha256": "1" * 64, "platform": "linux/amd64"}
        self.measurement = {**deepcopy(scope), "schema": "reviewed-source-measurement-v1", "pr_title": "",
                            "producer_attempts": {"coverage-test": 1}}
        self.commit = {"sha": scope["source_sha"], "tree": {"sha": scope["source_tree"]},
                       "parents": [{"sha": scope["base_sha"]}]}
        proof = publisher.approval.public_proof(self.staged, self.measurement, self.artifacts)
        self.check = {
            "id": 100, "name": publisher.approval.CHECK_NAME, "head_sha": scope["source_sha"], "app": {"id": 42},
            "status": "completed", "conclusion": "success",
            "details_url": f"https://github.com/{self.repository}/actions/runs/123",
            "external_id": publisher.approval.external_id(proof, now + timedelta(minutes=30)),
        }
        self.checks = [self.check]
        self.save_downloads()
        for mock in (
            patch.object(publisher, "publisher_context", return_value=(self.caller, {}, {}, 42)),
            patch.object(publisher, "dispatch_inputs", return_value=self.input),
            patch.object(publisher.transfer.artifacts, "attempt_jobs", return_value=self.jobs),
            patch.object(publisher.transfer.github, "api", side_effect=self.api),
            patch.object(publisher.transfer.github, "pages", side_effect=self.pages),
        ):
            mock.start()
            self.addCleanup(mock.stop)

    def save_downloads(self):
        for directory, name, payload in ((self.authority.image, "producer.json", self.staged),
                                         (self.authority.measurement, "measurement.json", self.measurement)):
            directory.mkdir(exist_ok=True)
            (directory / name).write_text(json.dumps(payload))

    def api(self, repository, endpoint):
        self.assertEqual(repository, self.repository)
        if endpoint == "actions/runs/123":
            return deepcopy(self.run)
        if endpoint == f"contents/{self.run['path']}?ref={self.run['head_sha']}":
            return {"type": "file", "sha": self.validation_blob}
        if endpoint == f"git/commits/{self.commit['sha']}":
            return deepcopy(self.commit)
        for artifact in self.artifacts.values():
            if endpoint == f"actions/artifacts/{artifact['id']}":
                return deepcopy(artifact)
        raise AssertionError(endpoint)

    def pages(self, repository, endpoint, key):
        self.assertEqual(repository, self.repository)
        if endpoint == "actions/runs/123/artifacts" and key == "artifacts":
            return iter(deepcopy(list(self.artifacts.values())))
        if endpoint == f"commits/{self.commit['sha']}/check-runs?filter=all&app_id=42" and key == "check_runs":
            return iter(deepcopy(self.checks))
        raise AssertionError(endpoint)

    def test_binds_approved_exact_attempt_and_separate_receipt(self):
        entered = self.authority.admit()
        self.assertEqual(entered["image"].split(":")[1], "reviewed-aaaaaaaa-123-1")
        self.assertEqual(entered["reviewed_approval"], {
            "app_id": 42, "check_id": 100, "external_id": self.check["external_id"],
        })
        receipt = self.authority.publication_receipt(entered, self.staged, self.staged["image_id"])
        self.assertEqual(receipt["schema"], "reviewed-source-image-publication-v1")
        self.assertEqual(receipt["producer_artifact"]["id"], self.input["image_artifact_id"])
        self.assertEqual(receipt["measurement_artifact"]["id"], self.input["measurement_artifact_id"])
        self.assertEqual(receipt["native_artifact"]["id"], self.input["native_artifact_id"])
        self.assertEqual(receipt["approval"], entered["reviewed_approval"])

    def test_changed_approval_rejected_on_fresh_admission(self):
        self.authority.admit()
        self.checks.append({**self.check, "id": 101, "status": "in_progress", "external_id": None})
        with self.assertRaisesRegex(ValueError, "approval is unavailable"):
            self.authority.admit()

    def test_approval_revoked_after_load_stops_shared_registry_push(self):
        expected = self.authority.admit()
        image_id = self.staged["image_id"]

        def docker(*args):
            if args[:3] == ("docker", "image", "load"):
                return f"Loaded image ID: {image_id}\n".encode()
            if args[:3] == ("docker", "image", "tag"):
                self.checks.append({**self.check, "id": 101, "conclusion": "failure"})
                return b""
            raise AssertionError(args)

        with patch.object(publisher.transfer, "transfer_record", return_value=(self.staged, {image_id})), \
                patch.object(publisher.transfer, "authenticated_intent", return_value=({"producer": self.staged}, {}, {})), \
                patch.object(publisher.transfer, "configure_containerd_store"), \
                patch.object(publisher.transfer, "image_present", return_value=False), \
                patch.object(publisher.transfer, "image_identity", return_value={"Id": image_id}), \
                patch.object(publisher.transfer, "remove_image") as remove, \
                patch.object(publisher.transfer, "command", side_effect=docker):
            with self.assertRaisesRegex(ValueError, "approval is unavailable"):
                publisher.transfer.publish(expected, self.authority.image, authority=self.authority)
        self.assertEqual({call.args[0] for call in remove.call_args_list}, {image_id, expected["image"]})

    def test_rejects_changed_artifact_even_with_the_approved_source(self):
        self.artifacts["native"]["digest"] = "sha256:" + "8" * 64
        with self.assertRaisesRegex(ValueError, "approval differs"):
            self.authority.admit()

    def test_rejects_carried_attempt_and_missing_smoke(self):
        self.measurement["producer_attempts"]["coverage-test"] = 0
        self.save_downloads()
        with self.assertRaisesRegex(ValueError, "freshly executed"):
            self.authority.admit()
        self.jobs[:] = [job for job in self.jobs if job["name"] != "Portable smoke"]
        with self.assertRaisesRegex(ValueError, "portable and hosted validation"):
            self.authority.admit()

    def test_rejects_different_current_attempt_and_recipe(self):
        self.run["run_attempt"] = 2
        with self.assertRaisesRegex(ValueError, "exact run, attempt"):
            self.authority.admit()
        self.run["run_attempt"] = 1
        self.run["referenced_workflows"][0]["sha"] = "0" * 40
        with self.assertRaisesRegex(ValueError, "exact pinned validation recipe"):
            self.authority.admit()

    def test_rejects_unfrozen_caller_and_ambiguous_jobs(self):
        original = self.validation_blob
        self.validation_blob = "9" * 40
        with self.assertRaisesRegex(ValueError, "exact frozen validation workflow"):
            self.authority.admit()
        self.validation_blob = original
        self.jobs.append({**self.jobs[0], "id": 274})
        with self.assertRaisesRegex(ValueError, "inventory is ambiguous"):
            self.authority.admit()
        self.jobs[-1] = {**self.jobs[0], "name": "Second producer"}
        with self.assertRaisesRegex(ValueError, "inventory is ambiguous"):
            self.authority.admit()

    def test_reconcile_rejects_a_registry_root_outside_the_tested_digest(self):
        expected = self.authority.admit()
        intent = {"publication": expected, "producer": self.staged}
        with patch.object(publisher.transfer, "registry_read", return_value=b"another image"), \
                self.assertRaisesRegex(ValueError, "digest differs from its tested image"):
            publisher.transfer.registry_publication(intent)


class ReviewedPublisherWorkflowTests(unittest.TestCase):
    def test_dispatch_inputs_are_exact_strings_and_bind_known_artifacts(self):
        expected = {name: index for index, name in enumerate(publisher.render_workflow.REVIEWED_IMAGE_INPUTS, 1)}
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "event.json"
            values = {name: str(number) for name, number in expected.items()}
            path.write_text(json.dumps({"inputs": values}))
            env = {**{name.upper(): value for name, value in values.items()}, "GITHUB_EVENT_PATH": str(path)}
            self.assertEqual(publisher.dispatch_inputs(env), expected)
            with self.assertRaisesRegex(ValueError, "differ from the exact dispatch"):
                publisher.dispatch_inputs({**env, "IMAGE_ARTIFACT_ID": "248"})
            path.write_text(json.dumps({"inputs": {**values, "approval_app_id": "42"}}))
            with self.assertRaisesRegex(ValueError, "differ from the exact dispatch"):
                publisher.dispatch_inputs(env)

    def test_exact_core_workflow_contains_only_publication_authority(self):
        document = publisher.render_workflow.render_reviewed_image_publisher("d" * 40, 42, "e" * 40)
        self.assertNotIn("secrets.", document)
        workflow = yaml.safe_load(document)
        trigger = workflow.get("on", workflow.get(True))
        self.assertEqual(set(trigger), {"workflow_dispatch"})
        self.assertEqual(set(trigger["workflow_dispatch"]["inputs"]), set(publisher.render_workflow.REVIEWED_IMAGE_INPUTS))
        self.assertEqual(set(workflow["jobs"]), {"dev-image-publication"})
        job = workflow["jobs"]["dev-image-publication"]
        self.assertEqual(job["env"]["REVIEWED_IMAGE_APPROVAL_APP_ID"], "42")
        self.assertEqual(job["env"]["REVIEWED_VALIDATION_WORKFLOW_BLOB"], "e" * 40)
        self.assertEqual(job["permissions"]["packages"], "write")
        self.assertIn("github.ref_protected", job["if"])
        downloads = [step for step in job["steps"] if "download-artifact" in step.get("uses", "")]
        self.assertEqual(len(downloads), 2)
        for step in downloads:
            self.assertEqual(step["with"]["digest-mismatch"], "error")
            self.assertEqual(step["with"]["run-id"], "${{ inputs.source_run_id }}")
        for step in job["steps"]:
            if "upload-artifact" in step.get("uses", ""):
                self.assertIn("public-image-reviewed-", step["with"]["name"])
            if "checkout" in step.get("uses", ""):
                self.assertEqual(step["with"]["repository"], "EndurantDevs/endurant-ci")
                self.assertIs(step["with"]["persist-credentials"], False)
