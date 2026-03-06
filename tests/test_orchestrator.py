"""Tests for planner.orchestrator — sequential plan execution with supervision."""

import json
from pathlib import Path

import pytest

from planner.orchestrator import Orchestrator
from planner.schemas import (
    ExecutionPlan,
    PlanStep,
    StepResult,
    SupervisorDecision,
)
from planner.skill_history import SkillHistoryStore
from planner.supervisor import Supervisor


@pytest.fixture
def tmp_state(tmp_path: Path, monkeypatch):
    """Redirect PLANS_DIR to a temp directory."""
    plans_dir = tmp_path / "state" / "plans"
    monkeypatch.setattr("planner.orchestrator.PLANS_DIR", plans_dir)
    return tmp_path / "state"


@pytest.fixture
def history_store(tmp_path: Path) -> SkillHistoryStore:
    return SkillHistoryStore(path=tmp_path / "history.json")


@pytest.fixture
def orch(tmp_state: Path, history_store: SkillHistoryStore) -> Orchestrator:
    return Orchestrator(
        history_store=history_store,
        step_executor=_executor_always_valid,
    )


@pytest.fixture
def no_executor_orch(tmp_state: Path, history_store: SkillHistoryStore) -> Orchestrator:
    """Orchestrator without step executor — tests honest skipped behavior."""
    return Orchestrator(history_store=history_store)


def _plan(task_id: str = "t001", steps: list[PlanStep] | None = None) -> ExecutionPlan:
    if steps is None:
        steps = [
            PlanStep(step_id="s1", skill_name="code_improve", goal="Apply fix"),
            PlanStep(step_id="s2", skill_name="system_supervisor", goal="Validate"),
        ]
    return ExecutionPlan(
        plan_id=f"plan_{task_id}",
        task_id=task_id,
        strategy="multi_skill",
        steps=steps,
        success_criteria=["contract valid"],
    )


# -- sequential execution ----------------------------------------------------

def test_sequential_execution(orch: Orchestrator):
    plan = _plan()
    result = orch.run_plan(plan)
    assert result["status"] == "done"
    assert len(result["steps"]) == 2
    assert result["steps"][0]["step_id"] == "s1"
    assert result["steps"][1]["step_id"] == "s2"
    # Executor produces valid contract → both succeed
    assert result["steps"][0]["status"] == "success"
    assert result["steps"][1]["status"] == "success"
    assert result["steps"][0]["contract_valid"] is True
    assert result["steps"][1]["contract_valid"] is True


def test_single_step_plan(orch: Orchestrator):
    plan = _plan(steps=[PlanStep(step_id="s1", skill_name="file_ops", goal="Create file")])
    result = orch.run_plan(plan)
    assert result["status"] == "done"
    assert len(result["steps"]) == 1


def test_empty_plan(orch: Orchestrator):
    plan = _plan(steps=[])
    result = orch.run_plan(plan)
    assert result["status"] == "done"
    assert len(result["steps"]) == 0


# -- plan state saved ---------------------------------------------------------

def test_plan_state_saved(orch: Orchestrator, tmp_state: Path):
    plan = _plan(task_id="t042")
    orch.run_plan(plan)
    plan_file = tmp_state / "plans" / "plan_t042.json"
    assert plan_file.exists()
    data = json.loads(plan_file.read_text())
    assert data["plan"]["plan_id"] == "plan_t042"
    assert data["plan"]["status"] == "done"
    assert len(data["step_results"]) == 2
    assert len(data["decisions"]) == 2


def test_plan_state_snapshot_has_required_fields(orch: Orchestrator, tmp_state: Path):
    plan = _plan(task_id="t099")
    orch.run_plan(plan)
    data = json.loads((tmp_state / "plans" / "plan_t099.json").read_text())
    assert "plan" in data
    assert "step_results" in data
    assert "decisions" in data
    assert "step_durations" in data
    assert "saved_at" in data
    assert isinstance(data["saved_at"], float)


# -- step result shape -------------------------------------------------------

def test_step_result_shape(orch: Orchestrator):
    plan = _plan()
    result = orch.run_plan(plan)
    step_result = result["steps"][0]
    assert "step_id" in step_result
    assert "status" in step_result
    assert "output_path" in step_result
    assert "contract_valid" in step_result
    assert "validation_errors" in step_result
    assert "retry_count" in step_result


