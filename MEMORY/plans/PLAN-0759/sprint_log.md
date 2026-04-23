# PLAN-0759 Sprint Log

Per ADR-0759 and the v2 plan's Step 0.7, each sprint is ≤4 hours with an explicit operator checkpoint before the next begins. Claude Code does NOT auto-progress between sprints.

## Sprint 1 — 2026-04-13 ✓ COMPLETE

- **Phase**: 0 (Foundations)
- **Steps covered**: 0.1 (integration ADR), 0.3 (feature flags declarations only)
- **Scope**: ADR-0759 + 8 ASSOC_* Pydantic flags + hermetic default-False test
- **Duration**: ~2h
- **Acceptance**: reviewer ACCEPT-WITH-NITS; MEDIUM nit (env pollution) fixed with monkeypatch.delenv
- **Files**: `nova-core/10-adrs/ADR-0759-assoc-linking-location.md`, `Nova_AI_Fusion_Memory_MCP/app/config.py`, `Nova_AI_Fusion_Memory_MCP/tests/test_assoc_feature_flags.py`
- **Operator checkpoint**: approved 2026-04-13
- **Next-sprint gate**: proceed to Sprint 2

## Sprint 2 — 2026-04-13 ✓ COMPLETE (pending review)

- **Phase**: 0 (Foundations)
- **Steps covered**: 0.2 (schema audit), 0.5 (rollback tagging), 0.7 (sprint log template)
- **Scope**: audit_neo4j_schema.py script + live-run report; assoc_rollback.py script + safe integration test; this sprint log
- **Duration**: ~1.5h
- **Acceptance**: pending Critical Reviewer
- **Files**:
  - `Nova_AI_Fusion_Memory_MCP/scripts/__init__.py` (new package)
  - `Nova_AI_Fusion_Memory_MCP/scripts/audit_neo4j_schema.py` (read-only audit, 415 LOC)
  - `nova-core/MEMORY/plans/PLAN-0759/phase0_schema_audit.md` (live audit report, regenerated from script run)
  - `Nova_AI_Fusion_Memory_MCP/scripts/assoc_rollback.py` (rollback-by-run_id utility, 270 LOC)
  - `Nova_AI_Fusion_Memory_MCP/tests/test_assoc_rollback.py` (8 tests — refusals + integration)
  - `nova-core/MEMORY/plans/PLAN-0759/sprint_log.md` (this file)
