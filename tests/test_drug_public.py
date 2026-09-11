"""Public source identity and measurement fail closed without private inputs."""

import copy
import hashlib
import importlib.util
import json
import os
import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


IDENTITY = load("source_identity", "scripts/source_identity.py")
POLICY = load("coverage_policy", "scripts/coverage_policy.py")
MEASUREMENT = load("drug_measurement", "scripts/drug/measurement.py")
NATIVE_TESTS = load("drug_native_tests", "scripts/drug/native_tests.py")
SOURCE, BASE, PIN = "1" * 40, "2" * 40, "3" * 40
REPOSITORY = "EndurantDevs/drug-api"


class DrugNativeSelectionTests(unittest.TestCase):
    def test_legacy_and_complete_source_preserve_existing_native_inventory(self):
        expected = (
            "tests/process/test_import_table_switching.py",
            "tests/process/test_ndc_rxnorm_mapping.py",
            "tests/process/test_drug_indications.py",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(NATIVE_TESTS.postgres_test_paths(root), expected)
            for name in NATIVE_TESTS.NDC_SOURCE_SET:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("raise AssertionError('source files must not execute during selection')\n")
            self.assertEqual(NATIVE_TESTS.postgres_test_paths(root), (
                *expected, "tests/process/test_ndc_publication_proof_postgres.py",
            ))

    def test_every_partial_publication_source_set_is_rejected(self):
        for present in range(1, 7):
            with self.subTest(present=present), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                for index, name in enumerate(NATIVE_TESTS.NDC_SOURCE_SET):
                    if present & (1 << index):
                        path = root / name
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.touch()
                with self.assertRaisesRegex(ValueError, "requires its native proof"):
                    NATIVE_TESTS.postgres_test_paths(root)

    def test_each_nonregular_publication_source_path_is_rejected(self):
        for name in NATIVE_TESTS.NDC_SOURCE_SET:
            for kind in ("directory", "symlink", "dangling-symlink"):
                with self.subTest(name=name, kind=kind), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    for candidate in NATIVE_TESTS.NDC_SOURCE_SET:
                        path = root / candidate
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.touch()
                    invalid = root / name
                    invalid.unlink()
                    if kind == "directory":
                        invalid.mkdir()
                    else:
                        target = root / "synthetic-source.py"
                        if kind == "symlink":
                            target.touch()
                        invalid.symlink_to(target)
                    with self.assertRaisesRegex(ValueError, "regular files"):
                        NATIVE_TESTS.postgres_test_paths(root)

    def test_gate_checks_selector_failure_before_migrations(self):
        gate = (ROOT / "scripts/drug/check").read_text()
        postgres = gate.split("\npostgres18() {", 1)[1].split("\n}\n", 1)[0]
        selection = '    selection="$("$python_bin" "$CI_ROOT/scripts/drug/native_tests.py")"'
        self.assertIn("\nset -euo pipefail\n", gate)
        self.assertIn("\n" + selection + "\n", postgres)
        self.assertLess(postgres.index(selection), postgres.index("mapfile -t postgres_tests"))
        self.assertLess(postgres.index("mapfile -t postgres_tests"), postgres.index("-m alembic upgrade head"))
        self.assertIn('"$python_bin" -m pytest -q "${postgres_tests[@]}"', postgres)
        self.assertNotIn("<(", postgres)


class PublicDrugTests(unittest.TestCase):
    def test_validation_uses_pinned_uv_and_lockfile_provenance(self):
        gate = (ROOT / "scripts/drug/check").read_text()
        workflow = (ROOT / ".github/workflows/drug.yml").read_text()
        self.assertIn("readonly UV_VERSION='0.12.12'", gate)
        self.assertIn("uv --no-config sync --locked --all-groups --python \"$python_bin\" --no-build", gate)
        self.assertIn("uv --no-config export --quiet --locked --all-groups", gate)
        self.assertIn("uv_lock_sha256=", gate)
        self.assertNotIn("-m pip install", gate)
        self.assertIn("actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97", workflow)
        self.assertIn("--only-binary=:all: --require-hashes -r /dev/stdin", workflow)
        self.assertIn(
            "uv==0.12.12 --hash=sha256:fa5df02fc619a3cc7a58810d6ffeb80c"
            "a1e01404b8ef7239bd1cf2103c02cacf",
            workflow,
        )
        self.assertNotIn("astral-sh/setup-uv@", workflow)

    def test_cleanup_verifies_absence_and_preserves_original_failure(self):
        source = (ROOT / "scripts/drug/check").read_text()
        functions = source[source.index("resource_suffix="):source.index("require_coverage_protocol()")]
        stub = r'''
python_bin=python3
ci_phase_end() { :; }
sleep() { [ "$*" = 1 ]; }
docker() {
  printf '%s\n' "$*" >> "$STUB_ROOT/calls"
  case "$1 ${2:-}" in
    'container ls')
      [ "$LIST_STATUS" = 0 ] || return "$LIST_STATUS"
      name=${5#name=^/}; name=${name%\$}
      [ ! -f "$STUB_ROOT/$name" ] || printf '%s\n' "$name"
      ;;
    'rm --force')
      [ "$REMOVE_STATUS" = 0 ] || return "$REMOVE_STATUS"
      for name in "${@:3}"; do rm -f "$STUB_ROOT/$name"; done
      ;;
    'image ls')
      [ "$LIST_STATUS" = 0 ] || return "$LIST_STATUS"
      [ ! -f "$STUB_ROOT/image" ] || printf 'sha256:synthetic\n'
      ;;
    'image rm')
      [ "$REMOVE_STATUS" = 0 ] || return "$REMOVE_STATUS"
      rm "$STUB_ROOT/image"
      ;;
    *) return 99 ;;
  esac
  return 0
}
'''
        for status, remove, listing, required, expected in (
            (0, 0, 0, 1, 0), (17, 0, 0, 1, 17), (0, 23, 0, 1, 1),
            (17, 23, 0, 1, 17), (0, 0, 29, 1, 1), (17, 0, 29, 1, 17),
            (0, 0, 29, 0, 0),
        ):
            with self.subTest(status=status, remove=remove, listing=listing, required=required):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    env = {**os.environ, "STUB_ROOT": directory, "REMOVE_STATUS": str(remove),
                           "LIST_STATUS": str(listing)}
                    script = stub + functions + (
                        '\ntouch "$STUB_ROOT/$postgres_name" "$STUB_ROOT/image" "$STUB_ROOT/unrelated"\n'
                        f'cleanup_required={required}\nexit {status}\n'
                    )
                    result = subprocess.run(["bash", "-euc", script], env=env,
                                            capture_output=True, text=True)
                    self.assertEqual(result.returncode, expected, result.stderr)
                    self.assertTrue((root / "unrelated").exists())
                    leftovers = list(root.glob("drug-ci-postgres-*"))
                    self.assertEqual(bool(leftovers), bool(remove or listing or not required))
                    self.assertEqual((root / "image").exists(), bool(remove or listing or not required))
                    if not required:
                        self.assertFalse((root / "calls").exists())

    def test_service_cleanup_waits_for_auto_removal_without_retrying_removal(self):
        source = (ROOT / "scripts/drug/check").read_text()
        resources = source[source.index("resource_suffix="):source.index("require_coverage_protocol()")]
        services = source[source.index("local_services()"):source.index("write_receipt()")]
        stub = r'''
python_bin=python3
ci_phase_end() { :; }
POSTGRES_IMAGE=postgres
REDIS_IMAGE=redis
postgres18() { :; }; redis() { :; }
sleep() { [ "$*" = 1 ]; printf '%s\n' "$*" >> "$STUB_ROOT/sleeps"; }
docker() {
  case "$1 ${2:-}" in
    'run --detach')
      while [ "$1" != --name ]; do shift; done
      touch "$STUB_ROOT/$2"
      ;;
    exec*) ;;
    port*) printf '127.0.0.1:1234\n' ;;
    'container ls')
      name=${5#name=^/}; name=${name%\$}
      if [ -f "$STUB_ROOT/$name.removing" ]; then
        checks=$(cat "$STUB_ROOT/$name.removing")
        checks=$((checks + 1))
        printf '%s\n' "$checks" > "$STUB_ROOT/$name.removing"
        [ "$LIST_STATUS" = 0 ] || return "$LIST_STATUS"
        [ "$checks" -lt "$SETTLE_CHECK" ] || rm -f "$STUB_ROOT/$name"
      fi
      [ ! -f "$STUB_ROOT/$name" ] || printf '%s\n' "$name"
      ;;
    'rm --force')
      printf '%s\n' "$*" >> "$STUB_ROOT/removals"
      [ "$#" = 3 ] || return 99
      printf '0\n' > "$STUB_ROOT/$3.removing"
      return 23
      ;;
    'image ls') ;;
    *) return 99 ;;
  esac
  return 0
}
'''
        for status in (0, 17):
            for settle, listing in ((1, 0), (3, 0), (4, 0), (3, 29)):
                with self.subTest(status=status, settle=settle, listing=listing):
                    with tempfile.TemporaryDirectory() as directory:
                        root = Path(directory)
                        env = {**os.environ, "STUB_ROOT": directory, "SETTLE_CHECK": str(settle),
                               "LIST_STATUS": str(listing)}
                        script = stub + resources + services + (
                            '\nlocal_services\n' + ('cleanup\n' if status == 0 else '') + f'exit {status}\n'
                        )
                        result = subprocess.run(["bash", "-euc", script], env=env,
                                                capture_output=True, text=True)
                        incomplete = bool(listing or settle > 3)
                        self.assertEqual(result.returncode, status or int(incomplete), result.stderr)
                        removals = (root / "removals").read_text().splitlines()
                        self.assertEqual(len(removals), 2)
                        self.assertEqual(len(set(removals)), 2)
                        for line in removals:
                            name = line.removeprefix("rm --force ")
                            self.assertEqual((root / name).exists(), incomplete)
                            self.assertEqual(int((root / f"{name}.removing").read_text()),
                                             1 if listing else min(settle, 3))
                        sleep_log = root / "sleeps"
                        sleeps = len(sleep_log.read_text().splitlines()) if sleep_log.exists() else 0
                        self.assertEqual(sleeps, 0 if listing else 2 * (min(settle, 3) - 1))

    def test_source_coverage_contract_rejects_downgrade_and_non_boolean(self):
        self.assertEqual(POLICY.drug_coverage_protocol({}, {}), "legacy")
        self.assertEqual(POLICY.drug_coverage_protocol({}, {"machine_artifact_required": True}), "machine")
        for base, source in (({"machine_artifact_required": True}, {}), ({}, {"machine_artifact_required": 1})):
            with self.subTest(base=base, source=source), self.assertRaises(ValueError):
                POLICY.drug_coverage_protocol(base, source)

    def test_pull_request_uses_exact_head_base_and_live_title(self):
        pr = {"number": 7, "state": "open", "title": "ci: validate public source",
              "base": {"ref": "main", "sha": BASE, "repo": {"full_name": REPOSITORY}},
              "head": {"sha": SOURCE}}
        payload = {"repository": {"full_name": REPOSITORY, "private": False}, "pull_request": copy.deepcopy(pr)}
        with patch.object(IDENTITY, "api", return_value=pr):
            result = IDENTITY.resolve_identity(REPOSITORY, "pull_request", payload, "4" * 40, "refs/pull/7/merge")
            self.assertEqual(result["source_sha"], SOURCE)
            self.assertEqual(result["base_sha"], BASE)
            self.assertEqual(result["pr_title"], pr["title"])
            for field, value in (("title", "ci: change the accepted title"), ("state", "closed")):
                changed = copy.deepcopy(pr)
                changed[field] = value
                with self.subTest(field=field), patch.object(IDENTITY, "api", return_value=changed):
                    with self.assertRaises(ValueError):
                        IDENTITY.resolve_identity(REPOSITORY, "pull_request", payload, SOURCE, "refs/pull/7/merge")

    def test_push_range_uses_complete_rebased_pull_request(self):
        intermediate, original, tree = "4" * 40, "5" * 40, "6" * 40
        commit = {"sha": SOURCE, "parents": [{"sha": intermediate}], "commit": {"tree": {"sha": tree}}}
        pr = {"number": 7, "merged": True, "state": "closed", "merge_commit_sha": SOURCE,
              "base": {"ref": "main", "sha": BASE, "repo": {"full_name": REPOSITORY}},
              "head": {"sha": original}, "commits": 2}
        commits = [{"sha": intermediate, "parents": [{"sha": BASE}]}, commit]
        comparison = {"status": "ahead", "behind_by": 0, "merge_base_commit": {"sha": BASE},
                      "total_commits": 2, "ahead_by": 2, "commits": commits}
        responses = {f"commits/{SOURCE}": commit, "pulls/7": pr, f"compare/{BASE}...{SOURCE}": comparison,
                     f"git/commits/{original}": {"sha": original, "tree": {"sha": tree}}}
        payload = {"repository": {"full_name": REPOSITORY, "private": False}, "ref": "refs/heads/main", "after": SOURCE,
                   "before": intermediate}
        associated = [{**pr, "merged_at": "2026-01-01T00:00:00Z"}]
        with patch.object(IDENTITY, "api", side_effect=lambda repo, path: responses[path]):
            with patch.object(IDENTITY, "pages", return_value=associated):
                result = IDENTITY.resolve_identity(REPOSITORY, "push", payload, SOURCE, "refs/heads/main")
                self.assertEqual(result["base_sha"], BASE)
                comparison["commits"] = [commit]
                with self.assertRaises(ValueError):
                    IDENTITY.resolve_identity(REPOSITORY, "push", payload, SOURCE, "refs/heads/main")

    def test_rejects_other_repository_and_manual_event(self):
        for repository, event in (("example/private", "push"), (REPOSITORY, "workflow_dispatch")):
            with self.subTest(repository=repository, event=event), self.assertRaises(ValueError):
                IDENTITY.resolve_identity(repository, event, {"repository": {"full_name": repository, "private": False}}, SOURCE, "refs/heads/main")
        with self.assertRaises(ValueError):
            IDENTITY.resolve_identity(REPOSITORY, "push", {"repository": {"full_name": REPOSITORY, "private": True}}, SOURCE, "refs/heads/main")

    def test_public_gate_retains_existing_execution_lanes(self):
        gate = (ROOT / "scripts/drug/check").read_text()
        self.assertIn("-m ruff check --no-cache --select E9,F api db process scripts tests main.py", gate)
        self.assertIn("-m ruff check --no-cache --select I api db process tests main.py", gate)
        self.assertIn("-m pylint --errors-only api db process main.py", gate)
        self.assertIn("-m pylint --source-roots=. --errors-only --disable=no-member tests", gate)
        self.assertNotIn("-m flake8", gate)
        self.assertNotIn("-m isort", gate)
        self.assertIn('scripts/python_format.py" "$(base_sha)"', gate)
        measure = gate.split("\nmeasure() {", 1)[1].split("\ncase ", 1)[0]
        for command in ("require_frozen_candidate", "install", "require_python", "public_hygiene", "quality",
                        "test_run", "coverage_report", "coverage_provenance", "coverage_measurement",
                        "wait_security", "runtime_image", "local_services", "write_receipt"):
            self.assertIn(f"\n    {command}", measure)
        self.assertIn("\n        security\n", measure)
        self.assertIn('scripts/coverage_ratchet.py --self-test', gate)
        self.assertNotIn('coverage_forecast.py forecast', gate)

    def test_security_overlaps_checks_and_joins_before_cleanup_on_every_failure(self):
        gate = (ROOT / "scripts/drug/check").read_text()
        functions = gate[gate.index("resource_suffix="):gate.index('\ncase "$mode" in')]
        stub = r'''
record() { printf '%s\n' "$1" >> "$STUB_ROOT/events"; }
wait_file() {
  for attempt in {1..500}; do
    [ ! -f "$STUB_ROOT/$1" ] || return 0
    sleep 0.01
  done
  return 99
}
git() { printf '%040d\n' 1; }
prepare_artifact_root() { artifact_root="$STUB_ROOT"; }
require_frozen_candidate() { :; }
install() { :; }; require_python() { :; }; public_hygiene() { :; }
source "$TIMING_HELPER"
security() {
  trap 'status=$?; touch "$STUB_ROOT/security-ended"; ci_phase_end "$status"' EXIT
  record security-start
  touch "$STUB_ROOT/security-started"
  wait_file quality-started
  sleep 0.03
  bash -c 'exit "$SECURITY_STATUS"'
  record security-passed
}
quality() {
  wait_file security-started
  record quality-start
  touch "$STUB_ROOT/quality-started"
  bash -c 'exit "$QUALITY_STATUS"'
  record quality-passed
}
test_run() { record tests; bash -c 'exit "$TEST_STATUS"'; }
coverage_report() { record coverage-report; }
coverage_provenance() { record coverage-provenance; }
coverage_measurement() { record coverage-measurement; }
runtime_image() { [ -f "$STUB_ROOT/security-ended" ]; record image; }
local_services() { record services; }
cleanup() { [ -f "$STUB_ROOT/security-ended" ]; record cleanup; }
write_receipt() { record receipt; }
measure
'''
        for quality, security, tests, expected in ((0, 0, 0, 0), (0, 17, 0, 17), (23, 0, 0, 23),
                                                   (23, 17, 0, 23), (0, 0, 29, 29)):
            with self.subTest(quality=quality, security=security, tests=tests):
                with tempfile.TemporaryDirectory() as directory:
                    environment = {**os.environ, "STUB_ROOT": directory, "BASE_SHA": "1" * 40,
                                   "TIMING_HELPER": str(ROOT / "scripts/phase_timing.sh"),
                                   "QUALITY_STATUS": str(quality), "SECURITY_STATUS": str(security),
                                   "TEST_STATUS": str(tests)}
                    result = subprocess.run(["bash", "-euc", "python_bin=python3\n" + functions + stub],
                                            env=environment, capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, expected, result.stderr)
                    self.assertRegex(result.stdout,
                                     rf"CI_PHASE end name=security elapsed_seconds=\d+ exit_code={security}")
                    self.assertRegex(result.stdout,
                                     rf"CI_PHASE end name=quality elapsed_seconds=\d+ exit_code={quality}")
                    events = (Path(directory) / "events").read_text().splitlines()
                    self.assertIn("cleanup", events)
                    self.assertEqual("security-passed" in events, security == 0)
                    self.assertEqual("quality-passed" in events, quality == 0)
                    self.assertEqual("receipt" in events, expected == 0)
                    if expected == 0:
                        for phase in ("tests", "coverage-report", "coverage-provenance",
                                      "coverage-measurement", "image", "services"):
                            self.assertIn(phase, events)
                    else:
                        self.assertNotIn("image", events)

    def test_publisher_emits_exact_hashed_data_and_rejects_forgery(self):
        identity = {"repository": REPOSITORY, "source_sha": SOURCE, "base_sha": BASE,
                    "source_branch": "main", "pr_number": "0", "pr_title": ""}
        with tempfile.TemporaryDirectory() as raw:
            root, staging, output = (Path(raw) / part for part in ("source", "stage", "output"))
            root.mkdir()
            staging.mkdir()
            report = {"meta": {"version": "7"}, "files": {"main.py": {"executed_lines": [1]}}}
            report_bytes = json.dumps(report).encode()
            (staging / MEASUREMENT.REPORT).write_bytes(report_bytes)
            provenance = {"schema_version": 1, "head_sha": SOURCE, "base_sha": BASE,
                          "report_path": MEASUREMENT.REPORT,
                          "report_sha256": hashlib.sha256(report_bytes).hexdigest(), "coverage_version": "7"}
            (staging / MEASUREMENT.PROVENANCE).write_text(json.dumps(provenance))
            with patch.object(MEASUREMENT.subprocess, "check_output", return_value="main.py\0"):
                result = MEASUREMENT.export_measurement(root, staging, output, identity, "123", "2", PIN)
                self.assertEqual(result["schema"], "public-source-measurement-v1")
                self.assertEqual(result["run_attempt"], 2)
                self.assertEqual(result["sha256"][MEASUREMENT.REPORT], hashlib.sha256(report_bytes).hexdigest())
                self.assertEqual(sorted(path.name for path in output.iterdir()),
                                 sorted((MEASUREMENT.REPORT, MEASUREMENT.PROVENANCE, "measurement.json")))
                with patch.object(MEASUREMENT, "MAX_FILE_BYTES", len(report_bytes) - 1):
                    with self.assertRaises(ValueError):
                        MEASUREMENT.export_measurement(root, staging, output, identity, "123", "2", PIN)
                with patch.object(MEASUREMENT, "LIMIT", sum(path.stat().st_size for path in staging.iterdir()) + MEASUREMENT.ZIP_RESERVE):
                    with self.assertRaises(ValueError):
                        MEASUREMENT.export_measurement(root, staging, output, identity, "123", "2", PIN)
                provenance["base_sha"] = PIN
                (staging / MEASUREMENT.PROVENANCE).write_text(json.dumps(provenance))
                with self.assertRaises(ValueError):
                    MEASUREMENT.export_measurement(root, staging, output, identity, "123", "2", PIN)
                (staging / "unexpected.sh").write_text("exit 0\n")
                with self.assertRaises(ValueError):
                    MEASUREMENT.export_measurement(root, staging, output, identity, "123", "2", PIN)

    def test_measurement_json_rejects_duplicate_fields_and_non_json_constants(self):
        with self.assertRaises(ValueError):
            json.loads('{"files":{},"files":{}}', object_pairs_hook=MEASUREMENT.unique_object)
        with self.assertRaises(ValueError):
            json.loads('{"metric":NaN}', parse_constant=MEASUREMENT.invalid_constant)


if __name__ == "__main__":
    unittest.main()
