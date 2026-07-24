---
name: writing-plans
description: "Use when you have a spec or requirements for a multi-step task, before touching code. Produces bite-sized, paste-ready tasks with exact file paths, code, and verification. Invoke when the operator says 'write the plan', 'plan this out', 'break this into tasks', or hands over an approved design doc."
source:
  upstream: obra/superpowers
  tag: v6.2.0
  commit: 3dcbd5c4b48e02263fbf4a3c01e3fe4f81d584d9
  path: skills/writing-plans/SKILL.md
  license: MIT
---

# Writing Plans

## Overview

Write comprehensive implementation plans assuming the engineer has zero context for our codebase and questionable taste. Document everything they need: which files to touch for each task, the code, testing, docs to check, how to verify. Give them the whole plan as bite-sized tasks. DRY. YAGNI. TDD. Frequent commits.

Assume they are a skilled developer but know almost nothing about the toolset or problem domain. Assume they don't know good test design very well.

**Announce at start:** "I'm using the writing-plans skill to create the implementation plan."

**Context:** should run in a dedicated worktree (created by the `using-git-worktrees` skill).

**Save plans to the Obsidian vault via `plan-tracker`** — the canonical store for NovaCore implementation plans. File path: `10-plans/plan-<plan-id>.md`. Frontmatter must match the plan-tracker schema (see "Plan Tracker Handoff" below). Optionally queue a `TASKS/<plan-id>.md` entry so the task pipeline picks it up.

> **NovaCore adaptations (vendored from Superpowers v6.2.0):**
> - Upstream references to `subagent-driven-development` and `executing-plans` are replaced with NovaCore's **`implementation-team`** orchestration skill in the execution handoff.
> - Plan output is written to the Obsidian vault via `plan-tracker` (replaces upstream's `docs/superpowers/plans/` path).
> - Scope-decomposition rules are unchanged from upstream.

## Scope Check

If the spec covers multiple independent subsystems, it should have been broken into sub-project specs during brainstorming. If it wasn't, suggest breaking this into separate plans — one per subsystem. Each plan should produce working, testable software on its own.

## File Structure

Before defining tasks, map out which files will be created or modified and what each is responsible for. This is where decomposition decisions get locked in.

- Design units with clear boundaries and well-defined interfaces. One responsibility per file.
- Prefer smaller, focused files over large ones that do too much.
- Files that change together should live together. Split by responsibility, not by technical layer.
- In existing codebases, follow established patterns. Don't unilaterally restructure — but if a file you're modifying has grown unwieldy, including a split in the plan is reasonable.

This structure informs the task decomposition. Each task should produce self-contained changes that make sense independently.

## Bite-Sized Task Granularity

**Each step is one action (2–5 minutes):**
- "Write the failing test" — step
- "Run it to make sure it fails" — step
- "Implement the minimal code to make the test pass" — step
- "Run the tests and make sure they pass" — step
- "Commit" — step

## Plan Document Header

Every plan starts with this header:

```markdown
# [Feature Name] Implementation Plan

> **For agentic workers:** use the `implementation-team` skill to execute this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** [One sentence describing what this builds]

**Architecture:** [2–3 sentences about approach]

**Tech Stack:** [Key technologies/libraries]

---
```

## Task Structure

````markdown
### Task N: [Component Name]

**Files:**
- Create: `exact/path/to/file.py`
- Modify: `exact/path/to/existing.py:123-145`
- Test: `tests/exact/path/to/test.py`

- [ ] **Step 1: Write the failing test**

```python
def test_specific_behavior():
    result = function(input)
    assert result == expected
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/path/test.py::test_name -v`
Expected: FAIL with "function not defined"

- [ ] **Step 3: Write minimal implementation**

```python
def function(input):
    return expected
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/path/test.py::test_name -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/path/test.py src/path/file.py
git commit -m "feat: add specific feature"
```
````

## No Placeholders

Every step must contain the actual content an engineer needs. These are **plan failures** — never write them:
- "TBD", "TODO", "implement later", "fill in details"
- "Add appropriate error handling" / "add validation" / "handle edge cases"
- "Write tests for the above" (without actual test code)
- "Similar to Task N" (repeat the code — tasks may be read out of order)
- Steps that describe what to do without showing how (code blocks required for code steps)
- References to types, functions, or methods not defined in any task

## Remember
- Exact file paths always
- Complete code in every step — if a step changes code, show the code
- Exact commands with expected output
- DRY, YAGNI, TDD, frequent commits

## Self-Review

After writing the plan, look at the spec with fresh eyes and check the plan against it. This is a checklist you run yourself — not a subagent dispatch.

1. **Spec coverage:** skim each section/requirement in the spec. Can you point to a task that implements it? List any gaps.
2. **Placeholder scan:** search for red flags from "No Placeholders". Fix them.
3. **Type consistency:** do types, method signatures, and property names in later tasks match what you defined earlier? `clearLayers()` in Task 3 but `clearFullLayers()` in Task 7 is a bug.

Fix issues inline. No need to re-review — just fix. If a spec requirement has no task, add the task.

## Plan Tracker Handoff

Plans are stored in the Obsidian vault via the `plan-tracker` skill. Every plan note uses this schema:

**Frontmatter:**

```yaml
---
title: "<Feature Name> Implementation Plan"
type: implementation-plan
plan_id: <unique-slug>                # e.g., "superpowers-plugin-integration"
status: backlog                       # backlog | active | completed | paused
priority: high | medium | low
progress: "0/<total-phases>"
confidence: high | medium | low
updated: <YYYY-MM-DD>
date: <YYYY-MM-DD>
source: nova-core-memory
tags:
  - "#type/plan"
  - "#status/backlog"
  - "#project/nova-core"
---
```

**Body requirements:**

- Preserve every task block from the plan structure above (exact file paths, full code, verification steps).
- Add a `## Phases` section using markdown checkboxes that map 1:1 with the tasks. Phase entries are rolled up at plan-tracker resolution; don't write individual task-level checkboxes here.

```markdown
## Phases

- [ ] **Phase 1: <name>** — <one-line description>
- [ ] **Phase 2: <name>** — <one-line description>
```

**Write the note** via `plan-tracker` (which uses `mcp__nova-vault__vault_write` under the hood). On status transitions (e.g., when `implementation-team` completes Phase 1), `plan-tracker` updates the checkbox + `progress` + `updated` fields.

**Optionally queue a task** — if this plan needs to show up in the TASKS/ pipeline for the orchestrator or scheduled agents, also write `TASKS/<plan_id>.md` pointing at the vault plan.

## Execution Handoff

After the plan is saved in the vault, hand off to NovaCore's orchestration layer:

> "Plan complete and saved to `10-plans/plan-<plan_id>.md` (status: backlog, progress: 0/<N>). Ready to execute via `implementation-team` — it will validate the plan against the current codebase, spawn an implementer, run an independent critical review, verify, and escalate on failure. On Phase 1 kickoff, `plan-tracker` will flip status to `active`. Say the word and I'll start."

Wait for operator confirmation. On approval, invoke `implementation-team` with the vault plan path.
