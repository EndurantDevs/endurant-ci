#!/usr/bin/env python3
"""Execute common tests, or authenticate an original equivalent complete result."""

import argparse
import configparser
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from contextlib import closing
from datetime import datetime, timedelta, timezone
from http.client import HTTPException
from pathlib import Path

import tomllib

# Load proof helpers from source even when an earlier process left valid caches.
_HELPER_CACHE = tempfile.TemporaryDirectory(prefix="common-test-helper-cache-")
sys.pycache_prefix = _HELPER_CACHE.name
sys.dont_write_bytecode = True

import result_equivalence as equivalent

UPLOAD_STEP = "Upload verified common-test measurements"
MAX_BYTES = 256 * 1024 * 1024
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
CONTENT_SCOPE = "python-unittest-content-v1"
PYTEST_CONTENT_SCOPE = "python-pytest-content-v1"
CONTENT_ENVIRONMENT = (
    "PATH",
    "LANG",
    "LANGUAGE",
    "LC_ALL",
    "LC_CTYPE",
    "LC_COLLATE",
    "LC_MESSAGES",
    "LC_MONETARY",
    "LC_NUMERIC",
    "LC_TIME",
    "TZ",
    "PYTHONHASHSEED",
    "PYTHONUTF8",
    "PYTHONIOENCODING",
    "PYTHONWARNINGS",
    "PYTHONDEVMODE",
    "PYTHONFAULTHANDLER",
)


def content_execution(arguments, temporary):
    """Prepare an explicitly enrolled complete source-only Python test contract."""
    root = arguments.source_root.resolve()
    command = arguments.command
    pytest = arguments.scope == PYTEST_CONTENT_SCOPE
    environment_names = CONTENT_ENVIRONMENT + (
        ("PYTHON_VERSION", "PYTHON_BIN") if pytest else ()
    )
    project = root / "pyproject.toml"
    configuration = tomllib.loads(project.read_text()) if project.is_file() else {}
    shape = (
        command[1:]
        == ["-m", "coverage", "run", "--branch", "--source=.", "-m", "pytest", "-q"]
        if pytest
        else len(command) == 12
        and command[1:5] == ["-m", "coverage", "run", "--branch"]
        and re.fullmatch(r"--source=[A-Za-z_][A-Za-z0-9_]*", command[5])
        and command[6:] == ["-m", "unittest", "discover", "-s", "tests", "-v"]
    )
    equivalent.require(
        command
        and shape
        and Path(shutil.which(command[0]) or command[0]).absolute()
        == Path(sys.executable).absolute()
        and arguments.data_root.resolve() == root
        and arguments.output_file == [".coverage"]
        and arguments.collection == "tests"
        and not arguments.runtime_file
        and not arguments.allowed_source_output
        and not os.environ.get("PYTHONHOME")
        and not os.environ.get("PYTHONPATH")
        and not any((root / name).exists() for name in (".coveragerc", "tox.ini"))
        and (pytest or not (root / "setup.cfg").exists())
        and "coverage" not in configuration.get("tool", {}),
        "Command or runtime is outside the enrolled content-only test contract",
    )
    equivalent.require(
        not any(
            name.startswith(("PYTEST_", "COVERAGE_"))
            or (name.startswith("GIT_") and name != "GIT_PAGER")
            or (
                name.startswith(("PYTHON", "_PYTHON_"))
                and name
                not in (
                    *environment_names,
                    # Recipe labels do not configure the running interpreter.
                    "PYTHON_VERSION",
                    "PYTHON_BIN",
                    "PYTHONPYCACHEPREFIX",
                    "PYTHONDONTWRITEBYTECODE",
                    "PYTHONNOUSERSITE",
                )
            )
            for name in os.environ
        )
        and os.environ.get("PYTHONNOUSERSITE", "1") == "1",
        "Ambient runtime configuration is outside the enrolled test contract",
    )
    if pytest:
        setup = configparser.ConfigParser(interpolation=None)
        setup.read(root / "setup.cfg")
        equivalent.require(
            not any(
                (root / name).exists()
                for name in (".env", ".pytest_cache", "pytest.ini")
            )
            and configuration.get("tool", {}).get("pytest", {})
            in ({}, {"ini_options": {"addopts": "-q"}})
            and not setup.defaults()
            and not any(name.startswith("coverage:") for name in setup.sections())
            and (dict(setup["tool:pytest"]) if setup.has_section("tool:pytest") else {})
            in ({}, {"addopts": "-q"}),
            "Pytest configuration is outside the enrolled default execution contract",
        )
    for distribution in equivalent.importlib.metadata.distributions():
        direct = distribution.read_text("direct_url.json")
        equivalent.require(
            not direct or not json.loads(direct).get("dir_info", {}).get("editable"),
            "Editable dependencies cannot establish source-only equivalence",
        )
    destination = temporary / "source"
    destination.mkdir()
    size = 0
    for name in tracked(root):
        source = root / name
        equivalent.require(
            source.is_file()
            and not source.is_symlink()
            and source.resolve().is_relative_to(root),
            "Content-only source contains an indirect or missing tracked input",
        )
        value = source.read_bytes()
        size += len(value)
        equivalent.require(size <= MAX_BYTES, "Content-only source exceeds its limit")
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(value)
        target.chmod(0o755 if source.stat().st_mode & 0o111 else 0o644)
        os.utime(target, (0, 0))
    for directory in [
        destination,
        *(path for path in destination.rglob("*") if path.is_dir()),
    ]:
        os.utime(directory, (0, 0))
    for name in ("home", "tmp", "bytecode"):
        (temporary / name).mkdir()
    if pytest:
        (temporary / "git-templates").mkdir()
    # This is a new execution contract; unqualified commands keep their stock environment.
    environment = {
        name: os.environ[name] for name in environment_names if name in os.environ
    }
    environment.update(
        HOME=str(temporary / "home"),
        TMPDIR=str(temporary / "tmp"),
        TEMP=str(temporary / "tmp"),
        TMP=str(temporary / "tmp"),
        COVERAGE_FILE=str(destination / ".coverage"),
        PYTHONNOUSERSITE="1",
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONPYCACHEPREFIX=str(temporary / "bytecode"),
        GIT_CEILING_DIRECTORIES=str(temporary),
    )
    tools = {}
    if pytest:
        environment.update(
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_ATTR_NOSYSTEM="1",
            GIT_TEMPLATE_DIR=str(temporary / "git-templates"),
        )
        for name, executable, probe in (
            ("fixture-git", "git", ["--version", "--build-options"]),
            ("fixture-shell", "/bin/sh", ["-c", "exit 0"]),
            (
                "fixture-python",
                "python",
                [
                    "-I",
                    "-c",
                    "import sys; print(sys.version); print(sys.prefix); print(sys.base_prefix)",
                ],
            ),
        ):
            path = Path(
                shutil.which(executable, path=environment.get("PATH")) or executable
            )
            equivalent.require(path.is_file(), "A fixture tool is unavailable")
            measured = subprocess.run(
                [str(path), *probe],
                env=environment,
                capture_output=True,
                check=True,
                timeout=10,
            )
            tools[name] = {
                "executable": str(path),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "stdout": measured.stdout.decode().strip(),
            }
    equivalent.require(
        subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=destination,
            env=environment,
            capture_output=True,
            check=False,
        ).returncode
        != 0,
        "Content-only source unexpectedly exposes checkout metadata",
    )
    return {
        "root": destination,
        "coverage": temporary / "coverage.data",
        "environment": environment,
        "inputs": {
            name: value.replace(str(temporary), "{content}")
            for name, value in environment.items()
        },
        "tools": tools,
    }


