"""Changed-file Ruff checks preserve an exact inherited baseline and fail closed."""

import hashlib
import importlib.util
import io
import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "python_format", Path(__file__).resolve().parents[1] / "scripts/python_format.py"
)
FORMAT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FORMAT)


def _diagnostic(
    code: str = "F401", message: str = "unused import", row: int = 1
) -> dict[str, object]:
    return {
        "code": code,
        "message": message,
        "location": {"row": row, "column": 1},
        "end_location": {"row": row, "column": 2},
    }


def _ruff_result(
    diagnostics: list[dict[str, object]], returncode: int = 1
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(
        args=["ruff"],
        returncode=returncode,
        stdout=json.dumps(diagnostics).encode("utf-8"),
        stderr=b"",
    )


class PythonFormattingTests(unittest.TestCase):
    def test_exact_commit_rejects_non_sha_input_before_git(self):
        with (
            patch.object(FORMAT.subprocess, "check_output") as output,
            self.assertRaisesRegex(ValueError, "full lowercase commit SHA"),
        ):
            FORMAT._exact_commit("HEAD")
        output.assert_not_called()

    def test_exact_commit_resolves_a_full_commit(self):
        sha = "a" * 40
        with patch.object(
            FORMAT.subprocess, "check_output", return_value=(sha + "\n").encode("ascii")
        ) as output:
            self.assertEqual(FORMAT._exact_commit(sha), sha)
        self.assertEqual(
            output.call_args.args[0],
            ["git", "rev-parse", "--verify", sha + "^{commit}"],
        )

    def test_non_ancestor_base_is_rejected(self):
        with (
            patch.object(
                FORMAT.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(["git"], 1, b"", b""),
            ),
            self.assertRaisesRegex(ValueError, "ancestor"),
        ):
            FORMAT._require_base_ancestor("a" * 40, "b" * 40)

    def test_changed_python_files_uses_nul_safe_exact_range_and_no_rename_detection(
        self,
    ):
        base = "a" * 40
        head = "b" * 40
        names = b"has space.py\0-dash.py\0newline\nname.pyi\0"
        with patch.object(
            FORMAT.subprocess, "check_output", return_value=names
        ) as diff:
            self.assertEqual(
                FORMAT.changed_python_files(base, head, "A"),
                ["has space.py", "-dash.py", "newline\nname.pyi"],
            )
        self.assertEqual(
            diff.call_args.args[0],
            [
                "git",
                "diff",
                "--no-renames",
                "--diff-filter=A",
                "--name-only",
                "-z",
                base + ".." + head,
                "--",
                "*.py",
                "*.pyi",
            ],
        )

    def test_ruff_configuration_changes_are_guarded_before_lint(self):
        with (
            patch.object(FORMAT, "_changed_paths", return_value=["nested/.ruff.toml"]),
            patch.object(FORMAT, "_source_if_present") as source,
        ):
            self.assertTrue(FORMAT._ruff_configuration_changes("a" * 40, "b" * 40))
        source.assert_not_called()

    def test_python_change_digest_uses_full_object_ids(self):
        with patch.object(
            FORMAT.subprocess, "check_output", return_value=b"exact raw diff"
        ) as output:
            self.assertEqual(
                FORMAT._python_change_digest("a" * 40, "b" * 40),
                hashlib.sha256(b"exact raw diff").hexdigest(),
            )
        self.assertIn("--abbrev=40", output.call_args.args[0])

    def test_pyproject_without_ruff_settings_does_not_trip_the_configuration_guard(
        self,
    ):
        with (
            patch.object(FORMAT, "_changed_paths", return_value=["pyproject.toml"]),
            patch.object(
                FORMAT,
                "_source_if_present",
                side_effect=[b"[project]\nname = 'x'\n", b"[project]\nname = 'y'\n"],
            ),
        ):
            self.assertFalse(FORMAT._ruff_configuration_changes("a" * 40, "b" * 40))

    def test_pyproject_with_ruff_settings_trips_the_configuration_guard(self):
        with (
            patch.object(FORMAT, "_changed_paths", return_value=["pyproject.toml"]),
            patch.object(
                FORMAT,
                "_source_if_present",
                side_effect=[b"[tool.ruff]\n", b"[tool.ruff.lint]\n"],
            ),
        ):
            self.assertTrue(FORMAT._ruff_configuration_changes("a" * 40, "b" * 40))

    def test_dependency_changes_preserve_unchanged_ruff_policy(self):
        before = b'[project]\ndependencies = ["example==1"]\n[tool.ruff]\nline-length = 120\n'
        after = before.replace(b"example==1", b"example==2")
        with (
            patch.object(FORMAT, "_changed_paths", return_value=["pyproject.toml"]),
            patch.object(FORMAT, "_source_if_present", side_effect=[before, after]),
        ):
            self.assertFalse(FORMAT._ruff_configuration_changes("a" * 40, "b" * 40))

    def test_dependency_changes_cannot_hide_changed_ruff_rules(self):
        before = b'[project]\ndependencies = ["example==1"]\n[tool.ruff.lint]\nselect = ["F"]\n'
        after = before.replace(b"example==1", b"example==2").replace(b'["F"]', b'[]')
        with (
            patch.object(FORMAT, "_changed_paths", return_value=["pyproject.toml"]),
            patch.object(FORMAT, "_source_if_present", side_effect=[before, after]),
        ):
            self.assertTrue(FORMAT._ruff_configuration_changes("a" * 40, "b" * 40))

    def test_quoted_or_inline_ruff_pyproject_settings_trip_the_configuration_guard(self):
        for contents in (
            b'[tool."ruff".lint]\nignore = ["F821"]\n',
            b'[tool]\nruff = { lint = { ignore = ["F821"] } }\n',
        ):
            with self.subTest(contents=contents), patch.object(
                FORMAT, "_changed_paths", return_value=["nested/pyproject.toml"]
            ), patch.object(
                FORMAT, "_source_if_present", side_effect=[None, contents]):
                self.assertTrue(FORMAT._ruff_configuration_changes("a" * 40, "b" * 40))

    def test_malformed_changed_pyproject_fails_closed(self):
        with patch.object(FORMAT, "_changed_paths", return_value=["pyproject.toml"]), patch.object(
            FORMAT, "_source_if_present", side_effect=[None, b"[tool.ruff\n"]
        ), self.assertRaisesRegex(ValueError, "not valid TOML"):
            FORMAT._ruff_configuration_changes("a" * 40, "b" * 40)

    def test_exact_reviewed_first_ruff_configuration_is_approved(self):
        contents = b"[tool.ruff]\nline-length = 120\n"
        transition = (
            "example/repository",
            "a" * 40,
            "c" * 64,
            "pyproject.toml",
            hashlib.sha256(contents).hexdigest(),
        )
        with (
            patch.dict(
                FORMAT.os.environ,
                {
                    "SOURCE_REPOSITORY": transition[0],
                    "GITHUB_REPOSITORY": "example/shared-ci",
                },
                clear=True,
            ),
            patch.object(
                FORMAT, "_APPROVED_RUFF_CONFIG_BASELINES", frozenset({transition})
            ),
            patch.object(FORMAT, "_changed_paths", return_value=[transition[3]]),
            patch.object(FORMAT, "_source_if_present", side_effect=[None, contents]),
            patch.object(FORMAT, "_python_change_digest", return_value=transition[2]),
        ):
            self.assertTrue(
                FORMAT._approved_ruff_configuration_baseline("a" * 40, "b" * 40)
            )

    def test_reviewed_ruff_configuration_transition_fails_closed(self):
        contents = b"[tool.ruff]\nline-length = 120\n"
        transition = (
            "example/repository",
            "a" * 40,
            "c" * 64,
            "pyproject.toml",
            hashlib.sha256(contents).hexdigest(),
        )
        cases = (
            ("another/repository", [transition[3]], None, contents, transition[2]),
            (transition[0], [transition[3]], b"[tool.ruff]\n", contents, transition[2]),
            (transition[0], [transition[3]], None, contents + b"ignore = []\n", transition[2]),
            (transition[0], [transition[3], ".ruff.toml"], None, contents, transition[2]),
            (transition[0], [transition[3]], None, contents, "d" * 64),
        )
        for repository, paths, before, after, digest in cases:
            with (
                self.subTest(repository=repository, paths=paths),
                patch.dict(
                    FORMAT.os.environ,
                    {"SOURCE_REPOSITORY": repository},
                    clear=True,
                ),
                patch.object(
                    FORMAT, "_APPROVED_RUFF_CONFIG_BASELINES", frozenset({transition})
                ),
                patch.object(FORMAT, "_changed_paths", return_value=paths),
                patch.object(FORMAT, "_source_if_present", side_effect=[before, after]),
                patch.object(FORMAT, "_python_change_digest", return_value=digest),
            ):
                self.assertFalse(
                    FORMAT._approved_ruff_configuration_baseline("a" * 40, "b" * 40)
                )

    def test_empty_diff_does_not_expand_to_the_whole_repository(self):
        sha = "a" * 40
        with (
            patch.object(FORMAT, "_exact_commit", return_value=sha),
            patch.object(FORMAT, "_head_commit", return_value=sha),
            patch.object(FORMAT, "_require_base_ancestor") as ancestor,
            patch.object(
                FORMAT, "_assert_ruff_configuration_unchanged", return_value=False
            ) as configuration,
            patch.object(
                FORMAT, "changed_python_files", side_effect=[[], []]
            ) as changed,
            patch.object(FORMAT, "_check_modified_file") as modified,
            patch.object(FORMAT, "_check_added_file") as added,
        ):
            FORMAT.check_changed_files(sha)
        ancestor.assert_called_once_with(sha, sha)
        configuration.assert_called_once_with(sha, sha)
        self.assertEqual(changed.call_args_list[0].args, (sha, sha, "M"))
        self.assertEqual(changed.call_args_list[1].args, (sha, sha, "A"))
        modified.assert_not_called()
        added.assert_not_called()

    def test_check_routes_modified_and_added_paths_to_blob_only_checks(self):
        base = "a" * 40
        head = "b" * 40
        with (
            patch.object(FORMAT, "_exact_commit", return_value=base),
            patch.object(FORMAT, "_head_commit", return_value=head),
            patch.object(FORMAT, "_require_base_ancestor"),
            patch.object(
                FORMAT, "_assert_ruff_configuration_unchanged", return_value=False
            ),
            patch.object(
                FORMAT,
                "changed_python_files",
                side_effect=[["changed.py"], ["added.py"]],
            ),
            patch.object(FORMAT, "_check_modified_file") as modified,
            patch.object(FORMAT, "_check_added_file") as added,
        ):
            FORMAT.check_changed_files(base)
        modified.assert_called_once_with(
            str(Path(FORMAT.sys.executable).with_name("ruff")), base, head, "changed.py"
        )
        added.assert_called_once_with(
            str(Path(FORMAT.sys.executable).with_name("ruff")), head, "added.py"
        )

    def test_modified_file_accepts_a_moved_preexisting_diagnostic(self):
        with (
            patch.object(
                FORMAT,
                "_source_at_revision",
                side_effect=[b"\nimport unused\n", b"import unused\n"],
            ),
            patch.object(
                FORMAT.subprocess,
                "run",
                side_effect=[
                    _ruff_result([_diagnostic(row=2)]),
                    _ruff_result([_diagnostic(row=1)]),
                ],
            ) as run,
        ):
            FORMAT._check_modified_file("ruff", "a" * 40, "b" * 40, "has space.py")
        self.assertEqual(run.call_count, 2)
        self.assertEqual(
            run.call_args_list[0].args[0][-3:],
            ["--stdin-filename", "has space.py", "-"],
        )
        self.assertEqual(run.call_args_list[0].kwargs["input"], b"\nimport unused\n")

    def test_modified_file_rejects_a_new_diagnostic(self):
        stderr = io.StringIO()
        with (
            patch.object(
                FORMAT,
                "_source_at_revision",
                side_effect=[b"import unused\n", b"import used\n"],
            ),
            patch.object(
                FORMAT.subprocess,
                "run",
                side_effect=[
                    _ruff_result([_diagnostic()]),
                    _ruff_result([], returncode=0),
                ],
            ),
            patch.object(FORMAT.sys, "stderr", stderr),
            self.assertRaises(subprocess.CalledProcessError),
        ):
            FORMAT._check_modified_file("ruff", "a" * 40, "b" * 40, "changed.py")
        self.assertIn("changed.py:1:1: F401 unused import", stderr.getvalue())

    def test_diagnostic_counter_rejects_one_new_duplicate(self):
        source = b"import unused\nimport unused\n"
        current = [_diagnostic(row=1), _diagnostic(row=2)]
        baseline = [_diagnostic(row=1)]
        self.assertEqual(
            FORMAT._new_diagnostics(current, source, baseline, source),
            [_diagnostic(row=2)],
        )

    def test_file_level_import_diagnostic_retains_only_its_baseline_count(self):
        baseline = [
            _diagnostic(code="I001", message="Import block is un-sorted", row=1)
        ]
        current = [_diagnostic(code="I001", message="Import block is un-sorted", row=2)]
        self.assertEqual(
            FORMAT._new_diagnostics(
                current, b"import b\nimport a\n", baseline, b"import a\nimport b\n"
            ),
            [],
        )

    def test_diagnostic_identity_rejects_malformed_locations(self):
        malformed = _diagnostic()
        malformed["location"] = {"row": 1, "column": 0}
        with self.assertRaisesRegex(TypeError, "malformed diagnostic"):
            FORMAT._diagnostic_identity(malformed, b"import unused\n")

        malformed = _diagnostic()
        malformed["end_location"] = {"row": 2, "column": 1}
        with self.assertRaisesRegex(ValueError, "malformed diagnostic"):
            FORMAT._diagnostic_identity(malformed, b"import unused\n")

    def test_log_safe_failure_output_escapes_control_characters(self):
        stderr = io.StringIO()
        with (
            patch.object(FORMAT.sys, "stderr", stderr),
            self.assertRaises(subprocess.CalledProcessError),
        ):
            FORMAT._fail_for_new_diagnostics(
                "ruff", "bad\npath.py", [_diagnostic(message="bad\nmessage")]
            )
        self.assertEqual(stderr.getvalue(), "bad\\npath.py:1:1: F401 bad\\nmessage\n")

    def test_ruff_json_rejects_malformed_or_inconsistent_results(self):
        malformed = subprocess.CompletedProcess(["ruff"], 1, b"not-json", b"")
        inconsistent = _ruff_result([_diagnostic()], returncode=0)
        with patch.object(
            FORMAT.subprocess, "run", side_effect=[malformed, inconsistent]
        ):
            with self.assertRaisesRegex(RuntimeError, "malformed JSON"):
                FORMAT._ruff_json_diagnostics("ruff", "changed.py", b"import unused\n")
            with self.assertRaisesRegex(RuntimeError, "inconsistent diagnostic"):
                FORMAT._ruff_json_diagnostics("ruff", "changed.py", b"import unused\n")

    def test_added_file_runs_default_import_and_format_checks_on_the_head_blob(self):
        source = b"import example\n"
        with (
            patch.object(FORMAT, "_source_at_revision", return_value=source) as blob,
            patch.object(
                FORMAT, "_ruff_json_diagnostics", side_effect=[[], []]
            ) as lint,
            patch.object(FORMAT, "_format_added_source") as format_check,
        ):
            FORMAT._check_added_file("ruff", "b" * 40, "added.py")
        blob.assert_called_once_with("b" * 40, "added.py")
        self.assertEqual(lint.call_args_list[0].kwargs["select"], None)
        self.assertEqual(lint.call_args_list[1].kwargs["select"], "I")
        format_check.assert_called_once_with("ruff", "added.py", source)

    def test_format_added_source_passes_blob_bytes_through_standard_input(self):
        source = b"x = 1\n"
        with patch.object(
            FORMAT.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(["ruff"], 0, b"", b""),
        ) as run:
            FORMAT._format_added_source("ruff", "-added.py", source)
        self.assertEqual(
            run.call_args.args[0][-3:], ["--stdin-filename", "-added.py", "-"]
        )
        self.assertEqual(run.call_args.kwargs["input"], source)
        self.assertFalse(run.call_args.kwargs["check"])


if __name__ == "__main__":
    unittest.main()
