"""Tests for Phase 3 mission guardrails: RED-cap and confidence-aware scoring."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from novatrade.autonomy.progress_scorer import ProgressScorer
from novatrade.autonomy.schemas import (
    AlertLevel,
    DimensionScore,
    ProgressReport,
    ScoringConfig,
)


# =====================================================================
# Helper
# =====================================================================


def _make_scorer(tmp_path, config: ScoringConfig | None = None) -> ProgressScorer:
    """Create a ProgressScorer wired with controllable mock collectors."""
    return ProgressScorer(config=config, base_path=str(tmp_path))


def _wire_collectors(scorer: ProgressScorer, scores: dict[str, tuple[float, float]]) -> None:
    """Wire mock collectors that return (score, confidence) per dimension.

    ``scores`` maps dimension name -> (score, confidence).
    """
    for name, coll in scorer._collectors.items():
        s, c = scores.get(name, (50.0, 1.0))

        async def make_collect(score=s, conf=c):
            return DimensionScore(name="test", score=score, confidence=conf)

        coll.collect = make_collect


# =====================================================================
# RED-cap tests
# =====================================================================


@pytest.mark.asyncio
async def test_red_cap_strategy_validity_red(tmp_path):
    """strategy_validity at 20 (RED) caps overall at 45 regardless of other scores."""
    scorer = _make_scorer(tmp_path)
    _wire_collectors(scorer, {
        "system_health": (100.0, 1.0),
        "execution_pipeline": (100.0, 1.0),
        "strategy_validity": (20.0, 1.0),  # RED
        "risk_engine": (100.0, 1.0),
        "performance_stability": (100.0, 1.0),
    })

    report = await scorer.score()
    assert report.overall_score == 45.0


@pytest.mark.asyncio
async def test_red_cap_execution_pipeline_red(tmp_path):
    """execution_pipeline at 30 (RED) caps overall at 45."""
    scorer = _make_scorer(tmp_path)
    _wire_collectors(scorer, {
        "system_health": (100.0, 1.0),
        "execution_pipeline": (30.0, 1.0),  # RED
        "strategy_validity": (100.0, 1.0),
        "risk_engine": (100.0, 1.0),
        "performance_stability": (100.0, 1.0),
    })

    report = await scorer.score()
    assert report.overall_score == 45.0


@pytest.mark.asyncio
async def test_no_red_cap_when_non_critical_red(tmp_path):
    """risk_engine at 20 (RED) does NOT cap overall — it is not mission-critical."""
    scorer = _make_scorer(tmp_path)
    _wire_collectors(scorer, {
        "system_health": (100.0, 1.0),
        "execution_pipeline": (100.0, 1.0),
        "strategy_validity": (100.0, 1.0),
        "risk_engine": (20.0, 1.0),  # RED but not mission-critical
        "performance_stability": (100.0, 1.0),
    })

    report = await scorer.score()
    # Weighted: 100*0.2 + 100*0.25 + 100*0.25 + 20*0.2 + 100*0.1 = 20+25+25+4+10 = 84
    assert report.overall_score == 84.0
    assert report.overall_alert == AlertLevel.GREEN


@pytest.mark.asyncio
async def test_red_cap_warning_added(tmp_path):
    """When RED-cap triggers, a descriptive warning is appended."""
    scorer = _make_scorer(tmp_path)
    _wire_collectors(scorer, {
        "system_health": (100.0, 1.0),
        "execution_pipeline": (100.0, 1.0),
        "strategy_validity": (10.0, 1.0),  # RED
        "risk_engine": (100.0, 1.0),
        "performance_stability": (100.0, 1.0),
    })

    report = await scorer.score()
    red_cap_warnings = [w for w in report.warnings if "capped" in w.lower()]
    assert len(red_cap_warnings) == 1
    assert "strategy_validity" in red_cap_warnings[0]
    assert "RED" in red_cap_warnings[0]


@pytest.mark.asyncio
async def test_red_cap_does_not_inflate(tmp_path):
    """If overall is already below red_cap_overall, RED-cap does not raise it."""
    scorer = _make_scorer(tmp_path)
    _wire_collectors(scorer, {
        "system_health": (10.0, 1.0),
        "execution_pipeline": (10.0, 1.0),  # RED + mission-critical
        "strategy_validity": (10.0, 1.0),   # RED + mission-critical
        "risk_engine": (10.0, 1.0),
        "performance_stability": (10.0, 1.0),
    })

    report = await scorer.score()
    # Overall = 10.0 already below 45 -> cap doesn't inflate
    assert report.overall_score == 10.0
    # No "capped" warning should appear
    red_cap_warnings = [w for w in report.warnings if "capped" in w.lower()]
    assert len(red_cap_warnings) == 0


@pytest.mark.asyncio
async def test_red_cap_custom_threshold(tmp_path):
    """Custom red_cap_overall value is respected."""
    config = ScoringConfig(red_cap_overall=30.0)
    scorer = _make_scorer(tmp_path, config=config)
    _wire_collectors(scorer, {
        "system_health": (100.0, 1.0),
        "execution_pipeline": (100.0, 1.0),
        "strategy_validity": (20.0, 1.0),  # RED
        "risk_engine": (100.0, 1.0),
        "performance_stability": (100.0, 1.0),
    })

    report = await scorer.score()
    assert report.overall_score == 30.0


# =====================================================================
# Confidence-aware scoring tests
# =====================================================================


def test_confidence_default_is_one():
    """DimensionScore() without confidence has confidence=1.0."""
    dim = DimensionScore(name="test", score=80.0)
    assert dim.confidence == 1.0


def test_confidence_field_validation():
    """Confidence must be between 0.0 and 1.0."""
    with pytest.raises(ValidationError):
        DimensionScore(name="bad", score=50.0, confidence=1.5)
    with pytest.raises(ValidationError):
        DimensionScore(name="bad", score=50.0, confidence=-0.1)


@pytest.mark.asyncio
async def test_low_confidence_capped_at_50(tmp_path):
    """Dimension with confidence=0.3 and score=90 contributes max 50 to weighted average."""
    config = ScoringConfig(
        weights={
            "system_health": 1.0,
            "execution_pipeline": 0.0,
            "strategy_validity": 0.0,
            "risk_engine": 0.0,
            "performance_stability": 0.0,
        },
        mission_critical=[],  # disable RED-cap for this test
    )
    scorer = _make_scorer(tmp_path, config=config)
    _wire_collectors(scorer, {
        "system_health": (90.0, 0.3),  # low confidence -> effective score capped at 50
        "execution_pipeline": (50.0, 1.0),
        "strategy_validity": (50.0, 1.0),
        "risk_engine": (50.0, 1.0),
        "performance_stability": (50.0, 1.0),
    })

    report = await scorer.score()
    # system_health: effective_score = min(90, 50) = 50, weight 1.0
    # all others weight 0 -> overall = 50 / 1.0 = 50.0
    assert report.overall_score == 50.0


@pytest.mark.asyncio
async def test_high_confidence_uses_real_score(tmp_path):
    """Dimension with confidence=0.8 and score=90 contributes full 90."""
    config = ScoringConfig(
        weights={
            "system_health": 1.0,
            "execution_pipeline": 0.0,
            "strategy_validity": 0.0,
            "risk_engine": 0.0,
            "performance_stability": 0.0,
        },
        mission_critical=[],  # disable RED-cap for this test
    )
    scorer = _make_scorer(tmp_path, config=config)
    _wire_collectors(scorer, {
        "system_health": (90.0, 0.8),  # high confidence -> real score
        "execution_pipeline": (50.0, 1.0),
        "strategy_validity": (50.0, 1.0),
        "risk_engine": (50.0, 1.0),
        "performance_stability": (50.0, 1.0),
    })

    report = await scorer.score()
    assert report.overall_score == 90.0


@pytest.mark.asyncio
async def test_low_confidence_warning(tmp_path):
    """Low-confidence dimensions generate a warning message."""
    scorer = _make_scorer(tmp_path)
    _wire_collectors(scorer, {
        "system_health": (80.0, 0.3),  # low confidence
        "execution_pipeline": (80.0, 1.0),
        "strategy_validity": (80.0, 1.0),
        "risk_engine": (80.0, 1.0),
        "performance_stability": (80.0, 1.0),
    })

    report = await scorer.score()
    confidence_warnings = [w for w in report.warnings if "low confidence" in w.lower()]
    assert len(confidence_warnings) == 1
    assert "system_health" in confidence_warnings[0]
    assert "0.3" in confidence_warnings[0]


@pytest.mark.asyncio
async def test_confidence_at_boundary(tmp_path):
    """Confidence exactly 0.5 uses real score (boundary condition)."""
    config = ScoringConfig(
        weights={
            "system_health": 1.0,
            "execution_pipeline": 0.0,
            "strategy_validity": 0.0,
            "risk_engine": 0.0,
            "performance_stability": 0.0,
        },
        mission_critical=[],
    )
    scorer = _make_scorer(tmp_path, config=config)
    _wire_collectors(scorer, {
        "system_health": (90.0, 0.5),  # exactly 0.5 -> should use real score
        "execution_pipeline": (50.0, 1.0),
        "strategy_validity": (50.0, 1.0),
        "risk_engine": (50.0, 1.0),
        "performance_stability": (50.0, 1.0),
    })

    report = await scorer.score()
    assert report.overall_score == 90.0
    # No low-confidence warning for 0.5
    confidence_warnings = [w for w in report.warnings if "low confidence" in w.lower()]
    assert len(confidence_warnings) == 0


@pytest.mark.asyncio
async def test_low_confidence_low_score_not_capped(tmp_path):
    """Low-confidence dim with score below 50 uses actual score (cap only prevents inflation)."""
    config = ScoringConfig(
        weights={
            "system_health": 1.0,
            "execution_pipeline": 0.0,
            "strategy_validity": 0.0,
            "risk_engine": 0.0,
            "performance_stability": 0.0,
        },
        mission_critical=[],
    )
    scorer = _make_scorer(tmp_path, config=config)
    _wire_collectors(scorer, {
        "system_health": (30.0, 0.2),  # low confidence, score 30 < 50 -> min(30, 50) = 30
        "execution_pipeline": (50.0, 1.0),
        "strategy_validity": (50.0, 1.0),
        "risk_engine": (50.0, 1.0),
        "performance_stability": (50.0, 1.0),
    })

    report = await scorer.score()
    assert report.overall_score == 30.0


@pytest.mark.asyncio
async def test_red_cap_and_confidence_interact(tmp_path):
    """RED-cap and confidence interact correctly — both guardrails can fire."""
    scorer = _make_scorer(tmp_path)
    _wire_collectors(scorer, {
        "system_health": (90.0, 0.3),  # low confidence -> effective = 50
        "execution_pipeline": (20.0, 1.0),  # RED + mission-critical
        "strategy_validity": (80.0, 1.0),
        "risk_engine": (80.0, 1.0),
        "performance_stability": (80.0, 1.0),
    })

    report = await scorer.score()
    # RED-cap triggers because execution_pipeline is RED
    assert report.overall_score == 45.0
    # Both warnings should be present
    red_warnings = [w for w in report.warnings if "capped" in w.lower()]
    conf_warnings = [w for w in report.warnings if "low confidence" in w.lower()]
    assert len(red_warnings) == 1
    assert len(conf_warnings) == 1


# =====================================================================
# ScoringConfig schema tests
# =====================================================================


def test_scoring_config_defaults():
    """ScoringConfig has sensible defaults for mission_critical and red_cap_overall."""
    config = ScoringConfig()
    assert "execution_pipeline" in config.mission_critical
    assert "strategy_validity" in config.mission_critical
    assert config.red_cap_overall == 45.0


def test_scoring_config_custom_mission_critical():
    """mission_critical can be customized."""
    config = ScoringConfig(mission_critical=["system_health"])
    assert config.mission_critical == ["system_health"]
    assert "execution_pipeline" not in config.mission_critical


def test_scoring_config_empty_mission_critical():
    """Empty mission_critical disables RED-cap entirely."""
    config = ScoringConfig(mission_critical=[])
    assert config.mission_critical == []
