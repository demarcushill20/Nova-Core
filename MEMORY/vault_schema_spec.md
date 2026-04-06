# Vault Note Schema Specification

Phase 0 deliverable — canonical type enum, required fields, validation rules,
and mapping of legacy types found in the vault.

Generated: 2026-03-13
Source: tools/mcp_vault_server.py validation logic + audit of all existing vault notes.

---

## 1. Canonical Note Type Enum

| Type | Required Tag | Purpose | Writable Folders |
|------|-------------|---------|-----------------|
| `agent-pattern` | `#type/pattern` | Reusable agent behavior patterns promoted from 2+ workflow learnings | 20-agent-patterns |
| `workflow-learning` | `#type/learning` | Compacted lessons from completed workflows | 30-workflow-learnings |
| `research-summary` | `#type/research` | Deep research reports with source counts and citations | 40-research |
| `implementation-plan` | `#type/plan` | Phased implementation plans with status tracking | 00-inbox (created), 10-plans (operator-moved) |
| `debugging-guide` | `#type/debugging` | Debugging playbooks and troubleshooting guides | 70-debugging |
| `inbox` | `#type/inbox` | Catch-all for notes that don't fit other types (daily summaries, misc) | 00-inbox |

| `adr` | `#type/adr` | Architecture Decision Records | 10-adrs (read-only, operator-managed) |
| `moc` | `#type/moc` | Map of Content — index/hub notes for topic clusters | 00-inbox |

No other type values are accepted. Any note with a type not in this list will be rejected.

---

## 2. Required Fields Per Type

### agent-pattern
| Field | Type | Required | Validation |
|-------|------|----------|------------|
| type | string | YES | Must be "agent-pattern" |
| pattern_id | string | YES | Non-empty. Convention: "ap-<slug>" |
| title | string | YES | Non-empty, ≤ 100 chars |
| agent_role | string | YES | Enum: research, coder, critic, verifier, planner, memory |
| confidence | string | YES | Enum: high, medium, low |
| task_classes | list[string] | YES | Each element in: research, code_impl, code_review, system, simple, unknown |
| date_created | string | YES | Non-empty. Convention: YYYY-MM-DD |
| source | string | YES | Enum: operator, nova-core-memory |
| tags | list[string] | YES | Non-empty, must include "#type/pattern", max 10 |

### workflow-learning
| Field | Type | Required | Validation |
|-------|------|----------|------------|
| type | string | YES | Must be "workflow-learning" |
| learning_id | string | YES | Non-empty. Convention: "wl-YYYY-MM-<slug>" |
| title | string | YES | Non-empty, ≤ 100 chars |
| workflow_id | string | YES | Non-empty |
| task_class | string | YES | Enum: research, code_impl, code_review, system, simple, unknown |
| verification_outcome | string | YES | Enum: approved, rejected, partial, not_verified |
| confidence | string | YES | Enum: high, medium, low |
| roles_involved | list[string] | YES | Non-empty list |
| date | string | YES | Non-empty. Convention: YYYY-MM-DD |
| source | string | YES | Enum: operator, nova-core-memory |
| tags | list[string] | YES | Non-empty, must include "#type/learning", max 10 |

### research-summary
| Field | Type | Required | Validation |
|-------|------|----------|------------|
| type | string | YES | Must be "research-summary" |
| research_id | string | YES | Non-empty. Convention: "rs-<topic>-YYYY-MM-DD" |
| title | string | YES | Non-empty, ≤ 100 chars |
| topic | string | YES | Non-empty |
| date_researched | string | YES | Non-empty. Convention: YYYY-MM-DD |
| sources_count | integer | YES | Must be int |
| confidence | string | YES | Enum: high, medium, low |
| source | string | YES | Enum: operator, nova-core-memory |
| tags | list[string] | YES | Non-empty, must include "#type/research", max 10 |

### implementation-plan
| Field | Type | Required | Validation |
|-------|------|----------|------------|
| type | string | YES | Must be "implementation-plan" |
| plan_id | string | YES | Non-empty |
| title | string | YES | Non-empty, ≤ 100 chars |
| date_created | string | YES | Non-empty. Convention: YYYY-MM-DD |
| confidence | string | YES | Enum: high, medium, low |
| source | string | YES | Enum: operator, nova-core-memory |
| tags | list[string] | YES | Non-empty, must include "#type/plan", max 10 |
| status | string | NO (optional) | If present, enum: backlog, active, completed, paused |
| priority | string | NO (optional) | If present, enum: high, medium, low |
| progress | string | NO (optional) | If present, format: "X/Y" (e.g., "3/7") |

**Note on status/priority/progress**: These fields are used by the plan-tracker skill and
exist on the 6 plan notes in 10-plans/. They are validated when present but not required,
to preserve backward compatibility with heartbeat-generated plans that omit them.

### debugging-guide
| Field | Type | Required | Validation |
|-------|------|----------|------------|
| type | string | YES | Must be "debugging-guide" |
| title | string | YES | Non-empty, ≤ 100 chars |
| date_created | string | YES | Non-empty |
| source | string | YES | Enum: operator, nova-core-memory |
| tags | list[string] | YES | Non-empty, must include "#type/debugging", max 10 |

### inbox
| Field | Type | Required | Validation |
|-------|------|----------|------------|
| type | string | YES | Must be "inbox" |
| title | string | YES | Non-empty, ≤ 100 chars |
| source | string | YES | Enum: operator, nova-core-memory |
| tags | list[string] | YES | Non-empty, must include "#type/inbox", max 10 |

