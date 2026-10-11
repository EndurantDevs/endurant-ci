"""Exercise recovery authority and one-shot writes with synthetic GitHub responses."""

import base64
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import metadata_recovery as recovery
import render_workflow as renderer

REPOSITORY = "sample/project"
HEAD, BASE = "a" * 40, "b" * 40
TRUSTED_WORKFLOW = b"reviewed source workflow\n"


def run(identifier, title, conclusion, attempt=1, target="main"):
    return {
        "id": identifier,
        "run_attempt": attempt,
        "workflow_id": 50,
        "path": recovery.WORKFLOW,
        "name": "CI",
        "display_title": title,
        "event": "pull_request",
        "head_sha": HEAD,
        "status": "completed",
        "conclusion": conclusion,
        "repository": {"full_name": REPOSITORY},
        "head_repository": {"full_name": REPOSITORY},
        "run_started_at": f"2026-01-01T10:{identifier:02}:00Z",
        "created_at": f"2026-01-01T10:{identifier:02}:00Z",
        "pull_requests": [
            {"number": 7, "head": {"sha": HEAD}, "base": {"sha": BASE, "ref": target}}
        ],
    }


class FakeGitHub:
    def __init__(self, kind="healthcare", target="main"):
        self.kind = kind
        self.full = run(20, "CI", "success", attempt=2, target=target)
        self.metadata = run(10, "CI metadata update", "failure", target=target)
        self.runs = [self.metadata, self.full]
        self.pr = {
            "number": 7,
            "state": "open",
            "merged": False,
            "head": {"sha": HEAD, "repo": {"full_name": REPOSITORY}},
            "base": {"sha": BASE, "ref": target, "repo": {"full_name": REPOSITORY}},
        }
        self.branch = {"name": target, "protected": True, "commit": {"sha": BASE}}
        self.content = TRUSTED_WORKFLOW
        self.base_content = TRUSTED_WORKFLOW
        self.default_content = b"different default-main workflow\n"
        self.calls, self.writes = [], []
        self.on_read = None
        self.jobs = []
        names = [
            ("portable import checks", "success"),
            (recovery.VALIDATION[kind], "failure"),
        ]
        names += [
            (sorted(recovery.skipped_names(label))[-1], "skipped")
            for label in recovery.SKIPPED[kind]
        ]
        for index, (name, conclusion) in enumerate(names):
            self.jobs.append(
                {
                    "id": 100 + index,
                    "run_id": 10,
                    "run_attempt": 1,
                    "head_sha": HEAD,
                    "name": name,
                    "status": "completed",
                    "conclusion": conclusion,
                    "steps": [
                        {"name": recovery.DEPENDENCY_STEP, "conclusion": "failure"}
                    ]
                    if conclusion == "failure"
                    else [],
                }
            )

    def api(self, repository, path, method="GET"):
        self.assert_repository(repository)
        self.calls.append(path)
        if self.on_read:
            self.on_read(path)
        if path.startswith("pulls/"):
            return deepcopy(self.pr)
        if path.startswith("branches/"):
            return deepcopy(self.branch)
        if path == "actions/workflows/ci.yml":
            return {"id": 50, "path": recovery.WORKFLOW}
        if path.startswith("actions/workflows/ci.yml/runs?"):
            return {"total_count": len(self.runs), "workflow_runs": deepcopy(self.runs)}
        if path.startswith("contents/"):
            revision = path.split("?ref=")[-1]
            content = {
                HEAD: self.content,
                BASE: self.base_content,
                "c" * 40: self.default_content,
            }[revision]
            return {
                "type": "file",
                "encoding": "base64",
                "size": len(content),
                "content": base64.b64encode(content).decode(),
            }
        if path.startswith("actions/runs/"):
            for value in self.runs:
                if path == f"actions/runs/{value['id']}":
                    return deepcopy(value)
        if path == "actions/jobs/101":
            return deepcopy(self.jobs[1])
        raise AssertionError("unexpected synthetic API path: " + path)

    @staticmethod
    def assert_repository(repository):
        if repository != REPOSITORY:
            raise AssertionError("unexpected synthetic repository")

    def pages(self, repository, path, key):
        self.assert_repository(repository)
        if path != "actions/runs/10/attempts/1/jobs" or key != "jobs":
            raise AssertionError("unexpected synthetic job inventory")
        return deepcopy(self.jobs)

    def rerun(self, repository, job_id):
        self.assert_repository(repository)
        self.writes.append(job_id)
        self.metadata.update(run_attempt=2, status="queued", conclusion=None)
        return True


