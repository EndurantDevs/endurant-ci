"""Test approval binding, issuer, expiry and supersession."""

import copy
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import reviewed_source_approval as approval


class ReviewedSourceApprovalTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.expiry = self.now + timedelta(days=1)
        self.proof = {
            "source_sha": "a" * 40,
            "base_sha": "b" * 40,
            "source_tree": "c" * 40,
            "producer": {"repository": "example/source", "run_id": "123", "run_attempt": 1},
            "archive_sha256": "d" * 64,
            "measurement_digest": "sha256:" + "e" * 64,
        }
        self.check = {
            "id": 100,
            "name": approval.CHECK_NAME,
            "head_sha": self.proof["source_sha"],
            "app": {"id": 42},
            "status": "completed",
            "conclusion": "success",
            "external_id": approval.external_id(self.proof, self.expiry),
            "details_url": "https://github.com/example/source/actions/runs/123",
        }

    def test_accepts_latest_exact_approval(self):
        reordered = dict(reversed(tuple(self.proof.items())))
        self.assertEqual(approval.verify([self.check], reordered, 42, now=self.now), 100)

    def test_refuses_source_image_and_measurement_substitutions(self):
        for key in ("base_sha", "source_tree", "archive_sha256", "measurement_digest", "producer"):
            with self.subTest(key=key):
                proof = copy.deepcopy(self.proof)
                if key == "producer":
                    proof[key]["run_attempt"] = 2
                else:
                    proof[key] = "0" * len(proof[key])
                with self.assertRaisesRegex(ValueError, "differs from the tested proof"):
                    approval.verify([self.check], proof, 42, now=self.now)

    def test_refuses_expiry_and_wrong_app(self):
        with self.assertRaisesRegex(ValueError, "approval expired"):
            approval.verify([self.check], self.proof, 42, now=self.expiry)
        with self.assertRaisesRegex(ValueError, "approval is unavailable"):
            approval.verify([self.check], self.proof, 43, now=self.now)

    def test_refuses_an_older_approval_after_a_newer_check(self):
        pending = {**self.check, "id": 101, "status": "in_progress", "external_id": None}
        with self.assertRaisesRegex(ValueError, "approval is unavailable"):
            approval.verify([self.check, pending], self.proof, 42, now=self.now)

    def test_refuses_private_or_mismatched_approval_link(self):
        check = {**self.check, "details_url": "https://github.com/example/other/actions/runs/123"}
        with self.assertRaisesRegex(ValueError, "differs from the tested proof"):
            approval.verify([check], self.proof, 42, now=self.now)


class ReviewedSourcePublicProofTests(unittest.TestCase):
    def setUp(self):
        repository = approval.REPOSITORY
        producer = {
            "repository": repository, "head_sha": "e" * 40, "workflow_sha": "e" * 40,
            "workflow_ref": f"{repository}/{approval.VALIDATION_WORKFLOW}@refs/heads/dev",
            "run_id": "123", "run_attempt": 1,
        }
        scope = {
            "repository": repository, "source_sha": "a" * 40, "base_sha": "b" * 40, "source_tree": "c" * 40,
            "source_branch": "reviewed", "pr_number": "0", "ci_revision": "d" * 40,
            "run_id": "123", "run_attempt": 1, "producer": producer,
        }
        self.staged = {
            **scope, "schema": "reviewed-source-image-staging-v1", "source_workflow_blob_sha": "f" * 40,
            "archive_sha256": "1" * 64, "image_id": "sha256:" + "2" * 64,
            "config_digest": "sha256:" + "3" * 64, "platform": "linux/amd64",
        }
        self.measurement = {**copy.deepcopy(scope), "schema": "reviewed-source-measurement-v1", "pr_title": ""}
        self.artifacts = {
            kind: {"id": index, "name": f"{name}-123-1", "digest": "sha256:" + ("4" * 64),
                   "expired": False, "workflow_run": {"id": 123}}
            for index, (kind, name) in enumerate(approval.ARTIFACT_NAMES.items(), 1)
        }

    def test_proof_carries_all_three_archive_digests_and_exact_source(self):
        proof = approval.public_proof(self.staged, self.measurement, self.artifacts)
        self.assertEqual(proof["source_sha"], "a" * 40)
        self.assertEqual(proof["source_tree"], "c" * 40)
        self.assertEqual(proof["archive_sha256"], "1" * 64)
        self.assertEqual(set(proof["artifacts"]), {"image", "measurement", "native"})
        for kind, artifact in proof["artifacts"].items():
            self.assertEqual(artifact["digest"], self.artifacts[kind]["digest"])

    def test_measurement_and_tested_source_must_match(self):
        self.measurement["base_sha"] = "5" * 40
        with self.assertRaisesRegex(ValueError, "source identities differ"):
            approval.public_proof(self.staged, self.measurement, self.artifacts)

    def test_digest_must_be_the_hex_string_in_the_image_description(self):
        self.staged["archive_sha256"] = int("1" * 64)
        with self.assertRaisesRegex(ValueError, "image description is incomplete"):
            approval.public_proof(self.staged, self.measurement, self.artifacts)

    def test_producer_workflow_must_be_the_protected_validation_wrapper(self):
        for record in (self.staged, self.measurement):
            record["producer"]["workflow_ref"] = record["producer"]["workflow_ref"].replace("dev", "other")
        with self.assertRaisesRegex(ValueError, "producer identity is invalid"):
            approval.public_proof(self.staged, self.measurement, self.artifacts)

    def test_native_and_measurement_archives_must_be_current_from_same_run(self):
        for kind, changes in (("native", {"workflow_run": {"id": 421}}), ("measurement", {"expired": True})):
            with self.subTest(kind=kind):
                artifacts = copy.deepcopy(self.artifacts)
                artifacts[kind].update(changes)
                with self.assertRaisesRegex(ValueError, "artifact identity is invalid"):
                    approval.public_proof(self.staged, self.measurement, artifacts)
