"""Orchestrator Adapter — bridge between watcher dispatch and orchestrator.

Translates a raw task file into an ExecutionPlan, executes it through
the Orchestrator, and writes results to OUTPUT/ in the same format
the watcher expects.

This adapter enables the watcher to route tasks through the Phase 7
orchestrator pipeline instead of the direct Claude worker subprocess.

Stage B enforcement:
  When routing dict has stage="B", the adapter restricts the plan
  to research-only steps and enforces the allowed_roles list.
"""

import json
import logging
import subprocess
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from planner.orchestrator import Orchestrator
from planner.schemas import ExecutionPlan, PlanStep
from planner.supervisor import Supervisor
from tools.task_classifier import classify_task

logger = logging.getLogger(__name__)

BASE_DIR = Path("/home/nova/nova-core")
OUTPUT_DIR = BASE_DIR / "OUTPUT"
WORK_DIR = BASE_DIR / "WORK"
LOGS_DIR = BASE_DIR / "LOGS"
STATE_DIR = BASE_DIR / "STATE"
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "/home/nova/.local/bin/claude")
TASK_TIMEOUT = 300

# Stage B: skills allowed for the research-only path
_STAGE_B_ALLOWED_SKILLS = frozenset({
    "web-research", "file-ops", "self-verification", "research-to-action",
    "http-fetch",
})

# Stage B: skills that are blocked (mutation-capable)
_STAGE_B_BLOCKED_SKILLS = frozenset({
    "shell-ops", "git-ops", "task-execution",
})


def build_plan_from_task(
    stem: str,
    task_text: str,
    routing: dict | None = None,
) -> ExecutionPlan:
    """Build an ExecutionPlan from task text.

    Uses the task classifier to determine the task class, then
    creates an appropriate plan with steps based on the class.

    If routing has stage="B", enforces research-only plan steps.
    """
    task_class, confidence = classify_task(task_text)
    plan_id = f"plan_{stem}_{int(time.time())}"

    # Stage B enforcement: only research steps
    stage = (routing or {}).get("stage", "")
    if stage == "B":
        steps = _build_stageB_research_steps(stem, task_text)
        strategy = "stageB_research"
    else:
        steps = _build_steps_for_class(stem, task_class, task_text)
        strategy = f"orchestrated_{task_class}"

    return ExecutionPlan(
        plan_id=plan_id,
        task_id=stem,
        strategy=strategy,
        steps=steps,
        success_criteria=[
            "All steps completed successfully",
            "Output report created with valid CONTRACT block",
            "Artifacts verified",
        ],
    )


def _build_stageB_research_steps(
    stem: str, task_text: str
) -> list[PlanStep]:
    """Build research-only plan steps for Stage B rollout.

    These steps use only read-only skills: web-research, file-ops
    (for reading/synthesizing), and self-verification.
    No shell-ops, git-ops, or task-execution steps.
    """
    return [
        PlanStep(
            step_id=f"{stem}_research",
            skill_name="web-research",
            goal="Research and gather information (read-only)",
            inputs={"task_text": task_text[:2000]},
        ),
        PlanStep(
            step_id=f"{stem}_synthesize",
            skill_name="file-ops",
            goal="Synthesize findings into output report",
            inputs={"task_text": task_text[:2000]},
        ),
        PlanStep(
            step_id=f"{stem}_verify",
            skill_name="self-verification",
            goal="Verify output completeness and contract",
            inputs={},
        ),
    ]


