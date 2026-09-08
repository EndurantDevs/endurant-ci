#!/usr/bin/env python3
"""Resolve public workflow events to exact source and comparison commits."""

import json
import os
from pathlib import Path
import re
import urllib.request

REPOSITORIES = {"EndurantDevs/drug-api", "EndurantDevs/healthcare-mrf-api"}
SHA = re.compile(r"[0-9a-f]{40}\Z")


def api(repository, path):
    if repository not in REPOSITORIES:
        raise ValueError("repository is outside the public validation allowlist")
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repository}/{path}",
        headers={
            "Authorization": f"Bearer {os.environ['GH_TOKEN']}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read(16 * 1024 * 1024 + 1)
    if len(body) > 16 * 1024 * 1024:
        raise ValueError("GitHub response exceeds the bounded limit")
    return json.loads(body)


def pages(repository, path, key=None):
    separator = "&" if "?" in path else "?"
    for page in range(1, 101):
        result = api(repository, f"{path}{separator}per_page=100&page={page}")
        values = result[key] if key else result
        yield from values
        if len(values) < 100:
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
    if base == parent:
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
    elif event_name == "push":
        if payload.get("deleted") or github_ref != payload.get("ref") or github_sha != payload.get("after"):
            raise ValueError("push event does not match the workflow source")
        branch = github_ref.removeprefix("refs/heads/")
        commit = api(repository, f"commits/{github_sha}")
        if commit.get("sha") != github_sha:
            raise ValueError("push source commit changed")
        source = github_sha
        base, _ = main_source_range(repository, commit, branch=branch)
        number = 0
    else:
        raise ValueError("public validation accepts only pull_request and push events")
    if branch not in {"main", "dev"}:
        raise ValueError("public validation target must be main or dev")
    if any(not isinstance(sha, str) or not SHA.fullmatch(sha) or set(sha) == {"0"} for sha in (source, base)):
        raise ValueError("event source and base must be exact nonzero commit SHAs")
    return {
        "repository": repository, "source_sha": source, "base_sha": base,
        "source_branch": branch, "pr_number": str(number), "pr_title": title,
    }


def main():
    revision = os.environ["CI_REVISION"]
    if not SHA.fullmatch(revision) or set(revision) == {"0"}:
        raise ValueError("shared CI revision must be an exact nonzero commit SHA")
    identity = resolve_identity(
        os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_EVENT_NAME"],
        json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text()),
        os.environ["GITHUB_SHA"], os.environ["GITHUB_REF"],
    )
    (Path(os.environ["RUNNER_TEMP"]) / "source-identity.json").write_text(
        json.dumps(identity, sort_keys=True) + "\n"
    )
    names = {"repository": "SOURCE_REPOSITORY", "source_sha": "SOURCE_SHA", "base_sha": "BASE_SHA",
             "source_branch": "SOURCE_BRANCH", "pr_number": "PR_NUMBER", "pr_title": "SOURCE_PR_TITLE"}
    for output_path in (os.environ["GITHUB_ENV"], os.environ["GITHUB_OUTPUT"]):
        with Path(output_path).open("a") as handle:
            for name, value in identity.items():
                handle.write(f"{names[name]}={value}\n")


if __name__ == "__main__":
    main()
