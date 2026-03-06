"""Supervisor — makes decisions after each execution step.

Responsibilities (from roadmap Phase 5.1):
- Validate contracts
- Retry failed workflows
- Escalate anomalies
- Generate improvement tasks

Rules:
- contract_valid=True → action="continue"
- invalid and retry_count < 2 → action="retry"
- invalid and retries exhausted → action="escalate"
- runtime anomaly → action="escalate"
"""

from __future__ import annotations

from planner.schemas import (
    ImprovementPlan,
    PlanEvaluation,
    PlanStep,
    StepResult,
    SupervisorDecision,
)

# Markers for non-retryable runtime anomalies
_ANOMALY_MARKERS = ("permission denied", "sandbox", "blocked", "timeout")


class Supervisor:
    """Evaluate step results and decide next action."""

    MAX_RETRIES = 2

    def evaluate_step(
        self,
        step: PlanStep,
        result: StepResult,
    ) -> SupervisorDecision:
        """Evaluate a step result and return a decision."""
        # Success path: contract valid → continue
        if result.contract_valid:
            return SupervisorDecision(
                action="continue",
                reason="Step succeeded with valid contract",
                retry_allowed=False,
            )

        # Check for non-retryable anomalies
        if not self.should_retry(result):
            return SupervisorDecision(
                action="escalate",
                reason=self._escalation_reason(result),
                retry_allowed=False,
            )

        # Retryable failure under limit
        if result.retry_count < self.MAX_RETRIES:
            return SupervisorDecision(
                action="retry",
                reason=self.build_retry_reason(result),
                retry_allowed=True,
            )

        # Retries exhausted
        return SupervisorDecision(
            action="escalate",
            reason=self._escalation_reason(result),
            retry_allowed=False,
        )

    def should_retry(self, result: StepResult) -> bool:
        """Determine if a step failure is retryable.

        Non-retryable: permission denied, sandbox violation, blocked, timeout.
        """
        for err in result.validation_errors:
            if any(marker in err.lower() for marker in _ANOMALY_MARKERS):
                return False
        return True

    def build_retry_reason(self, result: StepResult) -> str:
        """Build a human-readable retry reason."""
        parts: list[str] = []
        if result.status == "failed":
            parts.append("step execution failed")
        if result.contract_valid is False:
            parts.append("output contract invalid")
        if result.validation_errors:
            errors_str = "; ".join(result.validation_errors[:3])
            parts.append(f"errors: {errors_str}")
        return "; ".join(parts) if parts else "unknown failure, retrying"

    def generate_followup_task(self, result: StepResult) -> dict | None:
        """Generate a follow-up improvement task if appropriate.

        Returns None if the step succeeded with a valid contract.
        """
        if result.status == "success" and result.contract_valid:
            return None

        return {
            "title": f"Investigate failure in step {result.step_id}",
            "description": (
                f"Step {result.step_id} failed after retries.\n"
                f"Status: {result.status}\n"
                f"Contract valid: {result.contract_valid}\n"
                f"Validation errors: {result.validation_errors}\n"
                f"Retry count: {result.retry_count}\n\n"
                f"Action needed: investigate root cause and improve "
                f"skill resilience."
            ),
            "priority": "high",
            "source": "supervisor",
        }

    def recommend_followup_from_evaluation(
        self,
        plan_eval: PlanEvaluation,
    ) -> dict | None:
        """Generate a follow-up recommendation based on plan evaluation.

        Rules:
        - A grade, no failures → None
        - B or C grade with followup_recommended → medium-priority dict
        - D or F grade → high-priority dict
        - Otherwise → None

        Does NOT auto-submit. Returns the structured recommendation only.
        """
        if plan_eval.grade == "A" and not plan_eval.followup_recommended:
            return None

        if plan_eval.grade in ("D", "F"):
            return {
                "title": (
                    f"Improve plan {plan_eval.plan_id}: "
                    f"grade {plan_eval.grade}"
                ),
                "description": (
                    f"Plan {plan_eval.plan_id} scored "
                    f"{plan_eval.aggregate_score:.2f} "
                    f"({plan_eval.grade}). {plan_eval.summary}"
                ),
                "priority": "high",
                "source": "supervisor",
                "related_plan_id": plan_eval.plan_id,
            }

        if plan_eval.grade in ("B", "C") and plan_eval.followup_recommended:
            return {
                "title": (
                    f"Review plan {plan_eval.plan_id}: "
                    f"grade {plan_eval.grade}"
                ),
                "description": (
                    f"Plan {plan_eval.plan_id} scored "
                    f"{plan_eval.aggregate_score:.2f} "
                    f"({plan_eval.grade}). {plan_eval.followup_reason}"
                ),
                "priority": "medium",
                "source": "supervisor",
                "related_plan_id": plan_eval.plan_id,
            }

        return None

    def review_improvement_plan(
        self, plan: ImprovementPlan
    ) -> SupervisorDecision:
        """Review an improvement plan and return a supervisor decision.

        Rules (deterministic):
        - requires_human_review → escalate
        - max_steps > 3 or max_files_changed > 5 → escalate (exceeds bounds)
        - no actionable findings → fail
        - otherwise → continue
        """
        if plan.requires_human_review:
            return SupervisorDecision(
                action="escalate",
                reason="Improvement plan requires human review",
                retry_allowed=False,
            )

        if plan.max_steps > 3:
            return SupervisorDecision(
                action="escalate",
                reason=f"Improvement plan exceeds step limit: {plan.max_steps} > 3",
                retry_allowed=False,
            )

        if plan.max_files_changed > 5:
            return SupervisorDecision(
                action="escalate",
                reason=f"Improvement plan exceeds file limit: {plan.max_files_changed} > 5",
                retry_allowed=False,
            )

        if not plan.findings:
            return SupervisorDecision(
                action="fail",
                reason="Improvement plan has no actionable findings",
                retry_allowed=False,
            )

        return SupervisorDecision(
            action="continue",
            reason="Improvement plan approved for bounded execution",
            retry_allowed=False,
        )

    def approve_improvement(self, plan: ImprovementPlan) -> bool:
        """Gate an improvement plan before execution.

        Deterministic approval rules:
        - requires_human_review → False (must be manually approved)
        - max_steps > 3 → False (exceeds safety bound)
        - max_files_changed > 5 → False (exceeds safety bound)
        - no findings → False (nothing to improve)
        - otherwise → True
        """
        if plan.requires_human_review:
            return False
        if plan.max_steps > 3:
            return False
        if plan.max_files_changed > 5:
            return False
        if not plan.findings:
            return False
        return True

    def _escalation_reason(self, result: StepResult) -> str:
        """Build an escalation reason from the result."""
        if result.validation_errors:
            errors = "; ".join(result.validation_errors[:3])
            return f"Escalating: {errors}"
        if result.contract_valid is False:
            return "Escalating: output contract invalid after retries exhausted"
        return "Escalating: step failed for unknown reason"
