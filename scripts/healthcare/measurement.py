#!/usr/bin/env python3
"""Bind fixed, data-only Healthcare coverage producers to one public run."""

import hashlib
import json
import os
import re
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path, PurePosixPath

from coverage import CoverageData

MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024 - 64 * 1024  # Reserve ZIP headers below the verifier limit.
POSTGRES_SHARDS = (
    "core-services", "core-services-2", "core-imports", "core-imports-2", "core-snapshots", "core-snapshots-2", "core-ptg",
    *(f"registry-{index}" for index in range(48)),
    "directory-source", "directory-source-2", "directory-storage", "directory-address",
    "profile-storage", "profile-publication",
)
POSTGRES_COHORT_FILENAME = ".coverage-postgres-cohort.json"
POSTGRES_COVERAGE_FILENAME = ".coverage.postgres.combined"
SQL_COLUMNS = {
    "coverage_schema": ("version",), "meta": ("key", "value"),
    "file": ("id", "path"), "context": ("id", "context"),
    "line_bits": ("file_id", "context_id", "numbits"),
    "arc": ("file_id", "context_id", "fromno", "tono"),
    "tracer": ("file_id", "tracer"),
}


def producer_files():
    result = {}
    for kind, shards in (("main", ("0", "1", "2", "3", "4", "5")),
                         ("capacity", ("capacity",)),
                         ("postgres", POSTGRES_SHARDS)):
        for shard in shards:
            suffix = kind if kind == "capacity" else f"{kind}.{shard}"
            artifact = "mrf-python-coverage-" + suffix.replace(".", "-")
            result[artifact] = (f".coverage.{suffix}", f".coverage-provenance.{suffix}.json")
    result["mrf-rust-coverage"] = ("test-coverage-rust.json", "coverage-provenance-rust.json")
    return result


def transport_files():
    """Keep every provenance sidecar while transporting one PostgreSQL union."""
    original = producer_files()
    result = {name: files for name, files in original.items() if "-postgres-" not in name}
    result["mrf-python-coverage-postgres-combined"] = (
        POSTGRES_COVERAGE_FILENAME, POSTGRES_COHORT_FILENAME,
        *(original[f"mrf-python-coverage-postgres-{shard}"][1] for shard in POSTGRES_SHARDS),
    )
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


def _member_paths(directory, specification):
    expected = {name for names in specification.values() for name in names}
    paths = {path.name: path for path in directory.iterdir()}
    if set(paths) != expected:
        raise ValueError("measurement must contain exactly every coverage producer")
    return paths


def _hash_files(paths, aggregate):
    total = 0
    hashes = {}
    for name, path in sorted(paths.items()):
        if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= MAX_FILE_BYTES:
            raise ValueError("invalid measurement file")
        total += path.stat().st_size
        if aggregate and total > MAX_TOTAL_BYTES:
            raise ValueError("measurement exceeds aggregate limit")
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        if name.startswith(".coverage."):
            validate_sqlite(path)
    return hashes


def _verify_original_provenance(paths, identity, hashes, specification):
    for names in specification.values():
        report, provenance = names
        value = read_json(paths[provenance])
        if (not isinstance(value, dict) or value.get("head_sha") != identity["source_sha"]
                or value.get("base_sha") != identity["base_sha"]):
            raise ValueError("coverage provenance differs from the public source tuple")
        hash_field = "coverage_sha256" if report.startswith(".coverage.") else "report_sha256"
        if value.get(hash_field) != hashes[report]:
            raise ValueError("coverage provenance digest differs")
    if not isinstance(read_json(paths["test-coverage-rust.json"]), dict):
        raise ValueError("Rust coverage must be an object")


