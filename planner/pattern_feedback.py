"""Phase 7.5 — Planner feedback tracing for agent-pattern retrieval.

Records whether retrieved Obsidian agent-pattern guidance influenced
planning and whether post-execution evidence suggests it was useful.

Design constraints:
  - Traces remain in runtime/audit territory, never in Obsidian.
  - Tracing failure never blocks planning or execution (fail-open).
  - Usefulness signals are conservative: prefer "unknown" over weak inference.
  - No write-back to agent-pattern notes.
  - Bounded, structured, auditable.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

LOGS_DIR = Path("/home/nova/nova-core/LOGS")

# ---------------------------------------------------------------------------
# Usefulness taxonomy
# ---------------------------------------------------------------------------

# Bounded set of usefulness values
USEFULNESS_VALUES = frozenset(
    {
        "useful",  # patterns injected + execution succeeded well (A/B)
        "neutral",  # patterns injected + execution ok but unclear benefit
        "unused",  # patterns found but not injected or not extractable
        "unknown",  # cannot determine (e.g., execution failed)
        "not_applicable",  # no pattern retrieval attempted
    }
)

# Grades that indicate strong positive outcome
_STRONG_GRADES = frozenset({"A", "A+", "B", "B+"})

# Grades that indicate acceptable but unclear benefit
_NEUTRAL_GRADES = frozenset({"C", "C+"})


# ---------------------------------------------------------------------------
# Trace initialization (post-planning, pre-execution)
# ---------------------------------------------------------------------------


def init_pattern_trace(
    stem: str,
    task_class: str,
    plan: Any,
) -> dict[str, Any]:
    """Initialize a pattern feedback trace from plan state.

    Inspects the first step's inputs to determine whether pattern
    guidance was injected during planning.

    Returns a mutable trace dict to be finalized after execution.
    """
    trace: dict[str, Any] = {
        "task_id": stem,
        "task_class": task_class,
        "timestamp_init": datetime.now(timezone.utc).isoformat(),
        "pattern_retrieval_activated": False,
        "selected_pattern_paths": [],
        "selected_pattern_count": 0,
        "guidance_injected": False,
        "guidance_size_bytes": 0,
        "execution_status": None,
        "execution_grade": None,
        "usefulness": "not_applicable",
        "rationale": "",
        "trace_finalized": False,
        "errors": [],
    }

    try:
        if not plan.steps:
            trace["rationale"] = "no_plan_steps"
            return trace

        first_inputs = plan.steps[0].inputs
        pattern_guidance = first_inputs.get("pattern_guidance", "")
        pattern_paths = first_inputs.get("pattern_paths", [])

        if pattern_guidance:
            trace["pattern_retrieval_activated"] = True
            trace["guidance_injected"] = True
            trace["guidance_size_bytes"] = len(pattern_guidance)
            trace["selected_pattern_paths"] = list(pattern_paths)
            trace["selected_pattern_count"] = len(pattern_paths)
            trace["usefulness"] = "unknown"  # will be resolved at finalization
            trace["rationale"] = "guidance_injected_awaiting_outcome"
        elif pattern_paths:
            # Paths found but guidance was empty (no extractable sections)
            trace["pattern_retrieval_activated"] = True
            trace["selected_pattern_paths"] = list(pattern_paths)
            trace["selected_pattern_count"] = len(pattern_paths)
            trace["usefulness"] = "unused"
            trace["rationale"] = "patterns_found_no_extractable_guidance"
        else:
            trace["rationale"] = "no_patterns_retrieved"

    except Exception as exc:
        trace["errors"].append(f"init_error:{exc}")
        trace["rationale"] = f"init_failed:{exc}"

    return trace


# ---------------------------------------------------------------------------
# Trace finalization (post-execution)
# ---------------------------------------------------------------------------


def finalize_pattern_trace(
    trace: dict[str, Any],
    plan_summary: dict[str, Any],
) -> dict[str, Any]:
    """Finalize a trace with execution outcome and usefulness signal.

    Uses bounded, conservative evidence rules to assign usefulness.
    Prefers "unknown" over weak inference.

    Returns the mutated trace dict.
    """
    try:
        status = plan_summary.get("status", "")
        evaluation = plan_summary.get("evaluation", {})
        grade = evaluation.get("grade", "")

        trace["execution_status"] = status
        trace["execution_grade"] = grade
        trace["timestamp_final"] = datetime.now(timezone.utc).isoformat()
        trace["trace_finalized"] = True

        # Only compute usefulness if guidance was actually injected
        if not trace.get("guidance_injected", False):
            # Usefulness was already set during init (unused or not_applicable)
            return trace

        trace["usefulness"] = _compute_usefulness(
            guidance_injected=True,
            status=status,
            grade=grade,
            verifier_rejected=plan_summary.get("verifier_rejected", False),
        )
        trace["rationale"] = _build_rationale(
            trace["usefulness"],
            status,
            grade,
        )

    except Exception as exc:
        trace["errors"].append(f"finalize_error:{exc}")
        if not trace.get("trace_finalized"):
            trace["trace_finalized"] = True
            trace["usefulness"] = "unknown"
            trace["rationale"] = f"finalize_failed:{exc}"

    return trace


def _compute_usefulness(
    guidance_injected: bool,
    status: str,
    grade: str,
    verifier_rejected: bool,
) -> str:
    """Compute bounded usefulness signal using explicit evidence rules.

    Evidence rules (conservative, prefer unknown over weak inference):
    1. No guidance injected → unused or not_applicable (handled by caller)
    2. Guidance injected + done + grade A/B → useful
    3. Guidance injected + done + grade C → neutral
    4. Guidance injected + done + no grade → neutral
    5. Guidance injected + failed → unknown
    6. Guidance injected + rejected → unknown
    7. Guidance injected + verifier_rejected → unknown
    8. All other cases → unknown
    """
    if not guidance_injected:
        return "unused"

    if verifier_rejected:
        return "unknown"

    if status == "done":
        if grade in _STRONG_GRADES:
            return "useful"
        elif grade in _NEUTRAL_GRADES:
            return "neutral"
        else:
            # Done but no/unknown grade — can't determine
            return "neutral"

    if status in ("failed", "rejected", "partial"):
        return "unknown"

    return "unknown"


def _build_rationale(usefulness: str, status: str, grade: str) -> str:
    """Build a human-readable rationale string."""
    parts = [f"usefulness={usefulness}"]
    if status:
        parts.append(f"status={status}")
    if grade:
        parts.append(f"grade={grade}")
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# Trace logging (runtime audit, not Obsidian)
# ---------------------------------------------------------------------------


def log_pattern_trace(trace: dict[str, Any]) -> bool:
    """Append a finalized trace to the structured feedback log.

    Writes to LOGS/pattern_feedback.jsonl (one JSON object per line).
    Returns True on success, False on failure (fail-open).
    """
    log_path = LOGS_DIR / "pattern_feedback.jsonl"

    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)

        # Compact JSON, one trace per line
        line = json.dumps(trace, default=str, separators=(",", ":"))
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

        logger.debug(
            "PATTERN FEEDBACK LOGGED: task=%s usefulness=%s",
            trace.get("task_id", "?"),
            trace.get("usefulness", "?"),
        )
        return True

    except Exception as exc:
        logger.warning("PATTERN FEEDBACK LOG FAILED: %s", exc)
        return False
