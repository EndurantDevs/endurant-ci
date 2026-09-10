"""Enforce Ruff formatting and import order on newly added Python files."""

import os
from pathlib import Path
import subprocess
import sys


def check_new_files(base: str) -> None:
    """Check added files against the exact target, preserving legacy readability debt."""
    changed = subprocess.check_output(
        ["git", "diff", "--no-renames", "--diff-filter=A", "--name-only", "-z",
         base, "HEAD", "--", "*.py", "*.pyi"]
    )
    paths = [os.fsdecode(path) for path in changed.split(b"\0") if path]
    if not paths:
        print("No added Python files; existing formatting remains governed by readability policy.")
        return
    # Explicit paths must not bypass the source repository's reviewed exclusions.
    ruff = str(Path(sys.executable).with_name("ruff"))
    subprocess.run([ruff, "check", "--no-cache", "--force-exclude", "--select", "I", "--", *paths], check=True)
    subprocess.run([ruff, "format", "--check", "--no-cache", "--force-exclude", "--", *paths], check=True)


if __name__ == "__main__":
    check_new_files(sys.argv[1])
