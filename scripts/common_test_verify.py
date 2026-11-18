#!/usr/bin/env python3
"""Recheck immutable common-test origins from a separate trusted final job."""

import argparse
import hashlib
import http.client
import io
import json
import os
import re
import sys
import tempfile
import threading
import time
import urllib.error
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_HELPER_CACHE = tempfile.TemporaryDirectory(prefix="common-test-helper-cache-")
sys.pycache_prefix = _HELPER_CACHE.name
sys.dont_write_bytecode = True

import common_test_result as engine

EXECUTION_FIELDS = engine.EXECUTION_FIELDS


def original_execution(records):
    return engine.original_execution(records)


def selected_producers(run, repository):
    return engine.selected_producers(run, repository)


def coverage_reference(document, data):
    current = document["measurement"]
    result = {
        name: current[name]
        for name in ("schema", "source", "producer", "fingerprint", "executed")
    }
    result["measurement_sha256"] = hashlib.sha256(data).hexdigest()
    if document["origin"] is not None:
        proof = document["origin"]
        result["origin"] = {
            name: proof["origin"][name]
            for name in ("source", "producer", "fingerprint")
        }
        result["origin"].update(
            artifact_id=proof["artifact_id"], archive_sha256=proof["archive_sha256"]
        )
    return result


def current_source_matches(actual, expected, run, repository):
    if actual == expected:
        return True
    # A native PR check may execute GitHub's merge checkout. Keep both SHAs,
    # and accept that checkout only after independently proving identical trees.
    if not (
        actual.get("repository_id")
        == expected.get("repository_id")
        == run.get("repository", {}).get("id")
        and expected.get("commit") == run.get("head_sha")
        and actual.get("tree") == expected.get("tree")
    ):
        return False
    for source in (actual, expected):
        commit = engine.api(repository, "git/commits/" + source["commit"])
        if (
            commit.get("sha") != source["commit"]
            or commit.get("tree", {}).get("sha") != source["tree"]
        ):
            return False
    return True


_LIVE_RUN_FIELDS = (
    "id",
    "run_attempt",
    "head_sha",
    "status",
    "conclusion",
    "created_at",
    "repository",
    "event",
    "workflow_id",
    "path",
    "check_suite_id",
)


_RUN_CHILD_RE = re.compile(
    r"^actions/runs/([1-9][0-9]*)/(?:jobs(?:\?.*)?|artifacts(?:\?.*)?|attempts/[1-9][0-9]*)$"
)


def _with_transient_retry(call, *args):
    """Retry idempotent read-only GitHub API and artifact downloads on transient 5xx/network errors."""
    for attempt in range(2):
        try:
            return call(*args)
        except urllib.error.HTTPError as error:
            if error.code not in (500, 502, 503, 504):
                raise
            error.close()
        except (
            urllib.error.URLError,
            TimeoutError,
            http.client.HTTPException,
            ConnectionError,
        ):
            pass
        time.sleep(0.5 * (attempt + 1))
    return call(*args)


def _memoized_api_reader(original_api, current_run_id):
    """Deduplicate immutable Git and original workflow-run reads within one verify pass."""
    lock = threading.Lock()
    entries = {}
    run_snapshots = {}
    current_prefix = str(current_run_id)

    def cache_bucket(path):
        if path.startswith(("git/commits/", "git/trees/")):
            return "immutable"
        if re.fullmatch(r"actions/runs/[1-9][0-9]*", path):
            return "run"
        match = _RUN_CHILD_RE.match(path)
        if match is not None and match.group(1) != current_prefix:
            return "run_child"
        return None

    def read(repository, path):
        bucket = cache_bucket(path)
        if bucket is None:
            return _with_transient_retry(original_api, repository, path)
        key = (repository, path)
        with lock:
            entry = entries.get(key)
            if entry is None:
                entry = [threading.Event(), None, None]
                entries[key] = entry
                owner = True
            else:
                owner = False
        if not owner:
            entry[0].wait()
            if entry[2] is not None:
                raise entry[2]
            return entry[1]
        try:
            value = _with_transient_retry(original_api, repository, path)
        except BaseException as error:
            with lock:
                if entries.get(key) is entry:
                    del entries[key]
            entry[2] = error
            entry[0].set()
            raise
        if bucket == "run":
            with lock:
                run_snapshots[key] = value
        entry[1] = value
        entry[0].set()
        return value

    def verify_live_runs():
        for (repository, path), cached in list(run_snapshots.items()):
            fresh = _with_transient_retry(original_api, repository, path)
            engine.equivalent.require(
                isinstance(fresh, dict)
                and isinstance(cached, dict)
                and all(fresh.get(field) == cached.get(field) for field in _LIVE_RUN_FIELDS),
                "Original common-test workflow run changed during verification",
            )

    return read, verify_live_runs


