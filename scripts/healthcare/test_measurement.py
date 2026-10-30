"""Small runnable checks for the data-only public measurement boundary."""

import hashlib
import importlib.util
import json
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from coverage import CoverageData

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


IDENTITY = {"repository": "EndurantDevs/healthcare-mrf-api", "source_sha": "a" * 40,
            "base_sha": "b" * 40, "source_branch": "main", "pr_number": "0", "pr_title": ""}


def populated_staging(root):
    """Give every producer a distinct arc and preserve realistic sidecar headers."""
    staging = root / "staging"
    staging.mkdir()
    originals = {}
    for index, (artifact, (report, provenance)) in enumerate(MEASUREMENT.producer_files().items()):
        directory = staging / f"{artifact}-123-1"
        directory.mkdir()
        if report.startswith(".coverage."):
            data = CoverageData(basename=str(directory / report))
            data.set_context(f"context-{index % 2}")
            data.add_arcs({"api/sample.py": [(-1, 1), (1, index + 2), (index + 2, -1)]})
            data.touch_files(["db/unexecuted.py"])
            data.write()
            kind, shard = ("capacity", "capacity") if report.endswith("capacity") else report.removeprefix(".coverage.").split(".", 1)
            value = {"schema_version": 1, "kind": kind, "shard": shard, "report_name": "python",
                     "coverage_file": report, "coverage_version": "7.16.2", "baseline_sha256": "d" * 64,
                     "coverage_sha256": hashlib.sha256((directory / report).read_bytes()).hexdigest(),
                     "common_test_execution": {"origin": artifact}}
        else:
            (directory / report).write_text('{"data":[]}')
            value = {"report_sha256": hashlib.sha256((directory / report).read_bytes()).hexdigest()}
        value.update(head_sha=IDENTITY["source_sha"], base_sha=IDENTITY["base_sha"])
        (directory / provenance).write_text(json.dumps(value))
        originals[report] = directory / report
        originals[provenance] = directory / provenance
    return staging, originals


