#!/usr/bin/env bash
# Keep preparation and current-base coverage checks outside reusable execution.

public_common_test_run() {
  local scope="$1" coverage_file="$2"
  shift 2
  local source_repository="${SOURCE_REPOSITORY:-${GITHUB_REPOSITORY:-}}" evidence=false
  if [[ -n "${COMMON_TEST_EVIDENCE_DIRECTORY:-}" && "${COMMON_TEST_EVIDENCE_SHA256:-}" =~ ^[0-9a-f]{64}$ ]]; then
    evidence=true
  fi
  if [[ "${COMMON_TEST_EQUIVALENCE:-false}" != true || -z "${COMMON_TEST_STEP:-}" ||
        ( -z "${GH_TOKEN:-}" && "$evidence" != true ) ||
        ( "$source_repository" != "${GITHUB_REPOSITORY:-$source_repository}" && "$evidence" != true ) ]]; then
    (unset GH_TOKEN GITHUB_TOKEN SOURCE_TOKEN; "$@")
    return
  fi
  local engine_python="${COMMON_TEST_PYTHON_BIN:-${python_bin:-python}}"
  local -a arguments=(
    --source-root "$SOURCE_ROOT" --source-repository "$source_repository"
    --recipe-root "$CI_ROOT" --recipe-directory scripts
    --recipe-file ".github/workflows/${scope%%-*}.yml"
    --scope "$scope" --collection tests --step "$COMMON_TEST_STEP"
    --measurements "$RUNNER_TEMP/common-test-measurements"
    --toolchain-command bash '["bash","--version"]'
    --toolchain-command uv '["uv","--no-config","--version"]'
  )
  if [[ "$evidence" == true ]]; then
    arguments+=(--evidence-directory "$COMMON_TEST_EVIDENCE_DIRECTORY" --evidence-sha256 "$COMMON_TEST_EVIDENCE_SHA256")
  fi
  if [[ -n "${SOURCE_COMMON_TEST_AUTHORITY_PATH:-}" ]]; then
    arguments+=(--source-authority "$SOURCE_COMMON_TEST_AUTHORITY_PATH"
                --source-authority-sha256 "${SOURCE_COMMON_TEST_AUTHORITY_SHA256:-}")
  fi
  if [[ "$coverage_file" != - ]]; then
    arguments+=(--data-root "$(dirname "$coverage_file")" --output-file "$(basename "$coverage_file")")
  else
    arguments+=(--data-root "$SOURCE_ROOT")
  fi
  if [[ "$1" == timeout ]]; then
    arguments+=(--toolchain-command timeout '["timeout","--version"]')
  fi
  local context binary
  for context in CI GITHUB_ACTIONS PYTEST_ADDOPTS PYTEST_BOORST PYTHONPATH PYTHONHASHSEED COVERAGE_FILE CI_SHARD \
    CAPACITY_TEST_PATHS HLTHPRT_DB_DRIVER HLTHPRT_DB_HOST HLTHPRT_DB_PORT HLTHPRT_DB_USER \
    HLTHPRT_DB_DATABASE HLTHPRT_DB_SCHEMA DB_SCHEMA HLTHPRT_ENVIRONMENT; do
    arguments+=(--environment "$context")
  done
  while IFS= read -r -d '' binary; do
    arguments+=(--runtime-file "${binary#"$SOURCE_ROOT/"}")
  done < <(find "$SOURCE_ROOT" \( -type f -o -type l \) \( -name '*.so' -o -name '*.pyd' \) -print0)
  for binary in ptg2_scanner ptg2_provider_graph_v4 raw_claim_check uhc_semantic_facts; do
    if [[ -f "$SOURCE_ROOT/support/ptg2_scanner/target/debug/$binary" ]]; then
      arguments+=(--runtime-file "support/ptg2_scanner/target/debug/$binary")
    fi
  done
  if [[ "$scope" == *-postgres ]]; then
    local probe
    probe=$("$engine_python" - <<'PY'
import json
import sys

print(json.dumps([sys.executable, "-c", '''import asyncio, json, os, re, subprocess
import asyncpg

image = subprocess.check_output([
    "docker", "inspect", "--format", "{{.Image}}", os.environ["COMMON_TEST_POSTGRES_CONTAINER"]
], text=True).strip()
assert re.fullmatch(r"sha256:[0-9a-f]{64}", image), "PostgreSQL image is not immutable"

async def main():
    connection = await asyncpg.connect(
        host=os.environ["HLTHPRT_DB_HOST"], port=int(os.environ["HLTHPRT_DB_PORT"]),
        user=os.environ["HLTHPRT_DB_USER"], password=os.environ["HLTHPRT_DB_PASSWORD"],
        database=os.environ["HLTHPRT_DB_DATABASE"],
    )
    try:
        async with connection.transaction(readonly=True):
            version = await connection.fetchval("SELECT version()")
            settings = await connection.fetch(
                "SELECT name, setting FROM pg_settings WHERE name=ANY($1::text[]) ORDER BY name",
                ["server_encoding", "lc_collate", "lc_ctype", "TimeZone", "standard_conforming_strings"],
            )
            extensions = await connection.fetch("SELECT extname, extversion FROM pg_extension ORDER BY extname")
            collation = await connection.fetchrow(
                "SELECT datcollate, datctype FROM pg_database WHERE datname=current_database()"
            )
        print(json.dumps({"image": image, "version": version,
                          "settings": [dict(row) for row in settings],
                          "collation": dict(collation),
                          "extensions": [dict(row) for row in extensions]}, sort_keys=True))
    finally:
        await connection.close()

asyncio.run(main())''']))
PY
)
    arguments+=(--toolchain-command postgres "$probe")
  fi
  "$engine_python" "$CI_ROOT/scripts/common_test_result.py" "${arguments[@]}" -- "$@"
}

public_common_test_provenance() {
  local provenance="$1" scope="$2"
  [[ "${COMMON_TEST_EQUIVALENCE:-false}" == true && -n "${RUNNER_TEMP:-}" ]] || return 0
  "${COMMON_TEST_PYTHON_BIN:-${python_bin:-python}}" - "$provenance" "$scope" "$RUNNER_TEMP" <<'PY'
import hashlib
import json
from pathlib import Path
import sys

provenance, scope, temporary = sys.argv[1:]
measurement = Path(temporary) / "common-test-measurements" / hashlib.sha256(scope.encode()).hexdigest() / "measurement.json"
if measurement.exists():
    data = measurement.read_bytes()
    document = json.loads(data)
    current = document["measurement"]
    target = Path(provenance)
    value = json.loads(target.read_bytes())
    assert value["head_sha"] == current["source"]["commit"], "Coverage belongs to a different source execution"
    assert "common_test_execution" not in value, "Coverage execution reference already exists"
    reference = {name: current[name] for name in ("schema", "source", "producer", "fingerprint", "executed")}
    reference["measurement_sha256"] = hashlib.sha256(data).hexdigest()
    if document["origin"] is not None:
        proof = document["origin"]
        reference["origin"] = {name: proof["origin"][name] for name in ("source", "producer", "fingerprint")}
        reference["origin"].update(artifact_id=proof["artifact_id"], archive_sha256=proof["archive_sha256"])
    value["common_test_execution"] = reference
    target.write_text(json.dumps(value, sort_keys=True) + "\n")
PY
}
