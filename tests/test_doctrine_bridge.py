"""Tests for novatrade.doctrine.bridge — doctrine to StrategyConfig conversion."""

from __future__ import annotations

import pytest

from novatrade.cli.config_schema import StrategyConfig
from novatrade.doctrine.bridge import (
    config_to_doctrine_params,
    doctrine_to_config,
    validate_doctrine_config_compatibility,
)
from novatrade.doctrine.schema import (
    DoctrineData,
    ParamBound,
    StrategyDoctrine,
)
from novatrade.doctrine.translator import concept_to_doctrine


def _irb_doctrine(**kw) -> StrategyDoctrine:
    """Build a standard IRB doctrine for testing."""
    defaults = dict(
        name="IRB Test",
        concept="IRB reversal on H1",
        data=DoctrineData(pair="EURUSD"),
        mutable_params={
            "irb_threshold": ParamBound(default=0.45, min=0.30, max=0.60, step=0.01),
            "ema_period": ParamBound(default=20, min=10, max=50, step=1),
            "atr_period": ParamBound(default=14, min=7, max=21, step=1),
            "adx_period": ParamBound(default=14, min=7, max=21, step=1),
            "trend_slope_threshold": ParamBound(default=0.4, min=0.1, max=1.0, step=0.05),
            "adx_threshold": ParamBound(default=20.0, min=15.0, max=30.0, step=0.5),
            "overextension_threshold": ParamBound(default=2.0, min=1.5, max=3.0, step=0.1),
            "trail_atr_multiplier": ParamBound(default=1.5, min=1.0, max=3.0, step=0.1),
            "trigger_window_bars": ParamBound(default=20, min=10, max=40, step=1),
            "time_stop_bars": ParamBound(default=40, min=20, max=80, step=1),
            "mtf_lookback": ParamBound(default=5, min=1, max=20, step=1),
            "warmup_bars": ParamBound(default=34, min=20, max=60, step=1),
        },
        filters_mandatory=["trend_alignment"],
    )
    defaults.update(kw)
    return StrategyDoctrine(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# doctrine_to_config tests
# ---------------------------------------------------------------------------


class TestDoctrineToConfig:
    def test_basic_conversion(self):
        doctrine = _irb_doctrine()
        config = doctrine_to_config(doctrine)
        assert isinstance(config, StrategyConfig)
        assert config.irb_threshold == 0.45
        assert config.ema_period == 20
        assert config.atr_period == 14

    def test_metadata_mapped(self):
        doctrine = _irb_doctrine()
        config = doctrine_to_config(doctrine)
        assert config.name == "IRB Test"
        assert config.version == "1.0.0"

    def test_filters_mapped(self):
        doctrine = _irb_doctrine()
        config = doctrine_to_config(doctrine)
        assert "trend_alignment" in config.filters_enabled

    def test_overrides_applied(self):
        doctrine = _irb_doctrine()
        config = doctrine_to_config(doctrine, overrides={"irb_threshold": 0.50})
        assert config.irb_threshold == 0.50

    def test_override_respects_bounds(self):
        """Override that violates StrategyConfig bounds should fail."""
        doctrine = _irb_doctrine()
        with pytest.raises(Exception):  # noqa: B017
            doctrine_to_config(doctrine, overrides={"irb_threshold": 0.99})

    def test_extra_params_ignored(self):
        """Doctrine params not in StrategyConfig are silently ignored."""
        doctrine = _irb_doctrine(
            mutable_params={
                "irb_threshold": ParamBound(default=0.45, min=0.30, max=0.60),
                "custom_unknown_param": ParamBound(default=42, min=0, max=100),
            }
        )
        config = doctrine_to_config(doctrine)
        assert config.irb_threshold == 0.45
        assert not hasattr(config, "custom_unknown_param")

    def test_round_trip_config_values(self):
        """Config values should match doctrine defaults for known params."""
        doctrine = _irb_doctrine()
        config = doctrine_to_config(doctrine)
        for name, bound in doctrine.mutable_params.items():
            if hasattr(config, name):
                assert getattr(config, name) == bound.default, (
                    f"{name}: expected {bound.default}, got {getattr(config, name)}"
                )


# ---------------------------------------------------------------------------
# config_to_doctrine_params tests
# ---------------------------------------------------------------------------


class TestConfigToDoctrineParams:
    def test_extracts_all_optimizable(self):
        config = StrategyConfig()
        params = config_to_doctrine_params(config)
        for name in StrategyConfig.OPTIMIZABLE_PARAMS:
            assert name in params

    def test_values_match(self):
        config = StrategyConfig(irb_threshold=0.55, ema_period=30)
        params = config_to_doctrine_params(config)
        assert params["irb_threshold"] == 0.55
        assert params["ema_period"] == 30


# ---------------------------------------------------------------------------
# Compatibility validation tests
# ---------------------------------------------------------------------------


class TestCompatibilityValidation:
    def test_compatible_doctrine(self):
        doctrine = _irb_doctrine()
        issues = validate_doctrine_config_compatibility(doctrine)
        assert issues == []

    def test_default_below_config_min(self):
        doctrine = _irb_doctrine(
            mutable_params={
                "irb_threshold": ParamBound(default=0.10, min=0.05, max=0.60),
            }
        )
        issues = validate_doctrine_config_compatibility(doctrine)
        assert any("irb_threshold" in i and "min" in i for i in issues)

    def test_default_above_config_max(self):
        doctrine = _irb_doctrine(
            mutable_params={
                "irb_threshold": ParamBound(default=0.90, min=0.30, max=0.95),
            }
        )
        issues = validate_doctrine_config_compatibility(doctrine)
        assert any("irb_threshold" in i and "max" in i for i in issues)


# ---------------------------------------------------------------------------
# End-to-end: concept → doctrine → config
# ---------------------------------------------------------------------------


class TestEndToEnd:
    def test_irb_concept_to_config(self):
        doctrine = concept_to_doctrine(
            "IRB reversal on H1 with trend filter",
            pair="EURUSD",
            timeframe="H1",
        )
        config = doctrine_to_config(doctrine)
        assert isinstance(config, StrategyConfig)
        assert config.irb_threshold == 0.45  # IRB family default

    def test_breakout_concept_produces_valid_config(self):
        """Breakout doctrine has non-IRB params; config should still validate."""
        doctrine = concept_to_doctrine("Range breakout strategy", timeframe="H4")
        # Breakout params differ from IRB — config gets what it can
        config = doctrine_to_config(doctrine)
        assert isinstance(config, StrategyConfig)

    def test_unknown_concept_produces_valid_config(self):
        doctrine = concept_to_doctrine("some mystery strategy")
        config = doctrine_to_config(doctrine)
        assert isinstance(config, StrategyConfig)

    def test_all_families_produce_valid_configs(self):
        """Every strategy family must produce a valid StrategyConfig."""
        concepts = [
            "IRB reversal",
            "Range breakout",
            "Bollinger mean reversion",
            "Trend following EMA cross",
            "unknown mystery concept",
        ]
        for concept in concepts:
            doctrine = concept_to_doctrine(concept)
            config = doctrine_to_config(doctrine)
            assert isinstance(config, StrategyConfig), f"Failed for: {concept}"
