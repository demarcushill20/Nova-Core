---
name: langfuse-observability
description: >-
  LLM observability with Langfuse — trace calls, track costs, version prompts,
  detect drift. Use when analyzing token spend, debugging LLM responses,
  reviewing prompt performance, or when the user asks about costs, usage,
  or observability. Also auto-invoke after noticing repeated failures or
  unexpected LLM behavior.
argument-hint: "[action: costs|traces|prompts|health]"
---

# Langfuse LLM Observability

Track and analyze LLM usage across NovaCore using Langfuse Cloud.

## Setup Status

Langfuse Python SDK is installed. To activate:
1. Sign up at https://cloud.langfuse.com (free tier: 50k observations/month)
2. Create project "nova-core"
3. Add keys to `/etc/novacore/langfuse.env`:
   ```
   LANGFUSE_PUBLIC_KEY=pk-lf-...
   LANGFUSE_SECRET_KEY=sk-lf-...
   LANGFUSE_HOST=https://cloud.langfuse.com
   ```

## Integration Points

The `utils/langfuse_tracing.py` module provides:
- `trace_llm_call(name, input, output, model, metadata)` — record a generation
- `trace_task(task_name, metadata)` — create a task-level trace span
- `flush()` — ensure all events are sent
- Graceful no-op when credentials are not configured

## What to Instrument

Priority order for adding tracing:
1. **Heartbeat agent calls** — track per-cycle cost and latency
2. **Research/planning cycles** — most expensive, need cost visibility
3. **Watcher task executions** — per-task cost attribution
4. **Telegram LLM calls** — user-facing latency tracking

## Observability Queries

When analyzing performance, check:
- Cost per task type (research vs. planning vs. simple)
- Token usage trends over time
- Latency distribution (TTFT, total)
- Error rates and empty response frequency
- Prompt version effectiveness (A/B comparisons)

## Dashboard

Access at: https://cloud.langfuse.com (or self-hosted URL when migrated)