def _postgres_cohort(paths, identity, hashes):
    """Bind the union to every unchanged producer sidecar and common header."""
    headers = None
    inputs = {}
    for shard in POSTGRES_SHARDS:
        report, provenance = producer_files()[f"mrf-python-coverage-postgres-{shard}"]
        value = read_json(paths[provenance])
        fields = {"schema_version", "kind", "shard", "report_name", "head_sha", "base_sha",
                  "baseline_sha256", "coverage_version", "coverage_file", "coverage_sha256"}
        if (not isinstance(value, dict) or set(value) not in (fields, fields | {"common_test_execution"})
                or ("common_test_execution" in value and not isinstance(value["common_test_execution"], dict))):
            raise ValueError("PostgreSQL cohort provenance fields differ")
        candidate = {name: value.get(name) for name in
                     ("head_sha", "base_sha", "baseline_sha256", "coverage_version")}
        if (candidate["head_sha"] != identity["source_sha"]
                or candidate["base_sha"] != identity["base_sha"]
                or not isinstance(candidate["baseline_sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", candidate["baseline_sha256"])
                or not isinstance(candidate["coverage_version"], str) or not candidate["coverage_version"]
                or type(value.get("schema_version")) is not int or value["schema_version"] != 1
                or value.get("kind") != "postgres"
                or value.get("shard") != shard or value.get("report_name") != "python"
                or value.get("coverage_file") != report
                or not isinstance(value.get("coverage_sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", value["coverage_sha256"])):
            raise ValueError("PostgreSQL cohort provenance is malformed or stale")
        if headers is not None and candidate != headers:
            raise ValueError("PostgreSQL cohort provenance headers differ")
        headers = candidate
        inputs[shard] = {"coverage_sha256": value["coverage_sha256"],
                         "provenance_sha256": hashes[provenance]}
    return {"schema": "python-coverage-cohort/v1", "kind": "postgres", **headers,
            "coverage_file": POSTGRES_COVERAGE_FILENAME,
            "coverage_sha256": hashes.get(POSTGRES_COVERAGE_FILENAME), "inputs": inputs}


def _validate_input_paths(paths, identity):
    # Fixed producer count and per-file limits bound staging before consolidation.
    hashes = _hash_files(paths, aggregate=False)
    _verify_original_provenance(paths, identity, hashes, producer_files())
    _postgres_cohort(paths, identity, hashes)
    return hashes


def validate_input_files(directory, identity):
    """Validate all original files before any producer data is combined."""
    return _validate_input_paths(_member_paths(directory, producer_files()), identity)


def validate_files(directory, identity):
    """Validate the closed transport layout and authenticated PostgreSQL cohort."""
    paths = _member_paths(directory, transport_files())
    hashes = _hash_files(paths, aggregate=True)
    unchanged = {name: files for name, files in producer_files().items() if "-postgres-" not in name}
    _verify_original_provenance(paths, identity, hashes, unchanged)
    if read_json(paths[POSTGRES_COHORT_FILENAME]) != _postgres_cohort(paths, identity, hashes):
        raise ValueError("PostgreSQL cohort differs from its provenance or result")
    return hashes


def rewrite_postgres_cohort_result(directory, identity):
    """Rebind only the result after a trusted consumer adds PostgreSQL coverage."""
    paths = _member_paths(directory, transport_files())
    hashes = _hash_files(paths, aggregate=True)
    unchanged = {name: files for name, files in producer_files().items() if "-postgres-" not in name}
    _verify_original_provenance(paths, identity, hashes, unchanged)
    expected = _postgres_cohort(paths, identity, hashes)
    original = read_json(paths[POSTGRES_COHORT_FILENAME])
    old_hash = original.get("coverage_sha256") if isinstance(original, dict) else None
    if (not isinstance(old_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", old_hash)
            or {**original, "coverage_sha256": expected["coverage_sha256"]} != expected):
        raise ValueError("PostgreSQL cohort provenance cannot be rewritten")
    paths[POSTGRES_COHORT_FILENAME].write_text(json.dumps(expected, sort_keys=True) + "\n")
    return expected


def _combine_postgres(paths, output):
    """Union validated SQLite data without loading source configuration or plugins."""
    combined = CoverageData(basename=str(output / POSTGRES_COVERAGE_FILENAME))
    combined.add_arcs({})
    for shard in POSTGRES_SHARDS:
        original = CoverageData(basename=str(paths[f".coverage.postgres.{shard}"]))
        original.read()
        combined.update(original)
    combined.write()
    with closing(sqlite3.connect(output / POSTGRES_COVERAGE_FILENAME)) as database:
        database.execute("VACUUM")


def publish(staging, output, identity, run_id, run_attempt, revision):
    if not re.fullmatch(r"[1-9][0-9]*", str(run_id)) or not re.fullmatch(r"[1-9][0-9]*", str(run_attempt)):
        raise ValueError("measurement requires one exact run and consumer attempt")
    run_attempt = int(run_attempt)
    expected = producer_files()
    producers = {}
    for directory in staging.iterdir():
        match = re.fullmatch(rf"(.+)-{re.escape(str(run_id))}-([1-9][0-9]*)", directory.name)
        if not match or match[1] not in expected or not 1 <= int(match[2]) <= run_attempt or match[1] in producers:
            raise ValueError("missing, duplicate, or unexpected staging artifact")
        producers[match[1]] = (directory, int(match[2]))
    if set(producers) != set(expected):
        raise ValueError("missing or unexpected staging artifact")
    paths = {}
    for artifact, files in expected.items():
        directory, _ = producers[artifact]
        if directory.is_symlink() or not directory.is_dir() or {path.name for path in directory.iterdir()} != set(files):
            raise ValueError("unexpected staging artifact members")
        for name in files:
            source = directory / name
            if source.is_symlink() or not source.is_file() or source.stat().st_size > MAX_FILE_BYTES:
                raise ValueError("invalid staging artifact member")
            paths[name] = source
    input_hashes = _validate_input_paths(paths, identity)
    output.mkdir()
    retained = {name for files in transport_files().values() for name in files}
    for name in retained & paths.keys():
        shutil.copyfile(paths[name], output / name)
    _combine_postgres(paths, output)
    input_hashes[POSTGRES_COVERAGE_FILENAME] = hashlib.sha256(
        (output / POSTGRES_COVERAGE_FILENAME).read_bytes()).hexdigest()
    (output / POSTGRES_COHORT_FILENAME).write_text(
        json.dumps(_postgres_cohort(paths, identity, input_hashes), sort_keys=True) + "\n")
    hashes = validate_files(output, identity)
    total = sum(path.stat().st_size for path in output.iterdir())
    measurement = {"schema": "public-source-measurement-v2", **identity,
                   "run_id": str(run_id), "run_attempt": run_attempt,
                   "producer_attempts": {name: producers[name][1] for name in sorted(producers)},
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