class MetadataRecoveryChecks(unittest.TestCase):
    def exercise(self, fake, event=None, marker=True):
        with (
            patch.object(recovery.github, "api", side_effect=fake.api),
            patch.object(recovery.github, "pages", side_effect=fake.pages),
            patch.object(recovery, "dependency_failed", return_value=marker),
            patch.object(recovery, "request_rerun", side_effect=fake.rerun),
        ):
            return recovery.recover(REPOSITORY, fake.kind, event or deepcopy(fake.full))

    def test_dev_uses_protected_target_ci_even_when_default_main_ci_differs(self):
        for kind in ("healthcare", "drug"):
            with self.subTest(kind=kind):
                fake = FakeGitHub(kind, target="dev")
                self.assertNotEqual(fake.default_content, fake.content)
                self.assertTrue(self.exercise(fake))
                self.assertEqual(fake.writes, [101])
                self.assertEqual(fake.calls.count("branches/dev"), 3)
                self.assertNotIn("branches/main", fake.calls)
                content_reads = [
                    path for path in fake.calls if path.startswith("contents/")
                ]
                self.assertEqual(
                    set(content_reads),
                    {
                        f"contents/{recovery.WORKFLOW}?ref={HEAD}",
                        f"contents/{recovery.WORKFLOW}?ref={BASE}",
                    },
                )

    def test_target_ref_and_protected_base_are_exact_on_every_association(self):
        changes = (
            lambda f: f.full["pull_requests"][0]["base"].pop("ref"),
            lambda f: f.metadata["pull_requests"][0]["base"].pop("ref"),
            lambda f: f.pr["base"].pop("ref"),
            lambda f: f.pr["base"].update(ref="main"),
            lambda f: f.metadata["pull_requests"][0]["base"].update(ref="main"),
            lambda f: f.full["pull_requests"][0]["base"].update(ref="main"),
            lambda f: f.full["pull_requests"][0]["base"].update(ref="other"),
            lambda f: f.branch.update(name="main"),
            lambda f: f.branch.pop("name"),
            lambda f: f.branch.pop("protected"),
            lambda f: f.branch.update(protected=False),
            lambda f: f.branch.update(protected=1),
            lambda f: f.branch.update(protected="true"),
            lambda f: f.branch["commit"].update(sha="c" * 40),
            lambda f: f.branch.pop("commit"),
            lambda f: setattr(
                f, "base_content", b"different protected target workflow\n"
            ),
        )
        for index, change in enumerate(changes):
            with self.subTest(case=index):
                fake = FakeGitHub(target="dev")
                change(fake)
                self.assertFalse(self.exercise(fake))
                self.assertEqual(fake.writes, [])

    def test_target_protection_and_ref_are_refreshed_at_final_fence(self):
        changes = (
            ("branches/dev", 3, lambda f: f.branch.update(protected=False)),
            ("branches/dev", 3, lambda f: f.branch.pop("protected")),
            ("branches/dev", 3, lambda f: f.branch.update(name="main")),
            ("branches/dev", 3, lambda f: f.branch["commit"].update(sha="c" * 40)),
            ("pulls/7", 3, lambda f: f.pr["base"].update(ref="main")),
            ("pulls/7", 3, lambda f: f.pr["base"].pop("ref")),
            (
                "actions/runs/20",
                1,
                lambda f: f.full["pull_requests"][0]["base"].update(ref="main"),
            ),
            (
                "actions/runs/10",
                1,
                lambda f: f.metadata["pull_requests"][0]["base"].update(ref="main"),
            ),
        )
        for index, (trigger, occurrence, change) in enumerate(changes):
            with self.subTest(case=index):
                fake = FakeGitHub(target="dev")
                reads = 0

                def final_fence(path):
                    nonlocal reads
                    if path == trigger:
                        reads += 1
                        if reads == occurrence:
                            change(fake)

                fake.on_read = final_fence
                self.assertFalse(self.exercise(fake))
                self.assertEqual(fake.writes, [])

    def test_target_api_errors_fail_without_a_write_on_initial_or_final_read(self):
        for fail_at in (1, 3):
            with self.subTest(fail_at=fail_at):
                fake = FakeGitHub(target="dev")
                reads = 0

                def unavailable(path):
                    nonlocal reads
                    if path == "branches/dev":
                        reads += 1
                        if reads == fail_at:
                            raise urllib.error.URLError("synthetic branch API failure")

                fake.on_read = unavailable
                with self.assertRaises(urllib.error.URLError):
                    self.exercise(fake)
                self.assertEqual(fake.writes, [])

    def test_failure_then_success_recovers_only_gate_once_for_both_families(self):
        for kind in ("healthcare", "drug"):
            with self.subTest(kind=kind):
                fake = FakeGitHub(kind)
                fake.full.update(conclusion="failure", run_attempt=1)
                self.assertFalse(self.exercise(fake))
                self.assertEqual(fake.writes, [])
                fake.full.update(conclusion="success", run_attempt=2)
                event = deepcopy(fake.full)
                self.assertTrue(self.exercise(fake, event))
                self.assertEqual(fake.writes, [101])
                self.assertFalse(self.exercise(fake, event))
                self.assertEqual(fake.writes, [101])
                fake.metadata.update(status="completed", conclusion="failure")
                self.assertFalse(
                    self.exercise(fake, event), "failed recovery must not retry"
                )
                self.assertEqual(fake.writes, [101])

    def test_full_authority_and_metadata_completion_negative_cases(self):
        for conclusion in ("failure", "cancelled", "timed_out", None):
            fake = FakeGitHub()
            fake.full.update(
                conclusion=conclusion,
                status="in_progress" if conclusion is None else "completed",
            )
            with self.subTest(full_conclusion=conclusion):
                self.assertFalse(self.exercise(fake))
                self.assertEqual(fake.writes, [])
        for status, conclusion, attempt in (
            ("queued", None, 1),
            ("in_progress", None, 1),
            ("completed", "cancelled", 1),
            ("completed", "success", 1),
            ("completed", "failure", 2),
        ):
            fake = FakeGitHub()
            fake.metadata.update(
                status=status, conclusion=conclusion, run_attempt=attempt
            )
            with self.subTest(metadata=(status, conclusion, attempt)):
                self.assertFalse(self.exercise(fake))
                self.assertEqual(fake.writes, [])
        fake = FakeGitHub()
        self.assertFalse(
            self.exercise(fake, fake.metadata), "metadata completion cannot recurse"
        )

    def test_closed_changed_fork_and_absent_pr_associations_are_no_ops(self):
        changes = (
            lambda f: f.pr.update(state="closed"),
            lambda f: f.pr.update(merged=True),
            lambda f: f.pr["head"].update(sha="c" * 40),
            lambda f: f.pr["base"].update(sha="c" * 40),
            lambda f: f.pr["head"]["repo"].update(full_name="sample/fork"),
            lambda f: f.pr["base"]["repo"].update(full_name="sample/other"),
            lambda f: f.pr["base"].update(ref="other"),
            lambda f: f.full.update(pull_requests=[]),
            lambda f: f.metadata.update(pull_requests=[]),
            lambda f: f.full["pull_requests"][0].update(number=8),
            lambda f: f.full["pull_requests"][0]["base"].update(sha="c" * 40),
            lambda f: f.full.update(path=".github/workflows/other.yml"),
            lambda f: f.full.update(workflow_id=51),
            lambda f: f.full.update(event="push"),
            lambda f: f.full["repository"].update(full_name="sample/other"),
            lambda f: f.full["head_repository"].update(full_name="sample/fork"),
            lambda f: setattr(f, "content", b"unreviewed workflow edits"),
        )
        for index, change in enumerate(changes):
            with self.subTest(case=index):
                fake = FakeGitHub()
                change(fake)
                self.assertFalse(self.exercise(fake))
                self.assertEqual(fake.writes, [])

    def test_latest_full_attempt_and_latest_metadata_run_are_required(self):
        for status, conclusion in (
            ("completed", "failure"),
            ("completed", "cancelled"),
            ("queued", None),
            ("in_progress", None),
        ):
            fake = FakeGitHub()
            newer = run(30, "CI", conclusion)
            newer["status"] = status
            fake.runs.append(newer)
            with self.subTest(newer_full=(status, conclusion)):
                self.assertFalse(self.exercise(fake, deepcopy(fake.full)))
                self.assertEqual(fake.writes, [])
        fake = FakeGitHub()
        fake.runs.append(run(30, "CI metadata update", "success"))
        self.assertFalse(self.exercise(fake))

    def test_delayed_success_event_reconciles_latest_success_for_same_identity(self):
        for kind in ("healthcare", "drug"):
            for later_attempt in (False, True):
                with self.subTest(kind=kind, later_attempt=later_attempt):
                    fake = FakeGitHub(kind, target="dev")
                    event = deepcopy(fake.full)
                    if later_attempt:
                        fake.full["run_attempt"] += 1
                        latest_id = fake.full["id"]
                    else:
                        newer = run(30, "CI", "success", target="dev")
                        fake.runs.append(newer)
                        latest_id = newer["id"]
                    self.assertTrue(self.exercise(fake, event))
                    self.assertEqual(fake.writes, [101])
                    self.assertIn(f"actions/runs/{latest_id}", fake.calls)
                    self.assertFalse(self.exercise(fake, event))
                    self.assertEqual(fake.writes, [101])

    def test_newer_full_run_with_ambiguous_or_changed_association_never_exposes_old_success(
        self,
    ):
        changes = (
            lambda r: r.update(pull_requests=[]),
            lambda r: r["pull_requests"].append(deepcopy(r["pull_requests"][0])),
            lambda r: r["pull_requests"][0]["base"].update(sha="c" * 40),
            lambda r: r["pull_requests"][0].update(number=8),
            lambda r: r.update(workflow_id=51),
            lambda r: r.update(path=".github/workflows/other.yml"),
        )
        for index, change in enumerate(changes):
            with self.subTest(case=index):
                fake = FakeGitHub()
                newer = run(30, "CI", "failure")
                change(newer)
                fake.runs.append(newer)
                self.assertFalse(self.exercise(fake))
                self.assertEqual(fake.writes, [])

    def test_only_complete_portable_success_and_exact_skips_are_eligible(self):
        changes = (
            lambda f: f.jobs[0].update(conclusion="failure"),
            lambda f: f.jobs[0].update(conclusion="skipped"),
            lambda f: f.jobs[2].update(conclusion="failure"),
            lambda f: f.jobs[2].update(status="in_progress", conclusion=None),
            lambda f: f.jobs[2].update(name="unrelated metadata only"),
            lambda f: f.jobs[2].update(
                steps=[{"name": "unexpected execution", "conclusion": "success"}]
            ),
            lambda f: f.jobs[1]["steps"][0].update(name="Quality check"),
            lambda f: f.jobs[1]["steps"].append(
                {"name": "Other check", "conclusion": "failure"}
            ),
            lambda f: f.jobs[1]["steps"].append(
                {"name": "Unexpected execution", "conclusion": "success"}
            ),
            lambda f: f.jobs.pop(),
        )
        for index, change in enumerate(changes):
            with self.subTest(case=index):
                fake = FakeGitHub()
                change(fake)
                self.assertFalse(self.exercise(fake))
                self.assertEqual(fake.writes, [])
        for path, change in (
            ("actions/runs/20", lambda f: f.full.update(run_attempt=3)),
            ("actions/runs/20", lambda f: f.full.update(conclusion="cancelled")),
            ("actions/jobs/101", lambda f: f.jobs[1].update(run_attempt=2)),
            ("actions/jobs/101", lambda f: f.jobs[1].update(status="in_progress")),
            ("actions/jobs/101", lambda f: f.jobs[1].update(run_id=11)),
            ("actions/jobs/101", lambda f: f.jobs[1].update(head_sha="c" * 40)),
            ("actions/jobs/101", lambda f: f.pr["base"].update(sha="c" * 40)),
            ("actions/jobs/101", lambda f: f.pr.update(state="closed")),
        ):
            fake = FakeGitHub()
            fake.on_read = lambda observed, path=path, change=change: (
                change(fake) if observed == path else None
            )
            with self.subTest(final_fence_path=path):
                self.assertFalse(self.exercise(fake, deepcopy(fake.full)))
                self.assertEqual(fake.writes, [])
        fake = FakeGitHub()
        self.assertFalse(self.exercise(fake, marker=False))
        fake = FakeGitHub()
        fake.jobs[2]["id"] = fake.jobs[0]["id"]
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            self.exercise(fake)
        self.assertEqual(fake.writes, [])

    def test_changed_authority_and_one_read_of_lag_cannot_repeat_a_rerun(self):
        for field, value in (
            ("run_attempt", 2),
            ("status", "in_progress"),
            ("conclusion", "cancelled"),
        ):
            fake = FakeGitHub()

            def change(path):
                if path == "actions/runs/10":
                    fake.metadata[field] = value

            fake.on_read = change
            with self.subTest(changed_field=field):
                self.assertFalse(self.exercise(fake))
                self.assertEqual(fake.writes, [])
        for visible_at in ("second inventory", "direct run"):
            with self.subTest(visible_at=visible_at):
                fake = FakeGitHub()
                self.assertTrue(self.exercise(fake))
                current = deepcopy(fake.metadata)
                fake.metadata.update(
                    run_attempt=1, status="completed", conclusion="failure"
                )
                reads = 0

                def catch_up(path):
                    nonlocal reads
                    if path.startswith("actions/workflows/ci.yml/runs?"):
                        reads += 1
                        if visible_at == "second inventory" and reads == 2:
                            fake.metadata.update(current)
                    if visible_at == "direct run" and path == "actions/runs/10":
                        fake.metadata.update(current)

                fake.on_read = catch_up
                self.assertFalse(self.exercise(fake))
                self.assertEqual(fake.writes, [101])

    def test_markers_are_exact_timestamped_output_and_logs_are_bounded(self):
        marker = f"{recovery.MARKER} source={HEAD} base={BASE}"
        bodies = (
            (f"2026-01-01T00:00:00.123Z {marker}\n".encode(), True),
            (f"2026-01-01T00:00:00Z printf '{marker}\\n'\n".encode(), False),
            (f"2026-01-01T00:00:00Z {marker} extra\n".encode(), False),
            (marker.replace(BASE, "c" * 40).encode(), False),
        )
        for body, expected in bodies:
            with (
                self.subTest(body=body),
                patch.dict(os.environ, GH_TOKEN="synthetic"),
                patch("urllib.request.build_opener") as opener,
            ):
                opener.return_value.open.return_value.__enter__.return_value.read.return_value = body
                self.assertEqual(
                    recovery.dependency_failed(REPOSITORY, 101, HEAD, BASE), expected
                )
                opener.return_value.open.return_value.__enter__.return_value.read.assert_called_once_with(
                    1024 * 1024 + 1
                )
        with (
            patch.dict(os.environ, GH_TOKEN="synthetic"),
            patch("urllib.request.build_opener") as opener,
        ):
            opener.return_value.open.return_value.__enter__.return_value.read.return_value = (
                b"x" * (1024 * 1024 + 1)
            )
            with self.assertRaisesRegex(ValueError, "bounded"):
                recovery.dependency_failed(REPOSITORY, 101, HEAD, BASE)

    def test_log_redirect_drops_token_and_rerun_never_redirects_or_retries(self):
        redirects = recovery.LogRedirects()
        request = urllib.request.Request(
            "https://api.github.com/example", headers={"Authorization": "synthetic"}
        )
        moved = redirects.redirect_request(
            request, None, 302, "Found", {}, "https://logs.example/log"
        )
        self.assertIsNone(moved.get_header("Authorization"))
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            redirects.redirect_request(
                request, None, 302, "Found", {}, "http://logs.example/log"
            )
        with self.assertRaisesRegex(ValueError, "must not redirect"):
            redirects.redirect_request(
                urllib.request.Request(request.full_url, method="POST"),
                None,
                302,
                "Found",
                {},
                "https://logs.example/log",
            )
        for status in (201, 202, 409, 500):
            with (
                self.subTest(status=status),
                patch.dict(os.environ, GH_TOKEN="synthetic"),
                patch("urllib.request.build_opener") as opener,
            ):
                call = opener.return_value.open
                if status >= 400:
                    call.side_effect = urllib.error.HTTPError(
                        "https://api.github.com/example", status, "synthetic", {}, None
                    )
                else:
                    call.return_value.__enter__.return_value.status = status
                if status in (201, 409):
                    self.assertEqual(
                        recovery.request_rerun(REPOSITORY, 101), status == 201
                    )
                else:
                    with self.assertRaises(
                        (ValueError, urllib.error.HTTPError)
                    ) as raised:
                        recovery.request_rerun(REPOSITORY, 101)
                    if isinstance(raised.exception, urllib.error.HTTPError):
                        raised.exception.close()
                call.assert_called_once()
                self.assertEqual(call.call_args.args[0].get_method(), "POST")
                self.assertTrue(
                    call.call_args.args[0].full_url.endswith("/actions/jobs/101/rerun")
                )
        with (
            patch.dict(os.environ, GH_TOKEN="synthetic"),
            patch("urllib.request.build_opener") as opener,
        ):
            opener.return_value.open.side_effect = TimeoutError(
                "uncertain write response"
            )
            with self.assertRaises(TimeoutError):
                recovery.request_rerun(REPOSITORY, 101)
            opener.return_value.open.assert_called_once()

    def test_inventory_limit_and_duplicate_run_identity_fail_closed(self):
        for data in (
            {"total_count": 1001, "workflow_runs": []},
            {"total_count": 2, "workflow_runs": [run(20, "CI", "success")]},
            {"total_count": 2, "workflow_runs": [run(20, "CI", "success")] * 2},
        ):
            with (
                self.subTest(data=data),
                patch.object(recovery.github, "api", return_value=deepcopy(data)),
            ):
                with self.assertRaises(ValueError):
                    recovery.workflow_runs(REPOSITORY, HEAD)

    def test_workflow_inventory_pagination_and_workflow_bytes_fail_closed(self):
        runs = [{"id": index} for index in range(101)]
        with patch.object(
            recovery.github,
            "api",
            side_effect=[
                {"total_count": 101, "workflow_runs": runs[:100]},
                {"workflow_runs": runs[100:]},
            ],
        ) as api:
            self.assertEqual(recovery.workflow_runs(REPOSITORY, HEAD), runs)
            self.assertEqual(api.call_count, 2)
            self.assertTrue(api.call_args.args[1].endswith("page=2"))
        for value in (
            {"type": "symlink", "encoding": "base64", "size": 1},
            {"type": "file", "encoding": "base64", "size": 1024 * 1024 + 1},
            {"type": "file", "encoding": "utf-8", "size": 1},
            {"type": "file", "encoding": "base64", "size": 2, "content": "eA=="},
        ):
            with (
                self.subTest(value=value),
                patch.object(recovery.github, "api", return_value=value),
            ):
                with self.assertRaises(ValueError):
                    recovery.workflow_bytes(REPOSITORY, HEAD)

    def test_entrypoint_requires_default_branch_event_run_and_exact_helper_pin(self):
        pin = "1" * 40
        own_run = {
            "id": 90,
            "path": recovery.RECOVERY_WORKFLOW,
            "event": "workflow_run",
            "status": "in_progress",
            "conclusion": None,
            "head_sha": BASE,
            "head_branch": "main",
            "run_attempt": 1,
            "repository": {"full_name": REPOSITORY},
            "head_repository": {"full_name": REPOSITORY},
        }
        payload = {
            "action": "completed",
            "repository": {
                "full_name": REPOSITORY,
                "private": False,
                "default_branch": "main",
            },
            "workflow_run": {"id": 20},
        }
        environment = {
            "GITHUB_REPOSITORY": REPOSITORY,
            "GITHUB_EVENT_NAME": "workflow_run",
            "GITHUB_JOB": "metadata-recovery",
            "GITHUB_REF": "refs/heads/main",
            "CI_REVISION": pin,
            "GITHUB_RUN_ID": "90",
            "GITHUB_RUN_ATTEMPT": "1",
            "GITHUB_SHA": BASE,
        }
        with tempfile.TemporaryDirectory() as directory:
            event = Path(directory) / "event.json"
            environment["GITHUB_EVENT_PATH"] = str(event)
            changes = (
                lambda e, p, r: e.update(GITHUB_EVENT_NAME="pull_request"),
                lambda e, p, r: e.update(GITHUB_JOB="source-validation"),
                lambda e, p, r: e.update(GITHUB_REF="refs/pull/7/merge"),
                lambda e, p, r: e.update(CI_REVISION="2" * 40),
                lambda e, p, r: p.update(action="requested"),
                lambda e, p, r: p["repository"].update(private=True),
                lambda e, p, r: p["repository"].update(default_branch="other"),
                lambda e, p, r: r.update(id=91),
                lambda e, p, r: r.update(path=recovery.WORKFLOW),
                lambda e, p, r: r.update(event="pull_request"),
                lambda e, p, r: r.update(status="completed"),
                lambda e, p, r: r.update(head_sha=HEAD),
                lambda e, p, r: r.update(run_attempt=2),
            )
            for index, change in enumerate((None, *changes)):
                env, body, current = (
                    deepcopy(environment),
                    deepcopy(payload),
                    deepcopy(own_run),
                )
                if change:
                    change(env, body, current)
                event.write_text(json.dumps(body))
                with (
                    self.subTest(case=index),
                    patch.dict(os.environ, env),
                    patch.dict(recovery.KINDS, {REPOSITORY: "healthcare"}),
                    patch.object(
                        recovery.subprocess, "check_output", return_value=pin + "\n"
                    ),
                    patch.object(recovery.github, "api", return_value=current),
                    patch.object(
                        recovery, "workflow_bytes", return_value=TRUSTED_WORKFLOW
                    ),
                    patch.object(recovery, "recover", return_value=False) as recover,
                    patch("builtins.print"),
                ):
                    if change:
                        with self.assertRaises(ValueError):
                            recovery.main("healthcare")
                        recover.assert_not_called()
                    else:
                        recovery.main("healthcare")
                        recover.assert_called_once_with(
                            REPOSITORY,
                            "healthcare",
                            body["workflow_run"],
                        )

    def test_renderer_outputs_only_explicit_trusted_recovery_and_preserves_default_cli(
        self,
    ):
        self.assertEqual(recovery.METADATA_ONLY, renderer.METADATA_ONLY)
        with tempfile.TemporaryDirectory() as directory:
            caller = Path(directory) / "ci.yml"
            original = yaml.safe_dump(
                {
                    "name": "CI",
                    "on": {"pull_request": {}},
                    "jobs": {
                        "smoke": {
                            "name": "portable import checks",
                            "runs-on": "ubuntu-latest",
                            "steps": [{"run": "true"}],
                        }
                    },
                }
            )
            caller.write_text(original)
            for kind in ("healthcare", "drug"):
                with self.subTest(kind=kind):
                    source = yaml.safe_load(
                        renderer.render_workflow(kind, "1" * 40, caller)
                    )
                    skipped = [
                        job["name"][4:-3].strip()
                        for job in source["jobs"].values()
                        if "(metadata only)" in job["name"]
                    ]
                    self.assertEqual(
                        set(skipped),
                        {
                            next(
                                name
                                for name in recovery.skipped_names(label)
                                if name.startswith(recovery.METADATA_ONLY)
                            )
                            for label in recovery.SKIPPED[kind]
                        },
                    )
                    compiled = yaml.load(
                        renderer.render_metadata_recovery(kind, "1" * 40),
                        Loader=yaml.BaseLoader,
                    )
                    self.assertEqual(
                        compiled["on"],
                        {"workflow_run": {"workflows": ["CI"], "types": ["completed"]}},
                    )
                    self.assertEqual(
                        compiled["permissions"],
                        {
                            "contents": "read",
                            "pull-requests": "read",
                            "actions": "write",
                        },
                    )
                    self.assertEqual(
                        compiled["concurrency"]["cancel-in-progress"], "false"
                    )
                    eligibility = compiled["jobs"]["recover"]["if"][4:-3].strip()
                    self.assertEqual(
                        compiled["concurrency"]["group"],
                        "metadata-recovery-${{ "
                        + eligibility
                        + " && format('{0}-{1}-{2}-{3}-{4}', "
                        + "github.event.workflow_run.pull_requests[0].number, "
                        + "github.event.workflow_run.head_sha, "
                        + "github.event.workflow_run.pull_requests[0].base.sha, "
                        + "github.event.workflow_run.pull_requests[0].base.ref, "
                        + "github.event.workflow_run.workflow_id)"
                        + " || format('skip-{0}', github.event.workflow_run.id) }}",
                        "ineligible completions must not replace pending recovery",
                    )
                    for fence in (
                        "github.event.workflow_run.pull_requests[0].number > 0",
                        "!github.event.workflow_run.pull_requests[1]",
                        "github.event.workflow_run.workflow_id > 0",
                        "github.event.workflow_run.repository.full_name == github.repository",
                        "github.event.workflow_run.head_repository.full_name == github.repository",
                        "github.event.workflow_run.head_sha == github.event.workflow_run.pull_requests[0].head.sha",
                        "github.event.workflow_run.pull_requests[0].base.sha",
                        "github.event.workflow_run.pull_requests[0].base.ref == 'main'",
                        "github.event.workflow_run.pull_requests[0].base.ref == 'dev'",
                    ):
                        self.assertIn(fence, eligibility)
                    self.assertEqual(
                        compiled["jobs"]["recover"]["uses"],
                        "EndurantDevs/endurant-ci/.github/workflows/metadata-recovery.yml@"
                        + "1" * 40,
                    )
                    self.assertNotIn("steps", compiled["jobs"]["recover"])
                    self.assertIn(
                        "display_title == 'CI'", compiled["jobs"]["recover"]["if"]
                    )
            result = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(ROOT / "scripts/render_workflow.py"),
                    "healthcare",
                    "1" * 40,
                    str(caller),
                    "--metadata-recovery",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertEqual(caller.read_text(), original)
            caller.with_name("metadata-recovery.yml").unlink()
            result = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(ROOT / "scripts/render_workflow.py"),
                    "healthcare",
                    "1" * 40,
                    str(caller),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertFalse(caller.with_name("metadata-recovery.yml").exists())

    def test_recovery_template_executes_only_pinned_helpers_and_has_narrow_permissions(
        self,
    ):
        workflow = yaml.load(
            (ROOT / ".github/workflows/metadata-recovery.yml").read_text(),
            Loader=yaml.BaseLoader,
        )
        self.assertEqual(set(workflow["on"]), {"workflow_call"})
        self.assertEqual(
            workflow["permissions"],
            {"contents": "read", "pull-requests": "read", "actions": "write"},
        )
        job = workflow["jobs"]["metadata-recovery"]
        self.assertEqual(job["runs-on"], "ubuntu-latest")
        self.assertEqual(len(job["steps"]), 2)
        checkout, execute = job["steps"]
        self.assertEqual(
            checkout["with"],
            {
                "repository": "EndurantDevs/endurant-ci",
                "ref": "${{ inputs.ci_revision }}",
                "path": "ci",
                "persist-credentials": "false",
            },
        )
        self.assertEqual(
            execute["run"], 'python3 ci/scripts/metadata_recovery.py "$WORKFLOW_KIND"'
        )
        self.assertNotIn("secrets", json.dumps(workflow))
        self.assertNotIn("packages", workflow["permissions"])


if __name__ == "__main__":
    unittest.main()
