"""Distribute exact ordinary cases without changing their native process limits."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
FUNCTIONS = (ROOT / "scripts/healthcare/check").read_text().rsplit("\ncase ", 1)[0]
MODULE = "tests/test_provider_directory_profile_initial_cutover_postgres.py"
NAME = "test_ordinary_initial_full_swap_from_undated_legacy"
NODES = tuple(f"{MODULE}::{NAME}[{outcome}]" for outcome in ("commit", "rollback", "reader"))
LEGACY = MODULE + "::test_existing_initial_case"


class OrdinaryDistributionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / MODULE
        self.path.parent.mkdir()
        self.binary = self.root / "bin"
        self.binary.mkdir()
        (self.binary / "python").symlink_to(sys.executable)
        timeout = self.binary / "timeout"
        timeout.write_text("#!/bin/sh\nexit 0\n")
        timeout.chmod(0o755)
        self.environment = {
            **os.environ,
            "SOURCE_ROOT": str(self.root), "CI_ROOT": str(ROOT),
            "PATH": str(self.binary) + os.pathsep + os.environ["PATH"],
            "HLTHPRT_DB_PASSWORD": "", "HLTHPRT_DB_DATABASE": "fixture",
        }

    def populate(self, declaration=None):
        self.path.write_text(declaration or (
            "async def test_existing_initial_case():\n    pass\n"
            '@pytest.mark.parametrize("outcome", ["commit", "rollback", "reader"])\n'
            f"async def {NAME}():\n    pass\n"
        ))

    def shell(self, code, **environment):
        return subprocess.run(
            ["bash", "-eu"], input=FUNCTIONS + "\ngit() { :; }\n" + code,
            text=True, capture_output=True, timeout=10,
            env={**self.environment, **environment},
        )

    def test_absent_and_older_module_preserve_historical_collection(self):
        for declaration in (None, "async def test_existing_initial_case():\n    pass\n"):
            if declaration is not None:
                self.populate(declaration)
            result = self.shell("profile_ordinary_initial_nodeids\n")
            self.assertEqual((result.returncode, result.stdout), (0, ""), result.stderr)

    def test_exact_three_source_cases_are_selected_without_import(self):
        self.populate()
        self.path.write_text("raise AssertionError('do not import candidate')\n" + self.path.read_text())
        result = self.shell("profile_ordinary_initial_nodeids\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), list(NODES))

    def test_partial_duplicate_synchronous_linked_and_dirty_source_refuse(self):
        self.populate()
        original = self.path.read_text()
        declarations = (
            original.replace(', "reader"', ""),
            original.replace('"rollback"', '"commit"'),
            original.replace("async def " + NAME, "def " + NAME),
            original + f"async def {NAME}():\n    pass\n",
        )
        for declaration in declarations:
            with self.subTest(declaration=declaration):
                self.populate(declaration)
                self.assertNotEqual(self.shell("profile_ordinary_initial_nodeids\n").returncode, 0)
        self.populate()
        borrowed = self.root / "borrowed.py"
        self.path.rename(borrowed)
        self.path.symlink_to(borrowed)
        self.assertNotEqual(self.shell("profile_ordinary_initial_nodeids\n").returncode, 0)
        self.path.unlink()
        self.populate()
        self.assertNotEqual(self.shell("git() { return 1; }\nprofile_ordinary_initial_nodeids\n").returncode, 0)

    def selections(self, lane):
        log = self.root / "calls.jsonl"
        log.unlink(missing_ok=True)
        code = r'''
run_available_cms_postgres() {
  python - "$CALL_LOG" "$@" <<'PY'
import json,sys
with open(sys.argv[1], "a") as stream:
    stream.write(json.dumps(sys.argv[2:]) + "\n")
PY
}
create_test_database() { :; }
drop_test_database() { :; }
timeout() { :; }
python() { if [[ "${1-}" = - ]]; then command python "$@"; fi; }
run_provider_profile_postgres fixture "$LANE"
'''
        result = self.shell(code, CALL_LOG=str(log), LANE=lane)
        self.assertEqual(result.returncode, 0, result.stderr)
        nodes = []
        lines = log.read_text().splitlines() if log.exists() else ()
        for call in (json.loads(line) for line in lines):
            selected = []
            for argument in call:
                if argument == MODULE:
                    selected.extend((LEGACY, *NODES))
                elif argument in NODES:
                    selected.append(argument)
            excluded = {argument.removeprefix("--deselect=") for argument in call
                        if argument.startswith("--deselect=")}
            nodes.extend(node for node in selected if node not in excluded)
        return nodes

    def test_selection_recorder_preserves_duplicate_arguments(self):
        self.populate()
        invocation = 'run_available_cms_postgres owned "$ordinary_initial_node"'
        duplicated = FUNCTIONS.replace(invocation, invocation + ' "$ordinary_initial_node"')
        self.assertNotEqual(duplicated, FUNCTIONS)
        with patch(__name__ + ".FUNCTIONS", duplicated):
            self.assertEqual(self.selections("registry-0"), [NODES[0], NODES[0]])

    def test_all_registry_jobs_execute_every_unchanged_case_exactly_once(self):
        self.populate()
        nodes = []
        for index in range(48):
            lane = f"registry-{index}"
            selected = self.selections(lane)
            expected = {0: [NODES[0]], 1: [NODES[1]], 5: [NODES[2]], 20: [LEGACY]}.get(index, [])
            self.assertEqual(selected, expected, lane)
            nodes.extend(selected)
        self.assertEqual(Counter(nodes), Counter((LEGACY, *NODES)))

    def test_coarse_modes_retain_the_complete_source_suite(self):
        self.populate()
        self.assertEqual(Counter(self.selections("all")), Counter((LEGACY, *NODES)))
        self.assertEqual(self.selections("profile-publication"), [])


if __name__ == "__main__":
    unittest.main()
