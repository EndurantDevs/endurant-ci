"""Client provisioning uses isolated signed sources and cleans every failure path."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/install_postgres_client"
FAKE = r'''#!/usr/bin/env python3
import json, os, signal, subprocess, sys, time
from pathlib import Path

name = Path(sys.argv[0]).name
args = sys.argv[1:]
root = Path(os.environ["FAKE_ROOT"])
def record(**values):
    with (root / "commands.jsonl").open("a") as stream:
        stream.write(json.dumps(dict(command=name, args=args, **values)) + "\n")
if name == "sudo":
    os.execvp(args[0], args)
elif name == "timeout":
    assert args[:3] == ["--signal=TERM", "--kill-after=5s", "180s"]
    record()
    child = subprocess.Popen(args[3:], start_new_session=True)
    if os.environ.get("STALL"):
        deadline = time.monotonic() + 5
        while not (root / "stalled").exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert (root / "stalled").exists()
        os.killpg(child.pid, signal.SIGTERM)
        time.sleep(0.05)
        for pid in (child.pid, int((root / "stalled").read_text())):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        child.wait()
        sys.exit(124)
    code = child.wait(timeout=10)
    sys.exit(code)
elif name == "dpkg":
    if args == ["--print-architecture"]:
        print(os.environ.get("ARCHITECTURE", "amd64"))
    else:
        # Only cached packages that apt already simulated are installed directly.
        assert args[0] == "--install" and os.read(0, 1) == b""
        packages = [Path(arg) for arg in args[1:]]
        assert packages and {package.parent for package in packages} == {Path(os.environ["POSTGRES_CLIENT_CACHE"])}
        record(packages=[package.name for package in packages])
        sys.exit(100 if os.environ.get("CACHED_DPKG_FAILURE") else 0)
elif name == "curl":
    if args == ["--version"]:
        sys.exit(1 if os.environ.get("BOOTSTRAP") and not (root / "bootstrapped").exists() else 0)
    record()
    assert args[-1] == "https://www.postgresql.org/media/keys/ACCC4CF8.asc"
    Path(args[args.index("--output") + 1]).write_text("signed repository key")
elif name == "apt-get":
    options = dict(value.split("=", 1) for i, value in enumerate(args) if i and args[i-1] == "-o")
    # Existing third-party sources and cached indexes must never be selected.
    if "Dir::Etc::sourcelist" not in options:
        record(unscoped=True)
        raise AssertionError("apt sources were not isolated")
    assert os.environ["DEBIAN_FRONTEND"] == "noninteractive" and os.read(0, 1) == b""
    assert os.environ["NEEDRESTART_SUSPEND"] == "1"
    assert Path(options["Dir::Etc::sourcelist"]).read_text() == ""
    sources = Path(options["Dir::Etc::sourceparts"])
    assert sources.parent.parent == root / "temporary"
    assert options["Dir::Cache::pkgcache"] == options["Dir::Cache::srcpkgcache"] == ""
    assert options["DPkg::Lock::Timeout"] == "30"
    assert options["APT::Update::Error-Mode"] == "any"
    texts = {p.name: p.read_text() for p in sources.iterdir()}
    packages = [Path(arg) for arg in args if arg.endswith(".deb")]
    if packages:
        # Cached packages install with no configured source, so nothing can be downloaded.
        assert not texts and "install" in args and "--simulate" in args
        assert {package.parent for package in packages} == {Path(os.environ["POSTGRES_CLIENT_CACHE"])}
        record(sources=texts, packages=[package.name for package in packages])
        sys.exit(100 if os.environ.get("CACHED_INSTALL_FAILURE") else 0)
    assert texts and set(texts) <= {"ubuntu.sources", "pgdg.sources"}
    for source in texts.values():
        assert "Signed-By:" in source
        assert "Architectures: " + os.environ.get("ARCHITECTURE", "amd64") in source
    assert "third-party.invalid" not in "".join(texts.values())
    for key, folder in (("Dir::State::lists", "lists"), ("Dir::Cache::archives", "archives")):
        assert Path(options[key]) == sources.parent / folder
        (Path(options[key]) / "owned-cache").write_text("temporary")
    if "pgdg.sources" in texts:
        signed_by = next(line for line in texts["pgdg.sources"].splitlines() if line.startswith("Signed-By:"))
        assert signed_by.split() == ["Signed-By:", str(sources.parent / "pgdg.asc"),
                                     "B97B0AFCAA1A47F044F244A07FCC7D46ACCC4CF8"]
    record(sources=texts)
    if os.environ.get("APT_FAILURE"):
        sys.exit(100)
    if os.environ.get("UBUNTU_DNS_FAILURE") and "update" in args and "ubuntu.sources" in texts:
        sys.exit(100)
    if os.environ.get("UBUNTU_DEPENDENCIES") and "postgresql-client-18" in args and "ubuntu.sources" not in texts:
        sys.exit(100)
    if os.environ.get("INSTALL_FAILURE") and "postgresql-client-18" in args and "--simulate" not in args:
        sys.exit(100)
    if os.environ.get("STALL"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        (root / "stalled").write_text(str(os.getpid()))
        while True:
            time.sleep(1)
    if "ca-certificates" in args:
        (root / "bootstrapped").touch()
    if "postgresql-client-18" in args and "--simulate" not in args:
        (Path(options["Dir::Cache::archives"]) / "postgresql-client-18_18.2_amd64.deb").write_text("package")
elif name in {"psql", "pg_dump", "pg_restore"}:
    record()
    print(name + " (PostgreSQL) " + ("17.9" if name == os.environ.get("BAD_VERSION") else "18.2"))
else:
    raise AssertionError(name)
'''


class InstallPostgresClientChecks(unittest.TestCase):
    def run_installer(self, cache=None, **extra):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            commands, workspace = root / "bin", root / "temporary"
            commands.mkdir()
            workspace.mkdir()
            global_sources = root / "global-sources.list"
            global_sources.write_text("deb https://third-party.invalid stable main\n")
            global_index = root / "global-index"
            global_index.write_text("unrelated cached package")
            (root / "os-release").write_text("ID=ubuntu\nVERSION_CODENAME=noble\n")
            (root / "ubuntu-keyring").write_text("existing Ubuntu archive key")
            (root / "ca-certificates.crt").write_text("existing certificates")
            for name in ("sudo", "timeout", "dpkg", "curl", "apt-get", "psql", "pg_dump", "pg_restore"):
                path = commands / name
                path.write_text(FAKE)
                path.chmod(0o755)
            script = SCRIPT.read_text()
            for original, replacement in (
                ("/etc/os-release", str(root / "os-release")),
                ("/usr/share/keyrings/ubuntu-archive-keyring.gpg", str(root / "ubuntu-keyring")),
                ("/etc/ssl/certs/ca-certificates.crt", str(root / "ca-certificates.crt")),
                ("/usr/lib/postgresql/18/bin", str(commands)),
            ):
                script = script.replace(original, replacement)
            target = root / "installer"
            target.write_text(script)
            environment = dict(os.environ, FAKE_ROOT=str(root), RUNNER_TEMP=str(workspace),
                               GITHUB_PATH=str(root / "github-path"), PATH=str(commands) + os.pathsep + os.environ["PATH"])
            environment.update(extra)
            # cache=False is an empty CI cache directory; cache=True holds packages from an earlier install.
            package_cache, output = root / "package-cache", root / "github-output"
            if cache is not None:
                environment.update(POSTGRES_CLIENT_CACHE=str(package_cache), GITHUB_OUTPUT=str(output))
            if cache:
                package_cache.mkdir()
                (package_cache / "postgresql-client-18_18.2_amd64.deb").write_text("cached package")
            result = subprocess.run(["bash", str(target)], env=environment, capture_output=True, text=True, timeout=15)
            self.cached = sorted(path.name for path in package_cache.glob("*.deb"))
            self.output = output.read_text() if output.exists() else ""
            log = root / "commands.jsonl"
            calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
            self.assertEqual(list(workspace.iterdir()), [], result.stderr)
            self.assertEqual(global_sources.read_text(), "deb https://third-party.invalid stable main\n")
            self.assertEqual(global_index.read_text(), "unrelated cached package")
            path_written = (root / "github-path").exists()
            return result, calls, path_written

    def test_scoped_signed_sources_ignore_global_repository_and_indexes(self):
        result, calls, path_written = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        apt = [call for call in calls if call["command"] == "apt-get"]
        self.assertEqual(len(apt), 3)
        self.assertTrue(all(set(call["sources"]) == {"pgdg.sources"} for call in apt))
        self.assertEqual([arg for arg in apt[1]["args"] if arg != "--simulate"], apt[2]["args"])
        self.assertFalse(any("ca-certificates" in call["args"] for call in apt))
        self.assertEqual([call["command"] for call in calls if call["command"] in {"psql", "pg_dump", "pg_restore"}],
                         ["psql", "pg_dump", "pg_restore"])
        self.assertTrue(path_written)

    def test_missing_prerequisite_bootstraps_from_scoped_ubuntu_only(self):
        result, calls, _ = self.run_installer(BOOTSTRAP="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        apt = [call for call in calls if call["command"] == "apt-get"]
        self.assertEqual(len(apt), 5)
        self.assertTrue(all(set(call["sources"]) == {"ubuntu.sources"} for call in apt[:2]))
        self.assertTrue(all(set(call["sources"]) == {"pgdg.sources"} for call in apt[2:]))

    def test_arm64_uses_native_signed_ubuntu_ports(self):
        result, calls, _ = self.run_installer(ARCHITECTURE="arm64", UBUNTU_DEPENDENCIES="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        apt = next(call for call in calls if call["command"] == "apt-get" and "ubuntu.sources" in call["sources"])
        self.assertIn("http://ports.ubuntu.com/ubuntu-ports", apt["sources"]["ubuntu.sources"])
        self.assertNotIn("archive.ubuntu.com", apt["sources"]["ubuntu.sources"])

    def test_ubuntu_dns_is_unused_when_pgdg_resolves_dependencies(self):
        result, calls, path_written = self.run_installer(UBUNTU_DNS_FAILURE="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(path_written)
        self.assertTrue(all(set(call["sources"]) == {"pgdg.sources"}
                            for call in calls if call["command"] == "apt-get"))

    def test_missing_dependency_enables_signed_ubuntu_sources_before_install(self):
        result, calls, path_written = self.run_installer(UBUNTU_DEPENDENCIES="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        apt = [call for call in calls if call["command"] == "apt-get"]
        self.assertEqual(len(apt), 4)
        self.assertTrue(all(set(call["sources"]) == {"pgdg.sources"} for call in apt[:2]))
        self.assertTrue(all(set(call["sources"]) == {"pgdg.sources", "ubuntu.sources"} for call in apt[2:]))
        self.assertTrue(path_written)

    def test_failed_fallback_or_real_install_cannot_publish_client_path(self):
        for failure in ({"UBUNTU_DEPENDENCIES": "1", "UBUNTU_DNS_FAILURE": "1"},
                        {"INSTALL_FAILURE": "1"}):
            with self.subTest(failure=failure):
                result, _, path_written = self.run_installer(**failure)
                self.assertEqual(result.returncode, 100, result.stderr)
                self.assertFalse(path_written)

    def test_unknown_architecture_refuses_before_any_package_operation(self):
        result, calls, path_written = self.run_installer(ARCHITECTURE="unknown")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertFalse(any(call["command"] == "apt-get" for call in calls))
        self.assertFalse(path_written)

    def test_failed_apt_preserves_failure_and_cleans_owned_files(self):
        result, _, path_written = self.run_installer(APT_FAILURE="1")
        self.assertEqual(result.returncode, 100, result.stderr)
        self.assertFalse(path_written)

    def test_deadline_terminates_stalled_child_and_cleans_owned_files(self):
        result, _, path_written = self.run_installer(STALL="1")
        self.assertEqual(result.returncode, 124, result.stderr)
        self.assertFalse(path_written)

    def test_cache_miss_stages_signed_install_packages(self):
        result, calls, path_written = self.run_installer(cache=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sum(call["command"] == "curl" for call in calls), 1)
        self.assertEqual(self.cached, ["postgresql-client-18_18.2_amd64.deb"])
        self.assertEqual(self.output, "cache-ready=true\n")
        self.assertTrue(path_written)

    def test_cache_hit_installs_offline_without_key_or_index_downloads(self):
        result, calls, path_written = self.run_installer(cache=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(call["command"] == "curl" for call in calls))
        package = ["postgresql-client-18_18.2_amd64.deb"]
        self.assertEqual([(call["command"], call.get("sources"), call["packages"]) for call in calls if "packages" in call],
                         [("apt-get", {}, package), ("dpkg", None, package)])
        self.assertIn("Installed PostgreSQL client from cached packages", result.stdout)
        self.assertEqual(self.output, "")
        self.assertTrue(path_written)

    def test_rejected_cache_falls_back_to_signed_indexes(self):
        for failure, dpkg_calls in (("CACHED_INSTALL_FAILURE", 0), ("CACHED_DPKG_FAILURE", 1)):
            with self.subTest(failure=failure):
                result, calls, path_written = self.run_installer(cache=True, **{failure: "1"})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(sum(call["command"] == "curl" for call in calls), 1)
                apt = [call for call in calls if call["command"] == "apt-get"]
                self.assertEqual([bool(call.get("packages")) for call in apt], [True, False, False, False])
                self.assertEqual(sum(call["command"] == "dpkg" for call in calls), dpkg_calls)
                self.assertEqual(self.output, "")
                self.assertTrue(path_written)

    def test_wrong_native_client_major_fails_before_path_publication(self):
        result, _, path_written = self.run_installer(BAD_VERSION="pg_dump")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertFalse(path_written)


if __name__ == "__main__":
    unittest.main()
