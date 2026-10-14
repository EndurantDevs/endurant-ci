"""Keep retained native producers subject to fresh immutable-origin verification."""

import copy
import hashlib
import importlib.util
import io
import json
import sys
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import test_common_test_result as fixtures

SCRIPTS = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location(
    "common_test_verify", SCRIPTS / "common_test_verify.py"
)
verify = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verify)
common = fixtures.common
verify.engine = common
REPOSITORY = "example/project"


class SelectedProducerTests(unittest.TestCase):
    def setUp(self):
        self.run = {"id": 21, "run_attempt": 2, "head_sha": "a" * 40}

    def job(self, identifier=32, attempt=1, name="Complete unit tests", **changes):
        return {
            "id": identifier,
            "run_id": 21,
            "run_attempt": attempt,
            "head_sha": self.run["head_sha"],
            "name": name,
            "status": "completed",
            "conclusion": "success",
            "steps": [
                {
                    "name": common.UPLOAD_STEP,
                    "status": "completed",
                    "conclusion": "success",
                }
            ],
            **changes,
        }

    def select(self, rows):
        with patch.object(common, "api", return_value={"jobs": rows}):
            return verify.selected_producers(self.run, REPOSITORY)

    def test_selects_highest_attempt_per_fixed_name_and_retains_other_jobs(self):
        first = [
            self.job(32, 1),
            self.job(33, 2),
            self.job(34, 1, "Complete database tests"),
        ]
        self.assertEqual(self.select(first), {33: 2, 34: 1})
        self.assertEqual(self.select(list(reversed(first))), {33: 2, 34: 1})

    def test_newer_failed_skipped_or_incomplete_job_never_revives_older_success(self):
        for changes in (
            {"conclusion": "failure"},
            {"conclusion": "skipped"},
            {"status": "in_progress", "conclusion": None},
            {"steps": []},
            {
                "steps": [
                    {
                        "name": common.UPLOAD_STEP,
                        "status": "completed",
                        "conclusion": "failure",
                    }
                ]
            },
        ):
            with self.subTest(changes=changes):
                self.assertEqual(
                    self.select([self.job(), self.job(33, 2, **changes)]), {}
                )

    def test_rejects_ambiguous_or_foreign_native_job_identity(self):
        for rows in (
            [self.job(), self.job()],
            [self.job(), self.job(33)],
            [self.job(run_id=22)],
            [self.job(head_sha="b" * 40)],
            [self.job(attempt=3)],
            [self.job(attempt=True)],
            [self.job(identifier=True)],
            [self.job(name="")],
        ):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                self.select(rows)

    def test_paginates_all_attempts_before_choosing_latest_job(self):
        first = [
            self.job(100 + index, 1, f"Complete test {index}") for index in range(100)
        ]
        second = [self.job(300, 2, "Complete test 0", conclusion="failure")]
        with patch.object(
            common, "api", side_effect=[{"jobs": first}, {"jobs": second}]
        ) as read:
            selected = verify.selected_producers(self.run, REPOSITORY)
        self.assertEqual(len(selected), 99)
        self.assertNotIn(100, selected)
        self.assertNotIn(300, selected)
        self.assertEqual(
            [call.args[1] for call in read.call_args_list],
            [
                "actions/runs/21/jobs?filter=all&per_page=100&page=1",
                "actions/runs/21/jobs?filter=all&per_page=100&page=2",
            ],
        )

    def test_rejects_inventory_beyond_bounded_window(self):
        pages = [
            {
                "jobs": [
                    self.job(
                        100 + page * 100 + index, 1, f"Complete test {page}-{index}"
                    )
                    for index in range(100)
                ]
            }
            for page in range(10)
        ]
        with (
            patch.object(common, "api", side_effect=pages),
            self.assertRaises(ValueError),
        ):
            verify.selected_producers(self.run, REPOSITORY)


class RetainedAttemptVerificationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.CommonResultIntegrationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        fixture = self.fixture
        fixture.arguments.output_file.append(".coverage")
        fixture.arguments.command[-1] += (
            "; Path('.coverage').write_bytes(b'complete branch fixture')"
        )
        fixture.original_execution()
        self.original_archive = fixture.archive
        self.current_sha = fixture.git("rev-parse", "HEAD").strip()
        producer = {
            **fixture.producer,
            "commit": self.current_sha,
            "run_id": 21,
            "job_id": 32,
        }
        self.retained = {
            **fixture.run,
            "id": 21,
            "head_sha": self.current_sha,
            "check_suite_id": 41,
            "conclusion": "failure",
        }
        fixture.current_producer.return_value = (producer, self.retained)
        self.assertEqual(
            common.execute_isolated(fixture.arguments, read=fixture.read), 0
        )
        self.document = json.loads(
            next(fixture.directory.glob("*/measurement.json")).read_text()
        )
        self.assertFalse(self.document["measurement"]["executed"])
        self.current_archive = self.archive()
        self.latest = {
            **self.retained,
            "run_attempt": 2,
            "check_suite_id": 42,
            "status": "in_progress",
            "conclusion": None,
        }
        self.current_job = {
            **fixture.job,
            "id": 32,
            "run_id": 21,
            "head_sha": self.current_sha,
            "check_run_url": "https://api.github.com/repos/example/project/check-runs/33",
        }
        self.current_check = {
            **fixture.check,
            "id": 33,
            "head_sha": self.current_sha,
            "check_suite": {"id": 41},
            "details_url": "https://github.com/example/project/actions/runs/21/job/32",
        }
        self.current_artifact = {
            "id": 51,
            "name": common.artifact_name(producer),
            "expired": False,
            "digest": "sha256:" + hashlib.sha256(self.current_archive).hexdigest(),
            "created_at": fixture.now,
            "workflow_run": {"id": 21, "head_sha": self.current_sha},
        }
        self.artifacts = [self.current_artifact]
        self.reads = []
        for name, replacement in (("api", self.read), ("download", self.download)):
            active = patch.object(common, name, side_effect=replacement)
            active.start()
            self.addCleanup(active.stop)

    def archive(self, document=None):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            for path in self.fixture.directory.rglob("*"):
                if path.is_file():
                    value = (
                        json.dumps(document).encode()
                        if document is not None and path.name == "measurement.json"
                        else path.read_bytes()
                    )
                    archive.writestr(
                        path.relative_to(self.fixture.directory).as_posix(), value
                    )
        return output.getvalue()

    def read(self, repository, path):
        self.assertEqual(repository, REPOSITORY)
        self.reads.append(path)
        values = {
            "actions/runs/21/artifacts?per_page=100": {"artifacts": self.artifacts},
            "actions/runs/21/jobs?filter=all&per_page=100&page=1": {
                "jobs": [self.current_job]
            },
            "actions/runs/21/attempts/1": self.retained,
            "actions/jobs/32": self.current_job,
            "check-runs/33": self.current_check,
            "git/commits/" + self.current_sha: {
                "sha": self.current_sha,
                "tree": {"sha": self.fixture.tree},
            },
        }
        if path in values:
            return values[path]
        if path == "actions/artifacts/50":
            return self.fixture.read(
                repository, "actions/runs/20/artifacts?per_page=100"
            )["artifacts"][0]
        return self.fixture.read(repository, path)

    def download(self, repository, identifier):
        self.assertEqual(repository, REPOSITORY)
        self.assertIn(identifier, {50, 51})
        return self.original_archive if identifier == 50 else self.current_archive

    def refresh(self, selected=None):
        return verify.verify(
            self.latest,
            REPOSITORY,
            self.document["measurement"]["source"],
            producer_attempts={32: 1} if selected is None else selected,
        )

    def test_selected_retained_attempt_refreshes_original_with_exact_historical_native_run(
        self,
    ):
        references = self.refresh()
        self.assertEqual(len(references), 1)
        self.assertEqual(references[0]["artifact_id"], 50)
        self.assertEqual(references[0]["source"]["commit"], self.fixture.source_sha)
        for path in (
            "actions/runs/21/attempts/1",
            "actions/jobs/32",
            "check-runs/33",
            "actions/runs/20",
            "actions/jobs/30",
            "check-runs/31",
            "actions/artifacts/50",
        ):
            self.assertIn(path, self.reads)

    def test_equal_tree_merge_checkout_keeps_actual_and_native_commits_distinct(self):
        expected = copy.deepcopy(self.document["measurement"]["source"])
        merge = "b" * 40
        document = copy.deepcopy(self.document)
        document["measurement"]["source"]["commit"] = merge
        document["origin"]["source"]["commit"] = merge
        self.current_archive = self.archive(document)
        self.current_artifact["digest"] = (
            "sha256:" + hashlib.sha256(self.current_archive).hexdigest()
        )
        read = self.read

        def proof(repository, path):
            if path == "git/commits/" + merge:
                return {"sha": merge, "tree": {"sha": expected["tree"]}}
            return read(repository, path)

        with patch.object(common, "api", side_effect=proof):
            references = verify.verify(
                self.latest, REPOSITORY, expected, producer_attempts={32: 1}
            )
        self.assertEqual(references[0]["source"]["commit"], self.fixture.source_sha)
        self.assertEqual(document["measurement"]["source"]["commit"], merge)
        self.assertEqual(expected["commit"], self.current_sha)

        def changed(repository, path):
            if path == "git/commits/" + merge:
                return {"sha": merge, "tree": {"sha": "c" * 40}}
            return read(repository, path)

        with (
            patch.object(common, "api", side_effect=changed),
            self.assertRaisesRegex(ValueError, "scope/source"),
        ):
            verify.verify(self.latest, REPOSITORY, expected, producer_attempts={32: 1})

    def test_default_consumer_derives_native_selected_map_including_retained_job(self):
        references = verify.verify(
            self.latest, REPOSITORY, self.document["measurement"]["source"]
        )
        self.assertEqual(references[0]["artifact_id"], 50)
        self.assertIn("actions/runs/21/jobs?filter=all&per_page=100&page=1", self.reads)
        self.assertIn("actions/runs/21/attempts/1", self.reads)
        self.assertEqual(self.refresh({}), [])

    def test_selected_successful_upload_requires_exactly_one_current_artifact(self):
        original = self.artifacts
        for rows in (
            [],
            [{**self.current_artifact, "name": "common-tests-21-2-32"}],
            [{**self.current_artifact, "expired": True}],
            [self.current_artifact, {**self.current_artifact, "id": 52}],
        ):
            with self.subTest(artifacts=rows):
                self.artifacts = rows
                with self.assertRaises(ValueError):
                    self.refresh()
        self.artifacts = original
        for selected in ({32: 2}, {True: 1}, {32: True}, {32: 3}, {"32": 1}):
            with self.subTest(selected=selected), self.assertRaises(ValueError):
                self.refresh(selected)

    def test_current_artifact_after_first_page_is_still_fully_refreshed(self):
        unrelated = [
            {"id": 100 + index, "name": f"other-output-{index}"} for index in range(100)
        ]

        def read(repository, path):
            if path == "actions/runs/21/artifacts?per_page=100":
                return {"artifacts": unrelated}
            if path == "actions/runs/21/artifacts?per_page=100&page=2":
                return {"artifacts": [self.current_artifact]}
            return self.read(repository, path)

        with patch.object(common, "api", side_effect=read):
            self.assertEqual(self.refresh()[0]["artifact_id"], 50)
        self.assertIn("actions/jobs/30", self.reads)

    def test_coverage_provenance_binds_the_current_measurement_and_original_origin(
        self,
    ):
        data = next(self.fixture.directory.glob("*/measurement.json")).read_bytes()
        reference = verify.coverage_reference(self.document, data)
        arguments = {"producer_attempts": {32: 1}, "coverage_references": [reference]}
        result = verify.verify(
            self.latest, REPOSITORY, self.document["measurement"]["source"], **arguments
        )
        self.assertEqual(result[0]["artifact_id"], 50)
        self.assertEqual(
            reference["origin"]["source"]["commit"], self.fixture.source_sha
        )
        for references in (
            [],
            [{**reference, "executed": True}],
            [{**reference, "measurement_sha256": "a" * 64}],
        ):
            with (
                self.subTest(references=references),
                self.assertRaisesRegex(ValueError, "Coverage provenance"),
            ):
                verify.verify(
                    self.latest,
                    REPOSITORY,
                    self.document["measurement"]["source"],
                    producer_attempts={32: 1},
                    coverage_references=references,
                )

    def test_retained_reuse_rejects_revoked_expired_or_superseded_original(self):
        original_run, original_check = (
            copy.deepcopy(self.fixture.run),
            copy.deepcopy(self.fixture.check),
        )
        for change in ("failed", "superseded", "expired", "foreign_app"):
            with self.subTest(change=change):
                self.fixture.run.clear()
                self.fixture.run.update(copy.deepcopy(original_run))
                self.fixture.check.clear()
                self.fixture.check.update(copy.deepcopy(original_check))
                if change == "failed":
                    self.fixture.run["conclusion"] = "failure"
                elif change == "superseded":
                    self.fixture.run["run_attempt"] = 2
                elif change == "expired":
                    self.fixture.run["created_at"] = (
                        datetime.now(timezone.utc) - timedelta(days=2)
                    ).isoformat()
                else:
                    self.fixture.check["app"] = {"id": 1}
                with self.assertRaises(ValueError):
                    self.refresh()

    def test_retained_attempt_identity_must_still_match_live_historical_run(self):
        original = copy.deepcopy(self.retained)
        for changes in (
            {"id": 22},
            {"run_attempt": 2},
            {"head_sha": "b" * 40},
            {"repository": {"id": 11, "full_name": REPOSITORY}},
        ):
            with self.subTest(changes=changes):
                self.retained.clear()
                self.retained.update({**original, **changes})
                with self.assertRaises(ValueError):
                    self.refresh()

    def test_current_archive_source_and_raw_digest_remain_bound(self):
        original = self.current_archive
        for change in ("archive_digest", "source", "native_attempt"):
            with self.subTest(change=change):
                self.current_archive = original
                self.current_artifact["digest"] = (
                    "sha256:" + hashlib.sha256(original).hexdigest()
                )
                document = copy.deepcopy(self.document)
                if change == "archive_digest":
                    self.current_archive += b"tampered"
                elif change == "source":
                    document["measurement"]["source"]["repository_id"] = 11
                    self.current_archive = self.archive(document)
                    self.current_artifact["digest"] = (
                        "sha256:" + hashlib.sha256(self.current_archive).hexdigest()
                    )
                else:
                    self.current_job["run_attempt"] = 2
                with self.assertRaises(ValueError):
                    self.refresh()


if __name__ == "__main__":
    unittest.main()