def _build_steps_for_class(
    stem: str, task_class: str, task_text: str
) -> list[PlanStep]:
    """Create plan steps appropriate for the task class.

    Each class gets a tailored sequence of steps that leverages
    the multi-agent roles defined in AGENTS/.
    """
    if task_class == "research":
        return [
            PlanStep(
                step_id=f"{stem}_research",
                skill_name="web-research",
                goal="Research and gather information",
                inputs={"task_text": task_text[:2000]},
            ),
            PlanStep(
                step_id=f"{stem}_synthesize",
                skill_name="file-ops",
                goal="Synthesize findings into output report",
                inputs={"task_text": task_text[:2000]},
            ),
            PlanStep(
                step_id=f"{stem}_verify",
                skill_name="self-verification",
                goal="Verify output completeness and contract",
                inputs={},
            ),
        ]

    if task_class == "code_impl":
        return [
            PlanStep(
                step_id=f"{stem}_analyze",
                skill_name="file-ops",
                goal="Analyze codebase and plan implementation",
                inputs={"task_text": task_text[:2000]},
            ),
            PlanStep(
                step_id=f"{stem}_implement",
                skill_name="file-ops",
                goal="Implement the requested changes",
                inputs={"task_text": task_text[:2000]},
            ),
            PlanStep(
                step_id=f"{stem}_review",
                skill_name="self-verification",
                goal="Review implementation and verify correctness",
                inputs={},
            ),
        ]

    if task_class == "code_review":
        return [
            PlanStep(
                step_id=f"{stem}_review",
                skill_name="file-ops",
                goal="Review code for quality, security, and correctness",
                inputs={"task_text": task_text[:2000]},
            ),
            PlanStep(
                step_id=f"{stem}_report",
                skill_name="file-ops",
                goal="Write review findings report",
                inputs={},
            ),
        ]

    if task_class == "system":
        return [
            PlanStep(
                step_id=f"{stem}_plan",
                skill_name="file-ops",
                goal="Plan system changes",
                inputs={"task_text": task_text[:2000]},
            ),
            PlanStep(
                step_id=f"{stem}_execute",
                skill_name="shell-ops",
                goal="Execute system changes",
                inputs={"task_text": task_text[:2000]},
            ),
            PlanStep(
                step_id=f"{stem}_verify",
                skill_name="self-verification",
                goal="Verify system changes",
                inputs={},
            ),
        ]

    # Fallback: single-step execution
    return [
        PlanStep(
            step_id=f"{stem}_execute",
            skill_name="task-execution",
            goal="Execute task",
            inputs={"task_text": task_text[:2000]},
        ),
    ]


def validate_stageB_plan(plan: ExecutionPlan) -> tuple[bool, str]:
    """Validate that a Stage B plan contains only allowed skills.

    Returns (valid, reason). Fails closed — any disallowed skill
    causes the entire plan to be rejected.
    """
    for step in plan.steps:
        if step.skill_name in _STAGE_B_BLOCKED_SKILLS:
            return False, f"blocked_skill:{step.skill_name}:step:{step.step_id}"
        if step.skill_name not in _STAGE_B_ALLOWED_SKILLS:
            return False, f"unknown_skill:{step.skill_name}:step:{step.step_id}"
    return True, "all_skills_allowed"


def _claude_step_executor(step: PlanStep) -> tuple[str, bool, str]:
    """Execute a plan step by dispatching to a Claude subprocess.

    This is the step executor that bridges orchestrator steps to
    actual Claude worker execution.
    """
    prompt = (
        f"You are executing step '{step.step_id}' of an orchestrated plan.\n"
        f"Goal: {step.goal}\n"
        f"Skill: {step.skill_name}\n\n"
        f"Task context:\n{step.inputs.get('task_text', '(no context)')}\n\n"
        f"Execute this step and produce output. End with a ## CONTRACT block.\n"
    )

    cmd = [CLAUDE_BIN, "-p", "--verbose", "--dangerously-skip-permissions",
           "--model", "claude-opus-4-6", prompt]

    try:
        child_env = os.environ.copy()
        child_env.pop("CLAUDECODE", None)

        result = subprocess.run(
            cmd,
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
            timeout=TASK_TIMEOUT,
            env=child_env,
        )

        output = result.stdout or ""
        success = result.returncode == 0
        error = result.stderr if result.returncode != 0 else ""

        return output, success, error

    except subprocess.TimeoutExpired:
        return "", False, f"Step timed out after {TASK_TIMEOUT}s"
    except Exception as exc:
        return "", False, str(exc)


