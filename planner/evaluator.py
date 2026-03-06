"""Evaluator — deterministic execution quality grading.

Scores each step and the overall plan on a 0.0–1.0 scale, then maps
to a letter grade (A/B/C/D/F).

Scoring formula (per step):
    total_score = clamp(
        execution_base      (0.40 if success, else 0.00)
      + contract_bonus       (0.25 if contract valid, else 0.00)
      + verification_score   (0.00–0.20, evidence of verification)
      + duration_score        (0.00–0.15, bounded by speed)
      - retry_penalty         (0.05 per retry, max 0.15)
    , 0.0, 1.0)

Grade boundaries:
    A: 0.90 – 1.00
    B: 0.75 – <0.90
    C: 0.60 – <0.75
    D: 0.40 – <0.60
    F: <0.40
"""

from __future__ import annotations

from planner.schemas import (
    ExecutionEvaluation,
    ExecutionPlan,
    PlanEvaluation,
    PlanStep,
    StepResult,
)

# ---------------------------------------------------------------------------
# Scoring weights — sum to 1.0 for a perfect execution
# ---------------------------------------------------------------------------
_EXECUTION_BASE = 0.40       # awarded if execution succeeded
_CONTRACT_BONUS = 0.25       # awarded if contract is valid
_VERIFICATION_MAX = 0.20     # awarded based on verification evidence
_DURATION_MAX = 0.15         # awarded based on execution speed
_RETRY_PENALTY_PER = 0.05   # penalty per retry attempt
_RETRY_PENALTY_CAP = 0.15   # maximum total retry penalty

# Duration thresholds (milliseconds)
_DURATION_FAST = 1000        # < 1s   → full duration score (0.15)
_DURATION_MEDIUM = 5000      # < 5s   → 2/3 duration score  (0.10)
_DURATION_SLOW = 30000       # < 30s  → 1/3 duration score  (0.05)
                             # >= 30s → 0.00

# Grade boundaries
_GRADE_A = 0.90
_GRADE_B = 0.75
_GRADE_C = 0.60
_GRADE_D = 0.40


