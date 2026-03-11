"""Orchestrator Adapter — bridge between watcher dispatch and orchestrator.

Translates a raw task file into an ExecutionPlan, executes it through
the Orchestrator, and writes results to OUTPUT/ in the same format
the watcher expects.

This adapter enables the watcher to route tasks through the Phase 7
orchestrator pipeline instead of the direct Claude worker subprocess.

Stage B enforcement:
  When routing dict has stage="B", the adapter restricts the plan
  to research-only steps and enforces the allowed_roles list.

Stage C enforcement:
  When routing dict has stage="C", the adapter builds coding or research
  plans depending on task class. Coding plans have a mandatory
  self-verification step. All Stage C plans are validated against the
  Stage C allowed/blocked skill sets. Verifier rejection blocks finalization.

Stage D enforcement:
  When routing dict has stage="D", the adapter adds system_inspect support.
  System tasks get a read-only inspection plan using only file-ops and
  self-verification. Shell-ops, git-ops, and task-execution are blocked.
  All other classes continue under Stage C rules.
"""

import json
import logging
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from planner.orchestrator import Orchestrator
from planner.pattern_feedback import finalize_pattern_trace, init_pattern_trace, log_pattern_trace
from planner.pattern_promoter import attempt_pattern_promotion
from planner.pattern_retriever import retrieve_pattern_guidance
from planner.schemas import ExecutionPlan, PlanStep
from planner.supervisor import Supervisor
from planner.vault_context import inject_vault_context
from planner.workflow_promoter import attempt_promotion
from tools.task_classifier import classify_task
from tools.vault_sync import check_sync_after_write

logger = logging.getLogger(__name__)

BASE_DIR = Path(os.environ.get("NOVACORE_ROOT", "/home/nova/nova-core"))
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

# Stage C: skills allowed for low-risk coding path
# Same as Stage B plus file-ops is used for bounded code edits
_STAGE_C_ALLOWED_SKILLS = frozenset({
    "web-research", "file-ops", "self-verification", "research-to-action",
    "http-fetch",
})

# Stage C: skills that are blocked (dangerous execution)
_STAGE_C_BLOCKED_SKILLS = frozenset({
    "shell-ops", "git-ops", "task-execution",
})

# Stage D: skills allowed for system_inspect (read-only path)
_STAGE_D_ALLOWED_SKILLS = frozenset({
    "web-research", "file-ops", "self-verification",
    "http-fetch", "reading-obsidian-memory",
})

# Stage D: skills that are blocked for system tasks (mutation-capable)
_STAGE_D_BLOCKED_SKILLS = frozenset({
    "shell-ops", "git-ops", "task-execution",
})

# Stage D system_report: skills allowed (superset of inspect — adds shell-ops)
_STAGE_D_REPORT_ALLOWED_SKILLS = frozenset({
    "web-research", "file-ops", "self-verification",
    "http-fetch", "reading-obsidian-memory",
    "shell-ops",  # bounded by system_shell_allowlist
})

