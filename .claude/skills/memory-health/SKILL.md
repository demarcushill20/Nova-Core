---
name: memory-health
description: "Diagnose Fusion Memory system health, verify backend connectivity, and troubleshoot failures. Auto-invoked when memory operations fail or when the user asks about memory system status."
disable-model-invocation: false
allowed-tools:
  - mcp__nova-memory__check_health
  - mcp__nova-memory__get_recent_events
  - mcp__nova-memory__get_last_checkpoint
  - mcp__nova-memory__query_memory
tool_doctrine:
  diagnostics:
    workflow:
      - check_health_first
      - probe_each_backend
      - report_component_status
      - suggest_remediation
output_contract:
  required:
    - summary
    - component_status
    - diagnosis
    - remediation
    - confidence
---

# Memory Health

## When to use

- A memory operation (store, recall, checkpoint) failed or returned errors
- The user asks "is memory working?", "check memory health", "why did that fail?"
- Starting a session and want to confirm all backends are online
- After infrastructure changes (Docker restart, config changes, network issues)
- Debugging unexpected empty results from queries

## When NOT to use

- Normal memory operations are succeeding — don't proactively health-check every time
- Investigating non-memory issues (file system, git, etc.)

## Workflow

### Step 1 — Run Health Check

```
Tool: mcp__nova-memory__check_health
Args: {}
```

Expected healthy response:
```json
{
  "status": "ok",
  "pinecone": "ok",
  "graph": "ok",
  "reranker": "loaded",
  "redis": "connected",
  "redis_timeline": "active"
}
```

### Step 2 — Interpret Component Status

| Component | Status | Meaning | Impact |
|-----------|--------|---------|--------|
| `pinecone` | `ok` | Vector store connected | Semantic search works |
| `pinecone` | `error: ...` | Vector store down | No semantic queries, no upserts |
| `graph` | `ok` | Neo4j connected | Graph queries, session linking work |
| `graph` | `error: ...` | Neo4j down | No graph search, no session nodes |
| `reranker` | `loaded` | Cross-encoder model loaded | Result reranking active |
| `reranker` | `disabled/failed` | Model not loaded | Results returned without reranking |
| `redis` | `connected` | Redis connected | Fast timeline queries, Redis INCR |
| `redis` | `disabled/fallback` | Redis unavailable | Falls back to file-based counter + Pinecone temporal |
| `redis_timeline` | `active` | Sorted set timeline operational | O(log N) recency queries |
| `redis_timeline` | `inactive` | Timeline not initialized | Falls back to Pinecone dummy-vector queries |

### Step 3 — Probe Failing Components

If a component is down, verify with a targeted probe:

**Pinecone down** — test with a minimal query:
```
Tool: mcp__nova-memory__query_memory
Args: {"query": "health check probe", "top_k_final": 1}
```

**Neo4j down** — check Docker:
```bash
docker ps --filter name=nova_neo4j_db
docker logs nova_neo4j_db --tail 20
```

**Redis down** — check Docker:
```bash
docker ps --filter name=nova_redis
docker logs nova_redis --tail 20
```

### Step 4 — Diagnose and Remediate

Common issues and fixes:

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| All backends down | MCP server not running | Restart Claude Code session |
| Pinecone error | API key expired, quota exceeded | Check OpenAI/Pinecone billing |
| Neo4j error | Container stopped | `docker compose up -d neo4j` in MCP repo |
| Redis error | Container stopped | `docker compose up -d redis` in MCP repo |
| Reranker failed | Model download failed | Check HuggingFace connectivity |
| Empty query results | Index empty or wrong index | Check `PINECONE_INDEX` in .env |
| Upsert fails with encoding error | Old urllib3 | `pip3 install --upgrade urllib3` |
| Permission denied on seq file | Wrong EVENT_SEQ_FILE path | Check .env `EVENT_SEQ_FILE` setting |

### Step 5 — Verify Recovery

After applying a fix, re-run health check and test a simple operation:

```
Tool: mcp__nova-memory__check_health
Args: {}
```

Then test a write + read cycle:
```
Tool: mcp__nova-memory__get_recent_events
Args: {"n": 1}
```

## Tool Usage Rules

- **Diagnostic only.** This skill does not modify data. It reads and probes.
- **Health check is cheap.** It's safe to run frequently — it only pings backends.
- **Don't retry blindly.** If a component is down, diagnose the root cause before retrying operations.
- **Report component-level status.** Users need to know which specific backend failed, not just "memory is broken".
- **Infrastructure fixes may need shell.** Docker restart commands require shell-ops skill — coordinate if needed.

## Failure Handling

- If `check_health` itself fails (MCP server unreachable): the MCP server process crashed or isn't started. Restart Claude Code.
- If health returns mixed status (some ok, some error): the system is partially degraded. Report which operations are available and which are blocked.
- If all components show ok but operations still fail: the issue may be data-level (empty index, wrong namespace) rather than infrastructure.

## Outputs / Contract

```
## Memory Health Contract
summary: <overall system status in one sentence>
component_status:
  pinecone: <ok | error: detail>
  neo4j: <ok | error: detail>
  redis: <ok | error: detail>
  redis_timeline: <active | inactive>
  reranker: <loaded | failed>
diagnosis: <root cause if unhealthy, "all systems nominal" if healthy>
remediation: <fix applied or recommended, "none needed" if healthy>
verification: <health check result after fix, or "pre-check only">
confidence: <high | medium | low>
```

## Examples

### Example 1: Healthy system

```
Tool: mcp__nova-memory__check_health
→ {"status": "ok", "pinecone": "ok", "graph": "ok", "reranker": "loaded", "redis": "connected", "redis_timeline": "active"}
```

**Contract**:
```
summary: All Fusion Memory backends operational
component_status:
  pinecone: ok
  neo4j: ok
  redis: connected
  redis_timeline: active
  reranker: loaded
diagnosis: all systems nominal
remediation: none needed
verification: health check passed
confidence: high
```

### Example 2: Redis down, partial degradation

```
Tool: mcp__nova-memory__check_health
→ {"status": "ok", "pinecone": "ok", "graph": "ok", "reranker": "loaded", "redis": "disabled/fallback", "redis_timeline": "inactive"}
```

**Contract**:
```
summary: Fusion Memory operational with degraded performance — Redis offline, using file-based fallback
component_status:
  pinecone: ok
  neo4j: ok
  redis: disabled/fallback
  redis_timeline: inactive
  reranker: loaded
diagnosis: Redis container not running. SequenceService using file-based counter. Temporal queries falling back to Pinecone dummy-vector over-fetch (slower but functional).
remediation: Run `docker compose up -d redis` in /home/nova/Nova_AI_Fusion_Memory_MCP to restore Redis. All operations remain functional via fallback paths.
verification: pre-check only — Redis restart recommended but not blocking
confidence: high — degradation is well-defined, all operations have fallback paths
```
