"""Tests for planner.evaluator — deterministic execution quality grading."""

import pytest

from planner.evaluator import (
    Evaluator,
    _compute_duration_score,
    _compute_verification_score,
    _score_to_grade,
    _EXECUTION_BASE,
    _CONTRACT_BONUS,
    _VERIFICATION_MAX,
    _DURATION_MAX,
    _RETRY_PENALTY_PER,
)
from planner.schemas import (
    ExecutionEvaluation,
    ExecutionPlan,
    PlanEvaluation,
    PlanStep,
    StepResult,
)


@pytest.fixture
def evaluator() -> Evaluator:
    return Evaluator()


def _step(step_id: str = "s1", skill: str = "code_improve") -> PlanStep:
    return PlanStep(step_id=step_id, skill_name=skill, goal="test goal")


def _result(
    step_id: str = "s1",
    status: str = "success",
    contract_valid: bool = True,
    validation_errors: list[str] | None = None,
    retry_count: int = 0,
) -> StepResult:
    return StepResult(
        step_id=step_id,
        status=status,
        contract_valid=contract_valid,
        validation_errors=validation_errors or [],
        retry_count=retry_count,
    )


def _plan(
    plan_id: str = "plan_t001",
    task_id: str = "t001",
    steps: list[PlanStep] | None = None,
) -> ExecutionPlan:
    if steps is None:
        steps = [_step("s1"), _step("s2", "system_supervisor")]
    return ExecutionPlan(
        plan_id=plan_id,
        task_id=task_id,
        strategy="multi_skill",
        steps=steps,
        success_criteria=["contract valid"],
    )


# =============================================================================
# Step evaluation
# =============================================================================


# -- high-score valid step ----------------------------------------------------


def test_perfect_step_scores_1(evaluator: Evaluator):
    """Success + valid contract + fast execution + no retries → 1.0."""
    step = _step()
    result = _result(status="success", contract_valid=True, retry_count=0)
    ev = evaluator.evaluate_step(step, result, duration_ms=100)
    assert ev.total_score == 1.0
    assert ev.grade == "A"
    assert ev.execution_success is True
    assert ev.contract_valid is True
    assert ev.tests_passed is True
    assert ev.retry_penalty == 0.0


def test_perfect_step_has_all_fields(evaluator: Evaluator):
    step = _step()
    result = _result()
    ev = evaluator.evaluate_step(step, result, duration_ms=100)
    assert isinstance(ev, ExecutionEvaluation)
    assert hasattr(ev, "step_id")
    assert hasattr(ev, "execution_success")
    assert hasattr(ev, "contract_valid")
    assert hasattr(ev, "tests_passed")
    assert hasattr(ev, "retry_penalty")
    assert hasattr(ev, "duration_score")
    assert hasattr(ev, "verification_score")
    assert hasattr(ev, "total_score")
    assert hasattr(ev, "grade")
    assert hasattr(ev, "reasons")


# -- retry penalty lowers score -----------------------------------------------


def test_one_retry_lowers_score(evaluator: Evaluator):
    step = _step()
    no_retry = evaluator.evaluate_step(
        step, _result(retry_count=0), duration_ms=100
    )
    one_retry = evaluator.evaluate_step(
        step, _result(retry_count=1), duration_ms=100
    )
    assert one_retry.total_score < no_retry.total_score
    assert one_retry.retry_penalty == _RETRY_PENALTY_PER


def test_two_retries_penalty(evaluator: Evaluator):
    step = _step()
    ev = evaluator.evaluate_step(
        step, _result(retry_count=2), duration_ms=100
    )
    assert ev.retry_penalty == 2 * _RETRY_PENALTY_PER
    assert ev.total_score == round(1.0 - 2 * _RETRY_PENALTY_PER, 2)


def test_retry_penalty_capped(evaluator: Evaluator):
    """Retry penalty should not exceed cap even with high retry count."""
    step = _step()
    ev = evaluator.evaluate_step(
        step, _result(retry_count=10), duration_ms=100
    )
    assert ev.retry_penalty == 0.15  # capped
    assert "retry_penalty_applied:0.15" in ev.reasons