### adr
| Field | Type | Required | Validation |
|-------|------|----------|------------|
| type | string | YES | Must be "adr" |
| adr_id | string | YES | Non-empty. Convention: "ADR-NNN" |
| title | string | YES | Non-empty, ≤ 100 chars |
| status | string | YES | Enum: proposed, accepted, deprecated, superseded |
| date | string | YES | Non-empty. Convention: YYYY-MM-DD |
| source | string | YES | Enum: operator, nova-core-memory |
| tags | list[string] | YES | Non-empty, must include "#type/adr", max 10 |

### moc
| Field | Type | Required | Validation |
|-------|------|----------|------------|
| type | string | YES | Must be "moc" |
| moc_id | string | YES | Non-empty. Convention: "moc-<domain-slug>" |
| title | string | YES | Non-empty, ≤ 100 chars |
| domain | string | YES | Enum: novatrade, infrastructure, memory, autonomy, research, debugging, agents, risk, trading-strategies, operations |
| date_created | string | YES | Non-empty. Convention: YYYY-MM-DD |
| source | string | YES | Enum: operator, nova-core-memory |
| tags | list[string] | YES | Non-empty, must include "#type/moc", max 10 |

---

## 3. Global Validation Rules

1. **type** must be present and in the canonical enum
2. **All required fields** must be present, non-null, and non-empty (for strings)
3. **title** ≤ 100 characters
4. **tags** must be a non-empty list, include the type-specific required tag, max 10 tags
5. **source** must be in {"operator", "nova-core-memory"}
6. **confidence** (when present) must be in {"high", "medium", "low"}
7. **Sensitive content** — notes containing API keys, passwords, tokens, private keys are rejected
8. **Size** — total note size ≤ 34 KB
9. **Rate limit** — max 10 writes per 5-minute window
10. **No overwrite** — vault_write rejects if file already exists (append-only model)
11. **Source enforcement** — vault_write requires source="nova-core-memory" (operator notes are human-created)

---

## 4. Schema Violations Found (2026-03-13 Audit)

### 4.1 FIXED: implementation-plan missing field validation

**Problem**: The plan-tracker skill expects `status`, `priority`, and `progress` fields on
implementation-plan notes. The MCP server's validate_frontmatter() did not validate these
fields — they could be missing, have invalid values, or be malformed with no error.

**Impact**: Plans created by heartbeat (in 00-inbox/) lack these fields. Plans in 10-plans/
have these fields because the plan-tracker skill added them, but their values were never validated.

**Fix**: Added optional field validation for implementation-plan in validate_frontmatter():
- `status` (if present): must be in {"backlog", "active", "completed", "paused"}
- `priority` (if present): must be in {"high", "medium", "low"}
- `progress` (if present): must match pattern "X/Y" (digits/digits)

These remain optional to preserve backward compatibility with existing heartbeat-generated plans.

### 4.2 FIXED: "adr" type not in canonical enum

**Problem**: `10-adrs/ADR-001-multi-agent-architecture.md` has `type: "adr"` which was
not in the `_VALID_NOTE_TYPES` dictionary.

**Impact**: The ADR was in a read-only folder so writes were never attempted, but the type
was not recognized by the validator.

**Fix**: Added `"adr": "#type/adr"` to the canonical type enum, with required fields
(adr_id, title, status, date, source, tags) and status enum validation
(proposed, accepted, deprecated, superseded).

### 4.3 FIXED: plan-phase4 has status "not-started"

**Problem**: `00-inbox/plan-phase4-intelligent-heartbeat-20260312.md` has `status: "not-started"`
which is not in the plan status enum (backlog, active, completed, paused).

**Fix**: The repair script maps "not-started" → "backlog". Run with `--repair` to apply.

### 4.4 EXISTING: fusion-memory-chronological-upgrade-plan has invalid confidence

**Problem**: `40-research/fusion-memory-chronological-upgrade-plan.md` has:
- Missing `#type/research` tag (has other tags but not the required type tag)
- `confidence: "0.9"` (numeric string, not in {high, medium, low} enum)

**Impact**: This note was written before the validation pipeline existed.
It requires manual repair (confidence "0.9" → "high" and tag addition).

### 4.5 OBSERVATION: 6 notes with no frontmatter

These are all operator-authored or external documents. They are valid as-is:
- `10-plans/NovaCore Intelligent Memory Upgrade Roadmap.md` (operator-authored)
- `20 powerful use cases for combining n8n with Claude Code.md` (vault root, external)
- 4 notes in `40-research/` (operator-authored early research)

### 4.6 OBSERVATION: Human-authored notes in 40-research/

Four notes in 40-research/ have `source: "operator"` or no frontmatter (human-authored).
These are valid — the schema allows source="operator". The vault_update ownership check
correctly prevents nova-core from modifying operator-authored notes.

---

## 5. Legacy Type Mapping

No legacy types were found. All existing notes use canonical type values.
This section is included for completeness in case future migrations produce type drift.

| Legacy Type | Canonical Type | Migration Action |
|-------------|---------------|-----------------|
| (none found) | — | — |

---

## 6. Validation Code Reference

- **Schema validator**: `tools/mcp_vault_server.py` → `validate_frontmatter()` (L375-466)
- **Standalone validator**: `schemas/vault_note_schema.py` (created in Phase 0)
- **Repair script**: `scripts/repair_vault_types.py` (created in Phase 0)
- **Test coverage**: `tests/test_mcp_vault_server.py` → `TestValidateFrontmatter` (18 tests)
