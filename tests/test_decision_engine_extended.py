"""Extended tests for DecisionEngine — NovaTrade-specific detectors, cooldown, and persistence.

Covers the untested paths in decision_engine.py:
- _detect_novatrade_not_trading (Priority Rule 0)
- _detect_pipeline_broken (Priority Rule 0b)
- _check_cooldown (rate-limit/daily-limit)
- _specific_*_actions helpers
- _persist_decision dedup and atomic write
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from novatrade.autonomy.decision_context import DecisionContext, TaskSummary
from novatrade.autonomy.decision_engine import (
    ActionMode,
    Decision,
    DecisionConfig,
    DecisionEngine,
)
from novatrade.autonomy.schemas import (
    DimensionScore,
    ProgressReport,
    ScoreTrend,
    SubMetric,
)

# =====================================================================
# Helpers
# =====================================================================


def _dim(name: str, score: float, sub_metrics: list[SubMetric] | None = None) -> DimensionScore:
    sms = sub_metrics or [SubMetric(name="default", value=score)]
    return DimensionScore(name=name, score=score, sub_metrics=sms, warnings=[])


def _trend(current: float, avg_6h: float | None = None, direction: str = "stable") -> ScoreTrend:
    return ScoreTrend(current=current, avg_6h=avg_6h, direction=direction)


def _report(dims: dict[str, DimensionScore], trends: dict[str, ScoreTrend] | None = None) -> ProgressReport:
    overall = sum(d.score for d in dims.values()) / len(dims) if dims else 0.0
    return ProgressReport(
        overall_score=overall,
        dimensions=dims,
        trends=trends or {n: _trend(d.score) for n, d in dims.items()},
    )


def _ctx(
    report: ProgressReport, recent: list[dict] | None = None, tasks: list[TaskSummary] | None = None
) -> DecisionContext:
    return DecisionContext(
        progress_report=report,
        recent_decisions=recent or [],
        pending_tasks=tasks or [],
    )


# =====================================================================
# NovaTrade not-trading detector (Priority Rule 0)
# =====================================================================


class TestNovaTradeNotTrading:
    def test_zero_trades_triggers_repair(self):
        """Zero trades in 24h should trigger CRITICAL REPAIR."""
        dims = {
            "strategy_validity": _dim(
                "strategy_validity",
                30.0,
                [
                    SubMetric(name="trades_last_24h", value=20),  # score 20 = zero trades
                    SubMetric(name="other", value=80),
                ],
            ),
            "system_health": _dim("system_health", 80.0),
        }
        engine = DecisionEngine()
        result = engine._detect_novatrade_not_trading(_report(dims))
        assert result is not None
        assert result.mode == ActionMode.REPAIR
        assert "ZERO trades" in result.reason
        assert result.target_dimension == "strategy_validity"
        assert len(result.suggested_actions) > 0

    def test_low_trade_score_triggers_repair(self):
        """Trade score <=20 should trigger REPAIR."""
        dims = {
            "strategy_validity": _dim(
                "strategy_validity",
                50.0,
                [
                    SubMetric(name="trades_last_24h", value=15),
                ],
            ),
        }
        engine = DecisionEngine()
        result = engine._detect_novatrade_not_trading(_report(dims))
        assert result is not None
        assert result.mode == ActionMode.REPAIR

    def test_healthy_trades_returns_none(self):
        """Normal trade activity should return None."""
        dims = {
            "strategy_validity": _dim(
                "strategy_validity",
                80.0,
                [
                    SubMetric(name="trades_last_24h", value=80),
                ],
            ),
        }
        engine = DecisionEngine()
        result = engine._detect_novatrade_not_trading(_report(dims))
        assert result is None

    def test_missing_strategy_validity_returns_none(self):
        """No strategy_validity dimension should return None."""
        dims = {"system_health": _dim("system_health", 80.0)}
        engine = DecisionEngine()
        result = engine._detect_novatrade_not_trading(_report(dims))
        assert result is None

    def test_silent_failure_triggers_repair(self):
        """No signals for 4+ hours should trigger REPAIR."""
        dims = {
            "strategy_validity": _dim(
                "strategy_validity",
                50.0,
                [
                    SubMetric(name="trades_last_24h", value=80),  # trades OK
                    SubMetric(name="silent_failure_detected", value=0),  # silent failure!
                ],
            ),
        }
        engine = DecisionEngine()
        result = engine._detect_novatrade_not_trading(_report(dims))
        assert result is not None
        assert result.mode == ActionMode.REPAIR
        assert "silent failure" in result.reason.lower()

    def test_no_silent_failure_returns_none(self):
        """Normal signal flow should not trigger."""
        dims = {
            "strategy_validity": _dim(
                "strategy_validity",
                80.0,
                [
                    SubMetric(name="trades_last_24h", value=80),
                    SubMetric(name="silent_failure_detected", value=100),  # signals flowing
                ],
            ),
        }
        engine = DecisionEngine()
        result = engine._detect_novatrade_not_trading(_report(dims))
        assert result is None

    def test_no_trade_metric_returns_none(self):
        """Missing trades_last_24h sub-metric shouldn't trigger."""
        dims = {
            "strategy_validity": _dim(
                "strategy_validity",
                80.0,
                [
                    SubMetric(name="other_metric", value=80),
                ],
            ),
        }
        engine = DecisionEngine()
        result = engine._detect_novatrade_not_trading(_report(dims))
        assert result is None