# Stage D system_report: skills that remain blocked
_STAGE_D_REPORT_BLOCKED_SKILLS = frozenset({
    "git-ops", "task-execution",
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

    # Phase 5: inject read-only vault context for eligible tasks
    vault_ctx = inject_vault_context(task_class, task_text)
    advisory_context = vault_ctx.get("vault_advisory_context", "")

    # Stage enforcement: restrict steps based on rollout stage
    stage = (routing or {}).get("stage", "")
    if stage == "B":
        steps = _build_stageB_research_steps(stem, task_text)
        strategy = "stageB_research"
    elif stage == "D":
        if task_class == "system":
            # Check system_scope to decide inspect vs report plan
            system_scope = _get_system_scope(routing)
            if system_scope == "report":
                steps = _build_stageD_report_steps(stem, task_text)
                strategy = "stageD_system_report"
            else:
                steps = _build_stageD_inspect_steps(stem, task_text)
                strategy = "stageD_system_inspect"
        elif task_class in ("code_impl", "code_review"):
            steps = _build_stageC_coding_steps(stem, task_class, task_text)
            strategy = "stageD_coding"
        else:
            steps = _build_stageB_research_steps(stem, task_text)
            strategy = "stageD_research"
    elif stage == "C":
        if task_class in ("code_impl", "code_review"):
            steps = _build_stageC_coding_steps(stem, task_class, task_text)
            strategy = "stageC_coding"
        else:
            # Research path under Stage C — same steps as Stage B
            steps = _build_stageB_research_steps(stem, task_text)
            strategy = "stageC_research"
    else:
        steps = _build_steps_for_class(stem, task_class, task_text)
        strategy = f"orchestrated_{task_class}"

    # Phase 7: retrieve targeted agent-pattern guidance for eligible tasks
    pattern_ctx = retrieve_pattern_guidance(task_class, task_text)
    pattern_guidance = pattern_ctx.get("pattern_guidance_context", "")

    # Phase 5: inject advisory context into first step's inputs
    if advisory_context and steps:
        steps[0].inputs["vault_advisory_context"] = advisory_context
        steps[0].inputs["vault_note_paths"] = vault_ctx.get("vault_note_paths", [])
        logger.info(
            "VAULT CONTEXT INJECTED: %s — %d notes, reason=%s",
            stem, vault_ctx.get("vault_notes_found", 0),
            vault_ctx.get("vault_eligibility_reason", "?"),
        )
    elif not vault_ctx.get("vault_context_injected", False):
        logger.debug(
            "VAULT CONTEXT SKIPPED: %s — reason=%s",
            stem, vault_ctx.get("vault_eligibility_reason", "?"),
        )

    # Phase 7: inject pattern guidance into first step's inputs
    if pattern_guidance and steps:
        steps[0].inputs["pattern_guidance"] = pattern_guidance
        steps[0].inputs["pattern_paths"] = [
            p["path"] for p in pattern_ctx.get("selected_patterns", [])
        ]
        logger.info(
            "PATTERN GUIDANCE INJECTED: %s — %d patterns, reason=%s",
            stem, len(pattern_ctx.get("selected_patterns", [])),
            pattern_ctx.get("retrieval_reason", "?"),
        )
    elif not pattern_ctx.get("pattern_retrieval_activated", False):
        logger.debug(
            "PATTERN GUIDANCE SKIPPED: %s — reason=%s",
            stem, pattern_ctx.get("retrieval_reason", "?"),
        )

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


def _build_stageC_coding_steps(
    stem: str, task_class: str, task_text: str
) -> list[PlanStep]:
    """Build coding plan steps for Stage C rollout.

    These steps use file-ops for bounded code inspection/edits with a
    mandatory self-verification step. No shell-ops, git-ops, or
    task-execution steps. Verifier approval is required before finalization.
    """
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
                inputs={"task_text": task_text[:2000]},
            ),
            PlanStep(
                step_id=f"{stem}_verify",
                skill_name="self-verification",
                goal="Verify review completeness and contract (MANDATORY — verifier approval required)",
                inputs={},
            ),
        ]
    # code_impl (default)
    return [
        PlanStep(
            step_id=f"{stem}_analyze",
            skill_name="file-ops",
            goal="Analyze codebase and plan bounded changes",
            inputs={"task_text": task_text[:2000]},
        ),
        PlanStep(
            step_id=f"{stem}_implement",
            skill_name="file-ops",
            goal="Implement bounded code changes",
            inputs={"task_text": task_text[:2000]},
        ),
        PlanStep(
            step_id=f"{stem}_verify",
            skill_name="self-verification",
            goal="Verify implementation correctness (MANDATORY — verifier approval required)",
            inputs={},
        ),
    ]


def _get_system_scope(routing: dict | None) -> str:
    """Get system_scope from routing dict or feature flags.

    Returns 'inspect_only' by default (fail-closed).
    """
    if routing:
        ff = routing.get("feature_flags", {})
        scope = ff.get("system_scope", "inspect_only")
        return scope
    # Fallback: read from disk
    try:
        from tools.task_classifier import load_feature_flags
        flags = load_feature_flags()
        return flags.get("system_scope", "inspect_only")
    except Exception:
        return "inspect_only"


