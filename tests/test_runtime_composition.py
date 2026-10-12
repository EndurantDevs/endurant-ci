"""The normal package builder consumes a fixed dependency and preserves legacy sources."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FUNCTIONS = (ROOT / "scripts/healthcare/check").read_text().rsplit("\ncase ", 1)[0]


class CompositionTests(unittest.TestCase):
    def test_dependency_arguments_and_failed_selection_precede_the_build(self):
        for mode in ("legacy", "selected", "missing", "empty", "blank"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / "scripts").mkdir()
                if mode != "legacy":
                    helper = root / "scripts/drug_runtime_dependency.py"
                    programs = {
                        "missing": "raise SystemExit(17)\n",
                        "empty": "pass\n",
                        "blank": "print('--build-arg\\n\\nDRUG_RUNTIME_IMAGE=synthetic@sha256:' + 'd' * 64)\n",
                        "selected": "print('--build-arg\\nDRUG_RUNTIME_IMAGE=synthetic@sha256:' + 'd' * 64)\n",
                    }
                    helper.write_text(programs[mode])
                environment = {
                    **os.environ,
                    "SOURCE_ROOT": temporary,
                    "CI_ROOT": str(ROOT),
                    "SOURCE_SHA": "a" * 40,
                    "GITHUB_RUN_ID": "1",
                    "GITHUB_RUN_ATTEMPT": "1",
                    "CAPTURE": str(root / "arguments"),
                    "PUBLIC_CI_EXPORT_IMAGE": "0",
                }
                script = (
                    FUNCTIONS
                    + r"""
git() { printf '%s\n' "$SOURCE_SHA"; }
cleanup_container_image() { return "$1"; }
docker() { printf '%s\n' "$@" > "$CAPTURE"; return 19; }
run_container_package
"""
                )
                result = subprocess.run(
                    ["bash", "-eu"],
                    input=script,
                    cwd=root,
                    env=environment,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                refused = mode in {"missing", "empty", "blank"}
                self.assertEqual(result.returncode, 2 if refused else 19, result.stderr)
                if refused:
                    self.assertFalse((root / "arguments").exists())
                else:
                    arguments = (root / "arguments").read_text()
                    self.assertEqual(
                        "DRUG_RUNTIME_IMAGE=" in arguments, mode == "selected"
                    )


if __name__ == "__main__":
    unittest.main()
