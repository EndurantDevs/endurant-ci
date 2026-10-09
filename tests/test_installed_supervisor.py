"""Real bounded process trees and fail-closed installed-test cleanup contracts."""

import importlib.util
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts/healthcare/supervise_installed.py"
spec = importlib.util.spec_from_file_location("installed_supervisor", HELPER)
supervisor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(supervisor)

TREE = """
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

role, mode, directory = sys.argv[1:]
root = Path(directory)
if role != "controller":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
(root / (role + ".pid")).write_text(str(os.getpid()))
if role != "cli":
    child = subprocess.Popen([sys.executable, __file__,
                              "worker" if role == "controller" else "cli", mode, directory])
if role == "controller":
    while not (root / "cli.pid").exists():
        time.sleep(0.01)
    if mode == "exit":
        os._exit(17)
    if mode == "interrupt":
        os.kill(os.getppid(), signal.SIGTERM)
while True:
    time.sleep(0.1)
"""

RUNNER = """
import importlib.util
from pathlib import Path
import sys
spec = importlib.util.spec_from_file_location("supervisor", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.RUN_SECONDS, module.TERM_SECONDS, module.DRAIN_SECONDS = 1, 0.1, 1
sys.exit(module.supervise([sys.executable, sys.argv[2], "controller", sys.argv[3], sys.argv[4]],
                         Path(sys.argv[4]) / "drain"))
"""


