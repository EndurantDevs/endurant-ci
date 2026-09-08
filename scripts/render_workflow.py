#!/usr/bin/env python3
"""Compile reviewed shared jobs into a public repository's prefix-free CI workflow."""

import argparse
import json
from pathlib import Path
import re

import yaml


ROOT = Path(__file__).resolve().parents[1]
METADATA_ONLY = ("github.event_name == 'pull_request' && github.event.action == 'edited' "
                 "&& !github.event.changes.title && !github.event.changes.base")
GUARD = f"!({METADATA_ONLY}) && ("


def job_name(label, matrix_job=None):
    if label == "${{ matrix.label }}":
        if not matrix_job or not re.fullmatch(r"[a-z0-9_-]+", matrix_job):
            raise ValueError("matrix metadata labels require a fixed job identifier")
        return "${{ " + METADATA_ONLY + f" && '{matrix_job} (metadata only)' || matrix.label " + "}}"
    if not re.fullmatch(r"[A-Za-z0-9 ()/._-]+", label):
        raise ValueError("job labels must be fixed public text")
    return "${{ " + METADATA_ONLY + f" && '{label} (metadata only)' || '{label}' " + "}}"


def render_workflow(kind, revision, caller):
    if kind not in {"healthcare", "drug"} or not re.fullmatch(r"[0-9a-f]{40}", revision) or set(revision) == {"0"}:
        raise ValueError("an approved workflow kind and exact nonzero package revision are required")
    workflow = yaml.safe_load(caller.read_text())
    workflow["on"] = workflow.pop(True) if True in workflow else workflow["on"]
    canonical = yaml.safe_load((ROOT / ".github/workflows" / f"{kind}.yml").read_text())
    canonical = json.loads(json.dumps(canonical).replace("${{ inputs.ci_revision }}", revision))
    workflow["permissions"] = canonical["permissions"]
    if canonical.get("env"):
        workflow["env"] = canonical["env"]
    workflow["run-name"] = "${{ " + METADATA_ONLY + " && 'CI metadata update' || 'CI' }}"
    workflow["concurrency"] = {
        "group": "${{ " + METADATA_ONLY + " && format('ci-metadata-{0}', github.run_id) || format('ci-{0}', github.ref) }}",
        "cancel-in-progress": "${{ !(" + METADATA_ONLY + ") && github.ref != 'refs/heads/main' }}",
    }
    workflow["jobs"] = {"smoke": workflow["jobs"]["smoke"], **canonical["jobs"]}
    for job_id, job in workflow["jobs"].items():
        label = job["name"]
        if label == "${{ matrix.label }}":
            matrix = job.get("strategy", {}).get("matrix", {})
            rows = matrix.get("include", [])
            if set(matrix) != {"include"} or not isinstance(rows, list) or not rows:
                raise ValueError("matrix labels must come from fixed include rows")
            labels = [row.get("label", "") for row in rows]
            if len(labels) != len(set(labels)):
                raise ValueError("matrix job labels must be unique")
            for value in labels:
                job_name(value)
        elif label.startswith("${{"):
            match = re.search(r" \|\| '([^']+)' }}$", label)
            if not match or job_name(match[1]) != label:
                raise ValueError("existing job label is not the fixed metadata expression")
            label = match[1]
        job["name"] = job_name(label, job_id)
        condition = job.get("if", "success()")
        if condition.startswith("${{") and condition.endswith("}}"):
            condition = condition[3:-2].strip()
        if condition.startswith(GUARD) and condition.endswith(")"):
            condition = condition[len(GUARD):-1]
        job["if"] = "${{ " + GUARD + condition + ") }}"
    return "# Generated from the pinned Endurance CI workflow; refresh with scripts/render_workflow.py.\n" + yaml.safe_dump(workflow, sort_keys=False, width=120)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("healthcare", "drug"))
    parser.add_argument("revision")
    parser.add_argument("caller", type=Path)
    args = parser.parse_args()
    args.caller.write_text(render_workflow(args.kind, args.revision, args.caller))
