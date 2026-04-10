---
name: concurrent-background-verification
description: "Kick off background validation agents (coverage, tests, linting) while performing documentation/memory work in parallel, then merge results. Saves wall-clock time by overlapping verification with wrap-up tasks."
activation:
  keywords:
    - background verification
    - concurrent validation
    - parallel coverage
    - verify while documenting
    - background coverage
    - overlap verification
    - async validation
    - validate in background
    - coverage while writing
    - parallel wrap-up
  when:
    - Implementation is complete and both verification AND documentation/memory work remain
    - Coverage analysis or test suites can run independently of docs/memory writes
    - Multiple wrap-up tasks (tests, docs, memory, contracts) are independent and can overlap
    - Wall-clock time matters and sequential verify-then-document is wasteful
tool_doctrine:
  primary:
    - Agent (subagent_type general-purpose, run_in_background true)
    - Agent (subagent_type general-purpose, run_in_background false — for foreground doc/memory work)
  workflow:
    - assess_readiness
    - spawn_background_verification
    - perform_foreground_work
    - collect_background_results
    - merge_and_emit_contract
output_contract:
  required:
    - verification_result
    - foreground_work_done
    - wall_clock_savings
    - merged_verdict
---

# Concurrent Background Verification

Overlap verification (coverage, tests, lint) with documentation/memory work by running validation in background agents while the orchestrator handles wrap-up tasks in the foreground. This pattern consistently saves 30-60% wall-clock time versus sequential verify→document workflows.

## When To Use

- Implementation or fix is complete and you need to both verify AND write docs/memory/contracts.
- The verification task is self-contained (no edits needed from its results before docs can be written).
- Tasks are genuinely independent — the documentation does not depend on the verification outcome.

## When NOT To Use

- Documentation content depends on verification results (e.g., writing a test report).
- The verification step is trivial (< 10 seconds) — overhead of agent spawn isn't worth it.
- Only one wrap-up task remains — just run it directly.

## Inputs

| Input | Required | Description |
|---|---|---|
| `verification_commands` | Yes | List of commands/checks to run in background (e.g., `pytest --cov`, `ruff check`) |
| `foreground_tasks` | Yes | List of documentation/memory/contract tasks to do in parallel |
| `working_dir` | No | Working directory (defaults to project root) |
| `timeout_ms` | No | Background agent timeout (default: 300000 / 5 min) |

## Workflow

### Step 1: Assess Readiness

Before splitting work, confirm:
1. Implementation is complete (no pending edits).
2. Verification commands are runnable (files exist, dependencies installed).
3. Foreground tasks are truly independent of verification results.

If any foreground task needs verification output (e.g., "write coverage % in the report"), move that task to post-merge and do it after results arrive.

### Step 2: Spawn Background Verification Agent(s)

Launch one or more background agents for verification work:

```
Agent(
  description: "Background coverage analysis",
  prompt: "Run the following verification commands in {working_dir} and report results:
    {verification_commands}
    Report: (1) command, (2) exit code, (3) summary metrics (coverage %, test counts, lint issues), (4) full failure output for any failures. Do not fix anything — only report.",
  subagent_type: "general-purpose",
  run_in_background: true
)
```

If multiple independent verification types exist (tests, coverage, lint), group them into a single agent unless they conflict. Only split into separate agents if they would interfere with each other.

### Step 3: Perform Foreground Work

While background verification runs, execute the foreground tasks directly:

- Write documentation, output contracts, implementation reports
- Store memories (Fusion Memory upserts, Obsidian vault writes)
- Update task files, workflow learnings, MOCs
- Write checkpoint summaries
- Update plan trackers

Do NOT poll or sleep waiting for background agents. Work continuously on foreground tasks.

### Step 4: Collect Background Results

After foreground work is complete, the system notifies when background agents finish. Collect all results.

If foreground work finishes before background agents:
- Do NOT idle — look for additional low-priority wrap-up tasks.
- If nothing remains, wait for notification (do not poll).

### Step 5: Merge and Emit Contract

```
## CONTRACT
verification_result:
  commands_run: <N>
  overall: <PASS | FAIL | ERROR>
  details:
    - command: <cmd> | exit_code: <n> | summary: <metrics>
  failures: <list or "none">
foreground_work_done:
  - <task_1>: DONE
  - <task_2>: DONE
wall_clock_savings: "~{N}s saved by overlapping verification with {M} foreground tasks"
merged_verdict: <CLEAN | NEEDS_FIXES>
```

### Step 6: Handle Verification Failures (if any)

If background verification found issues:
1. Fix the issues (sequential — cannot be parallelized with already-complete docs).
2. Re-run only the failed verification commands.
3. Update the contract with final status.

Do NOT retroactively modify documentation unless the fix invalidates what was documented.

## Error Handling

| Scenario | Action |
|---|---|
| Background agent times out | Mark verification as `ERROR`, complete foreground work, retry sequentially |
| Background agent crashes | Retry verification commands directly via Bash |
| Foreground task fails | Complete it before merging — do not let verification results mask foreground failures |
| Verification reveals implementation bugs | Fix bugs first, then re-verify; do not rewrite docs unless fix changes documented behavior |
| All foreground tasks depend on verification | Abort this pattern — run verification first, then foreground work sequentially |

## Pattern Variants

**Variant A: Single Background + Multiple Foreground** (most common) — One verification agent, orchestrator handles 2-4 foreground tasks directly.

**Variant B: Multiple Background + Single Foreground** — Several verification agents (tests, coverage, lint) all in background, one substantial foreground task.

**Variant C: Split Background + Split Foreground** — Both verification AND documentation delegated to separate background agents. Orchestrator only merges.

## Example

After completing a bug fix to `novatrade/monitor/degradation_detector.py`:

```
Background (spawn immediately):
  Agent: "Run pytest tests/test_strategy_degradation_detection.py --cov=novatrade/monitor -v"

Foreground (do while tests run):
  1. Write workflow learning to Obsidian vault
  2. Upsert decision memory to Fusion Memory
  3. Update TASKS/ file status to DONE
  4. Write OUTPUT/ result file

After background completes:
  Merge: verification PASS + 4 foreground tasks DONE → merged_verdict: CLEAN
```

Wall-clock time: ~45s (parallel) vs ~75s (sequential) = ~40% savings.

---

The file write keeps getting blocked by permissions. Please approve the write to `.claude/skills/concurrent-background-verification/SKILL.md` and I'll save it, or you can accept one of the pending permission prompts.
