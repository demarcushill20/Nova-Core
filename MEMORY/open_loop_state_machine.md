# Open-Loop State Machine (Phase 7, Step 7.2)

## States

| Status | Terminal | Description |
|--------|----------|-------------|
| `proposed` | No | Detected but not yet confirmed as actionable |
| `open` | No | Confirmed unresolved work, actively tracked |
| `blocked` | No | Cannot progress due to an identified blocker |
| `deferred` | No | Intentionally postponed to a later time |
| `resolved` | **Yes** | Completed with explicit evidence |
| `closed_rejected` | **Yes** | Determined to be invalid or no longer needed |
| `stale` | No | No activity for extended period, needs triage |

## Transition Map

```
proposed ──→ open
proposed ──→ closed_rejected

open ──→ blocked
open ──→ deferred
open ──→ resolved
open ──→ closed_rejected
open ──→ stale

blocked ──→ open          (blocker cleared)
blocked ──→ deferred
blocked ──→ resolved
blocked ──→ closed_rejected
blocked ──→ stale

deferred ──→ open         (ready to resume)
deferred ──→ blocked
deferred ──→ resolved
deferred ──→ closed_rejected
deferred ──→ stale

stale ──→ open            (re-activated)
stale ──→ resolved
stale ──→ closed_rejected
```

## State Diagram

```
                    ┌─────────────┐
                    │  proposed   │
                    └──────┬──────┘
                      ┌────┴────┐
                      ▼         ▼
               ┌──────────┐  ┌───────────────┐
               │   open   │  │closed_rejected│ (TERMINAL)
               └────┬─────┘  └───────────────┘
            ┌───────┼────────┐        ▲
            ▼       ▼        ▼        │
      ┌─────────┐ ┌────────┐ ┌───────┴───┐
      │ blocked │ │deferred│ │ resolved  │ (TERMINAL)
      └────┬────┘ └───┬────┘ └───────────┘
           │          │            ▲
           └──────┬───┘            │
                  ▼                │
             ┌─────────┐          │
             │  stale   │─────────┘
             └─────────┘
```

## Transition Rules

1. **Terminal states are permanent**: `resolved` and `closed_rejected` cannot transition to any other state.
2. **Resolution requires evidence**: `closure_reason` must be ≥5 characters when resolving.
3. **History is append-only**: Every transition appends `{from, to, timestamp, reason}` to the loop's history.
4. **Invalid transitions raise ValueError**: Attempting an undefined transition is rejected with a clear error message.

## Automatic Detection → State

| Event Pattern | Initial Status | Confidence |
|---------------|---------------|------------|
| `task_failed` | `open` | `medium` |
| `plan_created` with open steps | `proposed` | `low` |
| `session_end` with `open_threads` | `proposed` | `low` |

## Staleness Policy

Loops can be marked stale via `mark_stale()`. Staleness is a non-terminal state —
stale loops can be reactivated (`open`), resolved, or rejected. Automatic staleness
detection (e.g., no update in N days) is deferred to a future phase.
