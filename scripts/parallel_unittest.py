"""Run every discovered unittest exactly once across one worker process per CPU."""

import concurrent.futures
import io
import multiprocessing
import os
import sys
import time
import unittest


def names(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from names(item)
        else:
            yield item.id()


def run(name):
    stream = io.StringIO()
    started = time.monotonic()
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromName(name)
    )
    return (
        name,
        result.testsRun,
        result.wasSuccessful(),
        stream.getvalue(),
        time.monotonic() - started,
    )


def main():
    started = time.monotonic()
    # `python -m unittest` imports from the working directory, not this script's.
    sys.path[0] = os.getcwd()
    loader = unittest.defaultTestLoader
    discovered = list(names(loader.discover("tests", pattern="test_*.py")))
    if loader.errors or not discovered or len(set(discovered)) != len(discovered):
        print(*loader.errors, "Test discovery is incomplete", sep="\n", file=sys.stderr)
        return 1
    # Forked workers inherit the modules discovery imported, so every worker
    # resolves a name to the same test the complete discovery found.
    with concurrent.futures.ProcessPoolExecutor(
        max_workers=os.process_cpu_count(),
        mp_context=multiprocessing.get_context("fork"),
    ) as pool:
        results = []
        for future in concurrent.futures.as_completed(
            [pool.submit(run, name) for name in discovered]
        ):
            results.append(future.result())
            passed, output = results[-1][2:4]
            # A passing test keeps only its verbose lines; a failure keeps its report.
            print(output if not passed else output.split("\n\n-----", 1)[0], flush=True)
    failed = sorted(name for name, _, passed, _, _ in results if not passed)
    ran = sum(count for _, count, _, _, _ in results)
    print("\nSlowest tests:")
    for name, _, _, _, seconds in sorted(results, key=lambda item: -item[4])[:10]:
        print(f"{seconds:8.1f}s {name}")
    print(f"\nRan {ran} tests in {time.monotonic() - started:.3f}s")
    if failed or ran != len(discovered):
        print(f"FAILED ({ran} of {len(discovered)} ran)", *failed, sep="\n")
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
