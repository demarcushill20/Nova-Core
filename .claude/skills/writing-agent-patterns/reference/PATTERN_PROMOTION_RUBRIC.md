# Pattern Promotion Rubric

## Purpose

Agent patterns represent stable, reusable methods. This rubric determines whether a candidate method is mature enough for promotion.

## Promotion Criteria

All must be true:

| Criterion | Threshold | How to check |
|-----------|-----------|--------------|
| **Repeated success** | 2+ independent tasks or sessions | Review workflow learnings, session diary, execution logs |
| **Stable behavior** | Method is consistent, not rapidly evolving | Compare how the method was applied across uses |
| **Agent-role clarity** | Clear single owner role | Can you name exactly one role (research, coder, critic, verifier, planner, memory)? |
| **Non-obvious value** | Encodes knowledge not obvious from task description | Would a new agent without this pattern make avoidable mistakes? |
| **Actionable steps** | Method can be described as a procedure | Can you write numbered steps that another agent could follow? |

## Confidence Levels

| Level | Meaning | Evidence required |
|-------|---------|-------------------|
| **high** | Method is well-established | 3+ confirming tasks, zero contradicting outcomes |
| **medium** | Method is promising but not fully proven | 2 confirming tasks, or 3+ with some caveats |
| **low** | Method is tentative | Should not normally be promoted — use workflow-learning instead |

## Deferral Reasons

Defer (do not promote) when:

- Only one task confirms the method — one datapoint is a learning, not a pattern
- The method is still evolving between uses — let it stabilize first
- Evidence is indirect or inferred rather than observed — wait for direct confirmation
- The pattern would duplicate an existing one — search first
- Confidence is low — prefer no write over a weak pattern

## Anti-patterns in Pattern Writing

| Anti-pattern | Why it's bad | Fix |
|-------------|-------------|-----|
| Promoting after first use | One success isn't a pattern | Wait for 2+ confirmations |
| Vague guidance ("be careful") | Not actionable | Specific steps and conditions |
| Missing failure modes | Incomplete — future agents won't know the boundaries | Add at least 2 known failure modes |
| Duplicating an existing pattern | Clutters the vault, causes confusion | Search before writing |
| Including session-specific details | Not reusable | Write for a future agent that has never seen the original task |
| Promoting speculative methods | May be wrong | Only promote verified outcomes |
