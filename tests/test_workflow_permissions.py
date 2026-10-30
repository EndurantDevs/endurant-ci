"""Keep shared public validation on read-only hosted execution."""
import pathlib
import shlex
import unittest
from unittest.mock import patch

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
COMPOSITE = "./ci/scripts/healthcare/setup"
COMPOSITE_PATH = ROOT / "scripts/healthcare/setup/action.yml"


class PublicWorkflowPermissions(unittest.TestCase):
    def check_steps(self, steps, *, composite=False):
        for index, step in enumerate(steps):
            run = step.get("run", "")
            self.assertNotRegex(run, r"\bpython3?\s+(?:-\S+\s+)*-m\s+pip\b")
            if "uses" not in step:
                continue
            action = step["uses"]
            if not action.startswith("./"):
                self.assertRegex(action, r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+@[0-9a-f]{40}$")
                self.assertFalse(action.startswith("astral-sh/setup-uv@"))
                self.assertFalse(action.startswith("actions/setup-python@"))
                if action.startswith("actions/checkout@"):
                    self.assertEqual(step.get("with", {}).get("persist-credentials"), "false")
                continue
            self.assertFalse(composite, "nested local composite actions are not approved")
            self.assertEqual(action, COMPOSITE)
            # The local action must come from the pinned shared package checkout.
            checkouts = [previous for previous in steps[:index]
                         if previous.get("uses", "").startswith("actions/checkout@")
                         and previous.get("with", {}).get("path") == "ci"]
            self.assertEqual(len(checkouts), 1)
            self.assertEqual(checkouts[0]["with"], {
                "repository": "EndurantDevs/endurant-ci", "ref": "${{ inputs.ci_revision }}",
                "path": "ci", "persist-credentials": "false",
            })
            text = COMPOSITE_PATH.read_text()
            self.assertNotRegex(text, r"\bsecrets[.\[]")
            definition = yaml.load(text, Loader=yaml.BaseLoader)
            self.assertEqual(definition["runs"]["using"], "composite")
            self.check_steps(definition["runs"]["steps"], composite=True)

    def test_workflow_permissions(self):
        for path in sorted((ROOT / ".github/workflows").glob("*.yml")):
            with self.subTest(workflow=path.name):
                workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
                recovery = path.name == "metadata-recovery.yml"
                if recovery:
                    self.assertEqual(workflow["on"].keys(), {"workflow_call"})
                    self.assertEqual(workflow["permissions"], {
                        "contents": "read", "pull-requests": "read", "actions": "write",
                    })
                for permissions in [workflow.get("permissions", {})] + [job.get("permissions", {}) for job in workflow.get("jobs", {}).values()]:
                    self.assertIsInstance(permissions, dict)
                    self.assertTrue(all(value in ("read", "none") or
                                        recovery and key == "actions" and value == "write"
                                        for key, value in permissions.items()))
                for job in workflow.get("jobs", {}).values():
                    if "runs-on" in job:
                        self.assertEqual(job["runs-on"], "ubuntu-latest")
                    self.assertNotIn("secrets", job)
                    self.check_steps(job.get("steps", []))
                self.assertNotRegex(path.read_text(), r"\bsecrets[.\[]")

    def test_package_policy_keeps_verbose_complete_tests_and_proof_inputs(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
        job = workflow["jobs"]["validate"]
        self.assertEqual(job["timeout-minutes"], 30)
        self.assertEqual(job.get("permissions", workflow["permissions"]),
                         {"contents": "read", "actions": "read", "checks": "read"})
        step = next(step for step in job["steps"]
                    if step.get("name") == "Check public workflow and helper contracts")
        self.assertEqual(shlex.split(step["run"].replace("\\\n", "")), [
            "python", "scripts/common_test_result.py",
            "--source-root", ".", "--source-repository", "$GITHUB_REPOSITORY",
            "--recipe-root", ".", "--recipe-file", "@tracked",
            "--scope", "package-unit", "--step", "Check public workflow and helper contracts",
            "--environment", "PUBLIC_CI_DOCKER_COMPATIBILITY",
            "--evidence-directory", "${COMMON_TEST_EVIDENCE_DIRECTORY:-$RUNNER_TEMP/common-test-evidence}",
            "--evidence-sha256", "${COMMON_TEST_EVIDENCE_SHA256:-}",
            "--measurements", "$RUNNER_TEMP/common-test-measurements", "--", "bash", "-euc",
            "python -m unittest discover -s tests -p 'test_*.py' -v; python scripts/healthcare/test_measurement.py",
        ])
        self.assertIn(
            '--evidence-directory "${COMMON_TEST_EVIDENCE_DIRECTORY:-$RUNNER_TEMP/common-test-evidence}"',
            step["run"],
        )

    def test_unapproved_uv_requirement_is_rejected(self):
        with self.assertRaises(AssertionError):
            self.check_steps([{"uses": "astral-sh/setup-uv@" + "a" * 40,
                               "with": {"version": "0.12.12"}}])
        with self.assertRaises(AssertionError):
            self.check_steps([{"run": "python -m pip install example"}])

    def test_composite_requires_its_pinned_checkout_and_pinned_nested_actions(self):
        checkout = {"uses": "actions/checkout@" + "a" * 40, "with": {
            "repository": "EndurantDevs/endurant-ci", "ref": "${{ inputs.ci_revision }}",
            "path": "ci", "persist-credentials": "false",
        }}
        call = {"uses": COMPOSITE}
        for steps in ([call, checkout], [{**checkout, "with": {**checkout["with"], "ref": "main"}}, call]):
            with self.subTest(steps=steps), self.assertRaises(AssertionError):
                self.check_steps(steps)
        with patch.object(pathlib.Path, "read_text", return_value="runs:\n  using: composite\n  steps:\n    - uses: example/action@main\n"):
            with self.assertRaises(AssertionError):
                self.check_steps([checkout, call])

    def test_independent_database_checks_preserve_every_measurement_producer(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/healthcare.yml").read_text())
        jobs = workflow["jobs"]
        expected = {
            "python-tests": [(str(index), f"Python tests ({index + 1}/6)", f"artifact_{index}")
                             for index in range(6)],
            "address-canonical-db-tests": [(shard, f"Database tests ({label})", "artifact_" + shard.replace("-", "_"))
                                            for shard, label in (
                                                ("core-services", "services"), ("core-services-2", "services 2"), ("core-imports", "imports"),
                                                ("core-imports-2", "imports 2"), ("core-snapshots", "snapshot imports"),
                                                ("core-snapshots-2", "snapshot imports 2"),
                                                ("core-ptg", "PTG"),
                                                *((f"registry-{index}", f"registry {index + 1}") for index in range(48)),
                                                ("directory-source", "directory source"),
                                                ("directory-source-2", "directory source 2"),
                                                ("directory-storage", "directory storage"),
                                                ("directory-address", "directory address"),
                                                ("profile-storage", "profile storage"),
                                                ("profile-publication", "profile publication"))],
        }
        artifact_ids = []
        for job_id, rows in expected.items():
            job = jobs[job_id]
            if job_id == "address-canonical-db-tests":
                # Database lanes are capped and listed longest-first; the rows are pinned as a set.
                self.assertEqual(set(job["strategy"]), {"max-parallel", "fail-fast", "matrix"})
                self.assertEqual(job["strategy"]["max-parallel"], 48)
                self.assertEqual(job["strategy"]["fail-fast"], False)
                self.assertEqual(set(job["strategy"]["matrix"]), {"include"})
                self.assertEqual(sorted(job["strategy"]["matrix"]["include"], key=lambda row: row["shard"]),
                                 sorted(({"shard": shard, "label": label, "output": output} for shard, label, output in rows),
                                        key=lambda row: row["shard"]))
            else:
                self.assertEqual(job["strategy"], {"fail-fast": job_id == "python-tests", "matrix": {"include": [
                    {"shard": shard, "label": label, "output": output} for shard, label, output in rows]}})
            self.assertEqual(job["name"], "${{ matrix.label }}")
            self.assertEqual(job["needs"], ["public-hygiene", "readability-preflight"])
            self.assertEqual(job["env"]["CI_SHARD"], "${{ matrix.shard }}")
            self.assertNotIn("continue-on-error", job)
            self.assertEqual(job["outputs"], {output: "${{ steps.coverage-output.outputs." + output + " }}"
                                               for _, _, output in rows})
            emitters = [step for step in job["steps"] if step.get("id") == "coverage-output"]
            self.assertEqual(len(emitters), 1)
            emitter = emitters[0]
            self.assertEqual(emitter["id"], "coverage-output")
            self.assertEqual(emitter["env"], {"ARTIFACT_OUTPUT": "${{ matrix.output }}",
                                              "ARTIFACT_ID": "${{ steps.coverage-artifact.outputs.artifact-id }}"})
            self.assertIn('"$ARTIFACT_OUTPUT" "$ARTIFACT_ID" >> "$GITHUB_OUTPUT"', emitter["run"])
            commands = "\n".join(step.get("run", "") for step in job["steps"])
            self.assertIn(('python-main' if job_id == 'python-tests' else 'postgres') + ' "$CI_SHARD"', commands)
            self.assertIn(job_id, jobs["measurement"]["needs"])
            artifact_ids.extend("${{ needs." + job_id + ".outputs." + output + " }}" for _, _, output in rows)
        artifact_ids.extend(("${{ needs.capacity-evidence.outputs.artifact_id }}",
                             "${{ needs.rust-scanner.outputs.artifact_id }}"))
        download = next(step for step in jobs["measurement"]["steps"]
                        if step.get("name") == "Download immutable measurement artifacts")
        self.assertEqual(set(download["with"]["artifact-ids"].split(",")), set(artifact_ids))
        self.assertEqual(len(download["with"]["artifact-ids"].split(",")), 69)
        self.assertEqual(sum(len(job.get("strategy", {}).get("matrix", {}).get("include", [None]))
                             for job in jobs.values()), 79)
        self.assertEqual(jobs["python-tests"]["timeout-minutes"], 20)

    def test_cheap_lint_blocks_heavy_jobs_without_changing_measurement_producers(self):
        jobs = yaml.safe_load((ROOT / ".github/workflows/healthcare.yml").read_text())["jobs"]
        roots = ("public-hygiene", "capacity-evidence", "api-contract", "rust-scanner",
                 "container-package", "security", "worker-queue-smoke")
        for job in roots:
            self.assertIn("python-quality", jobs[job]["needs"], job)
            if job != "public-hygiene":
                self.assertIn("readability-preflight", jobs[job]["needs"], job)
        for job in ("python-tests", "address-canonical-db-tests"):
            self.assertIn("public-hygiene", jobs[job]["needs"])

    def test_check_read_is_scoped_to_active_common_test_consumers(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/healthcare.yml").read_text())
        base = {"contents": "read", "pull-requests": "read", "actions": "read"}
        self.assertEqual(workflow["permissions"], base)
        readers = {"python-tests", "capacity-evidence", "measurement"}
        for identifier, job in workflow["jobs"].items():
            with self.subTest(job=identifier):
                if identifier == "source-validation":
                    self.assertEqual(job["permissions"], {"actions": "read"})
                else:
                    self.assertEqual(job.get("permissions", base), {
                        **base, **({"checks": "read"} if identifier in readers else {}),
                    })
        steps = workflow["jobs"]["address-canonical-db-tests"]["steps"]
        names = [step.get("name") for step in steps]
        self.assertNotIn("Upload verified common-test measurements", names)
        self.assertIn("Upload PostgreSQL coverage data", names)
        self.assertIn("Expose immutable coverage artifact", names)

    def test_database_lanes_reuse_prebuilt_native_artifacts_and_keep_job_limits(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/healthcare.yml").read_text())
        job = workflow["jobs"]["address-canonical-db-tests"]
        self.assertEqual(job["timeout-minutes"], 20)
        self.assertEqual(job["strategy"]["fail-fast"], False)
        setup = next(step for step in job["steps"] if step.get("name") == "Prepare source and toolchain")
        # Database lanes consume the hygiene job's debug binaries and native wheel; no Rust toolchain.
        self.assertNotIn("with", setup)
        hygiene = workflow["jobs"]["public-hygiene"]
        hygiene_setup = next(step for step in hygiene["steps"] if step.get("name") == "Prepare source and toolchain")
        self.assertEqual(hygiene_setup["with"], {"rust": "true", "cargo-cache": "hygiene"})
        staging = next(step for step in hygiene["steps"] if step.get("name") == "Stage shared Rust test binaries")
        self.assertIn('check" native-wheel "$binary_directory"', staging["run"])
        upload = next(step for step in hygiene["steps"] if step.get("name") == "Upload exact-source Rust test binaries")
        self.assertIn("rust-debug-binaries/*.whl", upload["with"]["path"].split("\n"))
        self.assertIn("rust-debug-binaries/ptg2-address-canon-wheel.sha256", upload["with"]["path"].split("\n"))
        rust_setup = next(step for step in workflow["jobs"]["rust-scanner"]["steps"]
                          if step.get("name") == "Prepare source and toolchain")
        self.assertEqual(rust_setup["with"], {"rust": "true", "cargo-cache": "rust"})
        for job_id in ("python-tests", "address-canonical-db-tests"):
            download = next(step for step in workflow["jobs"][job_id]["steps"]
                            if step.get("name") == "Download exact-source Rust test binaries")
            self.assertEqual(download["with"]["artifact-ids"],
                             "${{ needs.public-hygiene.outputs.rust_artifact_id || 'missing' }}", job_id)
        execution = next(step for step in job["steps"] if step.get("name") == "Run complete validation stage")
        self.assertEqual(execution["env"]["CI_RUST_BINARIES"], "${{ github.workspace }}/rust-debug-binaries")
        self.assertIn('postgres "$CI_SHARD"', execution["run"])

    def test_native_archive_clients_match_the_postgresql_service_major(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/healthcare.yml").read_text())
        database_job = workflow["jobs"]["address-canonical-db-tests"]
        self.assertIn("postgis/postgis:18-", database_job["services"]["postgres"]["image"])
        installer = next(step for step in database_job["steps"] if step.get("name") == "Install PostgreSQL client")
        self.assertEqual(installer["run"], 'bash "$CI_ROOT/scripts/install_postgres_client"')
        command = (ROOT / "scripts/install_postgres_client").read_text()
        self.assertIn("https://apt.postgresql.org/pub/repos/apt", command)
        self.assertIn("Acquire::Retries=3", command)
        self.assertIn("--retry-all-errors", command)
        self.assertIn("postgresql-client-18", command)
        self.assertIn('>> "$GITHUB_PATH"', command)
        self.assertIn("for tool in psql pg_dump pg_restore; do", command)
        self.assertIn('"/usr/lib/postgresql/18/bin/$tool" --version', command)


if __name__ == "__main__":
    unittest.main()
