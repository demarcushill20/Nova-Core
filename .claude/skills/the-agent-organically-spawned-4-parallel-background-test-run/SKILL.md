The sandbox is blocking writes to `.claude/skills/`. Here's the complete SKILL.md — you can save it to `.claude/skills/parallel-test-fanout/SKILL.md`:

---
name: parallel-test-fanout
description: "Fan out N test suites as parallel background agents, collect all results, and assert cross-run consistency. Use for bulk validation, regression checks, or pre-merge confidence gates."
activation:
  keywords:
    - parallel tests
    - fan out
    - fanout
    - run all tests
    - validate all
    - bulk test
    - test suites parallel
    - regression sweep
    - cross-validate tests
    - concurrent test
  when:
    - Multiple independent test suites need to run simultaneously
    - Validating a broad change across several test modules
    - Pre-merge or post-implementation confidence gate
    - Task requires running N test commands and comparing results
tool_doctrine:
  primary:
    - Agent (subagent_type general-purpose, run_in_background true)
    - Bash (for direct pytest/test commands when N=1)
  workflow:
    - enumerate_suites
    - spawn_parallel_agents
    - await_all_results
    - aggregate_and_compare
    - emit_contract
output_contract:
  required:
    - suites_run
    - results_table
    - overall_verdict
    - failures
    - consistency_check
---

# Parallel Test Fanout

Fan out N independent test suites as parallel background agents, wait for all to complete, then aggregate results and assert consistency.

## When To Use

- Validating recent changes across multiple test files or modules.
- Running a full regression sweep before committing or merging.
- Post-implementation verification requiring multiple independent test runs.
- Any task where N≥2 independent test commands can run concurrently.

## Inputs

| Input | Required | Description |
|---|---|---|
| `test_commands` | Yes | List of test commands or pytest patterns to run (e.g., `["pytest tests/test_foo.py", "pytest tests/test_bar.py"]`) |
| `working_dir` | No | Working directory (defaults to project root) |
| `timeout_ms` | No | Per-suite timeout in ms (default: 300000 / 5 min) |
| `fail_fast` | No | If true, report immediately on first failure without waiting for remaining suites (default: false) |

## Workflow

### Step 1: Enumerate Suites

Determine the list of test suites to run. Sources (in priority order):
1. Explicit list provided by caller.
2. Glob for `tests/test_*.py` files changed in the current diff (`git diff --name-only`).
3. All `tests/test_*.py` files in the project (full sweep).

Deduplicate and sort. Log the final list before proceeding.

### Step 2: Spawn Parallel Background Agents

For each suite, launch an Agent with `run_in_background: true`:

```
Agent(
  description: "Run test suite: {suite_name}",
  prompt: "Run `{test_command}` in {working_dir}. Report: (1) exit code, (2) total tests, (3) passed, (4) failed, (5) errors, (6) skipped, (7) full failure output for any failing test. Do not fix failures — only report.",
  subagent_type: "general-purpose",
  run_in_background: true
)
```

All agents MUST be launched in a **single message** (one tool-call block with N Agent invocations) to maximize parallelism.

### Step 3: Await All Results

Do not poll or sleep. The system notifies when each background agent completes. Wait for all N agents to finish before proceeding.

### Step 4: Aggregate Results

Build a results table:

| Suite | Total | Passed | Failed | Errors | Skipped | Verdict |
|---|---|---|---|---|---|---|
| test_foo.py | 42 | 42 | 0 | 0 | 0 | PASS |
| test_bar.py | 18 | 17 | 1 | 0 | 0 | FAIL |

### Step 5: Consistency Check

- If all suites PASS → `overall_verdict: PASS`
- If any suite has failures → `overall_verdict: FAIL`, list each failure with file, test name, and error summary.
- If any agent timed out or crashed → `overall_verdict: ERROR`, flag the affected suite.

### Step 6: Emit Contract

```
## CONTRACT
suites_run: <N>
results_table:
  - suite: <name> | total: <n> | passed: <n> | failed: <n> | errors: <n> | skipped: <n> | verdict: <PASS|FAIL|ERROR>
overall_verdict: <PASS | FAIL | ERROR>
failures:
  - suite: <name> | test: <test_name> | error: <one-line summary>
consistency_check: <ALL_PASS | MIXED | ALL_FAIL>
```

## Error Handling

| Scenario | Action |
|---|---|
| Agent times out | Mark suite as `ERROR`, continue collecting remaining results |
| Agent crashes / returns no parseable output | Mark suite as `ERROR`, include raw output in failures |
| Import error in test file | Report as `ERROR` (not `FAIL`), include traceback |
| Zero test suites to run | Skip fanout, emit contract with `suites_run: 0` and `overall_verdict: SKIP` |
| Permission denied / missing file | Report in failures, mark `ERROR` |

## Sizing Guidelines

| Suite Count | Strategy |
|---|---|
| 1 | Run directly with Bash — no fanout overhead needed |
| 2–6 | Standard parallel fanout (this skill) |
| 7–15 | Group related suites into 4–6 batches to limit agent count |
| 16+ | Split into two rounds: critical suites first, then remaining |

## Example Invocation

Given a validation task touching cross_validation, action_executor, and decision_engine:

```
Suites:
  1. pytest tests/test_cross_validation.py -x
  2. pytest tests/test_action_executor.py -x
  3. pytest tests/test_decision_engine_extended.py -x
  4. pytest tests/test_investigation_executor.py -x

→ Spawn 4 background agents in one message
→ Collect results
→ Emit CONTRACT with overall_verdict
```

---

Skill created: **parallel-test-fanout** — captures the fan-out-N-suites-collect-and-compare pattern. Key design choices:
- Improved name from the verbose suggestion to `parallel-test-fanout`
- Enforces single-message agent launch for true parallelism
- Includes sizing guidelines (don't fanout for N=1, batch for N>6)
- Structured output contract with consistency check
- Error handling for timeouts, crashes, and import errors

The sandbox blocked the file write. You'll need to approve the write or I can try an alternative path.
