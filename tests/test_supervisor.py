"""Tests for planner.supervisor — post-execution decision engine."""

import pytest

from planner.schemas import (
    ExecutionEvaluation,
    PlanEvaluation,
    PlanStep,
    StepResult,
    SupervisorDecision,
)
from planner.supervisor import Supervisor


@pytest.fixture
def sup() -> Supervisor:
    return Supervisor()


def _step(step_id: str = "s1", skill: str = "code_improve") -> PlanStep:
    return PlanStep(step_id=step_id, skill_name=skill, goal="test goal")


def _result(
    status: str = "success",
    contract_valid: bool = True,
    validation_errors: list[str] | None = None,
    retry_count: int = 0,
    step_id: str = "s1",
) -> StepResult:
    return StepResult(
        step_id=step_id,
        status=status,
        contract_valid=contract_valid,
        validation_errors=validation_errors or [],
        retry_count=retry_count,
    )


# -- continue on valid contract -----------------------------------------------

def test_continue_on_success_and_valid_contract(sup: Supervisor):
    step = _step()
    result = _result(status="success", contract_valid=True)
    decision = sup.evaluate_step(step, result)
    assert decision.action == "continue"
    assert decision.retry_allowed is False


def test_continue_reason_mentions_success(sup: Supervisor):
    step = _step()
    result = _result(status="success", contract_valid=True)
    decision = sup.evaluate_step(step, result)
    assert "success" in decision.reason.lower() or "valid" in decision.reason.lower()


# -- retry on invalid contract under limit ------------------------------------

def test_retry_on_invalid_contract_under_limit(sup: Supervisor):
    step = _step()
    result = _result(status="success", contract_valid=False, retry_count=0)
    decision = sup.evaluate_step(step, result)
    assert decision.action == "retry"
    assert decision.retry_allowed is True


def test_retry_on_failure_under_limit(sup: Supervisor):
    step = _step()
    result = _result(status="failed", contract_valid=False, retry_count=0)
    decision = sup.evaluate_step(step, result)
    assert decision.action == "retry"
    assert decision.retry_allowed is True


def test_retry_on_second_attempt(sup: Supervisor):
    step = _step()
    result = _result(status="failed", contract_valid=False, retry_count=1)
    decision = sup.evaluate_step(step, result)
    assert decision.action == "retry"
    assert decision.retry_allowed is True


# -- escalate after retry limit -----------------------------------------------

def test_escalate_when_retries_exhausted(sup: Supervisor):
    step = _step()
    result = _result(status="failed", contract_valid=False, retry_count=2)
    decision = sup.evaluate_step(step, result)
    assert decision.action == "escalate"
    assert decision.retry_allowed is False


def test_escalate_includes_reason(sup: Supervisor):
    step = _step()
    result = _result(
        status="failed",
        contract_valid=False,
        validation_errors=["module not found"],
        retry_count=2,
    )
    decision = sup.evaluate_step(step, result)
    assert decision.action == "escalate"
    assert "module not found" in decision.reason


# -- anomaly escalation (non-retryable errors) --------------------------------

def test_escalate_on_permission_denied(sup: Supervisor):
    step = _step()
    result = _result(
        status="failed",
        contract_valid=False,
        validation_errors=["Permission denied: /etc/shadow"],
        retry_count=0,
    )
    decision = sup.evaluate_step(step, result)
    assert decision.action == "escalate"
    assert decision.retry_allowed is False


def test_escalate_on_sandbox_violation(sup: Supervisor):
    step = _step()
    result = _result(
        status="failed",
        contract_valid=False,
        validation_errors=["Sandbox violation: path escape"],
        retry_count=0,
    )
    decision = sup.evaluate_step(step, result)
    assert decision.action == "escalate"
    assert decision.retry_allowed is False


def test_escalate_on_timeout(sup: Supervisor):
    step = _step()
    result = _result(
        status="failed",
        contract_valid=False,
        validation_errors=["Timeout after 300s"],
        retry_count=0,
    )
    decision = sup.evaluate_step(step, result)
    assert decision.action == "escalate"


def test_escalate_on_blocked(sup: Supervisor):
    step = _step()
    result = _result(
        status="failed",
        contract_valid=False,
        validation_errors=["Blocked by runner deny pattern"],
        retry_count=0,
    )
    decision = sup.evaluate_step(step, result)
    assert decision.action == "escalate"


# -- follow-up task generation ------------------------------------------------

def test_followup_task_on_failure(sup: Supervisor):
    result = _result(
        status="failed",
        contract_valid=False,
        validation_errors=["persistent failure"],
        retry_count=2,
    )
    ft = sup.generate_followup_task(result)
    assert ft is not None
    assert "title" in ft
    assert "description" in ft
    assert "priority" in ft
    assert ft["priority"] == "high"
    assert ft["source"] == "supervisor"


def test_no_followup_on_success(sup: Supervisor):
    result = _result(status="success", contract_valid=True)
    ft = sup.generate_followup_task(result)
    assert ft is None


def test_followup_includes_step_id(sup: Supervisor):
    result = _result(
        step_id="s42",
        status="failed",
        contract_valid=False,
        retry_count=2,
    )
    ft = sup.generate_followup_task(result)
    assert ft is not None
    assert "s42" in ft["title"]


# -- should_retry helper ------------------------------------------------------

def test_should_retry_false_for_permission_denied(sup: Supervisor):
    result = _result(
        status="failed",
        validation_errors=["Permission denied"],
    )
    assert sup.should_retry(result) is False


