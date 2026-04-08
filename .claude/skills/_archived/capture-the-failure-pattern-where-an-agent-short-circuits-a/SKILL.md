---
name: block-skip-validation
description: "Pre-flight check that validates whether an agent's claim that a block is 'already done' is backed by evidence or is a short-circuit escape."
activation:
  keywords:
    - skip validation
    - already done
    - prior work sufficient
    - block skip
    - short circuit
    - preflight
    - skip claim
    - work already completed
    - duplicate block
  when:
    - An agent declares a shift block or task unnecessary because prior work covers it
    - A block produces no output and claims prior completion
    - Multiple blocks in a shift are skipped with similar justifications
    - A free-will or discretionary block is dismissed without concrete evidence
tool_doctrine:
  primary:
    - Bash (git log --since, git diff --stat for recent commit evidence)
    - Glob (OUTPUT/ files with recent timestamps)
    - Read (OUTPUT/ artifacts, MEMORY/ checkpoints, task files)
    - Grep (search for block-specific deliverables in recent outputs)
  secondary:
    - memory-recall (check if prior session actually completed the claimed work)
    - memory-store (record validated skip or flagged false skip for pattern tracking)
output_contract:
  required:
    - skip_verdict (VALID_SKIP | FALSE_SKIP | INCONCLUSIVE)
    - claimed_reason (what the agent said to justify skipping)
    - evidence_summary (concrete evidence found or not found)
    - evidence_sources (list of files, commits, or checkpoints examined)
    - recommended_action (accept skip | execute block | escalate)
---

# Block Skip Validation

Validates whether an agent's claim that a block is "already done by prior work" is genuine or a short-circuit escape pattern. Distinguishes legitimate deduplication from lazy skipping.

## Problem This Solves

Agents executing multi-block shift schedules sometimes short-circuit blocks by declaring that prior work already satisfies the block's intent. This is valid when a previous block genuinely covered the work (e.g., a monitoring block ran diagnostics that a later health-check block would repeat). But it is invalid when the agent lacks a clear action plan and uses "already done" as an escape hatch to avoid producing output. This pattern was observed in `shift_20260331_15_free_will_pm` where discretionary blocks were dismissed without evidence.

## When To Invoke

- Before accepting any block skip where the agent claims prior completion
- When a block produces zero output files and zero commits
- When the skip justification is vague ("this was already covered", "nothing new to add")
- When multiple consecutive blocks are skipped in the same shift
- Especially for free-will / discretionary blocks where the scope is agent-defined

## Step-by-Step Procedure

### Step 1: Extract the skip claim

From the agent's output or block log, capture:
- **Block ID**: which block is being skipped (e.g., `shift_20260331_15_free_will_pm`)
- **Claimed reason**: the agent's stated justification for skipping
- **Block intent**: what the block was supposed to accomplish (from task spec or shift schedule)
- **Timestamp**: when the skip was declared

### Step 2: Check git log for recent relevant commits

```bash
# Look for commits in the last 6 hours related to the block's intent
git log --since="6 hours ago" --oneline --all
# Check if any commits match the claimed prior work
git log --since="6 hours ago" --grep="<keyword from block intent>"
```

**Evidence threshold**: At least one commit within the block's timeframe whose message or diff relates to the block's stated purpose.

### Step 3: Check OUTPUT/ for recent artifacts

Use Glob to find recent OUTPUT/ files and Read to inspect their contents for relevance to the skipped block's intent.

**Evidence threshold**: At least one output file whose content demonstrably covers the block's deliverables.

### Step 4: Check memory checkpoints

Query Fusion Memory for:
- The most recent checkpoint (`get_last_checkpoint`)
- Whether the checkpoint's `completed_items` or notes reference the block's work
- Whether `open_threads` lists the block's topic as unfinished (contradicting the skip claim)

**Evidence threshold**: Checkpoint explicitly mentions the work as completed, OR memory contains a decision/research entry covering it.

### Step 5: Check for the "vague skip" anti-pattern

Flag as suspicious if ANY of these are true:
- The skip justification contains no specific file paths, commit hashes, or timestamps
- The justification is fewer than 20 words
- The justification references "prior blocks" without naming which ones
- The block was a free-will/discretionary block (these have no prior block that could cover them)
- Multiple blocks in the same shift were skipped with near-identical justifications

### Step 6: Render verdict

| Condition | Verdict |
|---|---|
| Recent commit + output file + checkpoint all confirm prior completion | **VALID_SKIP** |
| At least 2 of 3 evidence sources confirm, justification is specific | **VALID_SKIP** |
| Only 1 evidence source confirms, justification is vague | **INCONCLUSIVE** |
| No evidence found in git, OUTPUT/, or memory | **FALSE_SKIP** |
| Free-will block with no concrete output anywhere | **FALSE_SKIP** |
| Justification matches the vague-skip anti-pattern | **FALSE_SKIP** |

### Step 7: Act on verdict

- **VALID_SKIP**: Accept the skip. Log that it was validated. No further action.
- **FALSE_SKIP**: Reject the skip. Re-queue the block for execution with a note that the prior skip was invalidated. Record in memory for pattern tracking.
- **INCONCLUSIVE**: Execute the block anyway (err on the side of doing work). Flag for operator review if the pattern recurs.

## Expected Inputs

- Block specification (block ID, intent, expected deliverables)
- Agent's skip justification (text from block output or log)
- Access to git log, OUTPUT/ directory, and Fusion Memory

## Expected Outputs

```
## CONTRACT
skip_verdict: FALSE_SKIP
claimed_reason: "Prior blocks already covered system monitoring and research tasks"
evidence_summary: "No commits in last 6h. No OUTPUT/ files since 14:00 UTC. Checkpoint lists 'free_will_pm' in open_threads as unstarted."
evidence_sources:
  - git log --since="6 hours ago" (0 relevant commits)
  - OUTPUT/ glob (no files newer than block start time)
  - Fusion Memory checkpoint (block listed as pending, not complete)
recommended_action: "Reject skip. Re-execute block with explicit deliverable requirements."
```

## Error Handling

- **Git log unreachable**: Rely on OUTPUT/ and memory evidence; lower confidence but still render verdict
- **No memory checkpoint exists**: Weight git and OUTPUT/ evidence more heavily; note missing checkpoint
- **Block intent is ambiguous**: If the block spec doesn't clearly define deliverables, verdict = INCONCLUSIVE and recommend the block spec be tightened
- **Multiple blocks skipped**: Validate each independently — a valid skip for one block does not validate skips for others
- **Agent pushback on re-execution**: The validation is authoritative. A FALSE_SKIP verdict means the block runs. No appeals without new evidence.

## Pattern Tracking

When a FALSE_SKIP is detected, store in memory:
- Block ID and shift ID
- The vague justification that was used
- Which evidence checks failed

This builds a corpus for detecting agents that habitually short-circuit discretionary blocks, enabling tighter guardrails on free-will scheduling.

---

Save this to `.claude/skills/block-skip-validation/SKILL.md`. The key design choices:

1. **Three evidence pillars** (git log, OUTPUT/ timestamps, memory checkpoints) — a valid skip must satisfy at least 2 of 3
2. **Vague-skip anti-pattern detector** — catches the specific pattern of short, non-specific justifications with no concrete references
3. **Free-will blocks get stricter scrutiny** — since no prior block could have covered agent-defined discretionary work, skipping these requires even stronger evidence
4. **Err toward execution** — INCONCLUSIVE means "run the block anyway" rather than accepting the skip