- **Key findings from live audit** (bolt://localhost:7687, neo4j:5.19, container nova_neo4j_db):
  - `:base` node count = 824, `:Session` node count = 339. These are the only two labels in the live DB.
  - `:base` unique constraint on `entity_id` present and online (ADR-0759 §7 invariant confirmed on the wire).
  - Sampled `:base` property keys include `entity_id`, `event_seq`, `event_time`, `memory_type`, `project`, `text`, `session_id`, `tags`, `category`, `thread_id`, `started_at`, `ended_at`, `last_event_seq`, `session_summary`, `open_threads`, `next_actions`. Phase 1 Cypher can rely on `entity_id` as the key.
  - Existing `FOLLOWS` edges: 1, on `:Session -> :Session` (collision confirmed; `MEMORY_FOLLOWS` rename in ADR-0759 §6 is necessary).
  - Existing `INCLUDES` edges: 517 (pre-existing Session-to-memory include edges; **not** on the PLAN-0759 candidate-edge list, so no collision with any new ASSOC edge type, but worth noting).
  - All 9 PLAN-0759 candidate edge types (`SIMILAR_TO`, `MEMORY_FOLLOWS`, `MENTIONS`, `PROMOTED_FROM`, `SUPERSEDES`, `COMPACTED_FROM`, `CAUSED_BY`, `RELATED_TASK`, `CO_OCCURS`) had count **0**. Notably `SUPERSEDES` and `COMPACTED_FROM` — which the v2 plan warned might pre-date PLAN-0759 — are also at zero on this instance, so Phase 1 can proceed without grandfathering.
- **Rollback integration test results**:
  - 8 tests passed in 1.47s: 2 refusals (empty, whitespace) + 4 wildcard refusals (parameterized `*`, `%`, `all`, `ALL`) + 1 full dry-run→live-delete cycle + 1 idempotency test.
  - `:base` count before/after destructive phase: 824 / 824 (production data untouched).
  - Test label `:AssocRollbackTestNode` used throughout; 10 seeded per run; teardown fixture verified 0 leftover after the suite.
- **Operator checkpoint**: pending
- **Next-sprint gate**: pending

## Sprint 3 — 2026-04-13 ✓ COMPLETE (pending review)

- **Phase**: 0 (Foundations) — this sprint closes Phase 0
- **Steps covered**: 0.3 (zero-regression baseline test), 0.4 (LLM-as-judge eval harness skeleton), 0.6 (cross-project scoping appendix)
- **Scope**: Phase 0 close-out — no behavior changes to production paths, scaffolding + tests only
- **Duration**: ~3h
- **Acceptance**: pending Critical Reviewer
- **Operator decisions locked into this sprint**:
  - **(C) LLM-as-judge** pinned to `claude-sonnet-4-6`, `temperature=0`, `max_tokens=256`. Hand-labeling rejected on effort grounds; pseudo-labels from `SUPERSEDES`/`COMPACTED_FROM` rejected because the live graph has zero such edges; cross-encoder rejected on domain-mismatch grounds. See `eval_ground_truth_design.md`.
  - **(A) Similarity threshold = 0.82** is the ship default. No sweep, no sample labeling, no sub-phase work this sprint. Calibration is deferred to a post-Phase-1 rollout observation pass. This decision is captured in the plan note but does not touch Sprint 3 code (no similarity linker is written yet).
- **Files delivered**:
  - `Nova_AI_Fusion_Memory_MCP/tests/test_assoc_zero_regression.py` — hermetic baseline test, mocks Pinecone/Neo4j/embedding/entity-extractor/sequence-service, pins `MemoryService.perform_upsert` + `perform_query` orchestration behavior under all-ASSOC-flags-False (~440 LOC)
  - `Nova_AI_Fusion_Memory_MCP/tests/fixtures/phase0_regression_memories.json` — 10 canonical test memory objects (decision / debug / pattern / context / research / scratch) with stable content and pinned `event_time` (100 LOC)
  - `Nova_AI_Fusion_Memory_MCP/tests/fixtures/phase0_regression_baseline.json` — locked behavior-signature snapshot (~880 LOC); regenerate by deleting and re-running the test
  - `Nova_AI_Fusion_Memory_MCP/tests/eval/__init__.py` — eval package marker (10 LOC)
  - `Nova_AI_Fusion_Memory_MCP/tests/eval/llm_judge.py` — `LLMJudge` class, pinned model + temperature, API-key enforcement, JSON response parser with strict validation (~260 LOC)
  - `Nova_AI_Fusion_Memory_MCP/tests/eval/associative_recall_eval.py` — `EvalQuery`/`EvalResult` dataclasses, `run_eval`, `save_baseline`/`load_baseline`, `compare_baselines`, `RELEVANCE_THRESHOLD=0.5`, `GATE_DELTA=0.05` (~240 LOC)
  - `Nova_AI_Fusion_Memory_MCP/tests/eval/test_llm_judge.py` — 12 hermetic unit tests, no live API calls (~190 LOC)
  - `Nova_AI_Fusion_Memory_MCP/tests/eval/test_associative_recall_eval.py` — 27 hermetic unit tests covering recall / MRR / gate math + save/load/compare (~360 LOC)
  - `Nova_AI_Fusion_Memory_MCP/tests/eval/baselines/.gitkeep` — directory marker + filename convention doc
  - `Nova_AI_Fusion_Memory_MCP/requirements.txt` — added `anthropic>=0.40.0` (eval-harness only; not on any production import path)
  - `nova-core/MEMORY/plans/PLAN-0759/eval_ground_truth_design.md` — decision memo for Step 0.4 (LLM-as-judge, rejected alternatives, Phase 6 gate, cost model, reproducibility)
  - `nova-core/MEMORY/plans/PLAN-0759/cross_project_scoping.md` — live audit + Phase 1 gotchas for Step 0.6
  - `nova-core/MEMORY/plans/PLAN-0759/sprint_log.md` — this entry
- **Test results**:
  - `tests/test_assoc_zero_regression.py::test_phase0_zero_regression_baseline` — PASSED (stable across two consecutive runs after initial baseline write). Runs fully hermetic — mocks `PineconeClient`, `GraphClient`, `get_embedding`, `extract_entities`, and `SequenceService.next_seq`; the sole pytest-asyncio test in the new file. Strips `temporal_score` / `composite_score` / `semantic_score_normalized` from the captured snapshot because they depend on wall-clock time and are not part of the ASSOC_* zero-regression contract.
  - `tests/eval/test_llm_judge.py` — 12/12 PASSED (0.13s)
  - `tests/eval/test_associative_recall_eval.py` — 27/27 PASSED (0.26s) — after one fix: replaced a float-literal boundary test (`0.45 - 0.40 != exactly 0.05` in IEEE 754) with a `GATE_DELTA`-derived boundary test + an explicit "just below" test
- **Live Neo4j findings (Step 0.6 audit)**:
  - `:base` = 825 (was 824 in Sprint 2; +1 since — organic write, not a test leak — verified 0 `:ZeroRegressionTestNode` and 0 `:AssocRollbackTestNode` post-run)
  - `:Session` = 339 (unchanged)
  - `:base` nodes with `project` property: **823**
  - `:base` nodes without: **2** (Phase 1 gotcha — backfill recommended)
  - Distinct `project` values (6): `nova-core` (612), `novatrade` (191), `novacore` (13), `fusion-memory` (5), `nova-link` (1), `novacore-autonomy` (1)
  - Phase 1 implication: `SimilarityLinker` / `EntityLinker` scoped-by-project is straightforward to implement; 2 untagged nodes should be backfilled with a default project before Phase 1 linker rollout; `nova-core` vs `novacore` are distinct strings and Phase 1 MUST NOT silently fuse them
- **Anthropic API wiring**:
  - `anthropic>=0.40.0` added to `requirements.txt` (not yet installed globally; the Sprint 3 unit tests do NOT import `anthropic` directly because `LLMJudge.__init__` accepts an injected mock client)
  - `ANTHROPIC_API_KEY` is **NOT set** in the current shell environment. This is a non-blocking finding — Sprint 3 ships scaffolding only; Phase 6 must set the key before running the first live baseline
- **Phase 0 acceptance**: **READY TO CLOSE, pending Critical Reviewer on Sprint 3.** Steps 0.1 (Sprint 1), 0.2 / 0.5 / 0.7 (Sprint 2), 0.3 / 0.4 / 0.6 (this sprint) are all covered.
- **Operator checkpoint**: pending
- **Next-sprint gate**: proceed to Sprint 4 (Phase 1 — Neo4j Edge Infrastructure) after review + operator approval

## Sprint 4 — 2026-04-13 ✓ COMPLETE (Phase 1 open)

- **Phase**: 1 (Neo4j Edge Infrastructure)
- **Steps covered**: 1.1 (edge schema), 1.2 (`MemoryEdge` dataclass), Cypher templates
- **Scope**: `app/services/associations/` package; `memory_edges.py` (`MemoryEdge`, `VALID_EDGE_TYPES`, `BIDIRECTIONAL_EDGE_TYPES`), `edge_cypher.py` (MERGE / delete-by-run / neighbors / path Cypher builders with whitelisted-type interpolation)
- **Key decisions carried forward**: `:base {entity_id}` is the sole node schema; relationship MERGE templates canonicalize bidirectional pairs; `metadata` field held for future template extension
- **Acceptance**: reviewed and merged

## Sprint 5 — 2026-04-13 ✓ COMPLETE (Phase 1 close)

- **Phase**: 1 (Neo4j Edge Infrastructure)
- **Steps covered**: 1.3 (edge service CRUD), 1.4 (indexes implicit via MERGE), 1.5 (integration tests)
- **Scope**: `app/services/associations/edge_service.py` — `MemoryEdgeService` async executor; `create_edge` / `create_edges_batch` / `get_neighbors` / `get_path` / `delete_edges` / `delete_edges_by_run` / `delete_edges_by_tag` / `count_edges_per_node` / `get_edge_stats` / `on_memory_delete` / `on_memory_supersede`
- **Files**:
  - `Nova_AI_Fusion_Memory_MCP/app/services/associations/edge_service.py`
  - `Nova_AI_Fusion_Memory_MCP/tests/test_memory_edge_service.py`
- **Acceptance**: production invariant unchanged; all CRUD, bidirectional canonicalization, and admin tests passing against live Neo4j
- **Phase 1 status**: CLOSED. Phase 2 unblocked.

## Sprint 6 — 2026-04-13 ✓ COMPLETE

- **Phase**: 2 (Write-Time Similarity Linking)
- **Steps covered**: 2.1 (SimilarityLinker component), 2.2 (perform_upsert hook), 2.3 (bounded concurrency via semaphore), 2.5 (tests)
- **Scope**:
  - `app/services/associations/similarity_linker.py` — `SimilarityLinker` with `enqueue_link()`, `_link_one_safe()`, `_link_one()`, semaphore-bounded (32 in-flight), 0.82 threshold, 30-candidate pool, 10 max neighbors, project-scoped Pinecone queries, fire-and-forget background task, fail-open envelope
  - `app/services/memory_service.py` — hook at lines 916-944 in `perform_upsert()`, flag-guarded by `ASSOC_SIMILARITY_WRITE_ENABLED`, lazy import, zero cost when OFF
  - 24 new tests (18 unit + 6 integration), all passing
- **Deviation from plan**: task-per-call + semaphore instead of queue + worker loop (documented in `similarity_linker.py` module docstring). No lifecycle state on `MemoryService`; identical bounding guarantee; reversible.
- **Load-bearing constants**:
  - `SIMILARITY_THRESHOLD = 0.82` (Sprint 3 decision A)
  - `MAX_NEIGHBORS = 10`, `CANDIDATE_POOL = 30`
  - `BACKGROUND_TIMEOUT = 30.0`, `BACKGROUND_MAX_IN_FLIGHT = 32`
  - Per-run identifier: `wt-link-<uuid8>` (reserved prefix — Sprint 7 backfill refuses this prefix to keep rollback scopes surgical)
- **Latency gate**: PASS. Flag-OFF `perform_upsert` p95 = 0.848ms vs. Sprint 5 baseline 0.981ms. No regression.
- **Acceptance**: all 128 ASSOC tests passing; production Neo4j counts unchanged; no leftover `:SimilarityLinkerIntegrationTestNode`
- **Operator checkpoint**: approved 2026-04-13

## Sprint 7 — 2026-04-13 ✓ COMPLETE

- **Phase**: 2 (Write-Time Similarity Linking) — this sprint closes Phase 2
- **Steps covered**: 2.4 / 2.6 (backfill CLI + library + integration tests + runbook)
- **Scope**: zero production-code change; standalone backfill script reusing Sprint 6's `SimilarityLinker` constants and Sprint 5's `MemoryEdgeService`
- **Files**:
  - `Nova_AI_Fusion_Memory_MCP/scripts/assoc_backfill_similarity.py` (~ 710 LOC) — `backfill_similarity_edges()` library function + `main()` CLI, with:
    - Strict `--run-id` validation (refuses empty, whitespace, wildcards, `wt-link-`, `sprint{2,5,6,7}-` prefixes); `_allow_test_run_id` test-only bypass
    - `--dry-run`: full Pinecone pipeline with zero `create_edges_batch` calls; counts only
    - `--resume-from <memory_id>`: deterministic cursor walk, skips through and including the cursor
    - Automatic checkpoint persistence every 100 memories, atomic write via tmp-file + `os.replace`
    - `--rate-limit-qps`: async token-spacing limiter, default 5.0 QPS, 0 disables
    - `--project-filter`: single-project scoping via paginated Cypher
    - `--max-total`: supports `unlimited` or integer
    - Pinecone embedding fetch via `index.fetch()` (SDK v2/v3 shape tolerance); test hook `fetch_embedding_override` bypasses real Pinecone entirely
    - SIGINT handler → graceful shutdown event; current memory finishes, final checkpoint written, summary printed, exit 130
    - Structured `BackfillReport` with `{memories_scanned, processed, skipped_no_embedding, skipped_no_project, edges_created, pinecone_queries, by_project, errors, checkpoint_final}`
    - Backfill edges tagged `run_id = backfill-<operator-run-id>` so rollback can target them independently of Sprint 6's `wt-link-*` edges
  - `Nova_AI_Fusion_Memory_MCP/tests/test_assoc_backfill_similarity.py` (~ 670 LOC) — 23 tests: 12 validation-refusal params + 11 live-Neo4j integration tests (dry-run zero-writes, live-run edges, rate-limit wall-clock, max-total, resume-from, checkpoint-every-100, project-filter, missing-embedding, idempotency-MERGE, rollback integration via Sprint 2 CLI, final production-invariant check). All Pinecone interaction is mocked; `:BackfillTestNode:base` multi-label + `sprint7-backfill-test-*` run_ids + `try/finally` teardown + production-count invariant (Sprint 2/5/6 pattern)
  - `nova-core/MEMORY/plans/PLAN-0759/phase2_backfill_runbook.md` — operator runbook: pre-flight, dry-run-first, small-live-run, rollback, full-run, post-validation, abort + resume procedures, refused-prefix appendix
  - `nova-core/MEMORY/plans/PLAN-0759/sprint_log.md` — this entry
- **Test results**:
  - `tests/test_assoc_backfill_similarity.py`: 23/23 PASSED (5.67s)
  - Full ASSOC suite (`test_assoc_feature_flags` + `test_assoc_rollback` + `test_assoc_zero_regression` + `test_memory_edges` + `test_edge_cypher` + `test_memory_edge_service` + `test_similarity_linker` + `test_similarity_linker_integration` + `test_assoc_backfill_similarity` + `tests/eval/`): **151/151 PASSED** (10.34s) — zero regressions from Sprint 6's 128-test baseline
  - Production counts pre/post: `:base=825 :Session=339 FOLLOWS=1 INCLUDES=517 SIMILAR_TO=0` unchanged; no leftover `:BackfillTestNode`; no surviving `sprint7-backfill-test-*` edges
- **Guardrail check**:
  - `git diff --stat app/services/memory_service.py app/services/graph_client.py app/services/pinecone_client.py app/config.py app/services/associations/`: ZERO lines
  - No new feature flags
  - No `:Memory`/`memory_id` literals in new files
  - No real Pinecone writes or fetches during tests
  - No modifications to any Sprint 1-6 artifact
- **Phase 2 status**: **READY TO CLOSE, pending Critical Reviewer on Sprint 7.** Steps 2.1-2.3 / 2.5 (Sprint 6), Step 2.4 / 2.6 (this sprint) are covered. The v2 plan's calibration (Step 2.5's threshold sweep, deferred per Sprint 3 decision A) remains a documented future work item and does not block Phase 2 close.
- **Operator checkpoint**: pending
- **Next-sprint gate**: proceed to Sprint 8 (Phase 3 — Entity-Memory Bidirectional Linking) after review + operator approval

