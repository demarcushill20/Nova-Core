"""ImprovementPlanner — bounded, one-cycle self-improvement loop.

Phase 6: inspects execution health, identifies concrete improvement
opportunities, generates a safe bounded improvement plan, and gates
execution through supervisor review.

All logic is deterministic. Findings are derived only from explicit
evidence in plan evaluations and recent plan states. No recursive
improvement (one cycle only).
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from planner.schemas import (
    HealthFinding,
    ImprovementPlan,
    ImprovementResult,
    PlanEvaluation,
)

logger = logging.getLogger(__name__)

STATE_DIR = Path(os.environ.get("NOVACORE_STATE", "/home/nova/nova-core/STATE"))
IMPROVEMENT_DIR = STATE_DIR / "improvement_runs"

# ---------------------------------------------------------------------------
# Finding category constants
# ---------------------------------------------------------------------------
CAT_LOW_GRADE = "low_grade_execution"
CAT_CONTRACT_FAILURE = "repeated_contract_failure"
CAT_RETRY_PATTERN = "repeated_retry_pattern"
CAT_SLOW_EXECUTION = "slow_execution"
CAT_VERIFICATION_WEAKNESS = "verification_weakness"

# ---------------------------------------------------------------------------
# Severity thresholds (deterministic)
# ---------------------------------------------------------------------------
_GRADE_SEVERITY = {"F": "critical", "D": "high", "C": "medium", "B": "low"}
_SLOW_THRESHOLD_MS = 30_000
_HIGH_RETRY_THRESHOLD = 2

# ---------------------------------------------------------------------------
# Improvement plan bounds (safety caps)
# ---------------------------------------------------------------------------
_DEFAULT_MAX_STEPS = 3
_DEFAULT_MAX_FILES = 5
_ALLOWED_SKILLS = ["code_improve", "repo_health_check", "self_verification"]


class ImprovementPlanner:
    """Deterministic improvement planner for bounded self-improvement.

    Inspects plan evaluations and recent execution states to generate
    concrete health findings, then builds a bounded improvement plan
    that can be gated by the supervisor before execution.
    """

    def build_health_findings(
        self,
        plan_evaluation: PlanEvaluation,
        recent_plan_states: list[dict[str, Any]] | None = None,
    ) -> list[HealthFinding]:
        """Extract health findings from a plan evaluation and recent states.

        Only generates findings from explicit evidence. Each finding
        has a category, severity, summary, and supporting evidence list.
        """
        findings: list[HealthFinding] = []
        counter = 0

        # --- 1. Low grade execution ---
        if plan_evaluation.grade in _GRADE_SEVERITY:
            counter += 1
            findings.append(HealthFinding(
                finding_id=f"hf_{counter:03d}",
                category=CAT_LOW_GRADE,
                severity=_GRADE_SEVERITY[plan_evaluation.grade],
                summary=(
                    f"Plan {plan_evaluation.plan_id} received grade "
                    f"{plan_evaluation.grade} "
                    f"(score {plan_evaluation.aggregate_score:.2f})"
                ),
                evidence=[
                    f"aggregate_score={plan_evaluation.aggregate_score:.2f}",
                    f"grade={plan_evaluation.grade}",
                    plan_evaluation.summary,
                ],
            ))

        # --- 2. Contract failures in step evaluations ---
        contract_failures = [
            e for e in plan_evaluation.step_evaluations
            if not e.contract_valid
        ]
        if contract_failures:
            counter += 1
            step_ids = [e.step_id for e in contract_failures]
            findings.append(HealthFinding(
                finding_id=f"hf_{counter:03d}",
                category=CAT_CONTRACT_FAILURE,
                severity="high" if len(contract_failures) > 1 else "medium",
                summary=(
                    f"{len(contract_failures)} step(s) had invalid contracts: "
                    f"{', '.join(step_ids)}"
                ),
                evidence=[
                    f"step {e.step_id}: contract_valid=False"
                    for e in contract_failures
                ],
            ))

        # --- 3. Retry patterns ---
        high_retry_steps = [
            e for e in plan_evaluation.step_evaluations
            if e.retry_penalty > 0
        ]
        if high_retry_steps:
            counter += 1
            findings.append(HealthFinding(
                finding_id=f"hf_{counter:03d}",
                category=CAT_RETRY_PATTERN,
                severity="medium",
                summary=(
                    f"{len(high_retry_steps)} step(s) required retries"
                ),
                evidence=[
                    f"step {e.step_id}: retry_penalty={e.retry_penalty:.2f}"
                    for e in high_retry_steps
                ],
            ))

        # --- 4. Slow execution ---
        slow_steps = [
            e for e in plan_evaluation.step_evaluations
            if e.duration_score == 0.0 and e.execution_success
        ]
        if slow_steps:
            counter += 1
            findings.append(HealthFinding(
                finding_id=f"hf_{counter:03d}",
                category=CAT_SLOW_EXECUTION,
                severity="low",
                summary=(
                    f"{len(slow_steps)} step(s) had zero duration score "
                    f"(execution >= {_SLOW_THRESHOLD_MS}ms)"
                ),
                evidence=[
                    f"step {e.step_id}: duration_score=0.00"
                    for e in slow_steps
                ],
            ))

        # --- 5. Verification weakness ---
        weak_verification = [
            e for e in plan_evaluation.step_evaluations
            if e.verification_score < 0.10 and e.execution_success
        ]
        if weak_verification:
            counter += 1
            findings.append(HealthFinding(
                finding_id=f"hf_{counter:03d}",
                category=CAT_VERIFICATION_WEAKNESS,
                severity="medium",
                summary=(
                    f"{len(weak_verification)} step(s) had weak verification "
                    f"(score < 0.10)"
                ),
                evidence=[
                    f"step {e.step_id}: verification_score="
                    f"{e.verification_score:.2f}"
                    for e in weak_verification
                ],
            ))

        # --- 6. Cross-plan patterns from recent states ---
        if recent_plan_states:
            low_grade_count = sum(
                1 for s in recent_plan_states
                if s.get("evaluation", {})
                and s["evaluation"].get("grade") in ("D", "F")
            )
            if low_grade_count >= 2:
                counter += 1
                findings.append(HealthFinding(
                    finding_id=f"hf_{counter:03d}",
                    category=CAT_LOW_GRADE,
                    severity="critical",
                    summary=(
                        f"{low_grade_count} recent plans scored D or F — "
                        f"systemic quality issue"
                    ),
                    evidence=[
                        f"plan {s.get('plan', {}).get('plan_id', '?')}: "
                        f"grade={s['evaluation']['grade']}"
                        for s in recent_plan_states
                        if s.get("evaluation", {})
                        and s["evaluation"].get("grade") in ("D", "F")
                    ],
                ))

        return findings

    def build_improvement_plan(
        self,
        findings: list[HealthFinding],
        source_plan_id: str | None = None,
    ) -> ImprovementPlan:
        """Build a bounded improvement plan from health findings.

        The plan is deterministically scoped:
        - max_steps capped at _DEFAULT_MAX_STEPS
        - max_files_changed capped at _DEFAULT_MAX_FILES
        - allowed_skills restricted to safe improvement skills
        - requires_human_review if any critical finding exists
        """
        if not findings:
            return ImprovementPlan(
                improvement_id=self._generate_id(),
                source_plan_id=source_plan_id,
                findings=[],
                goals=["No findings — no improvement needed"],
                max_steps=0,
                max_files_changed=0,
                status="skipped",
            )

        # Derive goals from findings
        goals = self._derive_goals(findings)

        # Determine bounds based on severity
        has_critical = any(f.severity == "critical" for f in findings)
        has_high = any(f.severity == "high" for f in findings)

        max_steps = min(len(findings), _DEFAULT_MAX_STEPS)
        max_files = _DEFAULT_MAX_FILES if has_high or has_critical else 3

        return ImprovementPlan(
            improvement_id=self._generate_id(),
            source_plan_id=source_plan_id,
            findings=findings,
            goals=goals,
            allowed_skills=list(_ALLOWED_SKILLS),
            max_steps=max_steps,
            max_files_changed=max_files,
            requires_human_review=has_critical,
            status="queued",
        )

    def should_execute(self, plan: ImprovementPlan) -> bool:
        """Determine if an improvement plan should be auto-executed.

        Rules (deterministic):
        - No findings → False
        - Status not 'queued' → False
        - requires_human_review → False (needs human gate)
        - max_steps == 0 → False
        - Otherwise → True
        """
        if not plan.findings:
            return False
        if plan.status != "queued":
            return False
        if plan.requires_human_review:
            return False
        if plan.max_steps == 0:
            return False
        return True

    def persist_improvement_run(
        self,
        plan: ImprovementPlan,
        result: ImprovementResult,
    ) -> Path:
        """Persist an improvement run to STATE/improvement_runs/.

        Returns the path to the saved JSON file.
        """
        IMPROVEMENT_DIR.mkdir(parents=True, exist_ok=True)
        filename = f"{plan.improvement_id}.json"
        path = IMPROVEMENT_DIR / filename

        data = {
            "improvement_id": plan.improvement_id,
            "source_plan_id": plan.source_plan_id,
            "findings": [
                {
                    "finding_id": f.finding_id,
                    "category": f.category,
                    "severity": f.severity,
                    "summary": f.summary,
                    "evidence": f.evidence,
                }
                for f in plan.findings
            ],
            "goals": plan.goals,
            "allowed_skills": plan.allowed_skills,
            "max_steps": plan.max_steps,
            "max_files_changed": plan.max_files_changed,
            "requires_human_review": plan.requires_human_review,
            "plan_status": plan.status,
            "result": {
                "executed": result.executed,
                "final_status": result.final_status,
                "evaluation_grade": result.evaluation_grade,
                "evaluation_score": result.evaluation_score,
                "followup_recommended": result.followup_recommended,
                "notes": result.notes,
            },
            "saved_at": time.time(),
        }

        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(path)
        logger.info("Persisted improvement run to %s", path)
        return path

    # -- internal helpers -----------------------------------------------------

    def _generate_id(self) -> str:
        """Generate a deterministic-ish improvement ID."""
        ts = int(time.time())
        return f"imp_{ts}"

    def _derive_goals(self, findings: list[HealthFinding]) -> list[str]:
        """Derive improvement goals from findings."""
        goals: list[str] = []
        categories_seen: set[str] = set()

        for f in findings:
            if f.category in categories_seen:
                continue
            categories_seen.add(f.category)

            if f.category == CAT_LOW_GRADE:
                goals.append(
                    "Improve execution quality to achieve grade B or higher"
                )
            elif f.category == CAT_CONTRACT_FAILURE:
                goals.append(
                    "Fix contract emission in failing steps"
                )
            elif f.category == CAT_RETRY_PATTERN:
                goals.append(
                    "Reduce retry frequency by improving first-attempt success"
                )
            elif f.category == CAT_SLOW_EXECUTION:
                goals.append(
                    "Optimize slow execution steps to complete within 30s"
                )
            elif f.category == CAT_VERIFICATION_WEAKNESS:
                goals.append(
                    "Strengthen verification coverage in affected steps"
                )

        return goals
