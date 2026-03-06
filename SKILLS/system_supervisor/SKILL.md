# Skill: system_supervisor

Meta-skill for validating step outputs, making continue/retry/escalate
decisions, and ensuring contract compliance across multi-skill plans.

---

## Frontmatter

```yaml
name: system_supervisor
version: 1.0.0
description: >
  Validate each execution step's output contract, decide whether to continue,
  retry, or escalate, evaluate execution quality with deterministic grading,
  and emit a structured supervisor contract block.

tools:
  - contracts.validate
  - logs.tail
  - shell.run

tool_rules:
  - "read-only by default — only validate, never mutate"
  - "never modify task or plan files directly"
  - "always emit a contract block"

output_contract:
  - summary
  - files_changed
  - verification
  - confidence
```

---

## When To Use

- After each step in a multi-skill execution plan completes.
- When the orchestrator needs a continue/retry/escalate decision.
- As the final step in any plan to validate overall results.
- When anomalous tool or runtime behavior is detected.

Do **not** use for:
- Direct task execution (use the appropriate domain skill).
- Modifying code or files (delegate to code_improve or file_ops).
- Initial task interpretation (that is PlanBuilder's job).

---

## Workflow

### 1. Receive step result

Accept the StepResult from the orchestrator, including:
- `status` field (success, failed, error, skipped)
- `contract_valid` flag
- `validation_errors` list
- `retry_count` integer

### 2. Validate output contract

Use `contracts.validate` to check the step's output for required fields.

```
Tool: contracts.validate
Args: { "text": "<step output text including ## CONTRACT block>" }
```

Check that all required contract fields are present and non-empty:
- `summary`
- `files_changed` (if applicable)
- `verification`
- `confidence`

### 3. Check logs for anomalies

Use `logs.tail` to inspect recent log entries for errors or warnings
that might indicate runtime issues not captured in the step result.

```
Tool: logs.tail
Args: { "path": "LOGS/", "lines": 50 }
```

Look for:
- Repeated errors or stack traces
- Timeout warnings
- Permission denied patterns
- Unexpected process exits

### 4. Make decision

Apply the decision rules:

| Condition | Action |
|-----------|--------|
| contract valid, no anomalies | **continue** |
| contract invalid, retries remaining | **retry** |
| contract invalid, retries exhausted | **escalate** |
| runtime anomaly detected | **escalate** |
| step success but contract missing | **retry** (contract formatting issue) |

### 5. Generate follow-up (if escalating)

When escalating, produce a structured follow-up task dict:
- `title`: descriptive failure summary
- `description`: error details, contract status, duration
- `priority`: high
- `source`: supervisor

### 6. Evaluate execution quality

After all steps complete (or execution stops), the Evaluator grades each step
and the overall plan:

- **Step scoring**: execution_success (0.40) + contract_bonus (0.25) + verification_score (0.20) + duration_score (0.15) - retry_penalty (0.05/retry)
- **Grade mapping**: A (≥0.90), B (≥0.75), C (≥0.60), D (≥0.40), F (<0.40)
- **Follow-up recommendation**: D/F → high priority, B/C with issues → medium priority, A → none

The evaluation is persisted in the plan state JSON and is available for
downstream decisions and self-improvement.

### 7. Emit contract

Produce the final supervisor contract block.

---

## Tool Usage Rules

- **Read-only operations only.** The supervisor observes and decides; it does not fix.
- All paths must be within `~/nova-core`.
- Use `contracts.validate` for structured contract checking.
- Use `logs.tail` for anomaly detection — do not read entire log files.
- Use `shell.run` only for read-only diagnostic commands (e.g., `systemctl status`).
- Never modify task files, plan files, or code.

---

## Verification

After each supervisor evaluation:

1. Confirm a decision was produced (one of: continue, retry, escalate, abort).
2. Confirm the decision has a non-empty reason string.
3. If escalating, confirm a follow-up task dict was generated.
4. Confirm the contract block has all required fields.

---

## Failure Handling

- **Contract validation tool unavailable**: fall back to manual field checks (presence of `summary`, `verification` in output text). Mark confidence as `low`.
- **Logs unreadable**: skip anomaly detection, note in reason. Mark confidence as `medium`.
- **Decision logic error**: default to `escalate` with an explanation. Never silently continue after an error.
- **Step result missing fields**: treat as contract invalid; apply retry/escalate rules.

---

## Output Contract

Every system_supervisor evaluation must end with:

```
## CONTRACT
summary: <one-line description of what was evaluated>
files_changed: none
verification: <checks performed and their pass/fail results>
confidence: <high | medium | low>
```