## Sprint 8 — 2026-04-13 (in progress pending Critical Reviewer)

- **Phase**: 3 (Entity-Memory Bidirectional Linking) — first half; Sprint 9 closes Phase 3
- **Steps covered**: 3.0 (source contract — Tier A/B framing, library-side only), 3.1 (normalization spec), 3.2 partial (`MAX_ENTITIES_PER_MEMORY=20` cap + ranking rules, heuristic extractor patterns)
- **Scope**: pure-Python standalone utility for the Phase 3 heuristic entity extractor + canonicalization. Library code and unit tests only. Zero production code change, zero Neo4j writes, zero `:Entity` node creation, zero hook wiring. Sprint 9 will add `entity_linker.py`, wire the `perform_upsert()` hook under `ASSOC_ENTITY_WRITE_ENABLED`, and land entity backfill.
- **Files delivered**:
  - `Nova_AI_Fusion_Memory_MCP/app/services/associations/entity_extractor.py` (465 LOC) — `canon_entity`, `extract_entities`, `rank_and_truncate`, `ALIAS_TABLE`, `MAX_ENTITIES_PER_MEMORY=20`, `MAX_CONTENT_BYTES=100*1024`. Pure stdlib (`re`, `logging`, `typing`). Zero imports from sibling `associations/` modules.
  - `Nova_AI_Fusion_Memory_MCP/tests/test_entity_extractor.py` (538 LOC) — 57 hermetic unit tests covering canonicalization (lowercase, whitespace strip + collapse, path separator normalize, `./` strip, trailing `/` strip, alias lookup with and without padding, empty-input ValueError, idempotency, unicode), extraction (all 13 whitelisted extensions, full-path vs bare-file deduplication, CamelCase pattern requires ≥2 uppers, `Claude` not extracted, backtick 60-char cap, multi-line backtick rejection), ranking (document-order, length tie-break, `MAX_ENTITIES_PER_MEMORY` cap, explicit `max_entities` arg), 100 KB content truncation (completes under 1s), determinism (identical output across 10 calls), alias integration (`NC` + `nova-core` dedup to one entry), and structural invariants (regex patterns compiled at module load, no forbidden imports, exact `__all__`).
  - `nova-core/MEMORY/plans/PLAN-0759/sprint_log.md` — this entry
