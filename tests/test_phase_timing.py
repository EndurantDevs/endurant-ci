"""Phase diagnostics preserve shell state and the original failure status."""

from pathlib import Path
import subprocess
import unittest


HELPER = Path(__file__).resolve().parents[1] / "scripts/phase_timing.sh"


class PhaseTimingTests(unittest.TestCase):
    def run_shell(self, commands):
        return subprocess.run(
            ["bash", "-euc", 'source "$1"\n' + commands, "phase-test", str(HELPER)],
            capture_output=True, text=True, check=False,
        )

    def test_boundaries_preserve_state_and_report_failure_without_masking_it(self):
        for status in (0, 17):
            with self.subTest(status=status):
                result = self.run_shell(
                    "trap 'status=$?; ci_phase_end \"$status\"; exit \"$status\"' EXIT\n"
                    "ci_phase_begin install\n"
                    "export PHASE_TEST_VALUE=ready\n"
                    "ci_phase_end\n"
                    "test \"$PHASE_TEST_VALUE\" = ready\n"
                    "ci_phase_begin tests\n"
                    f"exit {status}\n"
                )
                self.assertEqual(result.returncode, status, result.stderr)
                self.assertRegex(result.stdout, r"end name=install elapsed_seconds=\d+ exit_code=0")
                self.assertRegex(result.stdout, rf"end name=tests elapsed_seconds=\d+ exit_code={status}")
                self.assertEqual(result.stdout.count("CI_PHASE end"), 2)

    def test_errexit_is_not_suppressed_and_empty_end_is_safe(self):
        result = self.run_shell(
            "ci_phase_end\n"
            "trap 'status=$?; ci_phase_end \"$status\"; exit \"$status\"' EXIT\n"
            "ci_phase_begin tests\n"
            "fail() { false; echo unreachable; }\nfail\necho unreachable\n"
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("exit_code=1", result.stdout)
        self.assertNotIn("unreachable", result.stdout)

    def test_labels_and_overlapping_phases_are_rejected(self):
        for commands in ("ci_phase_begin 'bad label'", "ci_phase_begin one\nci_phase_begin two"):
            with self.subTest(commands=commands):
                self.assertEqual(self.run_shell(commands).returncode, 2)