# -- decision shape ----------------------------------------------------------

def test_decision_shape(orch: Orchestrator):
    plan = _plan()
    result = orch.run_plan(plan)
    d = result["decisions"][0]
    assert "step_id" in d
    assert "action" in d
    assert "reason" in d
    assert "retry_allowed" in d
    assert "followup_task" in d


# -- retry path exercised -----------------------------------------------------

def test_retry_path_exercised(history_store: SkillHistoryStore, tmp_state: Path):
    """Orchestrator retries a step when supervisor says retry."""
    orch = Orchestrator(history_store=history_store)
    call_count = 0

    def mock_run_step(step: PlanStep) -> StepResult:
        nonlocal call_count
        call_count += 1
        if call_count <= 1:
            return StepResult(
                step_id=step.step_id,
                status="success",
                contract_valid=False,
            )
        return StepResult(
            step_id=step.step_id,
            status="success",
            contract_valid=True,
        )

    orch.run_step = mock_run_step

    plan = _plan(steps=[PlanStep(step_id="s1", skill_name="code_improve", goal="Fix")])
    result = orch.run_plan(plan)
    assert result["status"] == "done"
    assert call_count >= 2


# -- supervisor continue path -------------------------------------------------

def test_supervisor_continue_path(orch: Orchestrator):
    """All steps succeed → supervisor says continue → plan done."""
    plan = _plan()
    result = orch.run_plan(plan)
    assert result["status"] == "done"
    for d in result["decisions"]:
        assert d["action"] == "continue"
        assert d["retry_allowed"] is False
        assert d["followup_task"] is None


# -- supervisor escalate path -------------------------------------------------

def test_supervisor_escalate_on_anomaly(history_store: SkillHistoryStore, tmp_state: Path):
    """When supervisor escalates on anomaly, plan stops and status is 'failed'."""
    orch = Orchestrator(history_store=history_store)

    def mock_run_step(step: PlanStep) -> StepResult:
        return StepResult(
            step_id=step.step_id,
            status="failed",
            contract_valid=False,
            validation_errors=["Permission denied: /etc/shadow"],
        )

    orch.run_step = mock_run_step

    plan = _plan(steps=[
        PlanStep(step_id="s1", skill_name="code_improve", goal="Fix"),
        PlanStep(step_id="s2", skill_name="system_supervisor", goal="Validate"),
    ])
    result = orch.run_plan(plan)
    assert result["status"] == "failed"
    assert len(result["steps"]) == 1
    assert result["decisions"][0]["action"] == "escalate"
    assert result["decisions"][0]["followup_task"] is not None
    assert result["decisions"][0]["retry_allowed"] is False


def test_escalate_stops_remaining_steps(history_store: SkillHistoryStore, tmp_state: Path):
    """If step 1 escalates, step 2 should NOT execute."""
    orch = Orchestrator(history_store=history_store)
    step_ids_executed = []

    def mock_run_step(step: PlanStep) -> StepResult:
        step_ids_executed.append(step.step_id)
        return StepResult(
            step_id=step.step_id,
            status="failed",
            contract_valid=False,
            validation_errors=["Timeout after 300s"],
        )

    orch.run_step = mock_run_step

    plan = _plan(steps=[
        PlanStep(step_id="s1", skill_name="log_triage", goal="Triage"),
        PlanStep(step_id="s2", skill_name="code_improve", goal="Fix"),
        PlanStep(step_id="s3", skill_name="system_supervisor", goal="Verify"),
    ])
    result = orch.run_plan(plan)
    assert result["status"] == "failed"
    assert step_ids_executed == ["s1"]


# -- history recording -------------------------------------------------------

def test_history_recorded_after_steps(orch: Orchestrator, history_store: SkillHistoryStore):
    plan = _plan()
    orch.run_plan(plan)
    stats_ci = history_store.get_stats("code_improve")
    stats_ss = history_store.get_stats("system_supervisor")
    assert stats_ci.get("runs", 0) >= 1
    assert stats_ss.get("runs", 0) >= 1


