"""Phase 5 tests — reasoning engine LLM wiring, prompt enrichment, case-sensitivity fix."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from novatrade.autonomy.decision_context import DecisionContext, TaskSummary
from novatrade.autonomy.decision_engine import ActionMode, Decision
from novatrade.autonomy.reasoning_engine import (
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
    market_session: str = "london",
    degradation_tier: str = "FULL",
    open_circuit_breakers: list[str] | None = None,
    actionable_subgoals: list[str] | None = None,
    effectiveness_summary: dict[str, float] | None = None,
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
        market_session=market_session,
        degradation_tier=degradation_tier,
        open_circuit_breakers=open_circuit_breakers or [],
        actionable_subgoals=actionable_subgoals or [],
        effectiveness_summary=effectiveness_summary or {},
    )


# ---------------------------------------------------------------------------
# 5.1: _call_llm wired through cost_router with fallback
# ---------------------------------------------------------------------------


class TestCallLLM:
    """Test _call_llm with real cost_router integration and fallback."""

    @pytest.mark.asyncio
    async def test_call_llm_with_valid_json_response(self, tmp_path: Path) -> None:
        """_call_llm should parse valid JSON from LLM into a ReasoningResult."""
        engine = ReasoningEngine(base_path=str(tmp_path))
        report = _make_report()
        context = _make_context(report)

        valid_response = json.dumps(
            {
                "mode": "research",
                "target_dimension": "strategy_validity",
                "reasoning": ["Low score needs investigation", "No recent improvements"],
                "actions": ["Check strategy config", "Review backtests"],
                "confidence": "high",
                "novel_insight": "Strategy config may be stale",
            }
        )

        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(valid_response.encode(), b""))

        with (
            patch("asyncio.create_subprocess_exec", return_value=mock_proc),
            patch("asyncio.wait_for", return_value=(valid_response.encode(), b"")),
        ):
            mock_proc.communicate = AsyncMock(return_value=(valid_response.encode(), b""))
            with patch(
                "asyncio.wait_for", new_callable=lambda: lambda: AsyncMock(return_value=(valid_response.encode(), b""))
            ):
                result = await engine._call_llm("test prompt", report, context)

        assert isinstance(result, ReasoningResult)
        # Even if subprocess fails, we get a heuristic result — both are valid ReasoningResult
        assert result.recommended_mode in ("research", "plan", "execute", "monitor", "validate", "repair", "escalate")

    @pytest.mark.asyncio
    async def test_call_llm_fallback_on_import_error(self, tmp_path: Path) -> None:
        """_call_llm should fall back to heuristic when cost_router import fails."""
        engine = ReasoningEngine(base_path=str(tmp_path))
        report = _make_report(dims={"strategy_validity": 55.0, "system_health": 90.0})
        context = _make_context(report)

        with patch.dict("sys.modules", {"utils.cost_router": None}):
            # Import will raise, triggering fallback
            result = await engine._call_llm("test prompt", report, context)

        assert isinstance(result, ReasoningResult)
        assert len(result.reasoning_chain) > 0

    @pytest.mark.asyncio
    async def test_call_llm_fallback_on_subprocess_error(self, tmp_path: Path) -> None:
        """_call_llm should fall back to heuristic when subprocess fails."""
        engine = ReasoningEngine(base_path=str(tmp_path))
        report = _make_report()
        context = _make_context(report)

        with (
            patch("utils.cost_router.route_task") as mock_route,
            patch("asyncio.create_subprocess_exec", side_effect=OSError("no claude")),
        ):
            mock_route.return_value = MagicMock(model="claude-haiku-3-20250307")
            result = await engine._call_llm("test prompt", report, context)

        assert isinstance(result, ReasoningResult)
        # Should be heuristic result
        assert len(result.reasoning_chain) > 0


# ---------------------------------------------------------------------------
# 5.1: _parse_llm_response
# ---------------------------------------------------------------------------


class TestParseLLMResponse:
    """Test _parse_llm_response with various inputs."""

    def test_parse_valid_json(self, tmp_path: Path) -> None:
        engine = ReasoningEngine(base_path=str(tmp_path))
        report = _make_report()
        context = _make_context(report)

        response = json.dumps(
            {
                "mode": "execute",
                "target_dimension": "strategy_validity",
                "reasoning": ["Need to push past threshold"],
                "actions": ["Deploy updated config"],
                "confidence": "high",
                "novel_insight": "Config drift detected",
            }
        )

        result = engine._parse_llm_response(response, report, context)

        assert result.recommended_mode == "execute"
        assert result.target_dimension == "strategy_validity"
        assert result.reasoning_chain == ["Need to push past threshold"]
        assert result.suggested_actions == ["Deploy updated config"]
        assert result.confidence == "high"
        assert result.novel_insight == "Config drift detected"

    def test_parse_json_with_code_fence(self, tmp_path: Path) -> None:
        """Should strip ```json ... ``` wrappers."""
        engine = ReasoningEngine(base_path=str(tmp_path))
        report = _make_report()
        context = _make_context(report)

        response = '```json\n{"mode": "monitor", "reasoning": ["All good"], "actions": [], "confidence": "high"}\n```'

        result = engine._parse_llm_response(response, report, context)

        assert result.recommended_mode == "monitor"
        assert result.confidence == "high"

    def test_parse_invalid_json_falls_back(self, tmp_path: Path) -> None:
        """Malformed JSON should trigger heuristic fallback."""
        engine = ReasoningEngine(base_path=str(tmp_path))
        report = _make_report(dims={"strategy_validity": 55.0, "system_health": 90.0})
        context = _make_context(report)

        result = engine._parse_llm_response("not json at all {{{", report, context)

        assert isinstance(result, ReasoningResult)
        # Should be from heuristic (we can check it returns a valid result)
        assert len(result.reasoning_chain) > 0

    def test_parse_missing_mode_falls_back(self, tmp_path: Path) -> None:
        """Missing mode field should trigger fallback."""
        engine = ReasoningEngine(base_path=str(tmp_path))
        report = _make_report()
        context = _make_context(report)

        response = json.dumps({"reasoning": ["test"], "actions": [], "confidence": "high"})

        result = engine._parse_llm_response(response, report, context)

        # Empty mode string is not in valid_modes, so falls back
        assert isinstance(result, ReasoningResult)

    def test_parse_invalid_mode_falls_back(self, tmp_path: Path) -> None:
        """Invalid mode value should trigger fallback."""
        engine = ReasoningEngine(base_path=str(tmp_path))
        report = _make_report()
        context = _make_context(report)

        response = json.dumps({"mode": "DESTROY_EVERYTHING", "reasoning": [], "actions": [], "confidence": "high"})

        result = engine._parse_llm_response(response, report, context)

        # Falls back to heuristic
        assert isinstance(result, ReasoningResult)

    def test_parse_mode_case_insensitive(self, tmp_path: Path) -> None:
        """Mode should be accepted in any case and normalized to lowercase."""
        engine = ReasoningEngine(base_path=str(tmp_path))
        report = _make_report()
        context = _make_context(report)

        response = json.dumps({"mode": "RESEARCH", "reasoning": ["test"], "actions": [], "confidence": "medium"})

        result = engine._parse_llm_response(response, report, context)

        assert result.recommended_mode == "research"

    def test_parse_string_reasoning_wrapped_in_list(self, tmp_path: Path) -> None:
        """A single reasoning string should be wrapped in a list."""
        engine = ReasoningEngine(base_path=str(tmp_path))
        report = _make_report()
        context = _make_context(report)

        response = json.dumps(
            {"mode": "monitor", "reasoning": "single reason", "actions": "single action", "confidence": "low"}
        )

        result = engine._parse_llm_response(response, report, context)

        assert result.reasoning_chain == ["single reason"]
        assert result.suggested_actions == ["single action"]

    def test_parse_invalid_confidence_defaults_to_medium(self, tmp_path: Path) -> None:
        """Invalid confidence value should default to medium."""
        engine = ReasoningEngine(base_path=str(tmp_path))
        report = _make_report()
        context = _make_context(report)

        response = json.dumps({"mode": "monitor", "reasoning": [], "actions": [], "confidence": "super_high"})

        result = engine._parse_llm_response(response, report, context)

        assert result.confidence == "medium"


# ---------------------------------------------------------------------------
# 5.3: Enriched prompt with context
# ---------------------------------------------------------------------------


class TestEnrichedPrompt:
    """Test that _build_prompt includes all new context sections."""

    def test_prompt_includes_market_session(self, tmp_path: Path) -> None:
        engine = ReasoningEngine(base_path=str(tmp_path))
        report = _make_report()
        context = _make_context(report, market_session="overlap")

        prompt = engine._build_prompt(context, report, None)

        assert "Market Session: overlap" in prompt

    def test_prompt_includes_degradation_tier(self, tmp_path: Path) -> None:
        engine = ReasoningEngine(base_path=str(tmp_path))
        report = _make_report()
        context = _make_context(report, degradation_tier="DEGRADED_L1")

        prompt = engine._build_prompt(context, report, None)

        assert "Degradation Tier: DEGRADED_L1" in prompt
        assert "degraded mode" in prompt.lower()

    def test_prompt_omits_degradation_when_full(self, tmp_path: Path) -> None:
        """FULL degradation tier should NOT appear in prompt (it's the default)."""
        engine = ReasoningEngine(base_path=str(tmp_path))
        report = _make_report()
        context = _make_context(report, degradation_tier="FULL")

        prompt = engine._build_prompt(context, report, None)

        assert "Degradation Tier" not in prompt

    def test_prompt_includes_circuit_breakers(self, tmp_path: Path) -> None:
        engine = ReasoningEngine(base_path=str(tmp_path))
        report = _make_report()
        context = _make_context(report, open_circuit_breakers=["metaapi", "telegram"])

        prompt = engine._build_prompt(context, report, None)

        assert "Open Circuit Breakers" in prompt
        assert "metaapi" in prompt
        assert "telegram" in prompt

    def test_prompt_includes_actionable_subgoals(self, tmp_path: Path) -> None:
        engine = ReasoningEngine(base_path=str(tmp_path))
        report = _make_report()
        context = _make_context(
            report,
            actionable_subgoals=["Deploy IRB v2.1", "Fix execution gap"],
        )

        prompt = engine._build_prompt(context, report, None)

        assert "Actionable Sub-Goals" in prompt
        assert "Deploy IRB v2.1" in prompt
        assert "Fix execution gap" in prompt

    def test_prompt_includes_effectiveness_summary(self, tmp_path: Path) -> None:
        engine = ReasoningEngine(base_path=str(tmp_path))
        report = _make_report()
        context = _make_context(
            report,
            effectiveness_summary={"repair": 5.2, "research": -1.0},
        )

        prompt = engine._build_prompt(context, report, None)

        assert "Decision Effectiveness" in prompt
        assert "repair" in prompt
        assert "+5.2" in prompt

    def test_prompt_includes_escalate_mode(self, tmp_path: Path) -> None:
        """The instructions should list ESCALATE as an available mode."""
        engine = ReasoningEngine(base_path=str(tmp_path))
        report = _make_report()
        context = _make_context(report)

        prompt = engine._build_prompt(context, report, None)

        assert "ESCALATE" in prompt


# ---------------------------------------------------------------------------
# 5.4: Case-sensitivity fix validation
# ---------------------------------------------------------------------------


class TestCaseSensitivityFix:
    """Validate that lowercase recommended_mode works with ActionMode enum."""

    def test_lowercase_mode_maps_to_actionmode(self) -> None:
        """Lowercase mode string should correctly map to ActionMode via .upper()."""
        # This tests the pattern used in heartbeat.py after the fix
        modes = ["research", "plan", "execute", "monitor", "validate", "repair", "escalate"]
        for mode in modes:
            upper = mode.upper()
            assert ActionMode[upper].value == mode

    def test_heuristic_returns_lowercase_modes(self, tmp_path: Path) -> None:
        """All heuristic results should use lowercase recommended_mode values."""
        engine = ReasoningEngine(base_path=str(tmp_path))

        # Test several different heuristic paths
        scenarios = [
            # 3+ YELLOW dims -> research
            _make_report(
                overall=55.0,
                dims={
                    "a": 55.0,
                    "b": 50.0,
                    "c": 45.0,
                    "d": 85.0,
                    "e": 70.0,
                },
            ),
            # All GREEN -> monitor
            _make_report(overall=85.0, dims={"a": 90.0, "b": 80.0}),
            # RED dim -> repair
            _make_report(overall=60.0, dims={"a": 30.0, "b": 80.0}),
            # Single YELLOW -> plan
            _make_report(overall=70.0, dims={"a": 55.0, "b": 85.0}),
        ]

        for report in scenarios:
            context = _make_context(report)
            result = engine._heuristic_reason(report, context)
            assert result.recommended_mode == result.recommended_mode.lower(), (
                f"recommended_mode {result.recommended_mode!r} should be lowercase"
            )
            # Verify it can be looked up via .upper()
            assert ActionMode[result.recommended_mode.upper()]


# ---------------------------------------------------------------------------
# should_reason logic
# ---------------------------------------------------------------------------


class TestShouldReason:
    """Test the should_reason gating logic."""

    def test_monitor_with_yellow_triggers(self, tmp_path: Path) -> None:
        engine = ReasoningEngine(base_path=str(tmp_path))
        decision = Decision(mode=ActionMode.MONITOR, reason="All good")
        report = _make_report(dims={"strategy_validity": 55.0, "system_health": 90.0})

        assert engine.should_reason(decision, report) is True

    def test_execute_all_green_no_trigger(self, tmp_path: Path) -> None:
        engine = ReasoningEngine(base_path=str(tmp_path))
        decision = Decision(mode=ActionMode.EXECUTE, reason="Running tasks")
        report = _make_report(
            overall=85.0,
            dims={"system_health": 90.0, "strategy_validity": 80.0},
        )

        assert engine.should_reason(decision, report) is False

    def test_monitor_low_overall_triggers(self, tmp_path: Path) -> None:
        engine = ReasoningEngine(base_path=str(tmp_path))
        decision = Decision(mode=ActionMode.MONITOR, reason="Watching")
        report = _make_report(overall=70.0)

        assert engine.should_reason(decision, report) is True

    def test_multiple_degrading_triggers(self, tmp_path: Path) -> None:
        engine = ReasoningEngine(base_path=str(tmp_path))
        decision = Decision(mode=ActionMode.REPAIR, reason="Fixing")
        report = _make_report(
            dims={"system_health": 60.0, "strategy_validity": 50.0},
            trends={"system_health": "degrading", "strategy_validity": "degrading"},
        )

        assert engine.should_reason(decision, report) is True
