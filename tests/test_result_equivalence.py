"""A different commit may consume only the same complete authenticated test inputs."""

import copy
import importlib.util
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "result_equivalence", Path(__file__).parents[1] / "scripts/result_equivalence.py"
)
equivalence = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(equivalence)


class ResultEquivalenceTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        inputs = {
            "recipe": "a" * 64,
            "runner": "linux/amd64/image-a",
            "toolchains": {"python": "3.13.1"},
            "dependencies": "b" * 64,
            "commands": ["python", "-m", "pytest"],
            "configuration": "c" * 64,
            "trust": "read-only-without-application-secrets",
        }
        scope = {"name": "unit/full", "collection": "d" * 64, "complete": True}
        producer = {
            "repository_id": 10,
            "commit": "e" * 40,
            "workflow": ".github/workflows/ci.yml",
            "run_id": 20,
            "run_attempt": 2,
            "job_id": 30,
            "step": "Run complete unit tests",
        }
        source = {"repository_id": 10, "commit": "e" * 40, "tree": "f" * 40}
        self.original = equivalence.measurement(
            source, inputs, scope, producer, executed=True
        )
        self.current = equivalence.measurement(
            {**source, "commit": "1" * 40},
            inputs,
            scope,
            {**producer, "commit": "1" * 40, "run_id": 21},
            executed=False,
        )
        self.run = {
            "id": 20,
            "run_attempt": 2,
            "head_sha": "e" * 40,
            "path": producer["workflow"],
            "repository": {"id": 10, "full_name": "example/project"},
            "head_repository": {"id": 10},
            "event": "push",
            "status": "completed",
            "conclusion": "success",
            "check_suite_id": 40,
            "created_at": self.now.isoformat(),
        }
        self.job = {
            "id": 30,
            "run_id": 20,
            "run_attempt": 2,
            "head_sha": "e" * 40,
            "name": "Unit tests",
            "status": "completed",
            "conclusion": "success",
            "check_run_url": "https://api.github.com/repos/example/project/check-runs/30",
            "steps": [
                {
                    "name": producer["step"],
                    "number": 2,
                    "status": "completed",
                    "conclusion": "success",
                },
                {
                    "name": "Upload common measurements",
                    "number": 3,
                    "status": "completed",
                    "conclusion": "success",
                    "started_at": self.now.isoformat(),
                    "completed_at": self.now.isoformat(),
                },
            ],
        }
        self.check = {
            "id": 30,
            "name": "Unit tests",
            "head_sha": "e" * 40,
            "app": {"id": 15368},
            "check_suite": {"id": 40},
            "status": "completed",
            "conclusion": "success",
            "details_url": "https://github.com/example/project/actions/runs/20/job/30",
        }
        self.artifact = {
            "id": 50,
            "name": "common-20-2-30",
            "created_at": self.now.isoformat(),
            "expired": False,
            "digest": "sha256:" + "2" * 64,
            "workflow_run": {"id": 20, "head_sha": "e" * 40},
        }

    def verify(self, **arguments):
        return equivalence.verify(
            self.current,
            self.original,
            self.run,
            self.job,
            self.check,
            self.artifact,
            archive_sha256="2" * 64,
            producer_recipe_verified=True,
            allowed_events={"push"},
            artifact_name="common-20-2-30",
            upload_step="Upload common measurements",
            now=self.now,
            **arguments,
        )

    def test_new_commit_reuses_the_same_tree_and_preserves_original_provenance(self):
        before = copy.deepcopy(self.original)
        proof = self.verify()
        self.assertEqual(proof["source"]["commit"], "1" * 40)
        self.assertEqual(proof["origin"]["source"]["commit"], "e" * 40)
        self.assertEqual(self.original, before)

    def test_each_changed_execution_input_requires_full_execution(self):
        for field in self.current["inputs"]:
            current = copy.deepcopy(self.current)
            current["inputs"][field] = "changed"
            self.current = equivalence.measurement(
                current["source"],
                current["inputs"],
                current["scope"],
                current["producer"],
                executed=False,
            )
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.verify()
            self.setUp()

    def test_source_sha_baked_into_a_native_binary_is_an_execution_input(self):
        original = copy.deepcopy(self.original)
        original["inputs"]["compiled_source"] = original["source"]["commit"]
        self.original = equivalence.measurement(
            original["source"],
            original["inputs"],
            original["scope"],
            original["producer"],
            executed=True,
        )
        current = copy.deepcopy(self.current)
        current["inputs"]["compiled_source"] = current["source"]["commit"]
        self.current = equivalence.measurement(
            current["source"],
            current["inputs"],
            current["scope"],
            current["producer"],
            executed=False,
        )
        with self.assertRaises(ValueError):
            self.verify()

    def test_rejects_reuse_chains_partial_collections_wrong_tree_and_foreign_sources(
        self,
    ):
        for group, field, value in (
            (None, "executed", False),
            ("scope", "complete", False),
            ("source", "tree", "3" * 40),
            ("source", "repository_id", 11),
        ):
            original = copy.deepcopy(self.original)
            (original[group] if group else original)[field] = value
            if group == "source":
                original = equivalence.measurement(
                    original["source"],
                    original["inputs"],
                    original["scope"],
                    original["producer"],
                    executed=True,
                )
            self.original = original
            expected = {
                None: "A reused result cannot produce another reuse result",
                "scope": "A complete selected test inventory is required",
                "source": "Source tree, execution inputs or complete test scope changed",
            }[group]
            with (
                self.subTest(group=group, field=field),
                self.assertRaisesRegex(ValueError, expected),
            ):
                self.verify()
            self.setUp()

    def test_rejects_failed_superseded_forked_and_expired_producers(self):
        for field, value in (
            ("conclusion", "failure"),
            ("run_attempt", 3),
            ("head_repository", {"id": 11}),
            ("created_at", (self.now - timedelta(days=2)).isoformat()),
        ):
            self.run[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.verify()
            self.setUp()

    def test_rejects_skipped_test_step_wrong_native_app_and_tampered_artifact(self):
        self.job["steps"][0]["conclusion"] = "skipped"
        with self.assertRaises(ValueError):
            self.verify()
        self.setUp()
        self.check["app"]["id"] = 1
        with self.assertRaises(ValueError):
            self.verify()
        self.setUp()
        self.artifact["digest"] = "sha256:" + "3" * 64
        with self.assertRaises(ValueError):
            self.verify()

    def test_rejects_artifact_from_another_job_or_outside_the_upload_window(self):
        for field, value in (
            ("name", "other-job"),
            ("created_at", (self.now + timedelta(minutes=1)).isoformat()),
        ):
            self.artifact[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.verify()
            self.setUp()


class DerivedInputTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / "recipe.py").write_text("print('tests')\n")
        subprocess.run(["git", "-C", str(self.root), "add", "recipe.py"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(self.root),
                "-c",
                "user.name=Example",
                "-c",
                "user.email=example@example.invalid",
                "commit",
                "-qm",
                "Initial recipe",
            ],
            check=True,
        )

    def derive(self, **arguments):
        values = {
            "recipe_files": ["recipe.py"],
            "configuration_files": ["recipe.py"],
            "commands": [["python", "-m", "unittest"]],
            "runner": {
                "os": "linux",
                "architecture": "x64",
                "image": "image@sha256:" + "a" * 64,
            },
            "trust": {"application_secrets": False, "permissions": "read-only"},
        }
        distributions = arguments.pop("distributions", [])
        values.update(arguments)
        with patch.object(
            equivalence.importlib.metadata, "distributions", return_value=distributions
        ):
            return equivalence.derive_inputs(self.root, self.root, **values)

    def test_reads_actual_recipe_configuration_and_git_tree(self):
        tree, values = self.derive()
        self.assertEqual(
            tree,
            subprocess.check_output(
                ["git", "-C", str(self.root), "rev-parse", "HEAD^{tree}"], text=True
            ).strip(),
        )
        self.assertEqual(
            values["recipe"], equivalence.file_inventory(self.root, ["recipe.py"])
        )
        self.assertEqual(values["configuration"], values["recipe"])

    def test_observable_git_identity_changes_even_when_full_tree_does_not(self):
        tree, original = self.derive()
        subprocess.run(
            [
                "git",
                "-C",
                str(self.root),
                "-c",
                "user.name=Example",
                "-c",
                "user.email=example@example.invalid",
                "commit",
                "--allow-empty",
                "-qm",
                "Changed identity",
            ],
            check=True,
        )
        changed_tree, changed = self.derive()
        self.assertEqual(tree, changed_tree)
        self.assertNotEqual(original["source_git"], changed["source_git"])
        before = changed["source_git"]
        subprocess.run(["git", "-C", str(self.root), "tag", "example-tag"], check=True)
        self.assertNotEqual(before, self.derive()[1]["source_git"])

    def test_unknown_untracked_code_rejected_but_declared_native_input_is_fingerprinted(
        self,
    ):
        (self.root / "native.so").write_bytes(b"compiled input")
        with self.assertRaises(ValueError):
            self.derive()
        _, values = self.derive(runtime_files=["native.so"])
        self.assertEqual(
            values["compiled_inputs"],
            equivalence.file_inventory(self.root, ["native.so"]),
        )

    def test_actual_dependency_bytes_are_measured_when_version_and_record_stay_the_same(
        self,
    ):
        (self.root / ".git/info/exclude").write_text(".venv/\n")
        dependency = self.root / ".venv/dependency.py"
        dependency.parent.mkdir()
        dependency.write_text("value = 1\n")

        class Distribution:
            version = "1.0"

            def __init__(self):
                self.metadata = {"Name": "example-dependency"}
                self.files = ["dependency.py"]

            def read_text(self, _):
                return "dependency.py,unchanged-record,10\n"

            def locate_file(self, _):
                return dependency

        _, original = self.derive(distributions=[Distribution()])
        dependency.write_text("value = 2\n")
        _, changed = self.derive(distributions=[Distribution()])
        self.assertNotEqual(original["dependencies"], changed["dependencies"])

    def test_ignored_test_or_import_code_cannot_bypass_the_input_inventory(self):
        (self.root / ".git/info/exclude").write_text("hidden.py\n")
        (self.root / "hidden.py").write_text("print('unexpected execution input')\n")
        with self.assertRaisesRegex(ValueError, "Ignored execution files"):
            self.derive()
        _, inputs = self.derive(runtime_files=["hidden.py"])
        self.assertEqual(
            inputs["compiled_inputs"],
            equivalence.file_inventory(self.root, ["hidden.py"]),
        )

    def test_sourceless_bytecode_cannot_be_an_unmeasured_import(self):
        (self.root / ".git/info/exclude").write_text("*.pyc\n")
        (self.root / "hidden.pyc").write_bytes(b"unmeasured source-free bytecode")
        with self.assertRaisesRegex(ValueError, "Sourceless bytecode"):
            self.derive()
        with self.assertRaisesRegex(ValueError, "Sourceless bytecode"):
            equivalence.require_source_bytecode(self.root / "hidden.pyc")
        (self.root / "hidden.pyc").unlink()
        cache = self.root / "__pycache__/hidden.cpython-314.pyc"
        cache.parent.mkdir()
        cache.write_bytes(b"unmeasured cache without source")
        with self.assertRaisesRegex(ValueError, "Sourceless bytecode"):
            self.derive()

    def test_rejects_dirty_recipe_and_secret_bearing_context(self):
        with self.assertRaises(ValueError):
            self.derive(trust={"application_secrets": True, "permissions": "read-only"})
        (self.root / "recipe.py").write_text("print('changed')\n")
        with self.assertRaises(ValueError):
            self.derive()


if __name__ == "__main__":
    unittest.main()
