"""Keep shared public validation on read-only hosted execution."""
import pathlib
import unittest
from unittest.mock import patch

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
COMPOSITE = "./ci/scripts/healthcare/setup"
COMPOSITE_PATH = ROOT / "scripts/healthcare/setup/action.yml"


class PublicWorkflowPermissions(unittest.TestCase):
    def check_steps(self, steps, *, composite=False):
        for index, step in enumerate(steps):
            if "uses" not in step:
                continue
            action = step["uses"]
            if not action.startswith("./"):
                self.assertRegex(action, r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+@[0-9a-f]{40}$")
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
                for permissions in [workflow.get("permissions", {})] + [job.get("permissions", {}) for job in workflow.get("jobs", {}).values()]:
                    self.assertIsInstance(permissions, dict)
                    self.assertTrue(all(value in ("read", "none") for value in permissions.values()))
                for job in workflow.get("jobs", {}).values():
                    if "runs-on" in job:
                        self.assertEqual(job["runs-on"], "ubuntu-latest")
                    self.assertNotIn("secrets", job)
                    self.check_steps(job.get("steps", []))
                self.assertNotRegex(path.read_text(), r"\bsecrets[.\[]")

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

    def test_fail_fast_matrices_preserve_every_measurement_producer(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/healthcare.yml").read_text())
        jobs = workflow["jobs"]
        expected = {
            "python-tests": [(str(index), f"Python tests ({index + 1}/4)", f"artifact_{index}")
                             for index in range(4)],
            "address-canonical-db-tests": [(shard, f"Database tests ({label})", "artifact_" + shard.replace("-", "_"))
                                            for shard, label in (("core", "core"), ("provider-directory", "directory"),
                                                                 ("provider-profile", "profiles"))],
        }
        artifact_ids = []
        for job_id, rows in expected.items():
            job = jobs[job_id]
            self.assertEqual(job["strategy"], {"fail-fast": True, "matrix": {"include": [
                {"shard": shard, "label": label, "output": output} for shard, label, output in rows]}})
            self.assertEqual(job["name"], "${{ matrix.label }}")
            self.assertEqual(job["needs"], ["public-hygiene", "readability-preflight"])
            self.assertEqual(job["env"]["CI_SHARD"], "${{ matrix.shard }}")
            self.assertNotIn("continue-on-error", job)
            self.assertEqual(job["outputs"], {output: "${{ steps.coverage-output.outputs." + output + " }}"
                                               for _, _, output in rows})
            emitter = job["steps"][-1]
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
        self.assertEqual(len(download["with"]["artifact-ids"].split(",")), 9)
        self.assertEqual(sum(len(job.get("strategy", {}).get("matrix", {}).get("include", [None]))
                             for job in jobs.values()), 18)


if __name__ == "__main__":
    unittest.main()
