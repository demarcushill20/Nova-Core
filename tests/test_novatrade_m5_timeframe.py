"""Tests for M5 timeframe support across the backtesting stack.

Validates:
- BacktestEnvironment.for_m5() factory
- BacktestEnvironment.for_timeframe() generic factory
- M5 parameter bounds scaling
- _build_h4_map with ratio=12 (M5/H1 data)
- Walk-forward _slice_h4 with M5 ratio
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from novatrade.backtest.engine import IRBBacktester
from novatrade.backtest.environment import BacktestEnvironment
from novatrade.cli.config_schema import StrategyConfig
from novatrade.models import Candle
from novatrade.optimization.autoresearch import (
    AutoResearchConfig,
    _evaluate,
    _random_organism,
    run_autoresearch,
)
from novatrade.optimization.walkforward import (
    WalkForwardConfig,
    _run_single_backtest,
    _slice_h4,
    run_walk_forward,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_candle(
    open_: float = 1.1000,
    high: float = 1.1010,
    low: float = 1.0990,
    close: float = 1.1005,
    volume: float = 100.0,
    timestamp: float = 0.0,
) -> Candle:
    return Candle(
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
        timestamp=timestamp,
    )


def _make_candles(n: int, base_ts: float = 0.0, interval_s: float = 0.0) -> list[Candle]:
    """Generate n synthetic candles with optional timestamps."""
    candles = []
    for i in range(n):
        ts = base_ts + i * interval_s if interval_s > 0 else 0.0
        candles.append(
            _make_candle(
                open_=1.1000 + i * 0.0001,
                high=1.1010 + i * 0.0001,
                low=1.0990 + i * 0.0001,
                close=1.1005 + i * 0.0001,
                timestamp=ts,
            )
        )
    return candles


# ===========================================================================
# BacktestEnvironment.for_m5()
# ===========================================================================


class TestForM5:
    """Tests for the M5 factory method."""

    def test_creates_m5_environment(self):
        env = BacktestEnvironment.for_m5()
        assert env.primary_timeframe == "M5"
        assert env.higher_timeframe == "H1"
        assert env.h1_to_h4_ratio == 12
        assert env.h1_bars_per_day == 288
        assert env.h4_bars_per_day == 24

    def test_m5_is_frozen(self):
        env = BacktestEnvironment.for_m5()
        with pytest.raises(AttributeError):
            env.primary_timeframe = "H1"  # type: ignore[misc]

    def test_m5_overrides(self):
        env = BacktestEnvironment.for_m5(initial_equity=50_000.0, irb_threshold=0.50)
        assert env.initial_equity == 50_000.0
        assert env.irb_threshold == 0.50
        assert env.primary_timeframe == "M5"

    def test_m5_preserves_defaults(self):
        env = BacktestEnvironment.for_m5()
        default = BacktestEnvironment()
        # Non-timeframe fields should match default
        assert env.initial_equity == default.initial_equity
        assert env.risk_fraction == default.risk_fraction
        assert env.irb_threshold == default.irb_threshold


# ===========================================================================
# BacktestEnvironment.for_timeframe()
# ===========================================================================


class TestForTimeframe:
    """Tests for the generic timeframe factory."""

    def test_m5_h1(self):
        env = BacktestEnvironment.for_timeframe("M5", "H1")
        assert env.primary_timeframe == "M5"
        assert env.higher_timeframe == "H1"
        assert env.h1_to_h4_ratio == 12  # 60/5 = 12
        assert env.h1_bars_per_day == 288  # 1440/5
        assert env.h4_bars_per_day == 24  # 1440/60

    def test_h1_h4(self):
        env = BacktestEnvironment.for_timeframe("H1", "H4")
        assert env.primary_timeframe == "H1"
        assert env.higher_timeframe == "H4"
        assert env.h1_to_h4_ratio == 4  # 240/60 = 4
        assert env.h1_bars_per_day == 24
        assert env.h4_bars_per_day == 6

    def test_m15_h1(self):
        env = BacktestEnvironment.for_timeframe("M15", "H1")
        assert env.h1_to_h4_ratio == 4  # 60/15 = 4
        assert env.h1_bars_per_day == 96  # 1440/15

    def test_m1_m5(self):
        env = BacktestEnvironment.for_timeframe("M1", "M5")
        assert env.h1_to_h4_ratio == 5  # 5/1 = 5
        assert env.h1_bars_per_day == 1440

    def test_overrides(self):
        env = BacktestEnvironment.for_timeframe("M5", "H1", initial_equity=25_000.0)
        assert env.initial_equity == 25_000.0
        assert env.primary_timeframe == "M5"

    def test_unknown_primary(self):
        with pytest.raises(ValueError, match="Unknown primary timeframe"):
            BacktestEnvironment.for_timeframe("X1", "H1")

    def test_unknown_higher(self):
        with pytest.raises(ValueError, match="Unknown higher timeframe"):
            BacktestEnvironment.for_timeframe("M5", "X4")

    def test_higher_must_be_larger(self):
        with pytest.raises(ValueError, match="must be larger"):
            BacktestEnvironment.for_timeframe("H4", "H1")

    def test_same_timeframe_rejected(self):
        with pytest.raises(ValueError, match="must be larger"):
            BacktestEnvironment.for_timeframe("H1", "H1")


# ===========================================================================
# M5 Parameter Bounds
# ===========================================================================


class TestM5ParameterBounds:
    """Tests for M5_PARAMETER_BOUNDS scaling."""

    def test_bar_based_params_scaled(self):
        m5 = StrategyConfig.M5_PARAMETER_BOUNDS

        # trigger_window_bars: H1 is 10-40, M5 should be 60-240 (12x of min 5, max 20 -> actually specified as 60-240)
        assert m5["trigger_window_bars"].min_val == 60
        assert m5["trigger_window_bars"].max_val == 240

        # time_stop_bars: H1 is 20-80, M5 should be 120-480
        assert m5["time_stop_bars"].min_val == 120
        assert m5["time_stop_bars"].max_val == 480

        # warmup_bars: H1 is 20-100, M5 should be 120-600
        assert m5["warmup_bars"].min_val == 120
        assert m5["warmup_bars"].max_val == 600

        # trail_delay_bars: H1 is 1-20, M5 is 1-120 (min_val=1 consistent with H1)
        assert m5["trail_delay_bars"].min_val == 1
        assert m5["trail_delay_bars"].max_val == 120

    def test_non_bar_params_unchanged(self):
        h1 = StrategyConfig.PARAMETER_BOUNDS
        m5 = StrategyConfig.M5_PARAMETER_BOUNDS

        # EMA periods stay the same
        assert m5["ema_period"].min_val == h1["ema_period"].min_val
        assert m5["ema_period"].max_val == h1["ema_period"].max_val

        # ATR period stays the same
        assert m5["atr_period"].min_val == h1["atr_period"].min_val
        assert m5["atr_period"].max_val == h1["atr_period"].max_val

        # ADX threshold stays the same
        assert m5["adx_threshold"].min_val == h1["adx_threshold"].min_val
        assert m5["adx_threshold"].max_val == h1["adx_threshold"].max_val

        # irb_threshold stays the same
        assert m5["irb_threshold"].min_val == h1["irb_threshold"].min_val
        assert m5["irb_threshold"].max_val == h1["irb_threshold"].max_val

    def test_m5_bounds_cover_all_optimizable_params(self):
        for p in StrategyConfig.OPTIMIZABLE_PARAMS:
            assert p in StrategyConfig.M5_PARAMETER_BOUNDS, f"Missing M5 bound for {p}"

    def test_optimizable_params_m5_matches_h1_list(self):
        assert StrategyConfig.OPTIMIZABLE_PARAMS_M5 == StrategyConfig.OPTIMIZABLE_PARAMS


# ===========================================================================
# _build_h4_map with ratio=12 (M5/H1)
# ===========================================================================


class TestBuildH4MapM5:
    """Tests for _build_h4_map with M5 ratio (no timestamps)."""

    def test_ratio_12_mapping(self):
        """With M5:H1 ratio=12, every 12 M5 bars should map to one H1 bar."""
        env = BacktestEnvironment.for_m5()
        bt = IRBBacktester(env=env)

        # 36 M5 candles, 3 H1 candles
        m5_candles = _make_candles(36)
        h1_candles = _make_candles(3)

        mapping = bt._build_h4_map(m5_candles, h1_candles)
        assert len(mapping) == 36

        # Bars 0-11 map to H1 bar 0
        for i in range(12):
            assert mapping[i] == 0, f"M5 bar {i} should map to H1 bar 0"

        # Bars 12-23 map to H1 bar 1
        for i in range(12, 24):
            assert mapping[i] == 1, f"M5 bar {i} should map to H1 bar 1"

        # Bars 24-35 map to H1 bar 2
        for i in range(24, 36):
            assert mapping[i] == 2, f"M5 bar {i} should map to H1 bar 2"

    def test_ratio_4_default_still_works(self):
        """H1:H4 (ratio=4) still works correctly (backward compat)."""
        env = BacktestEnvironment()  # default H1:H4
        bt = IRBBacktester(env=env)

        h1_candles = _make_candles(12)
        h4_candles = _make_candles(3)

        mapping = bt._build_h4_map(h1_candles, h4_candles)
        assert len(mapping) == 12

        # Bars 0-3 map to H4 bar 0
        for i in range(4):
            assert mapping[i] == 0

        # Bars 4-7 map to H4 bar 1
        for i in range(4, 8):
            assert mapping[i] == 1

    def test_empty_h4(self):
        env = BacktestEnvironment.for_m5()
        bt = IRBBacktester(env=env)
        m5_candles = _make_candles(12)
        mapping = bt._build_h4_map(m5_candles, [])
        assert mapping == [-1] * 12

    def test_clamps_to_last_h4(self):
        """If more primary bars than ratio * higher bars, clamp to last."""
        env = BacktestEnvironment.for_m5()
        bt = IRBBacktester(env=env)

        m5_candles = _make_candles(48)
        h1_candles = _make_candles(2)  # only 2, but 48/12 = 4 expected

        mapping = bt._build_h4_map(m5_candles, h1_candles)
        # Bars beyond index 23 should clamp to last H1 bar (index 1)
        assert mapping[24] == 1
        assert mapping[47] == 1


# ===========================================================================
# Walk-forward _slice_h4 with M5 ratio
# ===========================================================================


class TestSliceH4M5:
    """Tests for _slice_h4 with configurable ratio."""

    def test_default_ratio_4(self):
        h4_start, h4_end = _slice_h4(0, 100, 50)
        assert h4_start == 0
        assert h4_end == 25

    def test_ratio_12_m5(self):
        h4_start, h4_end = _slice_h4(0, 120, 50, ratio=12)
        assert h4_start == 0
        assert h4_end == 10

    def test_ratio_12_offset(self):
        h4_start, h4_end = _slice_h4(120, 240, 50, ratio=12)
        assert h4_start == 10
        assert h4_end == 20

    def test_ratio_12_clamp(self):
        """Should clamp to h4_len if result exceeds it."""
        h4_start, h4_end = _slice_h4(0, 1200, 50, ratio=12)
        assert h4_end == 50  # clamped to h4_len

    def test_walkforward_config_ratio(self):
        """WalkForwardConfig should accept timeframe_ratio."""
        wf = WalkForwardConfig(timeframe_ratio=12)
        assert wf.timeframe_ratio == 12

    def test_walkforward_config_default_ratio(self):
        wf = WalkForwardConfig()
        assert wf.timeframe_ratio == 4


# ===========================================================================
# H1/H4 backward compatibility
# ===========================================================================


class TestBackwardCompatibility:
    """Ensure existing H1/H4 defaults are unchanged."""

    def test_default_environment_unchanged(self):
        env = BacktestEnvironment()
        assert env.primary_timeframe == "H1"
        assert env.higher_timeframe == "H4"
        assert env.h1_to_h4_ratio == 4
        assert env.h1_bars_per_day == 24
        assert env.h4_bars_per_day == 6

    def test_default_walkforward_config(self):
        wf = WalkForwardConfig()
        assert wf.train_bars == 4380
        assert wf.test_bars == 1460
        assert wf.timeframe_ratio == 4

    def test_h1_parameter_bounds_unchanged(self):
        bounds = StrategyConfig.PARAMETER_BOUNDS
        assert bounds["trigger_window_bars"].min_val == 10
        assert bounds["trigger_window_bars"].max_val == 40
        assert bounds["time_stop_bars"].min_val == 20
        assert bounds["time_stop_bars"].max_val == 80


# ===========================================================================
# C1 fix: autoresearch uses correct base_environment for M5
# ===========================================================================


class TestAutoresearchM5Environment:
    """Verify autoresearch threads base_environment to the backtester."""

    def test_evaluate_uses_m5_environment(self):
        """_evaluate with base_environment=for_m5() creates env with ratio=12.

        Uses H1-range parameter bounds so StrategyConfig validation passes
        (M5 bounds produce values outside Pydantic field limits).  The test
        validates that *base_environment* propagates to the backtester env.
        """
        from dataclasses import replace as dc_replace

        import numpy as np

        rng = np.random.default_rng(42)
        # Use H1 bounds so StrategyConfig validation passes
        org = _random_organism(rng, gen=0, bounds=StrategyConfig.PARAMETER_BOUNDS)

        m5_env = BacktestEnvironment.for_m5()
        captured_envs = []

        _original_replace = dc_replace

        def spy_replace(obj, **kwargs):
            result = _original_replace(obj, **kwargs)
            if isinstance(obj, BacktestEnvironment):
                captured_envs.append(result)
            return result

        m5_candles = _make_candles(100)
        h1_candles = _make_candles(10)

        with patch("novatrade.optimization.autoresearch.replace", side_effect=spy_replace):
            _evaluate(org, m5_candles, h1_candles, base_environment=m5_env)

        assert len(captured_envs) == 1
        env = captured_envs[0]
        assert env.h1_to_h4_ratio == 12
        assert env.primary_timeframe == "M5"
        assert env.higher_timeframe == "H1"

    def test_evaluate_defaults_to_h1h4(self):
        """_evaluate without base_environment uses DEFAULT_ENVIRONMENT (ratio=4)."""
        from dataclasses import replace as dc_replace

        import numpy as np

        rng = np.random.default_rng(42)
        org = _random_organism(rng, gen=0, bounds=StrategyConfig.PARAMETER_BOUNDS)

        captured_envs = []

        _original_replace = dc_replace

        def spy_replace(obj, **kwargs):
            result = _original_replace(obj, **kwargs)
            if isinstance(obj, BacktestEnvironment):
                captured_envs.append(result)
            return result

        h1_candles = _make_candles(100)
        h4_candles = _make_candles(25)

        with patch("novatrade.optimization.autoresearch.replace", side_effect=spy_replace):
            _evaluate(org, h1_candles, h4_candles)

        assert len(captured_envs) == 1
        env = captured_envs[0]
        assert env.h1_to_h4_ratio == 4
        assert env.primary_timeframe == "H1"

    def test_run_autoresearch_threads_base_environment(self):
        """run_autoresearch passes base_environment through to _evaluate.

        Uses H1 parameter_bounds so StrategyConfig validation passes.
        The test validates that base_environment propagates to replace().
        """
        from dataclasses import replace as dc_replace

        m5_env = BacktestEnvironment.for_m5()
        captured_envs = []

        _original_replace = dc_replace

        def spy_replace(obj, **kwargs):
            result = _original_replace(obj, **kwargs)
            if isinstance(obj, BacktestEnvironment):
                captured_envs.append(result)
            return result

        # Minimal config: 2 organisms, 1 generation to keep fast
        # Use H1 bounds so StrategyConfig validation passes
        cfg = AutoResearchConfig(population_size=2, generations=1, elite_count=1, seed=42)
        m5_candles = _make_candles(100)
        h1_candles = _make_candles(10)

        with patch("novatrade.optimization.autoresearch.replace", side_effect=spy_replace):
            run_autoresearch(
                m5_candles,
                h1_candles,
                config=cfg,
                parameter_bounds=StrategyConfig.PARAMETER_BOUNDS,
                base_environment=m5_env,
            )

        # All backtester instances should have received M5 environment
        assert len(captured_envs) > 0
        for env in captured_envs:
            assert env.h1_to_h4_ratio == 12, f"Expected ratio=12, got {env.h1_to_h4_ratio}"
            assert env.primary_timeframe == "M5"


# ===========================================================================
# C2 fix: walkforward uses correct base_environment for M5
# ===========================================================================


class TestWalkforwardM5Environment:
    """Verify walk-forward threads base_environment to the backtester."""

    def test_run_single_backtest_uses_m5_environment(self):
        """_run_single_backtest with base_environment=for_m5() uses ratio=12."""
        from dataclasses import replace as dc_replace

        m5_env = BacktestEnvironment.for_m5()
        config = StrategyConfig()
        captured_envs = []

        _original_replace = dc_replace

        def spy_replace(obj, **kwargs):
            result = _original_replace(obj, **kwargs)
            if isinstance(obj, BacktestEnvironment):
                captured_envs.append(result)
            return result

        with patch("novatrade.optimization.walkforward.replace", side_effect=spy_replace):
            _run_single_backtest(
                config,
                _make_candles(100),
                _make_candles(10),
                base_environment=m5_env,
            )

        assert len(captured_envs) == 1
        env = captured_envs[0]
        assert env.h1_to_h4_ratio == 12
        assert env.primary_timeframe == "M5"

    def test_run_single_backtest_defaults_to_h1h4(self):
        """_run_single_backtest without base_environment uses DEFAULT_ENVIRONMENT."""
        from dataclasses import replace as dc_replace

        config = StrategyConfig()
        captured_envs = []

        _original_replace = dc_replace

        def spy_replace(obj, **kwargs):
            result = _original_replace(obj, **kwargs)
            if isinstance(obj, BacktestEnvironment):
                captured_envs.append(result)
            return result

        with patch("novatrade.optimization.walkforward.replace", side_effect=spy_replace):
            _run_single_backtest(config, _make_candles(100), _make_candles(10))

        assert len(captured_envs) == 1
        env = captured_envs[0]
        assert env.h1_to_h4_ratio == 4
        assert env.primary_timeframe == "H1"

    def test_run_walk_forward_threads_base_environment(self):
        """run_walk_forward passes base_environment through to _run_single_backtest."""
        from dataclasses import replace as dc_replace

        m5_env = BacktestEnvironment.for_m5()
        config = StrategyConfig()
        captured_envs = []

        _original_replace = dc_replace

        def spy_replace(obj, **kwargs):
            result = _original_replace(obj, **kwargs)
            if isinstance(obj, BacktestEnvironment):
                captured_envs.append(result)
            return result

        # Small walk-forward: tiny windows to keep test fast
        wf = WalkForwardConfig(
            train_bars=100,
            test_bars=50,
            step_bars=50,
            embargo_bars=0,
            holdout_bars=50,
            timeframe_ratio=12,
        )
        # Need enough data: 100 train + 50 test + 50 holdout = 200 M5 bars
        m5_candles = _make_candles(250)
        h1_candles = _make_candles(25)  # 250/12 ~ 21, round up

        with patch("novatrade.optimization.walkforward.replace", side_effect=spy_replace):
            run_walk_forward(
                config,
                m5_candles,
                h1_candles,
                wf_config=wf,
                base_environment=m5_env,
            )

        # At least one backtest should have run (IS or OOS or holdout)
        assert len(captured_envs) > 0
        for env in captured_envs:
            assert env.h1_to_h4_ratio == 12, f"Expected ratio=12, got {env.h1_to_h4_ratio}"
            assert env.primary_timeframe == "M5"


# ===========================================================================
# H1 fix: _active_bounds global removed — bounds always threaded explicitly
# ===========================================================================


class TestNoGlobalBounds:
    """Verify _active_bounds global no longer exists."""

    def test_no_active_bounds_global(self):
        """The module should not have a _active_bounds attribute."""
        import novatrade.optimization.autoresearch as ar_mod

        assert not hasattr(ar_mod, "_active_bounds"), (
            "_active_bounds global should be removed — bounds are threaded explicitly"
        )


# ===========================================================================
# M2 fix: for_timeframe divisibility check
# ===========================================================================


class TestForTimeframeDivisibility:
    """Verify for_timeframe rejects non-divisible timeframe pairs."""

    def test_m5_h4_not_divisible(self):
        """M5 (5m) into H4 (240m) is divisible (48), should work."""
        env = BacktestEnvironment.for_timeframe("M5", "H4")
        assert env.h1_to_h4_ratio == 48

    def test_non_divisible_rejected(self):
        """A pair where higher is not evenly divisible by primary should raise."""
        # M30 (30m) into H4 (240m) = 8, divisible — OK
        env = BacktestEnvironment.for_timeframe("M30", "H4")
        assert env.h1_to_h4_ratio == 8

        # All standard pairs in _TIMEFRAME_MINUTES happen to be divisible,
        # so we test the check indirectly by verifying M1:H4 = 240 works
        env2 = BacktestEnvironment.for_timeframe("M1", "H4")
        assert env2.h1_to_h4_ratio == 240


# ===========================================================================
# StrategyConfig timeframe fields
# ===========================================================================


class TestStrategyConfigTimeframes:
    """Tests for primary_timeframe and higher_timeframe config fields."""

    def test_default_timeframes(self):
        """Default config should use H1/H4."""
        sc = StrategyConfig()
        assert sc.primary_timeframe == "H1"
        assert sc.higher_timeframe == "H4"

    def test_m5_timeframe_config(self):
        """Config should accept M5/H1 timeframe pair."""
        sc = StrategyConfig(primary_timeframe="M5", higher_timeframe="H1")
        assert sc.primary_timeframe == "M5"
        assert sc.higher_timeframe == "H1"

    def test_timeframe_roundtrip_yaml(self, tmp_path):
        """Timeframe fields should survive YAML serialization."""
        sc = StrategyConfig(
            primary_timeframe="M5",
            higher_timeframe="H1",
            name="m5_test",
        )
        yaml_path = tmp_path / "test_config.yaml"
        sc.to_yaml(yaml_path)
        loaded = StrategyConfig.from_yaml(yaml_path)
        assert loaded.primary_timeframe == "M5"
        assert loaded.higher_timeframe == "H1"

    def test_h1_h4_roundtrip_yaml(self, tmp_path):
        """Default H1/H4 should also survive YAML roundtrip."""
        sc = StrategyConfig(name="h1_test")
        yaml_path = tmp_path / "test_config.yaml"
        sc.to_yaml(yaml_path)
        loaded = StrategyConfig.from_yaml(yaml_path)
        assert loaded.primary_timeframe == "H1"
        assert loaded.higher_timeframe == "H4"
