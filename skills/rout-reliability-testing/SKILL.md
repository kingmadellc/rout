---
name: rout-reliability-testing
description: Enforce reliability checks for Rout runtime and handlers. Use when modifying watcher polling/sending behavior, command parsing, handler contracts, config/path resolution, or test coverage around these flows.
---

# Rout Reliability Testing

Use this skill to keep runtime behavior safe while making changes.

## Workflow

1. Identify risk class.
- Parser/dispatch risk: `parse_command`, handler registry, invocation contract.
- Runtime IO risk: message polling, send path, file-backed state.
- Config risk: path resolution, credential fallbacks, startup checks.

2. Add the smallest effective tests.
- Prefer focused unit tests for parser/path logic.
- Add mock-mode tests for IO-dependent behavior.
- Avoid broad integration tests that require live iMessage.

3. Run reliability checks.
- Execute `scripts/run_reliability_checks.sh`.
- Capture failures and fix root causes before merge.

4. Document behavior deltas.
- In PR summary, state exactly what changed in runtime behavior.
- Include command output from validation checks.

## Quality Gates

- Every behavior change must have either a new test or an explicit manual validation note.
- Parser and routing changes require positive and negative test cases.
- Mock mode paths must not break normal mode paths.

## References

- `references/test-matrix.md`
- `scripts/run_reliability_checks.sh`
