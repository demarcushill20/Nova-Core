# Trigger Source Inventory

Phase 3 deliverable — identifies actual code locations that emit or observe
events relevant to automatic memory creation.

Generated: 2026-03-13

---

## Wired Sources (Phase 3)

### 1. Task Completion — watcher.py

| Property | Value |
|----------|-------|
| **File** | `watcher.py` L1140–1158 |
| **Function** | `dispatch()` — after lifecycle finalization |
| **Trigger class** | `task_lifecycle` |
| **Event types** | `task_completed`, `task_failed` |
| **Data available** | stem, task_class, contract (summary, files_changed, confidence), passed/failed |
| **Current behavior** | Legacy `capture_direct_task_memory()` + new `trigger_engine.fire()` |
| **Initial layer** | episodic |
| **Dedupe key** | Content hash of event_type + title + summary |
| **Priority** | P0 — highest-value trigger source |
| **Risk** | Low — non-fatal try/except, runs after legacy capture |
| **Status** | **WIRED** |

### 2. Task Failure — watcher.py

| Property | Value |
|----------|-------|
| **File** | `watcher.py` L1140–1158 (same block as completion) |
| **Function** | `dispatch()` — same block, different event_type |
| **Trigger class** | `task_lifecycle` |
| **Event types** | `task_failed` |
| **Data available** | Same as completion, with `passed=False` |
| **Initial layer** | episodic |
| **Status** | **WIRED** (same code path as task completion) |

### 3. Plan Execution Outcome — planner/orchestrator.py

| Property | Value |
|----------|-------|
| **File** | `planner/orchestrator.py` L178–190 |
| **Function** | `run_plan()` — after evaluation and improvement cycle |
| **Trigger class** | `plan_lifecycle` |
| **Event types** | `plan_created` (done), `plan_revised` (failed) |
| **Data available** | plan_id, task_id, status, grade, summary, step_count |
| **Current behavior** | Saves plan state to STATE/plans/ + now fires trigger |
| **Initial layer** | episodic |
| **Dedupe key** | plan_id + status in title |
| **Priority** | P0 — captures orchestrator outcomes |
| **Risk** | Low — helper function with try/except |
| **Status** | **WIRED** |

### 4. Heartbeat Cycle — heartbeat.py

| Property | Value |
|----------|-------|
| **File** | `heartbeat.py` L1603–1618 |
| **Function** | `main()` — after health check completion |
| **Trigger class** | `session_boundary` |
| **Event types** | `heartbeat_cycle` |
| **Data available** | checks count, fail_names, all_ok status |
| **Current behavior** | Writes HEARTBEAT.md + Telegram alert + now fires trigger |
| **Initial layer** | working |
| **Dedupe key** | Content hash includes HEALTHY/UNHEALTHY status |
| **Priority** | P1 — regular health snapshots |
| **Risk** | Low — guarded import, non-fatal |
| **Status** | **WIRED** |

---

## Identified But Not Wired (Future Phases)

### 5. Session End — watcher.py / session_manager.py

| Property | Value |
|----------|-------|
| **File** | `agents/session_manager.py` |
| **Trigger class** | `session_boundary` |
| **Event types** | `session_end` |
| **Data available** | session_id, task count, duration |
| **Current behavior** | Sessions tracked in STATE/sessions/ |
| **Triggerability** | Medium — needs clear session boundary signal |
| **Priority** | P2 |
| **Blocker** | Session expiry is passive (timeout-based), no explicit end event |

### 6. Research Cycle — heartbeat.py

| Property | Value |
|----------|-------|
| **File** | `heartbeat.py` L968–1030 |
| **Trigger class** | (would be `research`) |
| **Event types** | `research_completed` |
| **Data available** | topic, findings, sources |
| **Current behavior** | Prompt-delegated: saves to Fusion Memory + Obsidian via Claude subprocess |
| **Triggerability** | Not feasible from Python — prompt-delegated |
| **Priority** | P3 |
| **Blocker** | Cannot intercept Claude subprocess MCP calls from Python |

### 7. Planning Cycle — heartbeat.py

| Property | Value |
|----------|-------|
| **File** | `heartbeat.py` L1305–1450 |
| **Trigger class** | (would be `plan_lifecycle`) |
| **Event types** | `plan_created` |
| **Data available** | plan title, vault/memory persistence outcome |
| **Current behavior** | Prompt-delegated |
| **Triggerability** | Not feasible |
| **Priority** | P3 |
| **Blocker** | Same as research cycle |

### 8. Improvement Plan — planner/improvement_planner.py

| Property | Value |
|----------|-------|
| **File** | `planner/improvement_planner.py` L249–296 |
| **Trigger class** | (would be `plan_lifecycle`) |
| **Event types** | `plan_revised` |
| **Data available** | improvement_id, findings, goals, result status |
| **Current behavior** | Persists to STATE/improvement_runs/ |
| **Triggerability** | High — clear persist point, data available |
| **Priority** | P2 |
| **Blocker** | None — deferred to reduce Phase 3 scope |

### 9. Daily Summary — scripts/daily_summary.py

| Property | Value |
|----------|-------|
| **File** | `scripts/daily_summary.py` L20–79 |
| **Trigger class** | (would be `session_boundary`) |
| **Event types** | `session_end` |
| **Current behavior** | Generates a task file for the watcher to execute |
| **Triggerability** | Low — output is a task, not a direct event |
| **Priority** | P3 |
| **Blocker** | Prompt-delegated output |

### 10. Telegram Messages — telegram_bot.py

| Property | Value |
|----------|-------|
| **File** | `telegram_bot.py` L1229 |
| **Trigger class** | (would be `operator_decision`) |
| **Event types** | `decision_made`, `user_preference` |
| **Current behavior** | Logs slog.event for messages |
| **Triggerability** | Medium — could detect operator instructions |
| **Priority** | P2 |
| **Blocker** | Needs intent classification to distinguish commands from chat |

---

## Summary

| Status | Count | Sources |
|--------|-------|---------|
| **WIRED (Phase 3)** | 4 | task_completed, task_failed, plan_outcome, heartbeat_cycle |
| Identified P2 | 3 | session_end, improvement_plan, telegram_decision |
| Prompt-delegated P3 | 3 | research_cycle, planning_cycle, daily_summary |
| **Total identified** | **10** | |

---

## Prompt-Delegated Sources (Cannot Wire from Python)

These sources operate inside Claude subprocess prompts. The subprocess executes
MCP tool calls (upsert_memory, vault_write) that are embedded in the prompt text,
not callable from the Python process.

Until NovaCore has an SDK-level integration or post-subprocess hook, these
sources cannot participate in the automatic trigger pipeline. They are
documented honestly as enforcement gaps.

| Source | File | Current Write Target |
|--------|------|---------------------|
| Heartbeat research cycle | heartbeat.py | Fusion Memory + Obsidian Vault |
| Heartbeat planning cycle | heartbeat.py | Fusion Memory + Obsidian Vault |
| Daily summary output | scripts/daily_summary.py | Fusion Memory + Obsidian Vault |
| Watcher dispatch prompt | watcher.py | Fusion Memory (in worker prompt) |