def test_should_retry_false_for_sandbox(sup: Supervisor):
    result = _result(
        status="failed",
        validation_errors=["Sandbox violation"],
    )
    assert sup.should_retry(result) is False


def test_should_retry_true_for_contract_invalid(sup: Supervisor):
    result = _result(status="success", contract_valid=False)
    assert sup.should_retry(result) is True


def test_should_retry_true_for_empty_errors(sup: Supervisor):
    result = _result(status="failed", validation_errors=[])
    assert sup.should_retry(result) is True


# -- build_retry_reason -------------------------------------------------------

def test_build_retry_reason_includes_failed(sup: Supervisor):
    result = _result(status="failed", contract_valid=False)
    reason = sup.build_retry_reason(result)
    assert "failed" in reason.lower()


def test_build_retry_reason_includes_contract(sup: Supervisor):
    result = _result(status="success", contract_valid=False)
    reason = sup.build_retry_reason(result)
    assert "contract" in reason.lower()


def test_build_retry_reason_includes_errors(sup: Supervisor):
    result = _result(
        status="failed",
        contract_valid=False,
        validation_errors=["something broke"],
    )
    reason = sup.build_retry_reason(result)
    assert "something broke" in reason


# -- decision shape -----------------------------------------------------------

def test_decision_is_supervisor_decision(sup: Supervisor):
    step = _step()
    result = _result()
    decision = sup.evaluate_step(step, result)
    assert isinstance(decision, SupervisorDecision)
    assert hasattr(decision, "action")
    assert hasattr(decision, "reason")
    assert hasattr(decision, "retry_allowed")


def test_decision_retry_allowed_type(sup: Supervisor):
    step = _step()
    result = _result(status="failed", contract_valid=False, retry_count=0)
    decision = sup.evaluate_step(step, result)
    assert isinstance(decision.retry_allowed, bool)


# -- MAX_RETRIES constant -----------------------------------------------------

def test_max_retries_is_two():
    assert Supervisor.MAX_RETRIES == 2


# =============================================================================
# Phase 5.3 — recommend_followup_from_evaluation
# =============================================================================


def _plan_eval(
    plan_id: str = "plan_t001",
    aggregate_score: float = 1.0,
    grade: str = "A",
    followup_recommended: bool = False,
    followup_reason: str | None = None,
) -> PlanEvaluation:
    return PlanEvaluation(
        plan_id=plan_id,
        step_evaluations=[],
        aggregate_score=aggregate_score,
        grade=grade,
        summary="test summary",
        followup_recommended=followup_recommended,
        followup_reason=followup_reason,
    )


def test_followup_none_for_grade_A(sup: Supervisor):
    """A grade with no issues → no followup."""
    pe = _plan_eval(grade="A", followup_recommended=False)
    assert sup.recommend_followup_from_evaluation(pe) is None


def test_followup_high_for_grade_D(sup: Supervisor):
    """D grade → high-priority followup."""
    pe = _plan_eval(
        grade="D", aggregate_score=0.55, followup_recommended=True
    )
    rec = sup.recommend_followup_from_evaluation(pe)
    assert rec is not None
    assert rec["priority"] == "high"
    assert rec["source"] == "supervisor"


def test_followup_high_for_grade_F(sup: Supervisor):
    """F grade → high-priority followup."""
    pe = _plan_eval(
        grade="F", aggregate_score=0.15, followup_recommended=True
    )
    rec = sup.recommend_followup_from_evaluation(pe)
    assert rec is not None
    assert rec["priority"] == "high"


def test_followup_medium_for_grade_B_with_issues(sup: Supervisor):
    """B grade with followup_recommended → medium-priority followup."""
    pe = _plan_eval(
        grade="B",
        aggregate_score=0.80,
        followup_recommended=True,
        followup_reason="1 contract invalid",
    )
    rec = sup.recommend_followup_from_evaluation(pe)
    assert rec is not None
    assert rec["priority"] == "medium"


def test_followup_medium_for_grade_C_with_issues(sup: Supervisor):
    """C grade with followup_recommended → medium-priority followup."""
    pe = _plan_eval(
        grade="C",
        aggregate_score=0.65,
        followup_recommended=True,
        followup_reason="retries high",
    )
    rec = sup.recommend_followup_from_evaluation(pe)
    assert rec is not None
    assert rec["priority"] == "medium"


def test_followup_none_for_grade_B_no_issues(sup: Supervisor):
    """B grade without followup_recommended → no followup."""
    pe = _plan_eval(grade="B", aggregate_score=0.85, followup_recommended=False)
    assert sup.recommend_followup_from_evaluation(pe) is None


def test_followup_dict_shape_exact(sup: Supervisor):
    """Returned followup dict has exactly the required keys."""
    pe = _plan_eval(
        plan_id="plan_x",
        grade="D",
        aggregate_score=0.45,
        followup_recommended=True,
    )
    rec = sup.recommend_followup_from_evaluation(pe)
    assert rec is not None
    assert set(rec.keys()) == {
        "title", "description", "priority", "source", "related_plan_id"
    }
    assert rec["related_plan_id"] == "plan_x"
    assert rec["source"] == "supervisor"


def test_followup_includes_plan_id_in_title(sup: Supervisor):
    """Followup title references the plan ID."""
    pe = _plan_eval(plan_id="plan_abc", grade="F", aggregate_score=0.10)
    rec = sup.recommend_followup_from_evaluation(pe)
    assert rec is not None
    assert "plan_abc" in rec["title"]
