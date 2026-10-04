"""Bound the installed pytest process group before disposable database cleanup."""

import os
from pathlib import Path
import signal
import subprocess
import sys
import time

RUN_SECONDS = 285
TERM_SECONDS = 5
DRAIN_SECONDS = 5


def active_group(group, remaining):
    """Zombies cannot use the database, even when container PID 1 leaves them unreaped."""
    if remaining <= 0:
        raise TimeoutError("process drain deadline reached")
    listing = subprocess.check_output(
        ["/bin/ps", "-axo", "pgid=,stat="], text=True,
        stderr=subprocess.DEVNULL, timeout=min(remaining, 1),
    )
    if not listing.strip():
        raise RuntimeError("process accounting unavailable")
    return any(int(pgid) == group and state[0] not in "ZX"
               for pgid, state in (line.split() for line in listing.splitlines()))


def signal_group(group, signum):
    try:
        os.killpg(group, signum)
    except ProcessLookupError:
        pass


def drain(process, deadline):
    # The leader is deliberately unreaped until the last group signal: its PID
    # cannot be recycled into an unrelated process group during cleanup.
    signal_group(process.pid, signal.SIGTERM)
    force_at = time.monotonic() + TERM_SECONDS
    killed = False
    try:
        while active_group(process.pid, deadline - time.monotonic()):
            if not killed and time.monotonic() >= force_at:
                signal_group(process.pid, signal.SIGKILL)
                killed = True
            time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        process.wait(timeout=max(0, deadline - time.monotonic()))
    except BaseException:
        signal_group(process.pid, signal.SIGKILL)
        raise


def supervise(command, receipt):
    started = time.monotonic()
    interrupted = 0
    status = None
    process = None

    def stop(signum, _frame):
        nonlocal interrupted
        interrupted = interrupted or signum

    previous = {sig: signal.signal(sig, stop)
                for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT)}
    try:
        process = subprocess.Popen(command, start_new_session=True)
        while True:
            result = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            if result is not None:
                status = result.si_status if result.si_code == os.CLD_EXITED else 128 + result.si_status
                break
            if interrupted or time.monotonic() >= started + RUN_SECONDS:
                status = 128 + interrupted if interrupted else 124
                break
            time.sleep(0.05)
    except BaseException:
        status = status or 125
    finally:
        try:
            if process is not None:
                deadline = min(started + RUN_SECONDS + TERM_SECONDS + DRAIN_SECONDS,
                               time.monotonic() + TERM_SECONDS + DRAIN_SECONDS)
                drain(process, deadline)
                Path(receipt).write_text("drained\n", encoding="ascii")
        except BaseException:
            status = status or 125
            print(f"Installed test group {process.pid} did not drain; database cleanup is disabled.", file=sys.stderr)
            # Reap a terminated leader even if another group member did not drain.
            try:
                process.wait(timeout=0)
            except subprocess.TimeoutExpired:
                pass
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
    return status or (128 + interrupted if interrupted else 0)


if __name__ == "__main__":
    sys.exit(supervise([sys.executable, "-m", "pytest", "-q", *sys.argv[2:]], sys.argv[1]))
