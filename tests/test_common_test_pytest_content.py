"""The complete default pytest contract rejects unqualified ambient inputs."""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import test_common_test_content as fixtures

common = fixtures.common


class PytestContentTests(unittest.TestCase):
    def setUp(self):
        self.content = fixtures.ContentExecutionTests()
        self.content.setUp()
        self.addCleanup(self.content.doCleanups)
        self.root = self.content.root
        self.arguments = self.content.arguments
        self.arguments.scope = common.PYTEST_CONTENT_SCOPE
        self.arguments.command = [
            sys.executable,
            "-m",
            "coverage",
            "run",
            "--branch",
            "--source=.",
            "-m",
            "pytest",
            "-q",
        ]
        tools = self.root.parent / "tools"
        tools.mkdir()
        (tools / "python").symlink_to(sys.executable)
        self.real_git = shutil.which("git")
        self.git = tools / "git"
        self.git.write_text('#!/bin/sh\nexec "' + self.real_git + '" "$@"\n')
        self.git.chmod(0o755)
        self.path = patch.dict(
            os.environ, {"PATH": str(tools) + os.pathsep + os.environ["PATH"]}
        )
        self.path.start()
        self.addCleanup(self.path.stop)

    def test_equal_tree_rewrite_reuses_complete_original_pytest_execution(self):
        self.content.test_same_tree_rewrite_reuses_original_without_fabricating_source_identity()
        toolchains = self.content.document()["measurement"]["inputs"]["toolchains"]
        self.assertTrue(
            {"fixture-git", "fixture-shell", "fixture-python"} <= set(toolchains)
        )

    def test_changed_fixture_tool_bytes_require_full_execution(self):
        self.content.original()
        with self.git.open("a") as stream:
            stream.write("# changed tool bytes\n")
        self.assertEqual(self.content.execute(), 0)
        self.assertEqual(len(self.content.calls), 2)
        self.assertTrue(self.content.document()["measurement"]["executed"])

    def test_missing_evidence_executes_the_complete_same_profile(self):
        self.content.test_missing_evidence_runs_the_same_archived_command_and_environment()

    def test_recipe_controls_are_preserved_and_bind_rewritten_head_reuse(self):
        controls = {"PYTHON_VERSION": "3.14.6", "PYTHON_BIN": "python"}
        with patch.dict(os.environ, controls):
            self.content.test_same_tree_rewrite_reuses_original_without_fabricating_source_identity()
        inputs = self.content.document()["measurement"]["inputs"]["environment"]
        for name, value in controls.items():
            self.assertEqual(inputs[name], value)
            self.assertEqual(self.content.calls[0]["env"][name], value)
        with patch.dict(os.environ, {**controls, "PYTHON_BIN": "python3"}):
            self.assertEqual(self.content.execute(), 0)
        self.assertEqual(len(self.content.calls), 2)
        self.assertTrue(self.content.document()["measurement"]["executed"])

    def test_recipe_controls_do_not_change_the_unittest_contract(self):
        self.arguments.scope = common.CONTENT_SCOPE
        self.arguments.command = [
            sys.executable,
            "-m",
            "coverage",
            "run",
            "--branch",
            "--source=example",
            "-m",
            "unittest",
            "discover",
            "-s",
            "tests",
            "-v",
        ]
        with (
            patch.dict(
                os.environ, {"PYTHON_VERSION": "3.14.6", "PYTHON_BIN": "python"}
            ),
            tempfile.TemporaryDirectory() as directory,
        ):
            execution = common.content_execution(self.arguments, Path(directory))
        self.assertNotIn("PYTHON_VERSION", execution["environment"])
        self.assertNotIn("PYTHON_BIN", execution["environment"])

    def test_unqualified_configuration_keeps_stock_execution_without_evidence(self):
        for name, value in (
            ("PYTEST_ADDOPTS", "--lf"),
            ("PYTEST_PLUGINS", "example"),
            ("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1"),
            ("COVERAGE_RCFILE", "alternate"),
            ("GIT_CONFIG_GLOBAL", "alternate"),
            ("PYTHONOPTIMIZE", "1"),
        ):
            with (
                self.subTest(name=name),
                patch.dict(os.environ, {name: value}),
                patch.object(
                    self.content, "child", return_value=SimpleNamespace(returncode=0)
                ) as child,
            ):
                self.assertEqual(self.content.execute(), 0)
                self.assertEqual(child.call_args.kwargs["cwd"], self.root)
                self.assertEqual(child.call_args.kwargs["env"][name], value)
                self.assertFalse(
                    list(self.content.fixture.directory.glob("*/measurement.json"))
                )
        for name in (".env", "pytest.ini", ".pytest_cache"):
            with self.subTest(path=name):
                path = self.root / name
                path.write_text("alternate")
                with patch.object(
                    self.content, "child", return_value=SimpleNamespace(returncode=0)
                ) as child:
                    self.assertEqual(self.content.execute(), 0)
                    self.assertEqual(child.call_args.kwargs["cwd"], self.root)
                path.unlink()

    def test_setup_configuration_preserves_default_pytest_and_rejects_other_modes(self):
        path = self.root / "setup.cfg"
        for configuration, qualified in (
            ("[flake8]\nmax-line-length=140\n[tool:pytest]\naddopts=-q\n", True),
            ("[tool:pytest]\naddopts=-q --lf\n", False),
            ("[coverage:run]\nomit=tests/*\n", False),
            ("[DEFAULT]\naddopts=--lf\n", False),
        ):
            with self.subTest(configuration=configuration):
                path.write_text(configuration)
                with tempfile.TemporaryDirectory() as directory:
                    if qualified:
                        self.assertIn(
                            "root",
                            common.content_execution(self.arguments, Path(directory)),
                        )
                    else:
                        with self.assertRaises(ValueError):
                            common.content_execution(self.arguments, Path(directory))

    def test_arbitrary_command_under_profile_name_runs_stock_without_evidence(self):
        self.arguments.command = [
            sys.executable,
            "-c",
            "import os; assert os.environ['EXAMPLE_FLAG']=='kept'",
        ]
        with patch.dict(os.environ, {"EXAMPLE_FLAG": "kept"}):
            self.assertEqual(self.content.execute(), 0)
        self.assertEqual(self.content.calls[0]["cwd"], self.root)
        self.assertFalse(
            list(self.content.fixture.directory.glob("*/measurement.json"))
        )


if __name__ == "__main__":
    unittest.main()
