#!/usr/bin/env python3
"""Recover one failed metadata dependency check after exact full validation succeeds."""

import argparse
import base64
import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import source_identity as github
from artifact_cleanup import KINDS, attempt_jobs, timestamp

METADATA_ONLY = (
    "github.event_name == 'pull_request' && github.event.action == 'edited' "
    "&& !github.event.changes.title && !github.event.changes.base"
)
WORKFLOW = ".github/workflows/ci.yml"
RECOVERY_WORKFLOW = ".github/workflows/metadata-recovery.yml"
DEPENDENCY_STEP = "Require complete public validation"
MARKER = "METADATA_VALIDATION_DEPENDENCY_FAILED"
VALIDATION = {"healthcare": "Validation complete", "drug": "Coverage results"}
SKIPPED = {
    "healthcare": (
        "Repository checks",
        "Readability checks",
        "Python lint",
        "python-tests",
        "Capacity tests",
        "API contract",
        "Rust tests",
        "Container build",
        "Security scans",
        "Worker queue tests",
        "address-canonical-db-tests",
        "Coverage results",
        "DEV image publication",
        "CI artifact cleanup",
    ),
    "drug": ("Tests and build", "DEV image publication", "CI artifact cleanup"),
}
MATRIX_JOBS = {"python-tests", "address-canonical-db-tests"}


def skipped_names(label):
    fallback = "matrix.label" if label in MATRIX_JOBS else f"'{label}'"
    return {
        f"{label} (metadata only)",
        f"{METADATA_ONLY} && '{label} (metadata only)' || {fallback}",
    }


def associated(run, number, head, base, target):
    return any(
        pr.get("number") == number
        and pr.get("head", {}).get("sha") == head
        and pr.get("base", {}).get("sha") == base
        and pr.get("base", {}).get("ref") == target
        for pr in run.get("pull_requests", [])
    )


def current_pr(pr, repository, number, head, base, target):
    return (
        pr.get("number") == number
        and pr.get("state") == "open"
        and not pr.get("merged")
        and pr.get("head", {}).get("sha") == head
        and pr.get("base", {}).get("sha") == base
        and pr.get("head", {}).get("repo", {}).get("full_name") == repository
        and pr.get("base", {}).get("repo", {}).get("full_name") == repository
        and target in {"main", "dev"}
        and pr.get("base", {}).get("ref") == target
    )


def protected_target(repository, target, base):
    branch = github.api(repository, f"branches/{target}")
    return (
        branch.get("name") == target
        and branch.get("protected") is True
        and branch.get("commit", {}).get("sha") == base
    )


def source_run(run, repository, workflow_id):
    return (
        type(run.get("id")) is int
        and run["id"] > 0
        and type(run.get("run_attempt")) is int
        and run["run_attempt"] > 0
        and run.get("workflow_id") == workflow_id
        and run.get("path") == WORKFLOW
        and run.get("event") == "pull_request"
        and run.get("name") == "CI"
        and github.SHA.fullmatch(run.get("head_sha", ""))
        and run.get("repository", {}).get("full_name") == repository
        and run.get("head_repository", {}).get("full_name") == repository
    )


def run_order(run):
    return (
        timestamp(run.get("run_started_at") or run["created_at"]),
        run["id"],
        run["run_attempt"],
    )


def workflow_runs(repository, head):
    path = f"actions/workflows/ci.yml/runs?event=pull_request&head_sha={head}"
    first = github.api(repository, path + "&per_page=100&page=1")
    count = first.get("total_count")
    if type(count) is not int or not 0 <= count <= 1000:
        raise ValueError("workflow search exceeds its complete bounded inventory")
    runs = first["workflow_runs"]
    for page in range(2, (count + 99) // 100 + 1):
        runs.extend(
            github.api(repository, path + f"&per_page=100&page={page}")["workflow_runs"]
        )
    if len(runs) != count or len({run.get("id") for run in runs}) != count:
        raise ValueError("workflow search changed or has ambiguous run identities")
    return runs


def workflow_bytes(repository, revision):
    value = github.api(repository, f"contents/{WORKFLOW}?ref={revision}")
    if (
        value.get("type") != "file"
        or value.get("encoding") != "base64"
        or type(value.get("size")) is not int
        or not 0 < value["size"] <= 1024 * 1024
    ):
        raise ValueError("source workflow requires one bounded regular file")
    content = base64.b64decode(value["content"], validate=False)
    if len(content) != value["size"]:
        raise ValueError("source workflow size differs from its file identity")
    return content


class LogRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        if request.get_method() != "GET":
            raise ValueError("job rerun requests must not redirect")
        if urllib.parse.urlsplit(new_url).scheme != "https":
            raise ValueError("job log redirect requires HTTPS")
        redirected = super().redirect_request(
            request, fp, code, message, headers, new_url
        )
        redirected.remove_header("Authorization")
        return redirected


def dependency_failed(repository, job_id, head, base):
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repository}/actions/jobs/{job_id}/logs",
        headers={
            "Authorization": f"Bearer {os.environ['GH_TOKEN']}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.build_opener(LogRedirects()).open(
        request, timeout=30
    ) as response:
        body = response.read(1024 * 1024 + 1)
    if len(body) > 1024 * 1024:
        raise ValueError("metadata job log exceeds the bounded limit")
    # The runner also prints commands; accept only the actual timestamped marker output.
    marker = f"{MARKER} source={head} base={base}"
    return any(
        re.fullmatch(r"\d{4}-\d\d-\d\dT[0-9:.]+Z " + re.escape(marker), line)
        for line in body.decode("utf-8").splitlines()
    )


