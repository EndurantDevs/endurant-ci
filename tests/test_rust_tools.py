"""Pinned tool installation fails closed before executing any downloaded bytes."""

import hashlib
import importlib.util
import io
import subprocess
import tarfile
import tempfile
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("install_rust_tools", ROOT / "scripts/healthcare/install_rust_tools.py")
TOOLS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(TOOLS)


@contextmanager
def _responses(responses):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            status, body, incomplete = responses[min(len(requests), len(responses) - 1)]
            requests.append(self.path)
            self.send_response(status)
            self.send_header("Content-Length", str(len(body) + (10 if incomplete else 0)))
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True

        def log_message(self, *args):
            return

    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/archive", requests
        finally:
            server.shutdown()
            thread.join(timeout=5)
            if thread.is_alive():
                raise RuntimeError("Synthetic download server did not stop")


class RustToolsChecks(unittest.TestCase):
    def test_transient_status_retries_return_only_successful_bytes_and_remove_scratch(self):
        with tempfile.TemporaryDirectory() as directory, _responses(
            [(504, b"partial upstream response", False), (200, b"complete archive", False)]
        ) as (url, requests), patch.object(
            TOOLS, "TemporaryDirectory", lambda **kwargs: tempfile.TemporaryDirectory(dir=directory, **kwargs)
        ):
            self.assertEqual(TOOLS._download_archive(url), b"complete archive")
            self.assertEqual(requests, ["/archive", "/archive"])
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_exhausted_permanent_and_partial_failures_remove_scratch_without_returning_bytes(self):
        for status, body, incomplete, attempts, code in (
            (504, b"unavailable", False, 3, 22),
            (404, b"missing", False, 1, 22),
            (200, b"incomplete", True, 1, 18),
        ):
            with self.subTest(status=status, incomplete=incomplete), tempfile.TemporaryDirectory() as directory, _responses(
                [(status, body, incomplete)]
            ) as (url, requests), patch.object(
                TOOLS, "TemporaryDirectory", lambda **kwargs: tempfile.TemporaryDirectory(dir=directory, **kwargs)
            ):
                with self.assertRaises(subprocess.CalledProcessError) as failure:
                    TOOLS._download_archive(url)
                self.assertEqual(failure.exception.returncode, code)
                self.assertEqual(requests, ["/archive"] * attempts)
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_timeout_keeps_closed_transport_options_and_removes_partial_file(self):
        def timeout(command, *, check, timeout):
            self.assertEqual(command[:2], ["curl", "--disable"])
            self.assertEqual(command[command.index("--proto-redir") + 1], "=https")
            self.assertNotIn("--retry-all-errors", command)
            self.assertTrue(check)
            self.assertEqual(timeout, 95)
            for option, value in (("--retry", "2"), ("--retry-delay", "1"), ("--retry-max-time", "60"),
                                  ("--connect-timeout", "10"), ("--max-time", "30")):
                self.assertEqual(command[command.index(option) + 1], value)
            Path(command[command.index("--output") + 1]).write_bytes(b"partial")
            raise subprocess.TimeoutExpired(command, timeout)

        with tempfile.TemporaryDirectory() as directory, patch.object(
            TOOLS, "TemporaryDirectory", lambda **kwargs: tempfile.TemporaryDirectory(dir=directory, **kwargs)
        ), patch.object(TOOLS.subprocess, "run", side_effect=timeout):
            with self.assertRaises(subprocess.TimeoutExpired):
                TOOLS._download_archive("https://example.invalid/archive")
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_corrupt_successful_download_is_not_retried_or_installed(self):
        with tempfile.TemporaryDirectory() as directory, _responses(
            [(200, b"corrupt archive", False)]
        ) as (url, requests), patch.object(TOOLS.platform, "system", return_value="Linux"), patch.object(
            TOOLS.platform, "machine", return_value="x86_64"
        ), patch.object(TOOLS, "TOOLS", ((url, "0" * 64, "cargo-audit"),)):
            destination = Path(directory) / "binaries"
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                TOOLS.main(destination)
            self.assertEqual(requests, ["/archive"])
            self.assertEqual(list(destination.iterdir()), [])

    def test_only_verified_regular_binary_is_installed_and_existing_files_are_preserved(self):
        for member_type in (tarfile.REGTYPE, tarfile.SYMTYPE):
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
                member = tarfile.TarInfo("release/cargo-audit")
                member.type = member_type
                member.linkname = "../../outside"
                data = b"synthetic tool" if member_type == tarfile.REGTYPE else b""
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
                extra = tarfile.TarInfo("../../outside")
                archive.addfile(extra, io.BytesIO())
            payload = buffer.getvalue()
            checksum = hashlib.sha256(payload).hexdigest()
            with self.subTest(member_type=member_type), tempfile.TemporaryDirectory() as directory:
                destination = Path(directory)
                with self.assertRaises(ValueError):
                    TOOLS.install_archive(payload, "0" * 64, member.name, destination)
                self.assertEqual(list(destination.iterdir()), [])
                if member_type != tarfile.REGTYPE:
                    with self.assertRaises(ValueError):
                        TOOLS.install_archive(payload, checksum, member.name, destination)
                    self.assertEqual(list(destination.iterdir()), [])
                    continue
                TOOLS.install_archive(payload, checksum, member.name, destination)
                binary = destination / "cargo-audit"
                self.assertEqual(list(destination.iterdir()), [binary])
                self.assertEqual(binary.read_bytes(), data)
                self.assertEqual(binary.stat().st_mode & 0o777, 0o755)
                with self.assertRaises(FileExistsError):
                    TOOLS.install_archive(payload, checksum, member.name, destination)
                self.assertEqual(binary.read_bytes(), data)


if __name__ == "__main__":
    unittest.main()
