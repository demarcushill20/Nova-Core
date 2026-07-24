---
name: systematic-debugging
description: "Use when encountering any bug, test failure, or unexpected behavior, before proposing fixes. Enforces a 4-phase root-cause process with a 3-attempt escalation to architectural review. Invoke when the operator says 'debug', 'investigate', 'figure out why', or pastes an error message / failing test."
source:
  upstream: obra/superpowers
  tag: v6.2.0
  commit: 3dcbd5c4b48e02263fbf4a3c01e3fe4f81d584d9
  path: skills/systematic-debugging/SKILL.md
  license: MIT
---

# Systematic Debugging

## Overview

Random fixes waste time and create new bugs. Quick patches mask underlying issues.

**Core principle:** find the root cause before attempting any fix. Symptom fixes are failure.

**Violating the letter of this process is violating the spirit of debugging.**

> **NovaCore adaptations (vendored from Superpowers v6.2.0):**
> - For *general* structured reasoning (architecture trade-offs, multi-phase planning), prefer `sequential-thinking`. This skill is specifically for bug/failure/incident investigation.
> - The 3-attempt escalation routes to the **Critic agent** (`AGENTS/critic/AGENT.md`) via Task dispatch, and the root-cause summary is logged via **`memory-store`** with `memory_type: research` and tag `debug`. See the "3+ Failed Fixes — Escalation" section below.
> - Upstream phrasing "your human partner" is rendered here as "operator".

## The Iron Law

```
NO FIXES WITHOUT ROOT CAUSE INVESTIGATION FIRST
```

If you haven't completed Phase 1, you cannot propose fixes.

## When to Use

Use for any technical issue:
- Test failures
- Bugs in production
- Unexpected behavior
- Performance problems
- Build failures
- Integration issues

**Use this especially when:**
- Under time pressure (emergencies make guessing tempting)
- "Just one quick fix" seems obvious
- You've already tried multiple fixes
- The previous fix didn't work
- You don't fully understand the issue

**Don't skip when:**
- Issue seems simple (simple bugs have root causes too)
- You're in a hurry (rushing guarantees rework)
- The operator wants it fixed *now* (systematic is faster than thrashing)

## The Four Phases

Complete each phase before proceeding to the next.

### Phase 1: Root Cause Investigation

**Before attempting any fix:**

1. **Read error messages carefully.** Don't skip past errors or warnings. They often contain the exact solution. Read stack traces completely. Note line numbers, file paths, error codes.

2. **Reproduce consistently.** Can you trigger it reliably? What are the exact steps? Does it happen every time? If not reproducible → gather more data, don't guess.

3. **Check recent changes.** What changed that could cause this? `git diff`, recent commits, new dependencies, config changes, environmental differences.

4. **Gather evidence in multi-component systems.** When the system has multiple layers (CI → build → signing, API → service → database), add diagnostic instrumentation at each component boundary *before* proposing fixes:

    ```
    For each component boundary:
      - Log what data enters the component
      - Log what data exits the component
      - Verify environment/config propagation
      - Check state at each layer

    Run once to gather evidence showing WHERE it breaks.
    Then analyze evidence to identify the failing component.
    Then investigate that specific component.
    ```

5. **Trace data flow.** When the error is deep in the call stack, trace backward: where does the bad value originate? What called this with a bad value? Keep tracing up until you find the source. Fix at the source, not at the symptom.

### Phase 2: Pattern Analysis

**Find the pattern before fixing:**

1. **Find working examples.** Locate similar working code in the same codebase.
2. **Compare against references.** Read reference implementations *completely* — don't skim.
3. **Identify differences.** List every difference, however small. Don't assume "that can't matter".
4. **Understand dependencies.** What other components does this need? What settings, config, environment? What assumptions does it make?

### Phase 3: Hypothesis and Testing

**Scientific method:**

1. **Form a single hypothesis.** State it clearly: "I think X is the root cause because Y." Write it down. Be specific.
2. **Test minimally.** Make the smallest possible change to test the hypothesis. One variable at a time.
3. **Verify before continuing.** Did it work? Yes → Phase 4. Didn't work? Form a *new* hypothesis. Don't pile fixes on top.
4. **When you don't know.** Say "I don't understand X". Don't pretend. Ask for help. Research more.

### Phase 4: Implementation

**Fix the root cause, not the symptom:**

1. **Create a failing test case.** Simplest possible reproduction. Use `test-driven-development` for proper failing tests. This is required before fixing.

2. **Implement a single fix.** Address the root cause identified. One change at a time. No "while I'm here" improvements.

3. **Verify the fix.** Test passes? No other tests broken? Issue actually resolved?

4. **If the fix doesn't work:**
    - Stop.
    - Count: how many fixes have you tried?
    - If **< 3**: return to Phase 1, re-analyze with new information.
    - If **≥ 3**: stop and question the architecture (step 5 below).
    - Do not attempt fix #4 without architectural discussion.

5. **If 3+ fixes failed — escalate (see "3+ Failed Fixes — Escalation" below).**

    Pattern indicating architectural problem:
    - Each fix reveals new shared state / coupling / problem in a different place
    - Fixes require "massive refactoring" to implement
    - Each fix creates new symptoms elsewhere

    This is not a failed hypothesis — this is a wrong architecture. Do not attempt fix #4 without escalating.

## 3+ Failed Fixes — Escalation

When you cross the 3-attempt threshold, do **both** of the following before proposing any further fix:

### Step A — Dispatch the Critic agent

