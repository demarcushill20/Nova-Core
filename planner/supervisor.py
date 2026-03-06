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

from planner.schemas import PlanStep, StepResult, SupervisorDecision

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

    def _escalation_reason(self, result: StepResult) -> str:
        """Build an escalation reason from the result."""
        if result.validation_errors:
            errors = "; ".join(result.validation_errors[:3])
            return f"Escalating: {errors}"
        if result.contract_valid is False:
            return "Escalating: output contract invalid after retries exhausted"
        return "Escalating: step failed for unknown reason"
