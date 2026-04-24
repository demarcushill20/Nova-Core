# Phase 2 Similarity Backfill Runbook

**PLAN**: PLAN-0759
**Phase**: 2 (Write-Time Similarity Linking)
**Step**: 2.6 (Backfill)
**Script**: `Nova_AI_Fusion_Memory_MCP/scripts/assoc_backfill_similarity.py`
**Rollback**: `Nova_AI_Fusion_Memory_MCP/scripts/assoc_rollback.py`
**Audience**: operator running the one-shot backfill of `SIMILAR_TO` edges
for the existing Fusion Memory corpus after Sprint 6 lands write-time
linking.

This is a how-to, not a design doc. For design rationale see Sprint 7's
implementation report and the module docstring on `assoc_backfill_similarity.py`.

---

## 1. Pre-flight checklist

Before you start, confirm all of the following:

1. **Fusion Memory MCP on `main`, clean working tree**
   - `git status` in `~/Nova_AI_Fusion_Memory_MCP` shows no modified files.
   - `git log --oneline -1` shows the Sprint 7 merge commit.
2. **Sprint 6 feature flag status**
   - `ASSOC_SIMILARITY_WRITE_ENABLED` can be either `True` or `False`. The
     backfill does NOT read this flag — it instantiates `SimilarityLinker`
     directly with an injected edge service.
   - If the flag is `True`, new writes are also creating `SIMILAR_TO` edges
     with `run_id = wt-link-<uuid8>`. Those coexist peacefully with backfill
     edges tagged `run_id = backfill-<your-run-id>`. Rollback can target
     either set independently.
3. **Neo4j reachable**
   - From the host: `bolt://localhost:7687` (default). From inside the MCP
     Docker network: `bolt://neo4j:7687` (if you invoke the script from
     inside the container, which is not the recommended path).
   - Run the schema audit first to confirm baselines:
     `python -m scripts.audit_neo4j_schema --uri bolt://localhost:7687`
   - Expected baseline (≤ small organic drift on `:base`): `:base` ≥ 825,
     `:Session` = 339, `FOLLOWS` = 1, `INCLUDES` = 517, `SIMILAR_TO` may be
     non-zero if Sprint 6 has already been flagged on.
4. **Pinecone reachable**
   - `PINECONE_API_KEY` and `PINECONE_INDEX` set in the shell environment.
     The script picks these up via CLI defaults or the constructed
     `PineconeClient`.
   - Sanity check the index has non-zero vectors via the Pinecone console
     or `describe_index_stats()`.
5. **No other backfills or rollbacks running**
   - Two concurrent runs with the same `run_id` would both be safe (MERGE is
     idempotent), but the second run would double-count rate-limit budget
     and produce confusing logs. Serialize.
6. **Disk space for the checkpoint file**
   - Default path: `/tmp/assoc_backfill_<run_id>.checkpoint`. Tiny JSON
     file (< 1 KB), but confirm `/tmp` is writable.

---

## 2. Dry-run on a small subset first

Always start with a small, rate-limited dry run. The dry run makes real
Pinecone queries but writes no Neo4j edges — this is where you estimate
API cost.

```bash
python -m scripts.assoc_backfill_similarity \
    --run-id backfill-$(date +%Y%m%d) \
    --dry-run \
    --max-total 50 \
    --project-filter nova-core \
    --rate-limit-qps 2.0 \
    --verbose
```

**What to check in the output:**
- `memories_scanned` and `memories_processed` should both be ≤ 50.
- `edges_created` is the count of `SIMILAR_TO` edges that WOULD be created.
- `pinecone_queries` is the actual Pinecone call count (fetch + query per
  memory). Multiply by your Pinecone per-query cost (confirm with the
  billing dashboard — the operator must validate the unit price, the
  runbook does not hardcode it) to estimate the API bill.
- `memories_skipped_no_embedding` must be 0 for a healthy small sample;
  if it's non-zero, Pinecone's vector store has drifted away from Neo4j's
  node set and you should investigate before a larger run.

---

## 3. Full dry-run for cost estimation

Drop `--max-total` to see the full corpus cost before any edges are written.

```bash
python -m scripts.assoc_backfill_similarity \
    --run-id backfill-$(date +%Y%m%d) \
    --dry-run \
    --project-filter nova-core \
    --rate-limit-qps 5.0 \
    --verbose \
    > /tmp/assoc_backfill_dryrun_report.json
```

Look at `pinecone_queries` in the final JSON report. Confirm the estimated
cost is acceptable before proceeding.

---

## 4. Small live-run to validate mechanics

After the dry run looks reasonable, do a small live run to confirm edges
actually land in Neo4j.

```bash
python -m scripts.assoc_backfill_similarity \
    --run-id backfill-$(date +%Y%m%d) \
    --max-total 100 \
    --project-filter nova-core \
    --rate-limit-qps 2.0 \
    --verbose
```

**Post-run spot checks:**
```cypher
// Count edges created by this backfill run
MATCH ()-[r:SIMILAR_TO]->()
WHERE r.run_id = 'backfill-backfill-YYYYMMDD'
RETURN count(r)
```
(Note: the script prefixes `backfill-` onto the run_id you pass. The actual
`run_id` property on the edge is `backfill-<your-arg>`. See
`assoc_backfill_similarity.py` — search for `tagged_run_id`.)

```cypher
// Sample 10 edges and eyeball relevance
MATCH (a:base)-[r:SIMILAR_TO]->(b:base)
WHERE r.run_id = 'backfill-backfill-YYYYMMDD'
RETURN a.entity_id, b.entity_id, r.weight, a.project, b.project
LIMIT 10
```

**Red flags that mean you should stop and investigate:**
- Source and target are in completely different projects (unless
  cross-project linking is intentionally enabled).