def coverage_paths(path, *, original=None, destination=None):
    """Keep retained data relative and give the current reporter its own source paths."""
    with closing(sqlite3.connect(path)) as connection, connection:
        rows = connection.execute("SELECT id,path FROM file").fetchall()
        equivalent.require(rows, "Content-only coverage contains no source files")
        mapped = []
        for identifier, name in rows:
            relative = (
                Path(name).relative_to(original) if original is not None else Path(name)
            )
            equivalent.require(
                not relative.is_absolute() and ".." not in relative.parts,
                "Coverage file is outside the content-only source",
            )
            target = (
                str(destination / relative)
                if destination is not None
                else relative.as_posix()
            )
            mapped.append((target, identifier))
        connection.executemany("UPDATE file SET path=? WHERE id=?", mapped)
    if destination is None:
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("VACUUM")


def json_request(request):
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = response.read(16 * 1024 * 1024 + 1)
    except urllib.error.HTTPError as error:
        metadata = []
        for name in (
            "X-RateLimit-Limit",
            "X-RateLimit-Remaining",
            "X-RateLimit-Used",
            "X-RateLimit-Reset",
            "Retry-After",
        ):
            value = error.headers.get(name) if error.headers else None
            if isinstance(value, str) and re.fullmatch(r"[0-9]{1,20}", value):
                metadata.append(f"{name}={value}")
        if metadata:
            error.add_note("HTTP response numeric metadata: " + ", ".join(metadata))
        raise
    equivalent.require(len(data) <= 16 * 1024 * 1024, "API response exceeds its limit")
    return json.loads(data)


_PUBLIC_METADATA = {}


