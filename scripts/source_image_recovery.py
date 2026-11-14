"""Publish a retained tested image from a separately authenticated manual run."""

import base64
import hashlib
import json
import os
import re
import sys
import urllib.parse
from datetime import timedelta, timezone
from pathlib import Path

import render_workflow
import source_image as transfer
import yaml

REPOSITORY = "EndurantDevs/healthcare-mrf-api"
WORKFLOW = ".github/workflows/publish-retained-image.yml"
TITLE = "Retained DEV image publication"
INPUTS = {"source_run_id": "SOURCE_RUN_ID", "source_run_attempt": "SOURCE_RUN_ATTEMPT",
          "image_artifact_id": "IMAGE_ARTIFACT_ID", "measurement_artifact_id": "MEASUREMENT_ARTIFACT_ID"}
SCHEMA = "public-source-image-recovery-v1"
RECONCILED_PACKAGES = {"299748f7704606791db0ffb6809eba85b9f4e1e2"}
SOURCE_RECIPES = {("0ac279b558d1551346804a97d36a44bd3f8b96f2", "299748f7704606791db0ffb6809eba85b9f4e1e2")}
POST_AUTH_STEPS = ("Download validated image archive", "Stage DEV image publication intent",
                   "Upload DEV image publication intent", "Publish validated DEV image",
                   "Upload DEV image receipt", "Reconcile DEV image publication")


def positive(value):
    return type(value) is int and value > 0


def recipe(repository, sha, path, *, expected_workflow=None):
    item = transfer.github.api(repository, f"contents/{path}?ref={sha}")
    if (not isinstance(item, dict) or not transfer.github.SHA.fullmatch(item.get("sha", ""))
            or item.get("encoding") != "base64" or not isinstance(item.get("content"), str)
            or len(item["content"]) > 256 * 1024):
        raise ValueError("image publication requires its exact bounded caller workflow")
    raw = base64.b64decode("".join(item["content"].split()), validate=True)
    blob = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw, usedforsecurity=False).hexdigest()
    if blob != item["sha"] or expected_workflow is not None and raw != expected_workflow.encode():
        raise ValueError("image publication caller bytes differ from the exact trusted workflow")
    workflow = yaml.safe_load(raw)
    job = workflow["jobs"]["dev-image-publication"]
    revision = job.get("env", {}).get("CI_REVISION", workflow.get("env", {}).get("CI_REVISION"))
    checkouts = [step for step in job["steps"] if step.get("name") == "Check out trusted image publisher"]
    if (not isinstance(revision, str) or not transfer.github.SHA.fullmatch(revision) or set(revision) == {"0"}
            or len(checkouts) != 1 or checkouts[0].get("with", {}).get("repository") != "EndurantDevs/endurant-ci"
            or checkouts[0]["with"].get("ref") != revision
            or checkouts[0]["with"].get("persist-credentials") is not False):
        raise ValueError("image publication caller has no exact trusted package pin")
    return item["sha"], revision


