# Conflict Policy

## What is a conflict?

A conflict occurs when optimizing one skill's description causes it to steal
triggers from a neighboring skill. This is the primary failure mode of
per-skill optimization in a multi-skill system.

## Neighbor Sources

Neighbors are identified from four sources (merged, deduplicated):

1. **Manual curation:** `MANUAL_NEIGHBORS` in skill_discovery.py
2. **Domain/tag heuristics:** Skills sharing domain tags in metadata.json
3. **Embedding similarity:** (Optional, Phase 2+) Cosine similarity of descriptions
4. **Historical co-trigger:** (Optional, Phase 2+) Skills that historically compete

## Interference Check Protocol

Before accepting a candidate:

1. Run candidate description against each neighbor's positive eval set
2. Measure false trigger rate on neighbor queries
3. Compare to baseline false trigger rate
4. Reject if conflict rate exceeds threshold

## Conflict Rate Formula

```
conflict_rate = (false_triggers_on_neighbor_queries) / (total_neighbor_queries)
```

## Thresholds

- Default conflict limit: 0.10 (10%)
- High-risk skills: 0.05 (5%)
- Zero tolerance on direct overlap_pairs boundary queries

## Resolution

If a candidate improves its own skill but degrades neighbors:
1. Reject the candidate
2. Log the conflict with both skill names
3. Add to regression_watchlist.md
4. Consider whether the boundary definition needs updating

## Escalation

If repeated optimization attempts consistently trigger conflicts:
1. Flag the skill pair for manual review
2. Consider updating overlap_pairs.json with clearer boundaries
3. Consider whether the two skills should be merged
