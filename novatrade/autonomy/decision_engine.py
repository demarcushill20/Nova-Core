"""Adaptive Decision Engine — maps system state to action modes."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, Field

from novatrade.autonomy.decision_context import DecisionContext
from novatrade.autonomy.schemas import DimensionScore

log = logging.getLogger("novatrade.autonomy.decision_engine")


class ActionMode(str, Enum):
    """The 6 possible action modes from the blueprint."""

    RESEARCH = "research"
    PLAN = "plan"
    EXECUTE = "execute"
    MONITOR = "monitor"
    VALIDATE = "validate"
    REPAIR = "repair"


class DecisionConfig(BaseModel):
    """Tunable thresholds for decision logic."""

    research_threshold: float = 40.0  # score below this + low confidence → research
    plan_threshold: float = 60.0  # score below this + no plan → plan
    execute_threshold: float = 60.0  # score below this + has plan → execute
    regression_delta: float = 10.0  # 10-point drop = regression
    target_score: float = 70.0  # GREEN threshold
    cooldown_minutes: int = 30  # min time between same-mode decisions
    max_research_per_day: int = 3
    max_plan_per_day: int = 2


class Decision(BaseModel):
    """A single decision made by the engine."""

    mode: ActionMode
    reason: str  # "Why am I doing this?"
    target_dimension: str | None = None
    suggested_actions: list[str] = Field(default_factory=list)
    confidence: str = "medium"  # high / medium / low
    decided_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class DecisionEngine:
    """Maps DecisionContext → Decision using rule-based classification.

    Decision logic (from blueprint):
    1. IF knowledge gap AND dimension confidence LOW → RESEARCH
    2. ELIF dimension score < threshold AND no plan exists → PLAN
    3. ELIF dimension score < threshold AND plan exists → EXECUTE
    4. ELIF recent regression detected → REPAIR
    5. ELIF all dimensions > target AND recent change → VALIDATE
    6. ELSE → MONITOR
    """

    def __init__(
        self,
        config: DecisionConfig | None = None,
        base_path: str = "/home/nova/nova-core",
    ) -> None:
        self.config = config or DecisionConfig()
        self.base_path = Path(base_path)
        self._history_path = self.base_path / "STATE" / "decision_history.json"
        self._write_lock = asyncio.Lock()

    async def decide(self, context: DecisionContext) -> Decision:
        """Run the adaptive decision loop."""
        report = context.progress_report

        # Identify weakest dimension
        weakest_name, weakest_dim = self._find_weakest_dimension(report)

        # Check for knowledge gaps (low confidence / stale data)
        knowledge_gaps = self._detect_knowledge_gaps(report)

        # Check for regression
        regression = self._detect_regression(report, context.recent_decisions)

        # Rule-based classification
        decision = self._classify(
            weakest_name=weakest_name,
            weakest_dim=weakest_dim,
            knowledge_gaps=knowledge_gaps,
            regression=regression,
            report=report,
            context=context,
        )

        # Rate-limit check: override to MONITOR if cooldown or daily limit hit
        actionable = (ActionMode.RESEARCH, ActionMode.PLAN, ActionMode.EXECUTE, ActionMode.REPAIR)
        if decision.mode in actionable and self._check_cooldown(decision.mode, context):
            decision = Decision(
                mode=ActionMode.MONITOR,
                reason=f"Rate-limited: {decision.mode.value} mode is in cooldown or daily limit reached.",
                confidence="high",
            )

        # Persist decision
        await self._persist_decision(decision)

        return decision

    def _find_weakest_dimension(self, report) -> tuple[str | None, DimensionScore | None]:
        """Find the dimension with the lowest score."""
        if not report.dimensions:
            return None, None
        weakest_name = min(report.dimensions, key=lambda k: report.dimensions[k].score)
        return weakest_name, report.dimensions[weakest_name]

    def _detect_knowledge_gaps(self, report) -> list[str]:
        """Identify dimensions where data is stale or confidence is low."""
        gaps: list[str] = []
        for name, dim in report.dimensions.items():
            # Many warnings = low confidence
            if len(dim.warnings) >= 2:
                gaps.append(name)
            # Very low score with many sub-metrics at 0 = possibly no data
            zero_metrics = sum(1 for sm in dim.sub_metrics if sm.value == 0.0)
            if zero_metrics > 0 and zero_metrics >= len(dim.sub_metrics) // 2 and dim.sub_metrics:
                gaps.append(name)
        return list(dict.fromkeys(gaps))

    def _detect_regression(self, report, recent_decisions: list[dict]) -> str | None:
        """Check if any dimension dropped significantly from recent history."""
        for name, trend in report.trends.items():
            if trend.direction == "degrading":
                dim = report.dimensions.get(name)
                if dim and trend.avg_6h is not None:
                    delta = trend.avg_6h - dim.score
                    if delta >= self.config.regression_delta:
                        return name
        return None

    def _classify(
        self,
        weakest_name: str | None,
        weakest_dim: DimensionScore | None,
        knowledge_gaps: list[str],
        regression: str | None,
        report,
        context: DecisionContext,
    ) -> Decision:
        """Apply the 6-mode decision rules."""

        # Rule 1: Knowledge gap + low-scoring dimension → RESEARCH
        if knowledge_gaps and weakest_dim and weakest_dim.score < self.config.research_threshold:
            target = knowledge_gaps[0]
            return Decision(
                mode=ActionMode.RESEARCH,
                reason=f"Knowledge gap in {target} — confidence is low, need more data before acting.",
                target_dimension=target,
                suggested_actions=[
                    f"Research current state of {target}",
                    f"Gather metrics and logs for {target}",
                ],
                confidence="low",
            )

        # Rule 4: Regression detected → REPAIR (checked before PLAN/EXECUTE for urgency)
        if regression:
            dim = report.dimensions.get(regression)
            score_str = f"{dim.score:.0f}" if dim else "?"
            return Decision(
                mode=ActionMode.REPAIR,
                reason=f"Regression detected in {regression} — score dropped to {score_str}, was higher in 6h average.",
                target_dimension=regression,
                suggested_actions=[
                    f"Diagnose root cause of {regression} regression",
                    f"Check recent changes affecting {regression}",
                    f"Fix and verify {regression} recovery",
                ],
                confidence="high",
            )

        # Rule 2: Low score + no existing plan → PLAN
        if weakest_dim and weakest_dim.score < self.config.plan_threshold:
            has_plan = self._has_existing_plan(weakest_name, context)
            if not has_plan:
                return Decision(
                    mode=ActionMode.PLAN,
                    reason=(
                        f"{weakest_name} score is {weakest_dim.score:.0f}/100 "
                        "with no active plan — need to create improvement plan."
                    ),
                    target_dimension=weakest_name,
                    suggested_actions=[
                        f"Analyze blockers for {weakest_name}",
                        f"Create improvement plan for {weakest_name}",
                    ],
                    confidence="medium",
                )

            # Rule 3: Low score + has plan → EXECUTE
            return Decision(
                mode=ActionMode.EXECUTE,
                reason=(
                    f"{weakest_name} score is {weakest_dim.score:.0f}/100 "
                    "— existing plan found, proceed with execution."
                ),
                target_dimension=weakest_name,
                suggested_actions=[
                    f"Execute next step of {weakest_name} improvement plan",
                    "Verify progress after execution",
                ],
                confidence="medium",
            )

        # Rule 5: All dimensions above target + recent changes → VALIDATE
        all_above_target = all(d.score >= self.config.target_score for d in report.dimensions.values())
        has_recent_changes = any(t.direction == "improving" for t in report.trends.values())
        if all_above_target and has_recent_changes:
            return Decision(
                mode=ActionMode.VALIDATE,
                reason="All dimensions above target — validating recent improvements are stable.",
                target_dimension=None,
                suggested_actions=[
                    "Verify recent changes haven't introduced regressions",
                    "Run end-to-end validation checks",
                ],
                confidence="high",
            )

        # Rule 6: Default → MONITOR
        return Decision(
            mode=ActionMode.MONITOR,
            reason="System is operating within acceptable parameters — monitoring.",
            target_dimension=None,
            suggested_actions=[],
            confidence="high",
        )

    def _has_existing_plan(self, dimension: str | None, context: DecisionContext) -> bool:
        """Check if there's an active plan targeting this dimension."""
        if not dimension:
            return False
        for dec in context.recent_decisions:
            if dec.get("mode") == "plan" and dec.get("target_dimension") == dimension:
                return True
        # Also check for plan-type tasks in pending tasks
        for task in context.pending_tasks:
            if "plan" in task.title.lower() and dimension.replace("_", " ") in task.title.lower():
                return True
        return False

    def _check_cooldown(self, mode: ActionMode, context: DecisionContext) -> bool:
        """Return True if this mode is currently rate-limited."""
        now = datetime.now(timezone.utc)

        # Cooldown check: same mode too recently
        for dec in reversed(context.recent_decisions):
            dec_mode = dec.get("mode")
            dec_at = dec.get("decided_at", "")
            if dec_mode == mode.value and dec_at:
                try:
                    dt = datetime.fromisoformat(dec_at.replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    if (now - dt) < timedelta(minutes=self.config.cooldown_minutes):
                        return True
                except (ValueError, TypeError):
                    pass
                break  # only check most recent of same mode

        # Daily rate limit
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        limit_map = {
            ActionMode.RESEARCH: self.config.max_research_per_day,
            ActionMode.PLAN: self.config.max_plan_per_day,
        }
        limit = limit_map.get(mode)
        if limit is not None:
            mode_count = 0
            for dec in context.recent_decisions:
                if dec.get("mode") == mode.value:
                    dec_at = dec.get("decided_at", "")
                    if dec_at:
                        try:
                            dt = datetime.fromisoformat(dec_at.replace("Z", "+00:00"))
                            if dt.tzinfo is None:
                                dt = dt.replace(tzinfo=timezone.utc)
                            if dt >= day_start:
                                mode_count += 1
                        except (ValueError, TypeError):
                            pass
            if mode_count >= limit:
                return True

        return False

    async def _persist_decision(self, decision: Decision) -> None:
        """Append decision to STATE/decision_history.json."""
        async with self._write_lock:
            self._history_path.parent.mkdir(parents=True, exist_ok=True)
            history: list[dict] = []
            if self._history_path.exists():
                try:
                    data = json.loads(self._history_path.read_text())
                    history = data if isinstance(data, list) else []
                except (json.JSONDecodeError, OSError):
                    pass

            history.append(json.loads(decision.model_dump_json()))

            # Keep last 100 decisions
            history = history[-100:]

            # Atomic write
            fd, tmp_path = tempfile.mkstemp(dir=str(self._history_path.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(history, f, indent=2, default=str)
                os.replace(tmp_path, str(self._history_path))
            except BaseException:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
