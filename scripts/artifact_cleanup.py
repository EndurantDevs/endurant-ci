#!/usr/bin/env python3
"""Remove public CI intermediates and obsolete durable evidence."""

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import time

import source_identity as github


KINDS = {"EndurantDevs/healthcare-mrf-api": "healthcare", "EndurantDevs/drug-api": "drug"}
CLEANUP_JOB = "CI artifact cleanup"
STALE_CLEANUP_JOB = "stale-cleanup"
STALE_CLEANUP_WORKFLOW = ".github/workflows/artifact-cleanup.yml"
RUN_FIELDS = ("id", "run_attempt", "path", "event", "head_sha", "head_branch")
ARTIFACT_FIELDS = ("id", "name", "size_in_bytes", "expired", "created_at", "expires_at", "updated_at", "digest")
TERMINAL_CONCLUSIONS = {
    "success", "failure", "neutral", "cancelled", "skipped", "timed_out", "action_required", "stale",
    "startup_failure",
}


def current_run(run, expected, repository):
    return bool(
        run.get("status") == "in_progress" and run.get("conclusion") is None
        and run.get("path") == ".github/workflows/ci.yml"
        and run.get("event") in {"push", "pull_request"}
        and run.get("repository", {}).get("full_name") == repository
        and run.get("head_repository", {}).get("full_name") == repository
        and type(run.get("workflow_id")) is int and run["workflow_id"] > 0
        and run["workflow_id"] == expected.get("workflow_id", run["workflow_id"])
        and all(run.get(key) == expected.get(key) for key in RUN_FIELDS)
    )


def completed_consumers(repository, run):
    jobs = list(github.pages(repository, f"actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs", "jobs"))
    if (len(jobs) < 2 or len({job.get("id") for job in jobs}) != len(jobs)
            or len({job.get("name") for job in jobs}) != len(jobs)
            or sum(job.get("name") == CLEANUP_JOB for job in jobs) != 1):
        raise ValueError("cleanup requires a complete, unique job inventory including itself")
    for job in jobs:
        own_job = job.get("name") == CLEANUP_JOB
        if not (type(job.get("id")) is int and job["id"] > 0
                and job.get("run_id") == run["id"] and job.get("run_attempt") == run["run_attempt"]
                and job.get("head_sha") == run["head_sha"]
                and job.get("status") == ("in_progress" if own_job else "completed")
                and (job.get("conclusion") is None if own_job
                     else job.get("conclusion") in TERMINAL_CONCLUSIONS)):
            raise ValueError("retained intermediates: cleanup must be the sole active job after validation")
    return sorted((job["id"], job["name"], job.get("conclusion")) for job in jobs)


def temporary_names(kind, run):
    suffix = f"{run['id']}-{run['run_attempt']}"
    image = {f"{kind}-public-image-staging-{suffix}"} if run["event"] == "push" and run["head_branch"] == "dev" else set()
    if kind == "drug":
        return image | {f"drug-public-staging-{suffix}"}
    return {f"{name}-{suffix}" for name in (
        "healthcare-rust-debug", "mrf-rust-coverage", "mrf-python-coverage-capacity",
        *(f"mrf-python-coverage-main-{shard}" for shard in range(4)),
        *(f"mrf-python-coverage-postgres-{shard}" for shard in
          ("core", "provider-directory", "provider-profile")),
    )} | image