def public_package_api(repository, path):
    # A scoped source installation can exclude the public shared package.
    # Its immutable Git metadata needs no private credential or broader token.
    equivalent.require(
        repository == "EndurantDevs/endurant-ci",
        "Anonymous package fallback is not admitted",
    )
    if repository not in _PUBLIC_METADATA:
        metadata = json_request(
            urllib.request.Request(
                f"https://api.github.com/repos/{repository}",
                headers={
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
        )
        equivalent.require(
            metadata.get("full_name") == repository
            and metadata.get("private") is False,
            "Shared package is not the admitted public repository",
        )
        _PUBLIC_METADATA[repository] = metadata
    if not path:
        return _PUBLIC_METADATA[repository]
    return json_request(
        urllib.request.Request(
            f"https://api.github.com/repos/{repository}/{path}",
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
    )


def api(repository, path):
    equivalent.require(
        REPOSITORY.fullmatch(repository) and not path.startswith("/"),
        "Invalid read-only API target",
    )
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repository}" + (f"/{path}" if path else ""),
        headers={
            "Authorization": "Bearer " + os.environ["GH_TOKEN"],
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        return json_request(request)
    except urllib.error.HTTPError as error:
        if error.code not in {403, 404} or repository != "EndurantDevs/endurant-ci":
            raise
        error.close()
        return public_package_api(repository, path)


class ArtifactRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, destination):
        equivalent.require(
            destination.startswith("https://"), "Artifact redirect is not HTTPS"
        )
        redirected = super().redirect_request(
            request, response, code, message, headers, destination
        )
        if (
            urllib.parse.urlsplit(destination).netloc
            != urllib.parse.urlsplit(request.full_url).netloc
        ):
            redirected.remove_header("Authorization")
        return redirected


def download(repository, identifier):
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repository}/actions/artifacts/{identifier}/zip",
        headers={
            "Authorization": "Bearer " + os.environ["GH_TOKEN"],
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.build_opener(ArtifactRedirect()).open(
        request, timeout=30
    ) as response:
        equivalent.require(
            response.url.startswith("https://"), "Artifact redirect is not HTTPS"
        )
        data = response.read(MAX_BYTES + 1)
    equivalent.require(len(data) <= MAX_BYTES, "Measurement archive exceeds its limit")
    return data


def tracked(root):
    return [
        name
        for name in subprocess.check_output(
            ["git", "-C", str(root), "ls-files", "-z"], text=True
        ).split("\0")
        if name
    ]


def checkout_identity(root):
    remote = subprocess.check_output(
        ["git", "-C", str(root), "remote", "get-url", "origin"], text=True
    ).strip()
    match = re.fullmatch(
        r"(?:https://github\.com/|git@github\.com:)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?",
        remote,
    )
    equivalent.require(
        match is not None, "Recipe checkout is not an exact repository origin"
    )
    return {
        "repository": match[1],
        "tree": subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD^{tree}"], text=True
        ).strip(),
    }


def artifacts(repository, run_id):
    result, identifiers = [], set()
    for page in range(1, 11):
        suffix = "" if page == 1 else f"&page={page}"
        rows = api(repository, f"actions/runs/{run_id}/artifacts?per_page=100{suffix}")[
            "artifacts"
        ]
        equivalent.require(
            isinstance(rows, list), "Immutable artifact inventory is invalid"
        )
        for item in rows:
            equivalent.require(
                type(item.get("id")) is int
                and item["id"] > 0
                and item["id"] not in identifiers,
                "Immutable artifact inventory is invalid or duplicated",
            )
            identifiers.add(item["id"])
            result.append(item)
        if len(rows) < 100:
            return result
    raise ValueError("Immutable artifact inventory exceeds its bounded window")


def artifact_name(producer):
    return f"common-tests-{producer['run_id']}-{producer['run_attempt']}-{producer['job_id']}"


def current_producer(repository, step):
    run = api(repository, "actions/runs/" + os.environ["GITHUB_RUN_ID"])
    equivalent.require(
        run.get("repository", {}).get("full_name") == repository
        and run.get("head_repository", {}).get("id") == run["repository"]["id"]
        and run.get("run_attempt") == int(os.environ["GITHUB_RUN_ATTEMPT"])
        and run.get("event") in {"push", "pull_request", "workflow_dispatch"},
        "Current execution is not a same-repository authorized workflow attempt",
    )
    jobs = api(
        repository,
        f"actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs?per_page=100",
    )["jobs"]
    matches = [
        job
        for job in jobs
        if job.get("runner_name") == os.environ.get("RUNNER_NAME")
        and job.get("status") == "in_progress"
        and any(
            item.get("name") == step and item.get("status") == "in_progress"
            for item in job.get("steps", [])
        )
    ]
    equivalent.require(
        len(matches) == 1, "Current complete test job cannot be identified uniquely"
    )
    return {
        "repository_id": run["repository"]["id"],
        "commit": run["head_sha"],
        "workflow": run["path"],
        "run_id": run["id"],
        "run_attempt": run["run_attempt"],
        "job_id": matches[0]["id"],
        "step": step,
    }, run


def runner_identity(environment):
    image = environment.get("COMMON_TEST_RUNNER_IMAGE", "")
    if not image:
        equivalent.require(
            environment.get("ImageOS") and environment.get("ImageVersion"),
            "Runner does not expose an immutable image identity",
        )
        image = environment["ImageOS"] + ":" + environment["ImageVersion"]
    else:
        equivalent.require(
            re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", image),
            "Self-hosted runner image is not immutable",
        )
    return {
        "os": environment["RUNNER_OS"],
        "architecture": environment["RUNNER_ARCH"],
        "image": image,
    }


def path_inputs(value, source, recipe, source_tree, recipe_files):
    result = []
    for name in value.split(os.pathsep):
        path = Path(name or source).resolve()
        if path.is_relative_to(source):
            result.append(
                {
                    "kind": "source",
                    "path": path.relative_to(source).as_posix(),
                    "tree": source_tree,
                }
            )
        elif path.is_relative_to(recipe):
            result.append(
                {
                    "kind": "recipe",
                    "path": path.relative_to(recipe).as_posix(),
                    "files": recipe_files,
                }
            )
        else:
            equivalent.require(
                path.is_dir(), "An additional Python import root is absent"
            )
            if (path / ".git").exists():
                equivalent.require(
                    not subprocess.check_output(
                        [
                            "git",
                            "-C",
                            str(path),
                            "status",
                            "--porcelain",
                            "--untracked-files=normal",
                        ],
                        text=True,
                    ).strip(),
                    "An additional Git import checkout has modified or unknown code",
                )
                compiled = [
                    entry.relative_to(path).as_posix()
                    for entry in path.rglob("*")
                    if entry.is_file()
                    and entry.suffix in {".so", ".pyd", ".node", ".dll", ".dylib"}
                ]
                result.append(
                    {
                        "kind": "additional-git-import-root",
                        "checkout": checkout_identity(path),
                        "compiled_inputs": equivalent.file_inventory(path, compiled)
                        if compiled
                        else [],
                    }
                )
                continue
            files = [
                entry.relative_to(path).as_posix()
                for entry in path.rglob("*")
                if entry.is_file() and "__pycache__" not in entry.parts
            ]
            equivalent.require(
                files and len(files) <= 10000,
                "An additional import root cannot be measured completely",
            )
            result.append(
                {
                    "kind": "additional-import-root",
                    "files": equivalent.file_inventory(path, files),
                }
            )
    return result


def execution_value(value, source, recipe, data_root):
    """Use declared checkout/output roles without depending on a runner's scratch directory."""
    for root, label in sorted(
        ((source, "{source}"), (recipe, "{recipe}"), (data_root, "{data}")),
        key=lambda item: len(str(item[0])),
        reverse=True,
    ):
        value = value.replace(str(root), label)
    return value


def collect(arguments, producer, source_repository_id, execution=None):
    root, recipe = arguments.source_root.resolve(), arguments.recipe_root.resolve()
    data_root = arguments.data_root.resolve()
    source_files = tracked(root)
    recipe_files = (
        tracked(recipe)
        if arguments.recipe_file == ["@tracked"]
        else arguments.recipe_file
    )
    if arguments.recipe_directory:
        recipe_files = sorted(
            set(recipe_files)
            | {
                name
                for name in tracked(recipe)
                if any(
                    name.startswith(prefix.rstrip("/") + "/")
                    for prefix in arguments.recipe_directory
                )
            }
        )
    configuration = [
        name
        for name in source_files
        if Path(name).name
        in {
            "pyproject.toml",
            "uv.lock",
            "pytest.ini",
            "tox.ini",
            "setup.cfg",
            "test-coverage.ini",
            "Cargo.lock",
            "Cargo.toml",
            "package.json",
            "package-lock.json",
            "go.mod",
            "go.sum",
        }
        or Path(name).name.startswith("requirements")
    ]
    if not configuration and root == recipe:
        configuration = [
            name for name in recipe_files if name.startswith(".github/workflows/")
        ]
    equivalent.require(
        configuration, "The complete test configuration/dependency inventory is absent"
    )
    command = [
        execution_value(value, root, recipe, arguments.data_root.resolve())
        for value in arguments.command
    ]
    tree, inputs = equivalent.derive_inputs(
        root,
        recipe,
        recipe_files=recipe_files,
        configuration_files=configuration,
        commands=[command],
        runner=runner_identity(os.environ),
        trust={"application_secrets": False, "permissions": "read-only"},
        runtime_files=arguments.runtime_file,
        allowed_outputs=arguments.allowed_source_output
        + (arguments.output_file if arguments.data_root.resolve() == root else []),
    )
    # The names are declared by the reviewed caller. Their actual values, not
    # caller-supplied hashes, enter the execution fingerprint. Never accept secrets.
    equivalent.require(
        all(
            re.fullmatch(r"[A-Z][A-Z0-9_]*", name)
            and not any(part in name for part in ("TOKEN", "SECRET", "PASSWORD", "KEY"))
            for name in arguments.environment
        ),
        "Secret or invalid execution context",
    )
    inputs["environment"] = {
        name: (
            execution_value(
                os.environ[name], root, recipe, arguments.data_root.resolve()
            )
            if name in os.environ
            else None
        )
        for name in sorted(set(arguments.environment))
    }
    if inputs["environment"].get("PYTHONPATH"):
        inputs["environment"]["PYTHONPATH"] = path_inputs(
            os.environ["PYTHONPATH"], root, recipe, tree, inputs["recipe"]
        )
    if not inputs["environment"]:
        inputs["environment"] = {"declared_context": "none"}
    inputs["recipe_checkout"] = checkout_identity(recipe)
    # The verifier itself may be checked out independently of the caller.
    helper_root = Path(__file__).resolve().parents[1]
    inputs["verifier_checkout"] = checkout_identity(helper_root)
    inputs["verifier_git"] = equivalent.git_execution_context(helper_root)
    # No scope currently claims that source/ref identity is unobservable. Keep
    # these actual values: a rewritten checkout therefore takes the full path.
    inputs["identity_environment"] = {
        name: os.environ.get(name)
        for name in (
            "GITHUB_SHA",
            "GITHUB_REF",
            "GITHUB_REF_NAME",
            "GITHUB_REF_TYPE",
            "GITHUB_HEAD_REF",
            "GITHUB_BASE_REF",
            "GITHUB_EVENT_NAME",
            "GITHUB_REPOSITORY",
            "GITHUB_WORKFLOW_REF",
            "GITHUB_WORKFLOW_SHA",
            "SOURCE_SHA",
            "SOURCE_REPOSITORY",
            "BASE_SHA",
            "PR_NUMBER",
        )
    }
    if execution is not None:
        for name in (
            "source_git",
            "recipe_git",
            "verifier_git",
            "identity_environment",
        ):
            inputs.pop(name)
        inputs["execution_profile"] = arguments.scope
        inputs["environment"] = execution["inputs"]
        inputs["toolchains"].update(execution["tools"])
    inputs["verifier_files"] = equivalent.file_inventory(
        helper_root, ["scripts/result_equivalence.py", "scripts/common_test_result.py"]
    )
    inputs["output_layout"] = arguments.output_file or ["no-raw-data"]
    for name, text in arguments.toolchain_command:
        command = json.loads(text)
        equivalent.require(
            re.fullmatch(r"[a-z][a-z0-9_-]*", name)
            and isinstance(command, list)
            and command
            and all(isinstance(item, str) and item for item in command),
            "Actual toolchain inventory command is invalid",
        )
        environment = (
            execution["environment"]
            if execution is not None
            else {
                key: value
                for key, value in os.environ.items()
                if key not in {"GH_TOKEN", "GITHUB_TOKEN", "SOURCE_TOKEN"}
            }
        )
        measured = subprocess.run(
            command,
            cwd=root,
            env=environment,
            check=True,
            capture_output=True,
            timeout=30,
        )
        equivalent.require(
            len(measured.stdout) <= 4 * 1024 * 1024,
            "Resolved toolchain inventory exceeds its limit",
        )
        executable = Path(
            shutil.which(command[0], path=environment.get("PATH")) or command[0]
        )
        equivalent.require(
            executable.is_file(), "Toolchain executable cannot be fingerprinted"
        )
        inputs["toolchains"][name] = {
            "command": [
                execution_value(item, root, recipe, data_root) for item in command
            ],
            "stdout": execution_value(
                measured.stdout.decode().strip(), root, recipe, data_root
            ),
            "executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
        }
    tests = equivalent.file_inventory(
        root,
        [name for name in source_files if name.startswith(arguments.collection + "/")],
    )
    scope = {
        "name": arguments.scope,
        "collection": hashlib.sha256(
            equivalent.encoded({"tests": tests, "command": command})
        ).hexdigest(),
        "complete": True,
    }
    source = {
        "repository_id": source_repository_id,
        "commit": subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
        ).strip(),
        "tree": tree,
    }
    return equivalent.measurement(source, inputs, scope, producer, executed=False)


