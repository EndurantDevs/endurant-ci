"""Enforce new Ruff diagnostics plus full formatting for added Python files."""

import hashlib
import json
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import tomllib

_FULL_SHA = re.compile(r"[0-9a-f]{40}\Z")
_RUFF_CONFIG_NAMES = frozenset({".ruff.toml", "ruff.toml", "pyproject.toml"})
_FILE_LEVEL_DIAGNOSTIC_CODES = frozenset({"I001"})
_MAX_LOG_VALUE_BYTES = 500
_APPROVED_RUFF_CONFIG_BASELINES = frozenset(
    {
        (
            "EndurantDevs/healthcare-mrf-api",
            "107ec8a4f6f54ca215fd8f087f136d2ae8419712",
            "1781395447e0560fe27d20e4221a5858acbe7c0bd85604f23620e220d530a4a4",
            "pyproject.toml",
            "f9a8c2651f848736615566fed30132858863476b24e95f21c0dd90d54191277b",
        ),
        (
            "EndurantDevs/healthcare-mrf-api",
            "107ec8a4f6f54ca215fd8f087f136d2ae8419712",
            "17f7da45c1c20cc91466ddb5814ce59804eb44de9203ecbcad337af55c431423",
            "pyproject.toml",
            "f9a8c2651f848736615566fed30132858863476b24e95f21c0dd90d54191277b",
        ),
        (
            "EndurantDevs/healthcare-mrf-api",
            "107ec8a4f6f54ca215fd8f087f136d2ae8419712",
            "86fed94f6ad4cd4f7258610bb08518f5d03b93a85ab72529311bd3b84baadc34",
            "pyproject.toml",
            "f9a8c2651f848736615566fed30132858863476b24e95f21c0dd90d54191277b",
        ),
        (
            "EndurantDevs/healthcare-mrf-api",
            "107ec8a4f6f54ca215fd8f087f136d2ae8419712",
            "720f2b4f4dfd9b625cfbe4daeac454833af05568a1a0ad7883aec61ab011c241",
            "pyproject.toml",
            "f9a8c2651f848736615566fed30132858863476b24e95f21c0dd90d54191277b",
        ),
        (
            "EndurantDevs/healthcare-mrf-api",
            "107ec8a4f6f54ca215fd8f087f136d2ae8419712",
            "2ed49cef339ff8390c2a211c1bcc9ebf880789855955d65223527f26571b3ab5",
            "pyproject.toml",
            "f9a8c2651f848736615566fed30132858863476b24e95f21c0dd90d54191277b",
        ),
    }
)


def _exact_commit(revision: str) -> str:
    """Resolve one caller-supplied full SHA to an immutable commit identity."""

    if not _FULL_SHA.fullmatch(revision):
        raise ValueError("the comparison base must be a full lowercase commit SHA")
    resolved = subprocess.check_output(
        ["git", "rev-parse", "--verify", f"{revision}^{{commit}}"]
    )
    commit = resolved.decode("ascii").strip()
    if not _FULL_SHA.fullmatch(commit):
        raise RuntimeError(
            "Git did not resolve the comparison base to a full commit SHA"
        )
    return commit


def _head_commit() -> str:
    """Snapshot HEAD once so all Git reads share the same immutable source tree."""

    resolved = subprocess.check_output(
        ["git", "rev-parse", "--verify", "HEAD^{commit}"]
    )
    commit = resolved.decode("ascii").strip()
    if not _FULL_SHA.fullmatch(commit):
        raise RuntimeError("Git did not resolve HEAD to a full commit SHA")
    return commit


def _require_base_ancestor(base: str, head: str) -> None:
    """Reject comparison ranges that would make the inherited lint baseline ambiguous."""

    completed = subprocess.run(
        ["git", "merge-base", "--is-ancestor", base, head],
        check=False,
        capture_output=True,
    )
    if completed.returncode:
        raise ValueError("the comparison base must be an ancestor of HEAD")


