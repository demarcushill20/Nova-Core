"""Adaptive Decision Engine — maps system state to action modes."""

from __future__ import annotations

import fcntl
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
        self._lock_path = self._history_path.parent / ".decision_history.lock"

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
        """Apply the decision rules with NovaTrade-specific detection."""

        # --- Priority Rule 0: NovaTrade not trading (critical) ----------------
        # If strategy_validity shows zero trades during market hours, escalate
        # immediately regardless of other dimension scores.
        novatrade_critical = self._detect_novatrade_not_trading(report)
        if novatrade_critical:
            return novatrade_critical

        # --- Priority Rule 0b: Execution pipeline broken ----------------------
        pipeline_critical = self._detect_pipeline_broken(report)
        if pipeline_critical:
            return pipeline_critical

        # Rule 1: Knowledge gap + low-scoring dimension → RESEARCH
        if knowledge_gaps and weakest_dim and weakest_dim.score < self.config.research_threshold:
            target = knowledge_gaps[0]
            return Decision(
                mode=ActionMode.RESEARCH,
                reason=f"Knowledge gap in {target} — confidence is low, need more data before acting.",
                target_dimension=target,
                suggested_actions=self._specific_research_actions(target, report),
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
                suggested_actions=self._specific_repair_actions(regression, report),
                confidence="high",
            )

        # Rule 2: Low score + no existing plan → PLAN
        if weakest_name and weakest_dim and weakest_dim.score < self.config.plan_threshold:
            has_plan = self._has_existing_plan(weakest_name, context)
            if not has_plan:
                return Decision(
                    mode=ActionMode.PLAN,
                    reason=(
                        f"{weakest_name} score is {weakest_dim.score:.0f}/100 "
                        "with no active plan — need to create improvement plan."
                    ),
                    target_dimension=weakest_name,
                    suggested_actions=self._specific_plan_actions(weakest_name, report),
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
                suggested_actions=self._specific_execute_actions(weakest_name, report),
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

    # -------------------------------------------------------------------
    # NovaTrade-specific detectors (Priority Rule 0)
    # -------------------------------------------------------------------

    def _detect_novatrade_not_trading(self, report) -> Decision | None:
        """Detect if NovaTrade is running but not placing trades.

        This is the #1 failure the operator cares about — the system should
        never silently fail to trade during market hours.
        """
        dim = report.dimensions.get("strategy_validity")
        if dim is None:
            return None

        # Find the trades_last_24h sub-metric
        trades_metric = None
        silent_failure = None
        for sm in dim.sub_metrics:
            if sm.name == "trades_last_24h":
                trades_metric = sm
            elif sm.name == "silent_failure_detected":
                silent_failure = sm

        # Zero trades during market hours = critical
        # SubMetric.value is the normalized score (0-100); score 20 = zero trades during market hours
        if trades_metric and trades_metric.value <= 20:
            return Decision(
                mode=ActionMode.REPAIR,
                reason=(
                    f"CRITICAL: NovaTrade has ZERO trades in the last 24h "
                    f"(trade score={trades_metric.value:.0f}/100). "
                    "This means the strategy is not executing — immediate diagnosis required."
                ),
                target_dimension="strategy_validity",
                suggested_actions=[
                    "Check novacore-novatrade.service status with systemctl",
                    "Check STATE/novatrade/trade_log.json for recent entries",
                    "Check STATE/novatrade/halt_state.json for active risk halt",
                    "Check MetaApi connection status and recent errors",
                    "Check LOGS/ for order rejection or broker connection errors",
                    "Restart novacore-novatrade.service if service is down",
                    "Send Telegram alert to operator about zero-trade condition",
                ],
                confidence="high",
            )

        # Silent failure (no signals for 4+ hours during market hours)
        # SubMetric.value is the score (0-100): 0 = silence detected, 100 = signals flowing
        if silent_failure and silent_failure.value < 10:
            return Decision(
                mode=ActionMode.REPAIR,
                reason=(
                    "CRITICAL: NovaTrade silent failure — no signal output for 4+ hours "
                    "during market hours. Strategy pipeline may be stuck or crashed."
                ),
                target_dimension="strategy_validity",
                suggested_actions=[
                    "Check novacore-novatrade.service logs with journalctl -u novacore-novatrade -n 100",
                    "Check if strategy process is running but hung",
                    "Restart novacore-novatrade.service",
                    "Verify signal output resumes after restart",
                    "Send Telegram alert about silent failure condition",
                ],
                confidence="high",
            )

        return None

    def _detect_pipeline_broken(self, report) -> Decision | None:
        """Detect if the execution pipeline is critically broken."""
        dim = report.dimensions.get("execution_pipeline")
        if dim is None:
            return None

        # Check for critical broker connectivity failure
        for sm in dim.sub_metrics:
            if sm.name == "pipeline_connectivity" and sm.value < 20:
                return Decision(
                    mode=ActionMode.REPAIR,
                    reason=(
                        f"CRITICAL: Broker connectivity is at {sm.value:.0f}/100 — "
                        "most API calls are failing. Orders cannot be placed."
                    ),
                    target_dimension="execution_pipeline",
                    suggested_actions=[
                        "Check MetaApi SDK connection status",
                        "Verify API credentials are valid",
                        "Check network connectivity to MetaApi servers",
                        "Restart novacore-novatrade.service to force reconnect",
                        "Review STATE/metrics.json for error patterns",
                        "Send Telegram alert about broker connectivity failure",
                    ],
                    confidence="high",
                )

            if sm.name == "rejection_rate" and sm.value < 20:
                return Decision(
                    mode=ActionMode.REPAIR,
                    reason=(
                        f"CRITICAL: Order rejection rate is extremely high "
                        f"(score={sm.value:.0f}/100). Broker is rejecting most orders."
                    ),
                    target_dimension="execution_pipeline",
                    suggested_actions=[
                        "Check LOGS/ for specific rejection reasons from broker",
                        "Verify lot sizes comply with broker minimums",
                        "Check if FTMO daily loss limit has been hit",
                        "Review risk gate pass/fail logs",
                        "Send Telegram alert about order rejection spike",
                    ],
                    confidence="high",
                )

        return None

    # -------------------------------------------------------------------
    # Specific action generators (replace generic suggestions)
    # -------------------------------------------------------------------

    def _specific_research_actions(self, dimension: str, report) -> list[str]:
        """Generate specific research actions based on the failing dimension."""
        actions_map = {
            "strategy_validity": [
                "Check STATE/novatrade/trade_log.json for recent trade activity",
                "Review signal_log.json for signal generation patterns",
                "Check if strategy config at STATE/novatrade/strategy_config.json is correct",
                "Review LOGS/ for any strategy errors or exceptions",
            ],
            "execution_pipeline": [
                "Check STATE/metrics.json for contract success/failure rates",
                "Review MetaApi connection logs for errors",
                "Check if price feed is updating in STATE/novatrade/",
                "Verify broker account status and margin availability",
            ],
            "system_health": [
                "Run systemctl status on all novacore services",
                "Check disk space and memory usage",
                "Review error rate in LOGS/ for the last hour",
                "Check for orphaned tasks in TASKS/ older than 2 hours",
            ],
            "risk_engine": [
                "Check STATE/novatrade/halt_state.json for active halts",
                "Review daily_loss_tracker.json for drawdown status",
                "Check risk_policy.yaml for current risk parameters",
                "Review pre-trade gate logs for rejection patterns",
            ],
            "performance_stability": [
                "Load equity history from STATE/novatrade/equity_history.json",
                "Calculate current Sharpe ratio and drawdown",
                "Compare live performance to backtest baselines",
                "Check for unusual equity curve patterns",
            ],
        }
        return actions_map.get(
            dimension,
            [
                f"Research current state of {dimension}",
                f"Gather metrics and logs for {dimension}",
            ],
        )

    def _specific_repair_actions(self, dimension: str, report) -> list[str]:
        """Generate specific repair actions based on the failing dimension."""
        base = [f"Diagnose root cause of {dimension} regression"]
        extras = {
            "strategy_validity": [
                "Check if novacore-novatrade.service is running",
                "Check for risk halt in STATE/novatrade/halt_state.json",
                "Restart service if down: sudo systemctl restart novacore-novatrade",
                "Verify trades resume after fix",
            ],
            "execution_pipeline": [
                "Check MetaApi connection and recent errors",
                "Restart novacore-novatrade.service to force reconnect",
                "Verify order flow resumes after restart",
            ],
            "system_health": [
                "Restart failed services",
                "Clear error state and verify recovery",
            ],
        }
        return base + extras.get(dimension, [f"Fix and verify {dimension} recovery"])

    def _specific_plan_actions(self, dimension: str, report) -> list[str]:
        """Generate specific plan actions based on the failing dimension."""
        return [
            f"Analyze root causes of low {dimension} score",
            f"Identify specific blockers for {dimension} improvement",
            "Create step-by-step improvement plan with measurable targets",
            f"Estimate effort and set timeline for {dimension} recovery",
        ]

    def _specific_execute_actions(self, dimension: str, report) -> list[str]:
        """Generate specific execution actions based on the failing dimension."""
        return [
            f"Review existing plan for {dimension} improvement",
            f"Execute the next pending step of the {dimension} plan",
            f"Verify measurable improvement in {dimension} score after execution",
            "Update plan status and document what was done",
        ]

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
        """Append decision to STATE/decision_history.json.

        Deduplicates consecutive identical decisions to prevent the history
        from filling with redundant entries. Uses fcntl.flock for cross-instance
        mutual exclusion (v87 P3, race-condition fix v88).
        """
        self._history_path.parent.mkdir(parents=True, exist_ok=True)

        # File-based lock — works across all DecisionEngine instances in the
        # same process (and across processes). Replaces the per-instance
        # asyncio.Lock that allowed concurrent writes from different instances.
        with open(self._lock_path, "w") as lock_fd:  # noqa: ASYNC230 — intentional blocking for flock
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX)
            try:
                self._persist_decision_locked(decision)
            finally:
                fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)

    def _persist_decision_locked(self, decision: Decision) -> None:
        """Inner persist logic — caller must hold the file lock."""
        history: list[dict] = []
        if self._history_path.exists():
            try:
                data = json.loads(self._history_path.read_text())
                history = data if isinstance(data, list) else []
            except (json.JSONDecodeError, OSError):
                pass

        new_entry = json.loads(decision.model_dump_json())

        # Dedup: skip if identical mode+reason+target as the last entry
        if history:
            last = history[-1]
            if (
                last.get("mode") == new_entry.get("mode")
                and last.get("reason") == new_entry.get("reason")
                and last.get("target_dimension") == new_entry.get("target_dimension")
            ):
                # Update timestamp on existing entry instead of appending
                history[-1]["decided_at"] = new_entry.get("decided_at", "")
                history[-1]["dedup_count"] = history[-1].get("dedup_count", 1) + 1
                self._atomic_write_history(history)
                return

        history.append(new_entry)

        # Keep last 100 decisions
        history = history[-100:]
        self._atomic_write_history(history)

    def _atomic_write_history(self, history: list[dict]) -> None:
        """Atomic write via tempfile + rename."""
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
