#!/usr/bin/env python3
"""Export bounded public coverage data for a separate acceptance gate."""

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys

REPORT = "test-coverage-python.json"
PROVENANCE = "coverage-provenance.json"
LIMIT = 64 * 1024 * 1024
MAX_FILE_BYTES = 32 * 1024 * 1024
ZIP_RESERVE = 64 * 1024


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("coverage data contains a duplicate JSON key")
        result[key] = value
    return result


def invalid_constant(value):
    raise ValueError(f"coverage data contains non-JSON constant {value}")


def export_measurement(root, staging, output, identity, run_id, run_attempt, ci_revision):
    if sorted(path.name for path in staging.iterdir()) != sorted((REPORT, PROVENANCE)):
        raise ValueError("staging artifact must contain exactly the two coverage files")
    report_path, provenance_path = staging / REPORT, staging / PROVENANCE
    if any(path.is_symlink() or not path.is_file() for path in (report_path, provenance_path)):
        raise ValueError("coverage measurements must be regular files")
    if output.resolve().is_relative_to(root.resolve()):
        raise ValueError("measurements must be outside the frozen source checkout")
    sizes = [path.stat().st_size for path in (report_path, provenance_path)]
    if any(not 0 < size <= MAX_FILE_BYTES for size in sizes) or sum(sizes) > LIMIT - ZIP_RESERVE:
        raise ValueError("coverage measurement exceeds its size limit")
    report_bytes, provenance_bytes = report_path.read_bytes(), provenance_path.read_bytes()
    report, provenance = [json.loads(
        data, object_pairs_hook=unique_object, parse_constant=invalid_constant,
    ) for data in (report_bytes, provenance_bytes)]
    if not isinstance(report, dict) or not isinstance(provenance, dict):
        raise ValueError("coverage measurements must be JSON objects")
    files = report.get("files")
    if not isinstance(files, dict) or not files:
        raise ValueError("coverage report has no source file map")
    tracked = set(subprocess.check_output(
        ["git", "ls-files", "-z"], cwd=root, text=True,
    ).split("\0"))
    for filename in files:
        path = PurePosixPath(filename)
        if path.is_absolute() or ".." in path.parts or filename not in tracked:
            raise ValueError("coverage report includes a non-source path")
    report_hash = hashlib.sha256(report_bytes).hexdigest()
    if (provenance.get("schema_version") != 1
            or provenance.get("head_sha") != identity["source_sha"]
            or provenance.get("base_sha") != identity["base_sha"]
            or provenance.get("report_path") != REPORT
            or provenance.get("report_sha256") != report_hash):
        raise ValueError("coverage provenance does not match the exact source measurement")
    result = {
        "schema": "public-source-measurement-v1",
        **identity,
        "run_id": str(int(run_id)),
        "run_attempt": int(run_attempt),
        "ci_revision": ci_revision,
        "sha256": {
            REPORT: report_hash,
            PROVENANCE: hashlib.sha256(provenance_bytes).hexdigest(),
        },
    }
    if int(result["run_id"]) <= 0 or result["run_attempt"] <= 0:
        raise ValueError("measurement requires a positive workflow run and attempt")
    manifest_bytes = (json.dumps(result, indent=2, sort_keys=True) + "\n").encode()
    if sum(sizes) + len(manifest_bytes) > LIMIT - ZIP_RESERVE:
        raise ValueError("measurement envelope exceeds its archive size limit")
    output.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(report_path, output / REPORT)
    shutil.copyfile(provenance_path, output / PROVENANCE)
    (output / "measurement.json").write_bytes(manifest_bytes)
    return result


if __name__ == "__main__":
    export_measurement(
        Path(os.environ["SOURCE_ROOT"]), Path(sys.argv[1]), Path(sys.argv[2]),
        json.loads((Path(os.environ["RUNNER_TEMP"]) / "source-identity.json").read_text()),
        os.environ["GITHUB_RUN_ID"], os.environ["GITHUB_RUN_ATTEMPT"], os.environ["CI_REVISION"],
    )