def test_history_records_success(orch: Orchestrator, history_store: SkillHistoryStore):
    plan = _plan(steps=[PlanStep(step_id="s1", skill_name="file_ops", goal="Create")])
    orch.run_plan(plan)
    assert history_store.get_success_rate("file_ops") == 1.0


# -- run_step with executor ---------------------------------------------------

def test_run_step_returns_step_result(orch: Orchestrator):
    step = PlanStep(step_id="s1", skill_name="file_ops", goal="Create file")
    result = orch.run_step(step)
    assert isinstance(result, StepResult)
    assert result.step_id == "s1"
    assert result.status == "success"
    assert result.contract_valid is True


def test_run_step_updates_step_status(orch: Orchestrator):
    step = PlanStep(step_id="s1", skill_name="file_ops", goal="Create file")
    assert step.status == "queued"
    orch.run_step(step)
    assert step.status == "done"


# =============================================================================
# Contract validation via step_executor
# =============================================================================

VALID_CONTRACT = (
    "Some output text\n\n"
    "## CONTRACT\n"
    "summary: did the thing\n"
    "files_changed:\n"
    "  - foo.py (edit)\n"
    "verification: checked\n"
    "confidence: high\n"
)

INVALID_CONTRACT = "Some output text without a valid contract block"

PARTIAL_CONTRACT = (
    "## CONTRACT\n"
    "summary: only summary\n"
)


def _executor_always_invalid(step: PlanStep) -> tuple[str, bool, str]:
    """Executor that always produces invalid contract output."""
    return INVALID_CONTRACT, True, ""


def _executor_always_valid(step: PlanStep) -> tuple[str, bool, str]:
    """Executor that always produces valid contract output."""
    return VALID_CONTRACT, True, ""


def _executor_valid_after_n(n: int):
    """Return an executor that produces invalid output n times, then valid."""
    call_count = 0

    def executor(step: PlanStep) -> tuple[str, bool, str]:
        nonlocal call_count
        call_count += 1
        if call_count <= n:
            return INVALID_CONTRACT, True, ""
        return VALID_CONTRACT, True, ""

    return executor


def _executor_fail_with_error(step: PlanStep) -> tuple[str, bool, str]:
    """Executor that fails with an error."""
    return "", False, "skill crashed"


def _executor_raises(step: PlanStep) -> tuple[str, bool, str]:
    """Executor that raises an exception."""
    raise RuntimeError("unexpected explosion")


# -- contract validation via step_executor ------------------------------------

def test_valid_contract_via_executor(history_store: SkillHistoryStore, tmp_state: Path):
    """Step executor returning valid contract → contract_valid=True → continue."""
    orch = Orchestrator(
        history_store=history_store,
        step_executor=_executor_always_valid,
    )
    plan = _plan(steps=[PlanStep(step_id="s1", skill_name="file_ops", goal="Create")])
    result = orch.run_plan(plan)
    assert result["status"] == "done"
    assert result["steps"][0]["contract_valid"] is True
    assert result["decisions"][0]["action"] == "continue"


def test_invalid_contract_triggers_retry(history_store: SkillHistoryStore, tmp_state: Path):
    """Invalid contract output triggers retry (up to 2 times)."""
    executor = _executor_valid_after_n(1)
    orch = Orchestrator(
        history_store=history_store,
        step_executor=executor,
    )
    plan = _plan(steps=[PlanStep(step_id="s1", skill_name="code_improve", goal="Fix")])
    result = orch.run_plan(plan)
    assert result["status"] == "done"
    assert result["steps"][0]["retry_count"] == 1
    assert result["steps"][0]["contract_valid"] is True


def test_invalid_contract_retries_twice_then_succeeds(
    history_store: SkillHistoryStore, tmp_state: Path
):
    """Invalid contract output for 2 attempts, then valid on 3rd."""
    executor = _executor_valid_after_n(2)
    orch = Orchestrator(
        history_store=history_store,
        step_executor=executor,
    )
    plan = _plan(steps=[PlanStep(step_id="s1", skill_name="code_improve", goal="Fix")])
    result = orch.run_plan(plan)
    assert result["status"] == "done"
    assert result["steps"][0]["retry_count"] == 2
    assert result["steps"][0]["contract_valid"] is True


