#!/usr/bin/env python3
"""Transfer one DEV-tested image through a temporary artifact, without rebuilding."""

from datetime import datetime, timedelta, timezone
import base64
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile

import artifact_cleanup as artifacts
import source_identity as github

ROOT = Path(__file__).resolve().parents[1]
JOB = "DEV image publication"
IMAGES = {"EndurantDevs/healthcare-mrf-api": "ghcr.io/endurantdevs/healthcare-mrf-api-dev",
          "EndurantDevs/drug-api": "ghcr.io/endurantdevs/drug-api"}
PRODUCERS = {"EndurantDevs/healthcare-mrf-api": "Container build", "EndurantDevs/drug-api": "Tests and build"}
UPLOAD = "Upload tested DEV image"
INTENT_UPLOAD = "Upload DEV image publication intent"
RECEIPT_UPLOAD = "Upload DEV image receipt"
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
IDENTITY_FIELDS = ("repository", "source_sha", "base_sha", "source_branch", "pr_number")


def require_labels(configuration, identity):
    labels = configuration.get("config", {}).get("Labels") or {}
    expected = {"org.endurantdevs.public-ci.repository": identity["repository"],
                "org.endurantdevs.public-ci.run": f"{identity['run_id']}-{identity['run_attempt']}"}
    if identity["repository"] == "EndurantDevs/drug-api":
        expected["org.opencontainers.image.revision"] = identity["source_sha"]
    if any(labels.get(key) != value for key, value in expected.items()):
        raise ValueError("tested image labels do not belong to this exact source run and attempt")


def archive_identity(path, identity=None, *, require_provenance=False):
    """Read bounded metadata only; Docker and OCI loaders must select the same untagged image."""
    with tarfile.open(path, "r:gz") as archive:
        members = {}
        for item in archive:
            name = item.name.rstrip("/")
            if (len(members) >= 4096 or name in members or not (item.isfile() or item.isdir())
                    or name != PurePosixPath(name).as_posix() or PurePosixPath(name).is_absolute()
                    or ".." in PurePosixPath(name).parts or "\\" in name):
                raise ValueError("image archive has duplicate, unsafe, or excessive members")
            members[name] = item

        def contents(name):
            item = members.get(name)
            if item is None or not item.isfile() or not 0 < item.size <= 65536:
                raise ValueError("image archive metadata is missing, indirect, or oversized")
            return archive.extractfile(item).read()

        manifest = json.loads(contents("manifest.json"))
        if (not isinstance(manifest, list) or len(manifest) != 1 or manifest[0].get("RepoTags") not in (None, [])
                or not set(manifest[0]) <= {"Config", "RepoTags", "Layers", "LayerSources", "Parent"}):
            raise ValueError("image archive must contain exactly one untagged Docker image")
        config = contents(manifest[0]["Config"])
        configuration = json.loads(config)
        config_digest = "sha256:" + hashlib.sha256(config).hexdigest()
        if configuration.get("os") != "linux" or configuration.get("architecture") != "amd64":
            raise ValueError("image archive must contain the native linux/amd64 configuration")
        if identity is not None:
            require_labels(configuration, identity)
        layers = manifest[0].get("Layers")
        if (not isinstance(layers, list) or any(name not in members or not members[name].isfile() for name in layers)
                or len(layers) != len(configuration["rootfs"]["diff_ids"])):
            raise ValueError("image archive layer inventory differs from its configuration")
        identifiers = {config_digest}
        has_provenance = False
        if "index.json" in members or "oci-layout" in members:
            if json.loads(contents("oci-layout")) != {"imageLayoutVersion": "1.0.0"}:
                raise ValueError("unsupported image archive OCI layout")
            index = json.loads(contents("index.json"))
            roots = index.get("manifests", [])
            if len(roots) != 1 or not set(index) <= {"schemaVersion", "mediaType", "manifests", "annotations"}:
                raise ValueError("image archive must contain one OCI root")

            def descriptor(item):
                annotations = item.get("annotations", {})
                digest = item.get("digest", "")
                if (not set(item) <= {"mediaType", "digest", "size", "annotations", "platform", "artifactType", "urls", "data"}
                        or not DIGEST.fullmatch(digest) or any(name in annotations for name in
                        ("io.containerd.image.name", "org.opencontainers.image.ref.name"))):
                    raise ValueError("image archive OCI descriptor can introduce another tag")
                raw = contents("blobs/sha256/" + digest[7:])
                if "sha256:" + hashlib.sha256(raw).hexdigest() != digest or item.get("size") != len(raw):
                    raise ValueError("image archive OCI descriptor bytes changed")
                value = json.loads(raw)
                if not set(value) <= {"schemaVersion", "mediaType", "manifests", "config", "layers", "annotations", "artifactType", "subject"}:
                    raise ValueError("unsupported OCI manifest fields")
                return value

            root = descriptor(roots[0])
            identifiers.add(roots[0]["digest"])
            if "manifests" in root:
                native = [item for item in root["manifests"] if item.get("platform") == {"os": "linux", "architecture": "amd64"}]
                if len(native) != 1:
                    raise ValueError("image archive OCI index must select one native image")
                for item in root["manifests"]:
                    if item == native[0]:
                        continue
                    if (item.get("platform") != {"os": "unknown", "architecture": "unknown"}
                            or item.get("annotations", {}).get("vnd.docker.reference.type") != "attestation-manifest"
                            or item["annotations"].get("vnd.docker.reference.digest") != native[0]["digest"]):
                        raise ValueError("image archive contains another OCI runtime image")
                    attestation = descriptor(item)
                    layers = attestation.get("layers", [])
                    subject = attestation.get("subject", {})
                    if (attestation.get("artifactType") != "application/vnd.docker.attestation.manifest.v1+json"
                            or subject.get("digest") != native[0]["digest"] or len(layers) != 1
                            or layers[0].get("mediaType") != "application/vnd.in-toto+json"
                            or layers[0].get("annotations", {}).get("in-toto.io/predicate-type")
                            != "https://slsa.dev/provenance/v1"):
                        raise ValueError("image archive attestation is not native SLSA provenance")
                    layer = members.get("blobs/sha256/" + layers[0].get("digest", "")[7:])
                    if (not DIGEST.fullmatch(layers[0].get("digest", "")) or layer is None or not layer.isfile()
                            or layer.size != layers[0].get("size") or not 0 < layer.size <= 8 * 1024 * 1024
                            or "sha256:" + hashlib.file_digest(archive.extractfile(layer), "sha256").hexdigest()
                            != layers[0]["digest"]):
                        raise ValueError("image archive SLSA provenance bytes changed")
                    has_provenance = True
                root = descriptor(native[0])
            if (root.get("config", {}).get("digest") != config_digest
                    or contents("blobs/sha256/" + config_digest[7:]) != config):
                raise ValueError("Docker and OCI archive configurations differ")
        if require_provenance and not has_provenance:
            raise ValueError("image archive must preserve native SLSA provenance")
        return config_digest, identifiers


