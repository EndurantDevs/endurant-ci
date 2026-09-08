"""The flat graph preserves validation and isolates non-source PR metadata edits."""

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("render_workflow", ROOT / "scripts/render_workflow.py")
RENDERER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RENDERER)


class RenderWorkflowChecks(unittest.TestCase):
    def test_flat_graph_keeps_every_gate_and_metadata_edits_have_separate_contexts(self):
        original = {"name": "CI", "on": {"pull_request": {"types": ["opened", "synchronize", "reopened", "edited"]},
                                          "push": {"branches": ["main"]}},
                    "jobs": {"smoke": {"name": "portable import checks", "runs-on": "ubuntu-latest",
                                        "steps": [{"run": "python -m pytest -q tests/test_imports.py"}]}}}
        with tempfile.TemporaryDirectory() as temporary:
            caller = Path(temporary) / "ci.yml"
            for kind in ("healthcare", "drug"):
                with self.subTest(kind=kind):
                    caller.write_text(yaml.safe_dump(original))
                    canonical = yaml.safe_load((ROOT / ".github/workflows" / f"{kind}.yml").read_text())
                    rendered = RENDERER.render_workflow(kind, "1" * 40, caller)
                    workflow = yaml.safe_load(rendered)
                    self.assertEqual(workflow["on"], original["on"])
                    self.assertEqual(set(workflow["jobs"]), {"smoke", *canonical["jobs"]})
                    self.assertNotIn("${{ inputs.ci_revision }}", rendered)
                    for job_id, job in workflow["jobs"].items():
                        self.assertNotIn("uses", job)
                        self.assertEqual(job["runs-on"], "ubuntu-latest")
                        template = original["jobs"][job_id] if job_id == "smoke" else canonical["jobs"][job_id]
                        expected = json.loads(json.dumps(template).replace("${{ inputs.ci_revision }}", "1" * 40))
                        expected["name"] = RENDERER.job_name(expected["name"], job_id)
                        condition = expected.get("if", "success()").removeprefix("${{").removesuffix("}}").strip()
                        expected["if"] = "${{ " + RENDERER.GUARD + condition + ") }}"
                        self.assertEqual(job, expected)
                        for step in job.get("steps", []):
                            if step.get("with", {}).get("repository") == "EndurantDevs/endurant-ci":
                                self.assertEqual(step["with"]["ref"], "1" * 40)
                    self.assertIn(RENDERER.METADATA_ONLY, workflow["run-name"])
                    self.assertIn("format('ci-metadata-{0}', github.run_id)", workflow["concurrency"]["group"])
                    self.assertIn("format('ci-{0}', github.ref)", workflow["concurrency"]["group"])
                    self.assertIn(RENDERER.METADATA_ONLY, workflow["concurrency"]["cancel-in-progress"])
                    caller.write_text(rendered)
                    self.assertEqual(RENDERER.render_workflow(kind, "1" * 40, caller), rendered)
                    refreshed = yaml.safe_load(RENDERER.render_workflow(kind, "2" * 40, caller))
                    self.assertEqual(refreshed["jobs"]["smoke"], workflow["jobs"]["smoke"])

    def test_revision_and_job_labels_cannot_inject_expressions(self):
        for label in ("${{ inputs.name }}", "bad' || true || '"):
            with self.subTest(label=label), self.assertRaises(ValueError):
                RENDERER.job_name(label)
        for revision in ("main", "0" * 40, "1" * 39, "${{ github.sha }}"):
            with self.subTest(revision=revision), self.assertRaises(ValueError):
                RENDERER.render_workflow("healthcare", revision, Path("unused"))
        with self.assertRaises(ValueError):
            RENDERER.job_name("${{ matrix.label }}")
        matrix_names = [RENDERER.job_name("${{ matrix.label }}", name)
                        for name in ("python-tests", "address-canonical-db-tests")]
        self.assertEqual(len(set(matrix_names)), 2)
        for name in matrix_names:
            self.assertIn("(metadata only)' || matrix.label", name)


if __name__ == "__main__":
    unittest.main()
