"""Select native Drug proofs without executing source code or changing its checkout."""

import os
from pathlib import Path

BASE_TESTS = (
    "tests/process/test_import_table_switching.py",
    "tests/process/test_ndc_rxnorm_mapping.py",
    "tests/process/test_drug_indications.py",
)
NDC_TEST = "tests/process/test_ndc_publication_proof_postgres.py"
NDC_SOURCE_SET = (
    "process/ndc_stage.py",
    NDC_TEST,
    "tests/process/ndc_publication_fixtures.py",
)


def postgres_test_paths(source_root: Path) -> tuple[str, ...]:
    """Keep legacy selection; require the complete proof when any new NDC part exists."""
    ndc_paths = [source_root / name for name in NDC_SOURCE_SET]
    if not any(path.exists() or path.is_symlink() for path in ndc_paths):
        return BASE_TESTS
    if not all(path.is_file() and not path.is_symlink() for path in ndc_paths):
        raise ValueError("NDC publication source requires its native proof and fixture as regular files")
    return (*BASE_TESTS, NDC_TEST)


if __name__ == "__main__":
    try:
        print("\n".join(postgres_test_paths(Path(os.environ["SOURCE_ROOT"]))))
    except ValueError as error:
        raise SystemExit(str(error)) from error