def eligible_jobs(kind, jobs):
    if len(jobs) != len(SKIPPED[kind]) + 2:
        return None
    portable = [job for job in jobs if job["name"] == "portable import checks"]
    validation = [job for job in jobs if job["name"] == VALIDATION[kind]]
    if (
        len(portable) != 1
        or portable[0].get("status") != "completed"
        or portable[0].get("conclusion") != "success"
        or len(validation) != 1
    ):
        return None
    gate = validation[0]
    steps = gate.get("steps", [])
    if (
        gate.get("status") != "completed"
        or gate.get("conclusion") != "failure"
        or [step.get("name") for step in steps if step.get("conclusion") == "failure"]
        != [DEPENDENCY_STEP]
        or any(
            step.get("conclusion") not in {"success", "failure", "skipped"}
            for step in steps
        )
        or any(
            step.get("conclusion") != "skipped"
            for step in steps
            if step.get("name") not in {"Set up job", "Complete job", DEPENDENCY_STEP}
        )
    ):
        return None
    remaining = [job for job in jobs if job not in (portable[0], gate)]
    for label in SKIPPED[kind]:
        matching = [job for job in remaining if job["name"] in skipped_names(label)]
        if (
            len(matching) != 1
            or matching[0].get("status") != "completed"
            or matching[0].get("conclusion") != "skipped"
            or matching[0].get("steps") != []
        ):
            return None
        remaining.remove(matching[0])
    return gate if not remaining else None


def candidate(repository, kind, event_run):
    associations = event_run.get("pull_requests", [])
    if len(associations) != 1:
        return None
    association = associations[0]
    number = association.get("number")
    head, base = (
        association.get("head", {}).get("sha", ""),
        association.get("base", {}).get("sha", ""),
    )
    target = association.get("base", {}).get("ref")
    if (
        type(number) is not int
        or number <= 0
        or not github.SHA.fullmatch(head)
        or not github.SHA.fullmatch(base)
        or target not in {"main", "dev"}
    ):
        return None
    pr = github.api(repository, f"pulls/{number}")
    if not current_pr(pr, repository, number, head, base, target):
        return None
    if not protected_target(repository, target, base):
        return None
    workflow = github.api(repository, "actions/workflows/ci.yml")
    workflow_id = workflow.get("id")
    if (
        type(workflow_id) is not int
        or workflow_id <= 0
        or workflow.get("path") != WORKFLOW
    ):
        return None
    if (
        not source_run(event_run, repository, workflow_id)
        or event_run.get("head_sha") != head
    ):
        return None
    # Select before checking association: a newer ambiguous run must never expose
    # an older successful run as the latest authority.
    runs = workflow_runs(repository, head)
    full = [run for run in runs if run.get("display_title") == "CI"]
    metadata = [run for run in runs if run.get("display_title") == "CI metadata update"]
    if not full or not metadata:
        return None
    latest_full = max(full, key=run_order)
    latest_metadata = max(metadata, key=run_order)
    if any(
        not source_run(run, repository, workflow_id)
        or run.get("head_sha") != head
        or len(run.get("pull_requests", [])) != 1
        or not associated(run, number, head, base, target)
        for run in (latest_full, latest_metadata)
    ):
        return None
    # Completion events can arrive late; fresh latest validation is the authority.
    if (
        latest_full.get("status") != "completed"
        or latest_full.get("conclusion") != "success"
    ):
        return None
    if (
        latest_metadata.get("status") != "completed"
        or latest_metadata.get("conclusion") != "failure"
        or latest_metadata.get("run_attempt") != 1
    ):
        return None
    # A workflow-changing PR cannot supply executable code for this privileged recovery.
    if workflow_bytes(repository, head) != workflow_bytes(repository, base):
        return None
    gate = eligible_jobs(kind, attempt_jobs(repository, latest_metadata, 1))
    if gate is None or not dependency_failed(repository, gate["id"], head, base):
        return None
    return {
        "number": number,
        "head": head,
        "base": base,
        "target": target,
        "workflow_id": workflow_id,
        "full_id": latest_full["id"],
        "full_attempt": latest_full["run_attempt"],
        "metadata_id": latest_metadata["id"],
        "job_id": gate["id"],
    }