def publisher_context():
    repository = os.environ["GITHUB_REPOSITORY"]
    revision = os.environ["CI_REVISION"]
    if (repository != REPOSITORY or os.environ["GITHUB_EVENT_NAME"] != "workflow_dispatch"
            or os.environ["GITHUB_REF"] != "refs/heads/dev" or os.environ["GITHUB_JOB"] != "dev-image-publication"
            or os.environ["GITHUB_WORKFLOW_REF"] != f"{repository}/{WORKFLOW}@refs/heads/dev"
            or not transfer.github.SHA.fullmatch(revision) or set(revision) == {"0"}
            or transfer.command("git", "-C", str(transfer.ROOT), "rev-parse", "HEAD").decode().strip() != revision):
        raise ValueError("recovery requires its exact trusted source-side manual workflow")
    payload = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    inputs = payload.get("inputs")
    if (not isinstance(inputs, dict) or set(inputs) != set(INPUTS)
            or any(not isinstance(inputs[key], str) or not re.fullmatch(r"[1-9][0-9]*", inputs[key])
                   or os.environ.get(environment) != inputs[key] for key, environment in INPUTS.items())):
        raise ValueError("recovery requires exact positive source run, attempt and artifact inputs")
    identifiers = (os.environ["GITHUB_RUN_ID"], os.environ["GITHUB_RUN_ATTEMPT"])
    if any(not re.fullmatch(r"[1-9][0-9]*", value) for value in identifiers):
        raise ValueError("recovery requires its exact publisher run and attempt")
    run = transfer.github.api(repository, f"actions/runs/{identifiers[0]}")
    title = f"Retained DEV image {inputs['source_run_id']}-{inputs['source_run_attempt']}"
    if not (run.get("id") == int(identifiers[0]) and run.get("run_attempt") == int(identifiers[1])
            and run.get("status") == "in_progress" and run.get("conclusion") is None
            and run.get("event") == "workflow_dispatch" and run.get("head_branch") == "dev"
            and run.get("head_sha") == os.environ["GITHUB_SHA"] and run.get("path") == WORKFLOW
            and run.get("name") == TITLE and run.get("display_title") == title
            and positive(run.get("workflow_id")) and run.get("referenced_workflows", []) == []
            and all(run.get(key, {}).get("full_name") == repository for key in ("repository", "head_repository"))):
        raise ValueError("recovery publisher run or workflow identity changed")
    blob, caller_revision = recipe(repository, run["head_sha"], WORKFLOW,
                                   expected_workflow=render_workflow.render_image_recovery("healthcare", revision))
    if caller_revision != revision:
        raise ValueError("recovery caller package differs from its checkout")
    jobs = list(transfer.github.pages(repository, f"actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs", "jobs"))
    if (len(jobs) != 1 or not positive(jobs[0].get("id")) or jobs[0].get("name") != transfer.JOB
            or jobs[0].get("run_id") != run["id"] or jobs[0].get("run_attempt") != run["run_attempt"]
            or jobs[0].get("head_sha") != run["head_sha"] or jobs[0].get("status") != "in_progress"
            or jobs[0].get("conclusion") is not None):
        raise ValueError("recovery requires its unique still-active publisher job")
    publisher = {"repository": repository, "run_id": identifiers[0], "run_attempt": int(identifiers[1]),
                 "source_sha": run["head_sha"], "workflow_blob": blob, "ci_revision": revision,
                 "workflow_id": run["workflow_id"], "job_id": jobs[0]["id"]}
    return publisher, run, jobs[0]


def require_latest(publisher, run):
    runs = list(transfer.github.pages(REPOSITORY,
                f"actions/workflows/{run['workflow_id']}/runs?event=workflow_dispatch&branch=dev", "workflow_runs"))
    matches = [item for item in runs if item.get("display_title") == run["display_title"]]
    if not matches or any(not positive(item.get("id")) or not positive(item.get("run_attempt"))
                          or not isinstance(item.get("created_at"), str) for item in matches):
        raise ValueError("recovery publisher discovery is missing or ambiguous")
    latest = max(matches, key=lambda item: (item["created_at"], item["id"], item["run_attempt"]))
    if latest["id"] != int(publisher["run_id"]) or latest["run_attempt"] != publisher["run_attempt"]:
        raise ValueError("a newer recovery publisher superseded this run")


def require_latest_source(run, commit):
    since = (transfer.artifacts.timestamp(commit["commit"]["committer"]["date"]) - timedelta(days=7)).strftime("%Y-%m-%d")
    query = urllib.parse.urlencode({"event": "push", "branch": "dev", "created": f">={since}"})
    # GitHub's head_sha index can omit known runs; filter the bounded inventory locally.
    runs = list(transfer.github.pages(REPOSITORY,
                f"actions/workflows/{run['workflow_id']}/runs?{query}",
                "workflow_runs"))
    matches = [item for item in runs if item.get("workflow_id") == run["workflow_id"]
               and item.get("head_sha") == run["head_sha"] and item.get("head_branch") == "dev"
               and item.get("event") == "push" and item.get("path") == ".github/workflows/ci.yml"
               and all(item.get(key, {}).get("full_name") == REPOSITORY for key in ("repository", "head_repository"))]
    if not matches or any(not positive(item.get("id")) or not positive(item.get("run_attempt"))
                          or not isinstance(item.get("created_at"), str)
                          or not isinstance(item.get("updated_at", item.get("created_at")), str) for item in matches):
        raise ValueError("original source CI discovery is missing or ambiguous")
    latest = max(matches, key=lambda item: (item.get("updated_at", item["created_at"]), item["created_at"],
                                           item["id"], item["run_attempt"]))
    if (latest["id"] != run["id"] or latest["run_attempt"] != run["run_attempt"]
            or latest.get("status") != "completed" or latest.get("conclusion") != "failure"):
        raise ValueError("a newer original source CI run superseded the retained image")


