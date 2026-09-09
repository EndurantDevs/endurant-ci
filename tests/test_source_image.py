"""Image transfer boundaries, plus an explicitly enabled GitHub Docker check."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tarfile
import urllib.error
import urllib.response
import uuid
import zipfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import source_image as transfer


REPOSITORY = "EndurantDevs/drug-api"
IDENTITY = {"repository": REPOSITORY, "source_sha": "a" * 40, "base_sha": "b" * 40,
            "source_branch": "dev", "pr_number": "0", "run_id": "123", "run_attempt": 2,
            "ci_revision": "d" * 40}
LABELS = {"org.endurantdevs.public-ci.repository": REPOSITORY, "org.endurantdevs.public-ci.run": "123-2",
          "org.opencontainers.image.revision": "a" * 40}
CONFIG_BYTES = json.dumps({"os": "linux", "architecture": "amd64", "config": {"Labels": LABELS},
                           "rootfs": {"type": "layers", "diff_ids": []}}).encode()
CONFIG = "sha256:" + hashlib.sha256(CONFIG_BYTES).hexdigest()
NOW = datetime.now(timezone.utc).replace(microsecond=0)


def artifact(identifier, name, days):
    return {"id": identifier, "name": name, "digest": "sha256:" + "e" * 64,
            "size_in_bytes": 100, "expired": False, "created_at": NOW.isoformat(),
            "expires_at": (NOW + timedelta(days=days)).isoformat()}


def expected():
    return {"identity": {**IDENTITY, "caller_workflow_blob_sha": "f" * 40},
            "artifact": transfer.snapshot(artifact(44, "drug-public-image-staging-123-2", 1)),
            "measurement": transfer.snapshot(artifact(55, "drug-public-measurement-123-2", 90)),
            "image": transfer.IMAGES[REPOSITORY] + ":dev-main-aaaaaaaa-20260909120000",
            "workflow_id": 456, "producer_job_id": 1, "publisher_job_id": 4}


def docker_archive(*, entries=None, extra=None):
    config_name = CONFIG[7:] + ".json"
    entries = entries if entries is not None else [{"Config": config_name, "RepoTags": None, "Layers": []}]
    files = {config_name: CONFIG_BYTES, "manifest.json": json.dumps(entries).encode(), **(extra or {})}
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, data in files.items():
            item = tarfile.TarInfo(name)
            item.size = len(data)
            archive.addfile(item, io.BytesIO(data))
    return output.getvalue()


class AdmissionChecks(unittest.TestCase):
    def fixtures(self):
        run = {"id": 123, "run_attempt": 2, "event": "push", "head_sha": "a" * 40,
               "head_branch": "dev", "workflow_id": 456}
        jobs = [{"id": index + 1, "name": name, "run_id": 123, "run_attempt": 2,
                 "head_sha": "a" * 40, "status": "in_progress" if name == transfer.JOB else "completed",
                 "conclusion": None if name == transfer.JOB else "success", "started_at": "2026-09-09T12:00:00Z"}
                for index, name in enumerate(("Tests and build", "Coverage results", "portable import checks", transfer.JOB))]
        jobs[0]["steps"] = [{"name": transfer.UPLOAD, "status": "completed", "conclusion": "success",
                             "started_at": (NOW - timedelta(seconds=5)).isoformat(),
                             "completed_at": (NOW + timedelta(seconds=5)).isoformat()}]
        items = [artifact(44, "drug-public-image-staging-123-2", 1), artifact(55, "drug-public-measurement-123-2", 90)]
        return run, jobs, items

    def admit(self, run, jobs, items, head="a" * 40):
        def api(repository, path):
            self.assertEqual(repository, REPOSITORY)
            if path == "commits/dev":
                return {"sha": head}
            if path.startswith("contents/.github/workflows/ci.yml?ref="):
                return {"sha": "f" * 40}
            raise AssertionError(path)

        def pages(repository, path, key):
            self.assertEqual(repository, REPOSITORY)
            return deepcopy(jobs if key == "jobs" else items)

        with patch.dict(os.environ, {"GITHUB_JOB": "dev-image-publication"}), \
                patch.object(transfer, "context", return_value=(IDENTITY, run)), \
                patch.object(transfer.github, "api", side_effect=api), patch.object(transfer.github, "pages", side_effect=pages):
            return transfer.admit()

    def test_exact_successful_producer_and_final_are_required(self):
        run, jobs, items = self.fixtures()
        self.assertEqual(self.admit(run, jobs, items), expected())
        for changes in ({"status": "in_progress", "conclusion": None}, {"conclusion": "failure"},
                        {"conclusion": "cancelled"}, {"conclusion": "skipped"}, {"run_attempt": 1}, {"head_sha": "b" * 40}):
            changed = deepcopy(jobs)
            changed[0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.admit(run, changed, items)
        for changed in (items[:1], items[1:], items + [items[0]], [{**items[0], "expired": True}, items[1]],
                        [{**items[0], "created_at": (NOW - timedelta(minutes=1)).isoformat()}, items[1]],
                        [items[0], artifact(55, items[1]["name"], 1)]):
            with self.subTest(items=changed), self.assertRaises(ValueError):
                self.admit(run, jobs, changed)
        with self.assertRaisesRegex(ValueError, "advanced"):
            self.admit(run, jobs, items, head="b" * 40)

    def test_non_dev_publication_is_authenticated_noop(self):
        run, jobs, items = self.fixtures()
        self.assertIsNone(self.admit({**run, "event": "pull_request"}, jobs, items))
        with patch.dict(IDENTITY, {"source_branch": "main"}):
            self.assertIsNone(self.admit({**run, "head_branch": "main"}, jobs, items))
        changed = deepcopy(jobs)
        changed[-1]["run_attempt"] = 1
        with self.assertRaises(ValueError):
            self.admit({**run, "event": "pull_request"}, changed, items)

    def test_current_run_and_checkout_cannot_be_substituted_in_context(self):
        with tempfile.TemporaryDirectory() as temporary:
            event = Path(temporary) / "event.json"
            event.write_text(json.dumps({"ref": "refs/heads/dev", "after": "a" * 40}))
            environment = {"GITHUB_REPOSITORY": REPOSITORY, "CI_REVISION": "d" * 40, "GITHUB_EVENT_NAME": "push",
                           "GITHUB_EVENT_PATH": str(event), "GITHUB_SHA": "a" * 40, "GITHUB_REF": "refs/heads/dev",
                           "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "2"}
            run = {"id": 123, "run_attempt": 2, "workflow_id": 456, "event": "push", "head_sha": "a" * 40,
                   "head_branch": "dev", "path": ".github/workflows/ci.yml", "status": "in_progress", "conclusion": None,
                   "repository": {"full_name": REPOSITORY}, "head_repository": {"full_name": REPOSITORY}}
            with patch.dict(os.environ, environment), patch.object(transfer, "command", return_value=b"d" * 40), \
                    patch.object(transfer.github, "resolve_identity", return_value=IDENTITY), \
                    patch.object(transfer.github, "api", return_value=run) as api:
                self.assertEqual(transfer.context()[0], IDENTITY)
                for change in ({"run_attempt": 3}, {"status": "completed"}, {"head_sha": "b" * 40},
                               {"head_branch": "main"}, {"path": ".github/workflows/other.yml"},
                               {"head_repository": {"full_name": "Other/drug-api"}}):
                    api.return_value = {**run, **change}
                    with self.subTest(change=change), self.assertRaises(ValueError):
                        transfer.context()


class TransferChecks(unittest.TestCase):
    def setup_artifact(self, directory):
        (directory / "image.tar.gz").write_bytes(docker_archive())
        record = {"schema": "public-source-image-staging-v1", **expected()["identity"], "platform": "linux/amd64",
                  "image_id": CONFIG, "config_digest": CONFIG,
                  "archive_sha256": transfer.file_digest(directory / "image.tar.gz")}
        (directory / "producer.json").write_text(json.dumps(record))
        return record

    def exercise(self, directory, record, *, loaded_config=CONFIG, failure=None, stale=False, cleanup_failure=False):
        calls, images = [], set()
        raw = json.dumps({"schemaVersion": 2, "config": {"digest": CONFIG}}).encode()
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()

        def command(*args, **kwargs):
            calls.append(args)
            if args == ("docker", "image", "ls", "--all", "--quiet", "--no-trunc"):
                return "\n".join(images).encode()
            if args[:4] == ("docker", "image", "ls", "--quiet"):
                return b"present" if args[4] in images else b""
            if args[:3] == ("docker", "image", "load"):
                images.add(record["image_id"])
                return ("Loaded image ID: " + record["image_id"] + "\n").encode()
            if args[:3] == ("docker", "image", "inspect"):
                return json.dumps([{"Id": loaded_config, "Os": "linux", "Architecture": "amd64"}]).encode()
            if args[:3] == ("docker", "image", "tag"):
                images.add(args[4])
                return b""
            if args[:3] == ("docker", "image", "rm"):
                if cleanup_failure and args[3] == expected()["image"]:
                    raise subprocess.CalledProcessError(1, args)
                images.discard(args[3])
                return b""
            if args[:2] == ("docker", "login"):
                self.assertEqual(kwargs["input"], b"synthetic token")
                self.assertTrue(Path(kwargs["env"]["DOCKER_CONFIG"]).is_dir())
                return b""
            if args[:3] == ("docker", "image", "push"):
                if failure:
                    raise subprocess.CalledProcessError(1, args)
                return b""
            if args[:4] == ("docker", "buildx", "imagetools", "inspect"):
                self.assertIn("DOCKER_CONFIG", kwargs["env"])
                if args[-1] == "--raw":
                    return raw  # Buildx prints p.raw with %s and deliberately adds no newline.
                self.assertEqual(args[-1], '{{printf "%s" .Manifest.Digest}}')
                return digest.encode()
            raise AssertionError(args)

        values = [expected(), {**expected(), "workflow_id": 999}] if stale else None
        environment = {"RUNNER_TEMP": str(directory.parent), "GH_TOKEN": "synthetic token", "GITHUB_ACTOR": "synthetic"}
        with patch.dict(os.environ, environment), patch.object(transfer, "command", side_effect=command), \
                patch.object(transfer, "admit", return_value=expected(), side_effect=values), \
                patch.object(transfer, "authenticated_intent", return_value=({"producer": record}, {}, {})), \
                patch.object(transfer, "require_absent_registry_tag"):

            try:
                transfer.publish(expected(), directory)
            finally:
                self.assertEqual(images, {expected()["image"]} if cleanup_failure else set(),
                                 "cleanup must attempt every owned image even after one removal fails")
                self.assertFalse(list(directory.parent.glob("public-image-auth-*")), "credentials must be removed")
                self.assertFalse(any(command[1] in {"run", "build", "exec"} for command in calls))
                if loaded_config != CONFIG or stale:
                    self.assertFalse(any(command[:2] == ("docker", "login") for command in calls))
        return calls, digest

    def test_publish_preserves_tested_config_and_receipt_has_immutable_registry_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "input"
            directory.mkdir()
            record = self.setup_artifact(directory)
            calls, digest = self.exercise(directory, record)
            receipt = json.loads((directory.parent / "public-image-receipt/image.json").read_text())
            self.assertEqual(receipt, {"schema": "public-source-image-v1", **expected()["identity"],
                "producer_artifact": {key: expected()["artifact"][key] for key in ("id", "name", "digest")},
                "archive_sha256": record["archive_sha256"], "platform": "linux/amd64", "config_digest": CONFIG,
                "image": expected()["image"], "manifest_digest": digest})
            self.assertEqual(sum(command[:3] == ("docker", "image", "push") for command in calls), 1)

    def test_changed_archive_or_identity_never_reaches_docker(self):
        for change in ({"run_attempt": 1}, {"source_sha": "b" * 40}, {"ci_revision": "e" * 40},
                       {"caller_workflow_blob_sha": "e" * 40}, {"archive_sha256": "0" * 64},
                       {"platform": "linux/arm64"}, {"image_id": "sha256:" + "f" * 64}):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                record = self.setup_artifact(directory)
                (directory / "producer.json").write_text(json.dumps({**record, **change}))
                with patch.object(transfer, "admit", return_value=expected()), patch.object(transfer, "command") as docker:
                    with self.assertRaises(ValueError):
                        transfer.publish(expected(), directory)
                    docker.assert_not_called()

    def test_wrong_loaded_config_stale_authority_and_push_failure_cleanup_and_fail(self):
        for options in ({"loaded_config": "sha256:" + "b" * 64}, {"stale": True}, {"failure": True}, {"cleanup_failure": True}):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary) / "input"
                directory.mkdir()
                record = self.setup_artifact(directory)
                with self.assertRaises((ValueError, subprocess.CalledProcessError)):
                    self.exercise(directory, record, **options)
                self.assertFalse((directory.parent / "public-image-receipt/image.json").exists())

    def test_preexisting_image_is_preserved_and_never_loaded_or_pushed(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            record = self.setup_artifact(directory)
            with patch.object(transfer, "admit", return_value=expected()), \
                    patch.object(transfer, "authenticated_intent", return_value=({"producer": record}, {}, {})), \
                    patch.object(transfer, "command", return_value=(CONFIG + "\n").encode()) as docker:
                with self.assertRaisesRegex(ValueError, "pre-existing"):
                    transfer.publish(expected(), directory)
                self.assertEqual(docker.call_args_list, [(("docker", "image", "ls", "--all", "--quiet", "--no-trunc"),)])

    def test_export_uses_existing_tested_image_without_a_second_build(self):
        with tempfile.TemporaryDirectory() as temporary:
            def command(*args):
                return ("f" * 40 if args[-1].startswith("HEAD:") else "a" * 40).encode()
            with patch.dict(os.environ, {"RUNNER_TEMP": temporary, "SOURCE_ROOT": temporary}), \
                    patch.object(transfer, "context", return_value=(IDENTITY, {"event": "push"})), \
                    patch.object(transfer, "command", side_effect=command), \
                    patch.object(transfer, "image_identity", return_value={"Id": CONFIG, "Config": {"Labels": LABELS}}), \
                    patch.object(transfer.subprocess, "Popen") as save:
                process = save.return_value.__enter__.return_value
                process.stdout = io.BytesIO(gzip.decompress(docker_archive()))
                process.wait.return_value = 0
                transfer.export_image(CONFIG)
                save.assert_called_once_with(["docker", "image", "save", CONFIG], stdout=subprocess.PIPE)
            directory = Path(temporary) / "drug-public-image-staging-123-2"
            self.assertEqual(transfer.archive_identity(directory / "image.tar.gz"), (CONFIG, {CONFIG}))
            self.assertEqual(json.loads((directory / "producer.json").read_text())["config_digest"], CONFIG)

    def test_retagged_source_cannot_change_exported_test_identity(self):
        with patch.object(transfer, "context") as context:
            with self.assertRaisesRegex(ValueError, "immutable image ID"):
                transfer.export_image("drug-ci-runtime:" + "a" * 32)
            context.assert_not_called()
        root = Path(__file__).resolve().parents[1]
        for kind, start, end, variable in (("healthcare", "run_container_package() {", "run_security() {", "tested_image"),
                                          ("drug", "runtime_image() {", "local_services() {", "runtime_image_id")):
            script = (root / "scripts" / kind / "check").read_text().split(start, 1)[1].split(end, 1)[0]
            self.assertLess(script.index("{{.Id}}"), script.index("docker run"))
            self.assertIn(f'export "${variable}"', script)
            for invocation in script.split("docker run")[1:]:
                self.assertIn(f'"${variable}"', invocation)


class ArchiveChecks(unittest.TestCase):
    def inspect(self, data):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "image.tar.gz"
            path.write_bytes(data)
            return transfer.archive_identity(path)

    def test_exact_single_image_accepts_classic_and_oci_ids(self):
        self.assertEqual(self.inspect(docker_archive()), (CONFIG, {CONFIG}))
        raw = json.dumps({"schemaVersion": 2, "config": {"digest": CONFIG}, "layers": []}).encode()
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        index = {"schemaVersion": 2, "manifests": [{"digest": digest, "size": len(raw)}]}
        extra = {"oci-layout": b'{"imageLayoutVersion":"1.0.0"}', "index.json": json.dumps(index).encode(),
                 "blobs/sha256/" + digest[7:]: raw, "blobs/sha256/" + CONFIG[7:]: CONFIG_BYTES}
        self.assertEqual(self.inspect(docker_archive(extra=extra)), (CONFIG, {CONFIG, digest}))
        for roots in ([*index["manifests"], *index["manifests"]],
                      [{**index["manifests"][0], "annotations": {"io.containerd.image.name": "unrelated:latest"}}]):
            with self.subTest(roots=roots), self.assertRaises(ValueError):
                self.inspect(docker_archive(extra={**extra, "index.json": json.dumps({"manifests": roots}).encode()}))

    def test_extra_images_tags_unsafe_members_and_wrong_configuration_are_rejected(self):
        entry = {"Config": CONFIG[7:] + ".json", "RepoTags": None, "Layers": []}
        for entries in ([entry, entry], [{**entry, "RepoTags": ["unrelated:latest"]}],
                        [{**entry, "repotags": ["unrelated:latest"]}], [{**entry, "Config": "../config.json"}]):
            with self.subTest(entries=entries), self.assertRaises(ValueError):
                self.inspect(docker_archive(entries=entries))
        for extra in ({"../escape": b"data"}, {"/absolute": b"data"}, {"manifest.json": b" " * 65537},
                      {CONFIG[7:] + ".json": b'{"os":"linux","architecture":"arm64"}'}):
            with self.subTest(extra=list(extra)), self.assertRaises(ValueError):
                self.inspect(docker_archive(extra=extra))

    def test_buildx_raw_bytes_are_not_trimmed(self):
        for raw in (b'{"config":{"digest":"' + CONFIG.encode() + b'"}}',
                    b'{"config":{"digest":"' + CONFIG.encode() + b'"}}\n'):
            digest = "sha256:" + hashlib.sha256(raw).hexdigest()
            with patch.object(transfer, "command", return_value=raw):
                self.assertEqual(transfer.registry_config(transfer.IMAGES[REPOSITORY], digest, {}), CONFIG)
                with self.assertRaises(ValueError):
                    transfer.registry_config(transfer.IMAGES[REPOSITORY], "sha256:" + "0" * 64, {})


class RegistryChecks(unittest.TestCase):
    def check(self, response):
        with patch.dict(os.environ, {"GH_TOKEN": "synthetic token", "GITHUB_ACTOR": "synthetic"}), \
                patch.object(transfer.urllib.request, "build_opener") as factory:
            opener = factory.return_value
            authentication = unittest.mock.MagicMock()
            authentication.__enter__.return_value.status = 200
            authentication.__enter__.return_value.read.return_value = b'{"token":"synthetic-read-token"}'
            opener.open.side_effect = [authentication, response]
            transfer.require_absent_registry_tag(expected()["image"])
            self.assertEqual(opener.open.call_count, 2)
            self.assertIn("Authorization", opener.open.call_args_list[0].args[0].headers)
            self.assertEqual(opener.open.call_args_list[1].args[0].headers["Authorization"], "Bearer synthetic-read-token")

    def test_only_authenticated_manifest_unknown_means_absent(self):
        for status, code in ((404, "MANIFEST_UNKNOWN"), (404, "NAME_UNKNOWN"), (401, "UNAUTHORIZED"),
                             (403, "DENIED"), (429, "TOOMANYREQUESTS"), (500, "UNKNOWN")):
            error = urllib.error.HTTPError("https://ghcr.io/synthetic", status, "synthetic", {},
                                          io.BytesIO(json.dumps({"errors": [{"code": code}]}).encode()))
            with self.subTest(status=status, code=code):
                if status == 404 and code == "MANIFEST_UNKNOWN":
                    self.check(error)
                else:
                    with self.assertRaises(ValueError):
                        self.check(error)
        present = unittest.mock.MagicMock()
        present.__enter__.return_value.status = 200
        present.__enter__.return_value.read.return_value = b"{}"
        with self.assertRaisesRegex(ValueError, "overwrite"):
            self.check(present)
        with self.assertRaises(TimeoutError):
            self.check(TimeoutError("synthetic timeout"))

    def test_raw_registry_config_binds_exact_run_labels_and_manifest(self):
        intent = {"publication": expected(), "producer": {"config_digest": CONFIG}}
        manifest = json.dumps({"config": {"digest": CONFIG}}).encode()
        with patch.object(transfer, "registry_read", side_effect=[manifest, CONFIG_BYTES]) as read:
            self.assertEqual(transfer.registry_publication(intent), ["sha256:" + hashlib.sha256(manifest).hexdigest()])
            self.assertEqual(read.call_args_list[-1].kwargs, {"blob": True})
        for labels in ({}, {**LABELS, "org.endurantdevs.public-ci.run": "123-1"},
                       {**LABELS, "org.endurantdevs.public-ci.repository": "EndurantDevs/healthcare-mrf-api"}):
            raw = json.dumps({**json.loads(CONFIG_BYTES), "config": {"Labels": labels}}).encode()
            digest = "sha256:" + hashlib.sha256(raw).hexdigest()
            with self.subTest(labels=labels), patch.object(transfer, "registry_read", side_effect=[
                    json.dumps({"config": {"digest": digest}}).encode(), raw]):
                with self.assertRaisesRegex(ValueError, "labels"):
                    transfer.registry_publication({**intent, "producer": {"config_digest": digest}})
        with patch.object(transfer, "registry_read", side_effect=[manifest, CONFIG_BYTES + b"\n"]):
            with self.assertRaisesRegex(ValueError, "bytes changed"):
                transfer.registry_publication(intent)

    def test_signed_blob_redirect_does_not_forward_registry_credentials(self):
        def response(raw):
            value = unittest.mock.MagicMock()
            value.__enter__.return_value.status = 200
            value.__enter__.return_value.read.return_value = raw
            return value
        for host in ("pkg-containers.githubusercontent.com", "unrelated.example"):
            redirect = urllib.error.HTTPError("https://ghcr.io/synthetic", 307, "synthetic",
                {"Location": f"https://{host}/synthetic?signature=synthetic"}, io.BytesIO())
            with self.subTest(host=host), patch.dict(os.environ, {"GITHUB_ACTOR": "synthetic", "GH_TOKEN": "synthetic"}), \
                    patch.object(transfer.urllib.request, "build_opener") as factory:
                opener = factory.return_value
                opener.open.side_effect = [response(b'{"token":"synthetic"}'), redirect, response(CONFIG_BYTES)]
                if host == "unrelated.example":
                    with self.assertRaises(ValueError):
                        transfer.registry_read(transfer.IMAGES[REPOSITORY], CONFIG, blob=True)
                    self.assertEqual(opener.open.call_count, 2)
                else:
                    self.assertEqual(transfer.registry_read(transfer.IMAGES[REPOSITORY], CONFIG, blob=True), CONFIG_BYTES)
                    self.assertNotIn("Authorization", opener.open.call_args.args[0].headers)

    def test_real_urllib_handler_exposes_redirect_without_following_it(self):
        requests = []

        class SyntheticHTTP(transfer.urllib.request.HTTPHandler):
            def http_open(self, request):
                requests.append(request)
                response = urllib.response.addinfourl(io.BytesIO(),
                    {"Location": "https://pkg-containers.githubusercontent.com/synthetic"}, request.full_url, 307)
                response.msg = "Temporary Redirect"
                return response

        opener = transfer.urllib.request.build_opener(transfer.urllib.request.ProxyHandler({}),
                                                      SyntheticHTTP(), transfer.NoRegistryRedirect())
        request = transfer.urllib.request.Request("http://synthetic.invalid/config", headers={"Authorization": "Bearer synthetic"})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            opener.open(request)
        self.assertEqual(caught.exception.code, 307)
        self.assertEqual(caught.exception.headers["Location"], "https://pkg-containers.githubusercontent.com/synthetic")
        caught.exception.close()
        self.assertEqual(len(requests), 1)

    def test_native_and_attestation_index_has_one_exact_bound_graph(self):
        native = json.dumps({"config": {"digest": CONFIG}, "layers": []}).encode()
        native_digest = "sha256:" + hashlib.sha256(native).hexdigest()
        attestation = json.dumps({"config": {"digest": "sha256:" + "7" * 64}, "layers": []}).encode()
        attestation_digest = "sha256:" + hashlib.sha256(attestation).hexdigest()
        children = [{"digest": native_digest, "size": len(native), "platform": {"os": "linux", "architecture": "amd64"}},
            {"digest": attestation_digest, "size": len(attestation), "platform": {"os": "unknown", "architecture": "unknown"},
             "annotations": {"vnd.docker.reference.type": "attestation-manifest", "vnd.docker.reference.digest": native_digest}}]
        root = json.dumps({"manifests": children}).encode()
        intent = {"publication": expected(), "producer": {"config_digest": CONFIG}}
        with patch.object(transfer, "registry_read", side_effect=[root, native, attestation, CONFIG_BYTES]):
            self.assertEqual(transfer.registry_publication(intent), ["sha256:" + hashlib.sha256(root).hexdigest(),
                                                                    native_digest, attestation_digest])
        children[1]["annotations"]["vnd.docker.reference.digest"] = "sha256:" + "8" * 64
        with patch.object(transfer, "registry_read", side_effect=[json.dumps({"manifests": children}).encode(), native]):
            with self.assertRaisesRegex(ValueError, "attestation"):
                transfer.registry_publication(intent)


class IntentChecks(unittest.TestCase):
    def test_stage_records_complete_preexisting_inventory_and_refuses_reuse(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "input"
            directory.mkdir()
            record = TransferChecks().setup_artifact(directory)
            with patch.dict(os.environ, {"RUNNER_TEMP": temporary}), patch.object(transfer, "admit", return_value=expected()), \
                    patch.object(transfer, "package_id", return_value=321), \
                    patch.object(transfer, "package_versions", return_value=[{"id": 11}, {"id": 9}]), \
                    patch.object(transfer, "require_absent_registry_tag") as absent:
                transfer.stage(expected(), directory)
                intent = transfer.read_json(Path(temporary) / "public-image-intent/intent.json")
                self.assertEqual(intent["publication"], expected())
                self.assertEqual(intent["producer"], record)
                self.assertEqual(intent["prior_version_ids"], [9, 11])
                self.assertEqual(intent["package_id"], 321)
                absent.assert_called_once_with(expected()["image"])
                with self.assertRaises(FileExistsError):
                    transfer.stage(expected(), directory)

    def test_missing_or_wrong_run_labels_prevent_stage_and_export(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            record = TransferChecks().setup_artifact(directory)
            raw = json.dumps({**json.loads(CONFIG_BYTES), "config": {"Labels": {}}}).encode()
            digest = "sha256:" + hashlib.sha256(raw).hexdigest()
            (directory / "image.tar.gz").write_bytes(docker_archive(
                entries=[{"Config": digest[7:] + ".json", "Layers": [], "RepoTags": None}],
                extra={digest[7:] + ".json": raw}))
            record.update(image_id=digest, config_digest=digest, archive_sha256=transfer.file_digest(directory / "image.tar.gz"))
            (directory / "producer.json").write_text(json.dumps(record))
            with patch.object(transfer, "admit", return_value=expected()), patch.object(transfer, "package_api") as api:
                with self.assertRaisesRegex(ValueError, "labels"):
                    transfer.stage(expected(), directory)
                api.assert_not_called()

    def test_complete_package_inventory_does_not_stop_at_first_full_page(self):
        with patch.object(transfer, "package_api", side_effect=[[{"id": i + 1} for i in range(100)], [{"id": 101}]]) as api:
            self.assertEqual(len(transfer.package_versions(transfer.IMAGES[REPOSITORY])), 101)
            self.assertIn("page=2", api.call_args.args[1])
        for values in ([{"id": 1}, {"id": 1}], [{"id": True}]):
            with self.subTest(values=values), patch.object(transfer, "package_api", return_value=values):
                with self.assertRaises(ValueError):
                    transfer.package_versions(transfer.IMAGES[REPOSITORY])

    def test_stage_rejects_oversized_inventory_without_truncating_durable_intent(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "input"
            directory.mkdir()
            TransferChecks().setup_artifact(directory)
            with patch.dict(os.environ, {"RUNNER_TEMP": temporary}), patch.object(transfer, "admit", return_value=expected()), \
                    patch.object(transfer, "package_id", return_value=321), \
                    patch.object(transfer, "package_versions", return_value=[{"id": 10**15 + i} for i in range(5000)]), \
                    patch.object(transfer, "require_absent_registry_tag"):
                with self.assertRaisesRegex(ValueError, "metadata bound"):
                    transfer.stage(expected(), directory)
                self.assertFalse((Path(temporary) / "public-image-intent").exists())

    def test_publisher_can_reconcile_after_another_step_fails_without_current_dev_admission(self):
        run, jobs, _ = AdmissionChecks().fixtures()
        jobs[0]["conclusion"] = "failure"
        with patch.dict(os.environ, {"GITHUB_JOB": "dev-image-publication"}), \
                patch.object(transfer, "context", return_value=(IDENTITY, run)), \
                patch.object(transfer.github, "pages", return_value=jobs), patch.object(transfer.github, "api") as api:
            self.assertEqual(transfer.active_publisher(expected()), (run, jobs[-1]))
            api.assert_not_called()  # No commits/dev or all-success gate during cleanup.
            for change in ({"status": "completed", "conclusion": "success"}, {"id": 999}, {"run_attempt": 1}):
                with self.subTest(change=change), patch.dict(jobs[-1], change), self.assertRaises(ValueError):
                    transfer.active_publisher(expected())

    def upload(self, payload, *, conclusion="success", status="completed", corrupt=False, intent=True, missing=False):
        run, jobs, _ = AdmissionChecks().fixtures()
        step = {"name": transfer.INTENT_UPLOAD if intent else transfer.RECEIPT_UPLOAD, "status": status,
                "conclusion": conclusion, "started_at": (NOW - timedelta(seconds=5)).isoformat(),
                "completed_at": (NOW + timedelta(seconds=5)).isoformat()}
        job = {**jobs[-1], "steps": [step]}
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("intent.json" if intent else "image.json", json.dumps(payload))
        raw = output.getvalue()
        item = {**artifact(88, transfer.proof_name(IDENTITY, intent), 90), "size_in_bytes": len(raw),
                "digest": "sha256:" + hashlib.sha256(raw).hexdigest()}
        with patch.object(transfer.github, "pages", return_value=[] if missing else [item]), \
                patch.object(transfer.github, "api", return_value=item), \
                patch.object(transfer, "command", return_value=raw + b"x" if corrupt else raw):
            return transfer.uploaded_payload(expected(), run, job, intent=intent)

    def test_durable_intent_requires_successful_upload_and_exact_zip_bytes(self):
        self.assertEqual(self.upload({"synthetic": 1}), {"synthetic": 1})
        for options in ({"conclusion": "failure"}, {"conclusion": "skipped"}, {"corrupt": True}, {"missing": True}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.upload({"synthetic": 1}, **options)

    def test_local_intent_must_match_uploaded_record_and_active_publication_window(self):
        baseline, _, _ = ReconciliationChecks().fixtures()
        job = {"started_at": (NOW - timedelta(minutes=1)).isoformat()}
        for changes, remote_change in (({}, False), ({}, True),
                ({"expires_at": (NOW - timedelta(seconds=1)).isoformat()}, False),
                ({"prior_version_ids": [2, 1]}, False), ({"prior_version_ids": [True]}, False),
                ({"package_id": 0}, False),
                ({"publication": {**expected(), "publisher_job_id": 99}}, False)):
            with self.subTest(changes=changes, remote_change=remote_change), tempfile.TemporaryDirectory() as temporary:
                intent = {**baseline, **changes}
                directory = Path(temporary) / "public-image-intent"
                directory.mkdir()
                (directory / "intent.json").write_text(json.dumps(intent))
                with patch.dict(os.environ, {"RUNNER_TEMP": temporary}), \
                        patch.object(transfer, "active_publisher", return_value=({}, job)), \
                        patch.object(transfer, "uploaded_payload", return_value={} if remote_change else intent):
                    if changes or remote_change:
                        with self.assertRaises(ValueError):
                            transfer.authenticated_intent(expected())
                    else:
                        self.assertEqual(transfer.authenticated_intent(expected())[0], intent)

    def test_only_terminal_failed_or_skipped_upload_can_lack_a_success_bound_receipt(self):
        self.assertIsNone(self.upload({}, intent=False, conclusion="skipped", missing=True))
        for conclusion in ("failure", "cancelled"):
            for missing in (False, True):
                with self.subTest(conclusion=conclusion, missing=missing):
                    # Lost upload responses may leave backend artifacts. These cannot
                    # satisfy the consumer's successful-step/whole-attempt requirement.
                    self.assertIsNone(self.upload({}, intent=False, conclusion=conclusion, missing=missing))
        for conclusion in ("success", None):
            with self.subTest(conclusion=conclusion), self.assertRaises(ValueError):
                self.upload({}, intent=False, conclusion=conclusion, missing=True)
        with self.assertRaises(ValueError):
            self.upload({}, intent=False, status="in_progress", conclusion=None, missing=True)

    def test_publish_requires_uploaded_intent_before_any_local_image_or_registry_operation(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            TransferChecks().setup_artifact(directory)
            with patch.object(transfer, "admit", return_value=expected()), \
                    patch.object(transfer, "authenticated_intent", side_effect=ValueError("intent upload failed")), \
                    patch.object(transfer, "command") as command:
                with self.assertRaisesRegex(ValueError, "intent upload"):
                    transfer.publish(expected(), directory)
                command.assert_not_called()


class ReconciliationChecks(unittest.TestCase):
    def fixtures(self):
        intent = {"schema": "public-source-image-intent-v1", "publication": expected(),
                  "producer": {"config_digest": CONFIG, "archive_sha256": "c" * 64},
                  "package_id": 321, "prior_version_ids": [1, 2],
                  "created_at": (NOW - timedelta(seconds=10)).isoformat(),
                  "expires_at": (NOW + timedelta(minutes=29)).isoformat()}
        digest = "sha256:" + "9" * 64
        version = {"id": 3, "name": digest, "created_at": (NOW - timedelta(seconds=5)).isoformat(),
                   "updated_at": (NOW - timedelta(seconds=5)).isoformat(),
                   "metadata": {"container": {"tags": [expected()["image"].rsplit(":", 1)[1]]}}}
        return intent, digest, version

    def exercise(self, *, version_change=None, prior=False, receipts=None, delete_error=None,
                 absent=False, changed_final=False, deletion_remains=False, active=True,
                 indexed=False, existing_child=False, tagged_child=False):
        intent, digest, version = self.fixtures()
        if version_change:
            version.update(version_change)
        if prior:
            intent["prior_version_ids"].append(3)
        versions = [version]
        if indexed:
            versions.extend({**deepcopy(version), "id": number, "name": "sha256:" + str(number) * 64,
                             "metadata": {"container": {"tags": []}}} for number in (4, 5))
        if existing_child:
            intent["prior_version_ids"].append(4)
        if tagged_child:
            versions[-1]["metadata"]["container"]["tags"] = ["shared-live"]
        calls, deleted = [], set()

        def api(image, path, method="GET"):
            calls.append((image, path, method))
            self.assertEqual(image, transfer.IMAGES[REPOSITORY])
            self.assertTrue(path.startswith("/versions/"))
            identifier = int(path.rsplit("/", 1)[1])
            current = next(item for item in versions if item["id"] == identifier)
            if method == "DELETE":
                deleted.add(identifier)
                if delete_error:
                    raise delete_error
                return None
            if identifier in deleted and not deletion_remains:
                raise urllib.error.HTTPError("https://api.github.com/synthetic", 404, "missing", {}, io.BytesIO())
            return {**current, "updated_at": NOW.isoformat()} if changed_final else current

        with patch.object(transfer, "authenticated_intent", return_value=(intent, {}, {}),
                          side_effect=None if active else ValueError("publisher completed")), \
                patch.object(transfer, "uploaded_payload", return_value=None, side_effect=receipts), \
                patch.object(transfer, "package_id", return_value=321), \
                patch.object(transfer, "registry_publication", return_value=None if absent else [digest, *[item["name"] for item in versions[1:]]]), \
                patch.object(transfer, "package_versions", return_value=versions), \
                patch.object(transfer, "require_absent_registry_tag"), \
                patch.object(transfer, "package_api", side_effect=api), patch.object(transfer, "command") as docker, \
                patch.object(transfer, "admit", side_effect=AssertionError("cleanup must not re-admit")):
            try:
                return transfer.reconcile(expected()), calls
            finally:
                docker.assert_not_called()
                for item in versions:
                    self.assertLessEqual(sum(call[1:] == (f"/versions/{item['id']}", "DELETE") for call in calls), 1)
                self.calls = calls

    def test_failed_publication_deletes_only_new_exact_version_and_verifies_404(self):
        result, calls = self.exercise()
        self.assertTrue(result)
        self.assertEqual([method for _, _, method in calls], ["GET", "DELETE", "GET"])

    def test_terminal_failed_upload_with_lost_response_cannot_protect_an_orphan(self):
        for conclusion in ("failure", "cancelled"):
            for backend_missing in (True, False):
                with self.subTest(conclusion=conclusion, backend_missing=backend_missing):
                    receipt = IntentChecks().upload({}, intent=False, conclusion=conclusion, missing=backend_missing)
                    result, calls = self.exercise(receipts=[receipt, receipt])
                    self.assertTrue(result)
                    self.assertEqual(sum(method == "DELETE" for _, _, method in calls), 1)

    def test_index_cleanup_verifies_and_removes_only_its_new_untagged_children(self):
        result, calls = self.exercise(indexed=True)
        self.assertTrue(result)
        self.assertEqual([path for _, path, method in calls if method == "DELETE"], ["/versions/3", "/versions/4", "/versions/5"])
        for options in ({"existing_child": True}, {"tagged_child": True}):
            with self.subTest(options=options), self.assertRaisesRegex(ValueError, "existing, shared"):
                self.exercise(indexed=True, **options)
            self.assertFalse(any(method == "DELETE" for _, _, method in self.calls))

    def test_lost_delete_response_is_reconciled_without_retry(self):
        result, calls = self.exercise(delete_error=TimeoutError("synthetic lost response"))
        self.assertTrue(result)
        self.assertEqual(sum(method == "DELETE" for _, _, method in calls), 1)
        with self.assertRaisesRegex(ValueError, "not retried"):
            self.exercise(delete_error=TimeoutError("synthetic lost response"), deletion_remains=True)
        self.assertEqual(sum(method == "DELETE" for _, _, method in self.calls), 1)

    def test_existing_shared_old_or_changed_versions_never_reach_delete(self):
        for options in ({"prior": True}, {"version_change": {"metadata": {"container": {"tags": [
                expected()["image"].rsplit(":", 1)[1], "live"]}}}},
                {"version_change": {"created_at": (NOW - timedelta(days=1)).isoformat()}},
                {"version_change": {"name": "sha256:" + "8" * 64}}, {"changed_final": True}, {"active": False}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.exercise(**options)
            self.assertFalse(any(method == "DELETE" for _, _, method in self.calls))

    def test_receipt_or_authenticated_tag_absence_preserves_registry(self):
        intent, digest, _ = self.fixtures()
        receipt = transfer.publication_receipt(expected(), intent["producer"], digest)
        for options in ({"receipts": [receipt]}, {"absent": True}):
            with self.subTest(options=options):
                result, calls = self.exercise(**options)
                self.assertFalse(result)
                self.assertFalse(calls)
        with self.assertRaisesRegex(ValueError, "changed"):
            self.exercise(receipts=[None, receipt])
        self.assertFalse(any(method == "DELETE" for _, _, method in self.calls))
        with self.assertRaises(ValueError):
            self.exercise(receipts=[{**receipt, "source_sha": "b" * 40}])
        self.assertFalse(any(method == "DELETE" for _, _, method in self.calls))


@unittest.skipUnless(os.environ.get("GITHUB_ACTIONS") == "true" and os.environ.get("PUBLIC_CI_DOCKER_COMPATIBILITY") == "1",
                     "native Docker compatibility runs only in the package's GitHub CI")
class NativeArchiveCompatibility(unittest.TestCase):
    def test_standard_runner_preserves_captured_image_and_archive_configuration(self):
        tag = "endurant-ci-transfer:" + uuid.uuid4().hex
        owned = {tag}
        self.assertFalse(transfer.image_present(tag))
        try:
            with tempfile.TemporaryDirectory(prefix="public-image-compat-") as temporary:
                directory = Path(temporary)
                (directory / "Dockerfile").write_text("FROM scratch\nCOPY payload /payload\n")
                (directory / "payload").write_text(uuid.uuid4().hex)
                transfer.command("docker", "build", "--platform", "linux/amd64", "--tag", tag, str(directory))
                captured = transfer.image_identity(tag)["Id"]
                owned.add(captured)
                archive = directory / "image.tar.gz"
                archive.write_bytes(gzip.compress(transfer.command("docker", "image", "save", captured)))
                config, identifiers = transfer.archive_identity(archive)
                owned.update(identifiers)
                self.assertIn(captured, identifiers)
                transfer.remove_image(tag)
                output = transfer.command("docker", "image", "load", "--quiet", "--input", str(archive))
                prefix = b"Loaded image ID: "
                self.assertTrue(output.startswith(prefix))
                restored = output.strip().removeprefix(prefix).decode()
                self.assertIn(restored, identifiers)
                self.assertEqual(transfer.image_identity(restored)["Id"], restored)
                repeated = directory / "restored.tar.gz"
                repeated.write_bytes(gzip.compress(transfer.command("docker", "image", "save", restored)))
                self.assertEqual(transfer.archive_identity(repeated)[0], config)
        finally:
            failed = []
            for image in [tag, *sorted(owned - {tag})]:
                try:
                    transfer.remove_image(image)
                except (ValueError, subprocess.CalledProcessError) as error:
                    failed.append(error)
            self.assertEqual(failed, [], "all exact compatibility-test images must be cleaned")


if __name__ == "__main__":
    unittest.main()