def changed_python_files(base: str, head: str, diff_filter: str) -> list[str]:
    """Return NUL-delimited Python paths for one exact, no-rename comparison range."""

    changed = subprocess.check_output(
        [
            "git",
            "diff",
            "--no-renames",
            f"--diff-filter={diff_filter}",
            "--name-only",
            "-z",
            f"{base}..{head}",
            "--",
            "*.py",
            "*.pyi",
        ]
    )
    return [os.fsdecode(path) for path in changed.split(b"\0") if path]


def _changed_paths(base: str, head: str) -> list[str]:
    """Return every changed path so Ruff configuration changes cannot alter their own gate."""

    changed = subprocess.check_output(
        [
            "git",
            "diff",
            "--no-renames",
            "--name-only",
            "-z",
            f"{base}..{head}",
        ]
    )
    return [os.fsdecode(path) for path in changed.split(b"\0") if path]


def _python_change_digest(base: str, head: str) -> str:
    """Bind an approved baseline to exact Python paths, modes, and blobs."""

    changed = subprocess.check_output(
        [
            "git",
            "diff",
            "--no-renames",
            "--raw",
            "--abbrev=40",
            "-z",
            f"{base}..{head}",
            "--",
            "*.py",
            "*.pyi",
        ]
    )
    return hashlib.sha256(changed).hexdigest()


def _source_at_revision(revision: str, path: str) -> bytes:
    """Read one tracked source blob without following a worktree path or symlink."""

    return subprocess.check_output(["git", "show", f"{revision}:{path}"])


def _source_if_present(revision: str, path: str) -> bytes | None:
    """Read a tracked blob when it exists at a revision, including additions and deletions."""

    listing = subprocess.check_output(["git", "ls-tree", "-z", revision, "--", path])
    if not listing:
        return None
    return _source_at_revision(revision, path)


def _ruff_pyproject_settings(content: bytes | None) -> dict | None:
    """Parse one pyproject blob so quoted TOML keys cannot bypass the policy guard."""

    if content is None:
        return None
    try:
        document = tomllib.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ValueError("changed pyproject.toml is not valid TOML") from error
    tool = document.get("tool")
    return tool.get("ruff") if isinstance(tool, dict) else None


def _has_ruff_pyproject_settings(content: bytes | None) -> bool:
    return _ruff_pyproject_settings(content) is not None


def _reviewed_ruff_version_update(before: dict | None, after: dict | None) -> bool:
    """Allow the reviewed tool upgrade without changing any lint configuration."""
    if not isinstance(before, dict) or not isinstance(after, dict):
        return False
    previous, current = dict(before), dict(after)
    versions = (previous.pop("required-version", None), current.pop("required-version", None))
    return versions == ("==0.16.6", "==0.16.8") and previous == current


def _ruff_configuration_changes(base: str, head: str) -> bool:
    """Identify changed Ruff policy files before running either side under HEAD policy."""

    for path in _changed_paths(base, head):
        name = path.rsplit("/", 1)[-1]
        if name not in _RUFF_CONFIG_NAMES:
            continue
        if name != "pyproject.toml":
            return True
        contents = (_source_if_present(base, path), _source_if_present(head, path))
        before, after = (_ruff_pyproject_settings(content) for content in contents)
        if before != after and not _reviewed_ruff_version_update(before, after):
            return True
    return False


def _approved_ruff_configuration_baseline(base: str, head: str) -> bool:
    """Admit one reviewed config with its exact inherited Python baseline."""

    changes = []
    for path in _changed_paths(base, head):
        name = path.rsplit("/", 1)[-1]
        if name not in _RUFF_CONFIG_NAMES:
            continue
        if name != "pyproject.toml":
            changes.append((path, None, None))
            continue
        before = _source_if_present(base, path)
        after = _source_if_present(head, path)
        if any(
            _has_ruff_pyproject_settings(content) for content in (before, after)
        ):
            changes.append((path, before, after))
    if len(changes) != 1:
        return False
    path, before, after = changes[0]
    return (
        before is None
        and after is not None
        and (
            os.environ.get("SOURCE_REPOSITORY")
            or os.environ.get("GITHUB_REPOSITORY", ""),
            base,
            _python_change_digest(base, head),
            path,
            hashlib.sha256(after).hexdigest(),
        )
        in _APPROVED_RUFF_CONFIG_BASELINES
    )


