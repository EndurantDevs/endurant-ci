"""Keep shared public validation on read-only hosted execution."""
import pathlib
import re
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]


class PublicWorkflowPermissions(unittest.TestCase):
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
                    for step in job.get("steps", []):
                        if "uses" in step:
                            if step["uses"].startswith("./"):
                                self.assertEqual(step["uses"], "./ci/scripts/healthcare/setup")
                                self.assertIn("inputs.ci_revision", str(job["steps"]))
                            else:
                                self.assertRegex(step["uses"], r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+@[0-9a-f]{40}$")
                self.assertNotRegex(path.read_text(), r"\bsecrets[.\[]")


if __name__ == "__main__":
    unittest.main()