def request_rerun(repository, job_id):
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repository}/actions/jobs/{job_id}/rerun",
        method="POST",
        headers={
            "Authorization": f"Bearer {os.environ['GH_TOKEN']}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.build_opener(LogRedirects()).open(
            request, timeout=30
        ) as response:
            if response.status != 201:
                raise ValueError("metadata job rerun was not accepted")
    except urllib.error.HTTPError as error:
        if error.code == 409:
            error.close()
            return False
        raise
    return True


def recover(repository, kind, event_run):
    if (
        event_run.get("status") != "completed"
        or event_run.get("conclusion") != "success"
        or event_run.get("display_title") != "CI"
    ):
        return False
    selected = candidate(repository, kind, event_run)
    if selected is None or candidate(repository, kind, event_run) != selected:
        return False
    full = github.api(repository, f"actions/runs/{selected['full_id']}")
    metadata = github.api(repository, f"actions/runs/{selected['metadata_id']}")
    job = github.api(repository, f"actions/jobs/{selected['job_id']}")
    pr = github.api(repository, f"pulls/{selected['number']}")
    protected = protected_target(repository, selected["target"], selected["base"])
    if (
        not current_pr(
            pr,
            repository,
            selected["number"],
            selected["head"],
            selected["base"],
            selected["target"],
        )
        or not protected
        or any(
            not source_run(run, repository, selected["workflow_id"])
            or run.get("head_sha") != selected["head"]
            or len(run.get("pull_requests", [])) != 1
            or not associated(
                run,
                selected["number"],
                selected["head"],
                selected["base"],
                selected["target"],
            )
            for run in (full, metadata)
        )
        or full.get("id") != selected["full_id"]
        or full.get("run_attempt") != selected["full_attempt"]
        or full.get("display_title") != "CI"
        or full.get("status") != "completed"
        or full.get("conclusion") != "success"
        or metadata.get("id") != selected["metadata_id"]
        or metadata.get("display_title") != "CI metadata update"
        or metadata.get("run_attempt") != 1
        or metadata.get("status") != "completed"
        or metadata.get("conclusion") != "failure"
        or job.get("id") != selected["job_id"]
        or job.get("run_id") != selected["metadata_id"]
        or job.get("run_attempt") != 1
        or job.get("head_sha") != selected["head"]
        or job.get("name") != VALIDATION[kind]
        or job.get("status") != "completed"
        or job.get("conclusion") != "failure"
    ):
        return False
    return request_rerun(repository, selected["job_id"])


def main(kind):
    repository = os.environ["GITHUB_REPOSITORY"]
    payload = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    event_repository = payload.get("repository", {})
    default_branch = event_repository.get("default_branch")
    if (
        KINDS.get(repository) != kind
        or os.environ["GITHUB_EVENT_NAME"] != "workflow_run"
        or os.environ["GITHUB_JOB"] != "metadata-recovery"
        or event_repository.get("full_name") != repository
        or event_repository.get("private") is not False
        or default_branch != "main"
        or os.environ["GITHUB_REF"] != "refs/heads/main"
        or payload.get("action") != "completed"
    ):
        raise ValueError(
            "metadata recovery requires its trusted public default-branch workflow"
        )
    revision = os.environ["CI_REVISION"]
    if (
        not github.SHA.fullmatch(revision)
        or set(revision) == {"0"}
        or subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parents[1],
            text=True,
        ).strip()
        != revision
    ):
        raise ValueError(
            "metadata recovery helper revision differs from its trusted pin"
        )
    own_id = int(os.environ["GITHUB_RUN_ID"])
    own_run = github.api(repository, f"actions/runs/{own_id}")
    if (
        own_id <= 0
        or own_run.get("id") != own_id
        or own_run.get("path") != RECOVERY_WORKFLOW
        or own_run.get("event") != "workflow_run"
        or own_run.get("status") != "in_progress"
        or own_run.get("conclusion") is not None
        or own_run.get("head_sha") != os.environ["GITHUB_SHA"]
        or own_run.get("head_branch") != default_branch
        or own_run.get("run_attempt") != int(os.environ["GITHUB_RUN_ATTEMPT"])
        or own_run.get("repository", {}).get("full_name") != repository
        or own_run.get("head_repository", {}).get("full_name") != repository
    ):
        raise ValueError("metadata recovery run identity changed")
    requested = recover(repository, kind, payload.get("workflow_run", {}))
    print(
        "Requested one metadata validation recovery."
        if requested
        else "No eligible metadata validation recovery."
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("healthcare", "drug"))
    main(parser.parse_args().kind)
