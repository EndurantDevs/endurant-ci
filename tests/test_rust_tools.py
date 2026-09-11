"""Pinned tool installation fails closed before executing any downloaded bytes."""

import hashlib
import importlib.util
import io
from pathlib import Path
import tarfile
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("install_rust_tools", ROOT / "scripts/healthcare/install_rust_tools.py")
TOOLS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(TOOLS)


class RustToolsChecks(unittest.TestCase):
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
