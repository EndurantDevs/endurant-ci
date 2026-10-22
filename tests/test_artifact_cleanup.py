"""Exercise the privileged cleanup boundary with synthetic GitHub responses only."""

import hashlib
import importlib.util
import io
import json
import re
import sys
import tempfile
import threading
import unittest
import zipfile
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

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


def durable_artifact(identifier, kind, producer, suffix="measurement"):
    return {**artifact(identifier, f"{kind}-public-{suffix}-{producer['id']}-{producer['run_attempt']}", 90),
            "workflow_run": {"id": producer["id"], "head_sha": producer["head_sha"]}}


def completed_run(identifier, sha, branch, event="pull_request", conclusion="success",
                  repository="EndurantDevs/healthcare-mrf-api", created_hours=2):
    return {"id": identifier, "run_attempt": 1, "workflow_id": 456,
            "path": ".github/workflows/ci.yml", "event": event, "head_sha": sha,
            "head_branch": branch, "status": "completed", "conclusion": conclusion,
            "created_at": (datetime.now(timezone.utc) - timedelta(hours=created_hours)).isoformat(),
            "repository": {"full_name": repository}, "head_repository": {"full_name": repository}}


class JobPaginationChecks(unittest.TestCase):
    def setUp(self):
        self.run = run()
        self.repository = self.run["repository"]["full_name"]
        self.path = "actions/runs/123/attempts/2/jobs"
        self.jobs = [{**jobs()[0], "id": index + 1, "name": f"check-{index}"} for index in range(80)]

    def test_job_pages_use_thirty_rows_including_exact_multiples(self):
        for count in (0, 30, 60, 80):
            rows = self.jobs[:count]
            responses = [{"total_count": count, "jobs": rows[start:start + 30]}
                         for start in range(0, count + 1, 30)]
            with self.subTest(count=count), patch.object(cleanup.github, "api", side_effect=responses) as api:
                self.assertEqual(cleanup.attempt_jobs(self.repository, self.run, 2), rows)
            self.assertEqual([call.args for call in api.call_args_list], [
                (self.repository, f"{self.path}?per_page=30&page={page}")
                for page in range(1, len(responses) + 1)
            ])

    def test_later_job_pages_require_a_stable_integer_count_and_complete_schema(self):
        first = {"total_count": 80, "jobs": self.jobs[:30]}
        invalid = [None, [], {}, {"total_count": 80}, {"jobs": self.jobs[30:60]},
                   *({"total_count": value, "jobs": self.jobs[30:60]}
                     for value in (None, True, "80", 80.0, -1, 79, 81)),
                   *({"total_count": 80, "jobs": value}
                     for value in (None, {}, "jobs", [None] * 30, self.jobs[30:61], self.jobs[30:50], []))]
        for response in invalid:
            with self.subTest(response=response), patch.object(
                    cleanup.github, "api", side_effect=[first, response]) as api, \
                    self.assertRaisesRegex(ValueError, "job inventory"):
                cleanup.attempt_jobs(self.repository, self.run, 2)
            self.assertEqual(api.call_count, 2)

    def test_job_count_must_match_the_complete_last_page(self):
        for total, last in ((80, self.jobs[60:79]), (79, self.jobs[60:80]),
                            (60, [self.jobs[60]]), (80, [])):
            responses = [{"total_count": total, "jobs": values}
                         for values in (self.jobs[:30], self.jobs[30:60], last)]
            with self.subTest(total=total, last=len(last)), patch.object(
                    cleanup.github, "api", side_effect=responses), \
                    self.assertRaisesRegex(ValueError, "job inventory"):
                cleanup.attempt_jobs(self.repository, self.run, 2)

    def test_later_job_page_failure_prevents_artifact_access_and_deletion(self):
        failure = OSError("synthetic later page failure")
        responses = [self.run, self.run, {"total_count": 80, "jobs": self.jobs[:30]}, failure]
        with patch.object(cleanup.github, "api", side_effect=responses) as api, \
                patch.object(cleanup.time, "sleep") as sleep, self.assertRaises(OSError) as raised:
            cleanup.cleanup(self.repository, self.run)
        self.assertIs(raised.exception, failure)
        self.assertEqual([call.args for call in api.call_args_list], [
            (self.repository, "actions/runs/123"), (self.repository, "actions/runs/123"),
            (self.repository, f"{self.path}?per_page=30&page=1"),
            (self.repository, f"{self.path}?per_page=30&page=2"),
        ])
        sleep.assert_not_called()

    def test_last_job_page_cannot_duplicate_or_substitute_provenance(self):
        for change in ({"id": 1}, {"name": "check-0"}, {"run_id": 999}, {"run_attempt": 1},
                       {"run_attempt": 2.0}, {"head_sha": "b" * 40}):
            rows = deepcopy(self.jobs)
            rows[-1].update(change)
            responses = [{"total_count": 80, "jobs": rows[start:start + 30]} for start in (0, 30, 60)]
            with self.subTest(change=change), patch.object(cleanup.github, "api", side_effect=responses), \
                    self.assertRaisesRegex(ValueError, "ambiguous run-attempt provenance"):
                cleanup.attempt_jobs(self.repository, self.run, 2)

    def test_job_pagination_still_stops_after_one_hundred_full_pages(self):
        with patch.object(cleanup.github, "api", return_value={
                "total_count": 3000, "jobs": self.jobs[:30]}) as api, \
                self.assertRaisesRegex(ValueError, "bounded pagination limit"):
            list(cleanup.github.pages(self.repository, self.path, "jobs"))
        self.assertEqual(api.call_count, 100)
        self.assertEqual(api.call_args.args, (self.repository, f"{self.path}?per_page=30&page=100"))

    def test_non_job_pagination_keeps_one_hundred_row_pages(self):
        rows = [{"id": index} for index in range(120)]
        for path, key in (("actions/runs/123/artifacts", "artifacts"),
                          ("pulls?state=open", None), ("compare/example...change", "commits")):
            responses = [rows[:100], rows[100:]]
            if key:
                responses = [{key: values} for values in responses]
            with self.subTest(key=key), patch.object(cleanup.github, "api", side_effect=responses) as api:
                self.assertEqual(list(cleanup.github.pages(self.repository, path, key)), rows)
            separator = "&" if "?" in path else "?"
            self.assertEqual([call.args for call in api.call_args_list], [
                (self.repository, f"{path}{separator}per_page=100&page={page}") for page in (1, 2)
            ])


