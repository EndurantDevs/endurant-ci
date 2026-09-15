"""Require the installed CI environment to satisfy its declared inputs."""

from __future__ import annotations

import sys
from collections import deque
from collections.abc import Callable
from importlib import metadata
from pathlib import Path

try:
    from packaging.markers import default_environment
    from packaging.requirements import InvalidRequirement, Requirement
    from packaging.utils import canonicalize_name
except ModuleNotFoundError as error:
    if error.name != "packaging":
        raise
    from pip._vendor.packaging.markers import default_environment
    from pip._vendor.packaging.requirements import InvalidRequirement, Requirement
    from pip._vendor.packaging.utils import canonicalize_name

INPUT_NAMES = ("requirements.txt", "requirements-dev.txt", "requirements-ci.in")


def _input_paths(source_root: Path, policy_input: Path) -> dict[str, Path]:
    return {
        "requirements.txt": source_root / "requirements.txt",
        "requirements-dev.txt": source_root / "requirements-dev.txt",
        "requirements-ci.in": policy_input,
    }


def _declared_requirements(inputs: dict[str, Path]) -> tuple[Requirement, ...]:
    requirements: list[Requirement] = []
    visited: set[str] = set()

    def read_input(name: str) -> None:
        if name in visited:
            return
        visited.add(name)
        for raw_line in inputs[name].read_text(encoding="utf-8").splitlines():
            line = raw_line.partition("#")[0].strip()
            if not line:
                continue
            words = line.split()
            if words[0] == "-r":
                if len(words) != 2 or words[1] not in inputs:
                    raise ValueError(
                        f"{name} contains an unsupported recursive requirement"
                    )
                read_input(words[1])
                continue
            if words[0].startswith("-"):
                raise ValueError(
                    f"{name} contains an unsupported requirement directive"
                )
            try:
                requirement = Requirement(line)
            except InvalidRequirement as error:
                raise ValueError(f"{name} contains an invalid requirement") from error
            if requirement.url is not None:
                raise ValueError(
                    f"{name} contains an unsupported direct requirement URL"
                )
            requirements.append(requirement)

    for name in INPUT_NAMES:
        read_input(name)
    return tuple(requirements)


def _marker_applies(requirement: Requirement, extras: frozenset[str]) -> bool:
    if requirement.marker is None:
        return True
    environment = default_environment()
    return any(
        requirement.marker.evaluate({**environment, "extra": extra})
        for extra in (extras or {""})
    )


def _require_installed(
    requirement: Requirement,
    installed_version: Callable[[str], str],
) -> None:
    name = canonicalize_name(requirement.name)
    try:
        version = installed_version(requirement.name)
    except metadata.PackageNotFoundError as error:
        raise ValueError(f"required distribution {name} is not installed") from error
    if requirement.specifier and not requirement.specifier.contains(version):
        raise ValueError(
            f"installed distribution {name}=={version} does not satisfy its requirement"
        )


def _lock_names(lock_path: Path) -> set[str]:
    names = {
        canonicalize_name(line.partition("==")[0])
        for line in lock_path.read_text(encoding="utf-8").splitlines()
        if "==" in line and not line.startswith((" ", "#"))
    }
    if not names:
        raise ValueError("requirements-ci.lock contains no pinned distributions")
    return names


def verify_requirements(
    source_root: Path,
    policy_input: Path,
    *,
    installed_version: Callable[[str], str] = metadata.version,
    installed_requires: Callable[[str], list[str] | None] = metadata.requires,
) -> None:
    """Check declared requirements, selected extras, and their installed closure."""

    requirements = _declared_requirements(_input_paths(source_root, policy_input))
    pending = deque((requirement, frozenset()) for requirement in requirements)
    closure: set[str] = set()
    visited: set[tuple[str, str, tuple[str, ...], tuple[str, ...], str | None]] = set()

    while pending:
        requirement, parent_extras = pending.popleft()
        if not _marker_applies(requirement, parent_extras):
            continue
        key = (
            canonicalize_name(requirement.name),
            str(requirement.specifier),
            tuple(sorted(requirement.extras)),
            tuple(sorted(parent_extras)),
            None if requirement.marker is None else str(requirement.marker),
        )
        if key in visited:
            continue
        visited.add(key)
        _require_installed(requirement, installed_version)
        closure.add(canonicalize_name(requirement.name))
        for raw_requirement in installed_requires(requirement.name) or ():
            try:
                dependency = Requirement(raw_requirement)
            except InvalidRequirement as error:
                name = canonicalize_name(requirement.name)
                raise ValueError(
                    f"installed distribution {name} has an invalid dependency"
                ) from error
            if dependency.url is not None:
                name = canonicalize_name(requirement.name)
                raise ValueError(
                    f"installed distribution {name} has an unsupported direct dependency URL"
                )
            pending.append((dependency, frozenset(requirement.extras)))

    locked = _lock_names(source_root / "requirements-ci.lock")
    if locked - closure:
        raise ValueError(
            "requirements-ci.lock contains distributions outside the declared closure"
        )
    if closure - locked:
        raise ValueError(
            "requirements-ci.lock omits distributions from the declared closure"
        )


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: verify_python_requirements.py SOURCE_ROOT")
    source_root = Path(sys.argv[1]).resolve()
    policy_input = Path(__file__).resolve().with_name("requirements-ci.in")
    try:
        verify_requirements(source_root, policy_input)
    except ValueError as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