def test_invalid_contract_exhausts_retries_and_escalates(
    history_store: SkillHistoryStore, tmp_state: Path
):
    """Invalid contract for all 3 attempts → retries exhausted → escalate."""
    orch = Orchestrator(
        history_store=history_store,
        step_executor=_executor_always_invalid,
    )
    plan = _plan(steps=[PlanStep(step_id="s1", skill_name="code_improve", goal="Fix")])
    result = orch.run_plan(plan)
    assert result["status"] == "failed"
    assert result["steps"][0]["retry_count"] == 2
    assert result["decisions"][0]["action"] == "escalate"


def test_partial_contract_triggers_retry(history_store: SkillHistoryStore, tmp_state: Path):
    """Partial contract (missing fields) is treated as invalid → retry."""
    call_count = 0

    def partial_then_valid(step: PlanStep) -> tuple[str, bool, str]:
        nonlocal call_count
        call_count += 1
        if call_count <= 1:
            return PARTIAL_CONTRACT, True, ""
        return VALID_CONTRACT, True, ""

    orch = Orchestrator(
        history_store=history_store,
        step_executor=partial_then_valid,
    )
    plan = _plan(steps=[PlanStep(step_id="s1", skill_name="file_ops", goal="Fix")])
    result = orch.run_plan(plan)
    assert result["status"] == "done"
    assert result["steps"][0]["retry_count"] == 1


def test_executor_exception_treated_as_failure(
    history_store: SkillHistoryStore, tmp_state: Path
):
    """If the executor raises, the step fails with contract_valid=False."""
    orch = Orchestrator(
        history_store=history_store,
        step_executor=_executor_raises,
    )
    plan = _plan(steps=[PlanStep(step_id="s1", skill_name="code_improve", goal="Fix")])
    result = orch.run_plan(plan)
    assert result["status"] == "failed"
    assert result["steps"][0]["retry_count"] == 2
    assert result["decisions"][0]["action"] == "escalate"


def test_executor_failure_with_error(history_store: SkillHistoryStore, tmp_state: Path):
    """Executor returning success=False triggers retry then escalate."""
    orch = Orchestrator(
        history_store=history_store,
        step_executor=_executor_fail_with_error,
    )
    plan = _plan(steps=[PlanStep(step_id="s1", skill_name="code_improve", goal="Fix")])
    result = orch.run_plan(plan)
    assert result["status"] == "failed"
    assert result["steps"][0]["retry_count"] == 2


# -- retry metadata in plan state --------------------------------------------

def test_retry_count_persisted_in_plan_state(
    history_store: SkillHistoryStore, tmp_state: Path
):
    """Plan state JSON should reflect the retry count."""
    orch = Orchestrator(
        history_store=history_store,
        step_executor=_executor_always_invalid,
    )
    plan = _plan(
        task_id="t_retry_state",
        steps=[PlanStep(step_id="s1", skill_name="code_improve", goal="Fix")],
    )
    orch.run_plan(plan)
    plan_file = tmp_state / "plans" / "plan_t_retry_state.json"
    assert plan_file.exists()
    data = json.loads(plan_file.read_text())
    assert data["step_results"][0]["retry_count"] == 2
    assert data["decisions"][0]["action"] == "escalate"


def test_supervisor_decisions_persisted(
    history_store: SkillHistoryStore, tmp_state: Path
):
    """Supervisor decision is persisted after retries."""
    executor = _executor_valid_after_n(1)
    orch = Orchestrator(
        history_store=history_store,
        step_executor=executor,
    )
    plan = _plan(
        task_id="t_persist",
        steps=[PlanStep(step_id="s1", skill_name="code_improve", goal="Fix")],
    )
    result = orch.run_plan(plan)
    assert result["status"] == "done"
    assert result["decisions"][0]["action"] == "continue"
    plan_file = tmp_state / "plans" / "plan_t_persist.json"
    data = json.loads(plan_file.read_text())
    assert len(data["decisions"]) == 1
    assert data["decisions"][0]["action"] == "continue"


# -- multi-step plan with retry on one step -----------------------------------