def _verify_selected_artifact(
    item, run, repository, expected_source, authorize_source
):
    artifact, attempt, job_id, producer_run = item
    data = engine.download(repository, artifact["id"])
    digest = hashlib.sha256(data).hexdigest()
    engine.equivalent.require(
        artifact.get("digest") == "sha256:" + digest,
        "Current common-test immutable archive digest changed",
    )
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = archive.namelist()
        engine.equivalent.require(
            len(names) == len(set(names)) and len(names) <= 1000,
            "Current common-test archive is ambiguous or oversized",
        )
        documents = {}
        for entry in archive.infolist():
            if re.fullmatch(r"[0-9a-f]{64}/measurement\.json", entry.filename):
                engine.equivalent.require(
                    entry.file_size <= 4 * 1024 * 1024,
                    "Current common-test measurement is oversized",
                )
                raw = archive.read(entry)
                documents[entry.filename.split("/")[0]] = json.loads(raw), raw
    engine.equivalent.require(
        documents, "Current common-test archive has no complete scope"
    )
    artifact_references, artifact_coverage = [], []
    for key, (document, raw_document) in documents.items():
        measurement = document["measurement"]
        engine.equivalent.require(
            current_source_matches(
                measurement["source"], expected_source, run, repository
            )
            and measurement["producer"]["run_id"] == run["id"]
            and measurement["producer"]["run_attempt"] == attempt
            and measurement["producer"]["job_id"] == job_id
            and measurement["producer"]["commit"] == run["head_sha"]
            and key == hashlib.sha256(measurement["scope"]["name"].encode()).hexdigest(),
            "Current common-test scope/source/attempt is inconsistent or duplicated",
        )
        engine.archive_document(
            data, key, [output["path"] for output in document["outputs"]]
        )
        reference = engine.refresh_document(
            document,
            artifact,
            digest,
            producer_run,
            repository,
            authorize_source=authorize_source,
        )
        if reference is not None:
            artifact_references.append(reference)
        if any(
            Path(output["path"]).name.startswith(".coverage")
            or Path(output["path"]).name == "test-coverage-rust.json"
            for output in document["outputs"]
        ):
            artifact_coverage.append(coverage_reference(document, raw_document))
    return tuple(documents), artifact_references, artifact_coverage


def verify(
    run,
    repository,
    expected_source,
    *,
    authorize_source=None,
    producer_attempts=None,
    coverage_references=None,
):
    selected, attempt_runs, producers = [], {run["run_attempt"]: run}, set()
    original_api = engine.api
    original_download = engine.download
    memoized_api, verify_live_runs = _memoized_api_reader(original_api, run["id"])
    engine.api = memoized_api
    engine.download = lambda target_repository, identifier: _with_transient_retry(
        original_download, target_repository, identifier
    )
    try:
        if producer_attempts is None:
            producer_attempts = selected_producers(run, repository)
        engine.equivalent.require(
            isinstance(producer_attempts, dict)
            and all(
                type(job) is int
                and job > 0
                and type(attempt) is int
                and 0 < attempt <= run["run_attempt"]
                for job, attempt in producer_attempts.items()
            ),
            "Selected native producer identities are invalid",
        )
        for artifact in engine.artifacts(repository, run["id"]):
            match = re.fullmatch(
                rf"common-tests-{run['id']}-([1-9][0-9]*)-([1-9][0-9]*)",
                artifact.get("name", ""),
            )
            if match is None:
                continue
            attempt, job_id = map(int, match.groups())
            if producer_attempts.get(job_id) != attempt:
                continue
            engine.equivalent.require(
                job_id not in producers,
                "Selected producer has duplicate immutable common artifacts",
            )
            producers.add(job_id)
            engine.equivalent.require(
                attempt <= run["run_attempt"], "Current producer is from a future attempt"
            )
            if attempt not in attempt_runs:
                producer_run = engine.api(
                    repository, f"actions/runs/{run['id']}/attempts/{attempt}"
                )
                engine.equivalent.require(
                    producer_run.get("id") == run["id"]
                    and producer_run.get("run_attempt") == attempt
                    and producer_run.get("head_sha") == run["head_sha"],
                    "Selected retained producer attempt changed",
                )
                attempt_runs[attempt] = producer_run
            selected.append((artifact, attempt, job_id, attempt_runs[attempt]))
        if len(selected) <= 1:
            results = [
                _verify_selected_artifact(
                    item, run, repository, expected_source, authorize_source
                )
                for item in selected
            ]
        else:
            with ThreadPoolExecutor(max_workers=min(8, len(selected))) as pool:
                results = list(
                    pool.map(
                        lambda item: _verify_selected_artifact(
                            item, run, repository, expected_source, authorize_source
                        ),
                        selected,
                    )
                )
        verify_live_runs()
    finally:
        engine.api = original_api
        engine.download = original_download
    references, scopes, coverage = [], set(), []
    for keys, artifact_references, artifact_coverage in results:
        for key in keys:
            engine.equivalent.require(
                key not in scopes,
                "Current common-test scope/source/attempt is inconsistent or duplicated",
            )
            scopes.add(key)
        references.extend(artifact_references)
        coverage.extend(artifact_coverage)
    engine.equivalent.require(
        producers == set(producer_attempts),
        "A selected successful common-test upload has no immutable artifact",
    )
    if coverage_references is not None:
        engine.equivalent.require(
            isinstance(coverage_references, list)
            and sorted(engine.equivalent.encoded(item) for item in coverage_references)
            == sorted(engine.equivalent.encoded(item) for item in coverage),
            "Coverage provenance does not bind every actual native common-test measurement",
        )
    return references


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-repository", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    repository = os.environ["GITHUB_REPOSITORY"]
    run = engine.api(repository, "actions/runs/" + os.environ["GITHUB_RUN_ID"])
    engine.equivalent.require(
        run.get("run_attempt") == int(os.environ["GITHUB_RUN_ATTEMPT"])
        and arguments.source_repository == repository,
        "Current verifier source/attempt changed",
    )
    source = engine.api(
        arguments.source_repository, "git/commits/" + arguments.source_sha
    )
    identity = engine.api(arguments.source_repository, "")
    expected = {
        "repository_id": identity["id"],
        "commit": arguments.source_sha,
        "tree": source["tree"]["sha"],
    }
    references = verify(
        run, repository, expected, producer_attempts=selected_producers(run, repository)
    )
    arguments.output.write_text(
        json.dumps(
            {
                "schema": "common-test-current-verification-v1",
                "source": expected,
                "run_id": run["id"],
                "run_attempt": run["run_attempt"],
                "origins": references,
            },
            sort_keys=True,
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
