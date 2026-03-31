Sandbox is blocking writes to `.claude/skills/`. Here's the complete SKILL.md — save it to `.claude/skills/post-impl-verification/SKILL.md`:

---
name: post-impl-verification
description: "Fast verification of already-completed implementations by counting deliverables, running tests, and spot-checking risk mitigations."
version: "1.0.0"
derived_from: self-verification
activation:
  keywords:
    - implementation complete
    - verify deliverables
    - verify implementation
    - count files
    - count tests
    - post-implementation
    - deliverable check
    - verify phase
  when:
    - Task description contains an 'Implementation Complete' section
    - Task lists specific deliverable counts and test counts
    - Task is verification-only (no new code to write)
    - Phase completion needs independent confirmation
tool_doctrine:
  read_only: true
  workflow:
    - glob_deliverables
    - run_test_suite
    - spot_check_risks
    - emit_contract
  tools:
    - Glob: count and locate deliverable files
    - Bash: run pytest with count assertion
    - Read: spot-check risk mitigations in source
    - Grep: search for risk-relevant patterns
output_contract:
  required:
    - summary
    - deliverable_count
    - expected_deliverable_count
    - test_count
    - expected_test_count
    - test_failures
    - risk_checks
    - result
    - confidence
---

# Post-Implementation Verification

Lightweight, fast (~3 min) verification skill for tasks where implementation is already complete. Triggered when a task description includes an "Implementation Complete" section listing deliverable counts, test counts, and risk mitigations.

This is a **read-only** skill. It never modifies code — only confirms that claimed deliverables exist and pass.

## When To Use

- A task file or plan section declares "Implementation Complete" with specific counts
- You need to verify a completed phase before marking it done
- A deliverable manifest needs independent confirmation
- Post-merge verification of a feature branch

## Inputs

The task description must contain (explicitly or inferrable):

| Field | Example |
|---|---|
| **Deliverable file patterns** | `skills/evolution/*.py`, `tests/test_evolution_*.py` |
| **Expected deliverable count** | 14 files |
| **Expected test count** | 539 tests |
| **Risk register** | List of mitigations to spot-check |

## Workflow

### Step 1: Parse Claims

Extract from the task description:
- File patterns and expected count
- Test suite path and expected test count
- Risk mitigations to verify

### Step 2: Count Deliverables

```
Glob for each deliverable pattern → count unique files → compare to expected count
```

- Use `Glob` to find all files matching claimed patterns
- Count results and compare against the expected deliverable count
- Flag any missing or unexpected files
- **PASS**: actual >= expected
- **FAIL**: actual < expected (list missing files)

### Step 3: Run Test Suite

```
pytest <test_path> -q --tb=short → parse collected count + failures
```

- Run `pytest` targeting the relevant test directory/files
- Parse output for: collected count, passed count, failed count, error count
- **PASS**: collected >= expected AND failures == 0
- **FAIL**: collected < expected OR failures > 0

### Step 4: Spot-Check Risk Mitigations

For each item in the risk register:
- Use `Grep` or `Read` to confirm the mitigation exists in source
- Check for defensive patterns (e.g., input validation, circuit breakers, error handling)
- At minimum spot-check 3 risk items (or all if fewer than 3)
- **PASS**: all checked mitigations found
- **PARTIAL**: some mitigations missing or ambiguous
- **FAIL**: critical mitigations absent

### Step 5: Emit Contract

Produce the structured output contract (see below).

## Error Handling

| Scenario | Action |
|---|---|
| Glob returns 0 files | Check pattern is correct; report FAIL with pattern used |
| Pytest import error | Report the import error; do not mask as test failure |
| Pytest timeout (>120s) | Report timeout; re-run with `-x` for first-failure info |
| Risk pattern not found | Expand search to alternate file paths before marking FAIL |
| Task has no risk register | Skip Step 4; note in contract; lower confidence to medium |
| Ambiguous deliverable count | Use the higher count from task description; flag discrepancy |

## Output Contract

Every execution must end with:

```
## CONTRACT
summary: <what was verified, one line>
deliverable_count: <actual>
expected_deliverable_count: <from task>
test_count: <actual collected>
expected_test_count: <from task>
test_failures: <0 or count>
risk_checks:
  - <mitigation description> (pass | fail)
result: <pass | fail | partial>
confidence: <high | medium | low>
duration: <wall-clock time>
```

### Confidence Scoring

- **high**: all 3 steps pass, counts match or exceed, zero failures
- **medium**: counts match but risk checks skipped or ambiguous
- **low**: any count mismatch or test failures present

## Examples

### Minimal invocation
> Task says: "Implementation Complete — 14 files, 539 tests, all passing"
>
> 1. Glob `skills/evolution/*.py` + `tests/test_evolution_*.py` → 14 files
> 2. `pytest tests/test_evolution_*.py -q` → 539 passed, 0 failed
> 3. Spot-check: input validation in processor.py, circuit breaker in runtime.py
> 4. Result: **pass**, confidence: **high**, duration: 2m 47s

### Partial failure
> Expected 14 files, found 13 (missing `phase10_integration.py`)
>
> Result: **partial**, confidence: **medium**
> Action: report missing file, do not auto-create