"""Exercise the privileged cleanup boundary with synthetic GitHub responses only."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import artifact_cleanup as cleanup


def run(repository="EndurantDevs/healthcare-mrf-api"):
    return {"id": 123, "run_attempt": 2, "workflow_id": 456, "path": ".github/workflows/ci.yml",
            "event": "pull_request", "head_sha": "a" * 40, "head_branch": "fix/example",
            "status": "in_progress", "conclusion": None, "repository": {"full_name": repository},
            "head_repository": {"full_name": repository}}


def jobs():
    return [{"id": index + 1, "name": name, "run_id": 123, "run_attempt": 2, "head_sha": "a" * 40,
             "status": "in_progress" if name == cleanup.CLEANUP_JOB else "completed",
             "conclusion": None if name == cleanup.CLEANUP_JOB else "success"}
            for index, name in enumerate(("portable import checks", "Coverage results", cleanup.CLEANUP_JOB))]


def artifact(identifier, name, days=1):
    created = datetime.now(timezone.utc) - timedelta(hours=1)
    return {"id": identifier, "name": name, "expired": False, "size_in_bytes": 100,
            "created_at": created.isoformat(), "expires_at": (created + timedelta(days=days)).isoformat(),
            "digest": "sha256:" + "b" * 64}


class ArtifactCleanupChecks(unittest.TestCase):
    def exercise(self, items, expected=None, refresh=None, changed_artifact=None, delete_error=False,
                 job_inventory=None, refreshed_jobs=None):
        expected = expected or run()
        calls, deleted = [], []
        run_reads = job_reads = 0

        def api(repository, path, method="GET"):
            nonlocal run_reads, job_reads
            self.assertEqual(repository, expected["repository"]["full_name"])
            calls.append((method, path))
            if method == "DELETE":
                if delete_error:
                    raise OSError("synthetic permission failure")
                deleted.append(int(path.rsplit("/", 1)[1]))
                return None
            if path == "actions/runs/123":
                run_reads += 1
                return {**expected, **(refresh or {})} if run_reads > 1 else expected
            if path.startswith("actions/runs/123/attempts/2/jobs?"):
                job_reads += 1
                current = jobs() if job_inventory is None else job_inventory
                if job_reads > 1 and refreshed_jobs is not None:
                    current = refreshed_jobs
                return {"jobs": deepcopy(current)}
            if "per_page=100&page=" in path:
                self.assertFalse(deleted, "pagination must finish before deleting")
                page = int(path.rsplit("=", 1)[1])
                return {"artifacts": deepcopy(items[(page - 1) * 100:page * 100])}
            identifier = int(path.rsplit("/", 1)[1])
            result = deepcopy(next(item for item in items if item["id"] == identifier))
            result["workflow_run"] = {"id": expected["id"], "head_sha": expected["head_sha"]}
            if changed_artifact and identifier == changed_artifact[0]:
                result.update(changed_artifact[1])
            return result

        with patch.object(cleanup.github, "api", side_effect=api), patch("sys.stdout", new=io.StringIO()), \
                patch.object(cleanup.time, "sleep") as sleep:
            try:
                count = cleanup.cleanup(expected["repository"]["full_name"], expected)
            except (ValueError, OSError):
                self.assertEqual(deleted, [], "invalid or changed evidence must prevent deletion")
                sleep.assert_not_called()
                raise
            self.assertEqual(sleep.call_args_list, [((1,),)] * len(deleted))
        self.assertEqual(count, len(deleted))
        return deleted, calls

    def test_only_known_exact_attempt_intermediates_are_deleted_after_complete_pagination(self):
        for repository, kind in cleanup.KINDS.items():
            expected = run(repository)
            names = sorted(cleanup.temporary_names(kind, expected))
            intermediates = [artifact(index + 1, name) for index, name in enumerate(names)]
            keep = [artifact(100, f"{kind}-public-measurement-123-2", 90),
                    artifact(101, names[0].removesuffix("-2") + "-1"),
                    artifact(102, names[0].replace("123-2", "999-2")),
                    artifact(103, names[0] + "-unknown"),
                    artifact(104, names[0].removesuffix("-123-2"))]
            keep.extend(artifact(200 + index, f"unknown-{index}") for index in range(100))
            with self.subTest(repository=repository):
                deleted, calls = self.exercise(intermediates + keep, expected)
                self.assertEqual(deleted, [item["id"] for item in intermediates])
                self.assertIn(("GET", "actions/runs/123/artifacts?per_page=100&page=2"), calls)
                for index, (method, _) in enumerate(calls):
                    if method == "DELETE":
                        self.assertEqual(calls[index - 1], ("GET", "actions/runs/123"))

    def test_completed_failed_cancelled_wrong_workflow_and_repository_preserve_everything(self):
        for changes in ({"status": "completed"}, {"status": "queued"}, {"conclusion": "failure"},
                        {"conclusion": "cancelled"}, {"path": ".github/workflows/other.yml"},
                        {"event": "pull_request_target"}):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, "retained artifacts"):
                self.exercise([], {**run(), **changes})
        with self.assertRaises(ValueError):
            cleanup.cleanup("Other/service", run())

    def test_rerun_source_or_workflow_change_before_delete_preserves_artifacts(self):
        items = [artifact(1, "mrf-rust-coverage-123-2"), artifact(2, "healthcare-public-measurement-123-2", 90)]
        for changes in ({"run_attempt": 3}, {"status": "completed"}, {"head_sha": "c" * 40},
                        {"workflow_id": 999}, {"repository": {"full_name": "Other/service"}}):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, "stopped after 0 deletions"):
                self.exercise(items, refresh=changes)

    def test_consumers_must_all_finish_successfully_in_the_same_attempt_before_each_delete(self):
        items = [artifact(1, "mrf-rust-coverage-123-2"), artifact(2, "healthcare-public-measurement-123-2", 90)]
        for changes in ({"status": "in_progress", "conclusion": None}, {"conclusion": "failure"},
                        {"conclusion": "cancelled"}, {"conclusion": "skipped"}, {"run_attempt": 1},
                        {"run_id": 999}, {"head_sha": "c" * 40}):
            changed = jobs()
            changed[0].update(changes)
            for option in ("job_inventory", "refreshed_jobs"):
                with self.subTest(changes=changes, option=option), self.assertRaises(ValueError):
                    self.exercise(items, **{option: changed})
        for changed in ([], jobs()[:-1], [jobs()[-1]], jobs() + [jobs()[0]],
                        [{**item, "status": "completed", "conclusion": "success"} for item in jobs()]):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                self.exercise(items, job_inventory=changed)
        changed = jobs()
        changed[0]["id"] = 999
        with self.assertRaises(ValueError):
            self.exercise(items, refreshed_jobs=changed)

    def test_final_measurement_must_exist_and_remain_durable(self):
        temporary = artifact(1, "mrf-rust-coverage-123-2")
        final = artifact(2, "healthcare-public-measurement-123-2", 90)
        for replacement in ([], [artifact(2, final["name"], 1)], [{**final, "expired": True}],
                            [{**final, "size_in_bytes": 0}], [final, {**final, "id": 3}]):
            with self.subTest(replacement=replacement), self.assertRaises(ValueError):
                self.exercise([temporary, *replacement])
        for changes in ({"name": "unknown"}, {"expired": True}, {"digest": "sha256:" + "c" * 64}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.exercise([temporary, final], changed_artifact=(2, changes))
        with self.assertRaises(OSError):
            self.exercise([temporary, final], delete_error=True)

    def test_conflicting_producer_metadata_is_preserved(self):
        for producer in ({"id": 999}, {"id": 123, "head_sha": "c" * 40}):
            item = {**artifact(1, "mrf-rust-coverage-123-2"), "workflow_run": producer}
            self.assertEqual(self.exercise([item])[0], [])

    def test_delete_api_requires_confirmed_204_and_propagates_errors(self):
        with patch.dict("os.environ", {"GH_TOKEN": "synthetic"}), patch.object(
                cleanup.github.urllib.request, "urlopen") as request:
            response = request.return_value.__enter__.return_value
            response.status = 204
            self.assertIsNone(cleanup.github.api("EndurantDevs/drug-api", "actions/artifacts/1", "DELETE"))
            self.assertEqual(request.call_args.args[0].get_method(), "DELETE")
            response.read.assert_not_called()
            response.status = 200
            with self.assertRaises(ValueError):
                cleanup.github.api("EndurantDevs/drug-api", "actions/artifacts/1", "DELETE")
            request.side_effect = OSError("synthetic network failure")
            with self.assertRaises(OSError):
                cleanup.github.api("EndurantDevs/drug-api", "actions/artifacts/1", "DELETE")

    def test_main_binds_own_run_and_source_event_and_skips_fork_deletion(self):
        repository = "EndurantDevs/drug-api"
        payload = {"repository": {"full_name": repository, "private": False},
                   "pull_request": {"number": 7, "head": {"sha": "a" * 40, "ref": "fix/example",
                                    "repo": {"full_name": repository}},
                                    "base": {"ref": "dev", "repo": {"full_name": repository}}}}
        with tempfile.TemporaryDirectory() as temporary:
            event = Path(temporary) / "event.json"
            event.write_text(json.dumps(payload))
            environment = {"GITHUB_EVENT_PATH": str(event), "GITHUB_EVENT_NAME": "pull_request",
                           "GITHUB_REF": "refs/pull/7/merge", "GITHUB_REPOSITORY": repository,
                           "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "2", "GITHUB_JOB": "artifact-cleanup"}
            with patch.dict("os.environ", environment), patch.object(cleanup, "cleanup") as execute:
                cleanup.main()
                execute.assert_called_once_with(repository, {"id": 123, "run_attempt": 2,
                    "event": "pull_request", "head_sha": "a" * 40, "head_branch": "fix/example",
                    "path": ".github/workflows/ci.yml"})
            for changes in ({"GITHUB_EVENT_NAME": "workflow_run"}, {"GITHUB_REF": "refs/pull/1/merge"},
                            {"GITHUB_REPOSITORY": "Other/service"}, {"GITHUB_JOB": "validate"}):
                with patch.dict("os.environ", {**environment, **changes}), self.assertRaises(ValueError):
                    cleanup.main()
            payload["pull_request"]["head"]["repo"]["full_name"] = "Other/drug-api"
            event.write_text(json.dumps(payload))
            with patch.dict("os.environ", environment), patch.object(cleanup, "cleanup") as execute, \
                    patch("sys.stdout", new=io.StringIO()):
                cleanup.main()
                execute.assert_not_called()
            for branch in ("main", "dev"):
                push = {"repository": payload["repository"], "ref": f"refs/heads/{branch}", "after": "b" * 40}
                event.write_text(json.dumps(push))
                environment.update(GITHUB_EVENT_NAME="push", GITHUB_REF=push["ref"], GITHUB_SHA=push["after"])
                with patch.dict("os.environ", environment), patch.object(cleanup, "cleanup") as execute:
                    cleanup.main()
                    self.assertEqual(execute.call_args.args[1]["head_sha"], "b" * 40)
                    self.assertEqual(execute.call_args.args[1]["head_branch"], branch)
                with patch.dict("os.environ", {**environment, "GITHUB_SHA": "c" * 40}), self.assertRaises(ValueError):
                    cleanup.main()

    def test_current_upload_allowlist_and_retention(self):
        for repository, kind in cleanup.KINDS.items():
            workflow = yaml.load((ROOT / f".github/workflows/{kind}.yml").read_text(), Loader=yaml.BaseLoader)
            names = set()
            for definition in workflow["jobs"].values():
                for step in definition.get("steps", []):
                    if not step.get("uses", "").startswith("actions/upload-artifact@"):
                        continue
                    upload = step["with"]
                    if "public-measurement" in upload["name"]:
                        self.assertEqual(upload["retention-days"], "90")
                        continue
                    self.assertEqual(upload["retention-days"], "1")
                    for row in definition.get("strategy", {}).get("matrix", {}).get("include", [{}]):
                        names.add(upload["name"].replace("${{ github.run_id }}", "123")
                                  .replace("${{ github.run_attempt }}", "2")
                                  .replace("${{ matrix.shard }}", row.get("shard", "")))
            self.assertEqual(names, cleanup.temporary_names(kind, run(repository)))


if __name__ == "__main__":
    unittest.main()
