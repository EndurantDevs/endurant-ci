"""Runtime import smoke tests fail closed without a static inference tool."""

import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "runtime_imports",
    Path(__file__).resolve().parents[1] / "scripts/runtime_imports.py",
)
IMPORTS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(IMPORTS)


class RuntimeImportTests(unittest.TestCase):
    def test_exact_source_and_real_dependency_exports(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            module = source / "synthetic_runtime_contract.py"
            module.write_text("from json import dumps\n")
            try:
                IMPORTS.check_imports(source, [module.stem], ["json:dumps"])
            finally:
                sys.modules.pop(module.stem, None)
                sys.path.remove(str(source.resolve()))

    def test_missing_module_member_and_shadowed_source_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            for modules, members, error in (
                (["synthetic_missing_runtime_module"], [], ModuleNotFoundError),
                (["json"], [], ValueError),
                ([], ["json:quality_probe_missing_member"], AttributeError),
            ):
                with self.subTest(modules=modules), self.assertRaises(error):
                    try:
                        IMPORTS.check_imports(source, modules, members)
                    finally:
                        sys.path.remove(str(source.resolve()))

    def test_dependency_canary_rejects_a_permissive_missing_member(self):
        module = types.SimpleNamespace(
            dumps=lambda: None, quality_probe_missing_member=lambda: None
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(IMPORTS.importlib, "import_module", return_value=module),
        ):
            with self.assertRaisesRegex(ValueError, "accepts a missing member"):
                try:
                    IMPORTS.check_imports(Path(directory), [], ["synthetic:dumps"])
                finally:
                    sys.path.remove(str(Path(directory).resolve()))


if __name__ == "__main__":
    unittest.main()
