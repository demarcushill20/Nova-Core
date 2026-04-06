"""Execution lanes for task dispatch.

Tasks range from trivial monitoring to deep architecture work. This module
defines three lanes (Light, Medium, Heavy) that control model selection,
tool profiles, skill mode, memory access, timeout, and context budget.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from utils.cost_router import TaskComplexity

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lane enum & config
# ---------------------------------------------------------------------------


class Lane(enum.Enum):
    LIGHT = "light"
    MEDIUM = "medium"
    HEAVY = "heavy"


@dataclass
class LaneConfig:
    lane: Lane
    model: str  # preferred model (can be overridden by budget)
    tool_profile: str  # key into tool_profiles.PROFILES
    skill_mode: str  # "compact" or "full"
    memory_enabled: bool  # whether to do memory retrieval
    timeout_seconds: int  # subprocess timeout
    context_budget_tokens: int  # approximate target context size


# ---------------------------------------------------------------------------
# Lane configurations
# ---------------------------------------------------------------------------

LANE_CONFIGS = {
    Lane.LIGHT: LaneConfig(
        lane=Lane.LIGHT,
        model="claude-sonnet-4-20250514",
        tool_profile="simple",  # Core tools only
        skill_mode="compact",
        memory_enabled=False,
        timeout_seconds=300,  # 5 min
        context_budget_tokens=3000,
    ),
    Lane.MEDIUM: LaneConfig(
        lane=Lane.MEDIUM,
        model="claude-sonnet-4-20250514",  # cost-routed, this is default
        tool_profile="",  # set from task_class
        skill_mode="compact",
        memory_enabled=True,
        timeout_seconds=3600,  # 1 hour
        context_budget_tokens=12000,
    ),
    Lane.HEAVY: LaneConfig(
        lane=Lane.HEAVY,
        model="claude-opus-4-6",
        tool_profile="fallback",  # broad MCP set
        skill_mode="full",
        memory_enabled=True,
        timeout_seconds=14400,  # 4 hours
        context_budget_tokens=25000,
    ),
}


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def classify_lane(
    task_class: str,
    complexity: TaskComplexity,
    work_mode: str = "",
) -> Lane:
    """Decide which execution lane a task belongs to.

    Args:
        task_class: Semantic category of the task (e.g. "simple",
            "monitoring", "research", "planning", "system", ...).
        complexity: A ``TaskComplexity`` enum value from ``utils.cost_router``.
        work_mode: Optional runtime work mode hint (e.g. "monitoring").

    Returns:
        The appropriate ``Lane``.
    """
    # Lazy import to avoid circular dependency (cost_router may import us).
    from utils.cost_router import TaskComplexity

    # --- Light lane ---
    if task_class in ("simple", "monitoring"):
        log.debug(
            "Lane LIGHT: task_class=%s matches light-lane class list",
            task_class,
        )
        return Lane.LIGHT

    if work_mode == "monitoring":
        log.debug("Lane LIGHT: work_mode='monitoring'")
        return Lane.LIGHT

    if task_class == "system" and complexity <= TaskComplexity.SIMPLE:
        log.debug(
            "Lane LIGHT: task_class='system' with complexity=%s",
            complexity.name,
        )
        return Lane.LIGHT

    # --- Heavy lane ---
    if complexity == TaskComplexity.COMPLEX:
        log.debug("Lane HEAVY: complexity=COMPLEX")
        return Lane.HEAVY

    if task_class == "research" and complexity >= TaskComplexity.MODERATE:
        log.debug(
            "Lane HEAVY: task_class='research' with complexity=%s",
            complexity.name,
        )
        return Lane.HEAVY

    if task_class == "planning":
        log.debug("Lane HEAVY: task_class='planning'")
        return Lane.HEAVY

    # --- Medium lane (default) ---
    log.debug(
        "Lane MEDIUM (default): task_class=%s, complexity=%s, work_mode=%s",
        task_class,
        complexity.name,
        work_mode,
    )
    return Lane.MEDIUM


# ---------------------------------------------------------------------------
# Config retrieval
# ---------------------------------------------------------------------------


def get_lane_config(lane: Lane, task_class: str = "") -> LaneConfig:
    """Return a copy of the lane config, optionally refined by *task_class*.

    For the MEDIUM lane the ``tool_profile`` is set to *task_class* so it
    maps to the correct ``tool_profiles.PROFILES`` key.
    """
    cfg = replace(LANE_CONFIGS[lane])

    if lane is Lane.MEDIUM and task_class:
        cfg.tool_profile = task_class

    return cfg


# ---------------------------------------------------------------------------
# Budget-aware lane downgrade
# ---------------------------------------------------------------------------


def apply_weekly_budget_override(lane: Lane, budget_pct: float) -> Lane:
    """Optionally downgrade a lane when the weekly budget is running hot.

    Args:
        lane: The originally classified lane.
        budget_pct: Percentage of weekly budget consumed (0-100).

    Returns:
        The (possibly downgraded) lane.
    """
    # Thresholds raised to preserve autonomous research/planning quality.
    # Original: 90→LIGHT, 60→MEDIUM. New: 98→LIGHT, 95→MEDIUM.
    # Autonomous research→plan→implement cycles require Opus + full context.
    if budget_pct >= 98:
        if lane is not Lane.LIGHT:
            log.info(
                "Budget override: %.1f%% consumed — downgrading %s → LIGHT",
                budget_pct,
                lane.value,
            )
        return Lane.LIGHT

    if budget_pct >= 95 and lane is Lane.HEAVY:
        log.info(
            "Budget override: %.1f%% consumed — downgrading HEAVY → MEDIUM",
            budget_pct,
        )
        return Lane.MEDIUM

    return lane
