# Endurant CI

Shared validation workflows for public Python services. Application source and tests remain in their own repositories; orchestration and reusable validation scripts live here.

Compile the reviewed job graph into the service's `.github/workflows/ci.yml` with `uv run --with PyYAML==6.0.3 python scripts/render_workflow.py drug|healthcare <exact-package-commit> <caller-ci.yml>`. This keeps job labels prefix-free and pins every shared script checkout to the same commit. Validation jobs run on standard GitHub-hosted Linux runners with read-only repository permissions. Do not pass secrets.

PR title and base changes trigger validation. Description-only edits produce separately named, skipped checks in their own concurrency group, preserving active validation and required check results.

Public jobs produce source-bound measurements. A separate trusted consumer must verify the workflow revision, complete job results, run attempt and artifact provenance before using them for protected acceptance or deployment. A public workflow result does not grant deployment authority.

Repository CI validates workflow syntax and runs the helper tests. Service integration is verified by the caller repositories against exact source commits.

Generated service CI includes one final `CI artifact cleanup` job with `actions: write`; all validation jobs and canonical reusable workflows remain read-only. It runs only pinned shared helper code after smoke checks and aggregate validation, verifies every other job in its exact run attempt has passed, and removes known intermediates while its 90-day public measurement remains available. It never executes application code or artifact contents. Fork PRs skip deletion successfully; failed and cancelled runs retain their one-day debugging artifacts until GitHub expires them. Final measurements and unknown artifacts are preserved, changed evidence stops cleanup, and API errors fail visibly. Trusted consumers admitting a new caller must include this job and its two steps in their exact successful job inventory.
