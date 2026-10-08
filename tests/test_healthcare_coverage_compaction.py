"""Verify lossless, failure-atomic compaction of completed coverage data."""

import importlib.util
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

HELPERS = Path(__file__).resolve().parents[1] / "scripts/healthcare"
sys.path.insert(0, str(HELPERS))
SPEC = importlib.util.spec_from_file_location(
    "compact_coverage", HELPERS / "compact_coverage.py"
)
COMPACTOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(COMPACTOR)
sys.path.pop(0)


def create_coverage(path):
    with closing(sqlite3.connect(path)) as db, db:
        for table, columns in COMPACTOR.SQL_COLUMNS.items():
            if table == "arc":
                db.execute(
                    "CREATE TABLE arc (file_id integer, context_id integer, fromno integer, tono integer, UNIQUE(file_id, context_id, fromno, tono))"
                )
            else:
                db.execute(f"CREATE TABLE {table} ({', '.join(columns)})")
        db.execute("INSERT INTO coverage_schema VALUES (7)")
        db.executemany(
            "INSERT INTO meta VALUES (?, ?)", [("has_arcs", "1"), ("version", "7.16.1")]
        )
        db.executemany(
            "INSERT INTO file VALUES (?, ?)",
            [(1, "api/sample.py"), (2, "process/unexecuted.py")],
        )
        db.executemany("INSERT INTO context VALUES (?, ?)", [(1, ""), (2, "second")])
        db.executemany(
            "INSERT INTO arc VALUES (?, ?, ?, ?)",
            ((1, 1 + line % 2, line, line + 1) for line in range(-1, 10000)),
        )


def rows(path):
    with closing(sqlite3.connect(path)) as db:
        return {
            table: sorted(db.execute(f"SELECT * FROM {table}").fetchall())
            for table in COMPACTOR.SQL_COLUMNS
        }


class CoverageCompactionChecks(unittest.TestCase):
    def test_compaction_enables_uri_support_for_readonly_original_attachment(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / ".coverage"
            create_coverage(path)
            with patch.object(
                COMPACTOR.sqlite3, "connect", wraps=sqlite3.connect
            ) as connect:
                COMPACTOR.compact_coverage(path)
            self.assertEqual(connect.call_count, 3)
            self.assertTrue(
                all(call.kwargs.get("uri") is True for call in connect.call_args_list)
            )

    def test_all_rows_contexts_metadata_and_unexecuted_files_survive(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / ".coverage"
            create_coverage(path)
            original = rows(path)
            before, after = COMPACTOR.compact_coverage(path)
            self.assertLess(after, before)
            self.assertEqual(rows(path), original)
            COMPACTOR.validate_sqlite(path)
            COMPACTOR.compact_coverage(path)
            self.assertEqual(rows(path), original)

    def test_native_insert_or_ignore_and_new_context_append_remain_compatible(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / ".coverage"
            create_coverage(path)
            COMPACTOR.compact_coverage(path)
            with closing(sqlite3.connect(path)) as db, db:
                before = db.execute("SELECT count(*) FROM arc").fetchone()[0]
                db.execute("INSERT OR IGNORE INTO arc VALUES (1, 2, -1, 0)")
                self.assertEqual(
                    db.execute("SELECT count(*) FROM arc").fetchone()[0], before
                )
                db.execute("INSERT INTO context VALUES (3, 'appended')")
                db.execute("INSERT OR IGNORE INTO arc VALUES (1, 3, 1, 9)")
            self.assertIn((1, 3, 1, 9), rows(path)["arc"])
            COMPACTOR.compact_coverage(path)
            self.assertIn((1, 3, 1, 9), rows(path)["arc"])

    def test_replace_failure_leaves_original_bytes_and_no_temporary_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / ".coverage"
            create_coverage(path)
            original = path.read_bytes()
            with (
                patch.object(
                    COMPACTOR.os, "replace", side_effect=OSError("replacement refused")
                ),
                self.assertRaises(OSError),
            ):
                COMPACTOR.compact_coverage(path)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(Path(temporary).iterdir()), [path])

    def test_validation_failure_leaves_original_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / ".coverage"
            create_coverage(path)
            original = path.read_bytes()
            with (
                patch.object(
                    COMPACTOR,
                    "validate_sqlite",
                    side_effect=[None, ValueError("validation refused")],
                ),
                self.assertRaises(ValueError),
            ):
                COMPACTOR.compact_coverage(path)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(Path(temporary).iterdir()), [path])

    def test_incomplete_arcs_are_refused_without_loss(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / ".coverage"
            create_coverage(path)
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("INSERT INTO arc VALUES (1, 1, NULL, 7)")
            original = path.read_bytes()
            with self.assertRaisesRegex(ValueError, "complete identities"):
                COMPACTOR.compact_coverage(path)
            self.assertEqual(path.read_bytes(), original)

    def test_changed_candidate_rows_fail_closed_before_replacement(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / ".coverage"
            create_coverage(path)
            original = path.read_bytes()
            copyfile = COMPACTOR.shutil.copyfile

            def corrupt_copy(source, target):
                copyfile(source, target)
                with closing(sqlite3.connect(target)) as db, db:
                    db.execute("DELETE FROM context WHERE id = 2")

            with (
                patch.object(COMPACTOR.shutil, "copyfile", side_effect=corrupt_copy),
                self.assertRaisesRegex(ValueError, "changed measured data"),
            ):
                COMPACTOR.compact_coverage(path)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(Path(temporary).iterdir()), [path])

    def test_input_bounds_symlinks_and_active_sqlite_sidecars_are_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / ".coverage"
            create_coverage(path)
            original = path.read_bytes()
            link = Path(temporary) / "link"
            link.symlink_to(path)
            with self.assertRaises(ValueError):
                COMPACTOR.compact_coverage(link)
            with (
                patch.object(COMPACTOR, "MAX_FILE_BYTES", 1),
                self.assertRaises(ValueError),
            ):
                COMPACTOR.compact_coverage(path)
            for suffix in ("-wal", "-journal", "-shm"):
                sidecar = Path(str(path) + suffix)
                sidecar.touch()
                with self.assertRaisesRegex(ValueError, "must be closed"):
                    COMPACTOR.compact_coverage(path)
                sidecar.unlink()
            self.assertEqual(path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
