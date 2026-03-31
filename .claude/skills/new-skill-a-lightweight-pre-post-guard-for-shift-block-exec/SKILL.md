It seems the sandbox is blocking writes to `.claude/skills/`. Let me output the complete SKILL.md content directly for you to place at `.claude/skills/shift-block-guard/SKILL.md`:

---
name: shift-block-guard
description: "Pre/post guard for shift-block executions — prevents vacuous runs by asserting checkpoint loaded, work done, and artifacts produced."
activation:
  keywords:
    - shift block
    - shift guard
    - preflight
    - postflight
    - vacuous run
    - shift execution
    - block guard
    - shift start
    - shift end
  when:
    - Beginning of any shift-block execution (shift_YYYYMMDD_N_*)
    - End of any shift-block execution before reporting completion
    - When reviewing whether a shift block produced meaningful work
tool_doctrine:
  pre_guard:
    workflow:
      - load_last_checkpoint
      - parse_task_body
      - assert_context_loaded
  post_guard:
    workflow:
      - assert_health_checks_ran
      - assert_output_artifact_written
      - assert_checkpoint_created
output_contract:
  required:
    - phase (pre | post)
    - gate_result (PASS | FAIL)
    - checks_performed
    - evidence
    - blocked_reason (if FAIL)
---

# Shift Block Guard

Lightweight pre/post gate that wraps every shift-block execution. Prevents vacuous runs — sessions that complete without loading context, doing real work, or producing artifacts.

## Problem This Solves

Shift blocks can silently succeed while doing nothing meaningful: the checkpoint isn't loaded so context is empty, health checks are skipped, no OUTPUT/ artifact is written, and no memory checkpoint is created. The shift "completes" but adds zero value. This skill enforces three invariants that make vacuous runs structurally impossible.

## When To Invoke

- **PRE-GUARD**: At the very start of any shift-block execution, before substantive work begins.
- **POST-GUARD**: At the end of any shift-block execution, before declaring the shift complete.
- **REVIEW**: After a shift completes, to audit whether it met the guard contract.

## Pre-Guard (run FIRST in every shift block)

### Check 1 — Last checkpoint loaded

```
Tool: mcp__nova-memory__get_last_checkpoint
Args: {"project": "nova-core"}
```

**PASS** if: A checkpoint is returned with a valid `session_id`, `last_event_seq`, and `open_threads`/`next_actions`.
**FAIL** if: No checkpoint returned, or the checkpoint is stale (>48h old with no explanation).

Record the checkpoint `session_id` and `open_threads` — these feed into the shift's work plan.

### Check 2 — Task body parsed

Read the shift task file and extract:
- **Shift label** (e.g., `shift_20260330_1_system_health`)
- **Objective** — what the shift is supposed to accomplish
- **Required actions** — the concrete steps listed in the task

**PASS** if: All three fields are non-empty and actionable.
**FAIL** if: Task body is empty, unparseable, or contains only boilerplate with no concrete actions.

### Pre-Guard Verdict

Both checks must PASS to proceed. If either FAILS:
1. Log the failure to `LOGS/shift_guard_<shift_label>.log`
2. Do NOT proceed with shift execution
3. Emit the contract with `gate_result: FAIL` and `blocked_reason`

## Post-Guard (run LAST in every shift block)

### Check A — Health checks ran

Verify that at least one substantive diagnostic command was executed during the shift. Evidence includes:
- `systemctl status` or `systemctl is-active` calls for relevant services
- Port/process checks (`ss -tlnp`, `pgrep`)
- API health endpoint hits (`/health`, `/readiness`)
- Python test runs (`pytest`)
- Log file inspection for errors

**PASS** if: At least one shell-level health check was executed and its result was recorded.
**FAIL** if: No diagnostic commands were run — the shift skipped all verification.

### Check B — OUTPUT/ artifact written

```
ls -lt OUTPUT/ | head -5
```