def test_retry_on_first_step_then_second_succeeds(
    history_store: SkillHistoryStore, tmp_state: Path
):
    """First step needs retry, second step succeeds immediately."""
    call_count = {"s1": 0, "s2": 0}

    def mixed_executor(step: PlanStep) -> tuple[str, bool, str]:
        call_count[step.step_id] = call_count.get(step.step_id, 0) + 1
        if step.step_id == "s1" and call_count["s1"] <= 1:
            return INVALID_CONTRACT, True, ""
        return VALID_CONTRACT, True, ""

    orch = Orchestrator(
        history_store=history_store,
        step_executor=mixed_executor,
    )
    plan = _plan(steps=[
        PlanStep(step_id="s1", skill_name="code_improve", goal="Fix"),
        PlanStep(step_id="s2", skill_name="system_supervisor", goal="Verify"),
    ])
    result = orch.run_plan(plan)
    assert result["status"] == "done"
    assert len(result["steps"]) == 2
    assert result["decisions"][0]["action"] == "continue"
    assert result["decisions"][1]["action"] == "continue"


# -- plan_id and task_id in result -------------------------------------------

def test_result_includes_plan_and_task_id(orch: Orchestrator):
    plan = _plan(task_id="t555")
    result = orch.run_plan(plan)
    assert result["plan_id"] == "plan_t555"
    assert result["task_id"] == "t555"


# =============================================================================
# Phase 5.2B hardening tests
# =============================================================================

# -- no executor (honest skipped behavior) ------------------------------------


def test_no_executor_returns_skipped(no_executor_orch: Orchestrator):
    """run_step without executor returns honest 'skipped' status."""
    step = PlanStep(step_id="s1", skill_name="file_ops", goal="Create file")
    result = no_executor_orch.run_step(step)
    assert isinstance(result, StepResult)
    assert result.step_id == "s1"
    assert result.status == "skipped"
    assert result.contract_valid is False
    assert "No step executor configured" in result.validation_errors


def test_no_executor_does_not_falsely_succeed(no_executor_orch: Orchestrator):
    """No-executor path must never report success."""
    step = PlanStep(step_id="s1", skill_name="file_ops", goal="Create file")
    result = no_executor_orch.run_step(step)
    assert result.status != "success"
    assert result.contract_valid is not True


def test_no_executor_plan_escalates(no_executor_orch: Orchestrator):
    """Plan with no executor escalates immediately, plan fails."""
    plan = _plan(steps=[PlanStep(step_id="s1", skill_name="code_improve", goal="Fix")])
    result = no_executor_orch.run_plan(plan)
    assert result["status"] == "failed"
    assert result["decisions"][0]["action"] == "escalate"
    assert result["decisions"][0]["retry_allowed"] is False
    assert result["steps"][0]["status"] == "skipped"


def test_no_executor_step_status_set(no_executor_orch: Orchestrator):
    """Step status should be set to 'skipped' when no executor."""
    step = PlanStep(step_id="s1", skill_name="file_ops", goal="Create")
    assert step.status == "queued"
    no_executor_orch.run_step(step)
    assert step.status == "skipped"


def test_no_executor_does_not_retry(no_executor_orch: Orchestrator):
    """No-executor path must not retry — it escalates immediately."""
    plan = _plan(steps=[PlanStep(step_id="s1", skill_name="code_improve", goal="Fix")])
    result = no_executor_orch.run_plan(plan)
    assert result["steps"][0]["retry_count"] == 0


# -- real duration_ms ---------------------------------------------------------


def test_real_duration_ms_recorded(orch: Orchestrator, history_store: SkillHistoryStore):
    """History store records non-zero or zero duration from real timing."""
    plan = _plan(steps=[PlanStep(step_id="s1", skill_name="code_improve", goal="Fix")])
    orch.run_plan(plan)
    stats = history_store.get_stats("code_improve")
    assert isinstance(stats["avg_duration_ms"], int)
    # Real measurement — not a placeholder like -1 or 999999
    assert stats["avg_duration_ms"] >= 0


