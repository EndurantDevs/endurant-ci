"""Small runnable checks for the data-only public measurement boundary."""

import hashlib
from contextlib import closing
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest


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
            for artifact, (report, provenance) in MEASUREMENT.producer_files().items():
                directory = staging / f"{artifact}-123-2"
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
            self.assertEqual(len(manifest["sha256"]), 18)
            with self.assertRaises(ValueError):
                MEASUREMENT.publish(staging, root / "wrong-attempt", identity, "123", "1", "c" * 40)
            next(output.glob(".coverage.main.*")).write_bytes(b"not SQLite")
            (output / "measurement.json").unlink()
            with self.assertRaises(ValueError):
                MEASUREMENT.validate_files(output, identity)


if __name__ == "__main__":
    unittest.main()
