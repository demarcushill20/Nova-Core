---
name: free-will-pivot
description: "When a free-will block discovers its assigned task is already done, pivot to autonomous high-value discovery instead of re-running tests and exiting."
activation:
  keywords:
    - free will
    - free-will
    - exploration block
    - already done
    - pivot
    - autonomous discovery
    - unstructured block
    - self-directed
  when:
    - A free-will or exploration shift block starts and the intended task is already complete
    - The agent detects prior work covers the planned scope
    - No specific task was assigned and the block is open-ended
tool_doctrine:
  discovery:
    workflow:
      - scan_tasks_queue
      - check_system_health
      - audit_state_files
      - run_overdue_maintenance
      - pursue_novatrade_research
  validation:
    workflow:
      - verify_prior_work_complete
      - confirm_no_regressions
      - assess_highest_value_pivot
output_contract:
  required:
    - pivot_reason
    - discovery_source
    - work_performed
    - summary
    - files_changed
    - verification
    - confidence
---

# Free-Will Pivot: Autonomous Discovery on Task Completion

When a free-will or exploration block discovers its intended work is already done, the agent must **not** waste the block by re-running tests and exiting. Instead, it pivots to the highest-value autonomous work available.

## When To Use

- A free-will shift block starts and the prior session already completed the planned work.
- The agent's initial assessment reveals nothing new to do on the original topic.
- An unstructured block has no specific assignment.

## Step 1: Confirm Prior Work (< 2 minutes)

Before pivoting, verify the prior work is genuinely complete:

1. Read the most recent `OUTPUT/` file for the same shift block stem.
2. Confirm the output has a valid CONTRACT section with `confidence: medium` or higher.
3. Spot-check one or two claims (e.g., run the specific tests mentioned, confirm file changes exist).
4. If prior work is incomplete or broken, **resume it** instead of pivoting.

**Do NOT** run the entire test suite as a confirmation step — that wastes the block.

## Step 2: Discovery Cascade

Execute this priority-ordered cascade. Stop at the first category that yields actionable work:

### Priority 1: Queued Tasks
```
ls TASKS/*.md  # pending tasks (not .done, .failed, .inprogress)
```
If any pending tasks exist, execute the highest-priority one using the `task-execution` skill.

### Priority 2: System Health Issues
```
cat HEARTBEAT.md  # check for any non-healthy items
cat STATE/progress_history.json | tail -5  # recent scores and trends
```
Look for:
- Any heartbeat check showing degraded/failing status
- Progress dimensions marked `degrading` or below 70
- Metric anomalies (failure rates, stale data, unbounded growth)
- Service issues (high memory, stale PIDs, log bloat)

### Priority 3: Overdue Maintenance
Scan for systemic hygiene issues:
- **STATE/ file sizes** — anything over 5MB needs rotation or pruning
- **Log retention** — run `scripts/log_retention.py` if logs are large
- **Test pollution** — check if test runs are contaminating production state files
- **Dead code / unused files** — stale `.inprogress` files, orphaned configs
- **Dependency health** — outdated packages, security advisories

### Priority 4: Code Quality
- Run `ruff` or linting on recently changed files
- Fix any test failures in the suite (run targeted, not full suite)
- Address TODO/FIXME/HACK comments in actively maintained files

### Priority 5: NovaTrade Research & Advancement
If the system is healthy and no maintenance is needed, invest in NovaTrade:
- Review recent trade journal entries for patterns
- Research strategy improvements based on rejection reasons
- Investigate execution pipeline gaps
- Advance any open NovaTrade implementation phases

### Priority 6: Infrastructure Improvement
If everything above is clean:
- Improve monitoring coverage
- Add missing test cases for edge cases discovered in prior sessions
- Enhance error messages or logging in problem areas
- Document architectural decisions that lack ADRs

## Step 3: Execute the Pivot Work

Once a target is identified:

1. **State your pivot reason** — one sentence explaining why you chose this work over alternatives.
2. **Execute with full rigor** — treat the pivot work with the same discipline as an assigned task. Use implementation-team patterns for non-trivial changes.
3. **Write tests** if modifying production code.
4. **Write output** to `OUTPUT/<original_stem>__<timestamp>.md` preserving the shift block lineage.

## Step 4: Output Contract

Every pivot execution must end with:

```
## CONTRACT
pivot_reason: <why this work was chosen over alternatives>
discovery_source: <which cascade priority yielded the work — tasks/health/maintenance/quality/novatrade/infra>
work_performed: <one-line description>
summary: <2-3 sentences of what was done and why it matters>
files_changed: <comma-separated paths, or "none">
verification: <how correctness was confirmed>
confidence: <low | medium | high>
```

## Anti-Patterns to Avoid

| Anti-Pattern | Why It's Wrong | Do This Instead |
|---|---|---|
| Re-run full test suite and exit | Wastes the entire exploration block | Spot-check 1-2 things, then pivot |
| Repeat work from a prior session | Duplicates effort with zero new value | Read OUTPUT/ first, resume only if incomplete |
| Pick trivially easy work | Low-value use of exploration time | Follow the priority cascade honestly |
| Skip verification on pivot work | Pivot work is still production code | Apply same test/verify standards |
| Touch > 3 unrelated areas | Unfocused scatter leads to half-done changes | Pick one thing, finish it completely |

## Examples of Good Pivots

From `shift_20260331_15_free_will_pm` sessions:
- **Trade journal rotation**: Discovered 54K-line unbounded file growth, implemented rotation with atomic writes and auto-trigger — systemic fix, not a band-aid.
- **Metrics pollution cleanup**: Found 93% of "contract failures" were test artifacts, fixed 5 tests and cleaned 1,312 phantom entries — restored metric trustworthiness.
- **Progress history cap**: STATE file growing 4MB/day, added MAX_HISTORY_ENTRIES enforcement — prevented future performance degradation.
- **Risk engine data source fix**: Collector reading stale mock data instead of real drawdown state — wired to actual `novatrade_risk_state.json`.

Each pivot was **discovered autonomously**, **completed fully**, and **produced measurable improvement**.
