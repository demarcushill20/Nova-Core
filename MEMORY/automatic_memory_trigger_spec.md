# Automatic Memory Trigger Specification

Phase 3 deliverable — defines when, what, and how memory capture should happen
automatically in response to runtime events.

Generated: 2026-03-13

---

## What Is a Trigger

A **trigger** is a deterministic hook that observes a runtime event and,
if eligibility criteria are met, creates a memory candidate that flows
through the Unified Memory Router.

Triggers are:
- **Event-driven**: they fire in response to something that happened
- **Deterministic**: same event + same state = same trigger decision
- **Router-bound**: all triggered memory goes through `router.ingest_event()` → `router.store()`
- **Non-blocking**: trigger failures are non-fatal to the calling code path
- **Observable**: every trigger decision is traced via structured JSONL logs

---

## Trigger Classes

| Class | Description | Event Types | Initial Layer |
|-------|-------------|-------------|---------------|
| `task_lifecycle` | Task completion or failure | `task_completed`, `task_failed` | episodic |
| `plan_lifecycle` | Plan execution outcomes | `plan_created`, `plan_revised` | episodic |
| `error_failure` | Runtime errors, crash reports | `task_failed`, `bug_fixed` | episodic |
| `session_boundary` | Session/heartbeat boundaries | `session_end`, `heartbeat_cycle` | working |
| `operator_decision` | Explicit operator instructions | `decision_made`, `user_preference` | semantic |

---

## Trigger Pipeline

```
Event observed
    │
    ▼
┌─────────────────────┐
│ 1. Class validation  │  Is trigger_class valid? Is event_type allowed for it?
└──────────┬──────────┘
           │ pass
           ▼
┌─────────────────────┐
│ 2. Content quality   │  Title ≥ 5 chars? Summary ≥ 10 chars?
└──────────┬──────────┘
           │ pass
           ▼
┌─────────────────────┐
│ 3. Dedupe / cooldown │  Content hash unique? Class+source cooldown clear? Rate limit OK?
└──────────┬──────────┘
           │ pass
           ▼
┌─────────────────────┐
│ 4. Build event dict  │  Construct event with title, summary, source, tags, etc.
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│ 5. Router ingest     │  router.ingest_event() → CanonicalMemoryObject with layer metadata
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│ 6. Router store      │  router.store() → adapter dispatch with layer/store validation
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│ 7. Trace outcome     │  Structured JSONL: memory.trigger.{fired|rejected|suppressed}
└─────────────────────┘
```

---

## Eligibility Criteria

A trigger fires only when ALL conditions are met:

1. `trigger_class` is in `VALID_TRIGGER_CLASSES`
2. `event_type` is in `TRIGGER_CLASS_EVENT_TYPES[trigger_class]`
3. `title` length ≥ `min_title_length` (default: 5)
4. `summary` length ≥ `min_summary_length` (default: 10)
5. Content hash not seen within `dedupe_window_s` (default: 300s)
6. Same `trigger_class + source` not fired within `cooldown_window_s` (default: 60s)
7. Trigger class not over `rate_limit_max` within `rate_limit_window_s` (default: 20/hour)

---

## Suppression / Dedupe Rules

### Content Hash Dedupe
- SHA-256 of `event_type|title_lower|summary_lower[:200]`
- If hash seen within `dedupe_window_s`, trigger is suppressed
- Prevents: identical events stored repeatedly

### Class + Source Cooldown
- Key: `(trigger_class, source)`
- If same key fired within `cooldown_window_s`, trigger is suppressed
- Prevents: rapid-fire from the same source overwhelming storage

### Rate Limit
- Per trigger_class, rolling window of `rate_limit_window_s`
- If count ≥ `rate_limit_max`, trigger is suppressed
- Prevents: pathological event storms

### What Is NOT Suppressed
- Different tasks completing close together (different content hashes)
- Same trigger class from different sources (different cooldown keys)
- Events after the dedupe window expires

---

## Layer Assignment

Triggers do NOT assign layers directly. They pass `event_type` to the router,
which uses `infer_layer_from_event_type()` from Phase 2. The trigger engine
does not override layer assignment.

Expected mapping (from Phase 2):

| Event Type | Assigned Layer | Rationale |
|------------|---------------|-----------|
| task_completed | episodic | Record of a specific completed event |
| task_failed | episodic | Record of a specific failure |
| plan_created | episodic | Record of a plan execution |
| plan_revised | episodic | Record of a plan re-execution |
| bug_fixed | episodic | Record of a fix applied |
| heartbeat_cycle | working | Transient health snapshot |
| session_end | working | Transient session boundary |
| decision_made | episodic | Record of an operator decision |
| user_preference | semantic | Stable preference fact |

---

## What Should NOT Trigger Memory Creation

- **Every slog.event call**: structured logs are for observability, not memory
- **Every file write**: file operations are not meaningful events
- **Intermediate retries**: only final outcomes should trigger
- **Raw subprocess output**: too noisy, not meaningful
- **Health check details**: only the aggregate outcome matters (one trigger per heartbeat cycle)
- **Skill selection**: internal routing, not durable knowledge
- **Memory retrieval**: reads are not events
- **DLP gate decisions**: security internal, not memory

---

## Rejection Conditions

A trigger is rejected (not fired) when:
- Invalid trigger_class
- Event_type not in allowed set for the class
- Title or summary too short
- Router `ingest_event()` raises `ValueError`
- Router `store()` throws an unexpected exception

A trigger is suppressed (fire attempted but blocked) when:
- Content hash dedupe match
- Class+source cooldown active
- Rate limit exceeded

Rejections and suppressions are both traced but produce different log events
(`memory.trigger.rejected` vs `memory.trigger.suppressed`).

---

## Observability

Every trigger decision emits a structured log event:

| Event | When | Key Fields |
|-------|------|-----------|
| `memory.trigger.fired` | Trigger passed all checks and memory was routed | trigger_class, event_type, assigned_layer, stored, memory_id |
| `memory.trigger.rejected` | Trigger failed eligibility checks | trigger_class, event_type, rejection_reason |
| `memory.trigger.suppressed` | Trigger passed eligibility but blocked by dedupe/cooldown | trigger_class, event_type, suppression_reason |

---

## Configuration

All trigger policy parameters are defined in `TriggerPolicy` dataclass:

```python
@dataclass
class TriggerPolicy:
    dedupe_window_s: int = 300      # 5 minutes
    cooldown_window_s: int = 60     # 1 minute
    rate_limit_window_s: int = 3600 # 1 hour
    rate_limit_max: int = 20        # per class per hour
    min_title_length: int = 5
    min_summary_length: int = 10
```

The default policy (`DEFAULT_POLICY`) is suitable for production. Tests use
shorter windows for fast verification.
