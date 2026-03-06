"""PlanBuilder — converts a task into a single-skill or multi-skill execution plan.

Implements the roadmap's skill composition: one task can chain multiple skills.
First version uses deterministic keyword rules for multi-skill detection.
"""

from __future__ import annotations

import re

from planner.schemas import (
    ExecutionPlan,
    PlanStep,
    SkillScore,
    TaskIntent,
)

# ---------------------------------------------------------------------------
# Deterministic multi-skill chain rules (v1)
#
# Each rule: (condition_regex, skill_chain_with_goals)
# Checked in order; first match wins.
# ---------------------------------------------------------------------------

_DIAGNOSE_WORDS = re.compile(
    r"\b(diagnose|logs?|failure|crash|traceback|error)\b", re.I
)
_FIX_WORDS = re.compile(
    r"\b(fix|patch|change\s+code|repair|correct)\b", re.I
)
_SERVICE_WORDS = re.compile(
    r"\b(restart|service|status|systemctl|daemon|reload)\b", re.I
)

CHAIN_RULES: list[tuple[re.Pattern, list[tuple[str, str]]]] = [
    # diagnose + fix → log_triage → code_improve → system_supervisor
    (
        _DIAGNOSE_WORDS,
        [
            ("log_triage", "Identify failure source"),
            ("code_improve", "Apply minimal fix"),
            ("system_supervisor", "Validate and decide next action"),
        ],
    ),
    # fix/patch → code_improve → system_supervisor
    (
        _FIX_WORDS,
        [
            ("code_improve", "Apply code change"),
            ("system_supervisor", "Validate change"),
        ],
    ),
    # service ops → service_ops → system_supervisor
    (
        _SERVICE_WORDS,
        [
            ("service_ops", "Perform service operation"),
            ("system_supervisor", "Verify service state"),
        ],
    ),
]


class PlanBuilder:
    """Build execution plans from task intents and ranked skills."""

    def build_intent(
        self,
        task_id: str,
        raw_text: str,
        source: str = "worker",
    ) -> TaskIntent:
        """Parse raw task text into a structured TaskIntent.

        Sets goal to raw_text per spec.
        """
        return TaskIntent(
            task_id=task_id,
            goal=raw_text,
            source=source,
        )

    def build_plan(
        self,
        intent: TaskIntent,
        ranked_skills: list[SkillScore],
    ) -> ExecutionPlan:
        """Build an execution plan from an intent and ranked skills.

        Chain rules are checked first (deterministic multi-skill patterns).
        Falls back to top-ranked single skill.
        """
        plan_id = f"plan_{intent.task_id}"

        # Try deterministic chain rules first
        for pattern, chain in CHAIN_RULES:
            if pattern.search(intent.goal):
                steps = [
                    PlanStep(
                        step_id=f"s{i + 1}",
                        skill_name=skill_name,
                        goal=goal,
                    )
                    for i, (skill_name, goal) in enumerate(chain)
                ]
                return ExecutionPlan(
                    plan_id=plan_id,
                    task_id=intent.task_id,
                    strategy="multi_skill",
                    steps=steps,
                    success_criteria=["contract valid", "verification present"],
                )

        # Fallback: pick top-ranked single skill
        if ranked_skills:
            top = ranked_skills[0]
            step = PlanStep(
                step_id="s1",
                skill_name=top.skill_name,
                goal=f"Execute {top.skill_name} for task",
            )
            return ExecutionPlan(
                plan_id=plan_id,
                task_id=intent.task_id,
                strategy="single_skill",
                steps=[step],
                success_criteria=["contract valid"],
            )

        # No skills matched — return empty plan
        return ExecutionPlan(
            plan_id=plan_id,
            task_id=intent.task_id,
            strategy="single_skill",
            steps=[],
            success_criteria=[],
        )
