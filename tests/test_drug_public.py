"""Public source identity and measurement fail closed without private inputs."""

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


IDENTITY = load("source_identity", "scripts/source_identity.py")
POLICY = load("coverage_policy", "scripts/coverage_policy.py")
MEASUREMENT = load("drug_measurement", "scripts/drug/measurement.py")
SOURCE, BASE, PIN = "1" * 40, "2" * 40, "3" * 40
REPOSITORY = "EndurantDevs/drug-api"


class PublicDrugTests(unittest.TestCase):
    def test_source_coverage_contract_rejects_downgrade_and_non_boolean(self):
        self.assertEqual(POLICY.drug_coverage_protocol({}, {}), "legacy")
        self.assertEqual(POLICY.drug_coverage_protocol({}, {"machine_artifact_required": True}), "machine")
        for base, source in (({"machine_artifact_required": True}, {}), ({}, {"machine_artifact_required": 1})):
            with self.subTest(base=base, source=source), self.assertRaises(ValueError):
                POLICY.drug_coverage_protocol(base, source)

    def test_pull_request_uses_exact_head_base_and_live_title(self):
        pr = {"number": 7, "state": "open", "title": "ci: validate public source",
              "base": {"ref": "main", "sha": BASE, "repo": {"full_name": REPOSITORY}},
              "head": {"sha": SOURCE}}
        payload = {"repository": {"full_name": REPOSITORY, "private": False}, "pull_request": copy.deepcopy(pr)}
        with patch.object(IDENTITY, "api", return_value=pr):
            result = IDENTITY.resolve_identity(REPOSITORY, "pull_request", payload, "4" * 40, "refs/pull/7/merge")
            self.assertEqual(result["source_sha"], SOURCE)
            self.assertEqual(result["base_sha"], BASE)
            self.assertEqual(result["pr_title"], pr["title"])
            for field, value in (("title", "ci: change the accepted title"), ("state", "closed")):
                changed = copy.deepcopy(pr)
                changed[field] = value
                with self.subTest(field=field), patch.object(IDENTITY, "api", return_value=changed):
                    with self.assertRaises(ValueError):
                        IDENTITY.resolve_identity(REPOSITORY, "pull_request", payload, SOURCE, "refs/pull/7/merge")

    def test_push_range_uses_complete_rebased_pull_request(self):
        intermediate, original, tree = "4" * 40, "5" * 40, "6" * 40
        commit = {"sha": SOURCE, "parents": [{"sha": intermediate}], "commit": {"tree": {"sha": tree}}}
        pr = {"number": 7, "merged": True, "state": "closed", "merge_commit_sha": SOURCE,
              "base": {"ref": "main", "sha": BASE, "repo": {"full_name": REPOSITORY}},
              "head": {"sha": original}, "commits": 2}
        commits = [{"sha": intermediate, "parents": [{"sha": BASE}]}, commit]
        comparison = {"status": "ahead", "behind_by": 0, "merge_base_commit": {"sha": BASE},
                      "total_commits": 2, "ahead_by": 2, "commits": commits}
        responses = {f"commits/{SOURCE}": commit, "pulls/7": pr, f"compare/{BASE}...{SOURCE}": comparison,
                     f"git/commits/{original}": {"sha": original, "tree": {"sha": tree}}}
        payload = {"repository": {"full_name": REPOSITORY, "private": False}, "ref": "refs/heads/main", "after": SOURCE,
                   "before": intermediate}
        associated = [{**pr, "merged_at": "2026-01-01T00:00:00Z"}]
        with patch.object(IDENTITY, "api", side_effect=lambda repo, path: responses[path]):
            with patch.object(IDENTITY, "pages", return_value=associated):
                result = IDENTITY.resolve_identity(REPOSITORY, "push", payload, SOURCE, "refs/heads/main")
                self.assertEqual(result["base_sha"], BASE)
                comparison["commits"] = [commit]
                with self.assertRaises(ValueError):
                    IDENTITY.resolve_identity(REPOSITORY, "push", payload, SOURCE, "refs/heads/main")

    def test_rejects_other_repository_and_manual_event(self):
        for repository, event in (("example/private", "push"), (REPOSITORY, "workflow_dispatch")):
            with self.subTest(repository=repository, event=event), self.assertRaises(ValueError):
                IDENTITY.resolve_identity(repository, event, {"repository": {"full_name": repository, "private": False}}, SOURCE, "refs/heads/main")
        with self.assertRaises(ValueError):
            IDENTITY.resolve_identity(REPOSITORY, "push", {"repository": {"full_name": REPOSITORY, "private": True}}, SOURCE, "refs/heads/main")

    def test_public_gate_retains_existing_execution_lanes(self):
        gate = (ROOT / "scripts/drug/check").read_text()
        measure = gate.split("\nmeasure() {", 1)[1].split("\ncase ", 1)[0]
        for command in ("require_frozen_candidate", "install", "require_python", "public_hygiene", "quality",
                        "test_run", "coverage_report", "coverage_provenance", "coverage_measurement",
                        "security", "runtime_image", "local_services", "write_receipt"):
            self.assertIn(f"\n    {command}", measure)
        self.assertIn('scripts/coverage_ratchet.py --self-test', gate)
        self.assertNotIn('coverage_forecast.py forecast', gate)

    def test_publisher_emits_exact_hashed_data_and_rejects_forgery(self):
        identity = {"repository": REPOSITORY, "source_sha": SOURCE, "base_sha": BASE,
                    "source_branch": "main", "pr_number": "0", "pr_title": ""}
        with tempfile.TemporaryDirectory() as raw:
            root, staging, output = (Path(raw) / part for part in ("source", "stage", "output"))
            root.mkdir()
            staging.mkdir()
            report = {"meta": {"version": "7"}, "files": {"main.py": {"executed_lines": [1]}}}
            report_bytes = json.dumps(report).encode()
            (staging / MEASUREMENT.REPORT).write_bytes(report_bytes)
            provenance = {"schema_version": 1, "head_sha": SOURCE, "base_sha": BASE,
                          "report_path": MEASUREMENT.REPORT,
                          "report_sha256": hashlib.sha256(report_bytes).hexdigest(), "coverage_version": "7"}
            (staging / MEASUREMENT.PROVENANCE).write_text(json.dumps(provenance))
            with patch.object(MEASUREMENT.subprocess, "check_output", return_value="main.py\0"):
                result = MEASUREMENT.export_measurement(root, staging, output, identity, "123", "2", PIN)
                self.assertEqual(result["schema"], "public-source-measurement-v1")
                self.assertEqual(result["run_attempt"], 2)
                self.assertEqual(result["sha256"][MEASUREMENT.REPORT], hashlib.sha256(report_bytes).hexdigest())
                self.assertEqual(sorted(path.name for path in output.iterdir()),
                                 sorted((MEASUREMENT.REPORT, MEASUREMENT.PROVENANCE, "measurement.json")))
                with patch.object(MEASUREMENT, "MAX_FILE_BYTES", len(report_bytes) - 1):
                    with self.assertRaises(ValueError):
                        MEASUREMENT.export_measurement(root, staging, output, identity, "123", "2", PIN)
                with patch.object(MEASUREMENT, "LIMIT", sum(path.stat().st_size for path in staging.iterdir()) + MEASUREMENT.ZIP_RESERVE):
                    with self.assertRaises(ValueError):
                        MEASUREMENT.export_measurement(root, staging, output, identity, "123", "2", PIN)
                provenance["base_sha"] = PIN
                (staging / MEASUREMENT.PROVENANCE).write_text(json.dumps(provenance))
                with self.assertRaises(ValueError):
                    MEASUREMENT.export_measurement(root, staging, output, identity, "123", "2", PIN)
                (staging / "unexpected.sh").write_text("exit 0\n")
                with self.assertRaises(ValueError):
                    MEASUREMENT.export_measurement(root, staging, output, identity, "123", "2", PIN)

    def test_measurement_json_rejects_duplicate_fields_and_non_json_constants(self):
        with self.assertRaises(ValueError):
            json.loads('{"files":{},"files":{}}', object_pairs_hook=MEASUREMENT.unique_object)
        with self.assertRaises(ValueError):
            json.loads('{"metric":NaN}', parse_constant=MEASUREMENT.invalid_constant)


if __name__ == "__main__":
    unittest.main()
