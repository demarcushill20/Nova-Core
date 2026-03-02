---
name: self-verification
description: "Validate tool results and skill outputs to ensure correctness, completeness, and contract compliance before finalizing a task."
activation:
  keywords:
    - verify
    - health
    - check
    - diagnose
    - integrity
    - contract
    - validate
    - confirm
  when:
    - Task execution completed
    - File modification performed
    - Git operation completed
    - Shell command executed
    - Output contract required
tool_doctrine:
  runtime:
    workflow:
      - check_expected_state
      - confirm_no_errors
      - validate_contract_fields
      - prefer_read_after_write
output_contract:
  required:
    - summary
    - checks_performed
    - result
    - confidence
---

# When To Use

- After completing a task, to confirm outputs and logs are correct.
- After any file modification, to verify the change landed as intended.
- After git operations, to confirm commit, push, or branch state.
- After shell commands, to validate exit codes and expected side effects.
- Before reporting task completion — never mark `.done` without verification.
- Periodic health checks of the nova-core environment.

# Workflow

1. **Identify expected state** — determine what success looks like before checking.
2. **Read after write** — re-read any file that was modified to confirm contents.
3. **Check exit codes** — verify shell and git commands returned expected codes.
4. **Validate contract fields** — confirm all required fields are present and non-empty.
5. **Cross-reference** — check that related artifacts exist (e.g., `.done` task has output file).
6. **Score confidence** — assign `high`, `medium`, or `low` based on check coverage.
7. **Report** — emit a structured verification result.

For verification philosophy, confidence scoring, and false-positive guidance, see `reference.md`.

# Tool Usage Rules

- **Read-only by default.** Do not modify files during verification unless repairing a known, deterministic inconsistency.
- All checks scoped to `~/nova-core`.
- Never assume success — always confirm with an independent read or status check.
- Do not trust exit code 0 alone — also inspect stdout/stderr for warnings.
- Report anomalies clearly but do not auto-fix unless the fix is safe and deterministic.
- Prefer concrete checks (file exists, content matches) over heuristic checks (output "looks right").

# Verification

Self-verification verifies other skills. Its own verification is:

1. Confirm the verification report was generated with all required fields.
2. Confirm `checks_performed` lists at least one concrete check.
3. Confirm `result` is one of: `pass`, `fail`, `partial`.
4. Confirm `confidence` is one of: `high`, `medium`, `low`.

# Failure Handling

- **Expected file missing**: report the missing path and mark result as `fail`.
- **Exit code mismatch**: report expected vs. actual code, mark `fail`.
- **Contract field missing**: report which fields are absent, mark `fail`.
- **Partial success**: some checks pass, some fail. Mark result as `partial`, lower confidence.
- **Verification itself errors**: report the error clearly. Do not suppress exceptions to fake a `pass`.

# Output Contract

Every self-verification execution must end with a machine-checkable contract:

```
## CONTRACT
summary: <one-line description of what was verified>
checks_performed:
  - <check description> (<pass | fail>)
result: <pass | fail | partial>
confidence: <high | medium | low>
```

See `examples.md` for concrete instances.
