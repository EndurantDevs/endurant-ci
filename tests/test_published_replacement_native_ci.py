"""Run replacement proofs on the owned native database and exclude them from pure Python."""

import re
import unittest

from tests import test_membership_refresh_ci as support

NATIVE_TESTS = tuple(
    "tests/test_provider_directory_published_replacement_" + suffix + ".py"
    for suffix in (
        "postgres",
        "recovery_postgres",
        "inputs_postgres",
        "recipe_postgres",
    )
)
SERVING_BLOCK = (
    "serving_tests=()\n"
    + support.FUNCTIONS.split("  local serving_tests=() serving_test\n", 1)[1].split(
        "\n  fi", 1
    )[0]
    + "\n  fi\n"
)


class PublishedReplacementNativeChecks(unittest.TestCase):
    setUp = support.MembershipRefreshChecks.setUp
    shell = support.MembershipRefreshChecks.shell

    def populate(self):
        for name in NATIVE_TESTS:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()

    def native(self, status=0):
        return self.shell(
            'run_owned_provider_directory_postgres() { printf "%s\\n" "$@" > "$SOURCE_ROOT/native-arguments"; '
            f"return {status}; }}\n" + SERVING_BLOCK
        )

    def pure(self):
        stubs = "\n".join(
            f"{name}() {{ :; }}"
            for name in re.findall(
                r"^([a-z_]+_native_test_paths?)\(\)", support.FUNCTIONS, re.MULTILINE
            )
        )
        return self.shell(
            stubs
            + "\nmapfile() { capacity_tests=(tests/test_capacity_placeholder.py); }\nprepare_debug_rust_binaries() { :; }\n"
            "install_address_canon_wheel() { :; }\ntimeout() { :; }\n"
            'public_common_test_run() { printf "%s\\n" "$@" > "$SOURCE_ROOT/pure-arguments"; }\n'
            "run_python_main 0\n"
        )

    def test_existing_sources_remain_compatible_when_replacement_tests_are_absent(self):
        result = self.native()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertFalse((self.root / "native-arguments").exists())
        result = self.pure()
        self.assertEqual(result.returncode, 0, result.stderr)
        arguments = (self.root / "pure-arguments").read_text().splitlines()
        self.assertTrue(set(NATIVE_TESTS).isdisjoint(arguments))

    def test_present_proofs_run_once_on_native_database_and_leave_pure_lane(self):
        self.populate()
        result = self.native()
        self.assertEqual(result.returncode, 0, result.stderr)
        arguments = (self.root / "native-arguments").read_text().splitlines()
        self.assertEqual(
            arguments[:2],
            ["hc_directory_entities", "HLTHPRT_DIRECTORY_ENTITIES_TEST_DSN"],
        )
        self.assertEqual(arguments[2:], list(NATIVE_TESTS))
        result = self.pure()
        self.assertEqual(result.returncode, 0, result.stderr)
        arguments = (self.root / "pure-arguments").read_text().splitlines()
        for path in NATIVE_TESTS:
            self.assertEqual(arguments.count(path), 1)
            self.assertEqual(arguments[arguments.index(path) - 1], "--ignore")

    def test_native_refusal_propagates_without_changing_other_serving_enrollment(self):
        self.populate()
        existing = (
            self.root / "tests/test_provider_directory_serving_recovery_postgres.py"
        )
        existing.touch()
        result = self.native(status=31)
        self.assertEqual(result.returncode, 31, result.stderr)
        arguments = (self.root / "native-arguments").read_text().splitlines()
        self.assertEqual(arguments.count(str(existing.relative_to(self.root))), 1)
        self.assertEqual(arguments[3:], list(NATIVE_TESTS))


if __name__ == "__main__":
    unittest.main()
