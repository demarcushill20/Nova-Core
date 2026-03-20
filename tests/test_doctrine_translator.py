"""Tests for novatrade.doctrine.translator — concept-to-doctrine translation."""

from __future__ import annotations

import pytest

from novatrade.doctrine.translator import (
    concept_to_doctrine,
    detect_strategy_family,
)

# ---------------------------------------------------------------------------
# Family detection
# ---------------------------------------------------------------------------


class TestFamilyDetection:
    @pytest.mark.parametrize(
        "concept,expected",
        [
            ("IRB reversal on H1 with trend filter", "irb"),
            ("institutional rejection block entry", "irb"),
            ("inside bar breakout with volume", "irb"),
            ("Enter on imbalance recovery zone", "irb"),
        ],
    )
    def test_irb_detection(self, concept: str, expected: str):
        assert detect_strategy_family(concept) == expected

    @pytest.mark.parametrize(
        "concept,expected",
        [
            ("Range breakout above resistance", "breakout"),
            ("Channel break with volume confirmation", "breakout"),
            ("Consolidation break entry", "breakout"),
            ("Support resistance break trading", "breakout"),
        ],
    )
    def test_breakout_detection(self, concept: str, expected: str):
        assert detect_strategy_family(concept) == expected

    @pytest.mark.parametrize(
        "concept,expected",
        [
            ("Mean reversion with Bollinger Bands", "mean_reversion"),
            ("RSI reversal at oversold levels", "mean_reversion"),
            ("Revert to mean on H4", "mean_reversion"),
            ("Overbought sell signal", "mean_reversion"),
        ],
    )
    def test_mean_reversion_detection(self, concept: str, expected: str):
        assert detect_strategy_family(concept) == expected

    @pytest.mark.parametrize(
        "concept,expected",
        [
            ("Trend following with EMA crossover", "trend_following"),
            ("Moving average cross entry", "trend_following"),
            ("MACD signal line crossover", "trend_following"),
            ("Momentum pullback entry with trailing stop", "trend_following"),
        ],
    )
    def test_trend_detection(self, concept: str, expected: str):
        assert detect_strategy_family(concept) == expected

    def test_unknown_concept(self):
        assert detect_strategy_family("buy when moon is full") == "unknown"

    def test_empty_concept(self):
        assert detect_strategy_family("") == "unknown"


# ---------------------------------------------------------------------------
# Concept to doctrine translation
# ---------------------------------------------------------------------------


class TestConceptToDoctrine:
    def test_irb_concept(self):
        d = concept_to_doctrine(
            "IRB reversal: enter on rejection block when H4 trend aligns",
            pair="EURUSD",
            timeframe="H1",
        )
        assert d.setup_type == "reversal"
        assert d.entry_signal == "irb_rejection"
        assert d.data.pair == "EURUSD"
        assert "H1" in d.data.timeframes
        assert "H4" in d.data.timeframes
        assert "irb_threshold" in d.mutable_params
        assert "ema_period" in d.mutable_params

    def test_breakout_concept(self):
        d = concept_to_doctrine(
            "Range breakout with volume confirmation",
            pair="GBPUSD",
            timeframe="H4",
        )
        assert d.setup_type == "breakout"
        assert d.data.pair == "GBPUSD"
        assert "lookback_period" in d.mutable_params

    def test_mean_reversion_concept(self):
        d = concept_to_doctrine(
            "Bollinger Band mean reversion on M15",
            pair="USDJPY",
            timeframe="M15",
        )
        assert d.setup_type == "mean_reversion"
        assert d.data.pair == "USDJPY"
        assert "bb_period" in d.mutable_params
        assert "rsi_period" in d.mutable_params

    def test_trend_concept(self):
        d = concept_to_doctrine(
            "Trend following EMA crossover with pullback",
            pair="AUDUSD",
            timeframe="D1",
        )
        assert d.setup_type == "trend_following"
        assert "fast_ema" in d.mutable_params
        assert "slow_ema" in d.mutable_params

    def test_unknown_concept_returns_skeleton(self):
        d = concept_to_doctrine(
            "Buy when stars align and sell at dawn",
            pair="NZDUSD",
            timeframe="H1",
        )
        assert "[SKELETON]" in d.description
        assert d.entry_signal == "custom"
        # Skeleton still has basic params
        assert "atr_period" in d.mutable_params
        assert "trail_atr_multiplier" in d.mutable_params

    def test_all_doctrines_have_immutable_risk(self):
        """All families include immutable risk params."""
        for concept in [
            "IRB reversal",
            "Range breakout",
            "Bollinger mean reversion",
            "EMA cross trend following",
            "unknown strategy xyz",
        ]:
            d = concept_to_doctrine(concept)
            assert "risk_per_trade_pct" in d.immutable_params
            assert "max_concurrent_trades" in d.immutable_params

    def test_custom_author(self):
        d = concept_to_doctrine("IRB reversal", author="test_user")
        assert d.author == "test_user"

    def test_timeframe_inference_h1(self):
        d = concept_to_doctrine("IRB reversal", timeframe="H1")
        assert d.data.timeframes == ["H1", "H4"]

    def test_timeframe_inference_m15(self):
        d = concept_to_doctrine("IRB reversal", timeframe="M15")
        assert d.data.timeframes == ["M15", "H1"]

    def test_timeframe_inference_d1(self):
        d = concept_to_doctrine("EMA crossover trend", timeframe="D1")
        assert d.data.timeframes == ["D1", "W1"]

    def test_case_insensitive_detection(self):
        d = concept_to_doctrine("irb REJECTION BLOCK entry")
        assert d.setup_type == "reversal"

    def test_doctrine_is_valid(self):
        """All translated doctrines must pass Pydantic validation."""
        for concept in [
            "IRB reversal on H1",
            "Range breakout above resistance",
            "Bollinger Band mean reversion",
            "Trend following with EMA cross",
            "some unknown strategy",
        ]:
            d = concept_to_doctrine(concept)
            assert d.name  # has a name
            assert d.concept  # has concept text
            assert d.data.pair  # has pair
            assert len(d.data.timeframes) >= 1  # has timeframes
