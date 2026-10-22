#!/usr/bin/env python3
"""Recheck immutable common-test origins from a separate trusted final job."""

import argparse
import hashlib
import io
import json
import os
import re
import sys
import tempfile
import zipfile
from pathlib import Path

_HELPER_CACHE = tempfile.TemporaryDirectory(prefix="common-test-helper-cache-")
sys.pycache_prefix = _HELPER_CACHE.name
sys.dont_write_bytecode = True

import common_test_result as engine


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


def verify(
    run,
    repository,
    expected_source,
    *,
    authorize_source=None,
    producer_attempts=None,
    coverage_references=None,
):
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
    references, scopes, producers, coverage = [], set(), set(), []
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
        producer_run = (
            run
            if attempt == run["run_attempt"]
            else engine.api(repository, f"actions/runs/{run['id']}/attempts/{attempt}")
        )
        engine.equivalent.require(
            producer_run.get("id") == run["id"]
            and producer_run.get("run_attempt") == attempt
            and producer_run.get("head_sha") == run["head_sha"],
            "Selected retained producer attempt changed",
        )
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
                and key
                == hashlib.sha256(measurement["scope"]["name"].encode()).hexdigest()
                and key not in scopes,
                "Current common-test scope/source/attempt is inconsistent or duplicated",
            )
            scopes.add(key)
            engine.archive_document(
                data, key, [item["path"] for item in document["outputs"]]
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
                references.append(reference)
            if any(
                Path(item["path"]).name.startswith(".coverage")
                for item in document["outputs"]
            ):
                coverage.append(coverage_reference(document, raw_document))
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


def selected_producers(run, repository):
    """Select current native job executions without reviving a superseded failed job."""
    selected, identifiers = {}, set()
    page_size = 50
    for page in range(1, 21):
        rows = engine.api(
            repository,
            f"actions/runs/{run['id']}/jobs?filter=all&per_page={page_size}&page={page}",
        )["jobs"]
        engine.equivalent.require(
            isinstance(rows, list) and len(rows) <= page_size,
            "Current native job inventory is invalid",
        )
        for job in rows:
            engine.equivalent.require(
                type(job.get("id")) is int
                and job["id"] > 0
                and job["id"] not in identifiers
                and job.get("run_id") == run["id"]
                and type(job.get("run_attempt")) is int
                and 0 < job["run_attempt"] <= run["run_attempt"]
                and job.get("head_sha") == run["head_sha"]
                and isinstance(job.get("name"), str)
                and job["name"],
                "Current native job identity is invalid or duplicated",
            )
            identifiers.add(job["id"])
            previous = selected.get(job["name"])
            engine.equivalent.require(
                previous is None or previous["run_attempt"] != job["run_attempt"],
                "Current native job name/attempt is ambiguous",
            )
            if previous is None or previous["run_attempt"] < job["run_attempt"]:
                selected[job["name"]] = job
        if len(rows) < page_size:
            break
    else:
        raise ValueError("Current native job inventory exceeds its bounded window")
    return {
        job["id"]: job["run_attempt"]
        for job in selected.values()
        if job.get("status") == "completed"
        and job.get("conclusion") == "success"
        and any(
            step.get("name") == engine.UPLOAD_STEP
            and step.get("status") == "completed"
            and step.get("conclusion") == "success"
            for step in job.get("steps", [])
        )
    }


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
