"""Bind a tested image to an expiring publication approval."""

import hashlib
import json
import re
from datetime import datetime, timezone

CHECK_NAME = "Reviewed source image publication"
PREFIX = "reviewed-source-image-v1"
EXTERNAL_ID = re.compile(rf"{PREFIX}:([0-9]{{14}}):([0-9a-f]{{64}})\Z")
SHA = re.compile(r"[0-9a-f]{40}\Z")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
ARCHIVE_SHA = re.compile(r"[0-9a-f]{64}\Z")
REPOSITORY = "EndurantDevs/healthcare-mrf-api"
VALIDATION_WORKFLOW = ".github/workflows/reviewed-source-validation.yml"
SCOPE = ("repository", "source_sha", "base_sha", "source_tree", "source_branch", "pr_number",
         "run_id", "run_attempt", "ci_revision", "producer")
ARTIFACT_NAMES = {
    "image": "healthcare-public-image-staging",
    "measurement": "healthcare-public-measurement",
    "native": "healthcare-rust-debug",
}


def _matches(pattern, value):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def public_proof(staged, measurement, artifacts):
    """Canonical proof shared by the approving controller and the package publisher."""
    identity = {key: staged.get(key) for key in SCOPE}
    if (staged.get("schema") != "reviewed-source-image-staging-v1"
            or measurement.get("schema") != "reviewed-source-measurement-v1"
            or any(measurement.get(key) != value for key, value in identity.items())
            or identity["repository"] != REPOSITORY or identity["source_branch"] != "reviewed"
            or identity["pr_number"] != "0" or measurement.get("pr_title") != ""
            or not all(_matches(SHA, identity[key]) and set(identity[key]) != {"0"}
                       for key in ("source_sha", "base_sha", "source_tree", "ci_revision"))):
        raise ValueError("Reviewed hosted image and measurement source identities differ")
    producer, run_id, attempt = identity["producer"], identity["run_id"], identity["run_attempt"]
    if (not isinstance(run_id, str) or re.fullmatch(r"[1-9][0-9]*", run_id) is None
            or type(attempt) is not int or attempt <= 0
            or not isinstance(producer, dict)
            or set(producer) != {"repository", "head_sha", "workflow_sha", "workflow_ref", "run_id", "run_attempt"}
            or producer["repository"] != REPOSITORY or producer["run_id"] != run_id
            or type(producer["run_attempt"]) is not int or producer["run_attempt"] != attempt
            or producer["head_sha"] != producer["workflow_sha"] or not _matches(SHA, producer["head_sha"])
            or producer["workflow_ref"] != f"{REPOSITORY}/{VALIDATION_WORKFLOW}@refs/heads/dev"):
        raise ValueError("Reviewed hosted producer identity is invalid")
    if (staged.get("platform") != "linux/amd64"
            or not _matches(SHA, staged.get("source_workflow_blob_sha"))
            or not _matches(ARCHIVE_SHA, staged.get("archive_sha256"))
            or not all(_matches(DIGEST, staged.get(key)) for key in ("image_id", "config_digest"))):
        raise ValueError("Reviewed hosted image description is incomplete")
    if set(artifacts) != set(ARTIFACT_NAMES):
        raise ValueError("Reviewed hosted proof requires all three artifacts")
    descriptors = {}
    for kind, name in ARTIFACT_NAMES.items():
        artifact = artifacts[kind]
        identifier = artifact.get("id")
        if (type(identifier) is not int or identifier <= 0 or artifact.get("expired") is not False
                or (artifact.get("workflow_run") or {}).get("id") != int(run_id)
                or artifact.get("name") != f"{name}-{run_id}-{attempt}"
                or not _matches(DIGEST, artifact.get("digest"))):
            raise ValueError("Reviewed hosted artifact identity is invalid")
        descriptors[kind] = {key: artifact[key] for key in ("id", "name", "digest")}
    if len({artifact["id"] for artifact in descriptors.values()}) != len(descriptors):
        raise ValueError("Reviewed hosted artifacts must be distinct")
    return {
        **identity, "artifacts": descriptors, "source_workflow_blob_sha": staged["source_workflow_blob_sha"],
        "archive_sha256": staged["archive_sha256"], "image_id": staged["image_id"],
        "platform": staged["platform"], "config_digest": staged["config_digest"],
    }


def external_id(proof, expires_at):
    """Encode the approval of one verified hosted proof in a GitHub check ID."""
    if expires_at.tzinfo is None:
        raise ValueError("Image approval expiry requires a time zone")
    expires = expires_at.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S")
    data = json.dumps(proof, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    digest = hashlib.sha256(data).hexdigest()
    return f"{PREFIX}:{expires}:{digest}"


def verify(checks, proof, app_id, *, now=None):
    """Verify the newest App check for the exact source and tested artifact proof."""
    if type(app_id) is not int or app_id <= 0:
        raise ValueError("Image approval requires the configured GitHub App ID")
    matching = [
        check for check in checks
        if check.get("name") == CHECK_NAME
        and check.get("head_sha") == proof["source_sha"]
        and (check.get("app") or {}).get("id") == app_id
        and type(check.get("id")) is int
        and check["id"] > 0
    ]
    latest = max(matching, key=lambda check: check["id"]) if matching else {}
    match = EXTERNAL_ID.fullmatch(latest.get("external_id") or "")
    if not match or latest.get("status") != "completed" or latest.get("conclusion") != "success":
        raise ValueError("Current reviewed source image approval is unavailable")
    expires_at = datetime.strptime(match[1], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    if (now if now is not None else datetime.now(timezone.utc)) >= expires_at:
        raise ValueError("Reviewed source image approval expired")
    producer = proof["producer"]
    link = f"https://github.com/{producer['repository']}/actions/runs/{producer['run_id']}"
    if latest["external_id"] != external_id(proof, expires_at) or latest.get("details_url") != link:
        raise ValueError("Reviewed source image approval differs from the tested proof")
    return latest["id"]