def failure_state(job):
    steps = job.get("steps", [])
    states = (("pre-auth", ("failure", *("skipped" for _ in POST_AUTH_STEPS))),
              ("reconciled", ("success", "success", "success", "success", "failure", "skipped", "success")),
              ("reconciled", ("success", "success", "success", "success", "success", "failure", "success")))
    for state, conclusions in states:
        if all(len(matches := [step for step in steps if step.get("name") == name]) == 1
               and matches[0].get("status") == "completed" and matches[0].get("conclusion") == conclusion
               for name, conclusion in zip(("Authenticate DEV image input", *POST_AUTH_STEPS), conclusions)):
            return state
    raise ValueError("original publisher has no supported pre-publication or reconciled failure")


def original_context():
    repository = REPOSITORY
    run_id, attempt = os.environ["SOURCE_RUN_ID"], int(os.environ["SOURCE_RUN_ATTEMPT"])
    run = transfer.github.api(repository, f"actions/runs/{run_id}")
    if not (run.get("id") == int(run_id) and run.get("run_attempt") == attempt
            and run.get("status") == "completed" and run.get("conclusion") == "failure"
            and run.get("event") == "push" and run.get("head_branch") == "dev"
            and run.get("path") == ".github/workflows/ci.yml" and positive(run.get("workflow_id"))
            and run.get("referenced_workflows", []) == []
            and all(run.get(key, {}).get("full_name") == repository for key in ("repository", "head_repository"))):
        raise ValueError("original source run is not its current completed failed DEV attempt")
    source = run.get("head_sha", "")
    if not transfer.github.SHA.fullmatch(source) or set(source) == {"0"}:
        raise ValueError("original source SHA is invalid")
    commit = transfer.github.api(repository, f"commits/{source}")
    parents = commit.get("parents", [])
    if commit.get("sha") != source or len(parents) != 1 or not transfer.github.SHA.fullmatch(parents[0].get("sha", "")):
        raise ValueError("recovery requires the original source's exact sole parent")
    require_latest_source(run, commit)
    base, _ = transfer.github.main_source_range(repository, commit, branch="dev")
    blob, revision = recipe(repository, source, ".github/workflows/ci.yml")
    if (blob, revision) not in SOURCE_RECIPES:
        raise ValueError("original source recipe is not approved for retained image recovery")
    identity = {"repository": repository, "source_sha": source, "base_sha": base,
                "source_branch": "dev", "pr_number": "0", "run_id": run_id, "run_attempt": attempt,
                "ci_revision": revision, "caller_workflow_blob_sha": blob}
    transfer.require_dev_ancestor(repository, source)
    jobs = transfer.artifacts.effective_jobs(repository, {**run, "completed": True})
    required = {transfer.PRODUCERS[repository], "Coverage results", "Validation complete", transfer.JOB}
    if not required <= {job.get("name") for job in jobs}:
        raise ValueError("original source validation job inventory is incomplete")
    publishers = [job for job in jobs if job.get("name") == transfer.JOB]
    if (len(publishers) != 1 or publishers[0].get("status") != "completed"
            or publishers[0].get("conclusion") != "failure" or publishers[0].get("run_attempt") != attempt
            or any(job.get("status") != "completed" or job.get("conclusion") != "success"
                   for job in jobs if job.get("name") != transfer.JOB)):
        raise ValueError("every original source validation consumer must pass")
    failure_state(publishers[0])
    return identity, run, jobs


