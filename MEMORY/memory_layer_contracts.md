# Memory Layer Contracts

Phase 2 deliverable — defines the 4-layer memory model, what goes where,
and promotion rules between layers.

Generated: 2026-03-13

---

## Why Layers Exist

Without layers, every memory object is treated the same: a checkpoint has the
same standing as a proven debugging pattern. This causes:

- Transient state promoted as stable truth
- Proven procedures buried under noise
- No visibility into why something was stored or how durable it should be

The 4-layer model assigns each memory object an explicit purpose and lifecycle.

---

## Layer 1: Working

| Property | Value |
|----------|-------|
| **Purpose** | Short-term scratch state for the current execution. Not durable knowledge. |
| **What goes in** | Session checkpoints, in-flight task context, retry state, conversation recaps, temporary tool outputs, working_memory entries |
| **What never goes in** | Research findings, proven patterns, architectural decisions, bug fixes with root causes |
| **Who can write** | Any component (session_manager, watcher, blackboard, telegram) |
| **Retention** | Hours to days. May be garbage collected. 7-day max for session files. |
| **Retrieval purpose** | Resume current work, recover from interruption |
| **Promotion source** | None — this is the entry point |
| **Promotion destination** | → episodic (when a working-layer event represents a notable completed outcome) |
| **Examples** | Session checkpoint, in-progress task tracker, conversation working memory, retry/cancel state |
| **Anti-examples** | "Discovered that vault schema lacks ADR type" (this is episodic), "Always validate schemas before writing" (this is procedural) |

---

## Layer 2: Episodic

| Property | Value |
|----------|-------|
| **Purpose** | Records of specific events that happened — what, when, by whom. Time-bound facts about completed work. |
| **What goes in** | Task completion records, research reports, daily summaries, bug fix records, heartbeat cycle results, decision logs |
| **What never goes in** | Ongoing session state, retry/error artifacts, raw tool output, proven reusable procedures |
| **Who can write** | watcher (task completion), heartbeat (research/planning), daily_summary, orchestrator (workflow completion) |
| **Retention** | Permanent. Episodic records are historical facts. |
| **Retrieval purpose** | Understand what happened, find prior decisions, answer "when did we do X?" |
| **Promotion source** | ← working (completed outcomes) |
| **Promotion destination** | → semantic (when repeated episodic events reveal stable facts or patterns) |
| **Examples** | "Task 0042: implemented vault schema validation, 2026-03-13", "Research summary: context engineering practices", "Daily summary: 2026-03-12" |
| **Anti-examples** | "Always use schema-first approach" (this is procedural), session checkpoint (this is working) |

---

## Layer 3: Semantic

| Property | Value |
|----------|-------|
| **Purpose** | Stable facts, consolidated knowledge, and verified findings that have been confirmed across multiple events. |
| **What goes in** | Confirmed research findings, stable configuration facts, verified integration knowledge, consolidated workflow learnings, architecture facts |
| **What never goes in** | One-off task outputs, unverified hypotheses, session state, raw event logs |
| **Who can write** | Router (via promotion from episodic), operator (manual curation) |
| **Retention** | Permanent. Semantic knowledge persists until superseded. |
| **Retrieval purpose** | Inform planning, provide context for new tasks, answer "what do we know about X?" |
| **Promotion source** | ← episodic (when episodic events are consolidated or verified) |
| **Promotion destination** | → procedural (when semantic knowledge crystallizes into reusable procedures) |
| **Examples** | "Vault schema has 7 canonical note types", "Fusion Memory writes are prompt-delegated and cannot be validated from Python", "NovaCore uses claude CLI subprocess pattern, not SDK" |
| **Anti-examples** | "Task 0042 completed successfully" (this is episodic — specific event, not stable fact), session state |

---

## Layer 4: Procedural

| Property | Value |
|----------|-------|
| **Purpose** | Proven reusable methods, patterns, lessons, and architectural decisions. The highest-value memory layer. |
| **What goes in** | Agent patterns (promoted from 2+ converging learnings), ADRs, debugging playbooks, verified workflow patterns, stable operational procedures |
| **What never goes in** | One-off task records, session state, unverified research, raw episodic events |
| **Who can write** | Router (via promotion from semantic), operator (manual curation) |
| **Retention** | Permanent. Procedural knowledge is the most durable layer. May be superseded but not deleted. |
| **Retrieval purpose** | Guide execution, provide reusable methods, answer "how should we do X?" |
| **Promotion source** | ← semantic (when patterns are proven across multiple instances) |
| **Promotion destination** | None — this is the terminal layer. May be superseded by newer procedures. |
| **Examples** | Agent pattern: "schema-first validation approach", ADR-001: "multi-agent architecture", Debugging guide: "worker timeout troubleshooting" |
| **Anti-examples** | "Completed vault schema fix on 2026-03-13" (this is episodic), "Claude Max plan costs $200/mo" (this is semantic — a fact, not a procedure) |

---

## Promotion Rules Summary

```
working → episodic     Completed outcomes only. Not every working event qualifies.
episodic → semantic    Requires verification or consolidation across 2+ events.
semantic → procedural  Requires proven reuse across 2+ contexts or operator approval.
```

### Disallowed Transitions

| Transition | Why |
|-----------|-----|
| working → semantic | Skips episodic verification. Working state is not verified knowledge. |
| working → procedural | Skips two layers. No shortcut from scratch to proven procedure. |
| episodic → procedural | Skips semantic consolidation. A single event is not a proven procedure. |
| Any layer → working | Demotion back to scratch makes no sense. |
| procedural → semantic | Demotion. Once proven, it stays proven (may be superseded instead). |
| semantic → episodic | Demotion. Stable facts don't un-verify. |

### Allowed Transitions

| From | To | Criteria |
|------|----|---------|
| working | episodic | Event represents a completed, notable outcome |
| episodic | semantic | Pattern confirmed across 2+ events, or operator-verified |
| semantic | procedural | Method proven reusable across 2+ contexts, or operator-promoted |

---

## Layer Assignment Rules

When a memory object is created, its layer is assigned based on:

1. **Explicit layer**: If the caller specifies `current_layer`, use it (validated against enum)
2. **Event type inference**: Map event_type → default layer:
   - session_end, heartbeat_cycle → working
   - task_completed, task_failed, research_completed, plan_created, decision_made, bug_fixed, code_changed, plan_revised, conversation_insight → episodic
   - workflow_learning_promoted, user_preference → semantic
   - agent_pattern_promoted → procedural
3. **Provenance override**: `provenance=operator_requested` → allow any layer (operator knows best)
