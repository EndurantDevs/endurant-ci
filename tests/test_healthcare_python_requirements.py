"""Exercise source-owned CI dependency closure verification."""

from __future__ import annotations

import re
import runpy
import tempfile
import unittest
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERIFY_REQUIREMENTS = runpy.run_path(
    str(ROOT / "scripts/healthcare/verify_python_requirements.py")
)["verify_requirements"]
LOCKED_NAMES = (
    "runtime",
    "redis",
    "hiredis",
    "development",
    "policy",
    "transitive",
)


def _canonicalize_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _write_inputs(source: Path, policy: Path) -> None:
    (source / "requirements.txt").write_text(
        "runtime==1\nredis[hiredis]==1\n", encoding="utf-8"
    )
    (source / "requirements-dev.txt").write_text(
        "-r requirements.txt\ndevelopment==1\n", encoding="utf-8"
    )
    policy.write_text("-r requirements-dev.txt\npolicy==1\n", encoding="utf-8")
    _write_lock(source, LOCKED_NAMES)


def _write_lock(source: Path, names: tuple[str, ...]) -> None:
    (source / "requirements-ci.lock").write_text(
        "\n".join(f"{name}==1" for name in names) + "\n", encoding="utf-8"
    )


def _installed_environment(omitted: str | None = None):
    versions = {
        "runtime": "1",
        "redis": "1",
        "hiredis": "1",
        "development": "1",
        "policy": "1",
        "transitive": "1",
    }
    if omitted is not None:
        del versions[omitted]
    dependencies = {
        "runtime": ["transitive==1"],
        "redis": ["hiredis==1; extra == 'hiredis'"],
    }

    def version(name: str) -> str:
        normalized = _canonicalize_name(name)
        if normalized not in versions:
            raise metadata.PackageNotFoundError(name)
        return versions[normalized]

    def requires(name: str) -> list[str] | None:
        return dependencies.get(_canonicalize_name(name))

    return version, requires


class SourceRequirementClosureTests(unittest.TestCase):
    def source_and_policy(self, root: Path) -> tuple[Path, Path]:
        source = root / "source"
        source.mkdir()
        policy = root / "requirements-ci.in"
        _write_inputs(source, policy)
        return source, policy

    def test_source_requirement_closure_accepts_all_declared_dependencies(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, policy = self.source_and_policy(Path(temporary))
            version, requires = _installed_environment()

            VERIFY_REQUIREMENTS(
                source, policy, installed_version=version, installed_requires=requires
            )

    def test_source_requirement_closure_rejects_omitted_dependencies(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, policy = self.source_and_policy(Path(temporary))
            for omitted in ("runtime", "development", "hiredis", "transitive"):
                with self.subTest(omitted=omitted):
                    version, requires = _installed_environment(omitted)
                    with self.assertRaisesRegex(ValueError, omitted):
                        VERIFY_REQUIREMENTS(
                            source,
                            policy,
                            installed_version=version,
                            installed_requires=requires,
                        )

    def test_source_requirement_closure_rejects_untrusted_includes(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, policy = self.source_and_policy(Path(temporary))
            (source / "requirements.txt").write_text(
                "-r unexpected.txt\n", encoding="utf-8"
            )
            version, requires = _installed_environment()

            with self.assertRaisesRegex(
                ValueError, "unsupported recursive requirement"
            ):
                VERIFY_REQUIREMENTS(
                    source,
                    policy,
                    installed_version=version,
                    installed_requires=requires,
                )

    def test_source_requirement_closure_rejects_unsatisfied_versions(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, policy = self.source_and_policy(Path(temporary))
            (source / "requirements.txt").write_text("runtime>=2\n", encoding="utf-8")
            version, requires = _installed_environment()

            with self.assertRaisesRegex(ValueError, "does not satisfy"):
                VERIFY_REQUIREMENTS(
                    source,
                    policy,
                    installed_version=version,
                    installed_requires=requires,
                )

    def test_source_requirement_closure_rejects_direct_urls(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, policy = self.source_and_policy(Path(temporary))
            (source / "requirements.txt").write_text(
                "runtime @ https://example.invalid/runtime.whl\n", encoding="utf-8"
            )
            version, requires = _installed_environment()

            with self.assertRaisesRegex(
                ValueError, "unsupported direct requirement URL"
            ):
                VERIFY_REQUIREMENTS(
                    source,
                    policy,
                    installed_version=version,
                    installed_requires=requires,
                )

    def test_source_requirement_closure_rechecks_duplicate_specifiers(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, policy = self.source_and_policy(Path(temporary))
            (source / "requirements.txt").write_text(
                "runtime>=1\nruntime>=2\n", encoding="utf-8"
            )
            _write_lock(source, ("runtime", "development", "policy", "transitive"))
            version, requires = _installed_environment()

            with self.assertRaisesRegex(ValueError, "does not satisfy"):
                VERIFY_REQUIREMENTS(
                    source,
                    policy,
                    installed_version=version,
                    installed_requires=requires,
                )

    def test_source_requirement_closure_rejects_lock_only_distributions(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, policy = self.source_and_policy(Path(temporary))
            _write_lock(source, (*LOCKED_NAMES, "unexpected"))
            version, requires = _installed_environment()

            with self.assertRaisesRegex(ValueError, "outside the declared closure"):
                VERIFY_REQUIREMENTS(
                    source,
                    policy,
                    installed_version=version,
                    installed_requires=requires,
                )

    def test_source_requirement_closure_evaluates_extra_markers_exactly(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source"
            source.mkdir()
            policy = Path(temporary) / "requirements-ci.in"
            (source / "requirements.txt").write_text(
                "conditional[foo]==1\n", encoding="utf-8"
            )
            (source / "requirements-dev.txt").write_text(
                "-r requirements.txt\n", encoding="utf-8"
            )
            policy.write_text("-r requirements-dev.txt\n", encoding="utf-8")
            _write_lock(source, ("conditional",))

            def version(name: str) -> str:
                if _canonicalize_name(name) == "conditional":
                    return "1"
                raise metadata.PackageNotFoundError(name)

            def equality_requires(name: str) -> list[str] | None:
                return ["included==1; extra == 'foo'"]

            with self.assertRaisesRegex(
                ValueError, "required distribution included is not installed"
            ):
                VERIFY_REQUIREMENTS(
                    source,
                    policy,
                    installed_version=version,
                    installed_requires=equality_requires,
                )

            def inequality_requires(name: str) -> list[str] | None:
                return ["excluded==1; extra != 'foo'"]

            VERIFY_REQUIREMENTS(
                source,
                policy,
                installed_version=version,
                installed_requires=inequality_requires,
            )
