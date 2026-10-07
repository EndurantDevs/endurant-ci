"""Smoke-test exact source modules and dependency exports in the locked runtime."""

import argparse
import importlib
import sys
from pathlib import Path


def check_imports(source: Path, modules: list[str], members: list[str]) -> None:
    """Reject missing source imports, shadow modules, and unavailable dependency APIs."""
    source = source.resolve()
    sys.path.insert(0, str(source))
    for name in modules:
        module = importlib.import_module(name)
        expected = source.joinpath(*name.split(".")).with_suffix(".py")
        if Path(module.__file__).resolve() != expected:
            raise ValueError(f"source module {name} does not match its checkout")
    for reference in members:
        name, member = reference.split(":", 1)
        module = importlib.import_module(name)
        if not callable(getattr(module, member)):
            raise TypeError(f"dependency export {reference} is not callable")
        try:
            getattr(module, "quality_probe_missing_member")
        except AttributeError:
            pass
        else:
            raise ValueError(f"dependency module {name} accepts a missing member")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("modules", nargs="+")
    parser.add_argument("--source", type=Path, default=Path.cwd())
    parser.add_argument("--member", action="append", default=[])
    args = parser.parse_args()
    check_imports(args.source, args.modules, args.member)
