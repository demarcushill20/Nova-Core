---
name: auditing-obsidian-memory-safety
description: "Validate that proposed Obsidian memory writes are bounded, correctly typed, provenance-safe, non-secret-bearing, and compliant with the runtime-versus-memory boundary. Invoked when a workflow-learning or agent-pattern note candidate is about to be written, or when a reviewer needs to validate memory-write compliance."
disable-model-invocation: false
allowed-tools:
  - mcp__nova-vault__vault_validate
  - mcp__nova-vault__vault_read
  - mcp__nova-vault__vault_search
  - mcp__nova-vault__vault_frontmatter
activation:
  keywords:
    - audit memory
    - memory safety
    - validate write
    - memory audit
---

# Auditing Obsidian Memory Safety

## When to use

- A workflow-learning note candidate is about to be written to the vault
- An agent-pattern note candidate is about to be written to the vault
- A memory write needs safety review before approval
- A reviewer or operator wants to validate memory-write compliance
- An automated promotion pathway (Phase 5.5 or 6.5) needs pre-write audit
- You want to verify a proposed note does not violate runtime-boundary rules

## When NOT to use

- General vault browsing or reading — use `reading-obsidian-memory` instead
- Arbitrary note editing or freeform vault mutation — not supported
- Runtime-state inspection unrelated to a proposed write — use STATE/ files
- Weak speculative write proposals that lack a concrete note payload — nothing to audit
- Auditing existing notes already in the vault — this skill is for proposed writes, not retrospective scanning
- Performing the actual write — use `capturing-workflow-learnings` or `writing-agent-patterns` instead

## Inputs

- **candidate_content**: The full proposed note content (frontmatter + body) to audit. Required.
- **target_path**: The intended vault-relative path (e.g., `30-workflow-learnings/2026-03-example.md`). Required.
- **write_mode**: The intended write operation — `create` or `update`. Required.
- **context**: Optional description of why this write is being proposed (e.g., "post-execution workflow learning promotion").

## Audit Workflow

### Step 1 — Target folder check

Extract the top-level folder from `target_path` and verify it is in the approved set:

**Approved writable folders:**
- `00-inbox`
- `20-agent-patterns`
- `30-workflow-learnings`
- `40-research`
- `70-debugging`

If the folder is not in this set, **reject immediately**. No further checks needed.

Also verify:
- Path ends with `.md`
- Path contains no null bytes, `..` traversal, or absolute prefixes
- Path does not target `.obsidian/` or other system directories
- Filename is reasonable length (< 200 chars)

### Step 2 — Note type and schema check

Parse the candidate's YAML frontmatter and verify:

1. `type` field is present and matches a recognized note type:
   - `agent-pattern` (required tag: `#type/pattern`)
   - `workflow-learning` (required tag: `#type/learning`)
   - `research-summary` (required tag: `#type/research`)
   - `debugging-guide` (required tag: `#type/debugging`)
   - `inbox` (required tag: `#type/inbox`)
   - `moc` (required tag: `#type/moc`)

2. All required frontmatter fields for that type are present:

   | Type | Required Fields |
   |------|-----------------|
   | `agent-pattern` | type, pattern_id, title, agent_role, confidence, task_classes, date_created, source, tags |
   | `workflow-learning` | type, learning_id, title, workflow_id, task_class, verification_outcome, confidence, roles_involved, date, source, tags |
   | `research-summary` | type, research_id, title, topic, date_researched, sources_count, confidence, source, tags |
   | `debugging-guide` | type, title, date_created, source, tags |
   | `inbox` | type, title, source, tags |

3. Enum fields have valid values:
   - `confidence`: high, medium, low
   - `source`: operator, nova-core-memory
   - `agent_role`: research, coder, critic, verifier, planner, memory
   - `task_class`: research, code_impl, code_review, system, simple, unknown
   - `verification_outcome`: approved, rejected, partial, not_verified

4. Tags list is non-empty, includes the required type tag, and has at most 10 entries

5. Title is present and at most 100 characters

6. Check for `#domain/*` tag presence (soft warning — do not reject if missing, but record as a warning in the audit results)

