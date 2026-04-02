"""Tests for novatrade.backtest.environment."""

import pytest

from novatrade.backtest.environment import (
    DEFAULT_ENVIRONMENT,
    BacktestEnvironment,
    EngineType,
    FillModel,
    PendingOrderPolicy,
    SpreadAssumptions,
    TrailingStopSimulation,
)


class TestBacktestEnvironment:
    """Test environment configuration and defaults."""

    def test_default_environment_exists(self):
        env = DEFAULT_ENVIRONMENT
        assert env is not None
        assert env.engine_type == EngineType.PYTHON_REPRODUCTION

    def test_default_symbol(self):
        env = DEFAULT_ENVIRONMENT
        assert env.symbol_display == "EURUSD"
        assert env.symbol_broker == "EURUSD.sim"
        assert env.digits == 5
        assert env.pip_value == 0.0001

    def test_default_timeframes(self):
        env = DEFAULT_ENVIRONMENT
        assert env.primary_timeframe == "H1"
        assert env.higher_timeframe == "H4"
        assert env.h1_bars_per_day == 24

    def test_default_strategy_params(self):
        env = DEFAULT_ENVIRONMENT
        assert env.irb_threshold == 0.45
        assert env.ema_period == 20
        assert env.atr_period == 14
        assert env.adx_period == 14
        assert env.trend_slope_threshold == 0.4
        assert env.adx_threshold == 25.0
        assert env.overextension_threshold == 2.0
        assert env.trigger_window_bars == 20
        assert env.time_stop_bars == 40
        assert env.trail_atr_multiplier == 1.5
        assert env.warmup_bars == 34

    def test_default_position_sizing(self):
        env = DEFAULT_ENVIRONMENT
        assert env.initial_equity == 100_000.0
        assert env.risk_fraction == 0.01
        assert env.min_volume == 0.01
        assert env.max_volume == 1.00

    def test_to_dict_contains_all_keys(self):
        d = DEFAULT_ENVIRONMENT.to_dict()
        expected_keys = [
            "engine_type",
            "symbol",
            "symbol_broker",
            "primary_timeframe",
            "higher_timeframe",
            "timezone",
            "fill_model",
            "initial_equity",
            "risk_fraction",
            "warmup_bars",
            "irb_threshold",
            "directly_measured",
            "inferred_or_estimated",
            "not_measured",
        ]
        for key in expected_keys:
            assert key in d, f"Missing key: {key}"

    def test_custom_environment(self):
        env = BacktestEnvironment(
            initial_equity=50_000.0,
            risk_fraction=0.02,
            irb_threshold=0.50,
        )
        assert env.initial_equity == 50_000.0
        assert env.risk_fraction == 0.02
        assert env.irb_threshold == 0.50

    def test_environment_is_frozen(self):
        env = DEFAULT_ENVIRONMENT
        with pytest.raises(AttributeError):
            env.initial_equity = 200_000  # type: ignore[misc]


class TestSpreadAssumptions:
    def test_total_cost_pips_default(self):
        s = SpreadAssumptions()
        # avg_spread=1.0 + 2*slippage=2*0.2 = 1.4
        assert s.total_cost_pips == pytest.approx(1.4)

    def test_total_cost_pips_with_slippage(self):
        s = SpreadAssumptions(avg_spread_pips=1.5, slippage_pips=0.5)
        assert s.total_cost_pips == 2.5

    def test_fixed_spread_overrides_avg(self):
        s = SpreadAssumptions(fixed_spread_pips=2.0, avg_spread_pips=1.0, slippage_pips=0.0)
        assert s.total_cost_pips == 2.0


class TestPendingOrderPolicy:
    def test_defaults(self):
        p = PendingOrderPolicy()
        assert p.max_bars == 20
        assert p.enforcement == "hard_cancel"


class TestTrailingStopSimulation:
    def test_defaults(self):
        t = TrailingStopSimulation()
        assert t.atr_multiplier == 1.5
        assert t.evaluated_on_bar_close is True
        assert t.only_tightens is True


class TestEngineType:
    def test_values(self):
        assert EngineType.PYTHON_REPRODUCTION.value == "python_reproduction"
        assert EngineType.TRADINGVIEW_NATIVE.value == "tradingview_native"


class TestFillModel:
    def test_values(self):
        assert FillModel.NEXT_BAR_OHLC.value == "next_bar_ohlc"
        assert FillModel.INTRA_BAR_STOP.value == "intra_bar_stop"
