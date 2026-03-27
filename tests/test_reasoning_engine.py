"""Tests for ReasoningEngine — LLM-powered (heuristic fallback) gap analysis."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from novatrade.autonomy.decision_context import DecisionContext, TaskSummary
from novatrade.autonomy.decision_engine import ActionMode, Decision
from novatrade.autonomy.reasoning_engine import (
    ReasoningConfig,
    ReasoningEngine,
    ReasoningResult,
)
from novatrade.autonomy.schemas import (
    DimensionScore,
    ProgressReport,
    ScoreTrend,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_report(
    overall: float = 75.0,
    dims: dict[str, float] | None = None,
    trends: dict[str, str] | None = None,
) -> ProgressReport:
    if dims is None:
        dims = {
            "system_health": 90.0,
            "strategy_validity": 60.0,
            "execution_pipeline": 80.0,
            "risk_engine": 85.0,
            "performance_stability": 70.0,
        }
    if trends is None:
        trends = {name: "stable" for name in dims}
    return ProgressReport(
        overall_score=overall,
        dimensions={name: DimensionScore(name=name, score=score) for name, score in dims.items()},
        trends={name: ScoreTrend(current=dims[name], direction=trends.get(name, "stable")) for name in dims},
    )


def _make_context(
    report: ProgressReport | None = None,
    pending_count: int = 0,
    recent_decisions: list[dict] | None = None,
) -> DecisionContext:
    if report is None:
        report = _make_report()
    return DecisionContext(
        progress_report=report,
        pending_tasks=[
            TaskSummary(stem=f"task_{i}", title=f"Task {i}", status="pending") for i in range(pending_count)
        ],
        recent_decisions=recent_decisions or [],
        time_of_day="morning",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestShouldReason:
    """Test when reasoning should be triggered."""

    def test_monitor_with_yellow_triggers(self, tmp_path: Path) -> None:
        engine = ReasoningEngine(base_path=str(tmp_path))
        decision = Decision(mode=ActionMode.MONITOR, reason="All good")
        report = _make_report(dims={"strategy_validity": 55.0, "system_health": 90.0})

        assert engine.should_reason(decision, report) is True

    def test_monitor_all_green_no_trigger(self, tmp_path: Path) -> None:
        engine = ReasoningEngine(base_path=str(tmp_path))
        decision = Decision(mode=ActionMode.MONITOR, reason="All good")
        report = _make_report(
            overall=85.0,
            dims={"system_health": 90.0, "strategy_validity": 80.0},
        )

        assert engine.should_reason(decision, report) is False

    def test_multiple_degrading_triggers(self, tmp_path: Path) -> None:
        engine = ReasoningEngine(base_path=str(tmp_path))
        decision = Decision(mode=ActionMode.REPAIR, reason="Fixing")
        report = _make_report(
            dims={"system_health": 60.0, "strategy_validity": 50.0},
            trends={"system_health": "degrading", "strategy_validity": "degrading"},
        )

        assert engine.should_reason(decision, report) is True

    def test_monitor_low_overall_triggers(self, tmp_path: Path) -> None:
        engine = ReasoningEngine(base_path=str(tmp_path))
        decision = Decision(mode=ActionMode.MONITOR, reason="Monitoring")
        report = _make_report(overall=70.0)

        assert engine.should_reason(decision, report) is True


class TestReason:
    """Test the reasoning process."""

    @pytest.mark.asyncio
    async def test_reason_returns_result(self, tmp_path: Path) -> None:
        engine = ReasoningEngine(base_path=str(tmp_path))
        report = _make_report(dims={"strategy_validity": 55.0, "system_health": 90.0})
        context = _make_context(report)

        result = await engine.reason(context, report)

        assert result is not None
        assert isinstance(result, ReasoningResult)
        assert result.recommended_mode in (
            "research",
            "plan",
            "execute",
            "monitor",
            "validate",
            "repair",
        )
        assert len(result.reasoning_chain) > 0

    @pytest.mark.asyncio
    async def test_reason_skips_on_budget(self, tmp_path: Path) -> None:
        config = ReasoningConfig(max_calls_per_day=0)
        engine = ReasoningEngine(config=config, base_path=str(tmp_path))
        report = _make_report()
        context = _make_context(report)

        result = await engine.reason(context, report)
        assert result is None

    @pytest.mark.asyncio
    async def test_reason_skips_unchanged_score(self, tmp_path: Path) -> None:
        config = ReasoningConfig(skip_if_score_unchanged=True, score_change_threshold=5.0)
        engine = ReasoningEngine(config=config, base_path=str(tmp_path))
        report = _make_report(overall=75.0)
        context = _make_context(report)

        # First call succeeds (no previous score)
        result1 = await engine.reason(context, report)
        assert result1 is not None

        # Second call with same score skips
        result2 = await engine.reason(context, report)
        assert result2 is None

    @pytest.mark.asyncio
    async def test_reason_persists_to_disk(self, tmp_path: Path) -> None:
        engine = ReasoningEngine(base_path=str(tmp_path))
        report = _make_report()
        context = _make_context(report)

        await engine.reason(context, report)

        history_path = tmp_path / "STATE" / "reasoning_history.json"
        assert history_path.exists()
        data = json.loads(history_path.read_text())
        assert len(data) == 1


class TestHeuristicReasoning:
    """Test the heuristic reasoning patterns."""

    @pytest.mark.asyncio
    async def test_correlated_yellow(self, tmp_path: Path) -> None:
        """3+ YELLOW dimensions should suggest systemic issue."""
        engine = ReasoningEngine(base_path=str(tmp_path))
        report = _make_report(
            overall=55.0,
            dims={
                "system_health": 55.0,
                "strategy_validity": 50.0,
                "execution_pipeline": 45.0,
                "risk_engine": 85.0,
                "performance_stability": 70.0,
            },
        )
        context = _make_context(report)

        result = await engine.reason(context, report)

        assert result is not None
        assert result.recommended_mode == "research"
        assert result.novel_insight is not None
        assert "correlated" in result.novel_insight.lower()

    @pytest.mark.asyncio
    async def test_stalled_improvement(self, tmp_path: Path) -> None:
        """Improving but below GREEN should suggest continued execution."""
        engine = ReasoningEngine(base_path=str(tmp_path))
        report = _make_report(
            overall=65.0,
            dims={"strategy_validity": 60.0, "system_health": 90.0},
            trends={"strategy_validity": "improving", "system_health": "stable"},
        )
        context = _make_context(report)

        result = await engine.reason(context, report)

        assert result is not None
        assert result.recommended_mode == "execute"

    @pytest.mark.asyncio
    async def test_degrading_despite_repairs(self, tmp_path: Path) -> None:
        """Degrading + recent repairs should suggest research."""
        engine = ReasoningEngine(base_path=str(tmp_path))
        report = _make_report(
            overall=65.0,
            dims={"strategy_validity": 50.0, "system_health": 80.0},
            trends={"strategy_validity": "degrading", "system_health": "stable"},
        )
        context = _make_context(
            report,
            recent_decisions=[
                {"mode": "repair", "target_dimension": "strategy_validity"},
            ],
        )

        result = await engine.reason(context, report)

        assert result is not None
        assert result.recommended_mode == "research"
        assert result.novel_insight is not None

    @pytest.mark.asyncio
    async def test_task_saturation(self, tmp_path: Path) -> None:
        """Too many pending tasks should suggest execution over planning."""
        engine = ReasoningEngine(base_path=str(tmp_path))
        report = _make_report(overall=75.0)
        context = _make_context(report, pending_count=15)

        result = await engine.reason(context, report)

        assert result is not None
        assert result.recommended_mode == "execute"
        assert "saturation" in (result.novel_insight or "").lower()

    @pytest.mark.asyncio
    async def test_all_green_monitor(self, tmp_path: Path) -> None:
        """All GREEN dimensions should return MONITOR."""
        engine = ReasoningEngine(base_path=str(tmp_path))
        report = _make_report(
            overall=85.0,
            dims={
                "system_health": 90.0,
                "strategy_validity": 80.0,
                "execution_pipeline": 85.0,
                "risk_engine": 90.0,
                "performance_stability": 75.0,
            },
        )
        context = _make_context(report)

        result = await engine.reason(context, report)

        assert result is not None
        assert result.recommended_mode == "monitor"

    @pytest.mark.asyncio
    async def test_red_dimension_repair(self, tmp_path: Path) -> None:
        """RED dimension with no YELLOW clustering should suggest repair."""
        engine = ReasoningEngine(base_path=str(tmp_path))
        report = _make_report(
            overall=60.0,
            dims={
                "system_health": 30.0,
                "strategy_validity": 80.0,
            },
        )
        context = _make_context(report)

        result = await engine.reason(context, report)

        assert result is not None
        assert result.recommended_mode == "repair"
        assert result.target_dimension == "system_health"


class TestBudgetTracking:
    """Test daily budget enforcement."""

    @pytest.mark.asyncio
    async def test_budget_enforcement(self, tmp_path: Path) -> None:
        config = ReasoningConfig(max_calls_per_day=2)
        engine = ReasoningEngine(config=config, base_path=str(tmp_path))
        report = _make_report()
        context = _make_context(report)

        # First 2 calls succeed
        r1 = await engine.reason(context, report)
        assert r1 is not None

        # Need different score for 2nd call
        engine._last_score = None
        report2 = _make_report(overall=80.0)
        context2 = _make_context(report2)
        r2 = await engine.reason(context2, report2)
        assert r2 is not None

        # 3rd call should be blocked
        engine._last_score = None
        report3 = _make_report(overall=85.0)
        context3 = _make_context(report3)
        r3 = await engine.reason(context3, report3)
        assert r3 is None


class TestReasoningResult:
    """Test ReasoningResult data structure."""

    def test_defaults(self) -> None:
        r = ReasoningResult(
            recommended_mode="repair",
            target_dimension="system_health",
            reasoning_chain=["step1"],
            suggested_actions=["action1"],
            confidence="high",
        )
        assert r.timestamp  # auto-populated
        assert r.novel_insight is None

    def test_with_insight(self) -> None:
        r = ReasoningResult(
            recommended_mode="research",
            target_dimension="strategy_validity",
            reasoning_chain=["step1", "step2"],
            suggested_actions=["action1"],
            confidence="medium",
            novel_insight="Correlated failures across dimensions",
        )
        assert r.novel_insight == "Correlated failures across dimensions"


class TestPromptBuilding:
    """Test prompt construction."""

    def test_prompt_includes_scores(self, tmp_path: Path) -> None:
        engine = ReasoningEngine(base_path=str(tmp_path))
        report = _make_report(dims={"system_health": 90.0, "strategy_validity": 55.0})
        context = _make_context(report)

        prompt = engine._build_prompt(context, report, None)

        assert "system_health" in prompt
        assert "strategy_validity" in prompt
        assert "90" in prompt
        assert "55" in prompt

    def test_prompt_includes_outcomes(self, tmp_path: Path) -> None:
        engine = ReasoningEngine(base_path=str(tmp_path))
        report = _make_report()
        context = _make_context(report)
        outcomes = [{"mode": "repair", "delta": 5.0, "effectiveness": "effective"}]

        prompt = engine._build_prompt(context, report, outcomes)

        assert "effective" in prompt
        assert "repair" in prompt
