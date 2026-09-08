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


if __name__ == "__main__":
    unittest.main()