def _assert_ruff_configuration_unchanged(base: str, head: str) -> bool:
    """Keep source-controlled Ruff settings from suppressing a change's new findings."""

    if not _ruff_configuration_changes(base, head):
        return False
    if not _approved_ruff_configuration_baseline(base, head):
        raise ValueError(
            "Ruff configuration changes require a dedicated shared CI update"
        )
    return True


def _ruff_json_diagnostics(
    ruff: str,
    path: str,
    source: bytes,
    *,
    select: str | None = None,
) -> list[dict[str, Any]]:
    """Lint supplied Git-blob bytes under the clean HEAD checkout's reviewed Ruff policy."""

    command = [ruff, "check", "--no-cache", "--force-exclude", "--output-format=json"]
    if select is not None:
        command.extend(["--select", select])
    command.extend(["--stdin-filename", path, "-"])
    completed = subprocess.run(
        command,
        check=False,
        input=source,
        capture_output=True,
    )
    if completed.stderr:
        sys.stderr.buffer.write(completed.stderr)
    if completed.returncode not in {0, 1}:
        raise subprocess.CalledProcessError(
            completed.returncode,
            command,
            output=completed.stdout,
            stderr=completed.stderr,
        )
    try:
        diagnostics = json.loads(completed.stdout)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError("Ruff returned malformed JSON diagnostics") from exc
    if not isinstance(diagnostics, list) or any(
        not isinstance(item, dict) for item in diagnostics
    ):
        raise RuntimeError("Ruff returned malformed JSON diagnostics")
    if (completed.returncode == 0) != (not diagnostics):
        raise RuntimeError("Ruff returned an inconsistent diagnostic result")
    return diagnostics


def _diagnostic_identity(
    diagnostic: dict[str, Any], source: bytes
) -> tuple[str, str, bytes]:
    """Identify a finding by source lines, except count-only file-level diagnostics."""

    code = diagnostic.get("code")
    message = diagnostic.get("message")
    location = diagnostic.get("location")
    end_location = diagnostic.get("end_location")
    if not isinstance(code, str) or not isinstance(message, str):
        raise TypeError("Ruff returned a malformed diagnostic")
    if not isinstance(location, dict) or not isinstance(end_location, dict):
        raise TypeError("Ruff returned a malformed diagnostic")
    start_row = location.get("row")
    start_column = location.get("column")
    end_row = end_location.get("row")
    end_column = end_location.get("column")
    values = (start_row, start_column, end_row, end_column)
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 1
        for value in values
    ):
        raise TypeError("Ruff returned a malformed diagnostic")
    if end_row < start_row or (end_row == start_row and end_column < start_column):
        raise ValueError("Ruff returned a malformed diagnostic")
    lines = source.splitlines(keepends=True)
    if start_row > len(lines) or end_row > len(lines):
        raise ValueError("Ruff returned a malformed diagnostic")
    if code in _FILE_LEVEL_DIAGNOSTIC_CODES:
        return code, message, b""
    return code, message, b"".join(lines[start_row - 1 : min(end_row, len(lines))])


def _new_diagnostics(
    current: list[dict[str, Any]],
    current_source: bytes,
    baseline: list[dict[str, Any]],
    baseline_source: bytes,
) -> list[dict[str, Any]]:
    """Return source-line findings absent from the inherited baseline, retaining duplicate counts."""

    baseline_identities = Counter(
        _diagnostic_identity(item, baseline_source) for item in baseline
    )
    new_items: list[dict[str, Any]] = []
    for item in current:
        identity = _diagnostic_identity(item, current_source)
        if baseline_identities[identity]:
            baseline_identities[identity] -= 1
            continue
        new_items.append(item)
    return new_items