# =====================================================================
# Pipeline broken detector (Priority Rule 0b)
# =====================================================================


class TestPipelineBroken:
    def test_connectivity_failure_triggers_repair(self):
        """Broker connectivity < 20 should trigger REPAIR."""
        dims = {
            "execution_pipeline": _dim(
                "execution_pipeline",
                30.0,
                [
                    SubMetric(name="pipeline_connectivity", value=10),
                ],
            ),
        }
        engine = DecisionEngine()
        result = engine._detect_pipeline_broken(_report(dims))
        assert result is not None
        assert result.mode == ActionMode.REPAIR
        assert "connectivity" in result.reason.lower()
        assert result.target_dimension == "execution_pipeline"

    def test_rejection_rate_triggers_repair(self):
        """High order rejection rate should trigger REPAIR."""
        dims = {
            "execution_pipeline": _dim(
                "execution_pipeline",
                30.0,
                [
                    SubMetric(name="rejection_rate", value=10),
                ],
            ),
        }
        engine = DecisionEngine()
        result = engine._detect_pipeline_broken(_report(dims))
        assert result is not None
        assert result.mode == ActionMode.REPAIR
        assert "rejection" in result.reason.lower()

    def test_healthy_pipeline_returns_none(self):
        """Healthy pipeline should return None."""
        dims = {
            "execution_pipeline": _dim(
                "execution_pipeline",
                80.0,
                [
                    SubMetric(name="pipeline_connectivity", value=90),
                    SubMetric(name="rejection_rate", value=95),
                ],
            ),
        }
        engine = DecisionEngine()
        result = engine._detect_pipeline_broken(_report(dims))
        assert result is None

    def test_missing_pipeline_returns_none(self):
        """No execution_pipeline dimension should return None."""
        dims = {"system_health": _dim("system_health", 80.0)}
        engine = DecisionEngine()
        result = engine._detect_pipeline_broken(_report(dims))
        assert result is None

    def test_borderline_connectivity_does_not_trigger(self):
        """Connectivity at exactly 20 should NOT trigger."""
        dims = {
            "execution_pipeline": _dim(
                "execution_pipeline",
                50.0,
                [
                    SubMetric(name="pipeline_connectivity", value=20),
                ],
            ),
        }
        engine = DecisionEngine()
        result = engine._detect_pipeline_broken(_report(dims))
        assert result is None


# =====================================================================
# Priority Rule 0 overrides lower-priority rules
# =====================================================================


