#!/usr/bin/env python3
"""Resolve public workflow events to exact source and comparison commits."""

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPOSITORIES = {"EndurantDevs/drug-api", "EndurantDevs/healthcare-mrf-api"}
SHA = re.compile(r"[0-9a-f]{40}\Z")
HEALTHCARE = "EndurantDevs/healthcare-mrf-api"
REVIEWED_FIELDS = {
    "source_sha": "REVIEWED_SOURCE_SHA",
    "base_sha": "REVIEWED_BASE_SHA",
    "source_tree": "REVIEWED_SOURCE_TREE",
}


class JobInventoryPending(ValueError):
    """A complete job inventory cannot yet be read consistently."""


def api(repository, path, method="GET"):
    if repository not in REPOSITORIES:
        raise ValueError("repository is outside the public validation allowlist")
    suffix = f"/{path}" if path else ""
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repository}{suffix}",
        method=method,
        headers={
            "Authorization": f"Bearer {os.environ['GH_TOKEN']}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                if method == "DELETE":
                    if response.status != 204:
                        raise ValueError("GitHub artifact deletion did not return 204")
                    return None
                body = response.read(16 * 1024 * 1024 + 1)
            break
        except urllib.error.HTTPError as error:
            if method != "GET" or error.code not in {500, 502, 503, 504} or attempt == 2:
                raise
            error.close()
            time.sleep(attempt + 1)
    if len(body) > 16 * 1024 * 1024:
        raise ValueError("GitHub response exceeds the bounded limit")
    return json.loads(body)


def pages(repository, path, key=None):
    if key != "jobs":
        yield from _pages(repository, path, key)
        return
    for attempt in range(3):
        try:
            values = list(_pages(repository, path, key))
        except JobInventoryPending:
            if attempt == 2:
                raise
            time.sleep(attempt + 1)
        else:
            yield from values
            return


def _pages(repository, path, key):
    separator = "&" if "?" in path else "?"
    page_size = 50 if key == "jobs" else 100
    total, collected = None, 0
    for page in range(1, 61 if key == "jobs" else 101):
        result = api(repository, f"{path}{separator}per_page={page_size}&page={page}")
        if key == "jobs":
            values = result.get("jobs") if isinstance(result, dict) else None
            count = result.get("total_count") if isinstance(result, dict) else None
            if (type(count) is not int or count < 0 or (total is not None and count != total)
                    or not isinstance(values, list) or len(values) > page_size
                    or any(not isinstance(value, dict) for value in values)):
                raise JobInventoryPending("GitHub job inventory has invalid or changing pagination metadata")
            total = count
            collected += len(values)
            if collected > total or (len(values) < page_size and collected != total):
                raise JobInventoryPending("GitHub job inventory is incomplete or exceeds its declared count")
        else:
            values = result[key] if key else result
        yield from values
        if len(values) < page_size:
            return
    raise ValueError("GitHub result exceeds the bounded pagination limit")


def main_source_range(repository, commit, requested_base=None, *, branch="main"):
    """Verify the whole merged PR when a rebase created intermediate parents."""
    parents = commit.get("parents", [])
    if not parents:
        raise ValueError("source main has no comparison parent")
    parent = parents[0]["sha"]
    if len(parents) != 1 or requested_base == parent:
        return parent, [commit]
    associated = [pr for pr in pages(repository, f"commits/{commit['sha']}/pulls")
                  if pr.get("merged_at") and pr.get("merge_commit_sha") == commit["sha"]
                  and (branch == "main" or pr.get("base", {}).get("ref") == branch)]
    if not associated:
        return parent, [commit]
    if len(associated) != 1:
        raise ValueError("source main has ambiguous merged PR authority")
    pr = api(repository, f"pulls/{associated[0]['number']}")
    base = pr["base"]["sha"]
    if not (pr.get("merged") is True and pr.get("state") == "closed"
            and pr.get("merge_commit_sha") == commit["sha"]
            and pr["base"]["repo"]["full_name"] == repository
            and pr["base"]["ref"] == branch and SHA.fullmatch(base)
            and SHA.fullmatch(pr["head"]["sha"])):
        raise ValueError("source main merged PR identity changed")
    if base == parent or (requested_base is None and type(pr.get("commits")) is int and pr["commits"] == 1):
        return parent, [commit]
    comparison = api(repository, f"compare/{base}...{commit['sha']}")
    commits = comparison.get("commits", [])
    if comparison.get("total_commits", 0) > 250:  # GitHub's unpaged comparison limit.
        commits = list(pages(repository, f"compare/{base}...{commit['sha']}", "commits"))
    if not (comparison.get("status") == "ahead" and comparison.get("behind_by") == 0
            and comparison.get("merge_base_commit", {}).get("sha") == base
            and comparison.get("total_commits") == comparison.get("ahead_by")
            == pr.get("commits") == len(commits) and len(commits) > 1):
        raise ValueError("source main rebase comparison is incomplete or changed")
    previous = base
    for item in commits:
        if [value["sha"] for value in item.get("parents", [])] != [previous]:
            raise ValueError("source main rebase comparison is not one complete linear range")
        previous = item["sha"]
    head = api(repository, f"git/commits/{pr['head']['sha']}")
    if not (previous == commit["sha"] and head.get("sha") == pr["head"]["sha"]
            and head["tree"]["sha"] == commit["commit"]["tree"]["sha"]):
        raise ValueError("source main tree differs from its merged PR head")
    return base, commits



def merge_group_range(repository, payload, github_sha, github_ref):
    group = payload.get("merge_group", {})
    source, base = group.get("head_sha"), group.get("base_sha")
    if not (repository == "EndurantDevs/healthcare-mrf-api" and payload.get("action") == "checks_requested"
            and group.get("base_ref") == "refs/heads/dev" and group.get("head_ref") == github_ref
            and re.fullmatch(r"refs/heads/gh-readonly-queue/dev/pr-[1-9][0-9]*-[0-9a-f]+", github_ref)
            and source == github_sha and all(isinstance(sha, str) and SHA.fullmatch(sha)
                                            and set(sha) != {"0"} for sha in (source, base))):
        raise ValueError("merge group must identify one exact DEV queue candidate")
    queue = api(repository, "git/ref/" + github_ref.removeprefix("refs/"))
    if queue.get("ref") != github_ref or queue.get("object", {}).get("sha") != source:
        raise ValueError("merge group queue ref changed after its event")
    comparison = api(repository, f"compare/{base}...{source}")
    if not (comparison.get("status") == "ahead" and comparison.get("behind_by") == 0
            and comparison.get("merge_base_commit", {}).get("sha") == base):
        raise ValueError("merge group candidate does not descend from its comparison base")
    return source, base


def resolve_identity(repository, event_name, payload, github_sha, github_ref):
    event_repository = payload.get("repository", {})
    if (repository not in REPOSITORIES or event_repository.get("full_name") != repository
            or event_repository.get("private") is not False):
        raise ValueError("workflow repository differs from its public event")
    title = ""
    if event_name == "pull_request":
        event_pr = payload["pull_request"]
        number = event_pr["number"]
        if type(number) is not int or number <= 0:
            raise ValueError("invalid pull request number")
        pr = api(repository, f"pulls/{number}")
        branch, source, base = pr["base"]["ref"], pr["head"]["sha"], pr["base"]["sha"]
        title = pr.get("title", "")
        if not (pr["state"] == "open" and pr["base"]["repo"]["full_name"] == repository
                and event_pr["head"]["sha"] == source and event_pr["base"]["sha"] == base
                and event_pr["base"]["ref"] == branch
                and event_pr["base"]["repo"]["full_name"] == repository
                and event_pr.get("title") == title and github_ref == f"refs/pull/{number}/merge"):
            raise ValueError("pull request source, base, or title changed after its event")
        if not isinstance(title, str) or not title.strip() or any(c in title for c in "\r\n\0"):
            raise ValueError("pull request title must be present and single-line")
    elif event_name == "merge_group":
        source, base = merge_group_range(repository, payload, github_sha, github_ref)
        branch, number = "dev", 0
    elif event_name == "push":
        if payload.get("deleted") or github_ref != payload.get("ref") or github_sha != payload.get("after"):
            raise ValueError("push event does not match the workflow source")
        branch = github_ref.removeprefix("refs/heads/")
        commit = api(repository, f"commits/{github_sha}")
        if commit.get("sha") != github_sha:
            raise ValueError("push source commit changed")
        source = github_sha
        base, _ = main_source_range(
            repository, commit,
            payload.get("before") if (repository, branch) == ("EndurantDevs/healthcare-mrf-api", "dev") else None,
            branch=branch,
        )
        number = 0
    else:
        raise ValueError("public validation accepts only pull_request, push, and DEV merge_group events")
    if branch not in {"main", "dev"}:
        raise ValueError("public validation target must be main or dev")
    if any(not isinstance(sha, str) or not SHA.fullmatch(sha) or set(sha) == {"0"} for sha in (source, base)):
        raise ValueError("event source and base must be exact nonzero commit SHAs")
    return {
        "repository": repository, "source_sha": source, "base_sha": base,
        "source_branch": branch, "pr_number": str(number), "pr_title": title,
    }


def exact_sha(value):
    return isinstance(value, str) and bool(SHA.fullmatch(value) and set(value) != {"0"})


def reviewed_source(environ):
    """Resolve a separate source tuple for the protected read-only producer."""
    identity = {field: environ.get(variable, "") for field, variable in REVIEWED_FIELDS.items()}
    if not any(identity.values()):
        return None
    if (environ.get("GITHUB_REPOSITORY") != HEALTHCARE
            or environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
            or environ.get("GITHUB_REF") != "refs/heads/dev"
            or environ.get("GITHUB_REF_PROTECTED") != "true"
            or not all(exact_sha(value) for value in identity.values())):
        raise ValueError("reviewed source requires an exact tuple and a protected Healthcare dispatch")
    return {"repository": HEALTHCARE, **identity, "source_branch": "reviewed", "pr_number": "0", "pr_title": ""}


def reviewed_producer(environ):
    """Describe the caller run separately from its checked-out source."""
    if reviewed_source(environ) is None:
        raise ValueError("reviewed producer requires an explicit source tuple")
    head, workflow_sha = environ.get("GITHUB_SHA", ""), environ.get("GITHUB_WORKFLOW_SHA", "")
    workflow = environ.get("GITHUB_WORKFLOW_REF", "")
    run_id, attempt = environ.get("GITHUB_RUN_ID", ""), environ.get("GITHUB_RUN_ATTEMPT", "")
    if (not all(exact_sha(value) for value in (head, workflow_sha))
            or not re.fullmatch(rf"{re.escape(HEALTHCARE)}/\.github/workflows/[a-zA-Z0-9_.-]+\.ya?ml@refs/heads/dev",
                                workflow)
            or not all(re.fullmatch(r"[1-9][0-9]*", value) for value in (run_id, attempt))):
        raise ValueError("reviewed producer requires exact caller workflow and run coordinates")
    return {"repository": HEALTHCARE, "head_sha": head, "workflow_sha": workflow_sha,
            "workflow_ref": workflow, "run_id": run_id, "run_attempt": int(attempt)}


def verify_reviewed_checkout(path, identity):
    facts = subprocess.check_output(
        ["git", "-C", str(path), "rev-parse", "HEAD", "HEAD^{tree}", "HEAD^@"], text=True,
    ).splitlines()
    if facts != [identity["source_sha"], identity["source_tree"], identity["base_sha"]]:
        raise ValueError("reviewed checkout differs from its exact commit, tree or sole parent")


def main():
    revision = os.environ["CI_REVISION"]
    if not exact_sha(revision):
        raise ValueError("shared CI revision must be an exact nonzero commit SHA")
    identity = reviewed_source(os.environ)
    if sys.argv[1:] == ["verify-reviewed-checkout"]:
        if identity is None:
            raise ValueError("reviewed checkout requires an explicit source tuple")
        verify_reviewed_checkout(Path(os.environ["SOURCE_ROOT"]), identity)
        return
    if sys.argv[1:]:
        raise ValueError("unexpected source identity arguments")
    if identity is None:
        identity = resolve_identity(
            os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_EVENT_NAME"],
            json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text()),
            os.environ["GITHUB_SHA"], os.environ["GITHUB_REF"],
        )
    else:
        identity["producer"] = reviewed_producer(os.environ)
    (Path(os.environ["RUNNER_TEMP"]) / "source-identity.json").write_text(
        json.dumps(identity, sort_keys=True) + "\n"
    )
    names = {"repository": "SOURCE_REPOSITORY", "source_sha": "SOURCE_SHA", "base_sha": "BASE_SHA",
             "source_branch": "SOURCE_BRANCH", "pr_number": "PR_NUMBER", "pr_title": "SOURCE_PR_TITLE"}
    for output_path in (os.environ["GITHUB_ENV"], os.environ["GITHUB_OUTPUT"]):
        with Path(output_path).open("a") as handle:
            for name, variable in names.items():
                handle.write(f"{variable}={identity[name]}\n")


if __name__ == "__main__":
    main()