Verify that a new file was written to `OUTPUT/` during this shift with:
- A filename containing the shift label or a matching timestamp
- Non-zero file size
- Content that summarizes actual findings (not just a template or placeholder)

**PASS** if: A substantive output artifact exists from this shift.
**FAIL** if: No new OUTPUT/ file, or the file is empty/boilerplate.

### Check C — Memory checkpoint created

```
Tool: mcp__nova-memory__get_last_checkpoint
Args: {"project": "nova-core"}
```

Verify the returned checkpoint:
- Has a `session_id` that matches or postdates this shift
- Has a `last_event_seq` greater than the pre-guard checkpoint's value
- Contains `open_threads` and `next_actions` (not empty arrays)

**PASS** if: A fresh checkpoint exists with meaningful content.
**FAIL** if: No new checkpoint, or `session_id`/`event_seq` unchanged from pre-guard.

### Post-Guard Verdict

All three checks (A, B, C) must PASS for the shift to be considered complete. If any FAIL:
1. Log the failure to `LOGS/shift_guard_<shift_label>.log`
2. Attempt remediation:
   - Missing health check → run a quick `systemctl is-active novatrade` + process check
   - Missing output → write a summary of what was actually done to `OUTPUT/`
   - Missing checkpoint → create one with the shift's actual outcomes
3. Re-run the failed check after remediation
4. If still FAIL after remediation, emit the contract with `gate_result: FAIL`

## Expected Inputs

- **Shift task file path** (e.g., `TASKS/shift_20260330_1_system_health.md`)
- **Shift label** (extracted from filename)
- Access to Fusion Memory MCP (`get_last_checkpoint`, `create_checkpoint`)
- Access to `OUTPUT/` and `LOGS/` directories

## Expected Outputs

### Pre-Guard Contract

```
## SHIFT PRE-GUARD CONTRACT
phase: pre
shift_label: shift_20260330_1_system_health
gate_result: PASS
checks_performed:
  - checkpoint_loaded: PASS (session-2026-03-29, event_seq=142)
  - task_body_parsed: PASS (objective: system health audit, 4 required actions)
evidence:
  checkpoint_session: session-2026-03-29
  open_threads: [novatrade monitoring, strategy validity check]
  task_objective: "Run system health diagnostics and report status"
blocked_reason: null
```

### Post-Guard Contract

```
## SHIFT POST-GUARD CONTRACT
phase: post
shift_label: shift_20260330_1_system_health
gate_result: PASS
checks_performed:
  - health_checks_ran: PASS (systemctl status x3, port check, /health endpoint)
  - output_artifact_written: PASS (OUTPUT/shift_20260330_1_system_health_20260330_083000.md, 2.4KB)
  - checkpoint_created: PASS (session-2026-03-30, event_seq=148)
evidence:
  diagnostics_run: ["systemctl is-active novatrade", "ss -tlnp | grep 8877", "curl localhost:8877/health"]
  output_file: OUTPUT/shift_20260330_1_system_health_20260330_083000.md
  checkpoint_session: session-2026-03-30
  checkpoint_event_seq: 148
blocked_reason: null
```

## Error Handling

- **Fusion Memory unreachable**: Pre-guard Check 1 becomes INCONCLUSIVE. Log the connectivity failure. Proceed with shift but flag that checkpoint chain is broken. Post-guard Check C also INCONCLUSIVE — write output to `LOGS/` as fallback evidence.
- **Task file missing or empty**: Pre-guard FAIL. Do not execute the shift. Log the failure.
- **OUTPUT/ directory missing**: Create it. This is not a failure condition — the directory may not exist yet.
- **Remediation loop**: Post-guard remediation gets exactly one attempt per check. If it fails after remediation, accept the FAIL — do not retry indefinitely.
- **Stale checkpoint ambiguity**: If the last checkpoint is >48h old, treat as a soft warning (PASS with note), not a hard FAIL — the operator may have been offline.

---

You can write this to `.claude/skills/shift-block-guard/SKILL.md`. Want me to try writing it via the Write tool directly?