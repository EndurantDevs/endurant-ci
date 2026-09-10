"""Added-file formatting preserves exact-base scope and fails closed."""

import importlib.util
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "python_format", Path(__file__).resolve().parents[1] / "scripts/python_format.py"
)
FORMAT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FORMAT)


class PythonFormattingTests(unittest.TestCase):
    def test_empty_diff_does_not_expand_to_the_whole_repository(self):
        with patch.object(FORMAT.subprocess, "check_output", return_value=b""), \
                patch.object(FORMAT.subprocess, "run") as run:
            FORMAT.check_new_files("a" * 40)
        run.assert_not_called()

    def test_paths_remain_separate_and_renames_cannot_bypass_new_file_policy(self):
        with patch.object(FORMAT.subprocess, "check_output", return_value=b"has space.py\0-dash.py\0") as diff, \
                patch.object(FORMAT.subprocess, "run") as run:
            FORMAT.check_new_files("a" * 40)
        self.assertIn("--no-renames", diff.call_args.args[0])
        self.assertIn("--diff-filter=A", diff.call_args.args[0])
        self.assertIn("a" * 40, diff.call_args.args[0])
        self.assertEqual(run.call_count, 2)
        for call in run.call_args_list:
            self.assertEqual(call.args[0][-3:], ["--", "has space.py", "-dash.py"])
            self.assertIn("--force-exclude", call.args[0])
            self.assertTrue(call.kwargs["check"])

    def test_bad_base_and_lint_failure_stop_the_check(self):
        with patch.object(FORMAT.subprocess, "check_output", side_effect=subprocess.CalledProcessError(128, "git")), \
                patch.object(FORMAT.subprocess, "run") as run:
            with self.assertRaises(subprocess.CalledProcessError):
                FORMAT.check_new_files("missing")
            run.assert_not_called()
        with patch.object(FORMAT.subprocess, "check_output", return_value=b"new.py\0"), \
                patch.object(FORMAT.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "ruff")) as run:
            with self.assertRaises(subprocess.CalledProcessError):
                FORMAT.check_new_files("a" * 40)
            self.assertEqual(run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