class TestPriorityRuleOverride:
    @pytest.mark.asyncio
    async def test_novatrade_critical_overrides_plan(self, tmp_path):
        """Even if weakest dimension wants PLAN, NovaTrade critical should trigger REPAIR."""
        dims = {
            "strategy_validity": _dim(
                "strategy_validity",
                50.0,
                [
                    SubMetric(name="trades_last_24h", value=10),  # zero trades
                ],
            ),
            "system_health": _dim("system_health", 45.0),  # would normally trigger PLAN
        }
        report = _report(dims)
        engine = DecisionEngine(base_path=str(tmp_path))
        (tmp_path / "STATE").mkdir()
        decision = await engine.decide(_ctx(report))
        assert decision.mode == ActionMode.REPAIR
        assert "ZERO trades" in decision.reason

    @pytest.mark.asyncio
    async def test_pipeline_critical_overrides_research(self, tmp_path):
        """Pipeline failure should override RESEARCH mode."""
        dims = {
            "execution_pipeline": _dim(
                "execution_pipeline",
                20.0,
                [
                    SubMetric(name="pipeline_connectivity", value=5),
                ],
            ),
            "system_health": _dim(
                "system_health",
                25.0,
                [
                    SubMetric(name="m1", value=0.0),
                    SubMetric(name="m2", value=0.0),
                ],
            ),
        }
        report = _report(
            dims,
            trends={
                "execution_pipeline": _trend(20.0),
                "system_health": _trend(25.0),
            },
        )
        engine = DecisionEngine(base_path=str(tmp_path))
        (tmp_path / "STATE").mkdir()
        # Force knowledge gaps on system_health
        report.dimensions["system_health"].warnings = ["w1", "w2"]
        decision = await engine.decide(_ctx(report))
        assert decision.mode == ActionMode.REPAIR
        assert "connectivity" in decision.reason.lower()


# =====================================================================
# Cooldown / rate-limit logic
# =====================================================================


class TestCooldown:
    def test_no_recent_decisions_no_cooldown(self):
        engine = DecisionEngine()
        ctx = _ctx(_report({}))
        assert engine._check_cooldown(ActionMode.RESEARCH, ctx) is False

    def test_recent_same_mode_within_cooldown(self):
        """Same mode within cooldown window should be rate-limited."""
        engine = DecisionEngine(config=DecisionConfig(cooldown_minutes=30))
        recent_time = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        ctx = _ctx(
            _report({}),
            recent=[
                {
                    "mode": "research",
                    "decided_at": recent_time,
                }
            ],
        )
        assert engine._check_cooldown(ActionMode.RESEARCH, ctx) is True

    def test_recent_same_mode_after_cooldown(self):
        """Same mode after cooldown has expired should be allowed."""
        engine = DecisionEngine(config=DecisionConfig(cooldown_minutes=30))
        old_time = (datetime.now(timezone.utc) - timedelta(minutes=45)).isoformat()
        ctx = _ctx(
            _report({}),
            recent=[
                {
                    "mode": "research",
                    "decided_at": old_time,
                }
            ],
        )
        assert engine._check_cooldown(ActionMode.RESEARCH, ctx) is False

    def test_different_mode_no_cooldown(self):
        """Different mode should not trigger cooldown."""
        engine = DecisionEngine()
        recent_time = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        ctx = _ctx(
            _report({}),
            recent=[
                {
                    "mode": "plan",
                    "decided_at": recent_time,
                }
            ],
        )
        assert engine._check_cooldown(ActionMode.RESEARCH, ctx) is False

    def test_daily_limit_research(self):
        """Exceeding daily research limit should trigger cooldown."""
        engine = DecisionEngine(config=DecisionConfig(max_research_per_day=2))
        now = datetime.now(timezone.utc)
        recent = [
            {"mode": "research", "decided_at": (now - timedelta(hours=1)).isoformat()},
            {"mode": "research", "decided_at": (now - timedelta(hours=2)).isoformat()},
            {"mode": "monitor", "decided_at": (now - timedelta(hours=3)).isoformat()},
        ]
        ctx = _ctx(_report({}), recent=recent)
        assert engine._check_cooldown(ActionMode.RESEARCH, ctx) is True

    def test_daily_limit_not_exceeded(self):
        """Under daily limit should be allowed."""
        engine = DecisionEngine(config=DecisionConfig(max_research_per_day=3))
        now = datetime.now(timezone.utc)
        recent = [
            {"mode": "research", "decided_at": (now - timedelta(hours=1)).isoformat()},
        ]
        ctx = _ctx(_report({}), recent=recent)
        assert engine._check_cooldown(ActionMode.RESEARCH, ctx) is False

    def test_monitor_mode_no_daily_limit(self):
        """MONITOR mode should have no daily rate limit (only per-mode cooldown applies)."""
        engine = DecisionEngine(config=DecisionConfig(cooldown_minutes=0))  # disable cooldown
        now = datetime.now(timezone.utc)
        # Even with many recent MONITOR decisions, no daily limit cap
        recent = [{"mode": "monitor", "decided_at": (now - timedelta(minutes=i)).isoformat()} for i in range(20)]
        ctx = _ctx(_report({}), recent=recent)
        assert engine._check_cooldown(ActionMode.MONITOR, ctx) is False

    def test_invalid_datetime_in_decision_ignored(self):
        """Malformed decided_at should be gracefully handled."""
        engine = DecisionEngine()
        ctx = _ctx(
            _report({}),
            recent=[
                {
                    "mode": "research",
                    "decided_at": "not-a-date",
                }
            ],
        )
        # Should not raise
        result = engine._check_cooldown(ActionMode.RESEARCH, ctx)
        assert isinstance(result, bool)

    def test_z_suffix_datetime_parsed(self):
        """UTC 'Z' suffix should be handled."""
        engine = DecisionEngine(config=DecisionConfig(cooldown_minutes=30))
        recent_time = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        ctx = _ctx(
            _report({}),
            recent=[
                {
                    "mode": "research",
                    "decided_at": recent_time,
                }
            ],
        )
        assert engine._check_cooldown(ActionMode.RESEARCH, ctx) is True

    @pytest.mark.asyncio
    async def test_cooldown_overrides_to_monitor(self, tmp_path):
        """When cooldown is active, actionable decisions should be overridden to MONITOR."""
        dims = {
            "system_health": _dim("system_health", 50.0),
            "execution_pipeline": _dim("execution_pipeline", 80.0),
        }
        report = _report(dims)
        now = datetime.now(timezone.utc)
        recent = [{"mode": "plan", "decided_at": (now - timedelta(minutes=5)).isoformat(), "target_dimension": "other"}]
        engine = DecisionEngine(config=DecisionConfig(cooldown_minutes=30), base_path=str(tmp_path))
        (tmp_path / "STATE").mkdir()
        decision = await engine.decide(_ctx(report, recent=recent))
        assert decision.mode == ActionMode.MONITOR
        assert "rate-limited" in decision.reason.lower()