Use `vault_validate` to confirm schema compliance. If validation returns errors, record each error as a rejection reason.

### Step 3 — Provenance check

Verify the `source` field:

- For tool-written notes (via `vault_write`), `source` MUST be `"nova-core-memory"`. Any other value is rejected.
- The `source` field must be present in frontmatter. Missing source is rejected.
- `source: "operator"` is reserved for human-authored notes and must NOT appear in tool-written candidates.

### Step 4 — Runtime-boundary check

Scan the candidate content (both frontmatter and body) for signs that runtime/operational state is being stored in Obsidian. **Obsidian stores durable guidance and reusable memory, not live orchestration truth.**

**Red flags — reject or flag for review:**

- Live task status fields (`status: running`, `status: queued`, `status: pending`)
- Approval/budget/lease tracking (`approved_budget`, `remaining_credits`, `lease_expiry`)
- Transient workflow control data (`next_step`, `retry_count`, `current_phase`)
- Active dependency state (`blocked_by`, `waiting_for`, `depends_on` with live references)
- Execution locks or queue state (`lock_holder`, `queue_position`, `pid`)
- Ephemeral file paths that will not persist (`/tmp/`, `STATE/running/`, `TASKS/*.inprogress`)
- Live timestamps that track current execution (`last_heartbeat`, `started_at` as live values)

**Acceptable content:**
- Historical dates (`date: "2026-03-08"`, `date_created`)
- Completed workflow references (`workflow_id: "session_33"`)
- Static guidance and patterns
- Summarized metrics from completed work
- References to vault notes (`[[related-note]]`)

See `reference/runtime-boundary-redflags.md` for the full checklist.

### Step 5 — Secret and sensitive content check

Scan the candidate content for obvious secrets or credentials. This is a lightweight rule-based check, not a full DLP platform.

**Red flag patterns — reject immediately:**

| Pattern | Example |
|---------|---------|
| API key prefixes | `sk-`, `pk_live_`, `AKIA`, `ghp_`, `gho_`, `tvly-`, `BSA` followed by key-like strings |
| Password-like fields | `password:`, `passwd:`, `secret:`, `token:` followed by non-placeholder values |
| Private key blocks | PEM private key headers (RSA, EC, DSA variants) |
| Environment variable dumps | `export API_KEY=`, `export SECRET_KEY=` |
| Connection strings with credentials | `postgres://user:pass@`, `mysql://root:` |
| Raw bearer tokens | `Authorization: Bearer <long-string>` |

**Not flagged (acceptable):**
- Placeholder references (`"use the API key from environment"`)
- Pattern names that happen to contain "key" or "token" (`"multi-query-strategy"`)
- Documentation about security practices

**When uncertain, reject.** False positives are preferable to leaked credentials.

### Step 6 — Write-mode and ownership check

Verify the proposed write mode is appropriate:

- **create mode**: File must NOT already exist at target path. Use `vault_search` or `vault_read` to check if a note with the same path or very similar title already exists.
- **update mode**: File MUST already exist. Only Nova-Core-managed notes (`source: "nova-core-memory"`) can be updated. Human-authored notes (`source: "operator"` or unknown ownership) are never updateable by tools.
- Notes in `20-agent-patterns/` are **create-only** — updates to existing patterns are not allowed through the automated path.

### Step 7 — Size and structure check

- Total note size must not exceed 34,816 bytes (vault limit)
- Note should have both frontmatter (YAML between `---` delimiters) and a body
- Body should contain at least one markdown heading (`##`)
- Minimum viable size: 200 bytes (anything shorter is probably too thin)

### Step 8 — Compile audit result

Assemble the structured audit output (see Output Contract below). For each check dimension, record pass/fail/warning status and any specific rejection reasons.

**Decision logic:**
- If ANY check produces a rejection reason → `recommended_action: "reject"`
- If all checks pass but warnings exist → `recommended_action: "needs_human_review"`
- If all checks pass with no warnings → `recommended_action: "approve"`

## Tool Usage Rules