# -- invalid contract lowers score --------------------------------------------


def test_invalid_contract_lowers_score(evaluator: Evaluator):
    step = _step()
    valid = evaluator.evaluate_step(
        step, _result(contract_valid=True), duration_ms=100
    )
    invalid = evaluator.evaluate_step(
        step, _result(contract_valid=False), duration_ms=100
    )
    assert invalid.total_score < valid.total_score
    assert invalid.contract_valid is False
    assert "contract_invalid" in invalid.reasons


def test_invalid_contract_loses_contract_bonus_and_verification(
    evaluator: Evaluator,
):
    step = _step()
    ev = evaluator.evaluate_step(
        step, _result(contract_valid=False), duration_ms=100
    )
    # Should lose _CONTRACT_BONUS (0.25) and _VERIFICATION_MAX (0.20)
    expected = _EXECUTION_BASE + _DURATION_MAX  # 0.40 + 0.15 = 0.55
    assert ev.total_score == expected


# -- failed execution --------------------------------------------------------


def test_failed_execution_score(evaluator: Evaluator):
    step = _step()
    ev = evaluator.evaluate_step(
        step, _result(status="failed", contract_valid=False), duration_ms=100
    )
    assert ev.execution_success is False
    assert ev.tests_passed is False
    assert ev.total_score == _DURATION_MAX  # only duration score
    assert ev.grade == "F"


def test_skipped_step_score(evaluator: Evaluator):
    step = _step()
    ev = evaluator.evaluate_step(
        step, _result(status="skipped", contract_valid=False), duration_ms=0
    )
    assert ev.execution_success is False
    assert ev.tests_passed is False
    assert ev.total_score <= _DURATION_MAX


# -- grade boundaries --------------------------------------------------------


def test_grade_A(evaluator: Evaluator):
    """Score >= 0.90 → A."""
    step = _step()
    # Perfect step: 1.0
    ev = evaluator.evaluate_step(step, _result(), duration_ms=100)
    assert ev.grade == "A"


def test_grade_B(evaluator: Evaluator):
    """Score 0.75–0.89 → B."""
    step = _step()
    # Success + valid + fast + 3 retries: 1.0 - 0.15 = 0.85
    ev = evaluator.evaluate_step(
        step, _result(retry_count=3), duration_ms=100
    )
    assert ev.grade == "B"


def test_grade_C(evaluator: Evaluator):
    """Score 0.60–0.74 → C."""
    step = _step()
    # Success + invalid contract + fast: 0.40 + 0.15 = 0.55... that's D
    # Let's use success + invalid contract + tests_passed=None + fast + 0 retries = 0.55 → D
    # For C: need ~0.65. Success + valid + slow (>=30s): 0.40 + 0.25 + 0.20 + 0.00 = 0.85 → B
    # Success + valid + slow + 2 retries: 0.85 - 0.10 = 0.75 → B (boundary)
    # Success + valid + slow + 3 retries: 0.85 - 0.15 = 0.70 → C
    ev = evaluator.evaluate_step(
        step, _result(retry_count=3), duration_ms=31000
    )
    assert ev.grade == "C"


def test_grade_D(evaluator: Evaluator):
    """Score 0.40–0.59 → D."""
    step = _step()
    # Success + invalid contract + fast: 0.40 + 0.15 = 0.55
    ev = evaluator.evaluate_step(
        step,
        _result(status="success", contract_valid=False),
        duration_ms=100,
    )
    assert ev.grade == "D"


def test_grade_F(evaluator: Evaluator):
    """Score < 0.40 → F."""
    step = _step()
    ev = evaluator.evaluate_step(
        step,
        _result(status="failed", contract_valid=False, retry_count=2),
        duration_ms=100,
    )
    assert ev.grade == "F"


# -- score bounded 0.0–1.0 ---------------------------------------------------