def command(*arguments, **kwargs):
    return subprocess.run(arguments, check=True, stdout=subprocess.PIPE, **kwargs).stdout


def file_digest(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def read_json(path):
    with path.open("rb") as handle:
        content = handle.read(65537)
    if len(content) > 65536:
        raise ValueError("image identity exceeds the bounded metadata limit")
    return json.loads(content)


def image_identity(image):
    values = json.loads(command("docker", "image", "inspect", image))
    if (len(values) != 1 or not DIGEST.fullmatch(values[0].get("Id", ""))
            or values[0].get("Os") != "linux" or values[0].get("Architecture") != "amd64"):
        raise ValueError("the tested image must have one exact native linux/amd64 configuration")
    return values[0]


def configure_containerd_store():
    if (os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted"
            or sys.platform != "linux"):
        raise ValueError("attested image transport requires a fresh GitHub-hosted Linux runner")
    expected = ["driver-type", "io.containerd.snapshotter.v1"]
    if expected in json.loads(command("docker", "info", "--format", "{{json .DriverStatus}}")):
        return
    daemon = Path("/etc/docker/daemon.json")
    if daemon.is_symlink():
        raise ValueError("Docker daemon configuration cannot be a symlink")
    configuration = json.loads(command("sudo", "cat", str(daemon))) if daemon.exists() else {}
    if not isinstance(configuration, dict) or not isinstance(configuration.get("features", {}), dict):
        raise ValueError("Docker daemon configuration is malformed")
    configuration.setdefault("features", {})["containerd-snapshotter"] = True
    with tempfile.NamedTemporaryFile("w", dir=os.environ["RUNNER_TEMP"], delete=False) as output:
        json.dump(configuration, output, sort_keys=True)
        candidate = Path(output.name)
    try:
        command("sudo", "dockerd", "--validate", "--config-file", str(candidate))
        command("sudo", "install", "-o", "root", "-g", "root", "-m", "0644", str(candidate), str(daemon))
        command("sudo", "systemctl", "restart", "docker")
    finally:
        candidate.unlink()
    if expected not in json.loads(command("docker", "info", "--format", "{{json .DriverStatus}}")):
        raise ValueError("Docker did not activate the containerd image store")


def context():
    repository = os.environ["GITHUB_REPOSITORY"]
    revision = os.environ["CI_REVISION"]
    if (repository not in IMAGES or not github.SHA.fullmatch(revision) or set(revision) == {"0"}
            or command("git", "-C", str(ROOT), "rev-parse", "HEAD").decode().strip() != revision):
        raise ValueError("image transfer requires its exact trusted public CI checkout")
    event = os.environ["GITHUB_EVENT_NAME"]
    payload = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    identity = github.resolve_identity(repository, event, payload, os.environ["GITHUB_SHA"], os.environ["GITHUB_REF"])
    identifiers = [os.environ["GITHUB_RUN_ID"], os.environ["GITHUB_RUN_ATTEMPT"]]
    if any(not re.fullmatch(r"[1-9][0-9]*", value) for value in identifiers):
        raise ValueError("image transfer requires one exact run and attempt")
    identity = {key: identity[key] for key in IDENTITY_FIELDS}
    identity.update(run_id=identifiers[0], run_attempt=int(identifiers[1]), ci_revision=revision)
    expected = {"id": int(identifiers[0]), "run_attempt": int(identifiers[1]), "event": event,
                "head_sha": identity["source_sha"], "head_branch": identity["source_branch"],
                "path": ".github/workflows/ci.yml"}
    head_repository = repository
    if event == "pull_request":
        expected["head_branch"] = payload["pull_request"]["head"]["ref"]
        head_repository = payload["pull_request"]["head"]["repo"]["full_name"]
    run = github.api(repository, f"actions/runs/{identifiers[0]}")
    if not (run.get("status") == "in_progress" and run.get("conclusion") is None
            and type(run.get("workflow_id")) is int and run["workflow_id"] > 0
            and run.get("repository", {}).get("full_name") == repository
            and run.get("head_repository", {}).get("full_name") == head_repository
            and all(run.get(key) == value for key, value in expected.items())):
        raise ValueError("image transfer run, source, or attempt changed")
    return identity, run


def staging_name(identity):
    return f"{artifacts.KINDS[identity['repository']]}-public-image-staging-{identity['run_id']}-{identity['run_attempt']}"


def export_image(image):
    if not DIGEST.fullmatch(image):
        raise ValueError("export requires the immutable image ID captured before testing")
    identity, run = context()
    if run["event"] != "push" or identity["source_branch"] != "dev":
        raise ValueError("image export is restricted to DEV pushes")
    source = Path(os.environ["SOURCE_ROOT"])
    if command("git", "-C", str(source), "rev-parse", "HEAD").decode().strip() != identity["source_sha"]:
        raise ValueError("tested image source checkout changed")
    caller = command("git", "-C", str(source), "rev-parse", "HEAD:.github/workflows/ci.yml").decode().strip()
    if not github.SHA.fullmatch(caller):
        raise ValueError("image export requires the exact caller workflow blob")
    inspected = image_identity(image)
    if inspected["Id"] != image:
        raise ValueError("the tested immutable image identity changed")
    require_labels({"config": inspected["Config"]}, identity)
    destination = Path(os.environ["RUNNER_TEMP"]) / staging_name(identity)
    destination.mkdir(mode=0o700)  # Fresh per-job path; never reuse an earlier export.
    archive = destination / "image.tar.gz"
    with archive.open("xb") as output, gzip.GzipFile(fileobj=output, mode="wb", mtime=0) as compressed:
        with subprocess.Popen(["docker", "image", "save", image], stdout=subprocess.PIPE) as save:
            shutil.copyfileobj(save.stdout, compressed)
            if save.wait() != 0:
                raise ValueError("tested image export failed")
    config_digest, identifiers = archive_identity(archive, identity, require_provenance=True)
    if image not in identifiers:
        raise ValueError("exported archive differs from the tested immutable image")
    record = {"schema": "public-source-image-staging-v1", **identity,
              "caller_workflow_blob_sha": caller, "image_id": image,
              "platform": "linux/amd64", "config_digest": config_digest, "archive_sha256": file_digest(archive)}
    (destination / "producer.json").write_text(json.dumps(record, sort_keys=True) + "\n")


def snapshot(item):
    return {key: item.get(key) for key in artifacts.ARTIFACT_FIELDS}


def admit():
    if os.environ["GITHUB_JOB"] != "dev-image-publication":
        raise ValueError("publication requires its own trusted job")
    identity, run = context()
    repository = identity["repository"]
    jobs = list(github.pages(repository, f"actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs", "jobs"))
    own = [job for job in jobs if job.get("name") == JOB]
    if (len(own) != 1 or len({job.get("id") for job in jobs}) != len(jobs)
            or len({job.get("name") for job in jobs}) != len(jobs)
            or own[0].get("status") != "in_progress" or own[0].get("conclusion") is not None):
        raise ValueError("publication requires a unique active trusted job")
    for job in jobs:
        if (type(job.get("id")) is not int or job["id"] <= 0 or job.get("run_id") != run["id"]
                or job.get("run_attempt") != run["run_attempt"] or job.get("head_sha") != run["head_sha"]):
            raise ValueError("publication job identity differs from its run attempt")
    if run["event"] != "push" or identity["source_branch"] != "dev":
        return None  # Authenticated PR/main no-op, including fork PRs; no Docker or registry access.
    if github.api(repository, "commits/dev").get("sha") != identity["source_sha"]:
        raise ValueError("DEV source advanced before publication")
    for job in jobs:
        if job["name"] == JOB:
            continue
        if job["name"] == artifacts.CLEANUP_JOB and job.get("status") == "queued" and job.get("conclusion") is None:
            continue
        if job.get("status") != "completed" or job.get("conclusion") != "success":
            raise ValueError("all image consumers must pass before publication")
    producer = [job for job in jobs if job["name"] == PRODUCERS[repository]]
    if len(producer) != 1:
        raise ValueError("the exact image producer is missing")
    uploads = [step for step in producer[0].get("steps", []) if step.get("name") == UPLOAD]
    if len(uploads) != 1 or uploads[0].get("status") != "completed" or uploads[0].get("conclusion") != "success":
        raise ValueError("the exact image producer upload must succeed")
    items = list(github.pages(repository, f"actions/runs/{run['id']}/artifacts", "artifacts"))
    candidates = [item for item in items if item.get("name") == staging_name(identity)]
    finals = [item for item in items if item.get("name") ==
              f"{artifacts.KINDS[repository]}-public-measurement-{run['id']}-{run['run_attempt']}"]
    if (len(candidates) != 1 or not artifacts.available(candidates[0], run)
            or not DIGEST.fullmatch(candidates[0].get("digest", "")) or candidates[0].get("size_in_bytes", 0) <= 0
            or not (artifacts.timestamp(uploads[0]["started_at"]) <= artifacts.timestamp(candidates[0]["created_at"])
                    <= artifacts.timestamp(uploads[0]["completed_at"]))
            or len(finals) != 1 or not artifacts.durable(finals[0], run)):
        raise ValueError("publication requires its exact completed artifact and durable measurement")
    caller = github.api(repository, f"contents/.github/workflows/ci.yml?ref={identity['source_sha']}").get("sha", "")
    if not github.SHA.fullmatch(caller):
        raise ValueError("publication caller workflow identity is missing")
    identity = {**identity, "caller_workflow_blob_sha": caller}
    tag = f"dev-main-{identity['source_sha'][:8]}-{artifacts.timestamp(own[0]['started_at']).astimezone(timezone.utc):%Y%m%d%H%M%S}"
    return {"identity": identity, "artifact": snapshot(candidates[0]), "measurement": snapshot(finals[0]),
            "image": f"{IMAGES[repository]}:{tag}", "workflow_id": run["workflow_id"],
            "producer_job_id": producer[0]["id"], "publisher_job_id": own[0]["id"]}


def registry_config(image, digest, environment):
    raw = command("docker", "buildx", "imagetools", "inspect", f"{image}@{digest}", "--raw", env=environment)
    if "sha256:" + hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("published registry manifest bytes do not match their digest")
    manifest = json.loads(raw)
    if "manifests" in manifest:
        matches = [item for item in manifest["manifests"] if item.get("platform") == {"os": "linux", "architecture": "amd64"}]
        if len(matches) != 1 or not DIGEST.fullmatch(matches[0].get("digest", "")):
            raise ValueError("published registry index has no unique native image")
        raw = command("docker", "buildx", "imagetools", "inspect", f"{image}@{matches[0]['digest']}", "--raw", env=environment)
        if "sha256:" + hashlib.sha256(raw).hexdigest() != matches[0]["digest"]:
            raise ValueError("published native manifest digest changed")
        manifest = json.loads(raw)
    return manifest["config"]["digest"]


def image_present(image):
    if DIGEST.fullmatch(image):
        return image.encode() in command("docker", "image", "ls", "--all", "--quiet", "--no-trunc").splitlines()
    return bool(command("docker", "image", "ls", "--quiet", image).strip())


def remove_image(image):
    if image_present(image):
        command("docker", "image", "rm", image)
    if image_present(image):
        raise ValueError("unable to verify exact publication image cleanup")


class NoRegistryRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, *args, **kwargs):
        return None  # Expose redirects; never forward registry or GitHub credentials.