def coverage_semantics(path):
    with closing(sqlite3.connect(path)) as database:
        return {
            "files": set(database.execute("SELECT path FROM file")),
            "contexts": set(database.execute("SELECT context FROM context")),
            "arcs": set(database.execute(
                "SELECT f.path,c.context,a.fromno,a.tono FROM arc a "
                "JOIN file f ON f.id=a.file_id JOIN context c ON c.id=a.context_id")),
        }


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
                if "-postgres-" in artifact:
                    value.update(schema_version=1, kind="postgres", shard=artifact.removeprefix("mrf-python-coverage-postgres-"),
                                 report_name="python", coverage_file=report, baseline_sha256="d" * 64,
                                 coverage_version="7.16.2", common_test_execution={"origin": artifact})
                (directory / provenance).write_text(json.dumps(value))
            output = root / "output"
            MEASUREMENT.publish(staging, output, identity, "123", "2", "c" * 40)
            manifest = json.loads((output / "measurement.json").read_text())
            self.assertEqual(manifest["run_attempt"], 2)
            self.assertEqual(manifest["producer_attempts"], producer_attempts)
            self.assertEqual(manifest["schema"], "public-source-measurement-v2")
            self.assertEqual(len(manifest["sha256"]), 80)
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
            required_shards = ("core-services-2", "core-imports", "core-imports-2", "core-snapshots", "core-snapshots-2", *(f"registry-{index}" for index in range(48)))
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
            payload_size = sum(path.stat().st_size for path in output.iterdir() if path.name != "measurement.json")
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

    def test_postgres_union_preserves_every_arc_context_file_and_original_sidecar(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staging, originals = populated_staging(root)
            original_bytes = {name: path.read_bytes() for name, path in originals.items()}
            output = root / "output"
            MEASUREMENT.publish(staging, output, IDENTITY, "123", "2", "c" * 40)
            measurement = MEASUREMENT.read_json(output / "measurement.json")
            self.assertEqual(len(MEASUREMENT.producer_files()), 70)
            self.assertEqual(len(MEASUREMENT.transport_files()), 9)
            self.assertEqual(len(measurement["producer_attempts"]), 70)
            self.assertEqual(len(measurement["sha256"]), 80)
            expected = {kind: set() for kind in ("files", "contexts", "arcs")}
            for shard in MEASUREMENT.POSTGRES_SHARDS:
                report = f".coverage.postgres.{shard}"
                for kind, rows in coverage_semantics(originals[report]).items():
                    expected[kind].update(rows)
            self.assertEqual(coverage_semantics(output / MEASUREMENT.POSTGRES_COVERAGE_FILENAME), expected)
            for name, content in original_bytes.items():
                self.assertEqual(originals[name].read_bytes(), content)
                if not name.startswith(".coverage.postgres."):
                    self.assertEqual((output / name).read_bytes(), content)
            cohort = MEASUREMENT.read_json(output / MEASUREMENT.POSTGRES_COHORT_FILENAME)
            self.assertEqual(set(cohort["inputs"]), set(MEASUREMENT.POSTGRES_SHARDS))
            self.assertEqual(cohort["schema"], "python-coverage-cohort/v1")
            (output / "measurement.json").unlink()
            self.assertEqual(MEASUREMENT.validate_files(output, IDENTITY), measurement["sha256"])
            raw = root / "raw"
            raw.mkdir()
            for name, content in original_bytes.items():
                (raw / name).write_bytes(content)
            with patch.object(MEASUREMENT, "MAX_TOTAL_BYTES", 1):
                self.assertEqual(len(MEASUREMENT.validate_input_files(raw, IDENTITY)), 140)

    def test_transport_rejects_missing_raw_mixed_and_extra_layouts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staging, originals = populated_staging(root)
            output = root / "output"
            MEASUREMENT.publish(staging, output, IDENTITY, "123", "1", "c" * 40)
            (output / "measurement.json").unlink()
            missing = output / ".coverage-provenance.postgres.registry-0.json"
            saved = missing.read_bytes()
            missing.unlink()
            with self.assertRaisesRegex(ValueError, "exactly every"):
                MEASUREMENT.validate_files(output, IDENTITY)
            missing.write_bytes(saved)
            for extra in ("unexpected.json", ".coverage.postgres.registry-0"):
                path = output / extra
                path.write_bytes(originals[".coverage.postgres.registry-0"].read_bytes())
                with self.subTest(extra=extra), self.assertRaisesRegex(ValueError, "exactly every"):
                    MEASUREMENT.validate_files(output, IDENTITY)
                path.unlink()
            raw = root / "raw"
            raw.mkdir()
            for name, path in originals.items():
                shutil.copyfile(path, raw / name)
            with self.assertRaisesRegex(ValueError, "exactly every"):
                MEASUREMENT.validate_files(raw, IDENTITY)

    def test_cohort_rejects_tampered_headers_hashes_inventory_and_duplicate_keys(self):
        changes = (
            ("schema", "python-coverage-cohort/v2"), ("kind", "main"), ("head_sha", "e" * 40),
            ("base_sha", "e" * 40), ("baseline_sha256", "e" * 64), ("coverage_version", "other"),
            ("coverage_file", "other"), ("coverage_sha256", "0" * 64), ("extra", True),
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staging, _ = populated_staging(root)
            output = root / "output"
            MEASUREMENT.publish(staging, output, IDENTITY, "123", "1", "c" * 40)
            (output / "measurement.json").unlink()
            path = output / MEASUREMENT.POSTGRES_COHORT_FILENAME
            original = path.read_bytes()
            for name, value in changes:
                cohort = json.loads(original)
                cohort[name] = value
                path.write_text(json.dumps(cohort))
                with self.subTest(field=name), self.assertRaises(ValueError):
                    MEASUREMENT.validate_files(output, IDENTITY)
            for inventory in ("missing", "extra", "digest", "sidecar-digest", "digest-type"):
                cohort = json.loads(original)
                if inventory == "missing":
                    del cohort["inputs"]["registry-0"]
                elif inventory == "extra":
                    cohort["inputs"]["unexpected"] = dict(cohort["inputs"]["registry-0"])
                else:
                    field = "provenance_sha256" if inventory == "sidecar-digest" else "coverage_sha256"
                    cohort["inputs"]["registry-0"][field] = False if inventory == "digest-type" else "0" * 64
                path.write_text(json.dumps(cohort))
                with self.subTest(inventory=inventory), self.assertRaises(ValueError):
                    MEASUREMENT.validate_files(output, IDENTITY)
            path.write_bytes(original.rstrip()[:-1] + b',"schema":"python-coverage-cohort/v1"}')
            with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
                MEASUREMENT.validate_files(output, IDENTITY)

    def test_invalid_originals_fail_before_combination(self):
        changes = (("head_sha", "e" * 40), ("baseline_sha256", "e" * 64), ("coverage_version", "other"),
                   ("coverage_sha256", "0" * 64), ("schema_version", True), ("coverage_file", "other"),
                   ("kind", "main"), ("shard", "other"), ("report_name", "rust"),
                   ("baseline_sha256", False), ("coverage_sha256", "A" * 64),
                   ("common_test_execution", []), ("unknown", True))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staging, originals = populated_staging(root)
            sidecar = originals[".coverage-provenance.postgres.registry-0.json"]
            original = sidecar.read_bytes()
            for index, (name, value) in enumerate(changes):
                document = json.loads(original)
                document[name] = value
                sidecar.write_text(json.dumps(document))
                output = root / f"invalid-{index}"
                with self.subTest(field=name), patch.object(MEASUREMENT, "_combine_postgres") as combine:
                    with self.assertRaises(ValueError):
                        MEASUREMENT.publish(staging, output, IDENTITY, "123", "1", "c" * 40)
                    combine.assert_not_called()
                    self.assertFalse(output.exists())
            for encoded in (b'[]', b'{"head_sha":NaN}', original.rstrip()[:-1] + b',"kind":"postgres"}'):
                sidecar.write_bytes(encoded)
                with self.subTest(encoded=encoded), self.assertRaises(ValueError):
                    MEASUREMENT.publish(staging, root / "invalid-json", IDENTITY, "123", "1", "c" * 40)
            sidecar.write_bytes(original)
            directory = staging / "mrf-python-coverage-postgres-registry-0-123-1"
            duplicate = staging / "mrf-python-coverage-postgres-registry-0-123-2"
            duplicate.mkdir()
            with self.assertRaisesRegex(ValueError, "missing, duplicate"):
                MEASUREMENT.publish(staging, root / "duplicate", IDENTITY, "123", "2", "c" * 40)
            duplicate.rmdir()
            with closing(sqlite3.connect(originals[".coverage.postgres.registry-0"])) as database, database:
                database.execute("INSERT INTO tracer VALUES (1, 'external.plugin')")
            document = json.loads(original)
            document["coverage_sha256"] = hashlib.sha256(originals[".coverage.postgres.registry-0"].read_bytes()).hexdigest()
            sidecar.write_text(json.dumps(document))
            with self.assertRaisesRegex(ValueError, "plugins"):
                MEASUREMENT.publish(staging, root / "plugin", IDENTITY, "123", "1", "c" * 40)
            self.assertTrue(directory.exists())

    def test_trusted_rewrite_changes_only_result_hash_and_keeps_every_origin(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staging, _ = populated_staging(root)
            output = root / "output"
            MEASUREMENT.publish(staging, output, IDENTITY, "123", "1", "c" * 40)
            (output / "measurement.json").unlink()
            originals = {path.name: path.read_bytes() for path in output.iterdir()}
            cohort = MEASUREMENT.read_json(output / MEASUREMENT.POSTGRES_COHORT_FILENAME)
            data = CoverageData(basename=str(output / MEASUREMENT.POSTGRES_COVERAGE_FILENAME))
            data.read()
            data.set_context("additional-tests")
            data.add_arcs({"api/sample.py": [(7000, 7001)]})
            data.write()
            with self.assertRaisesRegex(ValueError, "cohort"):
                MEASUREMENT.validate_files(output, IDENTITY)
            rewritten = MEASUREMENT.rewrite_postgres_cohort_result(output, IDENTITY)
            self.assertNotEqual(rewritten["coverage_sha256"], cohort["coverage_sha256"])
            self.assertEqual({**rewritten, "coverage_sha256": cohort["coverage_sha256"]}, cohort)
            MEASUREMENT.validate_files(output, IDENTITY)
            for name, content in originals.items():
                if name not in (MEASUREMENT.POSTGRES_COVERAGE_FILENAME, MEASUREMENT.POSTGRES_COHORT_FILENAME):
                    self.assertEqual((output / name).read_bytes(), content)
            path = output / MEASUREMENT.POSTGRES_COHORT_FILENAME
            invalid = dict(rewritten, head_sha="e" * 40)
            path.write_text(json.dumps(invalid))
            unchanged = path.read_bytes()
            with self.assertRaisesRegex(ValueError, "cannot be rewritten"):
                MEASUREMENT.rewrite_postgres_cohort_result(output, IDENTITY)
            self.assertEqual(path.read_bytes(), unchanged)


if __name__ == "__main__":
    unittest.main()
