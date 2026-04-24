# PLAN-0759 Step 0.6 — Cross-Project Scoping Appendix

**Sprint**: 3 (Phase 0 close-out)
**Date**: 2026-04-13
**Status**: decision confirmed — single-project isolation is the default;
cross-project linking is opt-in via `ASSOC_CROSS_PROJECT_ENABLED=True`
(flag already added in Sprint 1, defaults to False)
**Author**: Implementer sub-agent (Sprint 3)

## Decision (from v2 plan, re-affirmed)

- **Default**: `SimilarityLinker`, `EntityLinker`, and every other
  associative linker introduced in PLAN-0759 Phase 1+ scope their Cypher
  queries to the source memory's `project` property. Links are only
  written between `:base` nodes sharing the same `project`.
- **Opt-in**: `ASSOC_CROSS_PROJECT_ENABLED=True` lifts the scope and
  allows cross-project edges. This flag is off by default and is the
  only ASSOC flag whose surface area is "scoping" rather than "writer
  subsystem".
- **Entity node key**: `(project, canon_entity(raw))`. This is already
  locked in the v2 plan; the audit below confirms it is implementable
  against the live data.

## Live audit (2026-04-13, bolt://localhost:7687)

Query executed:

```cypher
MATCH (n:base)
WITH count(n) AS total, count(n.project) AS with_project
RETURN total, with_project, total - with_project AS without_project
```

Result:

| metric          | value |
| --------------- | ----: |
| total `:base`   |   825 |
| with `project`  |   823 |
| without `project` | 2 |

**`count(n.project)` counts non-null property values** under Neo4j
Cypher semantics, so the two "without_project" nodes either have no
`project` property at all or have it set to `null`.

Distinct `project` values found (6):

| project              | `:base` count |
| -------------------- | ------------: |
| `nova-core`          |           612 |
| `novatrade`          |           191 |
| `novacore`           |            13 |
| `fusion-memory`      |             5 |
| `nova-link`          |             1 |
| `novacore-autonomy`  |             1 |

Notes on the value set:

- Both `nova-core` (612 nodes) and `novacore` (13 nodes) appear as
  distinct values. They almost certainly refer to the same logical
  project, with the difference being a stylistic write-time
  inconsistency ("nova-core" vs. "novacore"). Phase 1 linkers must
  NOT silently merge these — if the v2 plan wants them fused, the
  fusion must be an explicit backfill migration with its own
  review/rollback path, not an implicit normalization inside the
  linker.
- `novacore-autonomy` (1 node) and `nova-link` (1 node) look like
  one-off project strings; they should not break Phase 1 Cypher but
  are worth noting for anyone inspecting the first similarity-link
  snapshot.

## Cross-talk against the Sprint 2 invariant

Sprint 2 audit reported `:base = 824`. The Sprint 3 audit reports
`:base = 825`. The difference is +1 (a single new memory was written
between the two audit runs), not a test-leak: the Sprint 3 regression
test (`tests/test_assoc_zero_regression.py`) is fully hermetic and
does not touch Neo4j at all. The rollback test (`tests/test_assoc_rollback.py`)
uses `:AssocRollbackTestNode` and cleans up in a finally block — the
post-audit check confirms zero leftover `:AssocRollbackTestNode` nodes
and zero `:ZeroRegressionTestNode` nodes in the live DB.

## Implications for Phase 1

### 1. Linker scoping

`SimilarityLinker` and `EntityLinker` must read `project` from the source
memory and scope their queries by it. A naive first pass of the Cypher
looks like:

```cypher
MATCH (src:base {entity_id: $src_id})
MATCH (dst:base)
WHERE dst.entity_id <> src.entity_id
  AND (
        $cross_project_enabled = true
        OR (src.project IS NOT NULL AND dst.project = src.project)
      )
WITH src, dst, ... // similarity scoring
...
```

The `cross_project_enabled` parameter is bound from
`settings.ASSOC_CROSS_PROJECT_ENABLED`. Phase 1 must also decide whether
a source node that has `project IS NULL` is allowed to link at all when
`cross_project_enabled=False`; the safest Phase 1 default is **no**
(skip linking for projectless sources) and log a warning. An explicit
backfill-or-default pass is in scope for Phase 1 pre-flight.

### 2. Gotcha: 2 base nodes have no `project` property

These nodes will be invisible to scoped linkers. Phase 1 has two
acceptable options:

- **Option A — backfill**: pick a default project (`"novacore"` based
  on the existing dominant-by-memory-type ownership) and write it
  into the 2 untagged nodes once, before Phase 1 linker rollout. This
  is the preferred option because it makes the `project` property a
  true invariant going forward.
- **Option B — treat missing as unlinked**: Phase 1 linkers simply skip
  source nodes with `project IS NULL`. Cheaper; zero data migration; at
  the cost of two permanently-unlinkable memories.

Recommendation: **Option A.** The backfill is a one-shot 2-node UPDATE
with trivial review surface, and it lets Phase 1 Cypher assume
`project IS NOT NULL` on every `:base` node going forward.

### 3. Entity node key `(project, canon_entity(raw))` is implementable

Since 823/825 base nodes carry `project`, and the remaining 2 can be
backfilled under Option A, the v2 plan's `(project, canon_entity)`
entity key is implementable against the live data. The Sprint 2 unique
constraint on `(:base, entity_id)` is still the memory-node key —
entity nodes are a new label (Phase 2) and will need their own
constraint, likely `UNIQUE (:Entity { project, name })`, which Phase 2
is responsible for declaring.

### 4. Observability hook

Phase 1 should log the scoped-vs-cross-project decision on every link
write:

```
ASSOC_LINK write_id=... linker=similarity src_project=nova-core dst_project=nova-core cross_project=false
```

so a reviewer can confirm after the fact that no link crossed a project
boundary while `ASSOC_CROSS_PROJECT_ENABLED=False`.

## Summary for the Phase 0 close-out

- Cross-project isolation is the correct default — 6 distinct project
  values are in play, `nova-core` + `novatrade` are the two dominant
  silos, and there is no operational reason to let them bleed into
  each other by default.
- The `project` property is present on 99.76% of `:base` nodes (823/825);
  Phase 1 has a trivial backfill option for the remaining 2.
- The `ASSOC_CROSS_PROJECT_ENABLED` flag (added in Sprint 1) is the
  correct single-switch escape hatch for the rare operator-initiated
  cross-project traversal.
- No schema change is required to make the v2 plan's entity key
  `(project, canon_entity)` work on the live data.

## Verification snippet

```bash
# Host-side read-only check (re-runnable)
python3 - <<'PY'
from neo4j import GraphDatabase
d = GraphDatabase.driver("bolt://localhost:7687", auth=None)
d.verify_connectivity()
with d.session(database="neo4j") as s:
    r = s.run(
        "MATCH (n:base) "
        "WITH count(n) AS total, count(n.project) AS with_project "
        "RETURN total, with_project, total - with_project AS without_project"
    ).single()
    print(dict(r))
    ps = [row["p"] for row in s.run(
        "MATCH (n:base) WHERE n.project IS NOT NULL "
        "RETURN DISTINCT n.project AS p ORDER BY p"
    )]
    print(ps)
d.close()
PY
```
