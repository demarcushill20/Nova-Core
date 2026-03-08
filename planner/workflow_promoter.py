"""Phase 5.5 — Controlled post-execution workflow-learning promotion.

Decides whether a completed governed workflow contains durable reusable
learning worth promoting to the Obsidian vault, and if so, compacts and
writes it via the existing bounded write path.

Design constraints:
  - Promotion is post-execution only, never mid-execution.
  - Promotion is selective: only eligible successful workflows qualify.
  - Promotion reuses vault_validate + vault_write (bounded write path).
  - Promotion produces one compact workflow-learning note per workflow.
  - Failed, thin, trivial, or runtime-state outputs are never promoted.
  - All promotion attempts are logged for auditability.
  - Fail-open: promotion failure does not block task completion.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Promotion eligibility
# ---------------------------------------------------------------------------

# Task classes eligible for promotion
_PROMOTABLE_TASK_CLASSES = frozenset({"research", "code_impl", "code_review"})

# Minimum evaluation grade for promotion (A or B)
_MIN_PROMOTION_GRADES = frozenset({"A", "B"})

# Minimum step count to indicate non-trivial work
_MIN_STEPS_FOR_PROMOTION = 2

# Minimum total output size (bytes) across step outputs to avoid thin results
_MIN_OUTPUT_SIZE = 200

# Signals that indicate durable learning in the output
_LEARNING_SIGNALS = re.compile(
    r"\b(learn|lesson|caveat|anti.?pattern|gotcha|what.?worked|what.?failed|"
    r"reusable|guidance|insight|finding|discovered|decision|tradeoff|"
    r"pattern|approach|improvement|workaround|root.?cause|regression|"
    r"debugging|resolved|fix|refactor)\b",
    re.IGNORECASE,
)

# Minimum learning signal matches to consider output promotion-worthy
_MIN_LEARNING_SIGNALS = 2

# Roles eligible for the roles_involved field
_VALID_ROLES = frozenset({
    "research", "coder", "critic", "verifier", "planner", "memory",
})


def is_eligible_for_promotion(
    task_class: str,
    plan_summary: dict[str, Any],
    task_text: str,
) -> tuple[bool, str]:
    """Determine whether a completed workflow is eligible for promotion.

    Returns (eligible, reason). Fails closed on uncertainty.
    """
    # 1. Task class must be promotable
    if task_class not in _PROMOTABLE_TASK_CLASSES:
        return False, f"ineligible_class:{task_class}"

    # 2. Plan must have completed successfully
    status = plan_summary.get("status", "")
    if status != "done":
        return False, f"not_done:{status}"

    # 3. Must not be verifier-rejected
    if plan_summary.get("verifier_rejected", False):
        return False, "verifier_rejected"

    # 4. Evaluation grade must be sufficient
    evaluation = plan_summary.get("evaluation", {})
    grade = evaluation.get("grade", "")
    if grade not in _MIN_PROMOTION_GRADES:
        return False, f"low_grade:{grade}"

    # 5. Must have enough steps (non-trivial work)
    steps = plan_summary.get("steps", [])
    if len(steps) < _MIN_STEPS_FOR_PROMOTION:
        return False, f"too_few_steps:{len(steps)}"

    # 6. Check for learning signals in task text + step outputs
    combined_text = task_text
    for step in steps:
        # Step outputs may be in validation_errors or output_path
        combined_text += " " + " ".join(step.get("validation_errors", []))

    signal_count = len(_LEARNING_SIGNALS.findall(combined_text))
    if signal_count < _MIN_LEARNING_SIGNALS:
        return False, f"insufficient_signals:{signal_count}"

    return True, "eligible"


# ---------------------------------------------------------------------------
# Compaction: extract learning from plan summary
# ---------------------------------------------------------------------------

def compact_workflow_learning(
    stem: str,
    task_class: str,
    task_text: str,
    plan_summary: dict[str, Any],
    strategy: str,
) -> dict[str, Any]:
    """Compact a plan summary into a workflow-learning frontmatter + body.

    Returns {"frontmatter": dict, "body": str, "path": str}.
    """
    evaluation = plan_summary.get("evaluation", {})
    steps = plan_summary.get("steps", [])
    decisions = plan_summary.get("decisions", [])

    # Build learning_id
    date_str = time.strftime("%Y-%m-%d", time.gmtime())
    slug = re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")
    learning_id = f"wl-{date_str}-{slug}"

    # Determine roles involved from step skill names
    skill_to_role = {
        "web-research": "research",
        "http-fetch": "research",
        "research-to-action": "research",
        "file-ops": "coder",
        "self-verification": "verifier",
        "shell-ops": "coder",
    }
    roles = set()
    for step in steps:
        skill = step.get("skill_name", "") if isinstance(step, dict) else ""
        role = skill_to_role.get(skill)
        if role and role in _VALID_ROLES:
            roles.add(role)
    if not roles:
        roles = {"coder"}

    # Determine verification outcome
    verify_steps = [s for s in steps if "verify" in s.get("step_id", "")]
    if verify_steps and verify_steps[-1].get("status") == "done":
        verification_outcome = "approved"
    elif verify_steps:
        verification_outcome = "partial"
    else:
        verification_outcome = "not_verified"

    # Build frontmatter
    frontmatter = {
        "type": "workflow-learning",
        "learning_id": learning_id,
        "title": _extract_title(task_text, stem),
        "workflow_id": plan_summary.get("plan_id", f"plan_{stem}"),
        "task_class": task_class,
        "verification_outcome": verification_outcome,
        "confidence": _grade_to_confidence(evaluation.get("grade", "")),
        "roles_involved": sorted(roles),
        "date": date_str,
        "source": "nova-core-memory",
        "tags": [
            "#type/learning",
            f"#confidence/{_grade_to_confidence(evaluation.get('grade', ''))}",
            "#status/active",
            "#source/auto-promoted",
        ],
    }

    # Build body
    body_parts = []

    body_parts.append("## Task Summary\n")
    body_parts.append(_truncate(task_text, 300) + "\n")

    body_parts.append("## Execution Summary\n")
    body_parts.append(f"- **Strategy**: {strategy}\n")
    body_parts.append(f"- **Steps**: {len(steps)}\n")
    body_parts.append(f"- **Grade**: {evaluation.get('grade', 'N/A')}\n")
    body_parts.append(f"- **Score**: {evaluation.get('aggregate_score', 'N/A')}\n")

    if decisions:
        body_parts.append("\n## Key Decisions\n")
        for d in decisions[:5]:
            body_parts.append(
                f"- **{d.get('step_id', '?')}**: "
                f"{d.get('action', '?')} — {d.get('reason', '?')}\n"
            )

    # Extract learning signals from evaluation summary
    eval_summary = evaluation.get("summary", "")
    if eval_summary:
        body_parts.append("\n## Reusable Guidance\n")
        body_parts.append(_truncate(eval_summary, 500) + "\n")

    body_parts.append("\n## Trace\n")
    body_parts.append(f"- **Workflow ID**: {plan_summary.get('plan_id', 'N/A')}\n")
    body_parts.append(f"- **Task stem**: {stem}\n")
    body_parts.append("- **Promotion**: auto-promoted by Phase 5.5\n")

    body = "\n".join(body_parts)
    path = f"30-workflow-learnings/{date_str}-{slug}.md"

    return {
        "frontmatter": frontmatter,
        "body": body,
        "path": path,
    }


def _extract_title(task_text: str, stem: str) -> str:
    """Extract a concise title from task text."""
    # Use first line or first 80 chars
    first_line = task_text.strip().split("\n")[0].strip()
    # Remove markdown headers
    first_line = re.sub(r"^#+\s*", "", first_line)
    if len(first_line) > 80:
        first_line = first_line[:77] + "..."
    return first_line or f"Workflow learning: {stem}"


def _grade_to_confidence(grade: str) -> str:
    if grade in ("A", "A+"):
        return "high"
    if grade in ("B", "B+"):
        return "medium"
    return "low"


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len - 3] + "..."


# ---------------------------------------------------------------------------
# Promotion execution (uses bounded vault write path)
# ---------------------------------------------------------------------------

def attempt_promotion(
    stem: str,
    task_class: str,
    task_text: str,
    plan_summary: dict[str, Any],
    strategy: str,
) -> dict[str, Any]:
    """Attempt to promote a workflow outcome into a vault learning note.

    Returns a promotion result dict:
    {
        "promoted": bool,
        "reason": str,
        "note_path": str | None,
        "learning_id": str | None,
        "errors": list[str],
    }

    Fail-open: any error returns promoted=False, does not block execution.
    """
    # 1. Check eligibility
    eligible, reason = is_eligible_for_promotion(task_class, plan_summary, task_text)
    if not eligible:
        logger.debug("PROMOTION SKIPPED: %s — %s", stem, reason)
        return {
            "promoted": False,
            "reason": reason,
            "note_path": None,
            "learning_id": None,
            "errors": [],
        }

    # 2. Compact into learning note
    try:
        learning = compact_workflow_learning(
            stem, task_class, task_text, plan_summary, strategy,
        )
    except Exception as exc:
        logger.warning("PROMOTION COMPACTION FAILED: %s — %s", stem, exc)
        return {
            "promoted": False,
            "reason": f"compaction_error:{exc}",
            "note_path": None,
            "learning_id": None,
            "errors": [str(exc)],
        }

    # 3. Validate via bounded write path
    try:
        from tools.mcp_vault_server import vault_validate, vault_write
    except ImportError:
        logger.warning("PROMOTION SKIPPED: %s — vault MCP server not available", stem)
        return {
            "promoted": False,
            "reason": "vault_unavailable",
            "note_path": None,
            "learning_id": learning["frontmatter"].get("learning_id"),
            "errors": ["vault MCP server not importable"],
        }

    try:
        validation = vault_validate(
            frontmatter=learning["frontmatter"],
            body=learning["body"],
        )
    except Exception as exc:
        logger.warning("PROMOTION VALIDATION FAILED: %s — %s", stem, exc)
        return {
            "promoted": False,
            "reason": f"validation_error:{exc}",
            "note_path": None,
            "learning_id": learning["frontmatter"].get("learning_id"),
            "errors": [str(exc)],
        }

    if not validation.get("valid", False):
        errors = validation.get("errors", ["unknown validation error"])
        logger.warning("PROMOTION VALIDATION REJECTED: %s — %s", stem, errors)
        return {
            "promoted": False,
            "reason": "validation_failed",
            "note_path": learning["path"],
            "learning_id": learning["frontmatter"].get("learning_id"),
            "errors": errors,
        }

    # 4. Write via bounded write path
    try:
        write_result = vault_write(
            path=learning["path"],
            frontmatter=learning["frontmatter"],
            body=learning["body"],
        )
    except Exception as exc:
        logger.warning("PROMOTION WRITE FAILED: %s — %s", stem, exc)
        return {
            "promoted": False,
            "reason": f"write_error:{exc}",
            "note_path": learning["path"],
            "learning_id": learning["frontmatter"].get("learning_id"),
            "errors": [str(exc)],
        }

    if "error" in write_result:
        logger.warning(
            "PROMOTION WRITE REJECTED: %s — %s", stem, write_result["error"]
        )
        return {
            "promoted": False,
            "reason": f"write_rejected:{write_result['error']}",
            "note_path": learning["path"],
            "learning_id": learning["frontmatter"].get("learning_id"),
            "errors": [write_result["error"]],
        }

    logger.info(
        "PROMOTION SUCCESS: %s → %s (%d bytes)",
        stem, learning["path"], write_result.get("size", 0),
    )
    return {
        "promoted": True,
        "reason": "eligible",
        "note_path": learning["path"],
        "learning_id": learning["frontmatter"].get("learning_id"),
        "errors": [],
    }
