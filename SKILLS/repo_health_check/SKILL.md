# Skill: repo_health_check

## Purpose
Inspect NovaCore execution health by analyzing plan evaluations, contract compliance, and recent execution history. Produces deterministic `HealthFinding` artifacts that feed the bounded self-improvement loop.

## When To Use
- After a plan completes execution (via orchestrator)
- During periodic health sweeps
- When the supervisor recommends follow-up investigation

## Workflow
1. **Collect inputs** — receive a `PlanEvaluation` and optionally recent plan state dicts from `STATE/plans/`.
2. **Grade check** — if aggregate grade is B, C, D, or F, emit a `low_grade_execution` finding with deterministic severity mapped from `_GRADE_SEVERITY`.
3. **Contract scan** — iterate step evaluations; collect steps with `contract_valid=False`. Emit `repeated_contract_failure` finding if any exist (severity: `high` if >1 failure, else `medium`).
4. **Retry scan** — collect steps with `retry_penalty > 0`. Emit `repeated_retry_pattern` finding (severity: `medium`).
5. **Duration scan** — collect successful steps with `duration_score == 0.0`. Emit `slow_execution` finding (severity: `low`).
6. **Verification scan** — collect successful steps with `verification_score < 0.10`. Emit `verification_weakness` finding (severity: `medium`).
7. **Cross-plan scan** — if `recent_plan_states` provided, count plans with grade D or F. If >= 2, emit systemic `low_grade_execution` finding (severity: `critical`).
8. **Return findings** — return list of `HealthFinding` dataclasses, each with unique `finding_id`.

## Tool Usage Rules
- Read-only inspection — does not modify code or state
- All findings derived from explicit evidence (plan states, audit records)
- No heuristic or probabilistic scoring
- Categories are fixed: `low_grade_execution`, `repeated_contract_failure`, `repeated_retry_pattern`, `slow_execution`, `verification_weakness`
- Severity is deterministic and constrained to exactly four values: `low`, `medium`, `high`, `critical`
  - Grade F → `critical`
  - Grade D → `high`
  - Grade C → `medium`
  - Grade B → `low`
  - Multiple contract failures → `high`; single → `medium`
  - Retry pattern → `medium`
  - Slow execution → `low`
  - Verification weakness → `medium`
  - Systemic cross-plan failure → `critical`

## Inputs
- `plan_evaluation`: A `PlanEvaluation` dataclass from the evaluator
- `recent_plan_states` (optional): List of recent plan state dicts from `STATE/plans/`

## Verification
1. Every returned `HealthFinding` has a non-empty `finding_id`, `category`, `severity`, and `summary`.
2. All `finding_id` values are unique within a single invocation.
3. `severity` is always one of `low`, `medium`, `high`, `critical` (enforced by `HealthFinding.__post_init__`).
4. `category` is always one of the five fixed categories.
5. `evidence` list is non-empty for every finding.
6. No findings are generated for grade-A plans with no step-level issues.

## Failure Handling
- **Missing plan_evaluation**: raise `ValueError` — do not return empty findings silently.
- **Malformed step evaluations**: skip the individual step, log a warning, continue scanning remaining steps.
- **Invalid severity value**: `HealthFinding.__post_init__` raises `ValueError` immediately — this is a code bug that must be fixed, not silenced.
- **Empty recent_plan_states**: treat as no cross-plan data; skip cross-plan scan without error.

## Output Contract

```
## CONTRACT
summary: <one-line description of health check results>
findings_count: <int>
critical_findings: <int>
high_findings: <int>
categories: <comma-separated list of finding categories>
verification: findings derived from explicit evidence only
confidence: high
```

## Activation Rules
- Trigger: plan evaluation completed with grade B or lower
- Trigger: supervisor recommends follow-up
- Trigger: periodic health sweep (via watcher)

## Bounds
- No file modifications
- No git operations
- No network access required
- Output is a list of `HealthFinding` dataclasses
