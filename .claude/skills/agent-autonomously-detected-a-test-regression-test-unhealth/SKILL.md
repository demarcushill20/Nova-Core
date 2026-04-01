---
name: test-regression-autofix
description: "Detect test regressions, diagnose root cause, apply targeted fix, and verify full suite passes — autonomous detect→diagnose→fix→verify loop."
activation:
  keywords:
    - test regression
    - test failure
    - test timeout
    - test health
    - failing tests
    - broken tests
    - test suite red
    - flaky test
    - free will
    - exploration block
  when:
    - Free-will or exploration block with no explicit task assigned
    - Heartbeat detects test suite degradation
    - CI reports new test failures on main branch
    - Agent self-selects test health work during idle time
tool_doctrine:
  detect:
    workflow:
      - run_full_test_suite_with_timeout
      - capture_failures_and_stderr
      - classify_failure_type (timeout, assertion, import, crash)
  diagnose:
    workflow:
      - read_failing_test_source
      - read_code_under_test
      - check_recent_git_changes_to_touched_files
      - identify_root_cause (timeout_value, mock_drift, API_change, missing_fixture)
  fix:
    workflow:
      - prefer_smallest_safe_diff
      - edit_production_code_or_test_as_appropriate
      - never_delete_or_skip_test_to_make_suite_green
  verify:
    workflow:
      - rerun_previously_failing_test_in_isolation
      - rerun_full_suite_to_confirm_no_regressions
      - compare_test_count_before_and_after
output_contract:
  required:
    - failing_tests_detected
    - root_cause_diagnosis
    - fix_applied
    - verification_result
    - test_count_before
    - test_count_after
---

# Test Regression Autofix

Autonomous detect→diagnose→fix→verify loop for test suite health. Designed for free-will blocks where the agent self-selects work, or any context where test regressions need rapid triage.

## When To Use

- During free-will / exploration blocks when no higher-priority task exists
- When heartbeat or monitoring flags test suite degradation
- When a recent commit introduces test failures on main
- As a proactive health check after large refactors or dependency updates

## Inputs

- **Test command**: The project test runner invocation (default: `python -m pytest tests/ -x -q --timeout=30`)
- **Scope**: Full suite or targeted directory/file (prefer full suite for regression detection)
- **Prior state** (optional): Known passing test count for before/after comparison

## Workflow

### Phase 1: Detect

1. Run the full test suite with a reasonable timeout (30s per test default).
2. Capture stdout, stderr, and exit code.
3. If all tests pass, report `no regression detected` and exit.
4. Parse failure output to extract:
   - Test names and file paths
   - Failure type: `timeout`, `AssertionError`, `ImportError`, `AttributeError`, etc.
   - Count of failures vs. total tests

### Phase 2: Diagnose

1. Read the source of each failing test.
2. Read the production code under test (follow imports from the test file).
3. Run `git log --oneline -10 -- <touched_files>` to find recent changes.
4. Classify root cause:
   - **Timeout**: test hits real I/O or infinite loop; needs mock or timeout bump
   - **Mock drift**: production API changed but test mocks weren't updated
   - **Missing fixture**: new dependency not provided in test setup
   - **Logic regression**: actual bug in production code introduced by recent commit
   - **Flaky/environmental**: non-deterministic failure (timing, network, file system)
5. If root cause is a **logic regression in production code**, fix the production code — never patch the test to accept wrong behavior.

### Phase 3: Fix

1. Apply the smallest safe diff that resolves the root cause.
2. Rules:
   - **Never skip, delete, or `@pytest.mark.skip` a test** to make the suite green.
   - **Never weaken assertions** (e.g., changing `==` to `in` or removing checks).
   - **Prefer fixing production code** over adjusting tests when the production code is wrong.
   - **Prefer adjusting test expectations** only when the test was wrong (e.g., outdated mock, wrong expected value after intentional API change).
   - **Timeout fixes**: increase timeout only if the operation legitimately needs more time; otherwise add proper mocking.
3. Keep the diff minimal — do not refactor surrounding code.

### Phase 4: Verify

1. Re-run the previously failing test(s) in isolation to confirm the fix works.
2. Re-run the **full test suite** to confirm no new regressions were introduced.
3. Record:
   - `test_count_before`: total tests before the fix
   - `test_count_after`: total tests after (must be ≥ before; never lose tests)
   - `failures_before`: count of failures pre-fix
   - `failures_after`: must be 0
4. If new failures appear, return to Phase 2 — do not declare success with partial fixes.

## Error Handling

| Scenario | Action |
|----------|--------|
| Cannot reproduce failure | Run 3x with `--count=3`; if still no repro, flag as flaky and log |
| Root cause unclear after reading code + git log | Spawn Explore agent for deeper codebase search |
| Fix introduces new failures | Revert fix, re-diagnose with broader context |
| Test count decreases after fix | HARD FAIL — investigate why tests were lost |
| Timeout in full suite verification | Increase pytest timeout, retry once; if persistent, investigate resource leak |

## Anti-Patterns to Avoid

- **Deleting the failing test** — this hides the problem, doesn't fix it
- **Marking test as `xfail` or `skip`** without a linked issue/ticket
- **Broad rewrites** when a one-line fix suffices
- **Fixing symptoms** (e.g., bumping timeout to 300s) instead of root cause (missing mock)
- **Claiming success without full-suite verification** — isolated test pass is insufficient

## Output Contract

Every execution must produce:

```
## CONTRACT
failing_tests_detected:
  - <test_name> (<failure_type>)
root_cause_diagnosis: <one-line root cause>
fix_applied: <file:line — description of change>
verification_result: <pass | fail>
test_count_before: <N>
test_count_after: <N>
```

## Example

```
## CONTRACT
failing_tests_detected:
  - test_unhealthy_run (timeout — exceeded 10s)
root_cause_diagnosis: test called real subprocess with no mock; hung waiting for stdin
fix_applied: tests/test_watcher.py:142 — patched subprocess.run with mock returning rc=1
verification_result: pass
test_count_before: 11650
test_count_after: 11650
```

Save this to `.claude/skills/test-regression-autofix/SKILL.md` when the sandbox permits writes to that directory.