# =====================================================================
# Specific action generators
# =====================================================================


class TestSpecificActions:
    def test_research_actions_strategy_validity(self):
        engine = DecisionEngine()
        actions = engine._specific_research_actions("strategy_validity", None)
        assert len(actions) > 0
        assert any("trade" in a.lower() for a in actions)

    def test_research_actions_unknown_dimension(self):
        engine = DecisionEngine()
        actions = engine._specific_research_actions("unknown_dim", None)
        assert len(actions) == 2  # generic fallback

    def test_repair_actions_strategy_validity(self):
        engine = DecisionEngine()
        actions = engine._specific_repair_actions("strategy_validity", None)
        assert any("restart" in a.lower() for a in actions)

    def test_repair_actions_unknown_dimension(self):
        engine = DecisionEngine()
        actions = engine._specific_repair_actions("unknown_dim", None)
        assert len(actions) >= 2  # base + fallback

    def test_plan_actions(self):
        engine = DecisionEngine()
        actions = engine._specific_plan_actions("system_health", None)
        assert len(actions) == 4
        assert any("plan" in a.lower() for a in actions)

    def test_execute_actions(self):
        engine = DecisionEngine()
        actions = engine._specific_execute_actions("risk_engine", None)
        assert len(actions) == 4
        assert any("execute" in a.lower() or "plan" in a.lower() for a in actions)

    def test_research_actions_all_known_dimensions(self):
        """All 5 known dimensions should have specific research actions."""
        engine = DecisionEngine()
        for dim in ["strategy_validity", "execution_pipeline", "system_health", "risk_engine", "performance_stability"]:
            actions = engine._specific_research_actions(dim, None)
            assert len(actions) > 2, f"Expected specific actions for {dim}"


# =====================================================================
# Persistence — dedup and atomic write
# =====================================================================