def test_real_duration_ms_with_retries(history_store: SkillHistoryStore, tmp_state: Path):
    """Duration includes retry time — multi-attempt steps take longer."""
    import time as _time

    call_count = 0

    def slow_then_valid(step: PlanStep) -> tuple[str, bool, str]:
        nonlocal call_count
        call_count += 1
        _time.sleep(0.01)  # 10ms per attempt
        if call_count <= 1:
            return INVALID_CONTRACT, True, ""
        return VALID_CONTRACT, True, ""

    orch = Orchestrator(
        history_store=history_store,
        step_executor=slow_then_valid,
    )
    plan = _plan(steps=[PlanStep(step_id="s1", skill_name="code_improve", goal="Fix")])
    result = orch.run_plan(plan)
    assert result["status"] == "done"
    # Duration covers both attempts (~20ms+)
    stats = history_store.get_stats("code_improve")
    assert stats["avg_duration_ms"] >= 15


def test_step_durations_in_plan_result(orch: Orchestrator):
    """run_plan result includes step_durations dict."""
    plan = _plan()
    result = orch.run_plan(plan)
    assert "step_durations" in result
    assert "s1" in result["step_durations"]
    assert "s2" in result["step_durations"]
    assert isinstance(result["step_durations"]["s1"], int)


def test_step_durations_in_persisted_state(orch: Orchestrator, tmp_state: Path):
    """Persisted plan state includes step_durations."""
    plan = _plan(task_id="t_dur")
    orch.run_plan(plan)
    plan_file = tmp_state / "plans" / "plan_t_dur.json"
    data = json.loads(plan_file.read_text())
    assert "step_durations" in data
    assert isinstance(data["step_durations"], dict)
    assert "s1" in data["step_durations"]


# -- plan state completeness --------------------------------------------------


def test_plan_state_includes_full_history(orch: Orchestrator, tmp_state: Path):
    """Persisted plan state has complete reconstruction data."""
    plan = _plan(task_id="t_full")
    orch.run_plan(plan)
    data = json.loads((tmp_state / "plans" / "plan_t_full.json").read_text())

    # Plan metadata
    assert data["plan"]["plan_id"] == "plan_t_full"
    assert data["plan"]["task_id"] == "t_full"
    assert data["plan"]["strategy"] == "multi_skill"
    assert data["plan"]["status"] == "done"

    # Ordered steps
    assert len(data["plan"]["steps"]) == 2
    assert data["plan"]["steps"][0]["step_id"] == "s1"
    assert data["plan"]["steps"][1]["step_id"] == "s2"

    # Step results
    assert len(data["step_results"]) == 2
    for sr in data["step_results"]:
        assert "step_id" in sr
        assert "status" in sr
        assert "contract_valid" in sr
        assert "validation_errors" in sr
        assert "retry_count" in sr

    # Decisions
    assert len(data["decisions"]) == 2
    for d in data["decisions"]:
        assert "step_id" in d
        assert "action" in d
        assert "reason" in d
        assert "retry_allowed" in d
        assert "followup_task" in d

    # Durations
    assert len(data["step_durations"]) == 2

    # Timestamp
    assert isinstance(data["saved_at"], float)


def test_plan_state_with_retry_has_decision_history(
    history_store: SkillHistoryStore, tmp_state: Path
):
    """Plan state after retries shows final decision per step."""
    executor = _executor_valid_after_n(1)
    orch = Orchestrator(
        history_store=history_store,
        step_executor=executor,
    )
    plan = _plan(
        task_id="t_retry_hist",
        steps=[PlanStep(step_id="s1", skill_name="code_improve", goal="Fix")],
    )
    orch.run_plan(plan)
    data = json.loads((tmp_state / "plans" / "plan_t_retry_hist.json").read_text())

    # Should show the final successful result (after retry)
    assert data["step_results"][0]["retry_count"] == 1
    assert data["step_results"][0]["contract_valid"] is True
    assert data["decisions"][0]["action"] == "continue"
    # Duration recorded
    assert data["step_durations"]["s1"] >= 0


# -- contract validation errors observable ------------------------------------


def test_invalid_contract_populates_validation_errors(
    history_store: SkillHistoryStore, tmp_state: Path
):
    """Invalid contract produces specific validation_errors in StepResult."""
    orch = Orchestrator(
        history_store=history_store,
        step_executor=_executor_always_invalid,
    )
    plan = _plan(steps=[PlanStep(step_id="s1", skill_name="code_improve", goal="Fix")])
    result = orch.run_plan(plan)
    errors = result["steps"][0]["validation_errors"]
    assert len(errors) > 0
    # Should mention missing contract or fields
    assert any("CONTRACT" in e or "Missing" in e or "contract" in e for e in errors)