def archive_document(data, key, outputs):
    """Read only fixed measurement/data files; never extract producer paths or code."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = archive.namelist()
        equivalent.require(
            len(names) == len(set(names)) and len(names) <= 1000,
            "Measurement archive contains duplicate or excessive members",
        )
        expected = [
            f"{key}/measurement.json",
            *[f"{key}/data/{index}" for index in range(len(outputs))],
        ]
        entries = {entry.filename: entry for entry in archive.infolist()}
        equivalent.require(
            set(expected) <= set(entries),
            "Original selected scope or raw data is missing",
        )
        for name in names:
            entry = entries[name]
            equivalent.require(
                not name.startswith("/")
                and ".." not in Path(name).parts
                and (entry.external_attr >> 16) & 0o170000 in (0, 0o100000)
                and entry.file_size <= MAX_BYTES
                and entry.compress_type in (0, 8),
                "Measurement archive contains an invalid entry",
            )
        equivalent.require(
            sum(entry.file_size for entry in entries.values()) <= MAX_BYTES,
            "Expanded measurement archive exceeds its limit",
        )
        document = json.loads(archive.read(expected[0]))
        raw = [archive.read(name) for name in expected[1:]]
    equivalent.require(
        set(document) == {"measurement", "outputs", "origin"}
        and document["outputs"]
        == [
            {"path": path, "sha256": hashlib.sha256(value).hexdigest()}
            for path, value in zip(outputs, raw)
        ],
        "Original raw measurements do not match the selected output inventory",
    )
    return document["measurement"], raw


def source_authorized(current, original, run, arguments):
    if arguments.source_authority is None:
        source = api(
            arguments.source_repository, "git/commits/" + original["source"]["commit"]
        )
        equivalent.require(
            source.get("sha") == original["source"]["commit"]
            and source.get("tree", {}).get("sha") == original["source"]["tree"],
            "Original source tuple does not match its immutable Git tree",
        )
        return
    path = arguments.source_authority
    equivalent.require(
        path.is_file()
        and not path.is_symlink()
        and path.stat().st_size <= 262144
        and hashlib.sha256(path.read_bytes()).hexdigest()
        == arguments.source_authority_sha256,
        "Source authority differs from the trusted current bundle digest",
    )
    authority = json.loads(path.read_text())
    consumer = {
        "source_sha": current["source"]["commit"],
        "source_tree": current["source"]["tree"],
        "run_id": current["producer"]["run_id"],
        "run_attempt": current["producer"]["run_attempt"],
        "controller_sha": current["producer"]["commit"],
    }
    equivalent.require(
        authority.get("schema") == "common-test-source-authority-v1"
        and authority.get("consumer") == consumer
        and authority.get("source_repository_id") == current["source"]["repository_id"],
        "Source authority belongs to another consuming source or workflow attempt",
    )
    matches = [
        item
        for item in authority.get("origins", [])
        if item.get("source_sha") == original["source"]["commit"]
        and item.get("source_tree") == original["source"]["tree"]
        and item.get("run_id") == run["id"]
        and item.get("run_attempt") == run["run_attempt"]
        and item.get("controller_sha") == run["head_sha"]
    ]
    equivalent.require(
        len(matches) == 1,
        "Original source was not authenticated by the current source authorizer",
    )


def verify_original(current, original, run, artifact, data, repository):
    """Recheck original native provenance against live reads before final acceptance."""
    job = api(repository, f"actions/jobs/{original['producer']['job_id']}")
    match = re.fullmatch(
        rf"https://api.github.com/repos/{re.escape(repository)}/check-runs/([1-9][0-9]*)",
        job.get("check_run_url", ""),
    )
    equivalent.require(
        match is not None, "Original native job has no same-repository check"
    )
    check = api(repository, "check-runs/" + match[1])
    identity = original["inputs"]["recipe_checkout"]
    if identity["repository"] == repository:
        executed = api(repository, "git/commits/" + run["head_sha"])
        equivalent.require(
            executed.get("tree", {}).get("sha") == identity["tree"],
            "Original recipe checkout differs from the native workflow commit",
        )
    recipe_verified = equivalent.authenticate_recipe(
        original["inputs"]["recipe"], identity["repository"], identity["tree"], api
    )
    identity = original["inputs"]["verifier_checkout"]
    equivalent.authenticate_recipe(
        original["inputs"]["verifier_files"],
        identity["repository"],
        identity["tree"],
        api,
    )
    proof = equivalent.verify(
        current,
        original,
        run,
        job,
        check,
        artifact,
        archive_sha256=hashlib.sha256(data).hexdigest(),
        producer_recipe_verified=recipe_verified,
        allowed_events={"push", "pull_request", "workflow_dispatch"},
        artifact_name=artifact_name(original["producer"]),
        upload_step=UPLOAD_STEP,
    )
    latest = api(repository, f"actions/runs/{run['id']}")
    equivalent.require(
        latest.get("run_attempt") == run["run_attempt"]
        and latest.get("conclusion") == "success",
        "Original attempt changed during verification",
    )
    return proof


def refresh_document(
    document, artifact, archive_sha256, run, repository, *, authorize_source=None
):
    """Refresh native current/original provenance at a trusted final consumer."""
    current = document["measurement"]
    expected = equivalent.measurement(
        current["source"],
        current["inputs"],
        current["scope"],
        current["producer"],
        executed=current["executed"],
    )
    equivalent.require(
        current == expected, "Current measurement inputs do not match its fingerprint"
    )
    producer = current["producer"]
    job = api(repository, f"actions/jobs/{producer['job_id']}")
    match = re.fullmatch(
        rf"https://api.github.com/repos/{re.escape(repository)}/check-runs/([1-9][0-9]*)",
        job.get("check_run_url", ""),
    )
    equivalent.require(
        match is not None, "Current native test job has no same-repository check"
    )
    check = api(repository, "check-runs/" + match[1])
    equivalent.authenticate_job_artifact(
        producer,
        run,
        job,
        check,
        artifact,
        archive_sha256=archive_sha256,
        allowed_events={"push", "pull_request", "workflow_dispatch"},
        artifact_name=artifact_name(producer),
        upload_step=UPLOAD_STEP,
    )
    identity = current["inputs"]["recipe_checkout"]
    if identity["repository"] == repository:
        commit = api(repository, "git/commits/" + run["head_sha"])
        equivalent.require(
            commit.get("tree", {}).get("sha") == identity["tree"],
            "Current recipe checkout differs from its actual workflow commit",
        )
    equivalent.authenticate_recipe(
        current["inputs"]["recipe"], identity["repository"], identity["tree"], api
    )
    identity = current["inputs"]["verifier_checkout"]
    equivalent.authenticate_recipe(
        current["inputs"]["verifier_files"],
        identity["repository"],
        identity["tree"],
        api,
    )
    if current["executed"]:
        equivalent.require(
            document["origin"] is None,
            "An original execution cannot identify a reused origin",
        )
        return None
    proof = document["origin"]
    equivalent.require(
        isinstance(proof, dict)
        and set(proof)
        == {"source", "fingerprint", "origin", "artifact_id", "archive_sha256"},
        "Reused execution has no immutable origin",
    )
    original = proof["origin"]
    original_run = api(repository, f"actions/runs/{original['producer']['run_id']}")
    original_artifact = api(repository, f"actions/artifacts/{proof['artifact_id']}")
    data = download(repository, proof["artifact_id"])
    equivalent.require(
        hashlib.sha256(data).hexdigest() == proof["archive_sha256"],
        "Original raw artifact changed",
    )
    key = hashlib.sha256(current["scope"]["name"].encode()).hexdigest()
    archived, raw = archive_document(
        data, key, [item["path"] for item in document["outputs"]]
    )
    equivalent.require(
        archived == original,
        "Original immutable measurement differs from the reuse reference",
    )
    equivalent.require(
        document["outputs"]
        == [
            {"path": item["path"], "sha256": hashlib.sha256(value).hexdigest()}
            for item, value in zip(document["outputs"], raw)
        ],
        "Reused raw measurements differ from the authenticated original data",
    )
    if authorize_source is None:
        native_source = api(repository, "git/commits/" + original_run["head_sha"])
        executed_source = api(repository, "git/commits/" + original["source"]["commit"])
        equivalent.require(
            original["source"]["repository_id"] == original_run["repository"]["id"]
            and native_source.get("sha") == original_run["head_sha"]
            and executed_source.get("sha") == original["source"]["commit"]
            and native_source.get("tree", {}).get("sha") == original["source"]["tree"]
            and executed_source.get("tree", {}).get("sha")
            == original["source"]["tree"],
            "A central-controller or different-tree source needs independent source authority",
        )
    else:
        authorize_source(current, original, original_run)
    refreshed = verify_original(
        current, original, original_run, original_artifact, data, repository
    )
    equivalent.require(
        refreshed == proof,
        "Refreshed original provenance differs from the current reuse reference",
    )
    return {
        "scope": current["scope"]["name"],
        "fingerprint": current["fingerprint"],
        "source": original["source"],
        "producer": original["producer"],
        "artifact_id": proof["artifact_id"],
        "archive_sha256": proof["archive_sha256"],
    }


def select(current, repository, key, arguments):
    query = urllib.parse.urlencode({"status": "success", "per_page": 30})
    path = current["producer"]["workflow"].rsplit("/", 1)[1]
    runs = api(repository, f"actions/workflows/{path}/runs?{query}")["workflow_runs"]
    for summary in runs:
        if summary["id"] == current["producer"]["run_id"]:
            continue
        try:
            run = api(repository, f"actions/runs/{summary['id']}")
            if run.get("event") not in {"push", "pull_request", "workflow_dispatch"}:
                continue
            inventory = artifacts(repository, run["id"])
            for artifact in inventory:
                if not str(artifact.get("name", "")).startswith(
                    f"common-tests-{run['id']}-{run['run_attempt']}-"
                ):
                    continue
                data = download(repository, artifact["id"])
                original, raw = archive_document(data, key, arguments.output_file)
                if (
                    original["fingerprint"] != current["fingerprint"]
                    or original["executed"] is not True
                ):
                    continue
                equivalent.require(
                    original["source"]["repository_id"]
                    == current["source"]["repository_id"],
                    "Original source is from another repository",
                )
                if original["source"]["repository_id"] == run["repository"]["id"]:
                    native_source = api(repository, "git/commits/" + run["head_sha"])
                    equivalent.require(
                        native_source.get("sha") == run["head_sha"]
                        and native_source.get("tree", {}).get("sha")
                        == original["source"]["tree"],
                        "Original checkout tree differs from its native workflow source",
                    )
                else:
                    equivalent.require(
                        arguments.source_authority is not None,
                        "A central controller requires admitted original source authority",
                    )
                # For a centralized caller this read is supplied by the trusted
                # source-authority adapter, never by an artifact's source claim.
                source_authorized(current, original, run, arguments)
                proof = verify_original(
                    current, original, run, artifact, data, repository
                )
                return proof, raw
        except (
            HTTPException,
            KeyError,
            TypeError,
            ValueError,
            OSError,
            urllib.error.URLError,
            zipfile.BadZipFile,
        ):
            continue
    return None, None


def execute(arguments, *, read=api, choose=select, run=subprocess.run):
    # A separate empty prefix prevents valid stale cache files from overriding
    # the measured Python source. This also applies to the complete fallback.
    names = ("PYTHONPYCACHEPREFIX", "PYTHONDONTWRITEBYTECODE")
    previous = {name: os.environ.get(name) for name in names}
    with tempfile.TemporaryDirectory(prefix="common-test-bytecode-") as cache:
        os.environ.update(PYTHONPYCACHEPREFIX=cache, PYTHONDONTWRITEBYTECODE="1")
        try:
            if arguments.scope in {CONTENT_SCOPE, PYTEST_CONTENT_SCOPE}:
                with tempfile.TemporaryDirectory(
                    prefix="common-test-content-"
                ) as directory:
                    try:
                        execution = content_execution(
                            arguments, Path(directory).resolve()
                        )
                    except (
                        configparser.Error,
                        ValueError,
                        OSError,
                        TypeError,
                        AttributeError,
                        subprocess.SubprocessError,
                    ) as error:
                        execution = None
                        print(
                            f"Content-only profile unavailable; executing stock command: {error}"
                        )
                    if execution is not None:
                        return execute_isolated(
                            arguments,
                            read=read,
                            choose=choose,
                            run=run,
                            execution=execution,
                        )
            record = (
                arguments.measurements.resolve()
                / hashlib.sha256(arguments.scope.encode()).hexdigest()
            )
            if record.exists():
                equivalent.require(
                    record.is_dir() and not record.is_symlink(),
                    "Scope evidence destination is invalid",
                )
                shutil.rmtree(record)
            return run(
                arguments.command,
                cwd=arguments.source_root.resolve(),
                env={
                    name: value
                    for name, value in os.environ.items()
                    if name not in {"GH_TOKEN", "GITHUB_TOKEN", "SOURCE_TOKEN"}
                },
                check=False,
            ).returncode
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


def execute_isolated(
    arguments, *, read=api, choose=select, run=subprocess.run, execution=None
):
    """The stock command remains the fallback for every missing/incompatible proof."""
    current, proof, raw, producer = None, None, None, None
    root = arguments.source_root.resolve()
    data_root = arguments.data_root.resolve()
    record_directory = (
        arguments.measurements.resolve()
        / hashlib.sha256(arguments.scope.encode()).hexdigest()
    )
    if record_directory.exists():
        # The directory is one exact generated scope, never a producer archive
        # or application output. A fallback cannot expose a stale success record.
        equivalent.require(
            record_directory.is_dir() and not record_directory.is_symlink(),
            "Scope evidence destination is invalid",
        )
        shutil.rmtree(record_directory)
    equivalent.require(
        all(
            isinstance(name, str)
            and name
            and not Path(name).is_absolute()
            and ".." not in Path(name).parts
            for name in arguments.output_file
        ),
        "Raw measurement layout must contain only reviewed relative paths",
    )
    try:
        equivalent.require(
            (os.environ.get("GH_TOKEN") or arguments.evidence_directory)
            and os.environ.get("GITHUB_REPOSITORY"),
            "No read-only workflow evidence token",
        )
        evidence = None
        if arguments.evidence_directory:
            evidence = install_evidence(arguments)
            producer = evidence["producer"]
        else:
            producer, _workflow = current_producer(
                os.environ["GITHUB_REPOSITORY"], arguments.step
            )
        if arguments.source_authority is not None:
            path = arguments.source_authority
            equivalent.require(
                path.is_file()
                and not path.is_symlink()
                and path.stat().st_size <= 262144
                and hashlib.sha256(path.read_bytes()).hexdigest()
                == arguments.source_authority_sha256,
                "Current source authority differs from its trusted bundle digest",
            )
            source_repository_id = json.loads(path.read_text())["source_repository_id"]
        elif evidence is not None:
            source_repository_id = evidence["source"]["repository_id"]
        else:
            source_repository_id = read(arguments.source_repository, "")["id"]
        current = collect(arguments, producer, source_repository_id, execution)
        equivalent.require(
            len(equivalent.encoded(current)) <= 2 * 1024 * 1024,
            "Resolved execution measurement exceeds its bounded envelope",
        )
        proof, raw = choose(
            current,
            os.environ["GITHUB_REPOSITORY"],
            hashlib.sha256(arguments.scope.encode()).hexdigest(),
            arguments,
        )
    except (
        HTTPException,
        KeyError,
        TypeError,
        ValueError,
        OSError,
        urllib.error.URLError,
        subprocess.SubprocessError,
    ) as error:
        print(f"Full common-test execution required: {error}")
        current = None
    if proof is not None:
        for name, value in zip(arguments.output_file, raw):
            target = data_root / name
            equivalent.require(
                not target.is_symlink() and target.resolve().is_relative_to(data_root),
                "Current raw measurement path is outside the source checkout",
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(value)
        if execution is not None:
            execution["coverage"].write_bytes(raw[0])
            coverage_paths(data_root / ".coverage", destination=data_root)
        print("Authenticated equivalent original common-test execution")
        status = 0
    else:
        print(
            "No qualified original measurement; executing the complete common-test command"
        )
        environment = (
            execution["environment"]
            if execution is not None
            else {
                name: value
                for name, value in os.environ.items()
                if name not in {"GH_TOKEN", "GITHUB_TOKEN", "SOURCE_TOKEN"}
            }
        )
        status = run(
            arguments.command,
            cwd=execution["root"] if execution is not None else root,
            env=environment,
            check=False,
        ).returncode
        if status == 0 and execution is not None:
            source = execution["root"] / ".coverage"
            equivalent.require(
                source.is_file() and not source.is_symlink(),
                "Content-only command did not produce coverage data",
            )
            target = data_root / ".coverage"
            equivalent.require(
                not target.is_symlink(), "Coverage destination is indirect"
            )
            execution["coverage"].write_bytes(source.read_bytes())
            coverage_paths(execution["coverage"], original=execution["root"])
            target.write_bytes(execution["coverage"].read_bytes())
            coverage_paths(target, destination=data_root)
    if status == 0 and current is not None:
        current["executed"] = proof is None
        hashlib.sha256(arguments.scope.encode()).hexdigest()
        directory = record_directory
        directory.mkdir(parents=True, exist_ok=True)
        outputs = []
        for index, name in enumerate(arguments.output_file):
            path = data_root / name
            equivalent.require(
                path.is_file()
                and not path.is_symlink()
                and path.resolve().is_relative_to(data_root),
                "Common test did not produce its required raw data",
            )
            data = (
                execution["coverage"].read_bytes()
                if execution is not None
                else path.read_bytes()
            )
            target = directory / "data" / str(index)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            outputs.append({"path": name, "sha256": hashlib.sha256(data).hexdigest()})
        (directory / "measurement.json").write_bytes(
            equivalent.encoded(
                {"measurement": current, "outputs": outputs, "origin": proof}
            )
            + b"\n"
        )
        if os.environ.get("GITHUB_OUTPUT"):
            with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
                output.write("artifact_name=" + artifact_name(producer) + "\n")
        if os.environ.get("GITHUB_ENV"):
            with Path(os.environ["GITHUB_ENV"]).open("a") as environment:
                environment.write(
                    "COMMON_TEST_ARTIFACT_NAME=" + artifact_name(producer) + "\n"
                )
    return status


def install_evidence(arguments):
    """Consume the current broker's immutable reads without a source-process token."""
    global api, download
    directory = arguments.evidence_directory.resolve()
    path = directory / "evidence.json"
    equivalent.require(
        path.is_file()
        and not path.is_symlink()
        and path.stat().st_size <= 32 * 1024 * 1024
        and hashlib.sha256(path.read_bytes()).hexdigest() == arguments.evidence_sha256,
        "Broker evidence differs from the trusted preparatory step digest",
    )
    evidence = json.loads(path.read_text())
    equivalent.require(
        set(evidence)
        == {"schema", "created_at", "source", "producer", "responses", "archives"}
        and evidence["schema"] == "common-test-broker-v1",
        "Unsupported broker evidence",
    )
    created = datetime.fromisoformat(evidence["created_at"].replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    equivalent.require(
        created.tzinfo is not None and now - timedelta(minutes=45) <= created <= now,
        "Broker evidence is outside its short consumption window",
    )
    producer = evidence["producer"]
    equivalent.require(
        str(producer["run_id"]) == os.environ["GITHUB_RUN_ID"]
        and str(producer["run_attempt"]) == os.environ["GITHUB_RUN_ATTEMPT"]
        and producer["step"] == arguments.step,
        "Broker evidence belongs to another current job, step or workflow attempt",
    )
    current = subprocess.check_output(
        ["git", "-C", str(arguments.source_root), "rev-parse", "HEAD"], text=True
    ).strip()
    tree = subprocess.check_output(
        ["git", "-C", str(arguments.source_root), "rev-parse", "HEAD^{tree}"], text=True
    ).strip()
    equivalent.require(
        evidence["source"]["commit"] == current and evidence["source"]["tree"] == tree,
        "Broker source snapshot changed before common-test execution",
    )

    def read(repository, suffix):
        key = repository + "/" + suffix
        equivalent.require(
            key in evidence["responses"],
            "Immutable broker evidence lacks the required API proof",
        )
        return evidence["responses"][key]

    def raw(repository, identifier):
        equivalent.require(
            repository == os.environ["GITHUB_REPOSITORY"],
            "Broker archive is from another repository",
        )
        record = evidence["archives"][str(identifier)]
        equivalent.require(
            record["file"] == str(identifier) + ".zip", "Invalid immutable archive path"
        )
        archive = directory / record["file"]
        equivalent.require(
            archive.is_file()
            and not archive.is_symlink()
            and archive.stat().st_size <= MAX_BYTES,
            "Immutable broker archive is absent or too large",
        )
        data = archive.read_bytes()
        equivalent.require(
            hashlib.sha256(data).hexdigest() == record["sha256"],
            "Immutable broker archive digest changed",
        )
        return data

    api, download = read, raw
    return evidence


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--source-root", type=Path, required=True)
    result.add_argument("--data-root", type=Path)
    result.add_argument("--source-repository", required=True)
    result.add_argument("--recipe-root", type=Path, required=True)
    result.add_argument("--recipe-file", action="append", default=[])
    result.add_argument("--recipe-directory", action="append", default=[])
    result.add_argument("--collection", default="tests")
    result.add_argument("--scope", required=True)
    result.add_argument("--step", required=True)
    result.add_argument("--environment", action="append", default=[])
    result.add_argument("--runtime-file", action="append", default=[])
    result.add_argument("--toolchain-command", action="append", nargs=2, default=[])
    result.add_argument("--source-authority", type=Path)
    result.add_argument("--source-authority-sha256")
    result.add_argument("--evidence-directory", type=Path)
    result.add_argument("--evidence-sha256")
    result.add_argument("--output-file", action="append", default=[])
    result.add_argument("--allowed-source-output", action="append", default=[])
    result.add_argument("--measurements", type=Path, required=True)
    result.add_argument("command", nargs=argparse.REMAINDER)
    return result


if __name__ == "__main__":
    arguments = parser().parse_args()
    arguments.data_root = arguments.data_root or arguments.source_root
    if arguments.command[:1] == ["--"]:
        arguments.command = arguments.command[1:]
    equivalent.require(
        arguments.command, "The complete original common-test command is required"
    )
    sys.exit(execute(arguments))
