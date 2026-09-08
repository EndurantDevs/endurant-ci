#!/usr/bin/env python3
"""Bind fixed, data-only Healthcare coverage producers to one public run."""

import hashlib
from contextlib import closing
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3


MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024 - 64 * 1024  # Reserve ZIP headers below the verifier limit.
SQL_COLUMNS = {
    "coverage_schema": ("version",), "meta": ("key", "value"),
    "file": ("id", "path"), "context": ("id", "context"),
    "line_bits": ("file_id", "context_id", "numbits"),
    "arc": ("file_id", "context_id", "fromno", "tono"),
    "tracer": ("file_id", "tracer"),
}


def producer_files():
    result = {}
    for kind, shards in (("main", ("0", "1", "2", "3")),
                         ("capacity", ("capacity",)),
                         ("postgres", ("core", "provider-directory", "provider-profile"))):
        for shard in shards:
            suffix = kind if kind == "capacity" else f"{kind}.{shard}"
            artifact = "mrf-python-coverage-" + suffix.replace(".", "-")
            result[artifact] = (f".coverage.{suffix}", f".coverage-provenance.{suffix}.json")
    result["mrf-rust-coverage"] = ("test-coverage-rust.json", "coverage-provenance-rust.json")
    return result


def read_json(path):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate JSON key")
            value[key] = item
        return value
    return json.loads(path.read_bytes(), object_pairs_hook=unique,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))


def validate_sqlite(path):
    """Open coverage data without writes, extensions, views, triggers, or tracers."""
    with path.open("rb") as handle:
        header = handle.read(16)
    if header != b"SQLite format 3\0":
        raise ValueError("coverage data must be SQLite")
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
        db.execute("PRAGMA trusted_schema=OFF")
        db.execute("PRAGMA query_only=ON")
        budget = 10000
        def bounded():
            nonlocal budget
            budget -= 1
            return budget < 0
        db.set_progress_handler(bounded, 1000)
        schema = db.execute("SELECT type, name, sql FROM sqlite_master").fetchall()
        tables = {name for kind, name, _ in schema if kind == "table"}
        if tables != set(SQL_COLUMNS) or any(
            (kind == "table" and not sql.upper().startswith("CREATE TABLE "))
            or (kind != "table" and not (kind == "index" and name.startswith("sqlite_autoindex_") and sql is None))
            for kind, name, sql in schema
        ):
            raise ValueError("unexpected coverage SQLite schema")
        for table, columns in SQL_COLUMNS.items():
            info = db.execute(f"PRAGMA table_xinfo({table})").fetchall()
            if tuple(row[1] for row in info) != columns or any(row[6] != 0 for row in info):
                raise ValueError("unexpected coverage SQLite columns")
        if db.execute("SELECT version FROM coverage_schema").fetchall() != [(7,)]:
            raise ValueError("unexpected coverage SQLite version")
        if db.execute("SELECT count(*) FROM tracer WHERE tracer != ''").fetchone()[0]:
            raise ValueError("coverage plugins are not accepted")
        if db.execute("SELECT value FROM meta WHERE key = 'has_arcs'").fetchall() != [("1",)]:
            raise ValueError("branch coverage is required")
        for (name,) in db.execute("SELECT path FROM file"):
            path_value = PurePosixPath(name)
            if (not path_value.parts or path_value.is_absolute() or ".." in path_value.parts or "\\" in name
                    or not (name == "main.py" or path_value.parts[0] in
                            {"api", "db", "process", "public_evidence", "service"})):
                raise ValueError("coverage path is outside public application source")


def validate_files(directory, identity):
    expected = {name for names in producer_files().values() for name in names}
    actual = {path.name for path in directory.iterdir()}
    if actual != expected:
        raise ValueError("measurement must contain exactly every coverage producer")
    total = 0
    hashes = {}
    for name in sorted(expected):
        path = directory / name
        if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= MAX_FILE_BYTES:
            raise ValueError("invalid measurement file")
        total += path.stat().st_size
        if total > MAX_TOTAL_BYTES:
            raise ValueError("measurement exceeds aggregate limit")
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        if name.startswith(".coverage."):
            validate_sqlite(path)
    for names in producer_files().values():
        report, provenance = names
        value = read_json(directory / provenance)
        if value.get("head_sha") != identity["source_sha"] or value.get("base_sha") != identity["base_sha"]:
            raise ValueError("coverage provenance differs from the public source tuple")
        hash_field = "coverage_sha256" if report.startswith(".coverage.") else "report_sha256"
        if value.get(hash_field) != hashes[report]:
            raise ValueError("coverage provenance digest differs")
    # Rust coverage is JSON data; no source scripts or archives are consumed here.
    if not isinstance(read_json(directory / "test-coverage-rust.json"), dict):
        raise ValueError("Rust coverage must be an object")
    return hashes


def publish(staging, output, identity, run_id, run_attempt, revision):
    expected = {f"{name}-{run_id}-{run_attempt}": files for name, files in producer_files().items()}
    if {path.name for path in staging.iterdir()} != set(expected):
        raise ValueError("missing or unexpected staging artifact")
    output.mkdir()
    total = 0
    for artifact, files in expected.items():
        directory = staging / artifact
        if directory.is_symlink() or not directory.is_dir() or {path.name for path in directory.iterdir()} != set(files):
            raise ValueError("unexpected staging artifact members")
        for name in files:
            source = directory / name
            if source.is_symlink() or not source.is_file() or source.stat().st_size > MAX_FILE_BYTES:
                raise ValueError("invalid staging artifact member")
            total += source.stat().st_size
            if total > MAX_TOTAL_BYTES:
                raise ValueError("measurement exceeds aggregate limit")
            shutil.copyfile(source, output / name)
    hashes = validate_files(output, identity)
    measurement = {"schema": "public-source-measurement-v1", **identity,
                   "run_id": str(run_id), "run_attempt": int(run_attempt),
                   "ci_revision": revision, "sha256": hashes}
    encoded = (json.dumps(measurement, sort_keys=True) + "\n").encode()
    if total + len(encoded) > MAX_TOTAL_BYTES:
        raise ValueError("measurement metadata exceeds aggregate limit")
    (output / "measurement.json").write_bytes(encoded)


if __name__ == "__main__":
    temporary = Path(os.environ["RUNNER_TEMP"])
    publish(temporary / "healthcare-coverage-staging", temporary / "healthcare-public-measurement",
            read_json(temporary / "source-identity.json"), os.environ["GITHUB_RUN_ID"],
            os.environ["GITHUB_RUN_ATTEMPT"], os.environ["CI_REVISION"])
