# Endurant CI

Shared validation workflows for public Python services. Application source and tests remain in their own repositories; orchestration and reusable validation scripts live here.

Call the reusable workflow at an exact commit and set `ci_revision` to the same commit. Jobs run on standard GitHub-hosted Linux runners with read-only repository permissions. Do not pass secrets.

Public jobs produce source-bound measurements. A separate trusted consumer must verify the workflow revision, complete job results, run attempt and artifact provenance before using them for protected acceptance or deployment. A public workflow result does not grant deployment authority.

Repository CI validates workflow syntax and runs the helper tests. Service integration is verified by the caller repositories against exact source commits.
