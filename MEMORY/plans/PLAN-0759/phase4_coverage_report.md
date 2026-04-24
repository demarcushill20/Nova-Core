# PLAN-0759 Phase 4 — session_id Coverage Gate Report

- **Measured at**: 2026-04-15T13:48:53.051372+00:00
- **Neo4j URI**: `bolt://localhost:7687`
- **Database**: `neo4j`
- **Cutoff window**: last 30 days
- **Cutoff ISO**: 2026-03-16T13:48:53.043925+00:00
- **Threshold**: 50.0%
- **Gate**: **PASS**

## All-time `:base` coverage

- Total `:base` nodes: **829**
- With `session_id`: **515**
- Without `session_id`: **314**
- Coverage: **62.12%**
- Gate (all-time): **PASS**

## Recent `:base` coverage

- Cutoff: last 30 days (2026-03-16T13:48:53.043925+00:00)
- Recent total: **584**
- Recent with `session_id`: **386**
- Recent coverage: **66.1%**
- Gate (recent): **PASS**

## Decision

The `session_id` coverage on live `:base` nodes meets the
Phase 4 gate threshold of 50.0% for both
the all-time population and the recent window. Sprint 11 may
proceed to wire the `TemporalLinker` hook into
`MemoryService.perform_upsert()` under the
`ASSOC_TEMPORAL_WRITE_ENABLED` flag, land the integration
tests, and record the latency baseline.