def reconciled_producer(identity, run, jobs, inputs):
    if identity["ci_revision"] not in RECONCILED_PACKAGES:
        raise ValueError("original reconciliation package is not supported")
    publisher = next(job for job in jobs if job["name"] == transfer.JOB)
    items = list(transfer.github.pages(REPOSITORY, f"actions/runs/{run['id']}/artifacts", "artifacts"))
    receipt_steps = [step for step in publisher["steps"] if step["name"] == transfer.RECEIPT_UPLOAD]
    # A failed upload can leave an artifact that never became a successful receipt.
    failed_upload = (len(receipt_steps) == 1 and receipt_steps[0].get("status") == "completed"
                     and receipt_steps[0].get("conclusion") == "failure")
    if not failed_upload and any(item.get("name") == transfer.proof_name(identity) for item in items):
        raise ValueError("original publication has a receipt and cannot be recovered")
    intents = [item for item in items if item.get("name") == transfer.proof_name(identity, True)]
    upload = next(step for step in publisher["steps"] if step["name"] == transfer.INTENT_UPLOAD)
    if (len(intents) != 1 or not transfer.artifacts.current_evidence(intents[0], run)
            or not transfer.DIGEST.fullmatch(intents[0].get("digest", ""))
            or not 0 < intents[0].get("size_in_bytes", 0) <= 2 * transfer.INTENT_MAX_BYTES
            or not (transfer.artifacts.timestamp(upload["started_at"])
                    <= transfer.artifacts.timestamp(intents[0]["created_at"])
                    <= transfer.artifacts.timestamp(upload["completed_at"]))):
        raise ValueError("original reconciliation has no exact successful intent artifact")
    intent = transfer.artifact_payload(REPOSITORY, intents[0], "intent.json", transfer.INTENT_MAX_BYTES)
    started = transfer.artifacts.timestamp(publisher["started_at"]).astimezone(timezone.utc)
    target = f"{transfer.IMAGES[REPOSITORY]}:dev-main-{identity['source_sha'][:8]}-{started:%Y%m%d%H%M%S}"
    expected = {"identity": identity, **inputs, "image": target, "workflow_id": run["workflow_id"],
                "publisher_job_id": publisher["id"]}
    if (intent.get("schema") != "public-source-image-intent-v1" or intent.get("publication") != expected
            or not isinstance(intent.get("producer"), dict)
            or not (started <= transfer.artifacts.timestamp(intent["created_at"])
                    < transfer.artifacts.timestamp(intent["expires_at"])
                    <= transfer.artifacts.timestamp(intent["created_at"]) + timedelta(minutes=30))):
        raise ValueError("original reconciliation intent differs from the retained source and inputs")
    transfer.require_absent_registry_tag(target)
    return intent["producer"]


class RecoveryAuthority(transfer.PublicationAuthority):
    def admit(self):
        publisher, run, job = publisher_context()
        require_latest(publisher, run)
        identity, source_run, jobs = original_context()
        inputs = transfer.image_inputs(identity, source_run, jobs)
        original_publisher = next(job for job in jobs if job["name"] == transfer.JOB)
        prior = ({"prior_producer": reconciled_producer(identity, source_run, jobs, inputs)}
                 if failure_state(original_publisher) == "reconciled" else {})
        tag = f"dev-main-{identity['source_sha'][:8]}-{transfer.artifacts.timestamp(job['started_at']).astimezone(timezone.utc):%Y%m%d%H%M%S}"
        return {"identity": identity, "publisher": publisher, **inputs, **prior,
                "image": f"{transfer.IMAGES[REPOSITORY]}:{tag}", "workflow_id": run["workflow_id"],
                "publisher_job_id": job["id"]}

    def active_publisher(self, expected):
        publisher, run, job = publisher_context()
        tag = f"dev-main-{expected['identity']['source_sha'][:8]}-{transfer.artifacts.timestamp(job['started_at']).astimezone(timezone.utc):%Y%m%d%H%M%S}"
        if publisher != expected["publisher"] or expected["image"] != f"{transfer.IMAGES[REPOSITORY]}:{tag}":
            raise ValueError("recovery no longer belongs to its original active publisher")
        return run, job

    def publication_receipt(self, expected, record, digest):
        result = transfer.publication_receipt(expected, record, digest)
        result.update(schema=SCHEMA, publisher=expected["publisher"],
                      measurement_artifact={key: expected["measurement"][key] for key in ("id", "name", "digest")})
        return result


def main():
    authority = RecoveryAuthority()
    path = Path(os.environ["RUNNER_TEMP"]) / "public-image-publication.json"
    mode = sys.argv[1]
    if mode == "prepare":
        expected = authority.admit()
        path.write_text(json.dumps(expected, sort_keys=True) + "\n")
        with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
            output.write(f"publish=true\nartifact_id={expected['artifact']['id']}\n")
    elif mode in {"stage", "publish"}:
        getattr(transfer, mode)(transfer.read_json(path), Path(os.environ["RUNNER_TEMP"]) / "public-image-download",
                                authority=authority)
    elif mode == "reconcile":
        if not (Path(os.environ["RUNNER_TEMP"]) / "public-image-intent/intent.json").exists():
            print("No staged publication intent; no registry push was authorized.")
            return
        transfer.reconcile(transfer.read_json(path), authority=authority)
    else:
        raise ValueError("expected prepare, stage, publish, or reconcile")


if __name__ == "__main__":
    main()
