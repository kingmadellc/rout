---
name: oss-contributor-onboarding
description: Guide first-time and returning contributors through a consistent Rout onboarding flow. Use when a user asks how to make their first PR, needs local setup/testing steps, wants a scoped starter task, or needs help turning a local change into a review-ready contribution.
---

# OSS Contributor Onboarding

Use this workflow to move from zero context to a review-ready contribution.

## Workflow

1. Confirm repository state.
- Read `README.md`, `CONTRIBUTING.md`, and `.github/pull_request_template.md`.
- Run the baseline checks from `references/first-pr-guide.md`.

2. Select contribution scope.
- Prefer one behavior change per PR.
- If the task is broad, split into setup/docs first, runtime changes second.

3. Validate local environment.
- Run unit tests before editing.
- If tests fail on a clean tree, capture failures before making changes.

4. Implement and verify.
- Add or update tests for behavior changes.
- Re-run checks after edits.

5. Prepare contribution summary.
- List changed files, behavior impact, and validation steps.
- Include exact commands executed.

## Starter Task Selection

Use this rubric for first-time contributors:

- Best: docs quality gaps, missing tests, isolated handler bugs.
- Acceptable: small runtime fixes with existing test patterns.
- Avoid: cross-cutting refactors without maintainer alignment.

## Reference

Use `references/first-pr-guide.md` for command checklist and PR quality gate.
