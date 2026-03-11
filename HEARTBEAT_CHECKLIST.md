# Heartbeat Checklist

Run through this checklist each heartbeat cycle. For each section, use your judgment
to decide if action is needed RIGHT NOW. If nothing needs attention, respond
with exactly: HEARTBEAT_OK

## Research & Discovery Pipeline
- Is the task queue empty with no pending research? If so, inject a new research task.
  Good research topics: new MCP servers, CLI tools for agents, autonomous agent techniques,
  self-improvement patterns, code quality tools, monitoring tools, security practices.
- Has a research task completed recently? Check OUTPUT/ for new findings that should
  feed into an updated plan.
- Are research findings being saved to BOTH memory systems? Check that recent outputs
  mention Fusion Memory AND Obsidian Vault writes.

## Planning & Revision
- Is there a current enhancement plan in the vault/memory? If not, create a planning task.
- Has new research completed since the last plan revision? If so, inject a task to
  revise the plan with new findings.
- Are there quick-win items from the plan that haven't been turned into executable tasks yet?

## Task Queue Health
- Any TASKS/*.md files older than 30 minutes still pending (not picked up by watcher)?
- Any .inprogress files older than 20 minutes (stuck worker)?
- Any .failed files from the last 2 hours? If research failed, retry with different approach.
- Is the queue balanced? Should be a mix of research, planning, and implementation tasks.

## Memory & Knowledge
- Are both memory systems healthy? (Fusion Memory + Obsidian Vault)
- Have recent task outputs been captured as learning artifacts?
- Are goals in STATE/goals.json still current and progressing?

## System Health
- Are all services healthy? (pre-checked — see system state below)
- Any disk usage concerns? (pre-checked — see system state below)
- Any unusual failure rates in metrics?
- Is the Google Workspace token valid? (pre-checked — see google_workspace below)
- Is the latest backup recent (< 26 hours)? (pre-checked — see backup below)
- Are there ruff lint violations? If high count, consider injecting a cleanup task.
- Are log files growing too large? Check log_sizes for files > 50MB needing rotation.
- Is STATE/ bloated? If state_bloat fails, inject a cleanup task to prune old files.
- pip-audit runs on the 1st and 15th — check results if today is an audit day.

## Idle Detection
- If no tasks have run in the last 2 hours AND it's active hours, inject a research task.
  Nova-core should always be learning something when idle.
- Don't inject tasks if there are already 3+ pending — let the queue drain first.

## Response Format
If action is needed, respond with a JSON array of actions:
```json
[
  {"type": "notify", "message": "Brief message for the operator"},
  {"type": "task", "title": "short_descriptive_name", "body": "Full task description with instructions to save findings to both Fusion Memory (upsert_memory) and Obsidian Vault (vault_write)"}
]
```

Task body guidelines:
- Always include instructions to save to BOTH memory systems
- Include specific search queries or research directions
- Reference prior OUTPUT/ files when building on previous work
- End with standard CONTRACT block requirement

If nothing needs attention: respond with exactly HEARTBEAT_OK
