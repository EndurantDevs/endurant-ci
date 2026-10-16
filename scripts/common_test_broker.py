#!/usr/bin/env python3
"""Acquire bounded read-only test evidence before application code executes."""

import argparse
import hashlib
import http.client
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import zipfile
from datetime import datetime, timezone
from pathlib import Path

_HELPER_CACHE = tempfile.TemporaryDirectory(prefix="common-test-helper-cache-")
sys.pycache_prefix = _HELPER_CACHE.name
sys.dont_write_bytecode = True

import common_test_result as common


def prepare(arguments):
    directory = arguments.directory.resolve()
    common.equivalent.require(
        not directory.exists(), "Evidence broker destination must be fresh"
    )
    directory.mkdir(mode=0o700, parents=True)
    responses, archives = {}, {}
    real_api, real_download = common.api, common.download

    def read(repository, path):
        key = repository + "/" + path
        if key not in responses:
            responses[key] = real_api(repository, path)
        return responses[key]

    repository = os.environ["GITHUB_REPOSITORY"]
    try:
        common.api = read
        producer, _run = common.current_producer(
            repository, "Prepare read-only common-test evidence"
        )
        job = read(repository, f"actions/jobs/{producer['job_id']}")
        future = [
            step for step in job.get("steps", []) if step.get("name") == arguments.step
        ]
        active = [
            step
            for step in job.get("steps", [])
            if step.get("name") == "Prepare read-only common-test evidence"
            and step.get("status") == "in_progress"
        ]
        common.equivalent.require(
            len(future) == 1
            and len(active) == 1
            and future[0].get("status") in {"pending", "queued"}
            and future[0].get("number", 0) > active[0].get("number", 0),
            "Complete test step must be declared after the active evidence preparation",
        )
        producer["step"] = arguments.step
        source_sha = subprocess.check_output(
            ["git", "-C", str(arguments.source_root), "rev-parse", "HEAD"], text=True
        ).strip()
        tree = subprocess.check_output(
            ["git", "-C", str(arguments.source_root), "rev-parse", "HEAD^{tree}"],
            text=True,
        ).strip()
        if arguments.source_authority:
            path = arguments.source_authority
            common.equivalent.require(
                hashlib.sha256(path.read_bytes()).hexdigest()
                == arguments.source_authority_sha256,
                "Source authority differs from the current bundle digest",
            )
            authority = json.loads(path.read_text())
            source_id = authority["source_repository_id"]
            allowed = {
                (item["run_id"], item["run_attempt"], item["controller_sha"])
                for item in authority["origins"]
                if item["source_tree"] == tree
            }
        else:
            source_id = read(arguments.source_repository, "")["id"]
            allowed = None
        if source_id == producer["repository_id"] and source_sha != producer["commit"]:
            native_source = read(repository, "git/commits/" + producer["commit"])
            common.equivalent.require(
                native_source.get("sha") == producer["commit"]
                and native_source.get("tree", {}).get("sha") == tree,
                "Current executed checkout tree differs from its native workflow source",
            )
        workflow = producer["workflow"].rsplit("/", 1)[1]
        query = f"actions/workflows/{workflow}/runs?status=success&per_page=30"
        scopes = getattr(arguments, "scope", [])
        # Stock scopes cannot consume reuse evidence; keep their full execution.
        candidates = (
            []
            if scopes and not any(scope in common.CONTENT_SCOPES for scope in scopes)
            else read(repository, query)["workflow_runs"]
        )
        usable = []
        for candidate in candidates:
            if candidate["id"] == producer["run_id"]:
                continue
            if allowed is not None and not any(
                item[0] == candidate["id"] for item in allowed
            ):
                continue
            try:
                artifact_list = common.artifacts(repository, candidate["id"])
                if not any(
                    re.fullmatch(
                        rf"common-tests-{candidate['id']}-[1-9][0-9]*-[1-9][0-9]*",
                        item.get("name", ""),
                    )
                    and item.get("expired") is False
                    for item in artifact_list
                ):
                    continue
                original_run = read(repository, f"actions/runs/{candidate['id']}")
                common.equivalent.require(
                    original_run.get("id") == candidate["id"],
                    "Original run identity changed during discovery",
                )
                if (
                    allowed is not None
                    and (
                        original_run["id"],
                        original_run["run_attempt"],
                        original_run["head_sha"],
                    )
                    not in allowed
                ):
                    continue
                jobs = read(
                    repository,
                    f"actions/runs/{original_run['id']}/attempts/{original_run['run_attempt']}/jobs?per_page=100",
                )["jobs"]
                matches = [item for item in jobs if item.get("name") == job["name"]]
                common.equivalent.require(
                    len(matches) == 1, "Original complete job inventory changed"
                )
                original_job = matches[0]
                original_producer = {
                    **producer,
                    "run_id": original_run["id"],
                    "run_attempt": original_run["run_attempt"],
                    "commit": original_run["head_sha"],
                    "job_id": original_job["id"],
                }
                artifacts = [
                    item
                    for item in artifact_list
                    if item.get("name") == common.artifact_name(original_producer)
                    and item.get("expired") is False
                ]
                common.equivalent.require(
                    len(artifacts) == 1, "Original complete job measurement is absent"
                )
                artifact = artifacts[0]
                data = real_download(repository, artifact["id"])
                common.equivalent.require(
                    artifact.get("digest")
                    == "sha256:" + hashlib.sha256(data).hexdigest(),
                    "Original artifact digest changed",
                )
                with zipfile.ZipFile(io.BytesIO(data)) as archive:
                    entries = [
                        item
                        for item in archive.infolist()
                        if re.fullmatch(r"[0-9a-f]{64}/measurement.json", item.filename)
                    ]
                    common.equivalent.require(
                        entries
                        and len(entries) <= 100
                        and all(item.file_size <= 8 * 1024 * 1024 for item in entries),
                        "Original measurement scope inventory is invalid",
                    )
                    measurements = [
                        json.loads(archive.read(item))["measurement"]
                        for item in entries
                    ]
                common.equivalent.require(
                    all(
                        item["source"]["repository_id"] == source_id
                        and item["source"]["tree"] == tree
                        for item in measurements
                    ),
                    "Original measured source tree differs from the current source",
                )
                read(repository, f"actions/jobs/{original_job['id']}")
                check = re.fullmatch(
                    rf"https://api.github.com/repos/{re.escape(repository)}/check-runs/([1-9][0-9]*)",
                    original_job.get("check_run_url", ""),
                )
                common.equivalent.require(
                    check is not None,
                    "Original job has no same-repository native check",
                )
                read(repository, "check-runs/" + check[1])
                for measurement in measurements:
                    if allowed is None:
                        read(
                            arguments.source_repository,
                            "git/commits/" + measurement["source"]["commit"],
                        )
                    for name in ("recipe_checkout", "verifier_checkout"):
                        identity = measurement["inputs"][name]
                        read(
                            identity["repository"],
                            f"git/trees/{identity['tree']}?recursive=1",
                        )
                filename = str(artifact["id"]) + ".zip"
                (directory / filename).write_bytes(data)
                archives[str(artifact["id"])] = {
                    "file": filename,
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
                usable.append(candidate)
                if len(usable) == 3:
                    break
            except (
                AttributeError,
                KeyError,
                TypeError,
                ValueError,
                OSError,
                http.client.HTTPException,
                urllib.error.URLError,
                zipfile.BadZipFile,
            ):
                continue
        responses[repository + "/" + query] = {"workflow_runs": usable}
        document = {
            "schema": "common-test-broker-v1",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source": {"repository_id": source_id, "commit": source_sha, "tree": tree},
            "producer": producer,
            "responses": responses,
            "archives": archives,
        }
        path = directory / "evidence.json"
        path.write_bytes(common.equivalent.encoded(document) + b"\n")
        if os.environ.get("GITHUB_ENV"):
            with Path(os.environ["GITHUB_ENV"]).open("a") as output:
                output.write("COMMON_TEST_EVIDENCE_DIRECTORY=" + str(directory) + "\n")
                output.write(
                    "COMMON_TEST_EVIDENCE_SHA256="
                    + hashlib.sha256(path.read_bytes()).hexdigest()
                    + "\n"
                )
        return document
    finally:
        common.api = real_api


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--source-repository", required=True)
    parser.add_argument("--step", required=True)
    parser.add_argument(
        "--scope",
        action="append",
        default=[],
        help="Declare each scope passed to the result executor; omit for legacy discovery",
    )
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--source-authority", type=Path)
    parser.add_argument("--source-authority-sha256")
    arguments = parser.parse_args()
    try:
        prepare(arguments)
    except Exception as error:  # noqa: BLE001 - optional evidence cannot abort required execution
        # A missing proof changes no required job or test command.
        print(f"Full common-test execution required: {error}")