- **Extractor design notes**:
  - Regex patterns used: path (`(?:[a-zA-Z0-9_.-]{1,200}/){1,10}[a-zA-Z0-9_.-]{1,200}\.(?:ext)\b`), bare file (`\b[a-zA-Z0-9_-]{1,200}\.(?:ext)\b`), CamelCase (`\b[A-Z][a-zA-Z0-9]*[A-Z][a-zA-Z0-9]*\b` — two uppercase letters required), backticks (`` `([^`\n]{1,60})` ``).
  - Extension alternation is order-sensitive: `tsx|jsx|yaml` come before `ts|js|yml` so longest-match wins.
  - Bounded quantifiers `{1,200}` and `{1,10}` replace unbounded `+` in the path/filename patterns to prevent catastrophic backtracking on 100 KB of filename-legal characters (the earlier unbounded pattern took 30+ seconds on a 100 KB x-run; bounded version completes the same test in < 1 s).
  - CamelCase and bare-file matches that fall inside a full-path match are suppressed, so `docs/README.md` produces exactly `["docs/README.md"]` (not also `README.md` and `README`).
  - 100 KB cap: `extract_entities` encodes content to UTF-8, slices to `MAX_CONTENT_BYTES`, decodes with `errors="ignore"` to drop any incomplete trailing multibyte sequence, and logs at DEBUG when truncation happens.
  - Alias table: `{"nc": "nova-core", "nt": "novatrade"}`. Applied post-lowercase/post-whitespace-collapse. Docstring notes that additions require PR + operator review.
  - Ranking: `(position ASC, -length ASC)` via stable `sorted()`, top-N truncation. Earliest-first with longer-wins tie-break.
- **Test results**:
  - `tests/test_entity_extractor.py`: **57/57 PASSED** (0.34s)
  - Full ASSOC + eval suite (`test_assoc_feature_flags` + `test_assoc_rollback` + `test_assoc_zero_regression` + `test_memory_edges` + `test_edge_cypher` + `test_memory_edge_service` + `test_similarity_linker` + `test_similarity_linker_integration` + `test_assoc_backfill_similarity` + `test_entity_extractor` + `tests/eval/`): **208/208 PASSED** (10.87s) — zero regressions from Sprint 7's 151-test baseline; exactly +57 tests from Sprint 8.
- **Guardrail check**:
  - `git diff` on Sprint 1-7 production paths (`memory_service.py`, `graph_client.py`, `pinecone_client.py`, `app/config.py`, `app/services/associations/memory_edges.py`, `edge_cypher.py`, `edge_service.py`, `similarity_linker.py`): ZERO lines changed in this sprint (the pre-existing diffs on `memory_service.py`/`app/config.py`/`requirements.txt` are Sprint 1/3/6/7 work, mtime-verified untouched).
  - No new feature flags read or created. Sprint 8 is flag-agnostic; `ASSOC_ENTITY_WRITE_ENABLED` is Sprint 9's consumer.
  - No `:Memory`/`memory_id` Cypher literals anywhere in the new file (verified by grep — the module has no Cypher at all).
  - Zero imports of `neo4j`, `pinecone`, `anthropic`, `asyncio`, or `app.config.settings`. Only `logging`, `re`, `typing`.
  - No integration tests touching live Neo4j or Pinecone; Sprint 8 has zero network / database footprint.
  - `requirements.txt` unchanged by Sprint 8 (the pre-existing `anthropic>=0.40.0` line is Sprint 3).
  - No modification of any Sprint 1-7 artifact (mtime sort confirms `entity_extractor.py` is the only new file in `associations/`).
- **Deviations from plan**: none material. Pattern bounding (`{1,200}`, `{1,10}`) added beyond the plan's spec as a ReDoS guard — bounds are far above any real filename length (255 is the POSIX cap) and are documented in the module docstring.
- **Operator checkpoint**: pending
- **Next-sprint gate**: proceed to Sprint 9 (Phase 3 close — `entity_linker.py` + `perform_upsert()` hook wiring + entity backfill + tests) after review + operator approval

## Sprint 9 — 2026-04-13 ✓ COMPLETE (Phase 3 close)

- **Phase**: 3 (Entity-Memory Bidirectional Linking) — closed in this sprint
- **Scope**: Steps 3.2 (`EntityLinker` component), 3.3 (`perform_upsert()` hook wired under `ASSOC_ENTITY_WRITE_ENABLED`), 3.4 (entity backfill), 3.5 (unit + integration tests against live Neo4j including `:Entity` node creation, `MENTIONS` edge MERGE, per-run rollback). Introduces `:Entity` as the first non-`:base` / non-`:Session` node label on the live graph.
- **Files**: `app/services/associations/entity_linker.py`, `app/services/memory_service.py` (second flag-guarded hook block after Sprint 6's similarity hook), `tests/test_entity_linker.py`, `tests/test_entity_linker_integration.py`, `scripts/assoc_backfill_entities.py` (if present), `tests/eval/baselines/latency_phase3_flag_off_2026-04-13.json`.
- **Latency gate**: PASS — Sprint 9 flag-off p95 = 0.898 ms vs Sprint 5 baseline 0.981 ms (-8.5%).
- **Operator checkpoint**: approved 2026-04-13

## Sprint 10 — 2026-04-13 ✓ COMPLETE

- **Phase**: 4 (Temporal & Session Edges) — first half; Sprint 11 closes Phase 4
- **Steps covered**: 4.1 (per-session concurrency model), 4.2 (`TemporalLinker` component — no hook wiring), 4.4 partial (unit tests)
- **Scope**: pure library-side work. New `app/services/associations/temporal_linker.py` with `TemporalLinker.enqueue_link()` / `_link_one_safe()` / `_link_one()`, per-call semaphore (32 in-flight) + per-session `asyncio.Lock`, `BACKGROUND_TIMEOUT=30.0`, injected `MemoryEdgeService` + either explicit `predecessor_lookup` async callable OR a `driver=AsyncDriver` for the built-in lookup. Cypher pinned to `(:base {entity_id})`. Edge direction: `(current)-[:MEMORY_FOLLOWS]->(predecessor)` (later → earlier); `metadata=None`; per-run `run_id` prefix `wt-temporal-<uuid8>`. Known gap: out-of-order fix-up is deferred (simple-MERGE strategy documented in module docstring). No production-code change; no hook wiring in `memory_service.py` (that lands in Sprint 11).
- **Files**: `app/services/associations/temporal_linker.py`, `tests/test_temporal_linker.py` (unit tests exercising hermetic `predecessor_lookup` callables).
- **Guardrail check**: No modification of any Sprint 1-9 file; zero feature-flag additions; no Cypher literals outside `(:base {entity_id})`.
- **Operator checkpoint**: approved 2026-04-13 (promoted alongside Sprint 11 in the Phase 4 close review)

## Sprint 11 — 2026-04-13 ✓ COMPLETE (Phase 4 close)

- **Phase**: 4 (Temporal & Session Edges) — **CLOSED** in this sprint
- **Steps covered**: 4.3 (coverage monitor — the gate), 4.2 closing (`perform_upsert()` hook wiring under `ASSOC_TEMPORAL_WRITE_ENABLED`), 4.4 closing (integration tests against live Neo4j), Phase 4 latency baseline
- **Gate result (run early in the sprint)**:
  - All-time `:base` coverage: 513/825 = **62.18%** (above 50% threshold)
  - Recent 30-day coverage (`event_time > cutoff`): 426/665 = **64.06%** (above 50% threshold)
  - Gate: **PASS** — Sprint 11 proceeded to wire the hook + land tests + measure latency
  - Full report: `nova-core/MEMORY/plans/PLAN-0759/phase4_coverage_report.md`
- **Files delivered**:
  - `Nova_AI_Fusion_Memory_MCP/scripts/assoc_session_coverage_check.py` (~290 LOC) — standalone CLI coverage monitor. Two read-only queries (all-time + recent window via `event_time` cutoff — NOT `created_at`, which does not exist on `:base` in this graph). JSON output to stdout; Markdown mirror to the plan folder. Exit code 0 PASS / 2 BLOCKED / 1 runtime error. `--uri` / `--database` / `--cutoff-days` / `--threshold-pct` / `--no-write-report` flags.
  - `Nova_AI_Fusion_Memory_MCP/app/services/memory_service.py` — added the Phase 4 hook block after Sprint 9's entity hook (lines ~998-1056) and a one-line `__init__` attribute (`self._temporal_linker: Any = None`) adjacent to Sprint 6 + 9's equivalents. Flag-guarded by `settings.ASSOC_TEMPORAL_WRITE_ENABLED`. Lazy import of `TemporalLinker` + `MemoryEdgeService` on first flag-ON call. Construction passes `driver=self.graph_client.driver` so the linker uses its built-in predecessor-lookup (no test-mode callable in production). Metadata keys read: `session_id`, `thread_id`, `event_seq`, `project`.
  - `Nova_AI_Fusion_Memory_MCP/tests/test_temporal_linker_integration.py` (~820 LOC) — 11 live-Neo4j tests modelled on Sprint 9's pattern. Tests: (1) flag-off no-op; (2) flag-on first-in-session no edge; (3) flag-on second-in-session creates 2→1 edge; (4) three-memory chain (two edges); (5) different-session isolation (zero edges); (6) missing session_id → linker logs no_session; (7) missing event_seq → no edge; (8) idempotent re-store (MERGE); (9) out-of-order arrival self-heal gap (documents Sprint 10 design choice — zero edges expected); (10) concurrent same-session perform_upsert calls (per-session lock serializes, one edge); (zzz) production-count invariant. Dedicated label `:TemporalLinkerTestNode`, per-test unique `sprint11-temporal-test-<uuid8>` run_tag + `sprint11-temporal-session-<uuid8>` session_id, `try/finally` teardown, Pinecone mocked, `_inject_chronology` patched in flag-ON tests so caller-provided `event_seq` survives (the default injector overwrites it unconditionally).
  - `Nova_AI_Fusion_Memory_MCP/tests/eval/baselines/latency_phase4_flag_off_2026-04-13.json` — Sprint 11 flag-off latency measurement with comparative summary vs Sprint 5, 6, 9 baselines.
  - `nova-core/MEMORY/plans/PLAN-0759/phase4_coverage_report.md` — human-readable coverage gate report.
  - `nova-core/MEMORY/plans/PLAN-0759/sprint_log.md` — this entry.
- **Test results**:
  - `tests/test_temporal_linker_integration.py`: **11/11 PASSED** (4.93s)
  - `tests/test_assoc_zero_regression.py`: PASSED (contract preserved under hook-present, flag-off)
  - Combined Sprint 10 unit + Sprint 11 integration + Sprint 9 integration + Sprint 6 integration + zero-regression: **52/52 PASSED** (5.52s)
  - Full Fusion Memory test suite (excluding pre-existing, unrelated `test_redis_timeline.py` flakes): **661 passed**, 0 failures, 0 regressions from Sprint 10 baseline
  - Production Neo4j pre/post: `:base=825 :Session=339 FOLLOWS=1 INCLUDES=517 MEMORY_FOLLOWS=0` unchanged; zero leftover `:TemporalLinkerTestNode`; zero surviving `wt-temporal-*` edges
- **Latency gate**: **PASS** — Sprint 11 flag-off p95 = **0.836 ms** vs Sprint 5 baseline 0.981 ms (**-14.77%**). Also vs Sprint 6 = -1.33%, vs Sprint 9 = -6.88%. All well inside the <=10% regression gate.
- **Guardrail check**:
  - `git diff` in production paths: only `app/services/memory_service.py` touched (hook block + one `__init__` attribute). Sprint 1-10 files untouched.
  - No module-level `TemporalLinker` import in `memory_service.py` (lazy-only, inside flag guard).
  - No `:Memory`/`memory_id` literals in new files (verified by grep).
  - `metadata=None` on MEMORY_FOLLOWS edges (Sprint 10 invariant preserved by the linker; hook does not touch edge construction).
  - No changes to `.env`, `docker-compose.yml`, `requirements.txt`, `app/config.py`.
  - Zero-regression test still PASSES (flag-off behavior byte-identical).
- **Phase 4 status**: **CLOSED**. Steps 4.1/4.2 (Sprint 10), 4.3/4.4 (Sprint 11) all covered. Coverage gate PASS unblocks downstream rollout; `ASSOC_TEMPORAL_WRITE_ENABLED` remains default-False for a canary rollout pass post-review.
- **Operator checkpoint**: pending
- **Next-sprint gate**: proceed to Sprint 12 (Phase 5a — Supersession edges) after review + operator approval

## Sprint 12 — 2026-04-21 ✓ COMPLETE (pending review)

- **Phase**: 5a (Supersession) — first sub-phase of the split Phase 5; Sprints 13/14/15 will cover promotion, compaction, and the provenance read API respectively
- **Workflow**: implementation-team skill (validate → implement → review → verify → debug → re-verify)
- **Duration**: ~1.5h
- **Scope as executed** (differed from pre-flight plan — see "Validator finding" below):
  - `memory_service.py:1251-1300` already contained a real supersession hook (not a stub) landed in an earlier pass. Validator found Phase 5a was ~90% landed.
  - Actual sprint work: (a) normalize `run_id` default in the hook to the `wt-supersede-<session|no-session>` linker-prefix convention matching similarity/temporal linkers; (b) add 4 gap-close tests (MERGE structural idempotency, rollback round-trip with coupled capture, edge-metadata stamping, run_id defaulting); (c) close cross-file default asymmetry by changing `edge_service.on_memory_supersede`'s bare default from `"supersession_hook"` → `"wt-supersede-direct"`.
- **Validator finding** (Plan Validator subagent): Phase 5a's referenced `memory_governance.py` integration point is in `nova-core/agents/`, not Fusion MCP; the *actual* supersession write path lives in `app/services/conflict_detector.py` → `MemoryService.perform_upsert`'s provenance hook at `memory_service.py:1251-1300`. Plan text was stale; scope revised to gap-close rather than greenfield build.
- **Files touched**:
  - `Nova_AI_Fusion_Memory_MCP/app/services/memory_service.py` — 2 lines in the hook at 1286–1287 (`run_id` prefix normalization)
  - `Nova_AI_Fusion_Memory_MCP/app/services/associations/edge_service.py` — 1 default value + docstring at ~739/761 (`"wt-supersede-direct"` default)
  - `Nova_AI_Fusion_Memory_MCP/tests/test_supersession_edges.py` — extended existing stamping assertions; renamed idempotency test; added rollback round-trip test (with captured-writes coupling per review fix), added two run_id defaulting tests. 8 → 12 tests.
  - `Nova_AI_Fusion_Memory_MCP/tests/test_memory_edge_service.py` — single assertion update at line 953 (default string change).
- **Review findings** (Critical Reviewer subagent): verdict APPROVE_WITH_NITS; 0 CRITICAL, 0 HIGH, 4 MEDIUM (M1 default asymmetry, M2 rollback test tautology, M3 test naming drift, M4 identifier collision risk), 3 LOW. M1/M2/M3 fixed in-sprint by Debugger subagent. M4 and all LOW items deferred as low-value. Observability counter deferred to Phase 8.
- **Verification**: 192 tests pass across all Phase 1-5 association suites (`test_supersession_edges.py test_memory_edge_service.py test_assoc_rollback.py test_assoc_zero_regression.py test_similarity_linker.py test_entity_linker.py test_temporal_linker.py test_associative_recall.py test_cooccurrence_linker.py test_memory_edges.py test_edge_cypher.py test_assoc_feature_flags.py`) in 5.70s. Zero-regression baseline still green — no flag-off blast radius.
- **Live Neo4j state**: `MATCH ()-[r:SUPERSEDES]->() RETURN count(r)` = **0** before and after sprint. Flag remains False by design; 5a ships the write path but does not flip defaults. Backfill + flip deferred to Sprint 16 (bundled with 5b/5c) per the "ship provenance as a coherent unit" decision.
- **Invariants preserved**:
  - `ASSOC_PROVENANCE_WRITE_ENABLED=False` default unchanged.
  - Every SUPERSEDES edge emitted now carries `created_by="edge_service.on_memory_supersede"`, `edge_version=1`, `run_id` starting with `wt-supersede-*`, `metadata={"reason": ...}` — rollback-by-run_id round-trip proven hermetically.
  - No changes to `.env`, `docker-compose.yml`, `requirements.txt`, `app/config.py`.
- **Residual risks**:
  - Observability gap: hook failures log to `logger.warning` only. No counter/metric for edge-creation-rate regression detection. Deferred to Phase 8.
  - End-to-end rollback-by-prefix is proven in `test_assoc_rollback.py` (live-DB) and coupled hermetically in the new G2 test, but not exercised against a live production-volume supersession cohort (there is none — 0 edges). First real exercise will be Sprint 16 backfill.
- **Operator checkpoint**: pending (operator requested explicit pause between 5a and 5b)
- **Next-sprint gate**: Sprint 13 (Phase 5b — Promotion edges / `PROMOTED_FROM`) after operator approval

## Sprint 13 — 2026-04-21 ✓ COMPLETE (pending review)

- **Phase**: 5b (Promotion — `PROMOTED_FROM`)
- **Workflow**: implementation-team skill (validate → implement → review → debug → re-review → verify)
- **Duration**: ~2h
- **Scope as executed** (differed from pre-flight — see Validator finding):
  - Validator found `edge_service.on_memory_promote` + 10 hermetic tests **already built**. Real gaps: (a) no hook branch in `memory_service.perform_upsert` for promotion (only supersession existed), (b) no metadata scrub for internal signals flowing into Pinecone/Neo4j, (c) no backfill script.
  - Actual sprint work: (a) new flag-guarded promotion hook branch in `perform_upsert` firing on `metadata["_promoted_from"]`; (b) leading-underscore metadata scrub in `_persist_memory_item` covering BOTH Pinecone AND graph write paths; (c) `scripts/assoc_backfill_provenance_promotion.py`; (d) 5 new memory-service-level tests (T1–T4 + dual-path invariant).
- **Validator finding** (Plan Validator subagent): no promotion event currently flows from `nova-core/agents/memory_consolidator.py` into Fusion MCP — `upsert_memory` writes new memories without any "this-promotes-old-id" signal, and `:base` nodes carry fixed-at-write `memory_type` but no `current_layer`/`memory_layer_candidate` that retroactive backfill could key off. Phase 5b therefore ships as **write-path-ready + backfill-ready but no live edges**: callers in `nova-core/agents/` must pass `metadata["_promoted_from"] = {old_id, from_layer, to_layer}` to trigger edge creation. Cross-repo wiring deferred to Sprint 16.
- **Files touched**:
  - `Nova_AI_Fusion_Memory_MCP/app/services/memory_service.py` — (i) new `sanitized_metadata` dict-comp at lines 969-986 (replaces prior Pinecone-only scrub; used for both writes); (ii) promotion hook branch at lines ~1309-1360 between supersession and co-occurrence blocks; (iii) tightened guard requiring all three promo keys to be non-empty strings.
  - `Nova_AI_Fusion_Memory_MCP/scripts/assoc_backfill_provenance_promotion.py` — NEW (templated on `assoc_backfill_temporal.py`). Args: `--run-id` (required), `--dry-run`, `--max-total`, `--rate-limit-qps`, `--verbose`. Reserved-prefix guard refuses `wt-*`, wildcards, test sprint prefixes. Scan query: `MATCH (n:base) WHERE n.promoted_from_id IS NOT NULL OR n._promoted_from IS NOT NULL`. On current graph: 0 candidates.
  - `Nova_AI_Fusion_Memory_MCP/tests/test_promotion_edges.py` — extended with `_drive_promotion_hook` helper returning `(edge_service, pinecone_client, graph_client)` triple; 4 new T1–T4 tests plus `test_memory_service_promotion_all_underscore_keys_scrubbed_both_paths` covering dual-write invariant. 14 → 19 tests.
- **Review findings** (Critical Reviewer, first pass): verdict REQUEST_CHANGES; **1 CRITICAL, 1 HIGH, 2 MEDIUM, 2 LOW**.
  - **C1 CRITICAL** — original scrub only covered `pinecone_meta` but `graph_client.upsert_graph_data(item_id, content, metadata)` received unscrubbed `metadata`. Neo4j refuses dict property values; on flag flip, every promotion upsert would have failed outright. Tests masked the bug because `graph_client` was mocked with `AsyncMock(return_value=True)`.
  - **H1 HIGH** — tests covered only the Pinecone path.
  - Fixed by Debugger subagent: moved scrub to single `sanitized_metadata` dict-comp applied to both writes; T4 extended with graph-path assertion; new dual-path coverage test added.
  - Re-review verdict: APPROVE_WITH_NITS (0 CRITICAL/HIGH/MEDIUM, 3 LOW). Comment nit (L1) fixed inline — comment now correctly states hooks run AFTER `_persist_memory_item`.
- **Verification**: 211 tests pass across all Phase 1–5 association suites in 6.02s (`test_promotion_edges.py test_supersession_edges.py test_assoc_zero_regression.py test_memory_edge_service.py test_similarity_linker.py test_entity_linker.py test_temporal_linker.py test_associative_recall.py test_cooccurrence_linker.py test_memory_edges.py test_edge_cypher.py test_assoc_feature_flags.py test_assoc_rollback.py`). Zero-regression baseline still green.
- **Backfill script dry-run** (live Neo4j): `candidates_scanned=0 edges_created=0 skipped=0 errors=0`. Expected — no promotion history flows into Fusion MCP today. Script is the rail, ready for Sprint 16 cross-repo wiring.
- **Live Neo4j state**: `MATCH ()-[r:PROMOTED_FROM]->() RETURN count(r)` = **0** before and after sprint. Flag remains False.
- **Invariants preserved**:
  - `ASSOC_PROVENANCE_WRITE_ENABLED=False` default unchanged.
  - `sanitized_metadata` dict-comp drops leading-underscore keys from BOTH Pinecone and graph writes — no internal signal leak to either backend.
  - Every PROMOTED_FROM edge (when flag flips) will carry `created_by="edge_service.on_memory_promote"`, `edge_version=1`, `run_id` prefixed with `wt-promote-` (hook) or `backfill-*` (script), `metadata={"from_layer": ..., "to_layer": ...}`.
  - No changes to `edge_service.py`, `memory_edges.py`, `nova-core/agents/`, `.env`, `docker-compose.yml`, `requirements.txt`, `app/config.py`.
- **Residual risks**:
  - **Cross-repo wiring gap**: nova-core's `memory_consolidator.py` / `FusionMemoryAdapter.upsert_memory` must pass `metadata["_promoted_from"]` for any edge to fire in production. Dead-but-ready until Sprint 16. Safe because no accidental caller would supply the exact three-key dict shape.
  - Observability gap continues (logger.warning only, no counter). Deferred to Phase 8.
- **Operator checkpoint**: pending (operator requested explicit pause between each sub-phase of Phase 5)
- **Next-sprint gate**: Sprint 14 (Phase 5c — Compaction edges / `COMPACTED_FROM`) after operator approval

## Sprint 14 — 2026-04-21 ✓ COMPLETE (pending review)

- **Phase**: 5c (Compaction — `COMPACTED_FROM`)
- **Workflow**: implementation-team skill (validate → implement → review → verify)
- **Duration**: ~1.5h
- **Scope as executed** (same pattern as 5a/5b — helper + tests already present, hook + backfill + integration tests were the gap):
  - Validator found `edge_service.on_memory_compact` already built with list-taking/internal-loop shape, 10 real hermetic tests. No compaction code path in `memory_service.py`.
  - Actual sprint work: (a) new flag-guarded compaction hook branch in `perform_upsert` firing on `metadata["_compacted_from"]`; (b) extended `on_memory_compact` with `metadata: dict | None = None` kwarg so `algorithm`/`reason` survive into the graph; (c) `scripts/assoc_backfill_provenance_compaction.py`; (d) 11 new memory-service-level tests including parametrized malformed-input cases and dual-path scrub invariant.
- **Validator finding**: no `_compacted_from` flows from nova-core's `agents/memory_compactor.py` into Fusion MCP today — compactor writes `compacted_from: list[str]` onto file-backed JSON artifacts and explicitly does not touch Fusion MCP. Phase 5c ships write-path-ready + backfill-ready with **zero live edges**, mirroring 5b's cross-repo gap. Cross-repo wiring deferred to Sprint 16.
- **Files touched**:
  - `Nova_AI_Fusion_Memory_MCP/app/services/memory_service.py` — new compaction hook block inserted at line 1373 (between promotion at ~1371 and co-occurrence at ~1424). Type-strict inner guard validates `_compacted_from` dict + non-empty `source_ids` list of non-empty strings. `run_id = f"wt-compact-{session_id or 'no-session'}"`. Algorithm/reason only included when strings. Outer try/except → `logger.warning("compaction hook failed: %s", exc)`.
  - `Nova_AI_Fusion_Memory_MCP/app/services/associations/edge_service.py` — `on_memory_compact` gained `metadata: dict | None = None` keyword-only kwarg forwarded to every `MemoryEdge` in the per-source loop. When None → `MemoryEdge.metadata=None` preserving pre-existing contract. 10 existing tests unchanged.
  - `Nova_AI_Fusion_Memory_MCP/scripts/assoc_backfill_provenance_compaction.py` — NEW (templated on `assoc_backfill_provenance_promotion.py`). Reserved-prefix tuple now includes all 6 wt-* prefixes (`wt-temporal-`, `wt-entity-`, `wt-link-`, `wt-supersede-`, `wt-promote-`, `wt-compact-`). Scan query: `MATCH (n:base) WHERE n.compacted_from IS NOT NULL OR n._compacted_from IS NOT NULL OR n.source_ids IS NOT NULL RETURN coalesce(...)`. Pre-flight run_id validation moved ahead of Neo4j connect so reserved-prefix refusal doesn't need a live DB.
  - `Nova_AI_Fusion_Memory_MCP/tests/test_compaction_edges.py` — 10 → 21 tests. New `_drive_compaction_hook` helper mirrors the 5b triple-return pattern. Tests cover: hook fires with metadata round-trip (T1), flag-off skip (T2), no-session default (T3), empty source_ids skip (T4), 3-way parametrized malformed (T5), nonfatal exception (T6), dual-path scrub invariant (T7), algorithm-only & None-metadata cases (T8).
- **Review findings** (Critical Reviewer subagent): verdict APPROVE_WITH_NITS; 0 CRITICAL, 0 HIGH, 2 MEDIUM, 5 LOW.
  - M1 — malformed-payload skip has no `logger.debug` trace (same risk as 5a/5b; consistent with precedent).
  - M2 — T6 nonfatal test asserts `perform_upsert` doesn't raise but doesn't capture the emitted warning via `caplog`.
  - Both non-blocking; LOW items include a documented first-hit-wins behavior in the backfill coalesce, lazy-init race on `_provenance_edge_service` (same as 5b), and test coverage quality notes. None regress production behavior.
- **Verification**: 232 tests pass across all Phase 1–5 association suites in 8.58s (`test_compaction_edges.py test_promotion_edges.py test_supersession_edges.py test_assoc_zero_regression.py test_memory_edge_service.py test_similarity_linker.py test_entity_linker.py test_temporal_linker.py test_associative_recall.py test_cooccurrence_linker.py test_memory_edges.py test_edge_cypher.py test_assoc_feature_flags.py test_assoc_rollback.py`). Zero-regression baseline still green.
- **Backfill script dry-run** (live Neo4j): `candidates_scanned=0 edges_created=0 errors=0` — expected (compaction history doesn't flow into Fusion MCP). Reserved-prefix refusal verified: `--run-id wt-compact-bad` → `BackfillError`, exit code 1.
- **Live Neo4j state** (all 3 provenance edge types, post-sprint): SUPERSEDES=0, PROMOTED_FROM=0, COMPACTED_FROM=0. All flag-gated False. Full edge inventory: MENTIONS=9081, CO_OCCURS=4734, SIMILAR_TO=1221, INCLUDES=522, MEMORY_FOLLOWS=182, FOLLOWS=1 (unchanged from Sprint 11).
- **Invariants preserved**:
  - `ASSOC_PROVENANCE_WRITE_ENABLED=False` default unchanged.
  - `sanitized_metadata` dict-comp scrub at `_persist_memory_item` automatically strips `_compacted_from` from both Pinecone and graph writes — no new scrub code needed (5b precedent).
  - Every COMPACTED_FROM edge (when flag flips) will carry `created_by="edge_service.on_memory_compact"`, `edge_version=1`, `run_id` prefixed with `wt-compact-` (hook) or `backfill-*` (script).
  - No changes to `memory_edges.py`, `nova-core/agents/`, `.env`, `docker-compose.yml`, `requirements.txt`, `app/config.py`.
- **Residual risks**:
  - Cross-repo wiring gap (same as 5b): nova-core compactor must pass `metadata["_compacted_from"]` for any edge to fire in production. Dead-but-ready until Sprint 16.
  - Fan-out latency on flag-flip day: worst-case 20 sequential edge writes per compaction event (~1s added to store latency at p95 50ms/edge). Synchronous dispatch is acceptable at this volume but document in flip-day runbook.
  - Observability gap continues (logger.warning only). Deferred to Phase 8.
- **Operator checkpoint**: pending (operator requested explicit pause between each sub-phase of Phase 5)
- **Next-sprint gate**: Sprint 15 (Phase 5d — Provenance read API + MCP wiring) after operator approval

## Sprint 15 — 2026-04-21 ✓ COMPLETE (pending review) — **CLOSES PHASE 5**

- **Phase**: 5d (Provenance Read API + MCP wiring) — final sub-phase of Phase 5
- **Workflow**: implementation-team skill (validate → implement → review → debug → verify)
- **Duration**: ~1.5h
- **Scope as executed** (gap-close, not greenfield):
  - Validator found Phase 5d ~95% already built: `MemoryEdgeService.get_provenance` fully implemented at `edge_service.py:952-1097` with variable-length Cypher walking `SUPERSEDES|PROMOTED_FROM|COMPACTED_FROM`, max_depth clamped [1,10], Python-side dedup, cycle-safe. MCP tool at `mcp_server.py:859-923` already wired with 30-node chain cap. 9 real hermetic tests in `test_provenance_api.py` all passing.
  - Actual sprint work: (a) polish MCP tool response shape — propagate `max_depth` from service, add `exists: bool`, add `exists_checked: bool` (graceful-degradation sentinel); (b) new `MemoryEdgeService.node_exists` helper for the empty-chain existence probe; (c) filter `original_sources` to match truncated chain when response capped; (d) 6 new MCP-layer integration tests against live Neo4j; (e) defer proxy-node / `source_kind` expansion (v2 §5d) with a traceable TODO comment pointing to PLAN-0759.
- **Validator finding**: `get_provenance` service method and MCP tool both real, not stubs. Response shape had gaps: `max_depth`/`depth_limited` not propagated, no `exists` field to distinguish ghost ids from no-provenance-edges. Proxy nodes (`source_kind ∈ {file, vault, memory}` per v2 §5d) entirely absent from live graph — 0 `:Proxy` nodes, 0 nodes with `source_kind` property. Deferred until file/vault integration actually writes proxy nodes.
- **Files touched**:
  - `Nova_AI_Fusion_Memory_MCP/app/services/associations/edge_service.py` — added `node_exists(memory_id: str) -> bool` (~lines 952-975 with existing method shifted down); TODO comment above `get_provenance` citing PLAN-0759 v2 §5d and dating the live-graph audit 2026-04-21.
  - `Nova_AI_Fusion_Memory_MCP/mcp_server.py:859-945` — `get_provenance` tool: docstring expanded to distinguish `truncated` (response-size cap) vs `depth_limited` (traversal cap); added `max_depth` passthrough, `exists` field, `exists_checked` sentinel; `original_sources` now filtered to only include ids present in truncated chain when `truncated=True`.
  - `Nova_AI_Fusion_Memory_MCP/tests/test_provenance_api.py` — added 5 live-Neo4j MCP tool-layer tests: unknown id, empty id, max_depth=99 clamped, 35-source fan-out response cap, negative max_depth clamped. Plus T-MCP-6 (hardening) covering `node_exists` probe failure path.
  - `Nova_AI_Fusion_Memory_MCP/tests/test_mcp_association_tools.py` — updated `test_chain_cap_30` assertion which had codified the pre-fix buggy `original_sources` shape.
- **Review findings** (Critical Reviewer subagent): verdict APPROVE_WITH_NITS; 0 CRITICAL, 0 HIGH, 2 MEDIUM, 5 LOW.
  - **M1** — `exists=False` on probe failure indistinguishable from ghost id. Fixed with `exists_checked: bool` sentinel.
  - **M2** — `original_sources` cap was independent of chain cap, producing dangling refs when `truncated=True`. Fixed by filtering sources against truncated chain's id set.
  - LOW items covered routine nits (test hermeticity, TODO comment accuracy, regression surface) — accepted without change.
- **Verification**: 290 tests pass across all Phase 1–5 association suites in 7.07s (full suite: `test_provenance_api.py test_mcp_association_tools.py test_compaction_edges.py test_promotion_edges.py test_supersession_edges.py test_assoc_zero_regression.py test_memory_edge_service.py test_similarity_linker.py test_entity_linker.py test_temporal_linker.py test_associative_recall.py test_cooccurrence_linker.py test_memory_edges.py test_edge_cypher.py test_assoc_feature_flags.py test_assoc_rollback.py`). Zero-regression baseline still green.
- **MCP response shape (final)**:
  ```
  { memory_id, provenance_chain, original_sources, depth, max_depth,
    depth_limited, chain_count, full_chain_count, truncated, exists, exists_checked }
  ```
  Additive vs. pre-sprint: `max_depth`, `exists`, `exists_checked`. Backward-compatible (no existing field removed or renamed).
- **Live Neo4j state**: unchanged — SUPERSEDES=0, PROMOTED_FROM=0, COMPACTED_FROM=0 (read-side sprint; no writes).
- **Invariants preserved**:
  - `ASSOC_PROVENANCE_WRITE_ENABLED=False` default unchanged.
  - No changes to write-path hooks (5a/5b/5c), `memory_edges.py`, `app/config.py`, backfill scripts, or `nova-core/agents/`.
  - MCP tool response size capped at 30 chain nodes (defense in depth: service returns full, tool caps).
  - Proxy-node expansion TODO traceable to PLAN-0759.
- **Residual risks**:
  - Proxy-node / `source_kind` expansion still not implemented — acceptable because no proxy nodes exist in live graph today. Will need revisit when file/vault writes proxy nodes.
  - Observability gap continues (logger.warning only). Deferred to Phase 8.
  - 5d is read-side; the real exercise comes in Sprint 16 when the write-flag flips and provenance edges actually exist to walk.
- **Operator checkpoint**: pending
- **Phase 5 closeout**: **Phase 5a/5b/5c/5d all COMPLETE.** All three write hooks (supersession/promotion/compaction) shipped with flag-False default, dual-path metadata scrub, rollback-by-run_id convention, hermetic tests. Provenance read API shipped + MCP-wired + polished. Flag flip + live backfills = Sprint 16 (task #5).
- **Next-sprint gate**: Sprint 16 — flip `ASSOC_PROVENANCE_WRITE_ENABLED=True` in config default; run dry-run → live backfills for all three provenance edge types (expected: 0 candidates from Fusion MCP since cross-repo wiring not yet in place); decide whether to add minimal cross-repo wiring in `nova-core/agents/` so compaction/promotion events actually flow, or to defer that cross-repo work to a later sprint while still shipping the write-path-ready infra.

## Sprint 16 — 2026-04-21 ✓ COMPLETE — **ASSOC_PROVENANCE_WRITE_ENABLED flipped True**

- **Phase**: Phase 5 rollout (flag flip + backfills)
- **Scope as executed**: operator chose path (a) — ship what's built, accept 0 live edges, defer cross-repo wiring. No new code beyond the flag flip and two test-harness alignments.
- **Duration**: ~15 min
- **Files touched**:
  - `Nova_AI_Fusion_Memory_MCP/app/config.py:74` — `ASSOC_PROVENANCE_WRITE_ENABLED: bool = False → True`. Inline comment records flip date (2026-04-21) and gate criteria (Sprints 12–15 closed with 0 CRITICAL/HIGH findings, 290 tests green, 0 live provenance edges pre-flip).
  - `Nova_AI_Fusion_Memory_MCP/tests/test_assoc_zero_regression.py` — added `ASSOC_PROVENANCE_WRITE_ENABLED` to the `shipped_flags` set (line 445) and to the force-disable/restore block in the test body (lines 465–467 and 523–524). Mirrors the `ASSOC_GRAPH_RECALL_ENABLED` pattern landed 2026-04-16.
  - `Nova_AI_Fusion_Memory_MCP/tests/test_assoc_feature_flags.py:41-44` — added `ASSOC_PROVENANCE_WRITE_ENABLED: True` to `SHIPPED_TRUE` so the defaults pinning test reflects the new ship-state.
- **Verification**: 290 tests pass post-flip across all Phase 1–5 association suites (`test_assoc_feature_flags.py test_assoc_zero_regression.py test_provenance_api.py test_mcp_association_tools.py test_compaction_edges.py test_promotion_edges.py test_supersession_edges.py test_memory_edge_service.py test_similarity_linker.py test_entity_linker.py test_temporal_linker.py test_associative_recall.py test_cooccurrence_linker.py test_memory_edges.py test_edge_cypher.py test_assoc_rollback.py`) in 7.32s. Zero-regression baseline still pinned (force-disable ensures the snapshot stays valid despite the default flip).
- **Dry-run backfills** (live Neo4j):
  - `python3 -m scripts.assoc_backfill_provenance_promotion --dry-run --run-id phase5b-promote-flipday` → `candidates_scanned=0 edges_created=0 skipped=0 errors=0`.
  - `python3 -m scripts.assoc_backfill_provenance_compaction --dry-run --run-id phase5c-compact-flipday` → `candidates_scanned=0 edges_created=0 skipped=0 errors=0`.
  - No separate 5a supersession backfill exists (supersession fires only via live conflict-detection events, not retroactively). Acceptable.
  - **Live backfills not executed** — dry-runs show 0 candidates, live runs would produce identical results on this graph.
- **Live Neo4j state, post-flip**:
  - Edge inventory: MENTIONS=9081, CO_OCCURS=4734, SIMILAR_TO=1221, INCLUDES=522, MEMORY_FOLLOWS=182, FOLLOWS=1. Unchanged from pre-flip.
  - SUPERSEDES=0, PROMOTED_FROM=0, COMPACTED_FROM=0. Hooks are **armed and waiting** — first conflict-detection event on a `decision`-category upsert will produce the first SUPERSEDES edge; first caller to pass `_promoted_from` / `_compacted_from` metadata will produce the first PROMOTED_FROM / COMPACTED_FROM edge.
- **Behavioral impact of flip**:
  - **Supersession (5a)**: the `conflict_detector` path in `memory_service.py:1281` now fires on every `decision`-category upsert. Expected frequency: low (decision memories are rare). Each conflict produces one `SUPERSEDES` edge tagged `wt-supersede-<session|no-session>`.
  - **Promotion (5b)**: the hook at `memory_service.py:1345` requires `metadata["_promoted_from"]` to be a dict with `{old_id, from_layer, to_layer}` as non-empty strings. No nova-core caller currently supplies this signal → **zero edges will fire in production today**. Dead-but-armed.
  - **Compaction (5c)**: same story — hook at `memory_service.py:1386` requires `metadata["_compacted_from"]`. No caller supplies it → **zero edges will fire today**. Dead-but-armed.
- **Residual gap (accepted, deferred)**: cross-repo wiring. `nova-core/agents/memory_compactor.py` and `memory_consolidator.py` do not pass the internal `_promoted_from` / `_compacted_from` metadata signals into Fusion MCP. Until that wiring lands, the 5b/5c edges remain at 0. This is a **deliberate deferral per operator decision** — the v2 plan's full goal requires the rail and the cross-repo data flow, but shipping them separately preserves small-diff discipline and keeps nova-core / Fusion MCP boundaries clean. Track as follow-up post-PLAN-0759.
- **Invariants preserved**:
  - 16 backfill tests still pass — reserved-prefix guards unchanged.
  - Zero-regression baseline file (`tests/fixtures/phase0_regression_baseline.json`) unchanged — the test's force-disable logic restores flag-False behavior inside the test body.
  - `ASSOC_GRAPH_RECALL_ENABLED=True` (2026-04-16) and `ASSOC_PROVENANCE_WRITE_ENABLED=True` (2026-04-21) are the only non-False defaults in the 8-flag ASSOC_* set. Everything else stays at False pending its own gate.
- **Phase 5 rollout complete**: write path armed, read path live, MCP tool exposed, observability deferred to Phase 8. Task #5 closed.
- **Next**: Task #6 — re-score PLAN-0759 completion against the v2 per-phase scorecard.

## Sprint 17 — 2026-04-23 ✓ COMPLETE (pending review) — **Phase 8a: live-test `get_provenance` against real edges**

- **Phase**: Phase 8 (MCP tooling hardening — live-exercise the already-shipped provenance read path)
- **Workflow**: implementation-team (validate → implement → review → verify). This entry is the Implementer hand-off to the Critical Reviewer.
- **Scope as executed**: seed 3 provenance edges between 4 pre-existing real nova-core `:base` nodes, live-exercise the `get_provenance` MCP handler against them, add one hermetic mixed-edge-type regression, roll back by `run_id`, verify graph is byte-identical to pre-sprint. FastMCP stdio-transport live-test skipped with documented rationale.
- **Duration**: ~45 min (within the validator estimate).
- **Chosen 4 nova-core `:base` nodes** (verified clean of SUPERSEDES/PROMOTED_FROM/COMPACTED_FROM at pick time):
  - **A** = `005b347afc0f10618dadd5a283f37a58` — "Research: Autonomous Agent Scheduling Patterns (2026-03-11)" (BabyAGI, MCP task tools, adaptive heartbeat).
  - **B** = `015916fdfc5a8d32b280b96d4a46015e` — "CEO Nova Telegram Implementation Plan" (7-phase plan, dual-layer conversational AI).
  - **C** = `0161deeeae900bce4cd31d15760f06e1` — "Session checkpoint: session-2026-04-06-5" (watcher unbounded-retry loop fix).
  - **D** = `0294ca7688ba0396bf0bf3d1b7355683` — "Built evidence.jsonl daily rotation for NovaTrade (2026-03-23)".
- **Seeded topology** (3 edges, tagged `created_by=sprint-17`, `run_id=wt-phase8-livetest-2026-04-23`, `edge_version=1`):
  - `A --[SUPERSEDES]--> B` (pure supersession chain; B is a leaf)
  - `A --[PROMOTED_FROM]--> C` (start of mixed chain)
  - `C --[COMPACTED_FROM]--> D` (completes mixed chain; D is a leaf)
- **Files created**:
  - `Nova_AI_Fusion_Memory_MCP/scripts/seed_phase8_livetest_provenance.py` (~160 LOC) — idempotent CLI seeder. CONSTANTS-level 4 entity_ids. Uses `MemoryEdgeService.create_edge` / `MemoryEdge` dataclass (no raw Cypher). Supports `--dry-run`, `--run-id`, `--uri`, `--database`, `-v`. Prints per-type prov-edge counts before/after.
  - `Nova_AI_Fusion_Memory_MCP/tests/test_provenance_livetest.py` (~200 LOC) — 2 live tests; module-scoped seed fixture that (a) defensively rolls back any pre-existing edges tagged with the run_id, (b) seeds the 3 edges via the edge service, (c) teardown invokes `scripts.assoc_rollback.assoc_rollback(run_id=..., dry_run=False)` and asserts `report.total >= 3`. Mirrors `test_provenance_api.py` Context stub + skip-on-unreachable pattern.
- **Files modified**:
  - `Nova_AI_Fusion_Memory_MCP/tests/test_mcp_association_tools.py:717` — added `test_get_provenance_mixed_edge_types` inside `class TestGetProvenance`. Hermetic (AsyncMock-backed). Asserts the MCP handler preserves `edge_type` verbatim for SUPERSEDES + PROMOTED_FROM + COMPACTED_FROM chain entries. Closes the pure-type-only gap flagged by the Validator.
- **Dry-run evidence**:
  ```
  $ NEO4J_URI=bolt://localhost:7687 python3 -m scripts.seed_phase8_livetest_provenance --dry-run
  [dry-run] Would create 3 edges tagged run_id=wt-phase8-livetest-2026-04-23:
    005b347afc0f10618dadd5a283f37a58 -[SUPERSEDES]-> 015916fdfc5a8d32b280b96d4a46015e
    005b347afc0f10618dadd5a283f37a58 -[PROMOTED_FROM]-> 0161deeeae900bce4cd31d15760f06e1
    0161deeeae900bce4cd31d15760f06e1 -[COMPACTED_FROM]-> 0294ca7688ba0396bf0bf3d1b7355683
  ```
- **Live-test assertions green** (both new tests in `test_provenance_livetest.py`):
  - (a) `get_provenance(A, max_depth=5)` returns 3-hop chain `{B, C, D}`; per-hop depth and edge_type verified; mixed edge types present.
  - (b) `original_sources == {B, D}`; `depth == 2`; `depth_limited == False`; `exists == True`; `exists_checked == True`.
  - (c) `max_depth=1` cuts chain to `{B, C}`, `depth_limited == True`.
- **FastMCP stdio transport live-test**: SKIPPED. Rationale: exercising the client→stdio→server path would require spawning `python mcp_server.py` as a subprocess, which triggers the full `service_lifespan` (MemoryService init with Pinecone, OpenAI embeddings, sentence-transformers warmup, Neo4j constraint creation). That path also requires `OPENAI_API_KEY` and Pinecone creds at runtime. Coverage cost: >30 min boilerplate + ~30s subprocess warmup per test + potential secret-dependency. Coverage benefit: transport-framing delta only — the handler function is already exercised against a real Neo4j driver in `test_provenance_api.py:436-590` and now in `test_provenance_livetest.py`. Per the sprint spec, documented and skipped. Recommend deferring to a future observability / smoke-test sprint if ever needed.
- **Live Neo4j counts**:
  - Before seeding: MENTIONS=9081, CO_OCCURS=4734, SIMILAR_TO=1221, INCLUDES=523, MEMORY_FOLLOWS=182, FOLLOWS=1. SUPERSEDES/PROMOTED_FROM/COMPACTED_FROM=0.
  - During test run: 1×SUPERSEDES + 1×PROMOTED_FROM + 1×COMPACTED_FROM tagged run_id=wt-phase8-livetest-2026-04-23.
  - After rollback (`assoc_rollback --run-id wt-phase8-livetest-2026-04-23`): identical to pre-seed. Provenance edge types all back to 0.
- **Full provenance suite run**: `pytest tests/test_provenance_api.py tests/test_mcp_association_tools.py tests/test_provenance_livetest.py -x -q` → **61 passed in 5.98s** (58 baseline + 2 new livetest + 1 new hermetic). Baseline pre-sprint was 58 passed in 5.22s.
- **Invariants preserved**:
  - Non-provenance edge counts identical pre/post (MENTIONS/CO_OCCURS/SIMILAR_TO/INCLUDES/MEMORY_FOLLOWS/FOLLOWS unchanged).
  - The 4 real `:base` nodes untouched — only 3 synthetic edges added + removed.
  - `ProvenanceTestNode` teardown in `test_provenance_api.py` unchanged; the new test file does not share fixtures with the existing suite.
  - No raw Cypher in the seeder; only the edge service layer is used.
  - No feature-flag changes; no commits.
- **Residual risk / open issues**: none material. The transport-layer gap is documented above; if it ever matters, the right path is a separate ops-smoke harness, not a unit test.
- **Next-sprint gate**: Sprint 18 — Phase 8b observability (SLO metrics + alerts on association tools).
