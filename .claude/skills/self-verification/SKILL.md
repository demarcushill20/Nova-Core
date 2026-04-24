---
name: self-verification
description: "Validate tool results and skill outputs to ensure correctness, completeness, and contract compliance before finalizing a task. Enforces evidence-before-claims: no completion claim without fresh verification output."
merged_from:
  - upstream: obra/superpowers
    tag: v5.0.7
    commit: 1f20bef3f59b85ad7b52718f822e37c4478a3ff5
    path: skills/verification-before-completion/SKILL.md
    license: MIT
    merged: "2026-04-24"
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

# Evidence-Before-Claims Gate

> Merged from Superpowers `verification-before-completion` (v5.0.7).

Claiming work is complete without verification is dishonesty, not efficiency.

**Core principle:** evidence before claims, always.

## The Iron Law

```
NO COMPLETION CLAIMS WITHOUT FRESH VERIFICATION EVIDENCE
```

If you haven't run the verification command **in this message**, you cannot claim it passes. Stale evidence from an earlier turn does not count.

## The Gate Function

Before claiming any status or expressing satisfaction:

1. **IDENTIFY** — what command proves this claim?
2. **RUN** — execute the full command fresh and complete; no shortcuts.
3. **READ** — full output, check exit code, count failures.
4. **VERIFY** — does the output confirm the claim?
    - If NO: state actual status with evidence.
    - If YES: state the claim with evidence.
5. **ONLY THEN** make the claim.

Skipping any step = lying, not verifying.

## What Each Claim Requires

| Claim | Requires | Not Sufficient |
|-------|----------|----------------|
| Tests pass | Test command output: 0 failures | Previous run, "should pass" |
| Linter clean | Linter output: 0 errors | Partial check, extrapolation |
| Build succeeds | Build command: exit 0 | Linter passing, logs look good |
| Bug fixed | Test original symptom: passes | Code changed, assumed fixed |
| Regression test works | Red-green cycle verified | Test passes once |
| Agent completed | VCS diff shows changes | Agent reports "success" |
| Requirements met | Line-by-line checklist | Tests passing |

## Red Flags — STOP

- Using "should", "probably", "seems to"
- Expressing satisfaction before verification ("Great!", "Perfect!", "Done!")
- About to commit / push / PR without verification
- Trusting an agent's success report without checking the diff
- Relying on partial verification
- Thinking "just this once"
- Tired and wanting work over
- Any wording implying success without having run verification

## Rationalization Prevention

| Excuse | Reality |
|--------|---------|
| "Should work now" | Run the verification. |
| "I'm confident" | Confidence ≠ evidence. |
| "Just this once" | No exceptions. |
| "Linter passed" | Linter ≠ compiler. |
| "Agent said success" | Verify independently (check diff). |
| "I'm tired" | Exhaustion ≠ excuse. |
| "Partial check is enough" | Partial proves nothing. |
| "Different words, rule doesn't apply" | Spirit over letter. |

## Key Patterns

**Tests:**
```
✅ [run test command] [see: 34/34 pass] → "All tests pass"
❌ "Should pass now" / "Looks correct"
```

**Regression tests (TDD red-green):**
```
✅ Write → Run (pass) → Revert fix → Run (MUST FAIL) → Restore → Run (pass)
❌ "I've written a regression test" (without red-green verification)
```

**Build:**
```
✅ [run build] [see: exit 0] → "Build passes"
❌ "Linter passed" (linter doesn't check compilation)
```

**Requirements:**
```
✅ Re-read plan → create checklist → verify each → report gaps or completion
❌ "Tests pass, phase complete"
```

**Agent delegation:**
```
✅ Agent reports success → check VCS diff → verify changes → report actual state
❌ Trust the agent report as-is
```

## When This Applies

Always, before:
- Any variation of a success / completion claim
- Any expression of satisfaction
- Any positive statement about work state
- Committing, creating a PR, marking a task complete
- Moving to the next task
- Delegating to a subagent

The rule covers exact phrases, paraphrases, synonyms, and any implication of success.

## The Bottom Line

No shortcuts for verification. Run the command. Read the output. Then claim the result. This is non-negotiable.

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
