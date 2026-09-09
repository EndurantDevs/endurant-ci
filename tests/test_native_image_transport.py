"""GitHub-only registry/Docker/containerd digest compatibility; no Kubernetes cluster."""

import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import tarfile
import tempfile
import time
import unittest
import urllib.request
import uuid


K3S_URL = "https://github.com/k3s-io/k3s/releases/download/v1.35.5%2Bk3s1/k3s"
K3S_SHA256 = "267d18da7b3c837d82283f0588fb9031a8a6ff3c0dac772c260c40852ce515f6"
DIND = "docker.io/library/docker@sha256:64d6ee47ea821c986467199baa162f5ac8cde3f57b719f18e23f3ed7a7444131"
REPOSITORY = "docker.io/library/python"
INDEX = "sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6"
MANIFEST = "sha256:810da6270e43d30a1f3e0e1eabbeb6fbd9d78ad9dd2e754d5297a3d6cb42df46"
CONFIG = "sha256:9b84b0c75adf7a34fb1b00b406a50dc9f7bebd3bfbe3e7fd9ff168a6f9d0dfb9"


@unittest.skipUnless(os.environ.get("GITHUB_ACTIONS") == "true"
                     and os.environ.get("PUBLIC_CI_DOCKER_COMPATIBILITY") == "1",
                     "native transport runs only in explicitly enabled GitHub CI")
