"""Changed-file Ruff checks preserve exact-base scope and fail closed."""

import importlib.util
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "python_format", Path(__file__).resolve().parents[1] / "scripts/python_format.py"
)
FORMAT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FORMAT)


class PythonFormattingTests(unittest.TestCase):
    def test_empty_diff_does_not_expand_to_the_whole_repository(self):
        with (
            patch.object(FORMAT.subprocess, "check_output", return_value=b"") as diff,
            patch.object(FORMAT.subprocess, "run") as run,
        ):
            FORMAT.check_changed_files("a" * 40)
        run.assert_not_called()
        self.assertEqual(diff.call_count, 1)
        self.assertEqual(
            diff.call_args.args[0],
            ["git", "diff", "--no-renames", "--diff-filter=AM", "--name-only", "-z",
             "a" * 40 + "..HEAD", "--", "*.py", "*.pyi"],
        )

    def test_modified_paths_receive_only_default_lint(self):
        with (
            patch.object(
                FORMAT.subprocess,
                "check_output",
                side_effect=[b"has space.py\0-dash.py\0", b""],
            ) as diff,
            patch.object(FORMAT.subprocess, "run") as run,
        ):
            FORMAT.check_changed_files("a" * 40)
        self.assertEqual([call.args[0][3] for call in diff.call_args_list], ["--diff-filter=AM", "--diff-filter=A"])
        self.assertEqual(run.call_count, 1)
        self.assertEqual(
            run.call_args.args[0][-3:],
            ["--", "has space.py", "-dash.py"],
        )
        self.assertNotIn("--select", run.call_args.args[0])
        self.assertIn("--force-exclude", run.call_args.args[0])
        self.assertTrue(run.call_args.kwargs["check"])

    def test_added_and_modified_paths_keep_import_order_and_format_additions_only(self):
        with (
            patch.object(
                FORMAT.subprocess,
                "check_output",
                side_effect=[b"added.py\0changed.py\0", b"added.py\0"],
            ) as diff,
            patch.object(FORMAT.subprocess, "run") as run,
        ):
            FORMAT.check_changed_files("a" * 40)
        self.assertEqual([call.args[0][3] for call in diff.call_args_list], ["--diff-filter=AM", "--diff-filter=A"])
        self.assertEqual(run.call_count, 3)
        self.assertEqual(run.call_args_list[0].args[0][-3:], ["--", "added.py", "changed.py"])
        self.assertEqual(run.call_args_list[1].args[0][-2:], ["--", "added.py"])
        self.assertEqual(run.call_args_list[2].args[0][-2:], ["--", "added.py"])
        self.assertEqual(run.call_args_list[1].args[0][1:3], ["check", "--no-cache"])
        self.assertIn("--select", run.call_args_list[1].args[0])
        self.assertEqual(run.call_args_list[2].args[0][1:3], ["format", "--check"])
        for call in run.call_args_list:
            self.assertIn("--force-exclude", call.args[0])
            self.assertTrue(call.kwargs["check"])

    def test_bad_base_and_lint_failure_stop_the_check(self):
        with (
            patch.object(FORMAT.subprocess, "check_output", side_effect=subprocess.CalledProcessError(128, "git")),
            patch.object(FORMAT.subprocess, "run") as run,
        ):
            with self.assertRaises(subprocess.CalledProcessError):
                FORMAT.check_changed_files("missing")
            run.assert_not_called()
        with (
            patch.object(FORMAT.subprocess, "check_output", return_value=b"new.py\0") as diff,
            patch.object(FORMAT.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "ruff")) as run,
        ):
            with self.assertRaises(subprocess.CalledProcessError):
                FORMAT.check_changed_files("a" * 40)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(diff.call_count, 1)


if __name__ == "__main__":
    unittest.main()