- Weights cluster at the threshold floor (0.82). This can indicate the
  similarity calibration needs tightening, not that the script is broken.
- `created_by` on the edge is not `"assoc_backfill_similarity"`.

---

## 5. Rollback procedure (if the small run looks wrong)

The rollback is edge-level only. Nodes stay intact. Sprint 2's rollback
script handles this:

```bash
python -m scripts.assoc_rollback \
    --run-id backfill-backfill-YYYYMMDD \
    --dry-run
# Review the deleted_by_type count, then:
python -m scripts.assoc_rollback \
    --run-id backfill-backfill-YYYYMMDD
```

After rollback, re-run the post-run spot-check query — the count should be
zero.

---

## 6. Full live run

Only after a clean small-run validation. Use a **distinct** `run_id` (for
example, append `-full`) so rollback scope is surgical — you don't want to
conflate the validation run and the full run.

```bash
python -m scripts.assoc_backfill_similarity \
    --run-id backfill-$(date +%Y%m%d)-full \
    --rate-limit-qps 5.0 \
    --verbose \
    > /tmp/assoc_backfill_fullrun.json
```

For very large corpora, consider running this inside `tmux` or `screen` so
a dropped SSH connection does not abort the script. The final checkpoint
file at `/tmp/assoc_backfill_<run_id>.checkpoint` can be used to resume
via `--resume-from <last_processed_memory_id>`.

---

## 7. Post-backfill validation

Run each of these and record the results:

1. **Edge count for the run**
   ```cypher
   MATCH ()-[r:SIMILAR_TO]->()
   WHERE r.run_id = 'backfill-backfill-YYYYMMDD-full'
   RETURN count(r)
   ```
   This should be within ±5% of the dry-run estimate.

2. **Production invariant**
   ```cypher
   MATCH (n:base) RETURN count(n);
   MATCH (n:Session) RETURN count(n);
   MATCH ()-[r:FOLLOWS]->() RETURN count(r);
   MATCH ()-[r:INCLUDES]->() RETURN count(r);
   ```
   - `:base` may drift upward from organic writes, but never downward.
   - `:Session`, `FOLLOWS`, `INCLUDES` must be byte-for-byte identical to
     pre-backfill — the backfill never touches any of them.

3. **Sample quality eyeball**
   Pull 10 random edges and read the source/target memory text in Obsidian
   or via the MCP's recall interface. Do they actually look related? If
   most are unrelated, the 0.82 threshold is too loose for this corpus and
   you should roll back + reconsider calibration before the next run.

4. **Latency impact on `perform_upsert()`**
   Re-run Sprint 6's latency harness (see Sprint 6 implementation report
   for the exact command). `p95` should be within 10% of Sprint 5 baseline
   (the backfill doesn't touch the store path, so this is a smoke test for
   cache / contention side effects — not expected to drift).

---

## 8. Known-good exit criteria

Before marking the backfill complete, confirm ALL of:

- [ ] Full run completed without errors (`errors` array in JSON report is empty)
- [ ] Edge count matches dry-run estimate ±5%
- [ ] `:Session`, `FOLLOWS`, `INCLUDES` counts unchanged
- [ ] Sample of 10 edges passes relevance eyeball
- [ ] `perform_upsert()` p95 still within 10% of Sprint 5 baseline
- [ ] Checkpoint file archived (copy to `~/nova-core/MEMORY/plans/PLAN-0759/`
      for audit trail)

---

## 9. Abort procedure

**Graceful abort** (preserves clean state):
- Hit `Ctrl-C` once. SIGINT is caught by the script, which finishes the
  current memory, writes the final checkpoint, prints the summary, and
  exits with code 130. No half-processed memories, no orphan state.

**Hard abort** (leaves partial state):
- `kill -9 <pid>` or host OOM. Partial edges may exist. Clean up with:
  ```bash
  python -m scripts.assoc_rollback --run-id backfill-<your-run-id>
  ```

**Resume after abort**:
- Inspect `/tmp/assoc_backfill_<run_id>.checkpoint` to read
  `last_processed_memory_id`.
- Re-run the backfill with `--resume-from <that_memory_id>`. The script
  skips through that cursor and restarts processing.

---

## 10. Command quick reference

```bash
# Dry-run subset
python -m scripts.assoc_backfill_similarity \
    --run-id backfill-YYYYMMDD --dry-run \
    --max-total 50 --project-filter nova-core --rate-limit-qps 2.0 -v

# Small live run
python -m scripts.assoc_backfill_similarity \
    --run-id backfill-YYYYMMDD --max-total 100 \
    --project-filter nova-core --rate-limit-qps 2.0 -v

# Full live run
python -m scripts.assoc_backfill_similarity \
    --run-id backfill-YYYYMMDD-full --rate-limit-qps 5.0 -v

# Rollback
python -m scripts.assoc_rollback --run-id backfill-YYYYMMDD-full

# Resume after crash
python -m scripts.assoc_backfill_similarity \
    --run-id backfill-YYYYMMDD-full \
    --resume-from <last_processed_memory_id> \
    --rate-limit-qps 5.0 -v
```

---

## Appendix A — Refused run_id prefixes

The script refuses any `--run-id` that matches any of:

- Empty string or whitespace-only
- Wildcards: `*`, `%`, `all`, `ALL`
- Prefix `wt-link-` (reserved for Sprint 6's write-time linker)
- Prefix `sprint2-`, `sprint5-`, `sprint6-`, `sprint7-` (reserved for test
  scaffolding; test bypass flag never exposed to CLI)

Use `backfill-YYYYMMDD` or `backfill-YYYYMMDD-full` or any other
operator-meaningful identifier that does not match the above.
