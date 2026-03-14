# Memory Surface Inventory

Phase 0 deliverable — exhaustive map of every place NovaCore reads, writes, promotes, or injects memory.

Generated: 2026-03-13
Source: Codebase audit of all .py files, SKILL definitions, and prompt templates.

---

## 1. WRITE TOUCHPOINTS

### 1.1 Fusion Memory MCP (upsert_memory, create_checkpoint)

These are NOT direct Python API calls. They are instructions embedded in prompts that tell
the spawned Claude CLI process to call MCP tools at runtime.

| Location | Prompt/Function | What is written | Trigger | Validates before write | Bypasses future router |
|----------|-----------------|-----------------|---------|----------------------|----------------------|
| heartbeat.py ~L995-1028 | _build_research_prompt() | Research summary (category: research) | Heartbeat research cycle | No (prompt instruction) | YES — direct MCP call |
| heartbeat.py ~L1322-1355 | _build_planning_prompt() | Enhancement plan (category: decision) | Heartbeat planning cycle | No (prompt instruction) | YES — direct MCP call |
| watcher.py L216-223 | DISPATCH_PROMPT_TEMPLATE | Research/planning task results | Task dispatch (research tasks) | No (prompt instruction) | YES — direct MCP call |
| scripts/daily_summary.py L46-74 | Task template | Daily summary (category: daily_summary) | Cron-triggered daily task | No (prompt instruction) | YES — direct MCP call |

**Note**: All Fusion Memory writes are delegated to Claude subprocess execution via prompt instructions.
There is no Python-level validation or gating before these writes occur. The MCP server itself
handles validation on the Fusion Memory side.

### 1.2 Obsidian Vault MCP (vault_write, vault_update)

| Location | Function | What is written | Target folder | Trigger | Validates | Bypasses future router |
|----------|----------|-----------------|---------------|---------|-----------|----------------------|
| planner/workflow_promoter.py L335 | attempt_promotion() | Workflow-learning notes | 30-workflow-learnings/ | Orchestrator task completion (grade A/B, 2+ signals) | YES — vault_validate() then vault_write() | YES — direct vault call |
| planner/pattern_promoter.py L443 | attempt_pattern_promotion() | Agent-pattern notes | 20-agent-patterns/ | After successful workflow-learning promotion (2+ converging learnings) | YES — vault_validate() then vault_write() | YES — direct vault call |
| heartbeat.py ~L995 | _build_research_prompt() (prompt instruction) | Research summaries | 40-research/ | Heartbeat research cycle | No (prompt instruction) | YES |
| heartbeat.py ~L1322 | _build_planning_prompt() (prompt instruction) | Implementation plans | 00-inbox/ | Heartbeat planning cycle | No (prompt instruction) | YES |
| scripts/daily_summary.py L46 | Task template (prompt instruction) | Daily summaries | 00-inbox/ | Daily summary task | No (prompt instruction) | YES |

**Vault write pipeline** (tools/mcp_vault_server.py vault_write L1041-1150):
9-step fail-closed validation: feature flag → path format → path safety → folder restriction →
no-overwrite → frontmatter present → source=nova-core-memory → schema validation → size ≤ 34KB →
sensitive content scan → rate limit (10/5min).

### 1.3 File-Based Memory (MEMORY/ directory)