class TestPersistence:
    @pytest.mark.asyncio
    async def test_persist_creates_file(self, tmp_path):
        (tmp_path / "STATE").mkdir()
        engine = DecisionEngine(base_path=str(tmp_path))
        decision = Decision(mode=ActionMode.MONITOR, reason="test")
        await engine._persist_decision(decision)
        path = tmp_path / "STATE" / "decision_history.json"
        assert path.exists()
        data = json.loads(path.read_text())
        assert len(data) == 1

    @pytest.mark.asyncio
    async def test_persist_dedup_same_decision(self, tmp_path):
        (tmp_path / "STATE").mkdir()
        engine = DecisionEngine(base_path=str(tmp_path))
        d1 = Decision(mode=ActionMode.MONITOR, reason="same")
        d2 = Decision(mode=ActionMode.MONITOR, reason="same")
        await engine._persist_decision(d1)
        await engine._persist_decision(d2)
        data = json.loads((tmp_path / "STATE" / "decision_history.json").read_text())
        assert len(data) == 1
        assert data[0].get("dedup_count", 1) == 2

    @pytest.mark.asyncio
    async def test_persist_different_decisions_appended(self, tmp_path):
        (tmp_path / "STATE").mkdir()
        engine = DecisionEngine(base_path=str(tmp_path))
        d1 = Decision(mode=ActionMode.MONITOR, reason="reason1")
        d2 = Decision(mode=ActionMode.REPAIR, reason="reason2")
        await engine._persist_decision(d1)
        await engine._persist_decision(d2)
        data = json.loads((tmp_path / "STATE" / "decision_history.json").read_text())
        assert len(data) == 2

    @pytest.mark.asyncio
    async def test_persist_caps_at_100(self, tmp_path):
        (tmp_path / "STATE").mkdir()
        existing = [{"mode": "monitor", "reason": f"r{i}"} for i in range(99)]
        (tmp_path / "STATE" / "decision_history.json").write_text(json.dumps(existing))
        engine = DecisionEngine(base_path=str(tmp_path))
        d = Decision(mode=ActionMode.REPAIR, reason="new")
        await engine._persist_decision(d)
        data = json.loads((tmp_path / "STATE" / "decision_history.json").read_text())
        assert len(data) == 100

    @pytest.mark.asyncio
    async def test_persist_handles_corrupt_file(self, tmp_path):
        (tmp_path / "STATE").mkdir()
        (tmp_path / "STATE" / "decision_history.json").write_text("not json")
        engine = DecisionEngine(base_path=str(tmp_path))
        d = Decision(mode=ActionMode.MONITOR, reason="recover")
        await engine._persist_decision(d)
        data = json.loads((tmp_path / "STATE" / "decision_history.json").read_text())
        assert len(data) == 1

    @pytest.mark.asyncio
    async def test_persist_handles_non_list_json(self, tmp_path):
        (tmp_path / "STATE").mkdir()
        (tmp_path / "STATE" / "decision_history.json").write_text('{"not": "a list"}')
        engine = DecisionEngine(base_path=str(tmp_path))
        d = Decision(mode=ActionMode.MONITOR, reason="recover")
        await engine._persist_decision(d)
        data = json.loads((tmp_path / "STATE" / "decision_history.json").read_text())
        assert len(data) == 1


# =====================================================================
# has_existing_plan edge cases
# =====================================================================


class TestHasExistingPlan:
    def test_plan_in_task_title_case_insensitive(self):
        engine = DecisionEngine()
        tasks = [TaskSummary(stem="0001_Plan_system_health", title="Plan System Health Improvement", status="pending")]
        ctx = _ctx(_report({}), tasks=tasks)
        assert engine._has_existing_plan("system_health", ctx) is True

    def test_plan_in_decisions_only(self):
        engine = DecisionEngine()
        ctx = _ctx(_report({}), recent=[{"mode": "plan", "target_dimension": "system_health"}])
        assert engine._has_existing_plan("system_health", ctx) is True

    def test_no_plan_returns_false(self):
        engine = DecisionEngine()
        ctx = _ctx(_report({}))
        assert engine._has_existing_plan("system_health", ctx) is False

    def test_none_dimension_returns_false(self):
        engine = DecisionEngine()
        ctx = _ctx(_report({}))
        assert engine._has_existing_plan(None, ctx) is False