def _log_safe(value: str) -> str:
    """Render a bounded diagnostic fragment without allowing path or message log injection."""

    escaped = value.encode("unicode_escape", errors="backslashreplace")
    if len(escaped) > _MAX_LOG_VALUE_BYTES:
        escaped = escaped[:_MAX_LOG_VALUE_BYTES] + b"..."
    return escaped.decode("ascii")


def _fail_for_new_diagnostics(
    ruff: str, path: str, diagnostics: list[dict[str, Any]]
) -> None:
    """Print only log-safe introduced diagnostics before failing the quality stage."""

    for diagnostic in diagnostics:
        location = diagnostic["location"]
        print(
            f"{_log_safe(path)}:{location['row']}:{location['column']}: "
            f"{_log_safe(diagnostic['code'])} {_log_safe(diagnostic['message'])}",
            file=sys.stderr,
        )
    raise subprocess.CalledProcessError(
        1, [ruff, "check", "--stdin-filename", path, "-"]
    )


def _check_modified_file(ruff: str, base: str, head: str, path: str) -> None:
    """Reject lint introduced by a modified blob while retaining its exact prior-debt baseline."""

    current_source = _source_at_revision(head, path)
    baseline_source = _source_at_revision(base, path)
    current = _ruff_json_diagnostics(ruff, path, current_source)
    baseline = _ruff_json_diagnostics(ruff, path, baseline_source)
    diagnostics = _new_diagnostics(current, current_source, baseline, baseline_source)
    if diagnostics:
        _fail_for_new_diagnostics(ruff, path, diagnostics)


def _format_added_source(ruff: str, path: str, source: bytes) -> None:
    """Require an added blob to match the pinned Ruff formatter without reading its path."""

    command = [
        ruff,
        "format",
        "--check",
        "--no-cache",
        "--force-exclude",
        "--stdin-filename",
        path,
        "-",
    ]
    completed = subprocess.run(
        command,
        check=False,
        input=source,
        capture_output=True,
    )
    if completed.returncode:
        if completed.stderr:
            sys.stderr.buffer.write(completed.stderr)
        print(f"{_log_safe(path)}: Ruff format check failed", file=sys.stderr)
        raise subprocess.CalledProcessError(
            completed.returncode,
            command,
            output=completed.stdout,
            stderr=completed.stderr,
        )


def _check_added_file(ruff: str, head: str, path: str) -> None:
    """Apply default lint, import-order lint, and format checks to one new source blob."""

    source = _source_at_revision(head, path)
    for select in (None, "I"):
        diagnostics = _ruff_json_diagnostics(ruff, path, source, select=select)
        if diagnostics:
            _fail_for_new_diagnostics(ruff, path, diagnostics)
    _format_added_source(ruff, path, source)


def check_changed_files(base: str) -> None:
    """Compare modified blobs to inherited debt and fully check every added Python blob."""

    base_commit = _exact_commit(base)
    head_commit = _head_commit()
    _require_base_ancestor(base_commit, head_commit)
    if _assert_ruff_configuration_unchanged(base_commit, head_commit):
        print("Exact reviewed Ruff configuration baseline retained.")
        return
    modified = changed_python_files(base_commit, head_commit, "M")
    added = changed_python_files(base_commit, head_commit, "A")
    if not modified and not added:
        print(
            "No added or modified Python files; existing lint remains governed by readability policy."
        )
        return

    ruff = str(Path(sys.executable).with_name("ruff"))
    for path in modified:
        _check_modified_file(ruff, base_commit, head_commit, path)
    for path in added:
        _check_added_file(ruff, head_commit, path)


if __name__ == "__main__":
    check_changed_files(sys.argv[1])
