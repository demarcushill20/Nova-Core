# Vault Tag Taxonomy

Canonical tag dimensions for all Nova-Core vault notes.

## Required Dimensions (at least one each)

### `#type/*` — Note type (enforced by schema)
- `#type/pattern` — agent-pattern
- `#type/learning` — workflow-learning
- `#type/research` — research-summary
- `#type/plan` — implementation-plan
- `#type/debugging` — debugging-guide
- `#type/inbox` — inbox
- `#type/adr` — architecture decision record
- `#type/moc` — map of content

### `#domain/*` — Knowledge domain
- `#domain/novatrade` — trading strategies, backtesting, MT5, execution
- `#domain/autonomy` — decision engine, heartbeat, guardrails
- `#domain/memory` — Fusion Memory, vault, retrieval, Pinecone, Neo4j
- `#domain/infrastructure` — systemd, circuit breakers, self-healing, deploy
- `#domain/agents` — agent spawner, orchestrator, multi-agent runtime
- `#domain/risk` — risk engine, gates, filters, drawdown, exposure
- `#domain/research-methods` — search patterns, evaluation, benchmarks
- `#domain/operations` — general ops, weekly reviews, session diaries

### `#project/*` — Active project
- `#project/novatrade`
- `#project/fusion-memory`
- `#project/nova-core`

## Recommended Dimensions (when applicable)

### `#confidence/*`
- `#confidence/high`
- `#confidence/medium`
- `#confidence/low`

### `#status/*`
- `#status/active`
- `#status/superseded`
- `#status/draft`
- `#status/archived`
- `#status/stale`

### `#agent/*` — Agent role
- `#agent/research`
- `#agent/coder`
- `#agent/critic`
- `#agent/verifier`
- `#agent/planner`
- `#agent/memory`

### `#action/*` — Operator action needed
- `#action/review`
- `#action/move-to-diary`
- `#action/promote-to-adr`
- `#action/move-to-meta`

## Domain Inference Rules

When writing a note, infer the domain from content keywords:

| Keywords | Domain |
|----------|--------|
| trade, strategy, backtest, IRB, MT5, execution | novatrade |
| autonomy, heartbeat, decision engine, guardrail | autonomy |
| memory, fusion, pinecone, neo4j, vault, recall | memory |
| systemd, circuit breaker, self-heal, deploy, nginx | infrastructure |
| agent, spawner, orchestrator, multi-agent | agents |
| risk, gate, filter, drawdown, exposure | risk |
| Default | operations |

## Tag Budget

Current skills use 3-4 tags. Adding `#domain/*` and `#project/*` brings the total to 5-6, well under the 10-tag limit.

Typical tag set: `#type/*` + `#confidence/*` + `#status/*` + `#domain/*` + `#project/*` + optional `#agent/*` = 5-6 tags.