| Location | Function | What is written | Target path | Trigger | Validates | Bypasses future router |
|----------|----------|-----------------|-------------|---------|-----------|----------------------|
| agents/memory_engine.py L175-212 | write_memory_artifact() | JSON memory artifacts | MEMORY/workflow_learnings/*.json | capture_workflow_memory() or capture_direct_task_memory() | YES — validate_memory_artifact() (strict) | YES — direct file write |
| agents/memory_engine.py L477-510 | capture_workflow_memory() | Compacted workflow summaries | MEMORY/workflow_learnings/ | Orchestrator workflow completion | YES (via write_memory_artifact) | YES |
| agents/memory_engine.py L512-582 | capture_direct_task_memory() | Direct worker task artifacts | MEMORY/workflow_learnings/ | watcher.py L1120 — task completion | YES (via write_memory_artifact) | YES |

Validation: required fields, enum checks, artifact ID format regex, size ≤ 32KB, append-only (no overwrites).

### 1.4 State Persistence (STATE/ directory)

| Location | Function | What is written | Target path | Trigger | Validates | Bypasses future router |
|----------|----------|-----------------|-------------|---------|-----------|----------------------|
| agents/session_manager.py L242-250 | _persist() | Session state JSON | STATE/sessions/{id}.json | Task start/completion, session create | NO (best-effort) | N/A (runtime state) |
| agents/blackboard.py L117-255 | Multiple write methods | Agent state, delegations, workflows | STATE/agents/, STATE/delegations/, STATE/workflows/ | Multi-agent orchestration | NO (caller validates) | N/A (runtime state) |
| watcher.py L351-378 | _update_metrics() | Task execution metrics | STATE/metrics.json | Task completion | NO (best-effort) | N/A (runtime state) |
| telegram/working_memory.py L46-52 | record() | Active task context | STATE/working_memory.json | Task delegation from Telegram | Dataclass validation | N/A (runtime state) |
| telegram/recent_completions.py L47-73 | record_completion() | Recently completed tasks | STATE/recent_completions.json | Task completion | Field truncation | N/A (runtime state) |
| telegram/recap.py L50-75 | save_recap() | Conversation recap | STATE/conversation_recap/{chat_id}.json | Conversation update | NO | N/A (runtime state) |
| telegram/goals.py L38-87 | Various mutators | Active goals | STATE/goals.json | User goal commands | NO | N/A (runtime state) |

### 1.5 Audit & Structured Logging

| Location | Function | What is written | Target path | Trigger | Validates |
|----------|----------|-----------------|-------------|---------|-----------|
| utils/audit_log.py L110-115 | _write_event() | Hash-chained JSONL audit events | LOGS/audit/audit_YYYY-MM-DD.jsonl | Any security/auth event | SHA-256 chain |
| utils/structured_log.py L40-65 | event() | Structured JSON log events | LOGS/structured.jsonl | Instrumented code paths | NO |
| planner/pattern_feedback.py L216-241 | log_pattern_trace() | Pattern usefulness traces | LOGS/pattern_feedback.jsonl | Orchestrator post-execution | Enum validation |

---

## 2. READ TOUCHPOINTS

### 2.1 Fusion Memory MCP (query_memory, get_last_checkpoint, etc.)

| Location | Function | What is read | Trigger | How results are used |
|----------|----------|--------------|---------|---------------------|
| .claude/skills/memory-recall/SKILL.md | Skill definition | Semantic queries, temporal events, checkpoints | User invokes /memory-recall | Synthesized into response with citations |
| .claude/skills/memory-unified-recall/SKILL.md | Skill definition | Cross-system routed queries | Memory-related user queries | Merged results with source attribution |
| heartbeat.py (prompt) | _build_research_prompt() | Prior research context | Research cycle | Deduplication of research topics |
| heartbeat.py (prompt) | _build_planning_prompt() | Prior plans | Planning cycle | Plan evolution/revision |
| scripts/daily_summary.py | Task template | Recent events, queries | Daily summary generation | Synthesized into daily report |

### 2.2 Obsidian Vault MCP (vault_read, vault_search)

| Location | Function | What is read | Trigger | How results are used |
|----------|----------|--------------|---------|---------------------|
| planner/vault_context.py L129-194 | retrieve_vault_context() | Keyword-matched notes (max 3, 2KB) | Task eligibility (research/code_impl/code_review) | Injected as advisory context in planner |
| planner/pattern_retriever.py L121-413 | search_agent_patterns(), read_pattern_guidance() | Agent patterns from 20-agent-patterns/ (max 2, 1.5KB) | Task eligibility (research/code_impl/code_review) | Guidance sections injected into planner context |
| tools/mcp_vault_server.py L614-900+ | vault_list(), vault_read(), vault_search(), vault_frontmatter() | Any vault notes | MCP tool calls from Claude | Returned as JSON to caller |

### 2.3 File-Based Memory (MEMORY/ directory)

| Location | Function | What is read | Trigger | How results are used |
|----------|----------|--------------|---------|---------------------|
| agents/memory_engine.py L335-440 | _load_artifacts(), retrieve_related_patterns() | JSON artifacts from MEMORY/agent_patterns/ and MEMORY/workflow_learnings/ | watcher.py L858-866 before task dispatch | Ranked by relevance, formatted into advisory context |

### 2.4 State Files (STATE/ directory)

| Location | Function | What is read | Trigger | How results are used |
|----------|----------|--------------|---------|---------------------|
| agents/session_manager.py L190-239 | build_context_injection() | Recent task summaries from sessions | Task dispatch | Injected into worker prompt (max 3KB) |
| telegram/working_memory.py L148-158 | _load() | Active tasks | Startup | Formatted for CEO Nova conversation context |
| telegram/recent_completions.py L28-91 | _load(), get_recent() | Recently completed tasks (4hr retention) | Session reconstruction | "RECENTLY COMPLETED TASKS" block in prompt |
| telegram/recap.py L30-96 | load_recap(), format_for_context() | Last conversation messages | Session restart | "LAST CONVERSATION RECAP" block in prompt |
| telegram/goals.py L19-121 | _load(), format_goals_for_context() | Active goals | Conversation injection | "ACTIVE GOALS" block in prompt |
| heartbeat.py L29-49 | Health check functions | Service status, disk, task queue | Timer trigger | Written to HEARTBEAT.md |

---

## 3. PROMOTION TOUCHPOINTS

| Source | Target | Location | Function | Trigger | Validation |
|--------|--------|----------|----------|---------|------------|
| Workflow completion → MEMORY/workflow_learnings/ | File-based JSON | agents/memory_engine.py L477 | capture_workflow_memory() | Orchestrator completion | validate_memory_artifact() |
| Direct task completion → MEMORY/workflow_learnings/ | File-based JSON | agents/memory_engine.py L512 | capture_direct_task_memory() | watcher.py L1120 | validate_memory_artifact() |
| Workflow summary → 30-workflow-learnings/ | Obsidian Vault | planner/workflow_promoter.py L335 | attempt_promotion() | orchestrator_adapter.py L856 | vault_validate() |
| Workflow learnings → 20-agent-patterns/ | Obsidian Vault | planner/pattern_promoter.py L443 | attempt_pattern_promotion() | orchestrator_adapter.py L883 | vault_validate() |

**Promotion chain**: Task completion → MEMORY/ artifact → vault workflow-learning → vault agent-pattern.
Each step has independent validation. Failure at any step is fail-open (does not block execution).

---

## 4. MEMORY INJECTION INTO PROMPTS

| Location | Function | What is injected | Size bound | Source |
|----------|----------|------------------|------------|--------|
| watcher.py L891-894 | context_prefix construction | Session context + memory patterns | 3KB (session) + unbounded (memory) | session_manager + memory_engine |
| agents/session_manager.py L190-239 | build_context_injection() | Recent completed task summaries | MAX_CONTEXT_INJECTION_BYTES = 3,072 | STATE/sessions/ |
| agents/memory_engine.py L443-470 | format_retrieval_for_planner() | Related prior patterns (max 5) | No explicit byte cap | MEMORY/*.json |
| planner/vault_context.py L165 | retrieve_vault_context() | Vault notes by keyword | 2KB formatted output | Obsidian vault |
| planner/pattern_retriever.py L277-321 | format_pattern_guidance() | Agent pattern guidance | MAX_PATTERN_CONTEXT_SIZE = 1,536 bytes | Obsidian vault 20-agent-patterns/ |
| telegram/working_memory.py L113-123 | format_for_context() | Active delegated tasks | No explicit byte cap | STATE/working_memory.json |
| telegram/recent_completions.py L106-128 | format_for_context() | Recently completed tasks | No explicit byte cap | STATE/recent_completions.json |
| telegram/recap.py L77-96 | format_for_context() | Conversation recap | No explicit byte cap | STATE/conversation_recap/ |
| telegram/goals.py L108-121 | format_goals_for_context() | Active goals | No explicit byte cap | STATE/goals.json |

---

## 5. KEY OBSERVATIONS

1. **All Fusion Memory writes are prompt-delegated** — no direct Python SDK calls. Validation
   depends entirely on the MCP server and the Claude subprocess's behavior.

2. **All vault writes go through the MCP server pipeline** — 9-step fail-closed validation.
   This is the strongest validation in the system.

3. **File-based memory (MEMORY/*.json) has strict validation** — required fields, enum checks,
   size bounds, artifact ID format, append-only.

4. **State persistence (STATE/) has minimal validation** — best-effort writes, no schema enforcement.
   This is appropriate since STATE/ is runtime-only and not durable knowledge.

5. **Every write path bypasses the future router** — there is currently no unified memory router.
   Each component calls its target store directly.

6. **Prompt injection context has inconsistent size bounding** — session context (3KB) and
   pattern guidance (1.5KB) are bounded. Working memory, recent completions, recap, and goals
   context blocks have no explicit byte caps.

7. **Promotion is multi-stage and fail-open** — task → MEMORY/ artifact → vault workflow-learning
   → vault agent-pattern. Each stage validates independently. Failure does not cascade.

8. **No automatic triggers exist for diary generation, ADR surfacing, or pattern consolidation.**
   These are mentioned in skills but require explicit invocation.