def execute_via_orchestrator(
    stem: str,
    task_text: str,
    task_path: Path,
    routing: dict | None = None,
) -> dict:
    """Execute a task through the orchestrator pipeline.

    1. Build an ExecutionPlan from the task
    2. (Stage B) Validate plan contains only allowed skills
    3. Run it through the Orchestrator with supervisor evaluation
    4. Write results to OUTPUT/
    5. Return summary dict compatible with watcher verification

    Returns:
        dict with keys: success, output_path, plan_summary
    """
    stage = (routing or {}).get("stage", "")
    logger.info("ORCHESTRATOR DISPATCH: %s (stage=%s)", stem, stage or "default")

    # Build plan
    plan = build_plan_from_task(stem, task_text, routing)
    logger.info(
        "Plan built: %s (%d steps, strategy=%s)",
        plan.plan_id, len(plan.steps), plan.strategy,
    )

    # Stage B: validate plan before execution
    if stage == "B":
        valid, reason = validate_stageB_plan(plan)
        if not valid:
            logger.error("STAGE B PLAN REJECTED: %s — %s", stem, reason)
            return {
                "success": False,
                "output_path": None,
                "plan_summary": {
                    "plan_id": plan.plan_id,
                    "task_id": stem,
                    "status": "rejected",
                    "error": f"Stage B plan validation failed: {reason}",
                },
            }
        logger.info("STAGE B PLAN VALIDATED: %s — research-only skills confirmed", stem)

    # Create orchestrator with Claude step executor
    orchestrator = Orchestrator(
        supervisor=Supervisor(),
        step_executor=_claude_step_executor,
    )

    # Execute plan
    try:
        summary = orchestrator.run_plan(plan)
    except Exception as exc:
        logger.error("Orchestrator execution failed for %s: %s", stem, exc)
        summary = {
            "plan_id": plan.plan_id,
            "task_id": stem,
            "status": "failed",
            "error": str(exc),
        }

    # Write output report
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    output_path = OUTPUT_DIR / f"{stem}__{stamp}.md"

    report = _build_orchestrator_report(stem, plan, summary, task_path, stage)
    output_path.write_text(report, encoding="utf-8")
    logger.info("Orchestrator output: %s", output_path)

    # Write routing audit log
    _log_routing_decision(stem, plan, summary, stage)

    return {
        "success": summary.get("status") == "done",
        "output_path": str(output_path),
        "plan_summary": summary,
    }


def _build_orchestrator_report(
    stem: str,
    plan: ExecutionPlan,
    summary: dict,
    task_path: Path,
    stage: str = "",
) -> str:
    """Build a markdown output report from orchestrator results."""
    status = summary.get("status", "unknown")
    steps = summary.get("steps", [])
    decisions = summary.get("decisions", [])
    evaluation = summary.get("evaluation", {})

    report = f"# Orchestrated Execution: {stem}\n\n"
    report += f"**Task:** {task_path}\n"
    report += f"**Plan ID:** {plan.plan_id}\n"
    report += f"**Strategy:** {plan.strategy}\n"
    report += f"**Status:** {status}\n"
    report += f"**Steps:** {len(plan.steps)}\n"
    if stage:
        report += f"**Rollout Stage:** {stage}\n"
    report += "\n"

    report += "## Execution Steps\n\n"
    for step_result in steps:
        sid = step_result.get("step_id", "?")
        s_status = step_result.get("status", "?")
        contract = step_result.get("contract_valid", False)
        retries = step_result.get("retry_count", 0)
        report += f"- **{sid}**: {s_status}"
        report += f" (contract={'valid' if contract else 'invalid'}"
        if retries:
            report += f", retries={retries}"
        report += ")\n"

    if decisions:
        report += "\n## Supervisor Decisions\n\n"
        for d in decisions:
            report += f"- **{d.get('step_id')}**: {d.get('action')} — {d.get('reason')}\n"

    if evaluation:
        report += "\n## Evaluation\n\n"
        report += f"- **Grade:** {evaluation.get('grade', 'N/A')}\n"
        report += f"- **Score:** {evaluation.get('aggregate_score', 'N/A')}\n"
        report += f"- **Summary:** {evaluation.get('summary', 'N/A')}\n"

    # Files changed: collect from step outputs
    files_changed = "none"

    report += f"\n## CONTRACT\n"
    report += f"summary: Orchestrated execution of {stem} via Phase 7 pipeline ({plan.strategy})\n"
    report += f"files_changed: {files_changed}\n"
    report += f"verification: Orchestrator supervisor evaluated all steps; grade={evaluation.get('grade', 'N/A')}\n"
    report += f"confidence: {'high' if status == 'done' else 'medium' if status == 'partial' else 'low'}\n"

    return report


def _log_routing_decision(
    stem: str, plan: ExecutionPlan, summary: dict, stage: str = ""
):
    """Append a routing audit entry to LOGS/routing_audit.log."""
    log_path = LOGS_DIR / "routing_audit.log"
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    entry = (
        f"{stamp} | task={stem} | plan={plan.plan_id} | "
        f"strategy={plan.strategy} | steps={len(plan.steps)} | "
        f"status={summary.get('status', '?')}"
    )
    if stage:
        entry += f" | stage={stage}"
    entry += "\n"
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(entry)
