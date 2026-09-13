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
NDC_HANDOFF_TEST = "tests/process/test_ndc_handoff_postgres.py"
NDC_HANDOFF_SOURCE_SET = (
    "process/ndc_handoff.py",
    NDC_HANDOFF_TEST,
    "tests/process/ndc_handoff_role_fixtures.py",
)


def complete_source_set(source_root: Path, names: tuple[str, ...], label: str) -> bool:
    """Inspect a closed source cohort without importing or executing its files."""
    paths = [source_root / name for name in names]
    if not any(path.exists() or path.is_symlink() for path in paths):
        return False
    if not all(path.is_file() and not path.is_symlink() for path in paths):
        raise ValueError(f"{label} source requires its native proof and fixture as regular files")
    return True


def postgres_test_paths(source_root: Path) -> tuple[str, ...]:
    """Preserve legacy proofs and include every present NDC publication/handoff cohort."""
    publication = complete_source_set(source_root, NDC_SOURCE_SET, "NDC publication")
    handoff = complete_source_set(source_root, NDC_HANDOFF_SOURCE_SET, "NDC handoff")
    if handoff and not publication:
        raise ValueError("NDC handoff requires the complete NDC publication source set")
    selected = list(BASE_TESTS)
    if publication:
        selected.append(NDC_TEST)
    if handoff:
        selected.append(NDC_HANDOFF_TEST)
    return tuple(selected)


if __name__ == "__main__":
    try:
        print("\n".join(postgres_test_paths(Path(os.environ["SOURCE_ROOT"]))))
    except ValueError as error:
        raise SystemExit(str(error)) from error
