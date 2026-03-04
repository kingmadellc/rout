---
name: rout-release-legal
description: Prepare Rout for public releases with legal and metadata hygiene. Use when publishing a version, auditing OSS readiness, updating licensing/release docs, or validating that release metadata matches repository contents.
---

# Rout Release Legal

Use this skill before tagging a release or announcing public availability.

## Workflow

1. Verify legal baseline.
- Confirm `LICENSE` exists and matches README claims.
- Confirm security and conduct documents exist.

2. Verify release metadata.
- Confirm version tags and release notes are consistent.
- Confirm README badges/claims match actual release state.

3. Verify contributor readiness.
- Confirm contribution docs and templates are present.
- Confirm CI checks are green for the target commit.

4. Produce release audit summary.
- List pass/fail for each gate.
- Include exact files changed for any fixes.

## Quality Gates

- No release with missing or contradictory license metadata.
- No release without contributor and security reporting paths.
- No release if CI is failing on the release commit.

## Reference

Use `references/release-checklist.md` as the source checklist.