class NativeImageTransport(unittest.TestCase):
    def test_registry_digests_survive_the_native_import_transport(self):
        self.assertEqual((platform.system(), platform.machine()), ("Linux", "x86_64"))
        task = Path(tempfile.mkdtemp(prefix="ci-image-", dir="/tmp"))
        name = "ci-image-" + uuid.uuid4().hex
        daemon = None
        daemon_log = None
        container_id = None
        volume = name + "-docker"
        volume_owned = False
        image_owned = False

        def command(*args, **kwargs):
            return subprocess.run(args, check=True, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, timeout=180, **kwargs).stdout

        def inspect(kind, reference):
            result = subprocess.run(["docker", kind, "inspect", reference], capture_output=True, timeout=30)
            if result.returncode:
                if f"no such {kind}".encode() not in result.stderr.lower():
                    raise RuntimeError(result.stderr.decode())
                return None
            return json.loads(result.stdout)[0]

        def cleanup():
            errors = []
            try:
                existing = inspect("container", name)
                if existing is not None:
                    self.assertEqual(existing["Config"]["Labels"].get("ci.transport"), name)
                    if container_id is not None:
                        self.assertEqual(existing["Id"], container_id)
                    command("docker", "container", "rm", "--force", existing["Id"])
                    self.assertIsNone(inspect("container", existing["Id"]))
                if volume_owned:
                    existing_volume = inspect("volume", volume)
                    if existing_volume is not None:
                        self.assertEqual(existing_volume["Labels"].get("ci.transport"), name)
                        command("docker", "volume", "rm", volume)
                    self.assertIsNone(inspect("volume", volume))
                if image_owned and inspect("image", DIND) is not None:
                    command("docker", "image", "rm", "--no-prune", DIND)
                    self.assertIsNone(inspect("image", DIND))
            except Exception as error:
                errors.append(error)
            try:
                if daemon is not None and daemon.poll() is None:
                    command("sudo", "-n", "kill", "-TERM", "--", f"-{daemon.pid}")
                    try:
                        daemon.wait(timeout=20)
                    except subprocess.TimeoutExpired:
                        command("sudo", "-n", "kill", "-KILL", "--", f"-{daemon.pid}")
                        daemon.wait(timeout=10)
                        errors.append(AssertionError("containerd needed forced termination"))
                if daemon is not None:
                    self.assertIsNotNone(daemon.poll())
                if daemon_log is not None:
                    daemon_log.close()
                mounts = [row.split()[4] for row in Path("/proc/self/mountinfo").read_text().splitlines()]
                self.assertFalse([mount for mount in mounts if mount == str(task) or mount.startswith(str(task) + "/")])
                command("sudo", "-n", "rm", "-rf", "--", str(task))
                self.assertFalse(task.exists())
            except Exception as error:
                errors.append(error)
            self.assertEqual(errors, [], "exact transport resources must be cleaned")

        self.addCleanup(cleanup)  # Registered before downloads, containers, or daemons.
        self.assertIsNone(inspect("container", name))
        image_owned = inspect("image", DIND) is None
        binary = task / "k3s-bin"
        with urllib.request.urlopen(K3S_URL, timeout=60) as response, binary.open("xb") as output:
            while block := response.read(1024 * 1024):
                output.write(block)
        self.assertEqual(hashlib.sha256(binary.read_bytes()).hexdigest(), K3S_SHA256)
        binary.chmod(0o700)
        command(str(binary), "ctr", "--help", env=os.environ | {"K3S_DATA_DIR": str(task / "k3s")})
        embedded = task / "k3s/data/current/bin"
        for tool in ("containerd", "ctr"):
            self.assertTrue((embedded / tool).resolve(strict=True).is_relative_to(task))
        self.assertIn(b"v2.2.3-k3s1", command(str(embedded / "containerd"), "--version"))

        config = task / "containerd.toml"
        config.write_text('version = 3\nimports = []\ndisabled_plugins = ["io.containerd.grpc.v1.cri", '
                          '"io.containerd.cri.v1.images", "io.containerd.cri.v1.runtime", '
                          '"io.containerd.nri.v1.nri"]\n'
                          '[plugins."io.containerd.internal.v1.opt"]\npath = ' + json.dumps(str(task / "opt")) + "\n")
        socket = task / "containerd.sock"
        daemon_log = (task / "containerd.log").open("wb")
        daemon = subprocess.Popen(["sudo", "-n", str(embedded / "containerd"), "--config", str(config),
                                   "--root", str(task / "root"), "--state", str(task / "state"),
                                   "--address", str(socket)], stdout=daemon_log, stderr=subprocess.STDOUT,
                                  start_new_session=True)

        def ctr(*args, **kwargs):
            return command("sudo", "-n", str(embedded / "ctr"), "--address", str(socket),
                           "--namespace", name, *args, **kwargs)

        deadline = time.monotonic() + 60
        while not socket.exists():
            self.assertIsNone(daemon.poll(), (task / "containerd.log").read_text())
            self.assertLess(time.monotonic(), deadline, "isolated containerd did not start")
            time.sleep(0.5)
        version = ctr("version")
        self.assertEqual(version.count(b"v2.2.3-k3s1"), 2, version)
        docker_config = task / "daemon.json"
        docker_config.write_text('{"features":{"containerd-snapshotter":true}}\n')
        command("docker", "pull", "--platform", "linux/amd64", DIND)
        # Pinned DIND metadata declares only this volume; check before creation.
        self.assertEqual(inspect("image", DIND)["Config"].get("Volumes"), {"/var/lib/docker": {}})
        self.assertIsNone(inspect("volume", volume))
        volume_owned = True  # A failed create response can still leave this exact labelled volume.
        command("docker", "volume", "create", "--label", "ci.transport=" + name, volume)
        container_id = command("docker", "create", "--privileged", "--platform", "linux/amd64", "--name", name,
                               "--label", "ci.transport=" + name, "--env", "DOCKER_TLS_CERTDIR=",
                               "--mount", f"type=volume,src={volume},dst=/var/lib/docker",
                               "--mount", f"type=bind,src={docker_config},dst=/etc/docker/daemon.json,readonly",
                               DIND).decode().strip()
        mounts = inspect("container", container_id)["Mounts"]
        self.assertEqual([(item["Name"], item["Destination"]) for item in mounts if item["Type"] == "volume"],
                         [(volume, "/var/lib/docker")])
        command("docker", "start", container_id)

        def docker(*args, **kwargs):
            return command("docker", "exec", container_id, "docker", *args, **kwargs)

        deadline = time.monotonic() + 60
        while True:
            try:
                info = json.loads(docker("info", "--format", "{{json .}}"))
                break
            except subprocess.CalledProcessError:
                self.assertLess(time.monotonic(), deadline, "isolated Docker did not start")
                time.sleep(0.5)
        self.assertEqual(info["ServerVersion"], "29.1.3")
        self.assertEqual(info["Driver"], "overlayfs")
        self.assertIn(["driver-type", "io.containerd.snapshotter.v1"], info["DriverStatus"])

        for case, target in (("index", INDEX), ("manifest", MANIFEST)):
            with self.subTest(case=case):
                immutable = REPOSITORY + "@" + target
                raw = docker("buildx", "imagetools", "inspect", "--raw", immutable)
                self.assertEqual("sha256:" + hashlib.sha256(raw).hexdigest(), target)
                docker("pull", "--platform", "linux/amd64", immutable)
                tag = "localhost/ci-transport:" + name + "-" + case
                docker("tag", immutable, tag)
                archive = task / (case + ".tar")
                with archive.open("xb") as output:
                    subprocess.run(["docker", "exec", container_id, "docker", "save", tag],
                                   check=True, stdout=output, stderr=subprocess.PIPE, timeout=180)
                with tarfile.open(archive) as bundle:
                    item = bundle.getmember("index.json")
                    self.assertLessEqual(item.size, 65536)
                    roots = json.load(bundle.extractfile(item))["manifests"]
                    self.assertEqual(len(roots), 1)
                    self.assertEqual(roots[0]["digest"], target)
                with archive.open("rb") as incoming:
                    ctr("images", "import", "-", stdin=incoming)  # Same transfer API and unpack defaults as DEV.
                rows = [row.split() for row in ctr("images", "list").decode().splitlines()
                        if row.split() and row.split()[0] == tag]
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0][2], target)
                for digest in dict.fromkeys((target, MANIFEST, CONFIG)):
                    content = ctr("content", "get", digest)
                    self.assertEqual("sha256:" + hashlib.sha256(content).hexdigest(), digest)
                    document = json.loads(content)
                    if digest == INDEX:
                        native = [entry for entry in document["manifests"]
                                  if entry.get("platform") == {"os": "linux", "architecture": "amd64"}]
                        self.assertEqual([entry["digest"] for entry in native], [MANIFEST])
                    elif digest == MANIFEST:
                        self.assertEqual(document["config"]["digest"], CONFIG)
                    else:
                        self.assertEqual((document["os"], document["architecture"]), ("linux", "amd64"))
                ctr("images", "rm", tag)
                self.assertNotIn(tag, ctr("images", "list", "--quiet").decode().splitlines())
                print(f"native transport: {case} target={target} manifest={MANIFEST} config={CONFIG}")
