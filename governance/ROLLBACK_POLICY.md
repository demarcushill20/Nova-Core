# Rollback Policy

## Rollback Triggers

A rollback should be performed when:

1. A promoted change causes production regressions observed in use
2. A subsequent batch run detects baseline degradation
3. An operator identifies an issue with an accepted change
4. A post-promotion smoke test fails

## Rollback Mechanism

All changes are promoted via git feature branches with structured commits.
Rollback uses `git revert` on the specific commit(s) from a run.

### By Run ID

```bash
python3 -m tools.skill_optimizer.rollback --run-id <run_id>
```

This will:
1. Find all commits associated with the run ID
2. Create a revert commit for each
3. Create a rollback branch `rollback/<run_id>`

### By Skill Name

```bash
python3 -m tools.skill_optimizer.rollback --skill <skill_name> --to-version <version>
```

This will:
1. Find the specific skill's description at the target version
2. Revert to that description
3. Create a rollback branch

## Rollback Artifacts

Every rollback produces:
- `benchmarks/rollbacks/<run_id>/rollback_manifest.json`
- Updated `reports/regression_watchlist.md`
- Log entry in `LOGS/skill_optimizer.jsonl`

## Prevention

The best rollback is one that never happens:
- Conservative thresholds prevent most bad promotions
- Dry-run and propose modes allow manual review before promotion
- Global smoke tests catch ecosystem-level regressions
- Holdout test sets prevent overfitting

## Authority

Any operator can trigger a rollback. No approval needed — safety first.
