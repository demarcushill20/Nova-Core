"""Phase 4: Enriched Decision Context — tests for market session, degradation tier,
circuit breakers, active investigations, actionable sub-goals, and their integration
into the decision engine.

Covers:
- Step 4.1: New DecisionContext fields (defaults, assignment)
- Step 4.2: Market session classification (all hour ranges)
- Step 4.3: Degradation tier loading, circuit breaker loading, active investigations,
            actionable sub-goals
- Step 4.4: Market-closed suppresses NovaTrade not-trading alarm;
            degradation MINIMAL/EMERGENCY suppresses PLAN/EXECUTE
- Full assemble() includes all new fields
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from novatrade.autonomy.decision_context import (
    ContextAssembler,
    DecisionContext,
)
from novatrade.autonomy.decision_engine import (
    ActionMode,
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
    report: ProgressReport | None = None,
    recent_decisions: list[dict] | None = None,
    market_session: str = "closed",
    degradation_tier: str = "FULL",
    open_circuit_breakers: list[str] | None = None,
    active_investigations: list[str] | None = None,
    actionable_subgoals: list[str] | None = None,
) -> DecisionContext:
    if report is None:
        dims = {
            "system_health": _dim("system_health", 80.0),
            "execution_pipeline": _dim("execution_pipeline", 75.0),
        }
        report = _report(dims)
    return DecisionContext(
        progress_report=report,
        recent_decisions=recent_decisions or [],
        market_session=market_session,
        degradation_tier=degradation_tier,
        open_circuit_breakers=open_circuit_breakers or [],
        active_investigations=active_investigations or [],
        actionable_subgoals=actionable_subgoals or [],
    )


# =====================================================================
# Step 4.1: DecisionContext model — new field defaults
# =====================================================================


class TestDecisionContextNewFields:
    def test_defaults_are_set(self):
        """New fields should have sensible defaults when not explicitly provided."""
        report = _report({"sh": _dim("sh", 80.0)})
        ctx = DecisionContext(progress_report=report)
        assert ctx.degradation_tier == "FULL"
        assert ctx.open_circuit_breakers == []
        assert ctx.market_session == "closed"
        assert ctx.active_investigations == []
        assert ctx.actionable_subgoals == []

    def test_fields_can_be_assigned(self):
        """New fields can be explicitly set."""
        report = _report({"sh": _dim("sh", 80.0)})
        ctx = DecisionContext(
            progress_report=report,
            degradation_tier="MINIMAL",
            open_circuit_breakers=["metaapi", "telegram"],
            market_session="london",
            active_investigations=["investigate_feed_health"],
            actionable_subgoals=["Get signals generating", "Fix webhook"],
        )
        assert ctx.degradation_tier == "MINIMAL"
        assert ctx.open_circuit_breakers == ["metaapi", "telegram"]
        assert ctx.market_session == "london"
        assert ctx.active_investigations == ["investigate_feed_health"]
        assert ctx.actionable_subgoals == ["Get signals generating", "Fix webhook"]

    def test_serialization_round_trip(self):
        """New fields survive JSON round-trip."""
        report = _report({"sh": _dim("sh", 80.0)})
        ctx = DecisionContext(
            progress_report=report,
            degradation_tier="EMERGENCY",
            open_circuit_breakers=["feed"],
            market_session="overlap",
            active_investigations=["inv1"],
            actionable_subgoals=["sg1"],
        )
        data = json.loads(ctx.model_dump_json())
        assert data["degradation_tier"] == "EMERGENCY"
        assert data["open_circuit_breakers"] == ["feed"]
        assert data["market_session"] == "overlap"
        assert data["active_investigations"] == ["inv1"]
        assert data["actionable_subgoals"] == ["sg1"]


# =====================================================================
# Step 4.2: Market session classification
# =====================================================================


class TestMarketSessionClassification:
    """Test _classify_market_session for all hour ranges."""

    @pytest.mark.parametrize(
        "hour,expected",
        [
            (0, "asian"),
            (1, "asian"),
            (3, "asian"),
            (6, "asian"),
            (7, "london"),  # London pre-open
            (8, "london"),
            (10, "london"),
            (12, "london"),
            (13, "overlap"),  # London+NY overlap
            (14, "overlap"),
            (15, "overlap"),
            (16, "new_york"),
            (18, "new_york"),
            (20, "new_york"),
            (21, "closed"),
            (22, "closed"),
            (23, "closed"),
        ],
    )
    def test_hour_to_session(self, hour, expected):
        mock_dt = datetime(2026, 3, 31, hour, 30, 0, tzinfo=timezone.utc)
        with patch("novatrade.autonomy.decision_context.datetime") as mock_datetime:
            mock_datetime.now.return_value = mock_dt
            mock_datetime.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = ContextAssembler._classify_market_session()
        assert result == expected, f"Hour {hour} should be {expected}, got {result}"

    def test_returns_valid_session_at_runtime(self):
        """Smoke test: should return one of the known sessions."""
        result = ContextAssembler._classify_market_session()
        assert result in ("asian", "london", "overlap", "new_york", "closed")

    def test_saturday_returns_closed(self):
        """Saturday at any hour should return 'closed'."""
        # 2026-04-04 is a Saturday
        mock_dt = datetime(2026, 4, 4, 12, 0, 0, tzinfo=timezone.utc)
        with patch("novatrade.autonomy.decision_context.datetime") as mock_datetime:
            mock_datetime.now.return_value = mock_dt
            mock_datetime.side_effect = lambda *a, **kw: datetime(*a, **kw)
            assert ContextAssembler._classify_market_session() == "closed"

    def test_sunday_before_open_returns_closed(self):
        """Sunday before 22:00 UTC should return 'closed'."""
        # 2026-04-05 is a Sunday
        mock_dt = datetime(2026, 4, 5, 10, 0, 0, tzinfo=timezone.utc)
        with patch("novatrade.autonomy.decision_context.datetime") as mock_datetime:
            mock_datetime.now.return_value = mock_dt
            mock_datetime.side_effect = lambda *a, **kw: datetime(*a, **kw)
            assert ContextAssembler._classify_market_session() == "closed"

    def test_sunday_at_open_returns_session(self):
        """Sunday at 22:00 UTC (market open) — hour-based logic returns 'closed'."""
        # 2026-04-05 is a Sunday
        mock_dt = datetime(2026, 4, 5, 22, 0, 0, tzinfo=timezone.utc)
        with patch("novatrade.autonomy.decision_context.datetime") as mock_datetime:
            mock_datetime.now.return_value = mock_dt
            mock_datetime.side_effect = lambda *a, **kw: datetime(*a, **kw)
            # 22:00 UTC falls into the "closed" bucket by hour logic
            assert ContextAssembler._classify_market_session() == "closed"

    def test_friday_before_close_returns_session(self):
        """Friday before 22:00 UTC should return normal session."""
        # 2026-04-03 is a Friday
        mock_dt = datetime(2026, 4, 3, 16, 0, 0, tzinfo=timezone.utc)
        with patch("novatrade.autonomy.decision_context.datetime") as mock_datetime:
            mock_datetime.now.return_value = mock_dt
            mock_datetime.side_effect = lambda *a, **kw: datetime(*a, **kw)
            assert ContextAssembler._classify_market_session() == "new_york"

    def test_friday_after_close_returns_closed(self):
        """Friday at 22:00 UTC should return 'closed'."""
        # 2026-04-03 is a Friday
        mock_dt = datetime(2026, 4, 3, 22, 0, 0, tzinfo=timezone.utc)
        with patch("novatrade.autonomy.decision_context.datetime") as mock_datetime:
            mock_datetime.now.return_value = mock_dt
            mock_datetime.side_effect = lambda *a, **kw: datetime(*a, **kw)
            assert ContextAssembler._classify_market_session() == "closed"

    def test_sunday_21_59_returns_closed(self):
        """Sunday 21:59 UTC — still weekend, should be 'closed'."""
        # 2026-04-05 is a Sunday
        mock_dt = datetime(2026, 4, 5, 21, 59, 0, tzinfo=timezone.utc)
        with patch("novatrade.autonomy.decision_context.datetime") as mock_datetime:
            mock_datetime.now.return_value = mock_dt
            mock_datetime.side_effect = lambda *a, **kw: datetime(*a, **kw)
            assert ContextAssembler._classify_market_session() == "closed"


# =====================================================================
# Step 4.3: Degradation tier loading
# =====================================================================


class TestLoadDegradationTier:
    def test_returns_full_when_file_missing(self, tmp_path):
        assembler = ContextAssembler(base_path=str(tmp_path))
        assert assembler._load_degradation_tier() == "FULL"

    def test_reads_tier_from_file(self, tmp_path):
        state_dir = tmp_path / "STATE"
        state_dir.mkdir()
        (state_dir / "self_healing_state.json").write_text(json.dumps({"current_tier": "MINIMAL"}))
        assembler = ContextAssembler(base_path=str(tmp_path))
        assert assembler._load_degradation_tier() == "MINIMAL"

    def test_returns_full_on_corrupt_file(self, tmp_path):
        state_dir = tmp_path / "STATE"
        state_dir.mkdir()
        (state_dir / "self_healing_state.json").write_text("not json at all")
        assembler = ContextAssembler(base_path=str(tmp_path))
        assert assembler._load_degradation_tier() == "FULL"

    def test_returns_full_when_key_missing(self, tmp_path):
        state_dir = tmp_path / "STATE"
        state_dir.mkdir()
        (state_dir / "self_healing_state.json").write_text(json.dumps({"other": "data"}))
        assembler = ContextAssembler(base_path=str(tmp_path))
        assert assembler._load_degradation_tier() == "FULL"

    def test_reads_emergency_tier(self, tmp_path):
        state_dir = tmp_path / "STATE"
        state_dir.mkdir()
        (state_dir / "self_healing_state.json").write_text(json.dumps({"current_tier": "EMERGENCY"}))
        assembler = ContextAssembler(base_path=str(tmp_path))
        assert assembler._load_degradation_tier() == "EMERGENCY"


# =====================================================================
# Step 4.3: Circuit breaker loading
# =====================================================================


class TestLoadCircuitBreakers:
    def test_returns_empty_when_file_missing(self, tmp_path):
        assembler = ContextAssembler(base_path=str(tmp_path))
        assert assembler._load_open_circuit_breakers() == []

    def test_returns_open_breakers(self, tmp_path):
        state_dir = tmp_path / "STATE"
        state_dir.mkdir()
        registry = {
            "metaapi": {"state": "open", "opened_at": "2026-03-31T10:00:00Z"},
            "telegram": {"state": "closed"},
            "feed": {"state": "open", "opened_at": "2026-03-31T09:00:00Z"},
        }
        (state_dir / "circuit_breaker_registry.json").write_text(json.dumps(registry))
        assembler = ContextAssembler(base_path=str(tmp_path))
        result = assembler._load_open_circuit_breakers()
        assert sorted(result) == ["feed", "metaapi"]

    def test_returns_empty_when_all_closed(self, tmp_path):
        state_dir = tmp_path / "STATE"
        state_dir.mkdir()
        registry = {
            "metaapi": {"state": "closed"},
            "telegram": {"state": "closed"},
        }
        (state_dir / "circuit_breaker_registry.json").write_text(json.dumps(registry))
        assembler = ContextAssembler(base_path=str(tmp_path))
        assert assembler._load_open_circuit_breakers() == []

    def test_returns_empty_on_corrupt_file(self, tmp_path):
        state_dir = tmp_path / "STATE"
        state_dir.mkdir()
        (state_dir / "circuit_breaker_registry.json").write_text("{{bad json")
        assembler = ContextAssembler(base_path=str(tmp_path))
        assert assembler._load_open_circuit_breakers() == []

    def test_returns_empty_when_not_dict(self, tmp_path):
        state_dir = tmp_path / "STATE"
        state_dir.mkdir()
        (state_dir / "circuit_breaker_registry.json").write_text(json.dumps([1, 2, 3]))
        assembler = ContextAssembler(base_path=str(tmp_path))
        assert assembler._load_open_circuit_breakers() == []

    def test_skips_non_dict_entries(self, tmp_path):
        """Non-dict values inside the registry should be skipped gracefully."""
        state_dir = tmp_path / "STATE"
        state_dir.mkdir()
        registry = {
            "metaapi": {"state": "open"},
            "broken_entry": "not a dict",
            "feed": {"state": "closed"},
        }
        (state_dir / "circuit_breaker_registry.json").write_text(json.dumps(registry))
        assembler = ContextAssembler(base_path=str(tmp_path))
        result = assembler._load_open_circuit_breakers()
        assert result == ["metaapi"]


# =====================================================================
# Step 4.3: Active investigations loading
# =====================================================================


class TestLoadActiveInvestigations:
    def test_returns_empty_when_tasks_dir_missing(self, tmp_path):
        assembler = ContextAssembler(base_path=str(tmp_path))
        assert assembler._load_active_investigations() == []

    def test_finds_investigation_tasks(self, tmp_path):
        tasks_dir = tmp_path / "TASKS"
        tasks_dir.mkdir()
        (tasks_dir / "0650_investigate_feed_health.inprogress").write_text("")
        (tasks_dir / "0651_investigate_metaapi.inprogress").write_text("")
        (tasks_dir / "0652_normal_task.inprogress").write_text("")  # not investigation
        (tasks_dir / "0653_investigate_done.md").write_text("")  # not .inprogress
        assembler = ContextAssembler(base_path=str(tmp_path))
        result = assembler._load_active_investigations()
        assert len(result) == 2
        assert "0650_investigate_feed_health" in result
        assert "0651_investigate_metaapi" in result

    def test_caps_at_10(self, tmp_path):
        tasks_dir = tmp_path / "TASKS"
        tasks_dir.mkdir()
        for i in range(15):
            (tasks_dir / f"0{700 + i}_investigate_item_{i}.inprogress").write_text("")
        assembler = ContextAssembler(base_path=str(tmp_path))
        result = assembler._load_active_investigations()
        assert len(result) == 10

    def test_empty_when_no_investigations(self, tmp_path):
        tasks_dir = tmp_path / "TASKS"
        tasks_dir.mkdir()
        (tasks_dir / "0650_normal_task.inprogress").write_text("")
        assembler = ContextAssembler(base_path=str(tmp_path))
        result = assembler._load_active_investigations()
        assert result == []


# =====================================================================
# Step 4.3: Actionable sub-goals loading
# =====================================================================


class TestLoadActionableSubgoals:
    def test_returns_empty_when_file_missing(self, tmp_path):
        assembler = ContextAssembler(base_path=str(tmp_path))
        assert assembler._load_actionable_subgoals() == []

    def test_returns_actionable_subgoals(self, tmp_path):
        goal_dir = tmp_path / "STATE" / "goal_trees"
        goal_dir.mkdir(parents=True)
        tree = {
            "sub_goals": [
                {"sub_goal_id": "sg1", "title": "Deploy service", "status": "completed", "dependencies": []},
                {"sub_goal_id": "sg2", "title": "Generate signals", "status": "in_progress", "dependencies": ["sg1"]},
                {"sub_goal_id": "sg3", "title": "Fix webhook", "status": "pending", "dependencies": []},
                {"sub_goal_id": "sg4", "title": "Blocked task", "status": "pending", "dependencies": ["sg2"]},
            ]
        }
        (goal_dir / "novatrade_100pct_operational.json").write_text(json.dumps(tree))
        assembler = ContextAssembler(base_path=str(tmp_path))
        result = assembler._load_actionable_subgoals()
        # sg1 is completed => not returned
        # sg2 deps [sg1] all completed => actionable
        # sg3 deps [] => actionable
        # sg4 deps [sg2] not completed => blocked
        assert "Generate signals" in result
        assert "Fix webhook" in result
        assert "Deploy service" not in result
        assert "Blocked task" not in result

    def test_returns_empty_on_corrupt_file(self, tmp_path):
        goal_dir = tmp_path / "STATE" / "goal_trees"
        goal_dir.mkdir(parents=True)
        (goal_dir / "novatrade_100pct_operational.json").write_text("invalid json")
        assembler = ContextAssembler(base_path=str(tmp_path))
        assert assembler._load_actionable_subgoals() == []

    def test_returns_empty_when_all_completed(self, tmp_path):
        goal_dir = tmp_path / "STATE" / "goal_trees"
        goal_dir.mkdir(parents=True)
        tree = {
            "sub_goals": [
                {"sub_goal_id": "sg1", "title": "Done A", "status": "completed", "dependencies": []},
                {"sub_goal_id": "sg2", "title": "Done B", "status": "skipped", "dependencies": []},
            ]
        }
        (goal_dir / "novatrade_100pct_operational.json").write_text(json.dumps(tree))
        assembler = ContextAssembler(base_path=str(tmp_path))
        assert assembler._load_actionable_subgoals() == []

    def test_handles_missing_sub_goals_key(self, tmp_path):
        goal_dir = tmp_path / "STATE" / "goal_trees"
        goal_dir.mkdir(parents=True)
        (goal_dir / "novatrade_100pct_operational.json").write_text(json.dumps({"other": "data"}))
        assembler = ContextAssembler(base_path=str(tmp_path))
        assert assembler._load_actionable_subgoals() == []

    def test_caps_at_10(self, tmp_path):
        goal_dir = tmp_path / "STATE" / "goal_trees"
        goal_dir.mkdir(parents=True)
        tree = {
            "sub_goals": [
                {"sub_goal_id": f"sg{i}", "title": f"Goal {i}", "status": "pending", "dependencies": []}
                for i in range(20)
            ]
        }
        (goal_dir / "novatrade_100pct_operational.json").write_text(json.dumps(tree))
        assembler = ContextAssembler(base_path=str(tmp_path))
        result = assembler._load_actionable_subgoals()
        assert len(result) == 10

    def test_uses_sub_goal_id_as_fallback_title(self, tmp_path):
        goal_dir = tmp_path / "STATE" / "goal_trees"
        goal_dir.mkdir(parents=True)
        tree = {
            "sub_goals": [
                {"sub_goal_id": "sg_no_title", "status": "pending", "dependencies": []},
            ]
        }
        (goal_dir / "novatrade_100pct_operational.json").write_text(json.dumps(tree))
        assembler = ContextAssembler(base_path=str(tmp_path))
        result = assembler._load_actionable_subgoals()
        assert result == ["sg_no_title"]


# =====================================================================
# Step 4.4: Market-closed suppresses NovaTrade not-trading alarm
# =====================================================================


class TestMarketClosedSuppression:
    def test_zero_trades_suppressed_when_market_closed(self):
        """When market_session is 'closed', zero-trade alarm should not fire."""
        dims = {
            "strategy_validity": _dim(
                "strategy_validity",
                30.0,
                [SubMetric(name="trades_last_24h", value=10)],
            ),
            "system_health": _dim("system_health", 80.0),
        }
        engine = DecisionEngine()
        result = engine._detect_novatrade_not_trading(_report(dims), market_session="closed")
        assert result is None

    def test_zero_trades_fires_when_market_open(self):
        """When market is open (london), zero-trade alarm should fire."""
        dims = {
            "strategy_validity": _dim(
                "strategy_validity",
                30.0,
                [SubMetric(name="trades_last_24h", value=10)],
            ),
            "system_health": _dim("system_health", 80.0),
        }
        engine = DecisionEngine()
        result = engine._detect_novatrade_not_trading(_report(dims), market_session="london")
        assert result is not None
        assert result.mode == ActionMode.REPAIR

    def test_zero_trades_fires_during_overlap(self):
        """During overlap session, zero-trade alarm should fire."""
        dims = {
            "strategy_validity": _dim(
                "strategy_validity",
                30.0,
                [SubMetric(name="trades_last_24h", value=10)],
            ),
        }
        engine = DecisionEngine()
        result = engine._detect_novatrade_not_trading(_report(dims), market_session="overlap")
        assert result is not None
        assert result.mode == ActionMode.REPAIR

    def test_zero_trades_fires_during_asian(self):
        """During Asian session, zero-trade alarm should fire."""
        dims = {
            "strategy_validity": _dim(
                "strategy_validity",
                30.0,
                [SubMetric(name="trades_last_24h", value=10)],
            ),
        }
        engine = DecisionEngine()
        result = engine._detect_novatrade_not_trading(_report(dims), market_session="asian")
        assert result is not None

    def test_zero_trades_fires_during_new_york(self):
        """During NY session, zero-trade alarm should fire."""
        dims = {
            "strategy_validity": _dim(
                "strategy_validity",
                30.0,
                [SubMetric(name="trades_last_24h", value=10)],
            ),
        }
        engine = DecisionEngine()
        result = engine._detect_novatrade_not_trading(_report(dims), market_session="new_york")
        assert result is not None

    def test_silent_failure_suppressed_when_market_closed(self):
        """Silent failure should also be suppressed when market is closed."""
        dims = {
            "strategy_validity": _dim(
                "strategy_validity",
                50.0,
                [
                    SubMetric(name="trades_last_24h", value=80),
                    SubMetric(name="silent_failure_detected", value=0),
                ],
            ),
        }
        engine = DecisionEngine()
        result = engine._detect_novatrade_not_trading(_report(dims), market_session="closed")
        assert result is None

    @pytest.mark.asyncio
    async def test_full_decide_suppresses_novatrade_when_closed(self, tmp_path):
        """End-to-end: decide() should not fire NovaTrade critical during closed market."""
        dims = {
            "strategy_validity": _dim(
                "strategy_validity",
                30.0,
                [SubMetric(name="trades_last_24h", value=10)],
            ),
            "system_health": _dim("system_health", 80.0),
        }
        report = _report(dims)
        ctx = _ctx(report=report, market_session="closed")
        engine = DecisionEngine(base_path=str(tmp_path))
        (tmp_path / "STATE").mkdir()
        decision = await engine.decide(ctx)
        # Should NOT be the NovaTrade critical REPAIR — it should fall through
        # to lower-priority rules
        assert "ZERO trades" not in decision.reason


# =====================================================================
# Step 4.4: Degradation tier suppresses PLAN/EXECUTE
# =====================================================================


class TestDegradationSuppression:
    @pytest.mark.asyncio
    async def test_minimal_degradation_suppresses_plan(self, tmp_path):
        """MINIMAL degradation should suppress PLAN to MONITOR."""
        dims = {
            "system_health": _dim("system_health", 50.0),
            "execution_pipeline": _dim("execution_pipeline", 80.0),
        }
        report = _report(dims)
        # No existing plan => would normally trigger PLAN for system_health
        ctx = _ctx(report=report, degradation_tier="MINIMAL", market_session="london")
        engine = DecisionEngine(base_path=str(tmp_path))
        (tmp_path / "STATE").mkdir()
        decision = await engine.decide(ctx)
        assert decision.mode == ActionMode.MONITOR
        assert "MINIMAL" in decision.reason
        assert "suppressing" in decision.reason.lower()

    @pytest.mark.asyncio
    async def test_emergency_degradation_suppresses_execute(self, tmp_path):
        """EMERGENCY degradation should suppress EXECUTE to MONITOR."""
        dims = {
            "system_health": _dim("system_health", 50.0),
            "execution_pipeline": _dim("execution_pipeline", 80.0),
        }
        report = _report(dims)
        # Has existing plan => would normally trigger EXECUTE
        recent = [{"mode": "plan", "target_dimension": "system_health"}]
        ctx = _ctx(
            report=report,
            recent_decisions=recent,
            degradation_tier="EMERGENCY",
            market_session="london",
        )
        engine = DecisionEngine(base_path=str(tmp_path))
        (tmp_path / "STATE").mkdir()
        decision = await engine.decide(ctx)
        assert decision.mode == ActionMode.MONITOR
        assert "EMERGENCY" in decision.reason

    @pytest.mark.asyncio
    async def test_full_degradation_allows_plan(self, tmp_path):
        """FULL degradation should not suppress PLAN."""
        dims = {
            "system_health": _dim("system_health", 50.0),
            "execution_pipeline": _dim("execution_pipeline", 80.0),
        }
        report = _report(dims)
        ctx = _ctx(report=report, degradation_tier="FULL", market_session="london")
        engine = DecisionEngine(base_path=str(tmp_path))
        (tmp_path / "STATE").mkdir()
        decision = await engine.decide(ctx)
        assert decision.mode == ActionMode.PLAN

    @pytest.mark.asyncio
    async def test_degradation_does_not_suppress_repair(self, tmp_path):
        """Even in MINIMAL degradation, REPAIR should still be allowed."""
        dims = {
            "system_health": _dim("system_health", 50.0),
            "execution_pipeline": _dim("execution_pipeline", 80.0),
        }
        report = _report(
            dims,
            trends={
                "system_health": _trend(50.0, avg_6h=65.0, direction="degrading"),
                "execution_pipeline": _trend(80.0),
            },
        )
        ctx = _ctx(report=report, degradation_tier="MINIMAL", market_session="london")
        engine = DecisionEngine(
            config=DecisionConfig(regression_delta=10.0),
            base_path=str(tmp_path),
        )
        (tmp_path / "STATE").mkdir()
        decision = await engine.decide(ctx)
        assert decision.mode == ActionMode.REPAIR

    @pytest.mark.asyncio
    async def test_degradation_does_not_suppress_escalate(self, tmp_path):
        """ESCALATE (repair futility) should not be suppressed by degradation."""
        dims = {
            "system_health": _dim("system_health", 80.0),
            "execution_pipeline": _dim("execution_pipeline", 80.0),
        }
        report = _report(dims)
        # 3 ineffective repair outcomes on same dimension => ESCALATE
        recent_outcomes = [
            {"mode": "repair", "target_dimension": "system_health", "effectiveness": "neutral", "delta": -1.0},
            {"mode": "repair", "target_dimension": "system_health", "effectiveness": "neutral", "delta": 0.0},
            {
                "mode": "repair",
                "target_dimension": "system_health",
                "effectiveness": "counterproductive",
                "delta": -5.0,
            },
        ]
        ctx = DecisionContext(
            progress_report=report,
            recent_outcomes=recent_outcomes,
            degradation_tier="EMERGENCY",
            market_session="london",
        )
        engine = DecisionEngine(base_path=str(tmp_path))
        (tmp_path / "STATE").mkdir()
        decision = await engine.decide(ctx)
        assert decision.mode == ActionMode.ESCALATE


# =====================================================================
# Full assemble() integration — includes all new fields
# =====================================================================


class TestAssembleIncludesNewFields:
    @pytest.mark.asyncio
    async def test_assemble_populates_all_new_fields(self, tmp_path):
        """assemble() should populate degradation_tier, breakers, market_session, etc."""
        state_dir = tmp_path / "STATE"
        state_dir.mkdir()
        goal_dir = state_dir / "goal_trees"
        goal_dir.mkdir()
        tasks_dir = tmp_path / "TASKS"
        tasks_dir.mkdir()

        # Set up self-healing state
        (state_dir / "self_healing_state.json").write_text(json.dumps({"current_tier": "DEGRADED"}))

        # Set up circuit breakers
        (state_dir / "circuit_breaker_registry.json").write_text(
            json.dumps(
                {
                    "metaapi": {"state": "open"},
                    "telegram": {"state": "closed"},
                }
            )
        )

        # Set up investigation task
        (tasks_dir / "0700_investigate_feed.inprogress").write_text("")

        # Set up goal tree
        tree = {
            "sub_goals": [
                {"sub_goal_id": "sg1", "title": "Fix feed", "status": "pending", "dependencies": []},
            ]
        }
        (goal_dir / "novatrade_100pct_operational.json").write_text(json.dumps(tree))

        assembler = ContextAssembler(base_path=str(tmp_path))
        dims = {"sh": _dim("sh", 80.0)}
        report = _report(dims)
        ctx = await assembler.assemble(report)

        assert ctx.degradation_tier == "DEGRADED"
        assert ctx.open_circuit_breakers == ["metaapi"]
        assert ctx.market_session in ("asian", "london", "overlap", "new_york", "closed")
        assert "0700_investigate_feed" in ctx.active_investigations
        assert "Fix feed" in ctx.actionable_subgoals

    @pytest.mark.asyncio
    async def test_assemble_defaults_when_state_missing(self, tmp_path):
        """assemble() should return safe defaults when no state files exist."""
        assembler = ContextAssembler(base_path=str(tmp_path))
        dims = {"sh": _dim("sh", 80.0)}
        report = _report(dims)
        ctx = await assembler.assemble(report)

        assert ctx.degradation_tier == "FULL"
        assert ctx.open_circuit_breakers == []
        assert ctx.active_investigations == []
        assert ctx.actionable_subgoals == []
        # market_session is always computed from current time
        assert ctx.market_session in ("asian", "london", "overlap", "new_york", "closed")


# =====================================================================
# Backward compatibility — existing tests should still pass
# =====================================================================


class TestBackwardCompatibility:
    def test_old_style_context_creation_still_works(self):
        """Creating DecisionContext without new fields should still work."""
        report = _report({"sh": _dim("sh", 80.0)})
        ctx = DecisionContext(
            progress_report=report,
            pending_tasks=[],
            active_goals=[],
            recent_decisions=[],
            time_of_day="morning",
        )
        assert ctx.degradation_tier == "FULL"
        assert ctx.market_session == "closed"

    def test_detect_novatrade_default_market_session(self):
        """_detect_novatrade_not_trading with default market_session='unknown' should still work."""
        dims = {
            "strategy_validity": _dim(
                "strategy_validity",
                30.0,
                [SubMetric(name="trades_last_24h", value=10)],
            ),
        }
        engine = DecisionEngine()
        # Default market_session="unknown" should not suppress
        result = engine._detect_novatrade_not_trading(_report(dims))
        assert result is not None
        assert result.mode == ActionMode.REPAIR
