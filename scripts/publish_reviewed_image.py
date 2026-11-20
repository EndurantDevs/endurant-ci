"""Transfer the exact App-approved hosted image without executing source."""

import json
import os
import re
import sys
from pathlib import Path

import render_workflow
import reviewed_source_approval as approval
import source_image as transfer
from source_image_recovery import recipe


REPOSITORY = approval.REPOSITORY
PUBLISHER = ".github/workflows/publish-reviewed-source.yml"
RECIPE = "EndurantDevs/endurant-ci/.github/workflows/healthcare.yml"
REF = "refs/heads/dev"
ARTIFACT_JOBS = {
    "image": ("Hosted validation / Container build", transfer.UPLOAD),
    "measurement": ("Hosted validation / Coverage results", "Upload public measurement"),
    "native": ("Hosted validation / Repository checks", "Upload exact-source Rust test binaries"),
}


def positive(value):
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]*", value):
        raise ValueError("reviewed publication requires exact positive identifiers")
    return int(value)


def dispatch_inputs(environ):
    payload = transfer.read_json(Path(environ["GITHUB_EVENT_PATH"]))
    inputs = payload.get("inputs")
    if (not isinstance(inputs, dict) or set(inputs) != set(render_workflow.REVIEWED_IMAGE_INPUTS)
            or any(environ.get(name.upper()) != value for name, value in inputs.items())):
        raise ValueError("reviewed publication inputs differ from the exact dispatch")
    return {name: positive(value) for name, value in inputs.items()}


def workflow_run(run_id, attempt, path, *, active=False):
    run = transfer.github.api(REPOSITORY, f"actions/runs/{run_id}")
    status, conclusion = ("in_progress", None) if active else ("completed", "success")
    if (type(run.get("id")) is not int or run["id"] != run_id
            or type(run.get("run_attempt")) is not int or run["run_attempt"] != attempt
            or type(run.get("workflow_id")) is not int or run["workflow_id"] <= 0
            or run.get("status") != status or run.get("conclusion") != conclusion
            or run.get("event") != "workflow_dispatch" or run.get("head_branch") != "dev"
            or not transfer.github.exact_sha(run.get("head_sha", ""))
            or run.get("path") not in {path, path + "@dev", path + "@" + REF}
            or any(run.get(name, {}).get("full_name") != REPOSITORY for name in ("repository", "head_repository"))):
        raise ValueError("reviewed publication requires its exact run, attempt and trusted workflow")
    return run


def publisher_context(environ):
    revision = environ["CI_REVISION"]
    validation_blob = environ.get("REVIEWED_VALIDATION_WORKFLOW_BLOB")
    if (environ.get("GITHUB_REPOSITORY") != REPOSITORY or environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
            or environ.get("GITHUB_REF") != REF or environ.get("GITHUB_REF_PROTECTED") != "true"
            or environ.get("GITHUB_JOB") != "dev-image-publication"
            or environ.get("GITHUB_WORKFLOW_REF") != f"{REPOSITORY}/{PUBLISHER}@{REF}"
            or not transfer.github.exact_sha(revision)
            or not transfer.github.exact_sha(validation_blob)
            or transfer.command("git", "-C", str(transfer.ROOT), "rev-parse", "HEAD").decode().strip() != revision):
        raise ValueError("reviewed publication requires its exact protected Core publisher")
    app_id = positive(environ.get("REVIEWED_IMAGE_APPROVAL_APP_ID"))
    run_id = positive(environ.get("GITHUB_RUN_ID"))
    attempt = positive(environ.get("GITHUB_RUN_ATTEMPT"))
    run = workflow_run(run_id, attempt, PUBLISHER, active=True)
    if (run["head_sha"] != environ.get("GITHUB_SHA") or run["head_sha"] != environ.get("GITHUB_WORKFLOW_SHA")
            or run.get("referenced_workflows", []) != []):
        raise ValueError("reviewed publication workflow differs from the active dispatch")
    blob, _ = recipe(REPOSITORY, run["head_sha"], PUBLISHER,
                     expected_workflow=render_workflow.render_reviewed_image_publisher(revision, app_id, validation_blob))
    jobs = transfer.artifacts.attempt_jobs(REPOSITORY, run, attempt)
    if (len(jobs) != 1 or jobs[0].get("name") != transfer.JOB
            or jobs[0].get("status") != "in_progress" or jobs[0].get("conclusion") is not None):
        raise ValueError("reviewed publication requires its unique active publisher job")
    job = jobs[0]
    publisher = {"repository": REPOSITORY, "run_id": str(run_id), "run_attempt": attempt,
                 "head_sha": run["head_sha"], "ci_revision": revision, "workflow_blob_sha": blob,
                 "validation_workflow_blob_sha": validation_blob,
                 "workflow_id": run["workflow_id"], "job_id": job["id"]}
    return publisher, run, job, app_id


