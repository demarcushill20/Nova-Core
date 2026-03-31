The sandbox is blocking directory creation under `.claude/skills/`. Here's the complete SKILL.md — save it to `.claude/skills/autonomous-anomaly-repair/SKILL.md`:

---
name: autonomous-anomaly-repair
description: "Autonomous observe-diagnose-fix-test-verify cycle for free-will exploration shifts. Detects anomalies in logs, state machines, or signal pipelines, diagnoses root cause, implements a minimal fix, writes regression tests, and verifies full suite passes — all without human intervention."
activation:
  keywords:
    - anomaly
    - free-will
    - exploration
    - autonomous repair
    - signal rejection
    - state desync
    - diagnose fix
    - self-repair
    - proactive fix
    - observation cycle
  when:
    - Free-will or exploration shift block is active
    - Anomalous patterns detected in logs or metrics
    - Unexpected signal rejections or state machine desyncs
    - Heartbeat detects degraded subsystem health
    - Agent has idle cycles and autonomy score permits proactive work
tool_doctrine:
  observe:
    workflow:
      - scan_logs_for_anomalies
      - check_recent_signal_rejections
      - inspect_state_machine_consistency
      - query_memory_for_known_issues
  diagnose:
    workflow:
      - trace_anomaly_to_root_cause
      - read_relevant_source_files
      - check_git_blame_for_recent_changes
      - classify_severity
  fix:
    workflow:
      - prefer_minimal_targeted_diff
      - never_refactor_beyond_scope
      - edit_not_rewrite
      - validate_no_regressions_introduced
  test:
    workflow:
      - write_regression_tests_for_fix
      - run_targeted_test_suite
      - verify_full_suite_green
  verify:
    workflow:
      - read_after_write
      - confirm_anomaly_resolved
      - log_fix_to_output
      - store_pattern_to_memory
output_contract:
  required:
    - anomaly_description
    - root_cause
    - files_changed
    - fix_summary
    - tests_added
    - test_results
    - verification_status
---

# Autonomous Anomaly Repair

Structured workflow for autonomous observe → diagnose → fix → test → verify cycles during free-will exploration shifts. Designed for proactive self-repair when the agent detects degraded behavior without human prompting.

## When To Activate

- During **free-will shift blocks** when the agent has autonomy to explore
- When **log scanning** reveals anomalous patterns (e.g., repeated signal rejections, unexpected state transitions)
- When **heartbeat metrics** show subsystem health degradation
- When **broker reconciliation** or external state has drifted from internal state
- Any time the agent observes behavior that doesn't match expected invariants

## Step-by-Step Workflow

### Phase 1: Observe

1. **Scan recent logs** — Check `LOGS/` and service journals for error patterns, warnings, or unexpected rejection counts.
2. **Inspect state consistency** — Compare internal state machines against expected states. Look for desyncs between components (e.g., position tracker vs broker state).
3. **Query memory** — Use `query_memory` to check if this anomaly matches a known pattern or prior fix.
4. **Quantify impact** — Count affected signals, rejected trades, or degraded metrics. Establish a baseline for "fixed" comparison.

**Exit criteria:** Anomaly identified and quantified, or no anomaly found (stop here).

### Phase 2: Diagnose

1. **Trace to root cause** — Follow the anomaly upstream through the call chain. Read the relevant source files.
2. **Check recent changes** — Use `git log` and `git blame` to see if a recent commit introduced the issue.
3. **Classify severity** — HIGH (data loss, wrong trades), MEDIUM (degraded signals, missed opportunities), LOW (cosmetic, logging noise).
4. **Formulate hypothesis** — State the root cause clearly before writing any code.

**Exit criteria:** Root cause identified with evidence. If uncertain, escalate to operator via Telegram.

### Phase 3: Fix

1. **Minimal diff only** — Fix the root cause with the smallest possible change. No drive-by refactors.
2. **Use Edit, not Write** — Targeted edits preserve surrounding code and reduce regression risk.
3. **Stay in scope** — If the fix requires touching more than 3 files or 100 lines, pause and reassess. Consider breaking into smaller steps or escalating.
4. **Preserve invariants** — Ensure the fix doesn't break existing contracts, APIs, or state machine transitions.

**Exit criteria:** Fix implemented, code compiles/parses cleanly.

### Phase 4: Test

1. **Write regression tests** — At minimum, write tests that reproduce the original anomaly and confirm the fix resolves it.
2. **Cover edge cases** — Add tests for boundary conditions the root cause exposed.
3. **Run targeted suite** — Execute tests for the affected module first (fast feedback).
4. **Run full suite** — Verify no regressions across the entire test suite.

**Exit criteria:** All new tests pass. Full suite green (or no new failures).

### Phase 5: Verify

1. **Read after write** — Re-read all modified files to confirm edits landed correctly.
2. **Confirm anomaly resolved** — Re-check the original symptom (re-scan logs, re-inspect state).
3. **Log the fix** — Write a structured output to `OUTPUT/` with timestamp, anomaly description, root cause, fix summary, and test results.
4. **Store pattern** — Use `upsert_memory` to record the anomaly pattern and fix for future recall.

**Exit criteria:** Verification complete, output logged, pattern stored.

## Expected Inputs

- **Shift context**: The current shift block type (free-will, exploration, maintenance)
- **Log access**: Ability to read `LOGS/`, `journalctl`, and service-specific logs
- **Test suite**: Working test runner (`pytest`) with existing test coverage
- **Memory access**: Fusion Memory for pattern matching and storage

## Expected Outputs

| Field | Type | Description |
|---|---|---|
| `anomaly_description` | string | What was observed and how it manifested |
| `root_cause` | string | Technical root cause with evidence |
| `files_changed` | list | Files modified by the fix |
| `fix_summary` | string | What was changed and why |
| `tests_added` | int | Number of new regression tests |
| `test_results` | string | Pass/fail counts for targeted and full suite |
| `verification_status` | enum | `VERIFIED`, `PARTIAL`, or `FAILED` |

## Error Handling

- **Cannot identify root cause**: Log the anomaly with all gathered evidence, store to memory, and escalate to operator. Do not guess-fix.
- **Fix introduces regressions**: Revert the fix immediately (`git checkout -- <files>`), log the regression, and escalate.
- **Test suite already broken**: Note pre-existing failures separately. Only count NEW failures as regressions from the fix.
- **Severity is HIGH**: Send a single Telegram alert before fixing. Do not spam — one alert per issue.
- **Fix scope exceeds 100 lines**: This is likely not a quick fix. Create a TASK file instead and defer to the next structured shift.

## Anti-Patterns to Avoid

- **Shotgun debugging** — Do not make speculative changes without a diagnosis.
- **Scope creep** — Do not refactor adjacent code, add features, or "improve" unrelated logic.
- **Silent fixes** — Always log and store the fix. Future sessions need to know what was changed and why.
- **Retry loops** — If the fix doesn't work on the first attempt, diagnose again rather than iterating blindly.