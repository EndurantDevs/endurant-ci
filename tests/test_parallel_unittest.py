"""The parallel runner runs every discovered test once and never hides a failure."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

RUNNER = Path(__file__).resolve().parents[1] / "scripts/parallel_unittest.py"
CASES = """import unittest
from tests import test_first


class Second(unittest.TestCase):
    def test_pass(self):
        self.assertTrue(test_first)

    def test_other(self):
        self.assertTrue({passes})
"""


class ParallelRunnerTests(unittest.TestCase):
    def run_fixture(self, files):
        with tempfile.TemporaryDirectory() as temporary:
            tests = Path(temporary) / "tests"
            tests.mkdir()
            for name, text in files.items():
                (tests / name).write_text(text)
            return subprocess.run(
                [sys.executable, str(RUNNER)],
                cwd=temporary,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )

    def test_complete_run_reports_every_test_and_failures_fail(self):
        first = "import unittest\n\n\nclass First(unittest.TestCase):\n    def test_pass(self):\n        pass\n"
        for passes in (True, False):
            with self.subTest(passes=passes):
                result = self.run_fixture(
                    {
                        "test_first.py": first,
                        "test_second.py": CASES.format(passes=passes),
                    }
                )
                self.assertEqual(
                    result.returncode, int(not passes), result.stdout + result.stderr
                )
                self.assertIn("Ran 3 tests in ", result.stdout)
                for name in (
                    "test_first.First.test_pass",
                    "test_second.Second.test_pass",
                ):
                    self.assertIn(f"({name}) ... ok", result.stdout)
                if not passes:
                    self.assertIn(
                        "FAILED (3 of 3 ran)\ntest_second.Second.test_other",
                        result.stdout,
                    )

    def test_unimportable_or_missing_tests_fail_before_running(self):
        for files in ({}, {"test_broken.py": "import missing_module_for_fixture\n"}):
            with self.subTest(files=files):
                result = self.run_fixture(files)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn("Test discovery is incomplete", result.stderr)
                self.assertNotIn("Ran ", result.stdout)


if __name__ == "__main__":
    unittest.main()
