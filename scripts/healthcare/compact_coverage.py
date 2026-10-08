"""Compact completed branch coverage before its provenance digest is written."""

import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path

from measurement import MAX_FILE_BYTES, SQL_COLUMNS, validate_sqlite

ARC_SCHEMA = """CREATE TABLE arc (
    file_id integer, context_id integer, fromno integer, tono integer,
    foreign key (file_id) references file (id),
    foreign key (context_id) references context (id),
    primary key (file_id, context_id, fromno, tono)
) WITHOUT ROWID"""


def _digest(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").digest()


def compact_coverage(path):
    """Atomically preserve all coverage rows in a compact native SQLite layout.

    This runs only after the producer has closed coverage, before provenance.
    The arc primary key retains coverage.py's four-column uniqueness while
    avoiding a second copy of every arc in a rowid table and its unique index.
    """
    path = Path(path)
    if (
        path.is_symlink()
        or not path.is_file()
        or not 0 < path.stat().st_size <= MAX_FILE_BYTES
    ):
        raise ValueError("invalid completed coverage file")
    if any(
        Path(str(path) + suffix).exists() for suffix in ("-journal", "-wal", "-shm")
    ):
        raise ValueError("coverage database must be closed")
    validate_sqlite(path)
    before = path.stat().st_size
    original_digest = _digest(path)
    descriptor, name = tempfile.mkstemp(prefix=".coverage-compact-", dir=path.parent)
    os.close(descriptor)
    temporary = Path(name)
    try:
        shutil.copyfile(path, temporary)
        with closing(sqlite3.connect(temporary, uri=True)) as db:
            db.execute("PRAGMA trusted_schema=OFF")
            budget = 100000

            def bounded():
                nonlocal budget
                budget -= 1
                return budget < 0

            db.set_progress_handler(bounded, 1000)
            if db.execute(
                "SELECT 1 FROM arc WHERE file_id IS NULL OR context_id IS NULL OR fromno IS NULL OR tono IS NULL LIMIT 1"
            ).fetchone():
                raise ValueError("coverage arcs require complete identities")
            db.execute("BEGIN IMMEDIATE")
            db.execute("ALTER TABLE arc RENAME TO arc_rowid")
            db.execute(ARC_SCHEMA)
            db.execute(
                "INSERT INTO arc SELECT * FROM arc_rowid ORDER BY file_id, context_id, fromno, tono"
            )
            db.execute("DROP TABLE arc_rowid")
            db.commit()
            db.execute("VACUUM")
            db.execute(
                "ATTACH DATABASE ? AS original",
                (path.resolve().as_uri() + "?mode=ro&immutable=1",),
            )
            for table in SQL_COLUMNS:
                for left, right in (("main", "original"), ("original", "main")):
                    if db.execute(
                        f"SELECT * FROM {left}.{table} EXCEPT SELECT * FROM {right}.{table} LIMIT 1"
                    ).fetchone():
                        raise ValueError("coverage compaction changed measured data")
            if db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise ValueError("compacted coverage database is invalid")
        validate_sqlite(temporary)
        if temporary.stat().st_size > before or _digest(path) != original_digest:
            raise ValueError("completed coverage changed during compaction")
        os.chmod(temporary, path.stat().st_mode & 0o777)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        return before, path.stat().st_size
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("expected one completed coverage file")
    compact_coverage(Path(sys.argv[1]))