def _build_stageD_inspect_steps(
    stem: str, task_text: str
) -> list[PlanStep]:
    """Build read-only inspection plan steps for Stage D system_inspect.

    These steps use only inspect-safe skills: file-ops (for reading),
    web-research (for documentation), and self-verification.
    No shell-ops, git-ops, or task-execution steps.
    """
    return [
        PlanStep(
            step_id=f"{stem}_inspect",
            skill_name="file-ops",
            goal="Inspect system state and gather read-only information",
            inputs={"task_text": task_text[:2000]},
        ),
        PlanStep(
            step_id=f"{stem}_analyze",
            skill_name="file-ops",
            goal="Analyze findings and write inspection report",
            inputs={"task_text": task_text[:2000]},
        ),
        PlanStep(
            step_id=f"{stem}_verify",
            skill_name="self-verification",
            goal="Verify inspection completeness (MANDATORY — verifier approval required)",
            inputs={},
        ),
    ]


def _build_stageD_report_steps(
    stem: str, task_text: str
) -> list[PlanStep]:
    """Build system_report plan steps for Stage D.

    Extends inspect with bounded shell-ops for read-only diagnostics.
    Shell commands are validated against the system_shell_allowlist
    at execution time.
    """
    return [
        PlanStep(
            step_id=f"{stem}_inspect",
            skill_name="file-ops",
            goal="Inspect system state and gather read-only information",
            inputs={"task_text": task_text[:2000]},
        ),
        PlanStep(
            step_id=f"{stem}_diagnose",
            skill_name="shell-ops",
            goal=(
                "Run bounded read-only diagnostic commands (ps, df, free, "
                "uptime, journalctl, systemctl status). "
                "ALL commands must match the system_shell_allowlist. "
                "Do NOT run any mutation commands."
            ),
            inputs={"task_text": task_text[:2000], "shell_scope": "system_report_allowlist"},
        ),
        PlanStep(
            step_id=f"{stem}_report",
            skill_name="file-ops",
            goal="Compile diagnostic findings into a structured report",
            inputs={"task_text": task_text[:2000]},
        ),
        PlanStep(
            step_id=f"{stem}_verify",
            skill_name="self-verification",
            goal="Verify report completeness and confirm no mutations occurred (MANDATORY)",
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


def validate_stageC_plan(plan: ExecutionPlan) -> tuple[bool, str]:
    """Validate that a Stage C plan meets safety requirements.

    Returns (valid, reason). Fails closed.

    Requirements:
    1. All skills must be in _STAGE_C_ALLOWED_SKILLS
    2. No blocked skills
    3. Must include at least one self-verification step (verifier mandatory)
    """
    has_verifier = False
    for step in plan.steps:
        if step.skill_name in _STAGE_C_BLOCKED_SKILLS:
            return False, f"blocked_skill:{step.skill_name}:step:{step.step_id}"
        if step.skill_name not in _STAGE_C_ALLOWED_SKILLS:
            return False, f"unknown_skill:{step.skill_name}:step:{step.step_id}"
        if step.skill_name == "self-verification":
            has_verifier = True
    if not has_verifier:
        return False, "missing_mandatory_verifier_step"
    return True, "all_skills_allowed_verifier_present"


def validate_stageD_plan(plan: ExecutionPlan) -> tuple[bool, str]:
    """Validate that a Stage D system_inspect plan meets safety requirements.

    Returns (valid, reason). Fails closed.

    Requirements:
    1. All skills must be in _STAGE_D_ALLOWED_SKILLS
    2. No blocked skills (shell-ops, git-ops, task-execution)
    3. Must include at least one self-verification step (verifier mandatory)
    """
    has_verifier = False
    for step in plan.steps:
        if step.skill_name in _STAGE_D_BLOCKED_SKILLS:
            return False, f"blocked_skill:{step.skill_name}:step:{step.step_id}"
        if step.skill_name not in _STAGE_D_ALLOWED_SKILLS:
            return False, f"unknown_skill:{step.skill_name}:step:{step.step_id}"
        if step.skill_name == "self-verification":
            has_verifier = True
    if not has_verifier:
        return False, "missing_mandatory_verifier_step"
    return True, "all_skills_allowed_verifier_present"


def validate_stageD_report_plan(plan: ExecutionPlan) -> tuple[bool, str]:
    """Validate that a Stage D system_report plan meets safety requirements.

    Returns (valid, reason). Fails closed.

    Requirements:
    1. All skills must be in _STAGE_D_REPORT_ALLOWED_SKILLS
    2. No blocked skills (git-ops, task-execution)
    3. Must include at least one self-verification step (verifier mandatory)
    4. shell-ops steps must have shell_scope='system_report_allowlist' in inputs
    """
    has_verifier = False
    for step in plan.steps:
        if step.skill_name in _STAGE_D_REPORT_BLOCKED_SKILLS:
            return False, f"blocked_skill:{step.skill_name}:step:{step.step_id}"
        if step.skill_name not in _STAGE_D_REPORT_ALLOWED_SKILLS:
            return False, f"unknown_skill:{step.skill_name}:step:{step.step_id}"
        if step.skill_name == "self-verification":
            has_verifier = True
        if step.skill_name == "shell-ops":
            scope = step.inputs.get("shell_scope", "")
            if scope != "system_report_allowlist":
                return False, f"shell_step_missing_allowlist_scope:step:{step.step_id}"
    if not has_verifier:
        return False, "missing_mandatory_verifier_step"
    return True, "all_skills_allowed_verifier_present_shell_bounded"


def _claude_step_executor(step: PlanStep) -> tuple[str, bool, str]:
    """Execute a plan step by dispatching to a Claude subprocess.

    This is the step executor that bridges orchestrator steps to
    actual Claude worker execution.
    """
    vault_ctx = step.inputs.get("vault_advisory_context", "")
    vault_section = f"\n{vault_ctx}\n" if vault_ctx else ""

    pattern_ctx = step.inputs.get("pattern_guidance", "")
    pattern_section = f"\n{pattern_ctx}\n" if pattern_ctx else ""

    prompt = (
        f"You are executing step '{step.step_id}' of an orchestrated plan.\n"
        f"Goal: {step.goal}\n"
        f"Skill: {step.skill_name}\n\n"
        f"Task context:\n{step.inputs.get('task_text', '(no context)')}\n\n"
        f"{vault_section}"
        f"{pattern_section}"
        f"Execute this step and produce output.\n\n"
        f"IMPORTANT: You MUST end your output with a ## CONTRACT block "
        f"containing these exact fields:\n"
        f"## CONTRACT\n"
        f"summary: <one-line description of what you did>\n"
        f"files_changed: <comma-separated list of files modified, or 'none'>\n"
        f"verification: <how you verified correctness>\n"
        f"confidence: <high|medium|low>\n"
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


# ---------------------------------------------------------------------------
# Phase 7.12b — Live workflow state persistence for rollout evidence
# ---------------------------------------------------------------------------

# Map orchestrator summary status → workflow record status
_ORCHESTRATOR_STATUS_MAP = {
    "done": "completed",
    "failed": "failed",
    "partial": "failed",
    "rejected": "failed",
}


def _persist_workflow_state(
    stem: str,
    status: str,
    task_class: str = "",
    stage: str = "",
    halt_reason: str | None = None,
) -> Path | None:
    """Persist a workflow state record for rollout evidence and operational auditing.

    Writes to STATE/workflows/{stem}.json using atomic tmp+rename.
    The persisted record is compatible with rollout_gate.collect_evidence().
    """
    wf_dir = STATE_DIR / "workflows"
    wf_dir.mkdir(parents=True, exist_ok=True)

    now = time.time()
    record = {
        "workflow_id": stem,
        "status": status,
        "task_class": task_class,
        "stage": stage,
        "execution_path": "orchestrator",
        "created_at": now,
        "completed_at": now,
    }
    if halt_reason:
        record["halt_reason"] = halt_reason

    wf_path = wf_dir / f"{stem}.json"
    tmp_path = wf_path.with_suffix(".json.tmp")
    try:
        tmp_path.write_text(json.dumps(record, indent=2), encoding="utf-8")
        tmp_path.replace(wf_path)
        logger.info("WORKFLOW STATE: %s → %s (%s)", stem, status, wf_path.name)
        return wf_path
    except OSError as exc:
        logger.error("WORKFLOW STATE WRITE FAILED: %s — %s", stem, exc)
        return None


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
    task_class, _ = classify_task(task_text)
    logger.info("ORCHESTRATOR DISPATCH: %s (stage=%s)", stem, stage or "default")

    # Build plan
    plan = build_plan_from_task(stem, task_text, routing)
    logger.info(
        "Plan built: %s (%d steps, strategy=%s)",
        plan.plan_id, len(plan.steps), plan.strategy,
    )

    # Phase 7.5: initialize pattern feedback trace
    pattern_trace = init_pattern_trace(stem, task_class, plan)

    # Stage B: validate plan before execution
    if stage == "B":
        valid, reason = validate_stageB_plan(plan)
        if not valid:
            logger.error("STAGE B PLAN REJECTED: %s — %s", stem, reason)
            _persist_workflow_state(stem, "rejected", task_class, stage,
                                   halt_reason=f"plan_validation: {reason}")
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

    # Stage D: validate system plan (inspect or report) or coding/research
    elif stage == "D":
        if task_class == "system":
            system_scope = _get_system_scope(routing)
            if system_scope == "report":
                valid, reason = validate_stageD_report_plan(plan)
            else:
                valid, reason = validate_stageD_plan(plan)
            if not valid:
                logger.error("STAGE D PLAN REJECTED: %s — %s", stem, reason)
                _persist_workflow_state(stem, "rejected", task_class, stage,
                                       halt_reason=f"plan_validation: {reason}")
                return {
                    "success": False,
                    "output_path": None,
                    "plan_summary": {
                        "plan_id": plan.plan_id,
                        "task_id": stem,
                        "status": "rejected",
                        "error": f"Stage D plan validation failed: {reason}",
                    },
                }
            logger.info("STAGE D PLAN VALIDATED: %s — inspect-only skills confirmed", stem)
        else:
            # Non-system under Stage D: validate with Stage C rules
            valid, reason = validate_stageC_plan(plan)
            if not valid:
                logger.error("STAGE D (C-rules) PLAN REJECTED: %s — %s", stem, reason)
                _persist_workflow_state(stem, "rejected", task_class, stage,
                                       halt_reason=f"plan_validation: {reason}")
                return {
                    "success": False,
                    "output_path": None,
                    "plan_summary": {
                        "plan_id": plan.plan_id,
                        "task_id": stem,
                        "status": "rejected",
                        "error": f"Stage D plan validation failed: {reason}",
                    },
                }
            logger.info("STAGE D PLAN VALIDATED: %s — C-rules confirmed", stem)

    # Stage C: validate plan before execution (must include verifier step)
    elif stage == "C":
        valid, reason = validate_stageC_plan(plan)
        if not valid:
            logger.error("STAGE C PLAN REJECTED: %s — %s", stem, reason)
            _persist_workflow_state(stem, "rejected", task_class, stage,
                                   halt_reason=f"plan_validation: {reason}")
            return {
                "success": False,
                "output_path": None,
                "plan_summary": {
                    "plan_id": plan.plan_id,
                    "task_id": stem,
                    "status": "rejected",
                    "error": f"Stage C plan validation failed: {reason}",
                },
            }
        logger.info("STAGE C PLAN VALIDATED: %s — verifier-gated path confirmed", stem)

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

    # Stage C/D verifier enforcement: check verification step result
    if stage in ("C", "D") and (routing or {}).get("verifier_required", False):
        verify_steps = [
            s for s in summary.get("steps", [])
            if "verify" in s.get("step_id", "")
        ]
        if not verify_steps:
            logger.warning(
                "STAGE C VERIFIER MISSING: %s — no verification step in results",
                stem,
            )
            summary["status"] = "rejected"
            summary["verifier_rejected"] = True
        else:
            last_verify = verify_steps[-1]
            if last_verify.get("status") not in ("done", "success"):
                logger.warning(
                    "STAGE C VERIFIER REJECTED: %s — verification step status=%s",
                    stem, last_verify.get("status"),
                )
                summary["status"] = "rejected"
                summary["verifier_rejected"] = True
            else:
                logger.info("STAGE C VERIFIER APPROVED: %s", stem)

    # Phase 7.12b: persist workflow state for rollout evidence
    orch_status = summary.get("status", "failed")
    wf_status = _ORCHESTRATOR_STATUS_MAP.get(orch_status, "failed")
    wf_halt = None
    if summary.get("verifier_rejected"):
        wf_halt = "verifier_rejected"
    elif orch_status == "failed" and summary.get("error"):
        wf_halt = summary["error"][:200]
    _persist_workflow_state(stem, wf_status, task_class, stage, halt_reason=wf_halt)

    # Write output report
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    output_path = OUTPUT_DIR / f"{stem}__{stamp}.md"

    report = _build_orchestrator_report(stem, plan, summary, task_path, stage)
    output_path.write_text(report, encoding="utf-8")
    logger.info("Orchestrator output: %s", output_path)

    # Write routing audit log
    _log_routing_decision(stem, plan, summary, stage)

    # Phase 5.5: controlled post-execution workflow-learning promotion
    promotion_result = None
    if summary.get("status") == "done" and stage in ("B", "C", "D"):
        promotion_result = attempt_promotion(
            stem=stem,
            task_class=task_class,
            task_text=task_text,
            plan_summary=summary,
            strategy=plan.strategy,
        )
        if promotion_result.get("promoted"):
            logger.info(
                "WORKFLOW PROMOTED: %s → %s",
                stem, promotion_result.get("note_path"),
            )
        else:
            logger.debug(
                "PROMOTION SKIPPED: %s — %s",
                stem, promotion_result.get("reason", "?"),
            )

    # Phase 6.5: controlled agent-pattern promotion from converging learnings
    pattern_result = None
    if (
        promotion_result
        and promotion_result.get("promoted")
        and promotion_result.get("note_path")
        and promotion_result.get("learning_id")
    ):
        try:
            pattern_result = attempt_pattern_promotion(
                task_class=task_class,
                task_text=task_text,
                learning_id=promotion_result["learning_id"],
                learning_path=promotion_result["note_path"],
            )
            if pattern_result.get("promoted"):
                logger.info(
                    "PATTERN PROMOTED: %s → %s (%d evidence)",
                    stem,
                    pattern_result.get("note_path"),
                    pattern_result.get("evidence_count", 0),
                )
            else:
                logger.debug(
                    "PATTERN DEFERRED: %s — %s",
                    stem, pattern_result.get("reason", "?"),
                )
        except Exception as exc:
            logger.warning("PATTERN PROMOTION ERROR: %s — %s", stem, exc)
            pattern_result = {
                "promoted": False,
                "reason": f"error:{exc}",
                "errors": [str(exc)],
            }

    # Phase 7.5: finalize pattern feedback trace and log
    try:
        finalize_pattern_trace(pattern_trace, summary)
        log_pattern_trace(pattern_trace)
    except Exception as exc:
        logger.warning("PATTERN FEEDBACK TRACING ERROR: %s — %s", stem, exc)

    # Phase 10: post-write Obsidian sync check
    sync_result = None
    try:
        # Check sync for workflow-learning promotion
        if promotion_result and promotion_result.get("promoted"):
            sync_result = check_sync_after_write(
                write_path=promotion_result.get("note_path", ""),
                write_status="created",
            )
        # Check sync for agent-pattern promotion (may override with latest)
        if pattern_result and pattern_result.get("promoted"):
            sync_result = check_sync_after_write(
                write_path=pattern_result.get("note_path", ""),
                write_status="created",
            )
    except Exception as exc:
        logger.warning("VAULT SYNC CHECK ERROR: %s — %s", stem, exc)
        sync_result = {
            "obsidian_sync_attempted": False,
            "obsidian_sync_result": "error",
            "obsidian_sync_error": str(exc),
        }

    return {
        "success": summary.get("status") == "done",
        "output_path": str(output_path),
        "plan_summary": summary,
        "promotion": promotion_result,
        "pattern_promotion": pattern_result,
        "pattern_feedback": pattern_trace,
        "obsidian_sync": sync_result,
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

    report += "\n## CONTRACT\n"
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
