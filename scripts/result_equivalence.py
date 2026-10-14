"""Compare original complete test measurements without changing their provenance."""

import copy
import hashlib
import importlib.metadata
import importlib.util
import json
import platform
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SHA = re.compile(r"[0-9a-f]{40}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
SCHEMA = "test-result-equivalence-v1"
APP_ID = 15368


def file_inventory(root, paths):
    """Read actual reviewed recipe/configuration bytes, rather than supplied digests."""
    root = Path(root).resolve()
    require(
        isinstance(paths, (tuple, list)) and paths,
        "An execution file inventory is required",
    )
    records = []
    for name in sorted(set(paths)):
        require(
            isinstance(name, str)
            and name
            and not Path(name).is_absolute()
            and ".." not in Path(name).parts,
            "Execution file is outside its reviewed checkout",
        )
        path = root / name
        require(
            path.is_file()
            and not path.is_symlink()
            and path.resolve().is_relative_to(root),
            "Execution file is absent or resolves outside its reviewed checkout",
        )
        value = path.read_bytes()
        records.append(
            {
                "path": name,
                "mode": 0o755 if path.stat().st_mode & 0o111 else 0o644,
                "sha256": hashlib.sha256(value).hexdigest(),
                "git_blob": hashlib.sha1(
                    b"blob " + str(len(value)).encode() + b"\0" + value
                ).hexdigest(),
            }
        )
    return records


def require_source_bytecode(path):
    path = Path(path)
    require(not path.is_symlink(), "Indirect bytecode cannot establish equivalence")
    try:
        source = (
            Path(importlib.util.source_from_cache(str(path)))
            if path.parent.name == "__pycache__"
            else path.with_suffix(".py")
        )
    except ValueError as error:
        raise ValueError(
            "Unrecognized bytecode cannot establish equivalence"
        ) from error
    require(source.is_file(), "Sourceless bytecode cannot establish equivalence")


def git_execution_context(root):
    """Bind observable checkout identity until a scope proves it is irrelevant."""
    root = str(Path(root).resolve())

    def git(*arguments):
        return subprocess.check_output(["git", "-C", root, *arguments])

    head = git("rev-parse", "HEAD").decode().strip()
    require(SHA.fullmatch(head), "Execution checkout has no immutable commit")
    shallow_path = Path(git("rev-parse", "--git-path", "shallow").decode().strip())
    if not shallow_path.is_absolute():
        shallow_path = Path(root) / shallow_path
    return {
        "commit": head,
        "head": git("rev-parse", "--symbolic-full-name", "HEAD").decode().strip(),
        "references_sha256": hashlib.sha256(
            git("for-each-ref", "--format=%(refname) %(objectname) %(symref)")
        ).hexdigest(),
        "shallow": git("rev-parse", "--is-shallow-repository").decode().strip(),
        "shallow_boundary_sha256": hashlib.sha256(
            shallow_path.read_bytes() if shallow_path.is_file() else b""
        ).hexdigest(),
    }


def derive_inputs(
    source_root,
    recipe_root,
    *,
    recipe_files,
    configuration_files,
    commands,
    runner,
    trust,
    toolchains=None,
    runtime_files=(),
    allowed_outputs=(),
):
    """Collect the current checkout and resolved interpreter/dependency inputs."""
    source_root = Path(source_root).resolve()
    tracked_bytecode = subprocess.check_output(
        ["git", "-C", str(source_root), "ls-files", "-z", "--", "*.pyc", "*.pyo"],
        text=True,
    )
    for name in tracked_bytecode.split("\0"):
        if name:
            require_source_bytecode(source_root / name)
    tree = subprocess.check_output(
        ["git", "-C", str(source_root), "rev-parse", "HEAD^{tree}"], text=True
    ).strip()
    require(
        subprocess.run(
            ["git", "-C", str(source_root), "diff", "--quiet", "HEAD", "--"],
            check=False,
        ).returncode
        == 0,
        "Source checkout changed after authorization",
    )
    untracked = set(
        subprocess.check_output(
            [
                "git",
                "-C",
                str(source_root),
                "ls-files",
                "--others",
                "--exclude-standard",
                "-z",
            ],
            text=True,
        )
        .strip("\0")
        .split("\0")
    ) - {""}
    require(
        untracked <= set(runtime_files) | set(allowed_outputs),
        "Unknown untracked execution files prevent equivalence",
    )
    ignored_code = set(
        subprocess.check_output(
            [
                "git",
                "-C",
                str(source_root),
                "ls-files",
                "--others",
                "--ignored",
                "--exclude-standard",
                "-z",
                "--",
                "*.py",
                "*.pyc",
                "*.pyo",
                "*.pth",
                "*.so",
                "*.pyd",
                "*.node",
                "*.js",
                "*.cjs",
                "*.mjs",
                "*.ts",
                "*.tsx",
            ],
            text=True,
        )
        .strip("\0")
        .split("\0")
    ) - {""}
    # Normal source imports use a fresh isolated bytecode prefix during execution.
    # Reject legacy sourceless modules, which do not obey that source-cache rule.
    for name in ignored_code:
        if name.endswith((".pyc", ".pyo")):
            require_source_bytecode(source_root / name)
    # Installed environments are measured from their resolved distributions below.
    ignored_code = {
        name
        for name in ignored_code
        if Path(name).parts[0] not in {".venv", "venv", "node_modules"}
        and not name.endswith((".pyc", ".pyo"))
        and "__pycache__" not in Path(name).parts
    }
    require(
        ignored_code <= set(runtime_files) | set(allowed_outputs),
        "Ignored execution files are outside the measured runtime inventory",
    )
    require(
        isinstance(commands, list)
        and commands
        and all(
            isinstance(command, list)
            and command
            and all(isinstance(argument, str) and argument for argument in command)
            for command in commands
        ),
        "The actual complete test commands are required",
    )
    require(
        isinstance(runner, dict)
        and set(runner) == {"os", "architecture", "image"}
        and all(isinstance(value, str) and value for value in runner.values()),
        "A resolved runner image and architecture are required",
    )
    require(
        isinstance(trust, dict)
        and trust.get("application_secrets") is False
        and trust.get("permissions") == "read-only",
        "Secret-bearing execution cannot establish common-test equivalence",
    )
    dependencies = []
    for distribution in importlib.metadata.distributions():
        name, version = distribution.metadata.get("Name"), distribution.version
        require(
            isinstance(name, str) and name and isinstance(version, str) and version,
            "Resolved dependency identity is incomplete",
        )
        record = distribution.read_text("RECORD")
        require(
            isinstance(record, str) and record,
            "Resolved dependency lacks its installed file inventory",
        )
        installed_inputs = []
        require(
            distribution.files is not None,
            "Resolved dependency has no installed file list",
        )
        for entry in distribution.files:
            if str(entry).endswith((".pyc", ".pyo")):
                path = Path(distribution.locate_file(entry))
                if path.exists():
                    require_source_bytecode(path)
            else:
                path = Path(distribution.locate_file(entry))
                require(path.is_file(), "An installed dependency input is absent")
                digest = hashlib.sha256()
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                installed_inputs.append(
                    {"path": str(entry), "sha256": digest.hexdigest()}
                )
        dependencies.append(
            {
                "name": name.lower().replace("_", "-"),
                "version": version,
                "installed_inventory_sha256": hashlib.sha256(
                    record.encode()
                ).hexdigest(),
                "installed_inputs": {
                    "files": len(installed_inputs),
                    "sha256": hashlib.sha256(
                        encoded(sorted(installed_inputs, key=lambda item: item["path"]))
                    ).hexdigest(),
                },
            }
        )
    inputs = {
        "source_git": git_execution_context(source_root),
        "recipe_git": git_execution_context(recipe_root),
        "recipe": file_inventory(recipe_root, recipe_files),
        "runner": runner,
        "toolchains": {
            "python": {
                "bytecode_policy": "fresh-source-cache-v1",
                "version": sys.version,
                "implementation": sys.implementation.name,
                "platform": platform.platform(),
                "executable_sha256": hashlib.sha256(
                    Path(sys.executable).read_bytes()
                ).hexdigest(),
            },
            **(toolchains or {}),
        },
        "dependencies": sorted(
            dependencies, key=lambda item: (item["name"], item["version"])
        ),
        "commands": commands,
        "configuration": file_inventory(source_root, configuration_files),
        "trust": trust,
    }
    if runtime_files:
        inputs["compiled_inputs"] = file_inventory(source_root, runtime_files)
    return tree, inputs


def authenticate_recipe(records, repository, tree, read):
    """Compare every original recipe file to its immutable repository blob."""
    require(
        isinstance(records, list) and records and SHA.fullmatch(tree),
        "The original immutable recipe inventory is absent",
    )
    inventory = read(repository, f"git/trees/{tree}?recursive=1")
    require(
        inventory.get("sha") == tree and inventory.get("truncated") is False,
        "The original immutable recipe tree is incomplete",
    )
    entries = {entry["path"]: entry for entry in inventory["tree"]}
    for record in records:
        require(
            isinstance(record, dict)
            and set(record) == {"path", "mode", "sha256", "git_blob"},
            "The original recipe file inventory is incomplete",
        )
        path = record["path"]
        require(
            isinstance(path, str)
            and path
            and not Path(path).is_absolute()
            and ".." not in Path(path).parts,
            "The original recipe path is invalid",
        )
        value = entries.get(path, {})
        require(
            value.get("type") == "blob"
            and value.get("path") == path
            and value.get("mode") == ("100755" if record["mode"] == 0o755 else "100644")
            and value.get("sha") == record["git_blob"],
            "The original recipe bytes differ from their immutable checkout",
        )
    return True


def require(condition, message):
    if not condition:
        raise ValueError(message)


def encoded(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def fingerprint(source_tree, inputs, scope):
    """All declared execution inputs and the complete selected scope affect reuse."""
    require(
        isinstance(source_tree, str)
        and SHA.fullmatch(source_tree)
        and source_tree != "0" * 40,
        "An exact complete source tree is required",
    )
    require(
        isinstance(inputs, dict)
        and set(inputs)
        >= {
            "recipe",
            "runner",
            "toolchains",
            "dependencies",
            "commands",
            "configuration",
            "trust",
        },
        "Execution input inventory is incomplete",
    )
    require(all(inputs[name] for name in inputs), "An execution input is empty")
    require(
        isinstance(scope, dict)
        and set(scope) == {"name", "collection", "complete"}
        and isinstance(scope["name"], str)
        and scope["name"]
        and isinstance(scope["collection"], str)
        and DIGEST.fullmatch(scope["collection"])
        and scope["collection"] != "0" * 64
        and scope["complete"] is True,
        "A complete selected test inventory is required",
    )
    execution_inputs = copy.deepcopy(inputs)
    # Repository trees authenticate immutable files; unrelated files in a
    # separately checked-out recipe are not themselves execution inputs.
    # Actual selected file bytes remain in recipe/verifier_files above.
    for name in ("recipe_checkout", "verifier_checkout"):
        if name in execution_inputs:
            value = execution_inputs[name]
            require(
                isinstance(value, dict)
                and set(value) == {"repository", "tree"}
                and isinstance(value["repository"], str)
                and re.fullmatch(
                    r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value["repository"]
                )
                and isinstance(value["tree"], str)
                and SHA.fullmatch(value["tree"]),
                "Immutable execution checkout identity is incomplete",
            )
            execution_inputs[name] = {"repository": value["repository"]}
    return hashlib.sha256(
        encoded(
            {"source_tree": source_tree, "inputs": execution_inputs, "scope": scope}
        )
    ).hexdigest()


def measurement(source, inputs, scope, producer, *, executed):
    """The source identity stays separate from the inputs that determine test behavior."""
    require(
        isinstance(source, dict) and set(source) == {"repository_id", "commit", "tree"},
        "An exact source identity is required",
    )
    require(
        type(source["repository_id"]) is int
        and source["repository_id"] > 0
        and isinstance(source["commit"], str)
        and SHA.fullmatch(source["commit"])
        and source["commit"] != "0" * 40,
        "Invalid source identity",
    )
    require(
        isinstance(producer, dict)
        and set(producer)
        == {
            "repository_id",
            "commit",
            "workflow",
            "run_id",
            "run_attempt",
            "job_id",
            "step",
        },
        "An exact original producer is required",
    )
    require(
        all(
            type(producer[key]) is int and producer[key] > 0
            for key in ("repository_id", "run_id", "run_attempt", "job_id")
        )
        and isinstance(producer["commit"], str)
        and SHA.fullmatch(producer["commit"])
        and producer["commit"] != "0" * 40
        and isinstance(producer["workflow"], str)
        and re.fullmatch(
            r"\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml", producer["workflow"]
        )
        and isinstance(producer["step"], str)
        and producer["step"],
        "Invalid original producer",
    )
    require(type(executed) is bool, "Execution kind must be explicit")
    return {
        "schema": SCHEMA,
        "source": source,
        "inputs": inputs,
        "scope": scope,
        "fingerprint": fingerprint(source["tree"], inputs, scope),
        "producer": producer,
        "executed": executed,
    }


def authenticate_job_artifact(
    producer,
    run,
    job,
    check,
    artifact,
    *,
    archive_sha256,
    allowed_events,
    artifact_name,
    upload_step,
):
    """Authenticate a successful native job and its immutable upload, independently of run completion."""
    require(
        type(run.get("id")) is int
        and type(run.get("run_attempt")) is int
        and type(run.get("check_suite_id")) is int
        and run["check_suite_id"] > 0
        and run.get("id") == producer["run_id"]
        and run.get("run_attempt") == producer["run_attempt"]
        and run.get("head_sha") == producer["commit"]
        and run.get("path") == producer["workflow"]
        and run.get("repository", {}).get("id") == producer["repository_id"]
        and run.get("head_repository", {}).get("id") == producer["repository_id"]
        and run.get("event") in allowed_events,
        "The producer workflow identity differs from its measurement",
    )
    require(
        job.get("id") == producer["job_id"]
        and job.get("run_id") == producer["run_id"]
        and job.get("run_attempt") == producer["run_attempt"]
        and job.get("head_sha") == producer["commit"]
        and job.get("status") == "completed"
        and job.get("conclusion") == "success",
        "The original job is missing, failed or from another attempt",
    )
    steps = [
        step for step in job.get("steps", []) if step.get("name") == producer["step"]
    ]
    require(
        len(steps) == 1
        and steps[0].get("status") == "completed"
        and steps[0].get("conclusion") == "success",
        "The original complete test step did not run",
    )
    repository = run["repository"]["full_name"]
    require(
        type(check.get("id")) is int
        and check["id"] > 0
        and check.get("name") == job.get("name")
        and check.get("app", {}).get("id") == APP_ID
        and check.get("check_suite", {}).get("id") == run.get("check_suite_id")
        and check.get("head_sha") == producer["commit"]
        and check.get("status") == "completed"
        and check.get("conclusion") == "success"
        and job.get("check_run_url")
        == f"https://api.github.com/repos/{repository}/check-runs/{check['id']}"
        and check.get("details_url")
        == f"https://github.com/{repository}/actions/runs/{producer['run_id']}/job/{producer['job_id']}",
        "The original native check does not authenticate the full test job",
    )
    require(
        isinstance(archive_sha256, str)
        and DIGEST.fullmatch(archive_sha256)
        and type(artifact.get("id")) is int
        and artifact["id"] > 0
        and artifact.get("expired") is False
        and artifact.get("digest") == "sha256:" + archive_sha256
        and artifact.get("workflow_run", {}).get("id") == producer["run_id"]
        and artifact.get("workflow_run", {}).get("head_sha") == producer["commit"],
        "The original immutable artifact is absent or its digest changed",
    )
    uploads = [step for step in job.get("steps", []) if step.get("name") == upload_step]
    require(
        isinstance(artifact_name, str)
        and artifact_name
        and artifact.get("name") == artifact_name
        and len(uploads) == 1
        and uploads[0].get("status") == "completed"
        and uploads[0].get("conclusion") == "success"
        and uploads[0].get("number", 0) > steps[0].get("number", 0),
        "The artifact does not belong to this original complete test job",
    )
    timestamps = [
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        for value in (
            uploads[0]["started_at"],
            artifact["created_at"],
            uploads[0]["completed_at"],
        )
    ]
    require(
        all(value.tzinfo is not None for value in timestamps)
        and timestamps[0] <= timestamps[1] <= timestamps[2],
        "The artifact was not created by the authenticated upload step",
    )


def verify(
    current,
    original,
    run,
    job,
    check,
    artifact,
    *,
    archive_sha256,
    producer_recipe_verified,
    allowed_events,
    artifact_name,
    upload_step,
    now=None,
):
    """Authenticate an original execution; callers retain authority and fresh gates."""
    require(
        producer_recipe_verified is True,
        "The original execution recipe was not authenticated",
    )
    require(
        isinstance(allowed_events, (set, frozenset))
        and allowed_events
        and allowed_events <= {"push", "pull_request", "workflow_dispatch"},
        "The trusted producer event policy is absent",
    )
    for item in (current, original):
        require(
            isinstance(item, dict)
            and set(item)
            == {
                "schema",
                "source",
                "inputs",
                "scope",
                "fingerprint",
                "producer",
                "executed",
            }
            and item["schema"] == SCHEMA,
            "Unsupported or incomplete measurement",
        )
        expected = measurement(
            item["source"],
            item["inputs"],
            item["scope"],
            item["producer"],
            executed=item["executed"],
        )
        require(
            item == expected, "Measurement fingerprint differs from its complete inputs"
        )
    require(
        original["executed"] is True,
        "A reused result cannot produce another reuse result",
    )
    require(
        current["source"]["repository_id"] == original["source"]["repository_id"]
        and current["source"]["tree"] == original["source"]["tree"]
        and current["fingerprint"] == original["fingerprint"],
        "Source tree, execution inputs or complete test scope changed",
    )
    producer = original["producer"]
    require(
        current["producer"]["repository_id"] == producer["repository_id"]
        and current["producer"]["workflow"] == producer["workflow"]
        and current["producer"]["run_id"] != producer["run_id"],
        "Original and consuming workflow identities differ",
    )
    require(
        run.get("status") == "completed" and run.get("conclusion") == "success",
        "The original current workflow attempt did not fully succeed",
    )
    created = datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
    observed = now or datetime.now(timezone.utc)
    require(
        created.tzinfo is not None
        and observed - timedelta(days=1) <= created <= observed,
        "Original execution is outside the one-day reuse window",
    )
    authenticate_job_artifact(
        producer,
        run,
        job,
        check,
        artifact,
        archive_sha256=archive_sha256,
        allowed_events=allowed_events,
        artifact_name=artifact_name,
        upload_step=upload_step,
    )
    return {
        "source": copy.deepcopy(current["source"]),
        "fingerprint": current["fingerprint"],
        "origin": copy.deepcopy(original),
        "artifact_id": artifact["id"],
        "archive_sha256": archive_sha256,
    }