- **vault_validate**: Primary tool. Use to verify schema compliance of the candidate note. Always call this.
- **vault_read**: Use only if needed to check whether a target path already exists (ownership/dedup verification for create-only enforcement).
- **vault_search**: Use only if needed to check for duplicate or similar notes before approving a create.
- **vault_frontmatter**: Use only if needed to inspect existing note metadata for ownership checks.
- **No vault_write.** This skill audits. It never writes.
- **No vault_update.** This skill audits. It never mutates.
- **Bounded calls.** Maximum 4 tool calls per audit: 1 validate + 1 search (dedup) + 1 read (ownership) + 1 spare.

## Output Contract

Every invocation of this skill MUST produce:

```
## Memory Safety Audit

### Candidate
- **Note type**: <workflow-learning | agent-pattern | research-summary | debugging-guide | inbox>
- **Target path**: <vault-relative path>
- **Write mode**: <create | update>
- **Context**: <why this write is proposed>

### Audit Results

| Dimension | Status | Detail |
|-----------|--------|--------|
| Approved folder | pass/fail | <folder name, approved or rejected> |
| Path safety | pass/fail | <path validation result> |
| Schema validity | pass/fail | <vault_validate result or error list> |
| Required fields | pass/fail | <missing fields if any> |
| Provenance | pass/fail | <source field value and validity> |
| Runtime boundary | pass/fail/warn | <red flags found or clean> |
| Secret scan | pass/fail | <patterns matched or clean> |
| Write mode | pass/fail | <create-only/ownership compliance> |
| Ownership | pass/fail/n-a | <ownership check result> |
| Size/structure | pass/fail | <size, frontmatter present, body present> |
| Tag taxonomy | pass/warn | <#domain/* tag present or missing warning> |

### Decision
- **Audit passed**: <true | false>
- **Rejection reasons**: <list, or "none">
- **Warnings**: <list, or "none">
- **Recommended action**: <approve | reject | needs_human_review>

### Audit Log
| # | Tool | Input | Result |
|---|------|-------|--------|
| 1 | vault_validate | <note content> | valid/invalid |
| 2 | vault_search | "dedup query" | 0 hits / N hits |

### Confidence
<high / medium / low> — <1-sentence justification>
```

## Governance Principles

This skill enforces the following non-negotiable rules:

1. **Obsidian is not runtime truth.** Vault notes store durable guidance, reusable memory, and learned patterns. They do not store live task status, execution locks, or transient workflow state.
2. **No secrets in vault notes.** API keys, tokens, passwords, and credentials must never be written to the vault.
3. **Writes only through governed pathways.** All tool-mediated writes use `vault_write` (create-only) or `vault_update` (append-only, Nova-Core notes only). This skill validates compliance with those pathways.
4. **Write only to approved folders.** The five approved folders are the only valid targets.
5. **Validate schema before write.** Every note must pass `vault_validate` before being written.
6. **Log every write.** All writes are logged to `.nova-audit.log` by the write path itself. This skill ensures the candidate is audit-ready.
7. **Human-authored notes are untouchable.** Notes with `source: operator` or unknown ownership are never modified by tools.
8. **Prefer rejection over weak approval.** When uncertain about safety, reject or flag for human review.

## Examples

### Example 1: Approving a valid workflow learning

**Candidate**: Well-formed workflow-learning note with all required fields, `source: "nova-core-memory"`, target `30-workflow-learnings/2026-03-example.md`, no secrets, no runtime state.

**Decision**: approve — all 10 audit dimensions pass.

### Example 2: Rejecting a note with secrets

**Candidate**: Agent-pattern note containing `export OPENAI_API_KEY=sk-abc123...` in the body.

**Decision**: reject — secret scan failed (API key pattern detected in body).

### Example 3: Rejecting runtime state in Obsidian

**Candidate**: Note with frontmatter containing `status: running`, `retry_count: 3`, `next_step: "execute_phase_2"`.

**Decision**: reject — runtime boundary check failed (live task status, transient workflow control data).

### Example 4: Flagging a borderline case

**Candidate**: Valid workflow-learning note, all fields correct, but body references `STATE/running/task_0042.pid` as an active dependency.

**Decision**: needs_human_review — runtime boundary warning (reference to active STATE/ file).
