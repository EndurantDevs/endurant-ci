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
                    self.assertEqual(set(workflow["jobs"]), {"smoke", "dev-image-publication", "artifact-cleanup", *canonical["jobs"]})
                    self.assertNotIn("${{ inputs.ci_revision }}", rendered)
                    for job_id, job in workflow["jobs"].items():
                        self.assertNotIn("uses", job)
                        self.assertEqual(job["runs-on"], "ubuntu-latest")
                        if job_id in {"artifact-cleanup", "dev-image-publication"}:
                            continue  # Its complete privilege and execution contract is checked below.
                        template = original["jobs"][job_id] if job_id == "smoke" else canonical["jobs"][job_id]
                        expected = json.loads(json.dumps(template).replace("${{ inputs.ci_revision }}", "1" * 40))
                        if job_id != "smoke":
                            expected["name"] = RENDERER.job_name(expected["name"], job_id)
                        condition = expected.get("if", "success()").removeprefix("${{").removesuffix("}}").strip()
                        expected["if"] = "${{ success() }}" if job_id == "smoke" else "${{ " + RENDERER.GUARD + condition + ") }}"
                        self.assertEqual(job, expected)
                        for step in job.get("steps", []):
                            if step.get("with", {}).get("repository") == "EndurantDevs/endurant-ci":
                                self.assertEqual(step["with"]["ref"], "1" * 40)
                    self.assertIn(RENDERER.METADATA_ONLY, workflow["run-name"])
                    self.assertIn("format('ci-metadata-{0}', github.run_id)", workflow["concurrency"]["group"])
                    self.assertIn("format('ci-{0}', github.ref)", workflow["concurrency"]["group"])
                    self.assertIn("github.event_name == 'push' && format('ci-push-{0}', github.run_id)",
                                  workflow["concurrency"]["group"])
                    self.assertEqual(workflow["concurrency"]["cancel-in-progress"],
                                     "${{ github.event_name == 'pull_request' && !(" + RENDERER.METADATA_ONLY + ") }}")
                    self.assertIn(RENDERER.METADATA_ONLY, workflow["concurrency"]["cancel-in-progress"])
                    caller.write_text(rendered)
                    self.assertEqual(RENDERER.render_workflow(kind, "1" * 40, caller), rendered)
                    refreshed = yaml.safe_load(RENDERER.render_workflow(kind, "2" * 40, caller))
                    self.assertEqual(refreshed["jobs"]["smoke"], workflow["jobs"]["smoke"])

    def test_only_trusted_publication_and_final_cleanup_get_write_access(self):
        with tempfile.TemporaryDirectory() as directory:
            caller = Path(directory) / "ci.yml"
            for kind in ("healthcare", "drug"):
                caller.write_text(yaml.safe_dump({"on": {"push": {"branches": ["dev"]}},
                    "jobs": {"smoke": {"name": "portable import checks", "runs-on": "ubuntu-latest",
                                       "steps": [{"run": "echo synthetic"}]}}}))
                workflow = yaml.safe_load(RENDERER.render_workflow(kind, "1" * 40, caller))
                self.assertEqual(workflow["permissions"], {"contents": "read", "pull-requests": "read", "actions": "read"})
                cleanup_job = workflow["jobs"]["artifact-cleanup"]
                self.assertEqual(cleanup_job, {
                    "name": RENDERER.job_name("CI artifact cleanup"), "runs-on": "ubuntu-latest", "timeout-minutes": 10,
                    "needs": ["dev-image-publication"],
                    "if": "${{ " + RENDERER.GUARD + "always()) }}",
                    "permissions": {"contents": "read", "actions": "write"},
                    "steps": [{"name": "Check out trusted cleanup helper",
                               "uses": "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
                               "with": {"repository": "EndurantDevs/endurant-ci", "ref": "1" * 40,
                                        "path": "ci", "persist-credentials": False}},
                              {"name": "Remove validated CI intermediates",
                               "env": {"GH_TOKEN": "${{ github.token }}", "PYTHONDONTWRITEBYTECODE": "1"},
                               "run": "python3 ci/scripts/artifact_cleanup.py"}],
                })
                ancestors = set()
                pending = list(cleanup_job["needs"])
                while pending:
                    identifier = pending.pop()
                    if identifier in ancestors:
                        continue
                    ancestors.add(identifier)
                    needs = workflow["jobs"][identifier].get("needs", [])
                    pending.extend([needs] if isinstance(needs, str) else needs)
                self.assertEqual(ancestors, set(workflow["jobs"]) - {"artifact-cleanup"})
                for identifier in ancestors:
                    if identifier != "dev-image-publication":
                        self.assertTrue(all(value == "read" for value in workflow["jobs"][identifier].get("permissions", {}).values()))
                publisher = workflow["jobs"]["dev-image-publication"]
                self.assertEqual(publisher["permissions"], {"contents": "read", "pull-requests": "read", "actions": "read", "packages": "write"})
                self.assertEqual(publisher["needs"], ["smoke", "source-validation" if kind == "healthcare" else "publish"])
                self.assertEqual(publisher["env"], {"CI_REVISION": "1" * 40, "PYTHONDONTWRITEBYTECODE": "1"})
                self.assertEqual(publisher["steps"][0]["with"], {"repository": "EndurantDevs/endurant-ci", "ref": "1" * 40,
                                 "path": "ci", "persist-credentials": False})
                self.assertNotIn("if", publisher["steps"][1])
                self.assertEqual(publisher["steps"][1]["run"], "python3 ci/scripts/source_image.py prepare")
                steps = {step["name"]: step for step in publisher["steps"]}
                self.assertEqual(len(steps), len(publisher["steps"]))
                self.assertEqual(steps["Stage DEV image publication intent"]["run"], "python3 ci/scripts/source_image.py stage")
                self.assertEqual(steps["Publish validated DEV image"]["run"], "python3 ci/scripts/source_image.py publish")
                for step in publisher["steps"][2:-1]:
                    self.assertEqual(step["if"], "steps.image.outputs.publish == 'true'")
                self.assertEqual(publisher["steps"][2]["with"], {"artifact-ids": "${{ steps.image.outputs.artifact_id }}",
                    "digest-mismatch": "error", "path": "${{ runner.temp }}/public-image-download", "merge-multiple": True})
                self.assertEqual(steps["Upload DEV image receipt"]["with"], {
                    "name": kind + "-public-image-${{ github.run_id }}-${{ github.run_attempt }}",
                    "path": "${{ runner.temp }}/public-image-receipt/image.json",
                    "if-no-files-found": "error", "retention-days": 90})
                self.assertEqual(publisher["if"], "${{ " + RENDERER.GUARD + "success()) }}")
                self.assertEqual(steps["Upload DEV image publication intent"]["with"], {
                    "name": kind + "-public-image-intent-${{ github.run_id }}-${{ github.run_attempt }}",
                    "path": "${{ runner.temp }}/public-image-intent/intent.json",
                    "if-no-files-found": "error", "retention-days": 90})
                self.assertLess(list(steps).index("Upload DEV image publication intent"),
                                list(steps).index("Publish validated DEV image"))
                self.assertEqual(publisher["steps"][-1], {
                    "name": "Reconcile DEV image publication",
                    "if": "always() && steps.image.outputs.publish == 'true'",
                    "env": {"GH_TOKEN": "${{ github.token }}"},
                    "run": "python3 ci/scripts/source_image.py reconcile"})

    def test_existing_metadata_skipped_smoke_becomes_a_real_static_required_check(self):
        smoke = {"name": "portable import checks", "runs-on": "ubuntu-latest",
                 "steps": [{"name": "Run synthetic import checks", "run": "python -m pytest -q tests/test_imports.py"}]}
        previous = {**smoke, "name": RENDERER.job_name(smoke["name"]),
                    "if": "${{ " + RENDERER.GUARD + "success()) }}"}
        with tempfile.TemporaryDirectory() as temporary:
            caller = Path(temporary) / "ci.yml"
            for kind in ("healthcare", "drug"):
                with self.subTest(kind=kind):
                    caller.write_text(yaml.safe_dump({"name": "CI", "on": {"pull_request": {}},
                                                     "jobs": {"smoke": previous}}))
                    rendered = RENDERER.render_workflow(kind, "1" * 40, caller)
                    workflow = yaml.safe_load(rendered)
                    self.assertEqual(workflow["jobs"]["smoke"], {**smoke, "if": "${{ success() }}"})
                    self.assertEqual(workflow["permissions"], {"contents": "read", "pull-requests": "read", "actions": "read"})
                    self.assertIn("CI metadata update", workflow["run-name"])
                    for job_id, job in workflow["jobs"].items():
                        if job_id != "smoke":
                            self.assertIn(RENDERER.GUARD, job["if"])
                            self.assertIn("(metadata only)", job["name"])
                    caller.write_text(rendered)
                    self.assertEqual(RENDERER.render_workflow(kind, "1" * 40, caller), rendered)

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
