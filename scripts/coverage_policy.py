#!/usr/bin/env python3
"""Read the public coverage contract without executing source code."""

import json
import os
from pathlib import Path
import re
import subprocess


def drug_coverage_protocol(base_policy, source_policy):
    """Select the source CLI without letting a new-policy base regress to legacy."""
    markers = []
    for policy in (base_policy, source_policy):
        if not isinstance(policy, dict):
            raise ValueError("coverage policy must be a JSON object")
        marker = policy.get("machine_artifact_required", False)
        if type(marker) is not bool:
            raise ValueError("machine_artifact_required must be a boolean")
        markers.append(marker)
    base_machine, source_machine = markers
    if base_machine and not source_machine:
        raise ValueError("candidate removed the machine coverage artifact requirement")
    return "machine" if source_machine else "legacy"


def source_coverage_protocol(root, base_sha, source_sha):
    for revision in (base_sha, source_sha):
        if not re.fullmatch(r"[0-9a-f]{40}", revision) or set(revision) == {"0"}:
            raise ValueError("coverage policy requires exact nonzero source and base SHAs")
    policies = [json.loads(subprocess.check_output(
        ["git", "show", f"{revision}:test-coverage-baseline.json"], cwd=root,
    )) for revision in (base_sha, source_sha)]
    return drug_coverage_protocol(*policies)


if __name__ == "__main__":
    print(source_coverage_protocol(
        Path(os.environ["SOURCE_ROOT"]), os.environ["BASE_SHA"], os.environ["SOURCE_SHA"],
    ))
