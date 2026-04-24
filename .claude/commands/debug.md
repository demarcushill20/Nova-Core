# Debug

Invoke the `systematic-debugging` skill to diagnose a bug, test failure, or unexpected behavior through a 4-phase root-cause process.

## Usage

```
/debug <error message, failing test, or short symptom description>
```

## What it does

- **Phase 1 — Root Cause Investigation:** reads errors carefully, reproduces consistently, checks recent changes, gathers boundary evidence in multi-component systems
- **Phase 2 — Pattern Analysis:** finds working examples, compares against references, identifies differences
- **Phase 3 — Hypothesis & Testing:** forms one hypothesis, tests minimally, verifies before continuing
- **Phase 4 — Implementation:** writes a failing test case, implements a single fix, verifies

## The Iron Law

```
NO FIXES WITHOUT ROOT CAUSE INVESTIGATION FIRST
```

## 3-attempt escalation

If 3 fixes have failed, the skill dispatches the **Critic agent** (`AGENTS/critic/AGENT.md`) for an architectural review and logs the root cause via `memory-store` (`memory_type: research`, tag `debug`). Do not attempt fix #4 without operator approval.

## See also

- `.claude/skills/systematic-debugging/SKILL.md` — full skill contract
- `sequential-thinking` — for general structured reasoning (not bug-specific)
- `pinescript-debug` — specialized Pine Script debugger
