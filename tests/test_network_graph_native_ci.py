"""Keep optional network graph proofs in the measured PostgreSQL storage lane."""

import json
import re
import sys
import unittest

from tests import test_membership_refresh_ci as support

NATIVE_TEST = "tests/test_provider_directory_network_first_store_postgres.py"


class NetworkGraphNativeChecks(unittest.TestCase):
    setUp = support.MembershipRefreshChecks.setUp
    shell = support.MembershipRefreshChecks.shell

    def native(self, lane="directory-storage", status=0):
        timeout = self.root / "bin/timeout"
        timeout.write_text(
            f"#!{sys.executable}\n"
            "import json, os, pathlib, sys\n"
            "pathlib.Path(os.environ['SOURCE_ROOT'], 'native-call').write_text(json.dumps({\n"
            " 'arguments':sys.argv[1:],\n"
            " 'dsn':os.getenv('HLTHPRT_PROVIDER_DIRECTORY_PROJECTION_POSTGRES_DSN'),\n"
            " 'coverage':os.getenv('COVERAGE_FILE'), 'addopts':os.getenv('PYTEST_ADDOPTS')}))\n"
            f"sys.exit({status} if {NATIVE_TEST!r} in sys.argv else 0)\n"
        )
        timeout.chmod(0o755)
        return self.shell(
            'run_available_cms_postgres() { :; }\n'
            'postgres_block_runs() { return 1; }\n'
            f'run_provider_directory_postgres "postgresql://localhost/test_graph" "{lane}"\n',
            COVERAGE_FILE=".coverage.postgres.directory-storage",
            PYTEST_ADDOPTS="--cov=process --cov-branch --cov-append",
        )

    def pure(self):
        stubs = "\n".join(
            f"{name}() {{ :; }}"
            for name in re.findall(r"^([a-z_]+_native_test_paths?)\(\)", support.FUNCTIONS, re.MULTILINE)
        )
        return self.shell(
            stubs
            + "\nmapfile() { capacity_tests=(tests/test_capacity_placeholder.py); }\n"
            "prepare_debug_rust_binaries() { :; }\ninstall_address_canon_wheel() { :; }\n"
            'public_common_test_run() { printf "%s\\n" "$@" > "$SOURCE_ROOT/pure-arguments"; }\n'
            "run_python_main 0\n"
        )

    def populate(self):
        path = self.root / NATIVE_TEST
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()

    def test_absent_optional_proofs_preserve_older_sources(self):
        result = self.native()
        self.assertEqual(result.returncode, 0, result.stderr)
        arguments = json.loads((self.root / "native-call").read_text())["arguments"]
        self.assertNotIn(NATIVE_TEST, arguments)
        result = self.pure()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(NATIVE_TEST, (self.root / "pure-arguments").read_text().splitlines())

    def test_present_proofs_run_once_with_database_and_coverage_then_leave_pure_lane(self):
        self.populate()
        result = self.native()
        self.assertEqual(result.returncode, 0, result.stderr)
        call = json.loads((self.root / "native-call").read_text())
        self.assertEqual(call["arguments"].count(NATIVE_TEST), 1)
        self.assertEqual(call["dsn"], "postgresql://localhost/test_graph")
        self.assertEqual(call["coverage"], ".coverage.postgres.directory-storage")
        self.assertEqual(call["addopts"], "--cov=process --cov-branch --cov-append")
        result = self.pure()
        self.assertEqual(result.returncode, 0, result.stderr)
        arguments = (self.root / "pure-arguments").read_text().splitlines()
        self.assertEqual(arguments.count(NATIVE_TEST), 1)
        self.assertEqual(arguments[arguments.index(NATIVE_TEST) - 1], "--ignore")

    def test_native_failure_is_not_hidden(self):
        self.populate()
        result = self.native(status=17)
        self.assertEqual(result.returncode, 17, result.stderr)

    def test_other_lane_does_not_run_graph_proofs(self):
        self.populate()
        result = self.native(lane="directory-source-2")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / "native-call").exists())


if __name__ == "__main__":
    unittest.main()