class Evaluator:
    """Deterministic execution quality evaluator."""

    def evaluate_step(
        self,
        step: PlanStep,
        result: StepResult,
        duration_ms: int = 0,
    ) -> ExecutionEvaluation:
        """Evaluate a single step's execution quality.

        Args:
            step: The plan step that was executed.
            result: The execution result.
            duration_ms: Measured wall-clock duration in milliseconds.

        Returns:
            ExecutionEvaluation with explicit scoring breakdown.
        """
        reasons: list[str] = []

        # 1. Execution success
        execution_success = result.status == "success"
        exec_base = _EXECUTION_BASE if execution_success else 0.0
        reasons.append(
            "execution_success" if execution_success else "execution_failed"
        )

        # 2. Contract validity — directly from StepResult
        contract_valid = result.contract_valid is True
        contract_bonus = _CONTRACT_BONUS if contract_valid else 0.0
        reasons.append(
            "contract_valid" if contract_valid else "contract_invalid"
        )

        # 3. Tests passed — deterministic inference
        #    True  if contract valid (contract requires "verification" field)
        #    False if execution failed/errored/skipped
        #    None  if success but contract not valid (indeterminate)
        if contract_valid:
            tests_passed: bool | None = True
        elif result.status in ("failed", "error", "skipped"):
            tests_passed = False
        else:
            tests_passed = None
        if tests_passed is not None:
            reasons.append(f"tests_passed:{tests_passed}")

        # 4. Verification score
        verification_score = _compute_verification_score(
            contract_valid, tests_passed
        )
        reasons.append(f"verification_score:{verification_score:.2f}")

        # 5. Duration score
        duration_score = _compute_duration_score(duration_ms)
        reasons.append(f"duration_score:{duration_score:.2f}")

        # 6. Retry penalty
        retry_penalty = min(
            result.retry_count * _RETRY_PENALTY_PER, _RETRY_PENALTY_CAP
        )
        if retry_penalty > 0:
            reasons.append(f"retry_penalty_applied:{retry_penalty:.2f}")

        # Total score (clamped 0.0–1.0)
        raw = exec_base + contract_bonus + verification_score \
            + duration_score - retry_penalty
        total_score = max(0.0, min(1.0, round(raw, 2)))

        # Grade
        grade = _score_to_grade(total_score)
        reasons.append(f"grade:{grade}")

        return ExecutionEvaluation(
            step_id=result.step_id,
            execution_success=execution_success,
            contract_valid=contract_valid,
            tests_passed=tests_passed,
            retry_penalty=retry_penalty,
            duration_score=duration_score,
            verification_score=verification_score,
            total_score=total_score,
            grade=grade,
            reasons=reasons,
        )

    def evaluate_plan(
        self,
        plan: ExecutionPlan,
        step_results: list[StepResult],
        step_durations: dict[str, int] | None = None,
    ) -> PlanEvaluation:
        """Evaluate a complete plan's execution quality.

        Args:
            plan: The execution plan.
            step_results: Results from each executed step.
            step_durations: Per-step duration in milliseconds (keyed by step_id).

        Returns:
            PlanEvaluation with aggregate score, grade, and followup guidance.
        """
        durations = step_durations or {}

        # Evaluate each step
        step_evals: list[ExecutionEvaluation] = []
        for result in step_results:
            # Find matching step (for the PlanStep argument)
            step = _find_step(plan, result.step_id)
            dur = durations.get(result.step_id, 0)
            step_evals.append(self.evaluate_step(step, result, dur))

        # Aggregate score: mean of step scores (0.0 if no steps)
        if step_evals:
            aggregate_score = round(
                sum(e.total_score for e in step_evals) / len(step_evals), 2
            )
        else:
            aggregate_score = 0.0

        grade = _score_to_grade(aggregate_score)

        # Summary
        n_total = len(step_evals)
        n_succeeded = sum(1 for e in step_evals if e.execution_success)
        n_valid = sum(1 for e in step_evals if e.contract_valid)
        summary = (
            f"{n_succeeded}/{n_total} steps succeeded, "
            f"{n_valid}/{n_total} contracts valid, "
            f"aggregate {grade} ({aggregate_score:.2f})"
        )

        # Follow-up recommendation
        followup_recommended, followup_reason = _determine_followup(
            grade, step_evals
        )

        return PlanEvaluation(
            plan_id=plan.plan_id,
            step_evaluations=step_evals,
            aggregate_score=aggregate_score,
            grade=grade,
            summary=summary,
            followup_recommended=followup_recommended,
            followup_reason=followup_reason,
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _compute_verification_score(
    contract_valid: bool, tests_passed: bool | None
) -> float:
    """Compute verification score from contract validity and test evidence.

    - 0.20 if contract valid (contract requires verification field)
    - 0.10 if tests_passed is True but contract not valid
    - 0.00 otherwise
    Uses max of the two signals, capped at _VERIFICATION_MAX.
    """
    contract_signal = _VERIFICATION_MAX if contract_valid else 0.0
    tests_signal = _VERIFICATION_MAX / 2 if tests_passed else 0.0
    return min(max(contract_signal, tests_signal), _VERIFICATION_MAX)


def _compute_duration_score(duration_ms: int) -> float:
    """Compute duration score from measured milliseconds.

    Fast (<1s): 0.15, Medium (<5s): 0.10, Slow (<30s): 0.05, Very slow: 0.00
    """
    if duration_ms < _DURATION_FAST:
        return _DURATION_MAX
    elif duration_ms < _DURATION_MEDIUM:
        return round(_DURATION_MAX * 2 / 3, 2)
    elif duration_ms < _DURATION_SLOW:
        return round(_DURATION_MAX / 3, 2)
    else:
        return 0.0


def _score_to_grade(score: float) -> str:
    """Map a 0.0–1.0 score to a letter grade."""
    if score >= _GRADE_A:
        return "A"
    elif score >= _GRADE_B:
        return "B"
    elif score >= _GRADE_C:
        return "C"
    elif score >= _GRADE_D:
        return "D"
    else:
        return "F"


def _find_step(plan: ExecutionPlan, step_id: str) -> PlanStep:
    """Find a PlanStep by step_id, or return a placeholder."""
    for s in plan.steps:
        if s.step_id == step_id:
            return s
    # Fallback: return a minimal placeholder so evaluation doesn't crash
    return PlanStep(step_id=step_id, skill_name="unknown", goal="unknown")


def _determine_followup(
    grade: str,
    step_evals: list[ExecutionEvaluation],
) -> tuple[bool, str | None]:
    """Determine if a follow-up task should be recommended.

    Rules:
    - A grade, all steps succeeded with valid contracts → no followup
    - A grade, any step had issues → followup (edge case)
    - B or C grade, any contract invalid → followup
    - D or F grade → always followup
    """
    if not step_evals:
        return False, None

    any_contract_invalid = any(not e.contract_valid for e in step_evals)
    any_failed = any(not e.execution_success for e in step_evals)
    n_invalid = sum(1 for e in step_evals if not e.contract_valid)

    if grade == "A" and not any_contract_invalid and not any_failed:
        return False, None

    if grade in ("D", "F"):
        return True, (
            f"Low grade {grade}: execution quality below acceptable threshold"
        )

    if grade in ("B", "C"):
        if any_contract_invalid:
            return True, (
                f"Grade {grade}: {n_invalid} step(s) with invalid contracts"
            )
        if any_failed:
            return True, f"Grade {grade}: step execution failure detected"
        return False, None

    # grade A with edge-case issues
    if any_contract_invalid or any_failed:
        return True, (
            f"Grade A with issues: "
            f"{n_invalid} invalid contract(s), "
            f"{sum(1 for e in step_evals if not e.execution_success)} failure(s)"
        )

    return False, None