def registry_read(image, reference, *, blob=False):
    if image not in IMAGES.values() or not (DIGEST.fullmatch(reference)
            or not blob and re.fullmatch(r"dev-main-[0-9a-f]{8}-[0-9]{14}", reference)):
        raise ValueError("registry read is outside the fixed DEV image scope")
    scope = image.removeprefix("ghcr.io/")
    opener = urllib.request.build_opener(NoRegistryRedirect)
    credential = base64.b64encode(f"{os.environ['GITHUB_ACTOR']}:{os.environ['GH_TOKEN']}".encode()).decode()
    query = urllib.parse.urlencode({"service": "ghcr.io", "scope": f"repository:{scope}:pull"})
    request = urllib.request.Request("https://ghcr.io/token?" + query, headers={"Authorization": "Basic " + credential})
    with opener.open(request, timeout=30) as response:
        body = response.read(65537)
        if response.status != 200 or len(body) > 65536:
            raise ValueError("registry read authority is missing or oversized")
        token = json.loads(body).get("token", "")
    if not isinstance(token, str) or not token or any(c.isspace() for c in token):
        raise ValueError("registry read token is missing or malformed")
    request = urllib.request.Request(f"https://ghcr.io/v2/{scope}/{'blobs' if blob else 'manifests'}/{reference}", headers={
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.oci.image.index.v1+json, application/vnd.oci.image.manifest.v1+json, "
                  "application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.docker.distribution.manifest.v2+json"})
    try:
        with opener.open(request, timeout=30) as response:
            body = response.read(65537)
            if response.status != 200 or len(body) > 65536:
                raise ValueError("registry metadata response is missing or oversized")
            return body
    except urllib.error.HTTPError as error:
        with error:
            body = error.read(65537)
        if blob and error.code in (302, 307):
            location = error.headers.get("Location", "")
            parsed = urllib.parse.urlsplit(location)
            if (len(location) > 8192 or parsed.scheme != "https" or parsed.hostname != "pkg-containers.githubusercontent.com"
                    or parsed.username or parsed.password or parsed.port not in (None, 443)):
                raise ValueError("registry configuration redirect is outside its fixed blob host") from None
            # Signed blob transport needs no registry credential. A second redirect fails closed.
            with opener.open(urllib.request.Request(location), timeout=30) as response:
                body = response.read(65537)
                if response.status != 200 or len(body) > 65536:
                    raise ValueError("registry configuration is missing or oversized")
                return body
        if (error.code != 404 or len(body) > 65536
                or blob or DIGEST.fullmatch(reference)
                or [item.get("code") for item in json.loads(body).get("errors", [])] != ["MANIFEST_UNKNOWN"]):
            raise ValueError("registry tag absence could not be authenticated") from None
        return None