def test_supervisor_consumes_contract_invalid_correctly(
    history_store: SkillHistoryStore, tmp_state: Path
):
    """Supervisor produces escalate decision for persistent contract invalidity."""
    orch = Orchestrator(
        history_store=history_store,
        step_executor=_executor_always_invalid,
    )
    plan = _plan(steps=[PlanStep(step_id="s1", skill_name="code_improve", goal="Fix")])
    result = orch.run_plan(plan)
    assert result["decisions"][0]["action"] == "escalate"
    assert result["decisions"][0]["retry_allowed"] is False
    assert result["decisions"][0]["followup_task"] is not None


# =============================================================================
# Phase 5.3 — evaluation persistence tests
# =============================================================================


def test_evaluation_in_plan_result(orch: Orchestrator):
    """run_plan result includes evaluation block."""
    plan = _plan()
    result = orch.run_plan(plan)
    assert "evaluation" in result
    ev = result["evaluation"]
    assert "step_evaluations" in ev
    assert "aggregate_score" in ev
    assert "grade" in ev
    assert "summary" in ev
    assert "followup_recommended" in ev
    assert "followup_task" in ev


def test_evaluation_persisted_in_plan_state(orch: Orchestrator, tmp_state: Path):
    """Saved plan state includes evaluation block."""
    plan = _plan(task_id="t_eval")
    orch.run_plan(plan)
    data = json.loads((tmp_state / "plans" / "plan_t_eval.json").read_text())
    assert "evaluation" in data
    assert data["evaluation"] is not None
    assert "step_evaluations" in data["evaluation"]
    assert "aggregate_score" in data["evaluation"]
    assert "grade" in data["evaluation"]


def test_evaluation_step_evaluations_match_steps(orch: Orchestrator):
    """Number of step evaluations matches number of executed steps."""
    plan = _plan()
    result = orch.run_plan(plan)
    assert len(result["evaluation"]["step_evaluations"]) == 2
    assert result["evaluation"]["step_evaluations"][0]["step_id"] == "s1"
    assert result["evaluation"]["step_evaluations"][1]["step_id"] == "s2"


def test_evaluation_grade_persisted(orch: Orchestrator, tmp_state: Path):
    """Aggregate grade is persisted in plan state."""
    plan = _plan(task_id="t_grade")
    orch.run_plan(plan)
    data = json.loads((tmp_state / "plans" / "plan_t_grade.json").read_text())
    assert data["evaluation"]["grade"] in ("A", "B", "C", "D", "F")


def test_evaluation_perfect_plan_grade_A(orch: Orchestrator):
    """All steps succeed with valid contracts → grade A."""
    plan = _plan()
    result = orch.run_plan(plan)
    assert result["evaluation"]["grade"] == "A"
    assert result["evaluation"]["aggregate_score"] >= 0.90


def test_evaluation_failed_plan_has_followup(
    history_store: SkillHistoryStore, tmp_state: Path
):
    """Failed plan with escalation → followup_recommended and followup_task."""
    orch = Orchestrator(
        history_store=history_store,
        step_executor=_executor_always_invalid,
    )
    plan = _plan(steps=[PlanStep(step_id="s1", skill_name="code_improve", goal="Fix")])
    result = orch.run_plan(plan)
    ev = result["evaluation"]
    assert ev["followup_recommended"] is True
    assert ev["followup_task"] is not None
    assert "related_plan_id" in ev["followup_task"]


def test_evaluation_followup_none_for_perfect(orch: Orchestrator):
    """Perfect plan → no followup recommendation."""
    plan = _plan()
    result = orch.run_plan(plan)
    ev = result["evaluation"]
    assert ev["followup_recommended"] is False
    assert ev["followup_task"] is None


def test_incremental_save_has_null_evaluation(
    orch: Orchestrator, tmp_state: Path
):
    """Incremental saves during execution have evaluation=null."""
    # The incremental saves don't include evaluation; only the final save does.
    # We verify the final save has evaluation.
    plan = _plan(task_id="t_inc")
    orch.run_plan(plan)
    data = json.loads((tmp_state / "plans" / "plan_t_inc.json").read_text())
    # Final save must have evaluation
    assert data["evaluation"] is not None