Use `Task` (subagent dispatch) with `subagent_type` matching NovaCore's Critic agent (see `AGENTS/critic/AGENT.md` for the contract). The prompt is self-contained and includes:

- The original symptom and every reproduction step
- All three failed hypotheses with evidence showing why each failed
- Code diffs for each attempted fix
- The specific question: **is this pattern architecturally sound, or are we fighting the design?**

```
Task(
  subagent_type="critic",
  description="3-fix architectural escalation: <short symptom>",
  prompt="""
  Bug: <symptom + repro>
  Attempt 1 hypothesis: <X because Y>. Result: <what happened>. Diff: <paths>.
  Attempt 2 hypothesis: <...>. Result: <...>. Diff: <...>.
  Attempt 3 hypothesis: <...>. Result: <...>. Diff: <...>.

  Verdict requested: is this pattern fundamentally sound? Should we continue
  fixing symptoms or refactor the architecture? What is the minimum refactor
  that would make this class of bug impossible?
  """
)
```

The Critic is independent of this session's context — it sees only what you pass in. Include the raw evidence, not your interpretation.

### Step B — Log the root-cause finding via `memory-store`

Whether the Critic says "refactor" or "continue fixing", persist the finding. Use `mcp__nova-memory__upsert_memory` (or invoke the `memory-store` skill, which wraps it):

```
mcp__nova-memory__upsert_memory(
  content="<concrete root-cause statement — one concept, self-contained. "
          "Example: 'retry_operation() in src/worker.py shares mutable state "
          "between concurrent invocations via module-level cache; 3 fix "
          "attempts confirmed the coupling is architectural, not a lock bug.'>",
  metadata={
    "memory_type": "research",
    "project": "<project slug>",
    "tags": ["debug", "root-cause", "<component>"],
    "session_id": "<current session id>"
  }
)
```

One atomic fact per `upsert_memory` call. If the Critic returns multiple independent findings, use `bulk_upsert_memory`. Never store secrets or raw stack traces — distill to the insight.

### Step C — Present to the operator

After dispatching the Critic and logging the finding, summarise for the operator:

> "Hit the 3-fix threshold on `<symptom>`. Critic verdict: `<summary>`. Root cause logged to Fusion Memory as `<memory_id>`. Recommended next step: `<refactor X>` / `<one more targeted fix>`. Proceed?"

Wait for a decision. Do not attempt fix #4 without operator approval.

## Red Flags — STOP and Follow Process

Catching yourself thinking:
- "Quick fix for now, investigate later"
- "Just try changing X and see if it works"
- "Add multiple changes, run tests"
- "Skip the test, I'll manually verify"
- "It's probably X, let me fix that"
- "I don't fully understand but this might work"
- "Pattern says X but I'll adapt it differently"
- "Here are the main problems: [lists fixes without investigation]"
- Proposing solutions before tracing data flow
- "One more fix attempt" (after 2+ already)
- Each fix reveals a new problem in a different place

**All of these mean: stop. Return to Phase 1.**

If 3+ fixes failed: question the architecture (see Phase 4, step 5).

## Operator Signals You're Doing It Wrong

Watch for these redirections:
- "Is that not happening?" — you assumed without verifying
- "Will it show us…?" — you should have added evidence gathering
- "Stop guessing" — you're proposing fixes without understanding
- "Ultrathink this" — question fundamentals, not just symptoms
- "We're stuck?" (frustrated) — your approach isn't working

**When you see these:** stop. Return to Phase 1.

## Common Rationalizations

| Excuse | Reality |
|--------|---------|
| "Issue is simple, don't need process" | Simple issues have root causes too. Process is fast for simple bugs. |
| "Emergency, no time for process" | Systematic debugging is *faster* than guess-and-check thrashing. |
| "Just try this first, then investigate" | First fix sets the pattern. Do it right from the start. |
| "I'll write a test after confirming the fix works" | Untested fixes don't stick. Test first proves it. |
| "Multiple fixes at once saves time" | Can't isolate what worked. Causes new bugs. |
| "Reference too long, I'll adapt the pattern" | Partial understanding guarantees bugs. Read it completely. |
| "I see the problem, let me fix it" | Seeing symptoms ≠ understanding root cause. |
| "One more fix attempt" (after 2+ failures) | 3+ failures = architectural problem. Question the pattern. |

## Quick Reference

| Phase | Key Activities | Success Criteria |
|-------|---------------|------------------|
| **1. Root Cause** | Read errors, reproduce, check changes, gather evidence | Understand WHAT and WHY |
| **2. Pattern** | Find working examples, compare | Identify differences |
| **3. Hypothesis** | Form theory, test minimally | Confirmed or new hypothesis |
| **4. Implementation** | Create test, fix, verify | Bug resolved, tests pass |

## When Process Reveals "No Root Cause"

If investigation reveals a truly environmental, timing-dependent, or external issue:

1. You've completed the process.
2. Document what you investigated.
3. Implement appropriate handling (retry, timeout, error message).
4. Add monitoring/logging for future investigation.

**But:** 95% of "no root cause" cases are incomplete investigation.

## Related NovaCore Skills

- `test-driven-development` — for creating the failing test (Phase 4, Step 1)
- `self-verification` — verify the fix worked before claiming success
- `sequential-thinking` — general structured reasoning (not bug-specific)
- `memory-store` — log root-cause findings (see "3+ Failed Fixes — Escalation", Step B)
- Critic agent (`AGENTS/critic/AGENT.md`) — escalation target after 3+ failed fixes (see "3+ Failed Fixes — Escalation", Step A)