def test_score_bounded_at_zero(evaluator: Evaluator):
    """Score should not go below 0.0 even with heavy penalties."""
    step = _step()
    ev = evaluator.evaluate_step(
        step,
        _result(status="failed", contract_valid=False, retry_count=10),
        duration_ms=100000,
    )
    assert ev.total_score >= 0.0


def test_score_bounded_at_one(evaluator: Evaluator):
    """Score should not exceed 1.0."""
    step = _step()
    ev = evaluator.evaluate_step(
        step, _result(status="success", contract_valid=True), duration_ms=0
    )
    assert ev.total_score <= 1.0


# -- reasons list populated --------------------------------------------------


def test_reasons_populated(evaluator: Evaluator):
    step = _step()
    ev = evaluator.evaluate_step(step, _result(), duration_ms=100)
    assert len(ev.reasons) > 0
    assert any("execution_success" in r for r in ev.reasons)
    assert any("contract_valid" in r for r in ev.reasons)
    assert any("duration_score" in r for r in ev.reasons)
    assert any("verification_score" in r for r in ev.reasons)
    assert any("grade:" in r for r in ev.reasons)


def test_reasons_include_retry_penalty_when_applied(evaluator: Evaluator):
    step = _step()
    ev = evaluator.evaluate_step(
        step, _result(retry_count=1), duration_ms=100
    )
    assert any("retry_penalty_applied" in r for r in ev.reasons)


def test_reasons_no_retry_penalty_when_none(evaluator: Evaluator):
    step = _step()
    ev = evaluator.evaluate_step(
        step, _result(retry_count=0), duration_ms=100
    )
    assert not any("retry_penalty_applied" in r for r in ev.reasons)


# -- tests_passed inference ---------------------------------------------------


def test_tests_passed_true_when_contract_valid(evaluator: Evaluator):
    step = _step()
    ev = evaluator.evaluate_step(
        step, _result(contract_valid=True), duration_ms=100
    )
    assert ev.tests_passed is True


def test_tests_passed_false_when_failed(evaluator: Evaluator):
    step = _step()
    ev = evaluator.evaluate_step(
        step, _result(status="failed", contract_valid=False), duration_ms=100
    )
    assert ev.tests_passed is False


def test_tests_passed_none_when_indeterminate(evaluator: Evaluator):
    step = _step()
    ev = evaluator.evaluate_step(
        step,
        _result(status="success", contract_valid=False),
        duration_ms=100,
    )
    assert ev.tests_passed is None


# -- duration score -----------------------------------------------------------


def test_duration_fast():
    assert _compute_duration_score(500) == _DURATION_MAX


def test_duration_medium():
    score = _compute_duration_score(3000)
    assert 0 < score < _DURATION_MAX


def test_duration_slow():
    score = _compute_duration_score(15000)
    assert 0 < score < _compute_duration_score(3000)


def test_duration_very_slow():
    assert _compute_duration_score(35000) == 0.0


# -- verification score -------------------------------------------------------


def test_verification_contract_valid():
    assert _compute_verification_score(True, True) == _VERIFICATION_MAX


def test_verification_tests_only():
    score = _compute_verification_score(False, True)
    assert score == _VERIFICATION_MAX / 2


def test_verification_nothing():
    assert _compute_verification_score(False, None) == 0.0


# -- grade mapping ------------------------------------------------------------


def test_score_to_grade_boundaries():
    assert _score_to_grade(1.0) == "A"
    assert _score_to_grade(0.90) == "A"
    assert _score_to_grade(0.89) == "B"
    assert _score_to_grade(0.75) == "B"
    assert _score_to_grade(0.74) == "C"
    assert _score_to_grade(0.60) == "C"
    assert _score_to_grade(0.59) == "D"
    assert _score_to_grade(0.40) == "D"
    assert _score_to_grade(0.39) == "F"
    assert _score_to_grade(0.0) == "F"


# =============================================================================
# Plan evaluation
# =============================================================================


