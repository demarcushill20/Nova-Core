---
name: n8n-workflows
description: >-
  Manage n8n automation workflows — list, create, activate, execute, and monitor.
  Auto-invoked when the user mentions workflows, automation, n8n, or wants to
  connect external services (Slack, webhooks, APIs, scheduled jobs). Also use
  when automating repetitive multi-step processes.
argument-hint: "[action: list|create|run|status] [workflow-name]"
tools:
  - mcp__n8n__workflow_list
  - mcp__n8n__workflow_get
  - mcp__n8n__workflow_create
  - mcp__n8n__workflow_update
  - mcp__n8n__workflow_activate
  - mcp__n8n__workflow_deactivate
  - mcp__n8n__workflow_delete
  - mcp__n8n__execution_run
  - mcp__n8n__execution_get
  - mcp__n8n__execution_list
  - mcp__n8n__execution_stop
  - mcp__n8n__run_webhook
---

# n8n Workflow Automation

Manage n8n workflows for connecting external services, scheduling jobs, and automating multi-step processes.

## Workflow

1. **Discover** — `workflow_list` to see existing workflows
2. **Inspect** — `workflow_get` for workflow details and node configuration
3. **Create/Update** — build workflow JSON with nodes and connections
4. **Activate** — `workflow_activate` to enable trigger-based execution
5. **Execute** — `execution_run` for manual runs, `run_webhook` for webhook triggers
6. **Monitor** — `execution_list` + `execution_get` for status and results

## Common Patterns

- **Scheduled job**: Cron trigger node -> action nodes -> result
- **Webhook listener**: Webhook trigger -> transform -> action
- **Service bridge**: Watch trigger (email, Slack) -> process -> respond
- **Data pipeline**: Fetch -> transform -> store -> notify

## n8n Instance

- URL: `http://localhost:5678`
- API: `http://localhost:5678/api/v1`
- Docker container: `n8n`

## Safety

- List/get operations are always safe
- Activate/deactivate changes live behavior — confirm intent
- Delete requires explicit confirmation
- Never store secrets in workflow JSON — use n8n credentials store