class InstalledSupervisorTests(unittest.TestCase):
    def test_fixed_budget_and_zombie_accounting(self):
        self.assertEqual(supervisor.RUN_SECONDS + supervisor.TERM_SECONDS + supervisor.DRAIN_SECONDS, 295)
        for listing, active in (("42 Z\n7 S\n", False), ("42 Z\n42 S\n", True), ("42 X\n", False)):
            with self.subTest(listing=listing), patch.object(supervisor.subprocess, "check_output", return_value=listing):
                self.assertEqual(supervisor.active_group(42, 1), active)
        with patch.object(supervisor.subprocess, "check_output", return_value=""), self.assertRaises(RuntimeError):
            supervisor.active_group(42, 1)

    def test_controller_is_observed_without_reaping_until_group_cleanup(self):
        for status, code, expected in ((0, os.CLD_EXITED, 0), (17, os.CLD_EXITED, 17),
                                      (signal.SIGKILL, os.CLD_KILLED, 128 + signal.SIGKILL)):
            process = Mock(pid=42)
            exited = SimpleNamespace(si_status=status, si_code=code)
            events = []
            process.wait.side_effect = lambda **kwargs: events.append("reap")
            with self.subTest(status=status, code=code), tempfile.TemporaryDirectory() as temporary, \
                    patch.object(supervisor.subprocess, "Popen", return_value=process) as spawn, \
                    patch.object(supervisor.os, "waitid", return_value=exited) as observe, \
                    patch.object(supervisor, "active_group", return_value=False), \
                    patch.object(supervisor, "signal_group", side_effect=lambda *args: events.append("signal")):
                receipt = Path(temporary) / "drain"
                self.assertEqual(supervisor.supervise(["synthetic"], receipt), expected)
                spawn.assert_called_once_with(["synthetic"], start_new_session=True)
                observe.assert_called_once_with(os.P_PID, 42, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                self.assertEqual(events, ["signal", "reap"])
                self.assertEqual(receipt.read_text(), "drained\n")

    def test_accounting_failure_kills_group_preserves_primary_and_withholds_receipt(self):
        for status, failure in ((17, RuntimeError("unavailable")), (0, TimeoutError("deadline reached"))):
            process = Mock(pid=42)
            exited = SimpleNamespace(si_status=status, si_code=os.CLD_EXITED)
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temporary, \
                    patch.object(supervisor.subprocess, "Popen", return_value=process), \
                    patch.object(supervisor.os, "waitid", return_value=exited), \
                    patch.object(supervisor, "active_group", side_effect=failure), \
                    patch.object(supervisor, "signal_group") as send, patch.object(supervisor, "print"):
                receipt = Path(temporary) / "drain"
                self.assertEqual(supervisor.supervise(["synthetic"], receipt), status or 125)
                self.assertFalse(receipt.exists())
                self.assertEqual([call.args[:2] for call in send.call_args_list], [(42, signal.SIGTERM), (42, signal.SIGKILL)])
                self.assertEqual(send.call_args_list[0].args[2], send.call_args_list[1].args[2])
                process.wait.assert_called_once_with(timeout=0)

    def test_permission_refusal_requires_no_active_members_within_the_same_deadline(self):
        for signum in (signal.SIGTERM, signal.SIGKILL):
            for state in (False, True, RuntimeError("accounting unavailable"), TimeoutError("expired")):
                refusal = PermissionError("synthetic signal refusal")
                with self.subTest(signum=signum, state=state), \
                        patch.object(supervisor.os, "killpg", side_effect=refusal), \
                        patch.object(supervisor.time, "monotonic", return_value=10), \
                        patch.object(supervisor, "active_group") as active:
                    if isinstance(state, Exception):
                        active.side_effect = state
                        with self.assertRaises(type(state)):
                            supervisor.signal_group(42, signum, 12)
                    elif state:
                        active.return_value = True
                        with self.assertRaises(PermissionError) as raised:
                            supervisor.signal_group(42, signum, 12)
                        self.assertIs(raised.exception, refusal)
                    else:
                        active.return_value = False
                        supervisor.signal_group(42, signum, 12)
                    active.assert_called_once_with(42, 2)

    def test_exited_child_without_descendants_is_reaped_after_verified_drain(self):
        for status in (0, 17):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temporary:
                receipt = Path(temporary) / "drain"
                self.assertEqual(supervisor.supervise([sys.executable, "-c", f"raise SystemExit({status})"], receipt), status)
                self.assertEqual(receipt.read_text(), "drained\n")

    def test_signal_refusal_without_complete_accounting_withholds_drain_receipt(self):
        for state in (True, RuntimeError("accounting unavailable"), TimeoutError("expired")):
            process = Mock(pid=42)
            exited = SimpleNamespace(si_status=0, si_code=os.CLD_EXITED)
            with self.subTest(state=state), tempfile.TemporaryDirectory() as temporary, \
                    patch.object(supervisor.subprocess, "Popen", return_value=process), \
                    patch.object(supervisor.os, "waitid", return_value=exited), \
                    patch.object(supervisor.os, "killpg", side_effect=PermissionError("synthetic refusal")), \
                    patch.object(supervisor, "active_group") as active, patch.object(supervisor, "print"):
                if isinstance(state, Exception):
                    active.side_effect = state
                else:
                    active.return_value = state
                receipt = Path(temporary) / "drain"
                self.assertEqual(supervisor.supervise(["synthetic"], receipt), 125)
                self.assertFalse(receipt.exists())
                process.wait.assert_called_once_with(timeout=0)

    def _cleanup_tree(self, root, tree):
        receipt = root / "controller.pid"
        if not receipt.exists():
            return
        group = int(receipt.read_text())
        listing = subprocess.check_output(["/bin/ps", "-axo", "pgid=,args="], text=True, timeout=1)
        # A fallback signal requires this exact test-owned script in the group.
        if any(int(fields[0]) == group and str(tree) in fields[1]
               for line in listing.splitlines() if len(fields := line.split(maxsplit=1)) == 2):
            supervisor.signal_group(group, signal.SIGKILL, time.monotonic() + 1)
        deadline = time.monotonic() + 1
        while supervisor.active_group(group, deadline - time.monotonic()):
            time.sleep(0.02)

    def test_dead_controller_does_not_leave_term_resistant_worker_or_cli(self):
        for mode, expected in (("exit", 17), ("timeout", 124), ("interrupt", 128 + signal.SIGTERM)):
            with self.subTest(mode=mode):
                temporary = tempfile.TemporaryDirectory()
                self.addCleanup(temporary.cleanup)
                root = Path(temporary.name)
                tree = root / "tree.py"
                self.addCleanup(self._cleanup_tree, root, tree)
                tree.write_text(TREE)
                result = subprocess.run(
                    [sys.executable, "-c", RUNNER, str(HELPER), str(tree), mode, str(root)],
                    capture_output=True, text=True, timeout=6,
                )
                self.assertEqual(result.returncode, expected, result.stderr)
                self.assertEqual((root / "drain").read_text(), "drained\n")
                self.assertTrue(all((root / f"{role}.pid").is_file() for role in ("controller", "worker", "cli")))
                self.assertFalse(supervisor.active_group(int((root / "controller.pid").read_text()), 1))


if __name__ == "__main__":
    unittest.main()
