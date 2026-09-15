"""Enforce changed-file linting plus added-file formatting and import order."""

import os
import subprocess
import sys
from pathlib import Path


def changed_python_files(base: str, diff_filter: str) -> list[str]:
    """Return NUL-delimited changed Python paths for the exact comparison range."""
    changed = subprocess.check_output([
        "git",
        "diff",
        "--no-renames",
        f"--diff-filter={diff_filter}",
        "--name-only",
        "-z",
        f"{base}..HEAD",
        "--",
        "*.py",
        "*.pyi",
    ])
    return [os.fsdecode(path) for path in changed.split(b"\0") if path]


def check_changed_files(base: str) -> None:
    """Check changed files without applying legacy repository-wide debt to a PR."""
    paths = changed_python_files(base, "AM")
    if not paths:
        print("No added or modified Python files; existing lint remains governed by readability policy.")
        return

    # Explicit paths must not bypass the source repository's reviewed exclusions.
    ruff = str(Path(sys.executable).with_name("ruff"))
    subprocess.run([ruff, "check", "--no-cache", "--force-exclude", "--", *paths], check=True)

    added = changed_python_files(base, "A")
    if not added:
        return

    subprocess.run([ruff, "check", "--no-cache", "--force-exclude", "--select", "I", "--", *added], check=True)
    subprocess.run([ruff, "format", "--check", "--no-cache", "--force-exclude", "--", *added], check=True)


if __name__ == "__main__":
    check_changed_files(sys.argv[1])
