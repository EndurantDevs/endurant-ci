"""An enrolled source-only suite can reuse a real equal-tree rewritten commit."""

import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import unittest
import zipfile
from contextlib import closing
from http.client import IncompleteRead
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import test_common_test_result as fixtures

common = fixtures.common


class ContentExecutionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.CommonResultIntegrationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root.resolve()
        (self.root / "example").mkdir()
        (self.root / "example/__init__.py").write_text(
            "VALUE = 10\ndef choose(flag):\n if flag:\n  return VALUE\n return 0\n"
        )
        (self.root / "tests/test_example.py").write_text(
            "import os, unittest\nfrom pathlib import Path\nimport example\n"
            "class ExampleTests(unittest.TestCase):\n"
            " def test_content(self):\n"
            "  self.assertEqual(example.VALUE, 10)\n"
            "  self.assertEqual(example.choose(True), 10)\n"
            "  self.assertEqual(example.choose(False), 0)\n"
            "  self.assertFalse(Path('.git').exists())\n"
            "  self.assertFalse(set(os.environ) & {'GITHUB_SHA', 'SOURCE_SHA', "
            "'BASE_SHA', 'HTTP_PROXY', 'ARBITRARY_FEATURE', 'GH_TOKEN'})\n"
        )
        self.fixture.git("add", ".")
        self.fixture.git(
            "-c",
            "user.name=Example",
            "-c",
            "user.email=example@example.invalid",
            "commit",
            "-qm",
            "Add content suite",
        )
        self.fixture.source_sha = self.fixture.git("rev-parse", "HEAD").strip()
        self.fixture.tree = self.fixture.git("rev-parse", "HEAD^{tree}").strip()
        for value in (
            self.fixture.producer,
            self.fixture.run,
            self.fixture.job,
            self.fixture.check,
        ):
            key = "commit" if value is self.fixture.producer else "head_sha"
            value[key] = self.fixture.source_sha
        self.fixture.current_producer.return_value = (
            self.fixture.producer,
            self.fixture.run,
        )
        self.arguments = self.fixture.arguments
        self.arguments.scope = common.CONTENT_SCOPE
        self.arguments.output_file = [".coverage"]
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
        self.calls = []

    def child(self, *arguments, check=False, **keywords):
        self.calls.append({**keywords, "check": check})
        return subprocess.run(*arguments, check=check, **keywords)

    def execute(self):
        return common.execute(self.arguments, read=self.fixture.read, run=self.child)

    def document(self):
        return json.loads(
            next(self.fixture.directory.glob("*/measurement.json")).read_text()
        )

    def original(self):
        self.assertEqual(self.execute(), 0)
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            for path in self.fixture.directory.rglob("*"):
                if path.is_file():
                    archive.writestr(
                        path.relative_to(self.fixture.directory).as_posix(),
                        path.read_bytes(),
                    )
        self.fixture.archive = data.getvalue()
        self.fixture.current_producer.return_value = (
            {**self.fixture.producer, "run_id": 21},
            self.fixture.run,
        )

    def test_same_tree_rewrite_reuses_original_without_fabricating_source_identity(
        self,
    ):
        with patch.dict(
            os.environ,
            {"HTTP_PROXY": "http://invalid.test", "ARBITRARY_FEATURE": "first"},
        ):
            self.original()
        original = self.document()
        self.fixture.git(
            "-c",
            "user.name=Example",
            "-c",
            "user.email=example@example.invalid",
            "commit",
            "--allow-empty",
            "-qm",
            "Rewrite content identity",
        )
        current_sha = self.fixture.git("rev-parse", "HEAD").strip()
        self.assertNotEqual(current_sha, self.fixture.source_sha)
        self.assertEqual(
            self.fixture.git("rev-parse", "HEAD^{tree}").strip(), self.fixture.tree
        )
        self.fixture.current_producer.return_value = (
            {**self.fixture.producer, "commit": current_sha, "run_id": 21},
            self.fixture.run,
        )
        with patch.dict(
            os.environ,
            {
                "GITHUB_SHA": current_sha,
                "SOURCE_SHA": current_sha,
                "BASE_SHA": "c" * 40,
                "ARBITRARY_FEATURE": "second",
            },
        ):
            self.assertEqual(self.execute(), 0)
        current = self.document()
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(current["measurement"]["executed"])
        self.assertEqual(current["measurement"]["source"]["commit"], current_sha)
        self.assertEqual(
            current["origin"]["origin"]["source"]["commit"], self.fixture.source_sha
        )
        self.assertEqual(
            current["measurement"]["fingerprint"],
            original["measurement"]["fingerprint"],
        )
        inputs = current["measurement"]["inputs"]
        self.assertFalse(
            set(inputs)
            & {"source_git", "recipe_git", "verifier_git", "identity_environment"}
        )
        self.assertEqual(inputs["execution_profile"], self.arguments.scope)
        self.assertFalse(Path(self.calls[0]["cwd"]).exists())

    def test_changed_admitted_environment_requires_complete_execution(self):
        with patch.dict(os.environ, {"TZ": "UTC"}):
            self.original()
        with patch.dict(os.environ, {"TZ": "Etc/GMT-1"}):
            self.assertEqual(self.execute(), 0)
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(self.document()["measurement"]["executed"])

    def test_exported_coverage_is_reportable_after_the_archived_source_is_removed(self):
        self.assertEqual(self.execute(), 0)
        self.assertFalse(Path(self.calls[0]["cwd"]).exists())
        report = self.root.parent / "coverage.json"
        subprocess.run(
            [sys.executable, "-m", "coverage", "json", "-o", str(report)],
            cwd=self.root,
            check=True,
            capture_output=True,
        )
        document = json.loads(report.read_text())
        self.assertEqual(set(document["files"]), {"example/__init__.py"})
        self.assertEqual(
            document["files"]["example/__init__.py"]["executed_lines"], [1, 2, 3, 4, 5]
        )
        retained = next(self.fixture.directory.glob("*/data/0"))
        with (
            closing(sqlite3.connect(retained)) as original,
            closing(sqlite3.connect(self.root / ".coverage")) as current,
        ):
            self.assertEqual(
                original.execute("SELECT path FROM file").fetchall(),
                [("example/__init__.py",)],
            )
            self.assertEqual(
                current.execute("SELECT path FROM file").fetchall(),
                [(str(self.root / "example/__init__.py"),)],
            )
            for table in (
                "coverage_schema",
                "meta",
                "context",
                "line_bits",
                "arc",
                "tracer",
            ):
                with self.subTest(table=table):
                    self.assertEqual(
                        original.execute(f"SELECT * FROM {table}").fetchall(),
                        current.execute(f"SELECT * FROM {table}").fetchall(),
                    )
            self.assertTrue(original.execute("SELECT * FROM arc").fetchall())

    def test_coverage_relocation_rejects_unmatched_rows_without_dropping_data(self):
        self.assertEqual(self.execute(), 0)
        path = self.root / ".coverage"
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute(
                "INSERT INTO file(path) VALUES(?)", ("/outside/dependency.py",)
            )
            before = connection.execute(
                "SELECT id,path FROM file ORDER BY id"
            ).fetchall()
        with self.assertRaises(ValueError):
            common.coverage_paths(path, original=self.root)
        with closing(sqlite3.connect(path)) as connection:
            self.assertEqual(
                connection.execute("SELECT id,path FROM file ORDER BY id").fetchall(),
                before,
            )

    def test_missing_evidence_runs_the_same_archived_command_and_environment(self):
        with (
            patch.dict(
                os.environ,
                {"GITHUB_SHA": "a" * 40, "HTTP_PROXY": "http://invalid.test"},
            ),
            patch.object(
                common,
                "current_producer",
                side_effect=ValueError("Evidence unavailable"),
            ),
        ):
            self.assertEqual(self.execute(), 0)
        self.assertEqual(len(self.calls), 1)
        self.assertNotEqual(self.calls[0]["cwd"], self.root)
        self.assertNotIn("HTTP_PROXY", self.calls[0]["env"])
        self.assertNotIn("GITHUB_SHA", self.calls[0]["env"])
        self.assertTrue((self.root / ".coverage").is_file())
        self.assertFalse(list(self.fixture.directory.glob("*/measurement.json")))

    def test_truncated_direct_download_runs_the_complete_content_command_once(self):
        self.original()
        self.assertIsNone(self.arguments.evidence_directory)
        with patch.object(common, "download", side_effect=IncompleteRead(b"partial")):
            self.assertEqual(self.execute(), 0)
        self.assertEqual(len(self.calls), 2)
        self.assertNotEqual(self.calls[-1]["cwd"], self.root)
        self.assertNotIn("GH_TOKEN", self.calls[-1]["env"])
        self.assertTrue(self.document()["measurement"]["executed"])
        self.assertIsNone(self.document()["origin"])

    def test_truncated_direct_run_inventory_executes_without_reuse_evidence(self):
        self.original()
        with patch.object(common, "api", side_effect=IncompleteRead(b"partial")):
            self.assertEqual(self.execute(), 0)
        self.assertEqual(len(self.calls), 2)
        self.assertNotEqual(self.calls[-1]["cwd"], self.root)
        self.assertNotIn("GH_TOKEN", self.calls[-1]["env"])
        self.assertFalse(list(self.fixture.directory.glob("*/measurement.json")))

    def test_unqualified_scope_keeps_stock_environment_and_creates_no_reuse_record(
        self,
    ):
        self.arguments.scope = "ordinary-unit"
        self.arguments.command = [
            sys.executable,
            "-c",
            "import os; assert os.environ['ARBITRARY_FEATURE']=='kept'",
        ]
        stale = (
            self.fixture.directory
            / hashlib.sha256(self.arguments.scope.encode()).hexdigest()
        )
        stale.mkdir(parents=True)
        (stale / "measurement.json").write_text("stale")
        with patch.dict(os.environ, {"ARBITRARY_FEATURE": "kept"}):
            self.assertEqual(self.execute(), 0)
        self.assertEqual(self.calls[0]["cwd"], self.root.resolve())
        self.assertEqual(self.calls[0]["env"]["ARBITRARY_FEATURE"], "kept")
        self.assertFalse(stale.exists())

    def test_ambient_runtime_overrides_keep_stock_unittest_without_reuse_evidence(self):
        for name, value in (
            ("COVERAGE_RCFILE", "alternate"),
            ("COVERAGE_PROCESS_START", "alternate"),
            ("PYTHONOPTIMIZE", "1"),
            ("PYTHONNOUSERSITE", "0"),
            ("PYTEST_PLUGINS", "example"),
            ("GIT_CONFIG_GLOBAL", "alternate"),
        ):
            with (
                self.subTest(name=name),
                patch.dict(os.environ, {name: value}),
                patch.object(
                    self, "child", return_value=SimpleNamespace(returncode=0)
                ) as child,
            ):
                self.assertEqual(self.execute(), 0)
                self.assertEqual(child.call_args.kwargs["cwd"], self.root)
                self.assertEqual(child.call_args.kwargs["env"][name], value)
                self.assertFalse(
                    list(self.fixture.directory.glob("*/measurement.json"))
                )

    def test_unqualified_command_does_not_silently_change_its_environment(self):
        self.arguments.command = [
            sys.executable,
            "-c",
            "import os; assert os.environ['ARBITRARY_FEATURE']=='kept'",
        ]
        with patch.dict(os.environ, {"ARBITRARY_FEATURE": "kept"}):
            self.assertEqual(self.execute(), 0)
        self.assertEqual(self.calls[0]["cwd"], self.root.resolve())
        self.assertFalse(list(self.fixture.directory.glob("*/measurement.json")))


if __name__ == "__main__":
    unittest.main()
