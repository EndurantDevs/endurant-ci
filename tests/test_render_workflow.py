"""The flat graph preserves validation and isolates non-source PR metadata edits."""

import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
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
                        metadata_required = job_id == "smoke" or (kind == "healthcare" and job_id == "source-validation")
                        if not metadata_required:
                            expected["name"] = RENDERER.job_name(expected["name"], job_id)
                        condition = expected.get("if", "success()").removeprefix("${{").removesuffix("}}").strip()
                        expected["if"] = (
                            "${{ " + condition + " }}"
                            if metadata_required
                            else "${{ " + RENDERER.GUARD + condition + ") }}"
                        )
                        self.assertEqual(job, expected)
                        for step in job.get("steps", []):
                            if step.get("with", {}).get("repository") == "EndurantDevs/endurant-ci":
                                self.assertEqual(step["with"]["ref"], "1" * 40)
                    self.assertIn(RENDERER.METADATA_ONLY, workflow["run-name"])
                    self.assertIn("format('ci-metadata-{0}', github.event.pull_request.number)",
                                  workflow["concurrency"]["group"])
                    self.assertIn("format('ci-{0}', github.ref)", workflow["concurrency"]["group"])
                    self.assertIn("github.event_name == 'push' && format('ci-push-{0}', github.run_id)",
                                  workflow["concurrency"]["group"])
                    self.assertEqual(workflow["concurrency"]["cancel-in-progress"],
                                     "${{ github.event_name == 'pull_request' }}")
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
                producer = "container-package" if kind == "healthcare" else "validate"
                measurement = "measurement" if kind == "healthcare" else "publish"
                cleanup_job = workflow["jobs"]["artifact-cleanup"]
                self.assertEqual(cleanup_job, {
                    "name": RENDERER.job_name("CI artifact cleanup"), "runs-on": "ubuntu-latest", "timeout-minutes": 10,
                    "needs": ["dev-image-publication", producer, measurement],
                    "if": "${{ " + RENDERER.GUARD + "success()) }}",
                    "permissions": {"contents": "read", "actions": "write"},
                    "steps": [{"name": "Check out trusted cleanup helper",
                               "uses": "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
                               "with": {"repository": "EndurantDevs/endurant-ci", "ref": "1" * 40,
                                        "path": "ci", "persist-credentials": False}},
                              {"name": "Remove validated CI intermediates",
                               "env": {"GH_TOKEN": "${{ github.token }}", "PYTHONDONTWRITEBYTECODE": "1",
                                       "IMAGE_ARTIFACT_ID": "${{ needs." + producer + ".outputs.image_artifact_id }}",
                                       "MEASUREMENT_ARTIFACT_ID": "${{ needs." + measurement + ".outputs.measurement_artifact_id }}",
                                       "IMAGE_RECEIPT_ARTIFACT_ID": "${{ needs.dev-image-publication.outputs.receipt_artifact_id }}"},
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
                expected_needs = ["smoke", "source-validation", producer, measurement] if kind == "healthcare" else ["smoke", "publish", producer]
                self.assertEqual(publisher["needs"], expected_needs)
                self.assertEqual(publisher["env"], {"CI_REVISION": "1" * 40, "PYTHONDONTWRITEBYTECODE": "1"})
                self.assertEqual(publisher["outputs"], {"receipt_artifact_id": "${{ steps.receipt-artifact.outputs.artifact-id }}"})
                self.assertEqual(publisher["steps"][0]["with"], {"repository": "EndurantDevs/endurant-ci", "ref": "1" * 40,
                                 "path": "ci", "persist-credentials": False})
                self.assertNotIn("if", publisher["steps"][1])
                self.assertEqual(publisher["steps"][1]["run"], "python3 ci/scripts/source_image.py prepare")
                self.assertEqual(publisher["steps"][1]["env"], {
                    "GH_TOKEN": "${{ github.token }}",
                    "IMAGE_ARTIFACT_ID": "${{ needs." + producer + ".outputs.image_artifact_id }}",
                    "MEASUREMENT_ARTIFACT_ID": "${{ needs." + measurement + ".outputs.measurement_artifact_id }}"})
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
                self.assertEqual(steps["Upload DEV image receipt"]["id"], "receipt-artifact")
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
                        metadata_required = job_id == "smoke" or (kind == "healthcare" and job_id == "source-validation")
                        if not metadata_required:
                            self.assertIn(RENDERER.GUARD, job["if"])
                            self.assertIn("(metadata only)", job["name"])
                    caller.write_text(rendered)
                    self.assertEqual(RENDERER.render_workflow(kind, "1" * 40, caller), rendered)

    def test_healthcare_metadata_edits_keep_required_validation_context(self):
        original = {"name": "CI", "on": {"pull_request": {}},
                    "jobs": {"smoke": {"name": "portable import checks", "runs-on": "ubuntu-latest",
                                       "steps": [{"run": "echo synthetic"}]}}}
        with tempfile.TemporaryDirectory() as temporary:
            caller = Path(temporary) / "ci.yml"
            caller.write_text(yaml.safe_dump(original))
            workflow = yaml.safe_load(RENDERER.render_workflow("healthcare", "1" * 40, caller))
        source_validation = workflow["jobs"]["source-validation"]
        self.assertEqual(source_validation["name"], "Validation complete")
        self.assertEqual(source_validation["timeout-minutes"], 45)
        self.assertEqual(source_validation["if"], "${{ always() }}")
        self.assertEqual(source_validation["permissions"], {"actions": "read"})
        step = source_validation["steps"][0]
        self.assertEqual(step["env"], {
            "GH_TOKEN": "${{ github.token }}",
            "METADATA_ONLY": "${{ " + RENDERER.METADATA_ONLY + " }}",
            "PR_NUMBER": "${{ github.event.pull_request.number || '' }}",
            "SOURCE_SHA": "${{ github.event.pull_request.head.sha || github.sha }}",
            "BASE_SHA": "${{ github.event.pull_request.base.sha || '' }}",
            "RESULTS": "${{ toJSON(needs.*.result) }}",
        })
        self.assertEqual(step["run"], (
            "if [ \"$METADATA_ONLY\" != true ]; then\n"
            "  jq -e 'length > 0 and all(. == \"success\")' <<< \"$RESULTS\"\n"
            "  exit 0\n"
            "fi\n\n"
            "deadline=$((SECONDS + 2400))\n"
            "while :; do\n"
            "  if ! validation_state=\"$(\n"
            "    set -o pipefail\n"
            "    gh api --paginate --slurp \\\n"
            "      \"repos/$GITHUB_REPOSITORY/actions/workflows/ci.yml/runs?event=pull_request&head_sha=$SOURCE_SHA&per_page=100\" | jq --raw-output '[.[] | .workflow_runs[]\n"
            "        | select(.name == \"CI\" and .display_title == \"CI\")\n"
            "        | select(any(.pull_requests[]?; .number == (env.PR_NUMBER | tonumber) and .head.sha == env.SOURCE_SHA and .base.sha == env.BASE_SHA))\n"
            "        | {run_started_at, created_at, id, run_attempt, status, conclusion}]\n"
            "        | sort_by([(.run_started_at // .created_at), .id, .run_attempt])\n"
            "        | last\n"
            "        | if . == null then \"pending\"\n"
            "          elif .status != \"completed\" then \"pending\"\n"
            "          elif .conclusion == \"success\" then \"success\"\n"
            "          else \"failed\"\n"
            "          end'\n"
            "  )\"; then\n"
            "    validation_state=pending\n"
            "  fi\n"
            "  case \"$validation_state\" in\n"
            "    success) exit 0 ;;\n"
            "    failed)\n"
            "      printf 'No successful full validation exists for source %s at base %s.\\n' \"$SOURCE_SHA\" \"$BASE_SHA\" >&2\n"
            "      exit 1\n"
            "      ;;\n"
            "    pending)\n"
            "      if (( SECONDS >= deadline )); then\n"
            "        printf 'Timed out waiting for full validation of source %s at base %s.\\n' \"$SOURCE_SHA\" \"$BASE_SHA\" >&2\n"
            "        exit 1\n"
            "      fi\n"
            "      sleep 15\n"
            "      ;;\n"
            "    *)\n"
            "      printf 'Unexpected full-validation state: %s.\\n' \"$validation_state\" >&2\n"
            "      exit 1\n"
            "      ;;\n"
            "  esac\n"
            "done\n"
        ))

    def test_healthcare_metadata_validation_uses_the_latest_matching_full_run(self):
        original = {"name": "CI", "on": {"pull_request": {}},
                    "jobs": {"smoke": {"name": "portable import checks", "runs-on": "ubuntu-latest",
                                       "steps": [{"run": "echo synthetic"}]}}}
        with tempfile.TemporaryDirectory() as temporary:
            caller = Path(temporary) / "ci.yml"
            caller.write_text(yaml.safe_dump(original))
            workflow = yaml.safe_load(RENDERER.render_workflow("healthcare", "1" * 40, caller))
        run = workflow["jobs"]["source-validation"]["steps"][0]["run"]
        query = re.search(r"jq --raw-output '(.+?)'\n\s+\)\"", run, re.DOTALL).group(1)

        def full_run(*, started_at, identifier, attempt=1, status="completed", conclusion="success",
                     title="CI", number=17, head="source", base="base", created_at=None):
            return {
                "name": "CI", "display_title": title, "head_sha": head,
                "run_started_at": started_at, "created_at": created_at or started_at,
                "id": identifier, "run_attempt": attempt, "status": status, "conclusion": conclusion,
                "pull_requests": [{"number": number, "head": {"sha": head}, "base": {"sha": base}}],
            }

        def state(*pages):
            result = subprocess.run(
                ["jq", "--raw-output", query],
                input=json.dumps([{"workflow_runs": page} for page in pages]),
                text=True, capture_output=True, check=False,
                env={**os.environ, "PR_NUMBER": "17", "SOURCE_SHA": "source", "BASE_SHA": "base"},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout.strip()

        old_success = full_run(started_at="2026-01-01T10:00:00Z", identifier=10)
        later_failure = full_run(started_at="2026-01-01T11:00:00Z", identifier=11, conclusion="failure")
        rerun_success = full_run(started_at="2026-01-01T12:00:00Z", identifier=10, attempt=2,
                                 created_at="2026-01-01T09:00:00Z")
        in_progress = full_run(started_at="2026-01-01T13:00:00Z", identifier=12,
                               status="in_progress", conclusion=None)
        metadata = full_run(started_at="2026-01-01T14:00:00Z", identifier=13,
                            title="CI metadata update", conclusion="failure")
        wrong_pr = full_run(started_at="2026-01-01T15:00:00Z", identifier=14, number=18)
        wrong_head = full_run(started_at="2026-01-01T15:00:00Z", identifier=15, head="other-source")
        wrong_base = full_run(started_at="2026-01-01T15:00:00Z", identifier=16, base="other-base")

        with self.subTest("latest full failure blocks"):
            self.assertEqual(state([old_success, later_failure]), "failed")
        with self.subTest("newer rerun succeeds"):
            self.assertEqual(state([later_failure, rerun_success]), "success")
        with self.subTest("in-progress full run waits"):
            self.assertEqual(state([old_success, in_progress]), "pending")
        with self.subTest("metadata run is ignored"):
            self.assertEqual(state([old_success, metadata]), "success")
        with self.subTest("full validation remains visible beyond a metadata-only page"):
            metadata_page = [full_run(started_at="2026-01-01T14:00:00Z", identifier=100 + index,
                                      title="CI metadata update", conclusion="failure")
                             for index in range(100)]
            self.assertEqual(state(metadata_page, [old_success]), "success")
        with self.subTest("wrong identity is ignored"):
            self.assertEqual(state([wrong_pr, wrong_head, wrong_base]), "pending")
        with self.subTest("no full run waits"):
            self.assertEqual(state([]), "pending")

    def test_healthcare_metadata_validation_retries_an_api_failure(self):
        original = {"name": "CI", "on": {"pull_request": {}},
                    "jobs": {"smoke": {"name": "portable import checks", "runs-on": "ubuntu-latest",
                                       "steps": [{"run": "echo synthetic"}]}}}
        with tempfile.TemporaryDirectory() as temporary:
            caller = Path(temporary) / "ci.yml"
            caller.write_text(yaml.safe_dump(original))
            workflow = yaml.safe_load(RENDERER.render_workflow("healthcare", "1" * 40, caller))
            run = workflow["jobs"]["source-validation"]["steps"][0]["run"]
            syntax = subprocess.run(["bash", "-n"], input=run, text=True, capture_output=True, check=False)
            self.assertEqual(syntax.returncode, 0, syntax.stderr)

            commands = Path(temporary) / "commands"
            commands.mkdir()
            counter = Path(temporary) / "gh-count"
            gh = commands / "gh"
            gh.write_text(
                "#!/bin/sh\n"
                "count=0\n"
                "if [ -f \"$FAKE_GH_COUNT\" ]; then count=$(cat \"$FAKE_GH_COUNT\"); fi\n"
                "count=$((count + 1))\n"
                "printf '%s' \"$count\" > \"$FAKE_GH_COUNT\"\n"
                "case \"$*\" in *--paginate*--slurp*) ;; *) exit 2;; esac\n"
                "case \"$*\" in *--jq*) exit 2;; esac\n"
                "if [ \"$count\" -eq 1 ]; then\n"
                "  printf '%s\\n' 'synthetic API failure' >&2\n"
                "  exit 1\n"
                "fi\n"
                "printf '%s\\n' '[{\"workflow_runs\":[{\"name\":\"CI\",\"display_title\":\"CI\",\"id\":1,\"run_attempt\":1,\"status\":\"completed\",\"conclusion\":\"success\",\"pull_requests\":[{\"number\":17,\"head\":{\"sha\":\"source\"},\"base\":{\"sha\":\"base\"}}]}]}]'\n"
            )
            sleep = commands / "sleep"
            sleep.write_text("#!/bin/sh\nexit 0\n")
            gh.chmod(0o755)
            sleep.chmod(0o755)
            result = subprocess.run(
                ["bash", "-e", "-o", "pipefail", "-c", run], text=True, capture_output=True, check=False,
                timeout=10, env={
                    **os.environ, "PATH": str(commands) + os.pathsep + os.environ["PATH"],
                    "FAKE_GH_COUNT": str(counter), "GH_TOKEN": "synthetic", "METADATA_ONLY": "true",
                    "PR_NUMBER": "17", "SOURCE_SHA": "source", "BASE_SHA": "base",
                    "GITHUB_REPOSITORY": "owner/repository", "RESULTS": "[]",
                },
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(counter.read_text(), "2")

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