def timestamp(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("artifact timestamps require a timezone")
    return result


def available(artifact, run):
    # The exact-run listing supplies authority when workflow_run is omitted.
    producer = artifact.get("workflow_run")
    return bool(
        type(artifact.get("id")) is int and artifact["id"] > 0
        and artifact.get("expired") is False
        and (producer is None or (producer.get("id") == run["id"]
             and producer.get("head_sha", run["head_sha"]) == run["head_sha"]))
    )


def durable(artifact, run):
    return bool(
        available(artifact, run) and artifact.get("size_in_bytes", 0) > 0
        and timestamp(artifact["expires_at"]) > datetime.now(timezone.utc)
        and timestamp(artifact["expires_at"]) - timestamp(artifact["created_at"]) >= timedelta(days=89)
    )


def durable_parts(kind, name):
    match = re.fullmatch(
        rf"{kind}-public-(measurement|image(?:-intent)?)-([1-9][0-9]*)-([1-9][0-9]*)", name or ""
    )
    return (match[1], int(match[2]), int(match[3])) if match else None


def durable_identity(kind, name):
    parts = durable_parts(kind, name)
    return parts[1:] if parts else None


def public_ci_run(run, artifact, repository, identity):
    producer = artifact.get("workflow_run", {})
    status = run.get("status")
    conclusion = run.get("conclusion")
    return bool(
        type(run.get("id")) is int and run["id"] == identity[0]
        and type(run.get("run_attempt")) is int and run["run_attempt"] == identity[1]
        and type(run.get("workflow_id")) is int and run["workflow_id"] > 0
        and run.get("path") == ".github/workflows/ci.yml"
        and run.get("event") in {"push", "pull_request"}
        and isinstance(run.get("head_branch"), str) and run["head_branch"]
        and github.SHA.fullmatch(run.get("head_sha", ""))
        and run.get("repository", {}).get("full_name") == repository
        and run.get("head_repository", {}).get("full_name") == repository
        and type(producer.get("id")) is int and producer["id"] == run["id"]
        and producer.get("head_sha") == run["head_sha"]
        and status in {"queued", "in_progress", "completed"}
        and ((status == "completed" and conclusion in TERMINAL_CONCLUSIONS)
             or (status != "completed" and conclusion is None))
        and (run["event"] != "push" or run["head_branch"] in {"main", "dev"})
    )


def retained_non_candidate(run, artifact, repository, identity):
    producer = artifact.get("workflow_run", {})
    return bool(
        type(run.get("id")) is int and run["id"] == identity[0]
        and type(run.get("run_attempt")) is int and run["run_attempt"] >= identity[1]
        and type(run.get("workflow_id")) is int and run["workflow_id"] > 0
        and run.get("path") == ".github/workflows/ci.yml"
        and run.get("event") in {"push", "pull_request"}
        and github.SHA.fullmatch(run.get("head_sha", ""))
        and run.get("repository", {}).get("full_name") == repository
        and type(producer.get("id")) is int and producer["id"] == run["id"]
        and producer.get("head_sha") == run["head_sha"]
        and (run["run_attempt"] > identity[1]
             or (run["event"] == "pull_request"
                 and isinstance(run.get("head_repository", {}).get("full_name"), str)
                 and run["head_repository"]["full_name"] != repository))
    )


def artifact_pages(repository):
    # GitHub retains expired artifact records, so a busy repository can exceed
    # the generic 10,000-item page bound while using little live storage.
    for page in range(1, 601):
        result = github.api(repository, f"actions/artifacts?per_page=100&page={page}")
        values = result.get("artifacts") if isinstance(result, dict) else None
        if not isinstance(values, list):
            raise ValueError("artifact cleanup requires a complete repository artifact inventory")
        yield from values
        if len(values) < 100:
            return
    raise ValueError("artifact inventory exceeds the bounded cleanup window")


def active_heads(repository):
    metadata = github.api(repository, "")
    if not (metadata.get("full_name") == repository and metadata.get("private") is False
            and metadata.get("default_branch") == "main"):
        raise ValueError("stale cleanup requires the expected public repository")
    branches = {}
    for name in ("main", "dev"):
        branch = github.api(repository, f"branches/{name}")
        sha = branch.get("commit", {}).get("sha")
        if branch.get("name") != name or not github.SHA.fullmatch(sha or ""):
            raise ValueError("stale cleanup requires exact main and dev branch heads")
        branches[name] = sha
    heads = set(branches.values())
    for pr in github.pages(repository, "pulls?state=open"):
        sha = pr.get("head", {}).get("sha")
        if not (type(pr.get("number")) is int and pr["number"] > 0 and pr.get("state") == "open"
                and pr.get("base", {}).get("repo", {}).get("full_name") == repository
                and isinstance(pr.get("head", {}).get("repo", {}).get("full_name"), str)
                and github.SHA.fullmatch(sha or "")):
            raise ValueError("stale cleanup requires a complete open pull request inventory")
        heads.add(sha)
    return branches, heads


def current_stale_cleanup(run, expected, repository):
    return bool(
        run.get("status") == "in_progress" and run.get("conclusion") is None
        and run.get("path") == STALE_CLEANUP_WORKFLOW
        and run.get("event") in {"schedule", "workflow_dispatch"}
        and run.get("repository", {}).get("full_name") == repository
        and run.get("head_repository", {}).get("full_name") == repository
        and type(run.get("workflow_id")) is int and run["workflow_id"] > 0
        and all(run.get(key) == expected.get(key) for key in RUN_FIELDS)
    )


def stale_cleanup(repository, expected):
    if repository not in KINDS or any(type(expected.get(key)) is not int or expected[key] <= 0
                                    for key in ("id", "run_attempt")):
        raise ValueError("stale cleanup requires an allowlisted repository and exact run attempt")
    if not github.SHA.fullmatch(expected.get("head_sha", "")) or set(expected["head_sha"]) == {"0"}:
        raise ValueError("stale cleanup requires an exact source commit")
    cleanup_path = f"actions/runs/{expected['id']}"
    if not current_stale_cleanup(github.api(repository, cleanup_path), expected, repository):
        raise ValueError("stale cleanup run identity changed")
    branches, heads = active_heads(repository)
    if expected["head_sha"] != branches["main"]:
        raise ValueError("stale cleanup must run from current public main")

    kind = KINDS[repository]
    runs = {}
    durable_artifacts = []
    seen_ids = set()
    for artifact in artifact_pages(repository):
        parts = durable_parts(kind, artifact.get("name"))
        if not parts or artifact.get("expired") is not False:
            continue
        category, *identity = parts
        if artifact.get("id") in seen_ids:
            raise ValueError("artifact cleanup requires unique artifact identities")
        seen_ids.add(artifact.get("id"))
        if identity[0] not in runs:
            runs[identity[0]] = github.api(repository, f"actions/runs/{identity[0]}")
        run = runs[identity[0]]
        if retained_non_candidate(run, artifact, repository, identity):
            continue
        if not public_ci_run(run, artifact, repository, identity) or not durable(artifact, run):
            raise ValueError("recognized durable artifact has unauthenticated provenance")
        durable_artifacts.append((artifact, run, category))

    protected_runs = {run["id"] for _, run, _ in durable_artifacts
                      if run["head_sha"] in heads or run["status"] != "completed"}
    anchors = {}
    for branch in ("main", "dev"):
        for category in {category for _, _, category in durable_artifacts}:
            successful = [(run, artifact) for artifact, run, artifact_category in durable_artifacts
                          if artifact_category == category and run["event"] == "push"
                          and run["head_branch"] == branch and run["status"] == "completed"
                          and run["conclusion"] == "success"]
            if successful:
                anchor = max(successful, key=lambda item: (timestamp(item[0]["created_at"]), item[0]["id"]))
                protected_runs.add(anchor[0]["id"])
                anchors[(branch, category)] = anchor

    deleted = deleted_bytes = 0
    for artifact, run, category in durable_artifacts:
        if run["id"] in protected_runs or run["status"] != "completed":
            continue
        if not current_stale_cleanup(github.api(repository, cleanup_path), expected, repository):
            raise ValueError(f"stopped after {deleted} deletions: stale cleanup run changed")
        current_branches, current_heads = active_heads(repository)
        if current_branches["main"] != expected["head_sha"]:
            raise ValueError(f"stopped after {deleted} deletions: public main changed")
        if run["head_sha"] in current_heads:
            continue
        if run["event"] == "push":
            anchor = anchors.get((run["head_branch"], category))
            if anchor is None:
                continue
            anchor_run, expected_anchor = anchor
            refreshed_anchor = github.api(repository, f"actions/runs/{anchor_run['id']}")
            anchor_artifact = github.api(repository, f"actions/artifacts/{expected_anchor['id']}")
            anchor_identity = durable_identity(kind, anchor_artifact.get("name"))
            if (anchor_identity is None
                    or not public_ci_run(refreshed_anchor, anchor_artifact, repository, anchor_identity)
                    or not durable(anchor_artifact, refreshed_anchor)
                    or refreshed_anchor.get("status") != "completed"
                    or refreshed_anchor.get("conclusion") != "success"
                    or any(refreshed_anchor.get(key) != anchor_run.get(key) for key in
                           (*RUN_FIELDS, "workflow_id", "status", "conclusion", "created_at"))
                    or any(anchor_artifact.get(key) != expected_anchor.get(key)
                           for key in ARTIFACT_FIELDS)):
                raise ValueError(f"stopped after {deleted} deletions: latest branch evidence changed")
        refreshed_run = github.api(repository, f"actions/runs/{run['id']}")
        current = github.api(repository, f"actions/artifacts/{artifact['id']}")
        identity = durable_identity(kind, current.get("name"))
        if (identity is None or not public_ci_run(refreshed_run, current, repository, identity)
                or any(refreshed_run.get(key) != run.get(key) for key in
                       (*RUN_FIELDS, "workflow_id", "status", "conclusion", "created_at"))
                or any(current.get(key) != artifact.get(key) for key in ARTIFACT_FIELDS)):
            raise ValueError(f"stopped after {deleted} deletions: durable artifact provenance changed")
        github.api(repository, f"actions/artifacts/{artifact['id']}", "DELETE")
        deleted += 1
        deleted_bytes += artifact["size_in_bytes"]
        time.sleep(1)
    print(f"Deleted {deleted} obsolete durable artifacts ({deleted_bytes} bytes); preserved current branches, open PRs, latest successful branch evidence, and unknown artifacts.")
    return deleted


def cleanup(repository, expected):
    if repository not in KINDS or any(type(expected.get(key)) is not int or expected[key] <= 0
                                    for key in ("id", "run_attempt")):
        raise ValueError("cleanup requires an allowlisted repository and exact run attempt")
    if not github.SHA.fullmatch(expected.get("head_sha", "")) or set(expected["head_sha"]) == {"0"}:
        raise ValueError("cleanup requires an exact source commit")
    run_path = f"actions/runs/{expected['id']}"
    run = github.api(repository, run_path)
    if not current_run(run, expected, repository):
        raise ValueError("retained artifacts: current workflow run or attempt changed")
    expected = {**expected, "workflow_id": run["workflow_id"]}
    consumers = completed_consumers(repository, expected)
    # Complete pagination before deletion can shift numbered pages.
    artifacts = list(github.pages(repository, f"{run_path}/artifacts", "artifacts"))
    kind = KINDS[repository]
    final_name = f"{kind}-public-measurement-{expected['id']}-{expected['run_attempt']}"
    finals = [artifact for artifact in artifacts if artifact.get("name") == final_name]
    candidates = [artifact for artifact in artifacts
                  if artifact.get("name") in temporary_names(kind, expected) and available(artifact, expected)]
    if not candidates:
        print("No current-attempt intermediates to remove.")
        return 0
    successful = all(conclusion == "success" for _, name, conclusion in consumers if name != CLEANUP_JOB)
    proofs = []
    if successful:
        if len(finals) != 1 or not durable(finals[0], expected):
            raise ValueError("retained intermediates: exact durable public measurement is missing or invalid")
        proofs.append(finals[0])
        image_name = f"{kind}-public-image-staging-{expected['id']}-{expected['run_attempt']}"
        if any(item["name"] == image_name for item in candidates):
            receipt_name = f"{kind}-public-image-{expected['id']}-{expected['run_attempt']}"
            receipts = [item for item in artifacts if item.get("name") == receipt_name]
            if len(receipts) != 1 or not durable(receipts[0], expected):
                raise ValueError("retained image archive: durable publication receipt is missing or invalid")
            proofs.append(receipts[0])
    deleted = 0
    for artifact in candidates:
        # Refresh consumer completion, immutable artifacts, and run before each delete.
        if completed_consumers(repository, expected) != consumers:
            raise ValueError("retained intermediates: validation job inventory changed")
        current = github.api(repository, f"actions/artifacts/{artifact['id']}")
        if not available(current, expected) or any(current.get(key) != artifact.get(key) for key in ARTIFACT_FIELDS):
            raise ValueError("retained intermediates: artifact identity changed")
        for proof in proofs:
            measurement = github.api(repository, f"actions/artifacts/{proof['id']}")
            if not durable(measurement, expected) or any(measurement.get(key) != proof.get(key) for key in ARTIFACT_FIELDS):
                raise ValueError("retained intermediates: durable measurement or publication changed")
        if not current_run(github.api(repository, run_path), expected, repository):
            raise ValueError(f"stopped after {deleted} deletions: producing run changed")
        github.api(repository, f"actions/artifacts/{artifact['id']}", "DELETE")
        deleted += 1
        time.sleep(1)
    print(f"Deleted {deleted} validated intermediates; preserved final measurement and unknown artifacts.")
    return deleted


def main():
    payload = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    repository = os.environ["GITHUB_REPOSITORY"]
    event = os.environ["GITHUB_EVENT_NAME"]
    if not (repository in KINDS and event in {"push", "pull_request"}
            and os.environ["GITHUB_JOB"] == "artifact-cleanup"
            and payload.get("repository", {}).get("full_name") == repository
            and payload["repository"].get("private") is False):
        raise ValueError("cleanup requires its own public source CI job and event")
    if event == "pull_request":
        pr = payload["pull_request"]
        if not (type(pr.get("number")) is int and pr["number"] > 0
                and os.environ["GITHUB_REF"] == f"refs/pull/{pr['number']}/merge"
                and pr["base"]["repo"]["full_name"] == repository
                and pr["base"]["ref"] in {"main", "dev"}):
            raise ValueError("cleanup pull request identity differs from its source event")
        if pr["head"]["repo"]["full_name"] != repository:
            print("Retained fork PR artifacts for their one-day expiry; no write token is required.")
            return
        source, branch = pr["head"]["sha"], pr["head"]["ref"]
    else:
        if not (os.environ["GITHUB_REF"] == payload.get("ref")
                and payload["ref"] in {"refs/heads/main", "refs/heads/dev"}
                and os.environ["GITHUB_SHA"] == payload.get("after") and not payload.get("deleted")):
            raise ValueError("cleanup push identity differs from its source event")
        source, branch = payload["after"], payload["ref"].removeprefix("refs/heads/")
    cleanup(repository, {"id": int(os.environ["GITHUB_RUN_ID"]),
                         "run_attempt": int(os.environ["GITHUB_RUN_ATTEMPT"]),
                         "event": event, "head_sha": source, "head_branch": branch,
                         "path": ".github/workflows/ci.yml"})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stale", action="store_true", help="remove obsolete durable public evidence")
    arguments = parser.parse_args()
    if arguments.stale:
        repository = os.environ["GITHUB_REPOSITORY"]
        payload = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
        event = os.environ["GITHUB_EVENT_NAME"]
        if not (repository in KINDS and event in {"schedule", "workflow_dispatch"}
                and os.environ["GITHUB_JOB"] == STALE_CLEANUP_JOB
                and os.environ["GITHUB_REF"] == "refs/heads/main"
                and payload.get("repository", {}).get("full_name") == repository
                and payload["repository"].get("private") is False):
            raise ValueError("stale cleanup requires its protected public workflow")
        stale_cleanup(repository, {"id": int(os.environ["GITHUB_RUN_ID"]),
                                   "run_attempt": int(os.environ["GITHUB_RUN_ATTEMPT"]),
                                   "event": event, "head_sha": os.environ["GITHUB_SHA"],
                                   "head_branch": "main", "path": STALE_CLEANUP_WORKFLOW})
    else:
        main()