def hosted_artifacts(inputs, revision, validation_blob):
    run = workflow_run(inputs["source_run_id"], inputs["source_run_attempt"], approval.VALIDATION_WORKFLOW)
    references = run.get("referenced_workflows", [])
    if (len(references) != 1 or references[0].get("path") != f"{RECIPE}@{revision}"
            or references[0].get("sha") != revision):
        raise ValueError("reviewed hosted run must use the exact pinned validation recipe")
    caller = transfer.github.api(REPOSITORY, f"contents/{approval.VALIDATION_WORKFLOW}?ref={run['head_sha']}")
    if (not transfer.github.exact_sha(validation_blob) or caller.get("type") != "file"
            or caller.get("sha") != validation_blob):
        raise ValueError("reviewed hosted run must use the exact frozen validation workflow")
    inventory = transfer.artifacts.attempt_jobs(REPOSITORY, run, run["run_attempt"])
    if len({job["id"] for job in inventory}) != len(inventory) or len({job["name"] for job in inventory}) != len(inventory):
        raise ValueError("reviewed hosted job inventory is ambiguous")
    jobs = {job["name"]: job for job in inventory}
    required = {"Portable smoke", "Hosted validation / Validation complete", "Hosted validation / Security scans",
                *(job for job, _ in ARTIFACT_JOBS.values())}
    if required - jobs.keys() or any((job.get("status"), job.get("conclusion")) != ("completed", "success")
                                    for job in jobs.values()):
        raise ValueError("reviewed hosted run requires fresh successful portable and hosted validation")
    items = list(transfer.github.pages(REPOSITORY, f"actions/runs/{run['id']}/artifacts", "artifacts"))
    artifacts = {}
    for kind, (job_name, step_name) in ARTIFACT_JOBS.items():
        name = f"{approval.ARTIFACT_NAMES[kind]}-{run['id']}-{run['run_attempt']}"
        matches = [item for item in items if item.get("name") == name]
        steps = [step for step in jobs[job_name].get("steps", []) if step.get("name") == step_name]
        if (len(matches) != 1 or matches[0].get("id") != inputs[f"{kind}_artifact_id"]
                or len(steps) != 1 or (steps[0].get("status"), steps[0].get("conclusion")) != ("completed", "success")
                or not transfer.artifacts.current_evidence(matches[0], run)
                or not transfer.DIGEST.fullmatch(matches[0].get("digest", ""))
                or not transfer.artifacts.timestamp(steps[0]["started_at"])
                <= transfer.artifacts.timestamp(matches[0]["created_at"])
                <= transfer.artifacts.timestamp(steps[0]["completed_at"])):
            raise ValueError("reviewed hosted artifact differs from its fresh successful upload")
        item = transfer.github.api(REPOSITORY, f"actions/artifacts/{matches[0]['id']}")
        owner = item.get("workflow_run") or {}
        if (transfer.snapshot(item) != transfer.snapshot(matches[0]) or owner.get("id") != run["id"]
                or owner.get("head_sha") != run["head_sha"]):
            raise ValueError("reviewed hosted artifact no longer belongs to its exact producer")
        artifacts[kind] = item
    return run, jobs, artifacts


