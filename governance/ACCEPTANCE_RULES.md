# Acceptance Rules

## Hard Reject Conditions

A candidate is **immediately rejected** if ANY of these conditions are true:

1. **Precision below floor:** Precision < threshold for skill's risk level
   - Low risk: 0.70
   - Medium risk: 0.80
   - High risk: 0.90
   - Critical risk: N/A (frozen)

2. **FPR regression:** False positive rate increased by more than the allowed delta
   - Default: +0.05
   - High risk: +0.02

3. **Neighbor conflict regression:** Neighbor conflict rate exceeds limit
   - Default: 0.10
   - High risk: 0.05

4. **Smoke regression:** Any global smoke test regression (zero tolerance)

5. **Insufficient gain:** Weighted score improvement < minimum threshold
   - Default: +0.02
   - High risk: +0.05

6. **Policy violation:** Description contains disallowed patterns (catch-all phrases)

7. **Anti-broadening failure:** Candidate triggers for significantly more
   unique queries than baseline without corresponding hard-negative improvement

8. **Description too long:** Exceeds 1024 characters

9. **Dataset validation failure:** Candidate's dataset doesn't pass validation

## Soft Signals (logged but not blocking)

- Paraphrase consistency drop
- Recall decrease (if precision improves enough)
- Description length increase without metric improvement
- High variance across runs

## Acceptance Criteria

A candidate is accepted if:
1. No hard reject conditions are triggered
2. Weighted score improves over baseline by at least the minimum threshold
3. Test set performance does not regress (holdout discipline)

## Reason Codes

| Code | Meaning |
|------|---------|
| ACCEPT | All gates passed, score improved |
| REJECT_PRECISION | Precision below floor |
| REJECT_FPR | FPR regression too high |
| REJECT_NEIGHBOR | Neighbor conflict limit exceeded |
| REJECT_SMOKE | Global smoke regression |
| REJECT_GAIN | Insufficient score improvement |
| REJECT_POLICY | Policy violation detected |
| REJECT_BROADENING | Anti-broadening guard triggered |
| REJECT_LENGTH | Description too long |
| REJECT_DATASET | Dataset validation failure |
| REJECT_VARIANCE | Results too unstable |
| SKIP_FROZEN | Skill is frozen |
| SKIP_NO_DATA | No valid dataset |