def require_absent_registry_tag(target):
    image, tag = target.rsplit(":", 1)
    if registry_read(image, tag) is not None:
        raise ValueError("publication cannot overwrite an existing registry tag")


def package_api(image, suffix="", method="GET"):
    if (image not in IMAGES.values() or method not in {"GET", "DELETE"}
            or not re.fullmatch(r"(?:/versions(?:/[1-9][0-9]*|\?per_page=100&page=[1-9][0-9]*)?)?", suffix)
            or method == "DELETE" and not re.fullmatch(r"/versions/[1-9][0-9]*", suffix)):
        raise ValueError("package request is outside the fixed DEV image scope")
    name = image.rsplit("/", 1)[1]
    request = urllib.request.Request(f"https://api.github.com/orgs/EndurantDevs/packages/container/{name}{suffix}",
        method=method, headers={"Authorization": f"Bearer {os.environ['GH_TOKEN']}",
        "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
    with urllib.request.build_opener(NoRegistryRedirect).open(request, timeout=30) as response:
        if method == "DELETE":
            if response.status != 204:
                raise ValueError("exact package version deletion did not return 204")
            return None
        body = response.read(16 * 1024 * 1024 + 1)
        if response.status != 200 or len(body) > 16 * 1024 * 1024:
            raise ValueError("package response is missing or oversized")
        return json.loads(body)


def package_versions(image):
    versions = []
    for page in range(1, 101):
        batch = package_api(image, f"/versions?per_page=100&page={page}")
        if not isinstance(batch, list) or len(batch) > 100:
            raise ValueError("package version inventory is malformed")
        versions.extend(batch)
        if len(batch) < 100:
            ids = [item.get("id") for item in versions]
            if any(type(value) is not int or value <= 0 for value in ids) or len(set(ids)) != len(ids):
                raise ValueError("package version inventory is ambiguous")
            return versions
    raise ValueError("package version inventory exceeds complete pagination")


def package_id(image):
    value = package_api(image)
    if (type(value.get("id")) is not int or value["id"] <= 0 or value.get("package_type") != "container"
            or value.get("name") != image.rsplit("/", 1)[1]):
        raise ValueError("package identity changed")
    return value["id"]


def active_publisher(expected):
    """Same-job authority survives source advancement and earlier step failures."""
    identity, run = context()
    if (os.environ["GITHUB_JOB"] != "dev-image-publication" or run["event"] != "push"
            or identity["source_branch"] != "dev" or expected["workflow_id"] != run["workflow_id"]
            or any(expected["identity"].get(key) != value for key, value in identity.items())):
        raise ValueError("publication no longer belongs to this active DEV run")
    jobs = list(github.pages(identity["repository"], f"actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs", "jobs"))
    own = [job for job in jobs if job.get("name") == JOB]
    if (len(own) != 1 or len({job.get("id") for job in jobs}) != len(jobs)
            or len({job.get("name") for job in jobs}) != len(jobs)
            or own[0].get("id") != expected["publisher_job_id"]
            or own[0].get("status") != "in_progress" or own[0].get("conclusion") is not None
            or any(job.get("run_id") != run["id"] or job.get("run_attempt") != run["run_attempt"]
                   or job.get("head_sha") != run["head_sha"] for job in jobs)):
        raise ValueError("cleanup requires its exact unique still-active publisher job")
    tag = f"dev-main-{identity['source_sha'][:8]}-{artifacts.timestamp(own[0]['started_at']).astimezone(timezone.utc):%Y%m%d%H%M%S}"
    if expected["image"] != f"{IMAGES[identity['repository']]}:{tag}":
        raise ValueError("publication target does not belong to its exact publisher")
    return run, own[0]


def proof_name(identity, intent=False):
    return f"{artifacts.KINDS[identity['repository']]}-public-image-{'intent-' if intent else ''}{identity['run_id']}-{identity['run_attempt']}"


def uploaded_payload(expected, run, job, *, intent=False):
    """Authenticate the immutable uploaded ZIP and its single bounded JSON payload."""
    identity = expected["identity"]
    repository = identity["repository"]
    name = proof_name(identity, intent)
    items = [item for item in github.pages(repository, f"actions/runs/{run['id']}/artifacts", "artifacts") if item.get("name") == name]
    steps = [step for step in job.get("steps", []) if step.get("name") == (INTENT_UPLOAD if intent else RECEIPT_UPLOAD)]
    if not intent and len(steps) == 1 and steps[0].get("status") == "completed":
        # The private consumer requires this exact step and the whole attempt to succeed.
        # A terminal failed upload cannot authorize deployment, even if its backend
        # committed an artifact before losing the response. A rerun is another attempt.
        if steps[0].get("conclusion") in {"failure", "cancelled"}:
            return None
        if not items and steps[0].get("conclusion") == "skipped":
            return None
    if (len(items) != 1 or len(steps) != 1 or steps[0].get("status") != "completed"
            or steps[0].get("conclusion") != "success" or not artifacts.durable(items[0], run)
            or not 0 < items[0].get("size_in_bytes", 0) <= 131072 or not DIGEST.fullmatch(items[0].get("digest", ""))
            or not (artifacts.timestamp(steps[0]["started_at"]) <= artifacts.timestamp(items[0]["created_at"])
                    <= artifacts.timestamp(steps[0]["completed_at"]))):
        raise ValueError("durable publication upload is missing, ambiguous, or incomplete")
    item = items[0]
    if snapshot(github.api(repository, f"actions/artifacts/{item['id']}")) != snapshot(item):
        raise ValueError("durable publication artifact identity changed")
    raw = command("gh", "api", f"repos/{repository}/actions/artifacts/{item['id']}/zip")
    if len(raw) > 131072 or "sha256:" + hashlib.sha256(raw).hexdigest() != item["digest"]:
        raise ValueError("durable publication artifact bytes changed")
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        entries = archive.infolist()
        if (len(entries) != 1 or entries[0].filename != ("intent.json" if intent else "image.json")
                or not 0 < entries[0].file_size <= 65536 or (entries[0].external_attr >> 16) & 0o170000 == 0o120000):
            raise ValueError("durable publication artifact has unexpected files")
        return json.loads(archive.read(entries[0]))


def transfer_record(expected, directory):
    if set(path.name for path in directory.iterdir()) != {"producer.json", "image.tar.gz"} or any(
            not path.is_file() or path.is_symlink() for path in directory.iterdir()):
        raise ValueError("unexpected files in the image transfer artifact")
    record = read_json(directory / "producer.json")
    metadata = {"schema": "public-source-image-staging-v1", **expected["identity"], "platform": "linux/amd64"}
    if (set(record) != set(metadata) | {"image_id", "config_digest", "archive_sha256"}
            or any(record.get(key) != value for key, value in metadata.items())
            or not DIGEST.fullmatch(record.get("config_digest", ""))
            or not re.fullmatch(r"[0-9a-f]{64}", record.get("archive_sha256", ""))
            or file_digest(directory / "image.tar.gz") != record["archive_sha256"]):
        raise ValueError("image archive is not bound to this exact source and producer")
    config_digest, identifiers = archive_identity(
        directory / "image.tar.gz", expected["identity"], require_provenance=True)
    if record["config_digest"] != config_digest or record["image_id"] not in identifiers:
        raise ValueError("archive configuration differs from the tested immutable image")
    return record, identifiers


def stage(expected, directory):
    if admit() != expected:
        raise ValueError("publication authority changed before durable intent")
    record, _ = transfer_record(expected, directory)
    created = datetime.now(timezone.utc)
    image = IMAGES[expected["identity"]["repository"]]
    identifier = package_id(image)
    versions = package_versions(image)
    require_absent_registry_tag(expected["image"])
    intent = {"schema": "public-source-image-intent-v1", "publication": expected, "producer": record,
              "package_id": identifier, "prior_version_ids": sorted(item["id"] for item in versions),
              "created_at": created.isoformat(), "expires_at": (created + timedelta(minutes=30)).isoformat()}
    content = json.dumps(intent, sort_keys=True) + "\n"
    if len(content.encode()) > 65536:
        raise ValueError("complete publication intent exceeds its metadata bound")
    destination = Path(os.environ["RUNNER_TEMP"]) / "public-image-intent"
    destination.mkdir(mode=0o700)
    (destination / "intent.json").write_text(content)


def authenticated_intent(expected):
    path = Path(os.environ["RUNNER_TEMP"]) / "public-image-intent/intent.json"
    if path.is_symlink() or not path.is_file():
        raise ValueError("publication requires its fresh local intent")
    intent = read_json(path)
    run, job = active_publisher(expected)
    now = datetime.now(timezone.utc)
    if (intent.get("schema") != "public-source-image-intent-v1" or intent.get("publication") != expected
            or not (artifacts.timestamp(job["started_at"]) <= artifacts.timestamp(intent["created_at"]) <= now
                    < artifacts.timestamp(intent["expires_at"]) <= artifacts.timestamp(intent["created_at"]) + timedelta(minutes=30))
            or type(intent.get("package_id")) is not int or intent["package_id"] <= 0
            or not isinstance(intent.get("prior_version_ids"), list)
            or any(type(value) is not int or value <= 0 for value in intent["prior_version_ids"])
            or sorted(set(intent["prior_version_ids"])) != intent["prior_version_ids"]
            or uploaded_payload(expected, run, job, intent=True) != intent):
        raise ValueError("publication intent is not its exact authenticated durable record")
    return intent, run, job


def publication_receipt(expected, record, digest):
    if not DIGEST.fullmatch(digest):
        raise ValueError("publication receipt requires an exact manifest digest")
    return {"schema": "public-source-image-v1", **expected["identity"],
            "producer_artifact": {key: expected["artifact"][key] for key in ("id", "name", "digest")},
            "archive_sha256": record["archive_sha256"], "platform": "linux/amd64",
            "config_digest": record["config_digest"], "image": expected["image"], "manifest_digest": digest}


def publish(expected, directory):
    if admit() != expected:
        raise ValueError("publication authority changed after download authorization")
    record, identifiers = transfer_record(expected, directory)
    intent, _, _ = authenticated_intent(expected)
    if intent["producer"] != record:
        raise ValueError("tested image differs from its durable pre-push intent")
    configure_containerd_store()
    repository = expected["identity"]["repository"]
    target = expected["image"]
    for name in (*sorted(identifiers), target):
        if image_present(name):
            raise ValueError("publication cannot replace a pre-existing local image")
    loaded = tagged = False
    try:
        loaded = True
        output = command("docker", "image", "load", "--quiet", "--input", str(directory / "image.tar.gz"))
        restored = re.findall(rb"^Loaded image ID: (sha256:[0-9a-f]{64})$", output, re.MULTILINE)
        if (len(restored) != 1 or restored[0].decode() not in identifiers
                or output.strip() != b"Loaded image ID: " + restored[0]):
            raise ValueError("Docker did not restore the single validated image")
        image = restored[0].decode()
        if image_identity(image)["Id"] != image:
            raise ValueError("loaded immutable image identity changed")
        tagged = True
        command("docker", "image", "tag", image, target)
        if admit() != expected:
            raise ValueError("publication authority changed before registry push")
        if authenticated_intent(expected)[0] != intent:
            raise ValueError("durable intent changed before registry push")
        with tempfile.TemporaryDirectory(prefix="public-image-auth-", dir=os.environ["RUNNER_TEMP"]) as authentication:
            environment = {**os.environ, "DOCKER_CONFIG": authentication}
            command("docker", "login", "ghcr.io", "--username", os.environ["GITHUB_ACTOR"], "--password-stdin",
                    input=os.environ["GH_TOKEN"].encode(), env=environment)
            require_absent_registry_tag(target)
            command("docker", "image", "push", target, env=environment)
            digest = command("docker", "buildx", "imagetools", "inspect", target,
                             "--format", '{{printf "%s" .Manifest.Digest}}', env=environment).decode().strip()
            if digest != record["image_id"]:
                raise ValueError("published registry graph differs from the tested image")
            # The exact root retains the native manifest and its SLSA child; config binds runtime bytes.
            if registry_config(IMAGES[repository], digest, environment) != record["config_digest"]:
                raise ValueError("published registry image differs from the tested configuration")
        if admit() != expected:
            raise ValueError("publication authority changed before its receipt")
        result = publication_receipt(expected, record, digest)
    finally:
        failure = None
        for name, owned in [(target, tagged), *((identifier, loaded) for identifier in sorted(identifiers))]:
            if owned:
                try:
                    remove_image(name)
                except (ValueError, subprocess.CalledProcessError) as error:
                    failure = error
        if failure:
            raise ValueError("unable to verify exact publication image cleanup") from failure
    destination = Path(os.environ["RUNNER_TEMP"]) / "public-image-receipt"
    destination.mkdir(mode=0o700)
    (destination / "image.json").write_text(json.dumps(result, sort_keys=True) + "\n")


def registry_publication(intent):
    """Resolve only the owned native/attestation graph and its exact raw configuration."""
    expected = intent["publication"]
    image, tag = expected["image"].rsplit(":", 1)
    raw = registry_read(image, tag)
    if raw is None:
        return None
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    graph = [digest]
    manifest = json.loads(raw)
    if "manifests" in manifest:
        children = manifest["manifests"]
        native = [item for item in children if item.get("platform") == {"os": "linux", "architecture": "amd64"}]
        if len(native) != 1 or not 1 <= len(children) <= 32:
            raise ValueError("retained publication: index has no unique native runtime image")
        for child in children:
            child_digest = child.get("digest", "")
            if not DIGEST.fullmatch(child_digest) or child_digest in graph:
                raise ValueError("retained publication: index graph has ambiguous manifest identities")
            if child != native[0] and (child.get("platform") != {"os": "unknown", "architecture": "unknown"}
                    or child.get("annotations", {}).get("vnd.docker.reference.type") != "attestation-manifest"
                    or child["annotations"].get("vnd.docker.reference.digest") != native[0]["digest"]):
                raise ValueError("retained publication: attestation does not belong to the owned native image")
            raw = registry_read(image, child_digest)
            if "sha256:" + hashlib.sha256(raw).hexdigest() != child_digest or child.get("size") != len(raw):
                raise ValueError("registry child manifest bytes changed")
            value = json.loads(raw)
            if "manifests" in value or not DIGEST.fullmatch(value.get("config", {}).get("digest", "")):
                raise ValueError("retained publication: nested or malformed child manifest")
            graph.append(child_digest)
            if child == native[0]:
                manifest = value
    config = intent["producer"]["config_digest"]
    if manifest.get("config", {}).get("digest") != config or not DIGEST.fullmatch(config):
        raise ValueError("registry manifest differs from the tested image configuration")
    raw = registry_read(image, config, blob=True)
    if "sha256:" + hashlib.sha256(raw).hexdigest() != config:
        raise ValueError("registry raw configuration bytes changed")
    configuration = json.loads(raw)
    if configuration.get("os") != "linux" or configuration.get("architecture") != "amd64":
        raise ValueError("registry configuration has another runtime platform")
    require_labels(configuration, expected["identity"])
    return graph


def reconcile(expected):
    intent, run, job = authenticated_intent(expected)
    # A successful receipt or uncertain upload prevents deletion. Terminal failed
    # uploads cannot satisfy the private consumer's success-bound receipt contract.
    receipt = uploaded_payload(expected, run, job)
    if receipt is not None:
        if receipt != publication_receipt(expected, intent["producer"], receipt.get("manifest_digest", "")):
            raise ValueError("retained publication: uploaded receipt identity is ambiguous")
        print("Preserved publication with its durable receipt.")
        return False
    image, tag = expected["image"].rsplit(":", 1)
    if package_id(image) != intent["package_id"]:
        raise ValueError("retained publication: package identity changed")
    graph = registry_publication(intent)
    if graph is None:
        # An interrupted index push can commit children before its final tagged root.
        new = [item for item in package_versions(image) if item["id"] not in intent["prior_version_ids"]]
        if new:
            print("Unresolved publication versions: " + json.dumps(
                [{"id": item["id"], "digest": item.get("name")} for item in new], sort_keys=True))
            raise ValueError("retained publication: tag is absent but new package versions require ownership review")
        print("Authenticated registry tag is absent and no new package versions remain.")
        return False
    versions = [item for item in package_versions(image) if item.get("name") in graph
                or tag in item.get("metadata", {}).get("container", {}).get("tags", [])]
    if len(versions) != len(graph) or {item.get("name") for item in versions} != set(graph):
        raise ValueError("retained publication: registry graph and package versions are ambiguous")
    versions = sorted(versions, key=lambda item: graph.index(item["name"]))
    for index, version in enumerate(versions):
        if (version["id"] in intent["prior_version_ids"]
                or version.get("metadata", {}).get("container", {}).get("tags") != ([tag] if index == 0 else [])
                or not (artifacts.timestamp(intent["created_at"]) <= artifacts.timestamp(version["created_at"])
                        <= artifacts.timestamp(version["updated_at"]) <= datetime.now(timezone.utc))):
            raise ValueError("retained publication: graph version is existing, shared, or outside this publication")
    def require_exclusive_graph(remaining):
        current = [item for item in package_versions(image) if item["id"] not in intent["prior_version_ids"]]
        if {item["id"] for item in current} != {item["id"] for item in remaining}:
            # Untagged children can be shared by another newly published index.
            print("Unresolved publication versions: " + json.dumps(
                [{"id": item["id"], "digest": item.get("name")} for item in current], sort_keys=True))
            raise ValueError("retained publication: versions outside the owned graph may share its children")

    # Validate the whole graph before deleting anything. Its run labels prevent ordinary
    # cross-attempt digest reuse; no existing or tagged child version can be removed.
    require_exclusive_graph(versions)
    current_intent, run, job = authenticated_intent(expected)
    if (current_intent != intent or uploaded_payload(expected, run, job) is not None
            or package_id(image) != intent["package_id"]
            or any(package_api(image, f"/versions/{item['id']}") != item for item in versions)
            or registry_publication(intent) != graph):
        raise ValueError("retained publication: final receipt, version, or ownership changed")
    # Keep the complete bounded plan in the run log, including if cancellation interrupts
    # cleanup after the root is gone. No generic registry scan or subsequent retry runs here.
    print("Exact unreceipted publication cleanup graph: " + json.dumps(
        [{"id": item["id"], "digest": item["name"]} for item in versions], sort_keys=True))
    for index, version in enumerate(versions):
        endpoint = f"/versions/{version['id']}"
        if index:
            require_exclusive_graph(versions[index:])
            current_intent, run, job = authenticated_intent(expected)
            if (current_intent != intent or uploaded_payload(expected, run, job) is not None
                    or package_id(image) != intent["package_id"] or package_api(image, endpoint) != version):
                raise ValueError("remaining publication graph retained: ownership changed after root cleanup")
            require_absent_registry_tag(expected["image"])
        try:
            package_api(image, endpoint, "DELETE")  # One attempt per exact version, even on uncertainty.
        except (urllib.error.URLError, TimeoutError, ValueError) as error:
            if isinstance(error, urllib.error.HTTPError):
                error.close()
        try:
            package_api(image, endpoint)
        except urllib.error.HTTPError as error:
            code = error.code
            error.close()
            if code == 404 and package_id(image) == intent["package_id"]:
                print(f"Deleted and verified absent exact unreceipted publication version {version['id']}.")
                continue
            raise ValueError("exact publication version deletion could not be verified") from None
        raise ValueError("exact publication version remains; deletion is not retried")
    return True


def main():
    mode = sys.argv[1]
    path = Path(os.environ["RUNNER_TEMP"]) / "public-image-publication.json"
    if mode == "export":
        export_image(sys.argv[2])
    elif mode == "prepare-engine":
        configure_containerd_store()
    elif mode == "prepare":
        expected = admit()
        with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
            output.write(f"publish={'true' if expected else 'false'}\n")
            if expected:
                output.write(f"artifact_id={expected['artifact']['id']}\n")
        if expected:
            path.write_text(json.dumps(expected, sort_keys=True) + "\n")
        else:
            print("Authenticated source CI event does not publish a DEV image.")
    elif mode == "publish":
        publish(read_json(path), Path(os.environ["RUNNER_TEMP"]) / "public-image-download")
    elif mode == "stage":
        stage(read_json(path), Path(os.environ["RUNNER_TEMP"]) / "public-image-download")
    elif mode == "reconcile":
        if not (Path(os.environ["RUNNER_TEMP"]) / "public-image-intent/intent.json").exists():
            print("No staged publication intent; no registry push was authorized.")
            return
        try:
            reconcile(read_json(path))
        except ValueError as error:
            raise ValueError(f"Publication retained or cleanup unresolved: {error}") from None
        except (KeyError, TypeError, OSError, subprocess.CalledProcessError, zipfile.BadZipFile):
            raise ValueError("Publication retained or cleanup unresolved: exact same-job ownership could not be verified.") from None
    else:
        raise ValueError("expected export, prepare, stage, publish, or reconcile")


if __name__ == "__main__":
    main()