def metadata(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError("reviewed publication metadata must be regular downloaded files")
    return transfer.read_json(path)


def target_image(identity):
    return (f"{transfer.IMAGES[REPOSITORY]}:reviewed-{identity['source_sha'][:8]}-"
            f"{identity['run_id']}-{identity['run_attempt']}")


class ReviewedAuthority(transfer.PublicationAuthority):
    def __init__(self, temporary):
        self.image = temporary / "public-image-download"
        self.measurement = temporary / "reviewed-measurement-download"

    def admit(self):
        publisher, _, _, app_id = publisher_context(os.environ)
        inputs = dispatch_inputs(os.environ)
        run, jobs, artifacts = hosted_artifacts(inputs, publisher["ci_revision"], publisher["validation_workflow_blob_sha"])
        staged = metadata(self.image / "producer.json")
        measurement = metadata(self.measurement / "measurement.json")
        proof = approval.public_proof(staged, measurement, artifacts)
        attempts = measurement.get("producer_attempts")
        if (proof["producer"]["head_sha"] != run["head_sha"] or proof["run_id"] != str(run["id"])
                or proof["run_attempt"] != run["run_attempt"] or proof["ci_revision"] != publisher["ci_revision"]
                or not isinstance(attempts, dict) or not attempts
                or any(type(attempt) is not int or attempt != run["run_attempt"] for attempt in attempts.values())):
            raise ValueError("reviewed proof differs from the exact freshly executed hosted run")
        commit = transfer.github.api(REPOSITORY, f"git/commits/{proof['source_sha']}")
        if (commit.get("sha") != proof["source_sha"] or commit.get("tree", {}).get("sha") != proof["source_tree"]
                or [parent.get("sha") for parent in commit.get("parents", [])] != [proof["base_sha"]]):
            raise ValueError("approved source commit, tree or sole parent differs")
        checks = list(transfer.github.pages(
            REPOSITORY, f"commits/{proof['source_sha']}/check-runs?filter=all&app_id={app_id}", "check_runs"))
        check_id = approval.verify(checks, proof, app_id)
        approved = [check["external_id"] for check in checks if check.get("id") == check_id]
        if len(approved) != 1:
            raise ValueError("reviewed approval changed during check inventory")
        identity = {key: proof[key] for key in approval.SCOPE}
        identity["source_workflow_blob_sha"] = proof["source_workflow_blob_sha"]
        return {
            "identity": identity, "publisher": publisher, "artifact": transfer.snapshot(artifacts["image"]),
            "measurement": transfer.snapshot(artifacts["measurement"]), "native": transfer.snapshot(artifacts["native"]),
            "producer_run_attempt": run["run_attempt"], "measurement_run_attempt": run["run_attempt"],
            "producer_job_id": jobs[ARTIFACT_JOBS["image"][0]]["id"], "image": target_image(identity),
            "publisher_job_id": publisher["job_id"], "workflow_id": publisher["workflow_id"],
            "reviewed_approval": {"app_id": app_id, "check_id": check_id, "external_id": approved[0]},
        }

    def active_publisher(self, expected):
        publisher, run, job, _ = publisher_context(os.environ)
        if publisher != expected["publisher"] or expected["image"] != target_image(expected["identity"]):
            raise ValueError("reviewed publication no longer belongs to its exact active publisher")
        return run, job

    def transfer_record(self, expected, directory):
        return transfer.transfer_record(expected, directory, schema="reviewed-source-image-staging-v1")

    def publication_receipt(self, expected, record, digest):
        result = transfer.publication_receipt(expected, record, digest)
        result.update(schema="reviewed-source-image-publication-v1", publisher=expected["publisher"],
                      approval=expected["reviewed_approval"], measurement_artifact={
                          key: expected["measurement"][key] for key in ("id", "name", "digest")
                      }, native_artifact={key: expected["native"][key] for key in ("id", "name", "digest")})
        return result


def main():
    temporary = Path(os.environ["RUNNER_TEMP"])
    authority = ReviewedAuthority(temporary)
    path = temporary / "public-image-publication.json"
    mode = sys.argv[1]
    if mode == "prepare":
        publisher, _, _, _ = publisher_context(os.environ)
        _, _, artifacts = hosted_artifacts(
            dispatch_inputs(os.environ), publisher["ci_revision"], publisher["validation_workflow_blob_sha"])
        with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
            output.write(f"publish=true\nartifact_id={artifacts['image']['id']}\n"
                         f"measurement_artifact_id={artifacts['measurement']['id']}\n")
    elif mode == "stage":
        expected = authority.admit()
        with path.open("x") as output:
            output.write(json.dumps(expected, sort_keys=True) + "\n")
        transfer.stage(expected, authority.image, authority=authority)
    elif mode == "publish":
        transfer.publish(metadata(path), authority.image, authority=authority)
    elif mode == "reconcile":
        if (temporary / "public-image-intent/intent.json").exists():
            transfer.reconcile(metadata(path), authority=authority)
    else:
        raise ValueError("expected prepare, stage, publish, or reconcile")


if __name__ == "__main__":
    main()