def test_plan_evaluation_aggregate(evaluator: Evaluator):
    """Plan aggregate score is mean of step scores."""
    plan = _plan(steps=[_step("s1"), _step("s2")])
    results = [
        _result("s1", status="success", contract_valid=True),
        _result("s2", status="success", contract_valid=True),
    ]
    pe = evaluator.evaluate_plan(plan, results, {"s1": 100, "s2": 100})
    assert pe.aggregate_score == 1.0
    assert pe.grade == "A"
    assert len(pe.step_evaluations) == 2


def test_plan_evaluation_mixed_scores(evaluator: Evaluator):
    """Mixed step results produce intermediate aggregate."""
    plan = _plan(steps=[_step("s1"), _step("s2")])
    results = [
        _result("s1", status="success", contract_valid=True),
        _result("s2", status="failed", contract_valid=False),
    ]
    pe = evaluator.evaluate_plan(plan, results, {"s1": 100, "s2": 100})
    assert 0.0 < pe.aggregate_score < 1.0
    assert pe.followup_recommended is True


def test_plan_evaluation_empty(evaluator: Evaluator):
    """Empty plan produces 0.0 aggregate."""
    plan = _plan(steps=[])
    pe = evaluator.evaluate_plan(plan, [], {})
    assert pe.aggregate_score == 0.0
    assert pe.grade == "F"


def test_plan_evaluation_has_all_fields(evaluator: Evaluator):
    plan = _plan(steps=[_step("s1")])
    results = [_result("s1")]
    pe = evaluator.evaluate_plan(plan, results, {"s1": 100})
    assert isinstance(pe, PlanEvaluation)
    assert isinstance(pe.plan_id, str)
    assert isinstance(pe.step_evaluations, list)
    assert isinstance(pe.aggregate_score, float)
    assert isinstance(pe.grade, str)
    assert isinstance(pe.summary, str)
    assert isinstance(pe.followup_recommended, bool)


def test_plan_evaluation_summary_format(evaluator: Evaluator):
    plan = _plan(steps=[_step("s1"), _step("s2")])
    results = [
        _result("s1", status="success", contract_valid=True),
        _result("s2", status="success", contract_valid=True),
    ]
    pe = evaluator.evaluate_plan(plan, results, {"s1": 100, "s2": 100})
    assert "2/2 steps succeeded" in pe.summary
    assert "2/2 contracts valid" in pe.summary


# -- followup recommendation thresholds --------------------------------------


def test_no_followup_for_perfect_plan(evaluator: Evaluator):
    plan = _plan(steps=[_step("s1")])
    results = [_result("s1", status="success", contract_valid=True)]
    pe = evaluator.evaluate_plan(plan, results, {"s1": 100})
    assert pe.followup_recommended is False
    assert pe.followup_reason is None


def test_followup_for_failed_plan(evaluator: Evaluator):
    plan = _plan(steps=[_step("s1")])
    results = [_result("s1", status="failed", contract_valid=False)]
    pe = evaluator.evaluate_plan(plan, results, {"s1": 100})
    assert pe.followup_recommended is True
    assert pe.followup_reason is not None


def test_followup_for_invalid_contract_plan(evaluator: Evaluator):
    """Plan with success but invalid contract → followup recommended."""
    plan = _plan(steps=[_step("s1")])
    results = [_result("s1", status="success", contract_valid=False)]
    pe = evaluator.evaluate_plan(plan, results, {"s1": 100})
    # Grade D (0.55) → followup recommended
    assert pe.followup_recommended is True


def test_plan_evaluation_with_durations(evaluator: Evaluator):
    """Step durations are used in scoring."""
    plan = _plan(steps=[_step("s1")])
    results = [_result("s1", status="success", contract_valid=True)]
    fast = evaluator.evaluate_plan(plan, results, {"s1": 100})
    slow = evaluator.evaluate_plan(plan, results, {"s1": 35000})
    assert fast.aggregate_score > slow.aggregate_score
