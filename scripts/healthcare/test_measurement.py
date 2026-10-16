"""Small runnable checks for the data-only public measurement boundary."""

import hashlib
from contextlib import closing
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location("healthcare_measurement", Path(__file__).with_name("measurement.py"))
MEASUREMENT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MEASUREMENT)


def coverage_file(path):
    with closing(sqlite3.connect(path)) as db, db:
        for table, columns in MEASUREMENT.SQL_COLUMNS.items():
            db.execute(f"CREATE TABLE {table} ({', '.join(columns)})")
        db.execute("INSERT INTO coverage_schema VALUES (7)")
        db.execute("INSERT INTO meta VALUES ('has_arcs', '1')")
        db.execute("INSERT INTO file VALUES (1, 'api/sample.py')")


class MeasurementChecks(unittest.TestCase):
    def test_sqlite_rejects_executable_schema_paths_and_tracers(self):
        changes = (
            "CREATE VIEW extra AS SELECT 1", "CREATE TABLE extra(value)",
            "CREATE TRIGGER extra AFTER INSERT ON file BEGIN SELECT 1; END",
            "INSERT INTO tracer VALUES (1, 'external.plugin')",
            "UPDATE file SET path = '/private/source.py'",
            "UPDATE file SET path = 'api/../outside.py'",
            "UPDATE file SET path = ''", "UPDATE coverage_schema SET version = 6",
            "DELETE FROM meta",
        )
        with tempfile.TemporaryDirectory() as temporary:
            for index, change in enumerate(changes):
                with self.subTest(change=change):
                    path = Path(temporary) / str(index)
                    coverage_file(path)
                    MEASUREMENT.validate_sqlite(path)
                    with closing(sqlite3.connect(path)) as db, db:
                        db.execute(change)
                    with self.assertRaises(ValueError):
                        MEASUREMENT.validate_sqlite(path)

    def test_publisher_binds_every_attempt_member_and_digest(self):
        identity = {"repository": "EndurantDevs/healthcare-mrf-api", "source_sha": "a" * 40,
                    "base_sha": "b" * 40, "source_branch": "main", "pr_number": "0", "pr_title": ""}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staging = root / "staging"
            staging.mkdir()
            producer_attempts = {}
            for index, (artifact, (report, provenance)) in enumerate(MEASUREMENT.producer_files().items()):
                producer_attempts[artifact] = 1 + index % 2
                directory = staging / f"{artifact}-123-{producer_attempts[artifact]}"
                directory.mkdir()
                if report.startswith(".coverage."):
                    coverage_file(directory / report)
                    hash_field = "coverage_sha256"
                else:
                    (directory / report).write_text('{"data":[]}')
                    hash_field = "report_sha256"
                value = {"head_sha": identity["source_sha"], "base_sha": identity["base_sha"],
                         hash_field: hashlib.sha256((directory / report).read_bytes()).hexdigest()}
                (directory / provenance).write_text(json.dumps(value))
            output = root / "output"
            MEASUREMENT.publish(staging, output, identity, "123", "2", "c" * 40)
            manifest = json.loads((output / "measurement.json").read_text())
            self.assertEqual(manifest["run_attempt"], 2)
            self.assertEqual(manifest["producer_attempts"], producer_attempts)
            self.assertEqual(len(manifest["sha256"]), 70)
            for shard in ("4", "5"):
                producer = "mrf-python-coverage-main-" + shard
                member = staging / f"{producer}-123-{producer_attempts[producer]}"
                removed = root / "removed-producer"
                member.rename(removed)
                try:
                    with self.subTest(missing_main=shard), self.assertRaisesRegex(
                        ValueError, "missing or unexpected staging artifact"
                    ):
                        MEASUREMENT.publish(staging, root / "missing-main", identity, "123", "2", "c" * 40)
                finally:
                    removed.rename(member)
            required_shards = ("core-imports", "core-imports-2", "core-snapshots", "core-snapshots-2", *(f"registry-{index}" for index in range(16)))
            for shard in required_shards:
                producer = "mrf-python-coverage-postgres-" + shard
                member = staging / f"{producer}-123-{producer_attempts[producer]}"
                removed = root / "removed-producer"
                member.rename(removed)
                try:
                    with self.subTest(missing=shard), self.assertRaisesRegex(
                        ValueError, "missing or unexpected staging artifact"
                    ):
                        MEASUREMENT.publish(staging, root / "missing-producer", identity, "123", "2", "c" * 40)
                finally:
                    removed.rename(member)
            for snapshot_name in ("mrf-python-coverage-main-4", "mrf-python-coverage-main-5",
                                  *(f"mrf-python-coverage-postgres-{shard}" for shard in
                                    ("core-snapshots", "core-snapshots-2", "core-imports-2"))):
                snapshot = staging / f"{snapshot_name}-123-{producer_attempts[snapshot_name]}"
                sidecar = snapshot / MEASUREMENT.producer_files()[snapshot_name][1]
                original_sidecar = sidecar.read_bytes()
                foreign = json.loads(original_sidecar)
                foreign["head_sha"] = "d" * 40
                sidecar.write_text(json.dumps(foreign))
                try:
                    with self.subTest(foreign=snapshot_name), self.assertRaisesRegex(ValueError, "provenance"):
                        MEASUREMENT.publish(staging, root / f"foreign-{snapshot_name}", identity, "123", "2", "c" * 40)
                finally:
                    sidecar.write_bytes(original_sidecar)
            self.assertEqual(MEASUREMENT.MAX_FILE_BYTES, 32 * 1024 * 1024)
            self.assertEqual(MEASUREMENT.MAX_TOTAL_BYTES, 128 * 1024 * 1024 - 64 * 1024)
            payload_size = sum(path.stat().st_size for path in staging.glob("*/*"))
            with patch.object(MEASUREMENT, "MAX_TOTAL_BYTES", payload_size - 1):
                with self.assertRaisesRegex(ValueError, "aggregate limit"):
                    MEASUREMENT.publish(staging, root / "payload-over-limit", identity, "123", "2", "c" * 40)
            with patch.object(MEASUREMENT, "MAX_TOTAL_BYTES", payload_size):
                with self.assertRaisesRegex(ValueError, "metadata exceeds aggregate limit"):
                    MEASUREMENT.publish(staging, root / "metadata-over-limit", identity, "123", "2", "c" * 40)
            with patch.object(MEASUREMENT, "MAX_FILE_BYTES", 1):
                with self.assertRaisesRegex(ValueError, "invalid staging artifact member"):
                    MEASUREMENT.publish(staging, root / "member-over-limit", identity, "123", "2", "c" * 40)
            with self.assertRaises(ValueError):
                MEASUREMENT.publish(staging, root / "wrong-attempt", identity, "123", "1", "c" * 40)
            next(output.glob(".coverage.main.*")).write_bytes(b"not SQLite")
            (output / "measurement.json").unlink()
            with self.assertRaises(ValueError):
                MEASUREMENT.validate_files(output, identity)


if __name__ == "__main__":
    unittest.main()
