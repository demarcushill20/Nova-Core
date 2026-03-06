"""Orchestrator — executes plan steps in sequence, preserves state.

Uses existing worker/skill execution conventions in NovaCore.
Steps are run sequentially with supervisor evaluation after each.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable

from planner.schemas import (
    ExecutionPlan,
    PlanStep,
    StepResult,
    SupervisorDecision,
)
from planner.skill_history import SkillHistoryStore
from planner.supervisor import Supervisor
from tools.adapters.contracts_validate import contracts_validate

logger = logging.getLogger(__name__)

STATE_DIR = Path(os.environ.get("NOVACORE_STATE", "/home/nova/nova-core/STATE"))
PLANS_DIR = STATE_DIR / "plans"

# Type alias for pluggable step executors.
# A step executor receives a PlanStep and returns (output_text, success, error).
StepExecutor = Callable[[PlanStep], tuple[str, bool, str]]


class Orchestrator:
    """Execute an ExecutionPlan step by step."""

    def __init__(
        self,
        supervisor: Supervisor | None = None,
        history_store: SkillHistoryStore | None = None,
        step_executor: StepExecutor | None = None,
    ):
        self.supervisor = supervisor or Supervisor()
        self.history_store = history_store or SkillHistoryStore()
        self._step_executor = step_executor

    # -- public API -----------------------------------------------------------

    def run_plan(self, plan: ExecutionPlan) -> dict[str, Any]:
        """Execute all steps in a plan sequentially.

        Returns a summary dict with overall status, step results, and
        supervisor decisions.
        """
        plan.status = "running"
        step_results: list[StepResult] = []
        decisions: list[dict[str, Any]] = []
        step_durations: dict[str, int] = {}

        for step in plan.steps:
            t0 = time.monotonic()
            result, decision = self._execute_with_supervision(step)
            duration_ms = int((time.monotonic() - t0) * 1000)
            step_durations[step.step_id] = duration_ms

            step_results.append(result)

            # Build decision record with optional followup
            followup = None
            if decision.action == "escalate":
                followup = self.supervisor.generate_followup_task(result)

            decisions.append({
                "step_id": step.step_id,
                "action": decision.action,
                "reason": decision.reason,
                "retry_allowed": decision.retry_allowed,
                "followup_task": followup,
            })

            # Record history with real measured duration
            self.history_store.record_run(
                skill_name=step.skill_name,
                success=(
                    result.status == "success"
                    and result.contract_valid is True
                ),
                duration_ms=duration_ms,
                retries=result.retry_count,
            )

            # Save incremental state
            self.save_plan_state(plan, step_results, decisions, step_durations)

            # Handle supervisor decision
            if decision.action in ("escalate", "fail", "abort"):
                plan.status = "failed"
                logger.warning(
                    "Plan %s stopped at step %s: %s",
                    plan.plan_id, step.step_id, decision.reason,
                )
                break
        else:
            # All steps completed without escalation
            plan.status = "done"

        # Final save
        self.save_plan_state(plan, step_results, decisions, step_durations)

        return {
            "plan_id": plan.plan_id,
            "task_id": plan.task_id,
            "status": plan.status,
            "steps": [_result_to_dict(r) for r in step_results],
            "decisions": decisions,
            "step_durations": step_durations,
        }

    def run_step(self, step: PlanStep) -> StepResult:
        """Execute a single plan step.

        If a step_executor was provided, it is called and the output is
        validated against the ## CONTRACT format.  Otherwise the default
        stub returns a placeholder success result.
        """
        step.status = "running"

        if self._step_executor is not None:
            return self._run_with_executor(step)

        # No executor: honest result — step was not executed.
        step.status = "skipped"
        return StepResult(
            step_id=step.step_id,
            status="skipped",
            contract_valid=False,
            validation_errors=["No step executor configured"],
        )

    def save_plan_state(
        self,
        plan: ExecutionPlan,
        step_results: list[StepResult],
        decisions: list[dict[str, Any]] | None = None,
        step_durations: dict[str, int] | None = None,
    ) -> None:
        """Persist a plan snapshot to STATE/plans/<plan_id>.json."""
        PLANS_DIR.mkdir(parents=True, exist_ok=True)
        path = PLANS_DIR / f"{plan.plan_id}.json"
        snapshot = {
            "plan": _plan_to_dict(plan),
            "step_results": [_result_to_dict(r) for r in step_results],
            "decisions": decisions or [],
            "step_durations": step_durations or {},
            "saved_at": time.time(),
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(snapshot, indent=2))
        tmp.replace(path)
        logger.debug("Saved plan state to %s", path)

    # -- internal -------------------------------------------------------------

    def _execute_with_supervision(
        self,
        step: PlanStep,
    ) -> tuple[StepResult, SupervisorDecision]:
        """Execute a step with retry logic driven by the supervisor.

        Retries up to 2 times. Each attempt is evaluated by the supervisor.
        """
        retry_count = 0
        while True:
            result = self.run_step(step)

            # No executor: step was not executed — escalate immediately
            if result.status == "skipped":
                return result, SupervisorDecision(
                    action="escalate",
                    reason="Step not executed: no step executor configured",
                    retry_allowed=False,
                )

            result.retry_count = retry_count
            decision = self.supervisor.evaluate_step(step, result)

            if decision.action == "retry" and decision.retry_allowed:
                retry_count += 1
                logger.info(
                    "Retrying step %s (attempt %d): %s",
                    step.step_id, retry_count, decision.reason,
                )
                continue

            return result, decision

    def _run_with_executor(self, step: PlanStep) -> StepResult:
        """Run step through the pluggable executor and validate output."""
        try:
            output_text, success, error = self._step_executor(step)
        except Exception as exc:
            step.status = "failed"
            return StepResult(
                step_id=step.step_id,
                status="error",
                contract_valid=False,
                validation_errors=[str(exc)],
            )

        # Validate the output against ## CONTRACT format
        validation = contracts_validate(output_text)
        contract_valid = validation.get("valid", False)
        validation_errors = validation.get("errors", [])

        if error:
            validation_errors.append(error)

        status = "success" if success else "failed"
        step.status = "done" if success else "failed"

        return StepResult(
            step_id=step.step_id,
            status=status,
            contract_valid=contract_valid,
            validation_errors=validation_errors,
        )


# -- serialization helpers (module-level) ------------------------------------


def _plan_to_dict(plan: ExecutionPlan) -> dict:
    return {
        "plan_id": plan.plan_id,
        "task_id": plan.task_id,
        "strategy": plan.strategy,
        "steps": [
            {
                "step_id": s.step_id,
                "skill_name": s.skill_name,
                "goal": s.goal,
                "inputs": s.inputs,
                "status": s.status,
            }
            for s in plan.steps
        ],
        "success_criteria": plan.success_criteria,
        "status": plan.status,
    }


def _result_to_dict(r: StepResult) -> dict:
    return {
        "step_id": r.step_id,
        "status": r.status,
        "output_path": r.output_path,
        "contract_valid": r.contract_valid,
        "validation_errors": r.validation_errors,
        "retry_count": r.retry_count,
    }