class ArtifactCleanupChecks(unittest.TestCase):
    def test_postgres_workflow_measurement_and_cleanup_inventories_match(self):
        spec = importlib.util.spec_from_file_location("measurement", ROOT / "scripts/healthcare/measurement.py")
        measurement = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(measurement)
        workflow = yaml.safe_load((ROOT / ".github/workflows/healthcare.yml").read_text())
        rows = workflow["jobs"]["address-canonical-db-tests"]["strategy"]["matrix"]["include"]
        shards = tuple(row["shard"] for row in rows)
        labels = {row["shard"]: row["label"] for row in rows}
        self.assertEqual(labels["core-imports"], "Database tests (imports)")
        self.assertEqual(labels["core-imports-2"], "Database tests (imports 2)")
        self.assertEqual(labels["core-snapshots"], "Database tests (snapshot imports)")
        self.assertEqual(labels["core-snapshots-2"], "Database tests (snapshot imports 2)")
        self.assertEqual(len(shards), len(set(shards)))
        self.assertEqual(workflow["jobs"]["address-canonical-db-tests"]["timeout-minutes"], 20)
        outputs = set(workflow["jobs"]["address-canonical-db-tests"]["outputs"])
        self.assertEqual(outputs, {row["output"] for row in rows})
        download = next(step["with"]["artifact-ids"] for step in workflow["jobs"]["measurement"]["steps"]
                        if step.get("name") == "Download immutable measurement artifacts")
        self.assertEqual(set(re.findall(r"needs\.address-canonical-db-tests\.outputs\.([a-z_0-9]+)", download)), outputs)
        self.assertEqual(measurement.POSTGRES_SHARDS, shards)
        self.assertEqual(len(measurement.producer_files()), 69)
        expected_run = run()
        names = cleanup.temporary_names("healthcare", expected_run)
        main_shards = tuple(row["shard"] for row in workflow["jobs"]["python-tests"]["strategy"]["matrix"]["include"])
        self.assertEqual(main_shards, tuple(str(index) for index in range(6)))
        main_producers = {f"mrf-python-coverage-main-{shard}" for shard in main_shards}
        self.assertEqual({name for name in measurement.producer_files() if "-main-" in name}, main_producers)
        self.assertEqual({name for name in names if name.startswith("mrf-python-coverage-main-")},
                         {f"{producer}-123-{attempt}" for producer in main_producers
                          for attempt in range(1, expected_run["run_attempt"] + 1)})
        expected = {f"mrf-python-coverage-postgres-{shard}-123-{attempt}"
                    for shard in shards for attempt in range(1, expected_run["run_attempt"] + 1)}
        self.assertEqual({name for name in names if name.startswith("mrf-python-coverage-postgres-")}, expected)

    def exercise(self, items, expected=None, refresh=None, changed_artifact=None, delete_error=False,
                  job_inventory=None, refreshed_jobs=None, job_snapshots=None, settle_waits=2,
                  missing_artifacts=(), publication_payload=None, publication_reader=None):
        expected = expected or run()
        kind = cleanup.KINDS[expected["repository"]["full_name"]]
        measurement = next((item for item in items if f"{kind}-public-measurement-" in item.get("name", "")), None)
        image = next((item for item in items if f"{kind}-public-image-staging-" in item.get("name", "")), None)
        receipt = next((item for item in items if f"{kind}-public-image-" in item.get("name", "")
                        and "-staging-" not in item.get("name", "")), None)
        expected = {**expected, "measurement_artifact_id": expected.get(
                        "measurement_artifact_id", measurement["id"] if measurement else 9999),
                    **({"image_artifact_id": expected.get("image_artifact_id", image["id"])} if image else {}),
                    **({"image_receipt_artifact_id": receipt["id"]} if receipt else {})}
        payload = publication_payload or {
            "schema": "public-source-image-v1", "repository": expected["repository"]["full_name"],
            "source_sha": expected["head_sha"], "source_branch": expected["head_branch"],
            "run_id": str(expected["id"]), "run_attempt": expected["run_attempt"],
            "producer_run_attempt": expected["run_attempt"],
            "measurement_run_attempt": expected["run_attempt"],
            "producer_artifact": {key: image[key] for key in ("id", "name", "digest")} if image else {},
        }
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
                return {**expected, **(refresh or {})} if run_reads > 4 else expected
            if path.startswith("actions/runs/123/attempts/2/jobs?"):
                job_reads += 1
                current = jobs() if job_inventory is None else job_inventory
                if job_snapshots is not None:
                    current = job_snapshots[min(job_reads - 1, len(job_snapshots) - 1)]
                if job_reads > 3 and refreshed_jobs is not None:
                    current = refreshed_jobs
                page = int(path.rsplit("=", 1)[1])
                return {"total_count": len(current), "jobs": deepcopy(current[(page - 1) * 30:page * 30])}
            if "per_page=100&page=" in path:
                self.assertFalse(deleted, "pagination must finish before deleting")
                page = int(path.rsplit("=", 1)[1])
                return {"artifacts": deepcopy(items[(page - 1) * 100:page * 100])}
            identifier = int(path.rsplit("/", 1)[1])
            if identifier in missing_artifacts:
                raise cleanup.urllib.error.HTTPError("https://api.github.test", 404, "missing", {}, None)
            result = deepcopy(next(item for item in items if item["id"] == identifier))
            result["workflow_run"] = {"id": expected["id"], "head_sha": expected["head_sha"]}
            if changed_artifact and identifier == changed_artifact[0]:
                result.update(changed_artifact[1])
            return result

        with patch.object(cleanup.github, "api", side_effect=api), patch("sys.stdout", new=io.StringIO()), \
                patch.object(cleanup.time, "sleep") as sleep, \
                patch.object(cleanup, "publication_record", return_value=payload, side_effect=publication_reader):
            try:
                count = cleanup.cleanup(expected["repository"]["full_name"], expected)
            except (ValueError, OSError):
                self.assertEqual(deleted, [], "invalid or changed evidence must prevent deletion")
                self.assertTrue(all(call.args == (2,) for call in sleep.call_args_list))
                raise
            self.assertEqual(sleep.call_args_list, [((2,),)] * settle_waits + [((1,),)] * len(deleted))
        self.assertEqual(count, len(deleted))
        return deleted, calls

    def test_per_delete_reads_are_bounded_parallel_and_complete_before_delete(self):
        barrier = threading.Barrier(4, timeout=3)
        workers, finished = set(), []
        lock = threading.Lock()

        def read(value):
            with lock:
                workers.add(threading.get_ident())
            barrier.wait()
            with lock:
                finished.append(value)
            return value

        with cleanup.ThreadPoolExecutor(max_workers=4) as executor, \
                patch.object(cleanup, "completed_consumers", side_effect=lambda *args: read("jobs")), \
                patch.object(cleanup.github, "api", side_effect=lambda repo, path: read(path)):
            values = cleanup.deletion_snapshot(executor, "repository", {}, {"id": 1}, [{"id": 2}, {"id": 3}])
        self.assertEqual(len(workers), 4)
        self.assertEqual(len(finished), 4)
        self.assertEqual(values, ["jobs", "actions/artifacts/1", "actions/artifacts/2", "actions/artifacts/3"])

    def test_parallel_read_failure_drains_all_reads_without_deletion(self):
        failure = OSError("synthetic proof read failure")
        futures = [Mock(), Mock(), Mock(), Mock()]
        for future in futures:
            future.exception.return_value = None
        futures[0].exception.return_value = failure
        executor = Mock()
        executor.submit.side_effect = futures
        with patch.object(cleanup, "wait") as wait, self.assertRaises(OSError) as raised:
            cleanup.deletion_snapshot(executor, "repository", {}, {"id": 1}, [{"id": 2}, {"id": 3}])
        self.assertIs(raised.exception, failure)
        wait.assert_called_once_with(futures)
        for future in futures:
            future.exception.assert_called_once_with()
        futures[0].result.assert_not_called()
        for future in futures[1:]:
            future.result.assert_called_once_with()
        self.assertEqual(executor.submit.call_count, 4)

    def test_eighty_one_jobs_keep_all_per_delete_json_calls_and_pacing(self):
        expected = {**run(), "event": "push", "head_branch": "dev"}
        inventory = [{**jobs()[0], "id": index + 1, "name": f"check-{index}"} for index in range(80)]
        inventory.append({**jobs()[-1], "id": 81})
        items = [artifact(1, "healthcare-public-image-staging-123-2"),
                 artifact(2, "mrf-rust-coverage-123-2"),
                 artifact(3, "healthcare-public-measurement-123-2"),
                 artifact(4, "healthcare-public-image-123-2", 90)]
        deleted, calls = self.exercise(items, expected, job_inventory=inventory)
        self.assertEqual(deleted, [1, 2])
        self.assertEqual(len(calls), 14 + 2 * 8)
        self.assertEqual(sum("/jobs?" in path for _, path in calls), 9 + 2 * 3)

    def test_missing_tested_archive_resumes_only_with_bound_publication_receipt(self):
        expected = {**run(), "event": "push", "head_branch": "dev", "image_artifact_id": 1}
        image = artifact(1, "healthcare-public-image-staging-123-2")
        payload = {"schema": "public-source-image-v1", "repository": expected["repository"]["full_name"],
                   "source_sha": expected["head_sha"], "source_branch": "dev", "run_id": "123",
                   "run_attempt": 2, "producer_run_attempt": 2, "measurement_run_attempt": 2,
                   "producer_artifact": {key: image[key] for key in ("id", "name", "digest")}}
        items = [artifact(2, "mrf-rust-coverage-123-2"),
                 artifact(3, "healthcare-public-measurement-123-2"),
                 artifact(4, "healthcare-public-image-123-2", 90)]
        deleted, _ = self.exercise(items, expected, missing_artifacts=(1,), publication_payload=payload)
        self.assertEqual(deleted, [2])
        later_attempt = {**expected, "run_attempt": 3}
        missing = cleanup.urllib.error.HTTPError("https://api.github.test", 404, "missing", {}, None)
        with patch.object(cleanup, "publication_record", return_value=payload), \
                patch.object(cleanup.github, "api", side_effect=missing):
            cleanup.require_tested_archive(expected["repository"]["full_name"], later_attempt,
                                           items, items[1], items[2])
        for field, replacement in (("repository", "Other/service"), ("source_sha", "c" * 40),
                                   ("run_id", "999"), ("run_attempt", 1), ("producer_run_attempt", 1),
                                   ("measurement_run_attempt", 1)):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "does not bind"):
                self.exercise(items, expected, missing_artifacts=(1,),
                              publication_payload={**payload, field: replacement})
        for changes in ({"id": 999}, {"name": "healthcare-public-image-staging-999-2"}, {"digest": "invalid"}):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, "does not bind"):
                self.exercise(items, expected, missing_artifacts=(1,),
                              publication_payload={**payload, "producer_artifact": {**payload["producer_artifact"], **changes}})
        with self.assertRaisesRegex(ValueError, "current publication"):
            self.exercise(items[:-1], expected, missing_artifacts=(1,), publication_payload=payload)
        for changes in ({"expired": True}, {"size_in_bytes": 0}):
            with self.subTest(proof=changes), self.assertRaisesRegex(ValueError, "current publication"):
                self.exercise([*items[:-1], {**items[-1], **changes}], expected,
                              missing_artifacts=(1,), publication_payload=payload)
        with self.assertRaisesRegex(ValueError, "publication changed"):
            self.exercise(items, expected, missing_artifacts=(1,), publication_payload=payload,
                          changed_artifact=(4, {"digest": "sha256:" + "c" * 64}))

    def test_present_archive_conflict_is_not_treated_as_absence(self):
        expected = {**run(), "event": "push", "head_branch": "dev"}
        image = artifact(1, "healthcare-public-image-staging-123-2")
        measurement = artifact(3, "healthcare-public-measurement-123-2")
        receipt = artifact(4, "healthcare-public-image-123-2", 90)
        payload = {"schema": "public-source-image-v1", "repository": expected["repository"]["full_name"],
                   "source_sha": expected["head_sha"], "source_branch": "dev", "run_id": "123",
                   "run_attempt": 2, "producer_run_attempt": 2, "measurement_run_attempt": 2,
                   "producer_artifact": {key: image[key] for key in ("id", "name", "digest")}}
        for changes in ({"id": 2}, {"digest": "sha256:" + "c" * 64}, {"expired": True}):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, "conflicts"):
                self.exercise([{**image, **changes}, measurement, receipt],
                              {**expected, "image_artifact_id": 1}, publication_payload=payload)
        with patch.object(cleanup, "publication_record", return_value=payload), \
                patch.object(cleanup.github, "api", return_value=image), \
                self.assertRaisesRegex(ValueError, "outside the exact run"):
            cleanup.require_tested_archive(expected["repository"]["full_name"],
                                           {**expected, "image_artifact_id": 1}, [], measurement, receipt)

    def test_already_observed_missing_candidate_is_idempotent(self):
        temporary = artifact(1, "mrf-rust-coverage-123-2")
        final = artifact(2, "healthcare-public-measurement-123-2")
        self.assertEqual(self.exercise([temporary, final], missing_artifacts=(1,))[0], [])
        with self.assertRaises(cleanup.urllib.error.HTTPError) as raised:
            self.exercise([temporary, final], missing_artifacts=(2,))
        raised.exception.close()

    def test_publication_zip_requires_exact_digest_and_single_bounded_record(self):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("image.json", '{"schema":"synthetic"}')
        raw = output.getvalue()
        proof = {**artifact(1, "healthcare-public-image-123-2", 90),
                 "digest": "sha256:" + hashlib.sha256(raw).hexdigest()}
        with patch.object(cleanup.subprocess, "run", return_value=cleanup.subprocess.CompletedProcess([], 0, raw)):
            self.assertEqual(cleanup.publication_record("EndurantDevs/healthcare-mrf-api", proof), {"schema": "synthetic"})
            with self.assertRaisesRegex(ValueError, "bytes changed"):
                cleanup.publication_record("EndurantDevs/healthcare-mrf-api", {**proof, "digest": "sha256:" + "c" * 64})
        for name, payload in (("foreign.json", "{}"), ("image.json", "x" * 65537)):
            output = io.BytesIO()
            with zipfile.ZipFile(output, "w") as archive:
                archive.writestr(name, payload)
            raw = output.getvalue()
            changed = {**proof, "digest": "sha256:" + hashlib.sha256(raw).hexdigest()}
            with self.subTest(name=name), patch.object(cleanup.subprocess, "run", return_value=
                    cleanup.subprocess.CompletedProcess([], 0, raw)), self.assertRaisesRegex(ValueError, "unexpected files"):
                cleanup.publication_record("EndurantDevs/healthcare-mrf-api", changed)
        for kind in ("duplicate", "symlink", "malformed", "oversized"):
            output = io.BytesIO()
            with zipfile.ZipFile(output, "w") as archive:
                entry = zipfile.ZipInfo("image.json")
                if kind == "symlink":
                    entry.external_attr = 0o120777 << 16
                archive.writestr(entry, "{}")
                if kind == "duplicate":
                    with self.assertWarns(UserWarning):
                        archive.writestr("image.json", "{}")
            raw = b"not a zip" if kind == "malformed" else output.getvalue()
            if kind == "oversized":
                raw += b"x" * 131072
            changed = {**proof, "digest": "sha256:" + hashlib.sha256(raw).hexdigest()}
            with self.subTest(kind=kind), patch.object(cleanup.subprocess, "run", return_value=
                    cleanup.subprocess.CompletedProcess([], 0, raw)), \
                    self.assertRaises((ValueError, zipfile.BadZipFile)):
                cleanup.publication_record("EndurantDevs/healthcare-mrf-api", changed)
        with patch.object(cleanup.subprocess, "run") as command, self.assertRaises(ValueError):
            cleanup.publication_record("EndurantDevs/healthcare-mrf-api", {**proof, "size_in_bytes": 131073})
        command.assert_not_called()
        with patch.object(cleanup.subprocess, "run", side_effect=cleanup.subprocess.CalledProcessError(1, ["gh"])), \
                self.assertRaises(cleanup.subprocess.CalledProcessError):
            cleanup.publication_record("EndurantDevs/healthcare-mrf-api", proof)

    def test_actual_producer_receipt_roundtrip_authorizes_absent_prior_attempt_archive(self):
        import source_image

        expected = {**run(), "event": "push", "head_branch": "dev", "run_attempt": 3, "image_artifact_id": 1}
        image = artifact(1, "healthcare-public-image-staging-123-1")
        measurement = artifact(2, "healthcare-public-measurement-123-2")
        identity = {"repository": expected["repository"]["full_name"], "source_sha": expected["head_sha"],
                    "base_sha": "b" * 40, "source_branch": "dev", "pr_number": 0,
                    "run_id": "123", "run_attempt": 2, "ci_revision": "c" * 40,
                    "caller_workflow_blob_sha": "d" * 40}
        publication = {"identity": identity, "producer_run_attempt": 1, "measurement_run_attempt": 2,
                       "artifact": image, "image": "ghcr.io/example/service:dev-example"}
        producer = {"archive_sha256": "e" * 64, "config_digest": "sha256:" + "f" * 64}
        record = source_image.publication_receipt(publication, producer, "sha256:" + "1" * 64)
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("image.json", json.dumps(record))
        raw = output.getvalue()
        receipt = {**artifact(3, "healthcare-public-image-123-2", 90),
                   "digest": "sha256:" + hashlib.sha256(raw).hexdigest()}
        missing = cleanup.urllib.error.HTTPError("https://api.github.test", 404, "missing", {}, None)
        with patch.object(cleanup.subprocess, "run", return_value=
                cleanup.subprocess.CompletedProcess([], 0, raw)) as command, \
                patch.object(cleanup.github, "api", side_effect=missing) as api:
            cleanup.require_tested_archive(identity["repository"], expected, [measurement, receipt], measurement, receipt)
        self.assertEqual(command.call_count, 1)
        api.assert_called_once_with(identity["repository"], "actions/artifacts/1")
        reader = cleanup.publication_record
        remaining = [image, artifact(4, "mrf-rust-coverage-123-2"),
                     artifact(5, "healthcare-rust-debug-123-2"), measurement, receipt]
        with patch.object(cleanup.subprocess, "run", return_value=
                cleanup.subprocess.CompletedProcess([], 0, raw)) as command:
            deleted, _ = self.exercise(remaining, {**run(), "event": "push", "head_branch": "dev"},
                                      publication_reader=reader)
        self.assertEqual(deleted, [1, 4, 5])
        self.assertEqual(command.call_count, 1)

    def test_only_known_exact_attempt_intermediates_are_deleted_after_complete_pagination(self):
        for repository, kind in cleanup.KINDS.items():
            expected = run(repository)
            names = sorted(cleanup.temporary_names(kind, expected))
            intermediates = [artifact(index + 1, name) for index, name in enumerate(names)]
            current_name = next(name for name in names if name.endswith("-123-2"))
            keep_id = len(intermediates) + 1
            keep = [artifact(keep_id, f"{kind}-public-measurement-123-2", 1),
                    artifact(keep_id + 1, current_name.removesuffix("-2") + "-3"),
                    artifact(keep_id + 2, current_name.replace("123-2", "999-2")),
                    artifact(keep_id + 3, current_name + "-unknown"),
                    artifact(keep_id + 4, current_name.removesuffix("-123-2"))]
            keep.extend(artifact(keep_id + 5 + index, f"unknown-{index}") for index in range(100))
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
        items = [artifact(1, "mrf-rust-coverage-123-2"), artifact(2, "healthcare-public-measurement-123-2", 1)]
        for changes in ({"run_attempt": 3}, {"status": "completed"}, {"head_sha": "c" * 40},
                        {"workflow_id": 999}, {"repository": {"full_name": "Other/service"}}):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, "stopped after 0 deletions"):
                self.exercise(items, refresh=changes)

    def test_consumers_must_all_finish_in_the_same_attempt_before_each_delete(self):
        items = [artifact(1, "mrf-rust-coverage-123-2"), artifact(2, "healthcare-public-measurement-123-2", 1)]
        for changes in ({"status": "in_progress", "conclusion": None}, {"status": "queued", "conclusion": None},
                        {"conclusion": None}, {"conclusion": "unknown"}, {"run_id": 999}, {"run_attempt": 1},
                        {"run_attempt": 2.0}, {"head_sha": "c" * 40}):
            changed = jobs()
            changed[0].update(changes)
            for option in ("job_inventory", "refreshed_jobs"):
                with self.subTest(changes=changes, option=option), self.assertRaises(ValueError):
                    self.exercise(items, **{option: changed})
        for changed in ([], jobs()[:-1], [jobs()[-1]], jobs() + [jobs()[0]],
                        jobs() + [{**jobs()[0], "id": 999}], jobs() + [{**jobs()[0], "name": "Other job"}],
                        [{**item, "status": "completed", "conclusion": "success"} for item in jobs()]):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                self.exercise(items, job_inventory=changed)
        changed = jobs()
        changed[0]["id"] = 999
        with self.assertRaises(ValueError):
            self.exercise(items, refreshed_jobs=changed)

    def test_failed_validation_retains_known_intermediates_for_failed_only_rerun(self):
        failed_jobs = jobs()
        failed_jobs[0]["conclusion"] = "failure"
        temporary = artifact(1, "mrf-rust-coverage-123-2")
        unknown = artifact(2, "unknown")
        self.assertEqual(self.exercise([temporary, unknown], job_inventory=failed_jobs)[0], [])

    def test_inventory_stabilizes_before_artifacts_are_considered(self):
        initial, settled = jobs(), jobs()
        settled[0]["id"] = 999
        pending = deepcopy(initial)
        pending[-1].update(status="queued", conclusion=None)
        with patch.object(cleanup.github, "api", return_value=run()), \
                patch.object(cleanup, "effective_jobs", side_effect=[pending, initial, settled, settled, settled]) as reads, \
                patch.object(cleanup.time, "sleep") as sleep:
            frozen = cleanup.stable_consumers(run()["repository"]["full_name"], run())
        self.assertEqual(frozen, sorted((j["id"], j["name"], j["run_attempt"], j["conclusion"]) for j in settled))
        self.assertEqual(reads.call_count, 5)
        self.assertEqual(sleep.call_args_list, [((2,),)] * 4)
        items = [artifact(1, "mrf-rust-coverage-123-2"), artifact(2, "healthcare-public-measurement-123-2", 1)]
        deleted, calls = self.exercise(items, job_snapshots=[pending, initial, settled, settled, settled], settle_waits=4)
        self.assertEqual(deleted, [1])
        enumeration = calls.index(("GET", "actions/runs/123/artifacts?per_page=100&page=1"))
        self.assertEqual(sum("/jobs?" in path for _, path in calls[:enumeration]), 5)

    def test_unsettled_inventory_has_a_bound_and_changed_run_stops_waiting(self):
        pending = jobs()
        pending[0].update(status="in_progress", conclusion=None)
        with patch.object(cleanup.github, "api", return_value=run()), \
                patch.object(cleanup, "effective_jobs", return_value=pending) as reads, \
                patch.object(cleanup.time, "sleep") as sleep:
            with self.assertRaisesRegex(ValueError, "did not stabilize"):
                cleanup.stable_consumers(run()["repository"]["full_name"], run())
        self.assertEqual(reads.call_count, 10)
        self.assertEqual(sleep.call_args_list, [((2,),)] * 9)
        with patch.object(cleanup.github, "api", side_effect=[run(), {**run(), "run_attempt": 3}]), \
                patch.object(cleanup, "effective_jobs", return_value=jobs()), patch.object(cleanup.time, "sleep") as sleep:
            with self.assertRaisesRegex(ValueError, "current workflow run or attempt changed"):
                cleanup.stable_consumers(run()["repository"]["full_name"], run())
        sleep.assert_called_once_with(2)

    def test_final_measurement_must_exist_and_remain_current(self):
        temporary = artifact(1, "mrf-rust-coverage-123-2")
        final = artifact(2, "healthcare-public-measurement-123-2", 1)
        for replacement in ([], [{**final, "expired": True}],
                            [{**final, "expires_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()}],
                            [{**final, "size_in_bytes": 0}],
                            [final, {**final, "id": 3}]):
            with self.subTest(replacement=replacement), self.assertRaises(ValueError):
                self.exercise([temporary, *replacement])
        for changes in ({"name": "unknown"}, {"expired": True}, {"digest": "sha256:" + "c" * 64}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.exercise([temporary, final], changed_artifact=(2, changes))
        with self.assertRaises(OSError):
            self.exercise([temporary, final], delete_error=True)

    def test_conflicting_producer_metadata_is_preserved(self):
        final = artifact(2, "healthcare-public-measurement-123-2", 1)
        for producer in ({"id": 999}, {"id": 123, "head_sha": "c" * 40}):
            item = {**artifact(1, "mrf-rust-coverage-123-2"), "workflow_run": producer}
            self.assertEqual(self.exercise([item, final])[0], [])

    def test_success_without_intermediates_still_requires_final_evidence(self):
        with self.assertRaisesRegex(ValueError, "current public measurement"):
            self.exercise([])
        expected = {**run("EndurantDevs/drug-api"), "event": "push", "head_branch": "dev",
                    "image_artifact_id": 77, "image_receipt_artifact_id": 78}
        measurement = artifact(2, "drug-public-measurement-123-2", 1)
        with self.assertRaisesRegex(ValueError, "current publication"):
            self.exercise([measurement], expected)

    def test_dev_archive_requires_current_publication_and_preserves_both_final_receipts(self):
        expected = {**run("EndurantDevs/drug-api"), "event": "push", "head_branch": "dev"}
        archive = artifact(1, "drug-public-image-staging-123-2")
        measurement = artifact(2, "drug-public-measurement-123-2", 1)
        publication = artifact(3, "drug-public-image-123-2", 1)
        for receipts in ([], [{**publication, "expired": True}], [{**publication, "size_in_bytes": 0}]):
            with self.subTest(receipts=receipts), self.assertRaisesRegex(ValueError, "current publication"):
                self.exercise([archive, measurement, *receipts], expected)
        deleted, _ = self.exercise([archive, measurement, publication], expected)
        self.assertEqual(deleted, [1])
        with self.assertRaisesRegex(ValueError, "publication changed"):
            self.exercise([archive, measurement, publication], expected, changed_artifact=(3, {"digest": "sha256:" + "c" * 64}))
        self.assertEqual(self.exercise([measurement], run("EndurantDevs/drug-api"))[0], [])
        with self.assertRaisesRegex(ValueError, "exact tested image"):
            self.exercise([archive, measurement, publication], run("EndurantDevs/drug-api"))

        failed_jobs = jobs()
        failed_jobs[0]["conclusion"] = "failure"
        self.assertEqual(self.exercise([archive], expected, job_inventory=failed_jobs)[0], [])

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

    def test_repository_metadata_request_has_no_trailing_slash(self):
        with patch.dict("os.environ", {"GH_TOKEN": "synthetic"}), patch.object(
                cleanup.github.urllib.request, "urlopen") as request:
            request.return_value.__enter__.return_value.read.return_value = b"{}"
            self.assertEqual(cleanup.github.api("EndurantDevs/drug-api", ""), {})
        self.assertEqual(request.call_args.args[0].full_url,
                         "https://api.github.com/repos/EndurantDevs/drug-api")

    def test_stale_sweep_preserves_active_and_latest_branch_evidence(self):
        repository = "EndurantDevs/healthcare-mrf-api"
        main_sha, dev_sha, open_sha = "f" * 40, "d" * 40, "e" * 40
        expected = {"id": 999, "run_attempt": 1, "workflow_id": 789,
                    "path": cleanup.STALE_CLEANUP_WORKFLOW, "event": "workflow_dispatch",
                    "head_sha": main_sha, "head_branch": "main", "status": "in_progress",
                    "conclusion": None, "repository": {"full_name": repository},
                    "head_repository": {"full_name": repository}}
        runs = {
            101: completed_run(101, "a" * 40, "fix/closed"),
            102: completed_run(102, open_sha, "fix/open"),
            103: completed_run(103, main_sha, "main", "push"),
            104: completed_run(104, "b" * 40, "dev", "push", created_hours=2),
            105: completed_run(105, dev_sha, "dev", "push", "failure", created_hours=1),
            106: completed_run(106, "c" * 40, "dev", "push", created_hours=3),
            107: {**completed_run(107, "9" * 40, "fix/running"), "status": "in_progress", "conclusion": None},
            108: completed_run(108, "8" * 40, "fix/failed", conclusion="failure"),
            109: {**completed_run(109, "7" * 40, "fix/rerun"), "run_attempt": 2},
            110: {**completed_run(110, "6" * 40, "fix/fork"),
                  "head_repository": {"full_name": "Example/fork"}},
            111: completed_run(111, "5" * 40, "dev", "push", created_hours=5),
            112: completed_run(112, "4" * 40, "dev", "push", created_hours=4),
        }
        items = [durable_artifact(index, "healthcare", producer)
                 for index, producer in enumerate(runs.values(), 1)]
        items[8]["name"] = "healthcare-public-measurement-109-1"
        items[-2]["name"] = "healthcare-public-image-111-1"
        items[-1]["name"] = "healthcare-public-image-112-1"
        items.extend([
            artifact(50, "unknown-durable", 90),
            artifact(51, "healthcare-public-measurement-999-1", 1),
            artifact(52, "healthcare-public-image-intent-999-1", 1),
            artifact(53, "healthcare-public-image-999-1", 1),
        ])
        deleted = []

        def api(actual_repository, path, method="GET"):
            self.assertEqual(actual_repository, repository)
            if method == "DELETE":
                deleted.append(int(path.rsplit("/", 1)[1]))
                return None
            if path == "actions/runs/999":
                return deepcopy(expected)
            if path == "":
                return {"full_name": repository, "private": False, "default_branch": "main"}
            if path == "branches/main":
                return {"name": "main", "commit": {"sha": main_sha}}
            if path == "branches/dev":
                return {"name": "dev", "commit": {"sha": dev_sha}}
            if path.startswith("pulls?state=open"):
                return [{"number": 7, "state": "open", "head": {"sha": open_sha,
                    "repo": {"full_name": repository}}, "base": {"repo": {"full_name": repository}}}]
            if path.startswith("actions/artifacts?per_page=100&page="):
                return {"artifacts": deepcopy(items) if path.endswith("=1") else []}
            if path.startswith("actions/runs/"):
                return deepcopy(runs[int(path.rsplit("/", 1)[1])])
            identifier = int(path.rsplit("/", 1)[1])
            return deepcopy(next(item for item in items if item["id"] == identifier))

        with patch.object(cleanup.github, "api", side_effect=api), patch.object(cleanup.time, "sleep") as sleep, \
                patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(cleanup.stale_cleanup(repository, expected), 4)
        self.assertEqual(deleted, [1, 6, 8, 11])
        self.assertEqual(sleep.call_count, 4)

    def test_stale_sweep_rechecks_open_heads_and_rejects_changed_provenance(self):
        repository = "EndurantDevs/drug-api"
        main_sha, stale_sha = "f" * 40, "a" * 40
        expected = {"id": 999, "run_attempt": 1, "workflow_id": 789,
                    "path": cleanup.STALE_CLEANUP_WORKFLOW, "event": "schedule",
                    "head_sha": main_sha, "head_branch": "main", "status": "in_progress",
                    "conclusion": None, "repository": {"full_name": repository},
                    "head_repository": {"full_name": repository}}
        producer = completed_run(101, stale_sha, "fix/closed", repository=repository)
        item = durable_artifact(1, "drug", producer)
        pr_reads = 0

        def api(actual_repository, path, method="GET"):
            nonlocal pr_reads
            self.assertEqual(actual_repository, repository)
            if method == "DELETE":
                self.fail("a head that became active must not be deleted")
            if path == "actions/runs/999": return deepcopy(expected)
            if path == "": return {"full_name": repository, "private": False, "default_branch": "main"}
            if path == "branches/main": return {"name": "main", "commit": {"sha": main_sha}}
            if path == "branches/dev": return {"name": "dev", "commit": {"sha": "d" * 40}}
            if path.startswith("pulls?state=open"):
                pr_reads += 1
                pulls = [] if pr_reads == 1 else [{"number": 7, "state": "open", "head": {
                    "sha": stale_sha, "repo": {"full_name": repository}},
                    "base": {"repo": {"full_name": repository}}}]
                return pulls
            if path.startswith("actions/artifacts?"): return {"artifacts": [deepcopy(item)]}
            if path == "actions/runs/101": return deepcopy(producer)
            if path == "actions/artifacts/1": return deepcopy(item)
            raise AssertionError(path)

        with patch.object(cleanup.github, "api", side_effect=api), patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(cleanup.stale_cleanup(repository, expected), 0)

        changed = {**item, "workflow_run": {"id": 999, "head_sha": stale_sha}}
        with patch.object(cleanup, "artifact_pages", return_value=iter([changed])), \
                patch.object(cleanup, "active_heads", return_value=({"main": main_sha, "dev": "d" * 40},
                                                                     {main_sha, "d" * 40})), \
                patch.object(cleanup.github, "api", side_effect=lambda repo, path, method="GET":
                    deepcopy(expected) if path == "actions/runs/999" else deepcopy(producer)), \
                self.assertRaisesRegex(ValueError, "unauthenticated provenance"):
            cleanup.stale_cleanup(repository, expected)

    def test_get_api_retries_only_transient_github_failures(self):
        def failure(code):
            return cleanup.github.urllib.error.HTTPError(
                "https://api.github.test", code, "synthetic", {}, None)

        with patch.dict("os.environ", {"GH_TOKEN": "synthetic"}), patch.object(
                cleanup.github.urllib.request, "urlopen") as request, patch.object(
                cleanup.github.time, "sleep") as sleep:
            response = request.return_value.__enter__.return_value
            response.read.return_value = b"{}"
            request.side_effect = [failure(500), request.return_value]
            self.assertEqual(cleanup.github.api("EndurantDevs/drug-api", "actions/runs/1"), {})
            self.assertEqual([item.args for item in sleep.call_args_list], [(1,)])

        for method, code in (("GET", 429), ("DELETE", 500)):
            with self.subTest(method=method, code=code), patch.dict(
                    "os.environ", {"GH_TOKEN": "synthetic"}), patch.object(
                    cleanup.github.urllib.request, "urlopen", side_effect=failure(code)) as request, \
                    patch.object(cleanup.github.time, "sleep") as sleep, self.assertRaises(
                    cleanup.github.urllib.error.HTTPError) as raised:
                cleanup.github.api("EndurantDevs/drug-api", "actions/runs/1", method)
            raised.exception.close()
            self.assertEqual(request.call_count, 1)
            sleep.assert_not_called()

        with patch.dict("os.environ", {"GH_TOKEN": "synthetic"}), patch.object(
                cleanup.github.urllib.request, "urlopen", side_effect=[failure(503) for _ in range(3)]
                ) as request, patch.object(cleanup.github.time, "sleep") as sleep, self.assertRaises(
                cleanup.github.urllib.error.HTTPError) as raised:
            cleanup.github.api("EndurantDevs/drug-api", "actions/runs/1")
        raised.exception.close()
        self.assertEqual(request.call_count, 3)
        self.assertEqual([item.args for item in sleep.call_args_list], [(1,), (2,)])

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
                           "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "2", "GITHUB_JOB": "artifact-cleanup",
                           "MEASUREMENT_ARTIFACT_ID": "55"}
            with patch.dict("os.environ", environment), patch.object(cleanup, "cleanup") as execute:
                cleanup.main()
                execute.assert_called_once_with(repository, {"id": 123, "run_attempt": 2,
                    "event": "pull_request", "head_sha": "a" * 40, "head_branch": "fix/example",
                    "path": ".github/workflows/ci.yml", "measurement_artifact_id": 55})
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
                if branch == "dev":
                    environment.update(IMAGE_ARTIFACT_ID="44", IMAGE_RECEIPT_ARTIFACT_ID="66")
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
                    durable = kind == "drug" and step.get("name") == "Upload public measurement"
                    self.assertEqual(upload["retention-days"], "90" if durable else "1")
                    if step.get("name") == "Upload verified common-test measurements":
                        continue  # Expiring common-cache data must survive intermediate cleanup.
                    if upload["name"].startswith("healthcare-rust-debug-"):
                        self.assertEqual(upload["compression-level"], "6")
                    if "public-measurement" in upload["name"]:
                        continue
                    for row in definition.get("strategy", {}).get("matrix", {}).get("include", [{}]):
                        names.add(upload["name"].replace("${{ github.run_id }}", "123")
                                  .replace("${{ github.run_attempt }}", "2")
                                  .replace("${{ matrix.shard }}", row.get("shard", "")))
            expected = cleanup.temporary_names(kind, {**run(repository), "event": "push", "head_branch": "dev"})
            self.assertEqual(names, {name for name in expected if name.endswith("-123-2")})


if __name__ == "__main__":
    unittest.main()
