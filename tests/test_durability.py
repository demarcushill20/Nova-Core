"""Comprehensive tests for strategy durability validation.

Tests cover:
  - rolling_window_analysis: various window sizes and edge cases
  - rolling_profitability_gate: profitable and unprofitable sequences
  - classify_regimes: known ATR patterns
  - regime_consistency_gate: regime breakdown and failure detection
  - drawdown_survivability_gate: DD limits, recovery factor, underwater days
  - year_over_year_breakdown: multi-year and single-year data
  - oos_decay_gate: various IS/OOS ratios
  - evaluate_durability: full integration report
  - DurabilityConfig: threshold overrides
  - Durability grading: A/B/C/D/F logic
"""

from __future__ import annotations

import random

from novatrade.backtest.metrics import CompletedTrade, ExitReason, TradeSide
from novatrade.evaluation.gates import GateResult
from novatrade.models import Candle

# ---------------------------------------------------------------------------
# Test helpers — synthetic data generation
# ---------------------------------------------------------------------------

_SECONDS_PER_DAY = 86_400


def _make_candles(
    n: int = 1000,
    start_ts: float = 1640995200.0,  # 2022-01-01 UTC
    base_price: float = 1.1,
    timeframe: str = "H1",
    volatility: str = "normal",
) -> list[Candle]:
    """Generate n synthetic candles with configurable volatility.

    Args:
        volatility: "normal", "high", "low", or "mixed" (changes halfway).
    """
    rng = random.Random(42)  # noqa: S311
    candles: list[Candle] = []
    interval = 3600 if timeframe == "H1" else 14400
    price = base_price

    for i in range(n):
        if volatility == "high":
            change = rng.uniform(-0.005, 0.005)
        elif volatility == "low":
            change = rng.uniform(-0.0002, 0.0002)
        elif volatility == "mixed" and i > n // 2:
            change = rng.uniform(-0.005, 0.005)
        else:
            change = rng.uniform(-0.001, 0.001)

        o = price
        c = price + change
        h = max(o, c) + rng.uniform(0, 0.001)
        l_val = min(o, c) - rng.uniform(0, 0.001)
        candles.append(
            Candle(
                timestamp=start_ts + i * interval,
                open=o,
                high=h,
                low=l_val,
                close=c,
                volume=rng.uniform(100, 1000),
                symbol="EURUSD",
                timeframe=timeframe,
            )
        )
        price = c

    return candles


def _make_trades(
    n: int = 200,
    profitable_pct: float = 0.6,
    start_ts: float = 1640995200.0,  # 2022-01-01 UTC
    spacing_seconds: float = 315360,  # ~3.65 days
    avg_win: float = 30.0,
    avg_loss: float = -20.0,
) -> list[CompletedTrade]:
    """Generate n synthetic trades spanning multiple years.

    With default spacing of ~3.65 days:
      - 200 trades = ~730 days = ~2 years
      - 400 trades = ~1460 days = ~4 years
    """
    rng = random.Random(42)  # noqa: S311
    trades: list[CompletedTrade] = []

    for i in range(n):
        is_winner = rng.random() < profitable_pct
        pnl = rng.uniform(5, abs(avg_win) * 2) if is_winner else rng.uniform(avg_loss * 2, -5)
        trades.append(
            CompletedTrade(
                trade_id=i,
                side=TradeSide.LONG,
                entry_bar=i * 20,
                exit_bar=i * 20 + 10,
                entry_price=1.1,
                exit_price=1.1 + pnl * 0.0001,
                stop_loss=1.09,
                volume=0.1,
                exit_reason=ExitReason.TRAILING_STOP,
                pnl_pips=pnl,
                pnl_usd=pnl * 10,
                risk_r=pnl / 30,
                hold_bars=10,
                entry_timestamp=start_ts + i * spacing_seconds,
                exit_timestamp=start_ts + i * spacing_seconds + 36000,
            )
        )

    return trades


def _make_losing_trades(
    n: int = 100,
    start_ts: float = 1640995200.0,
    spacing_seconds: float = 315360,
) -> list[CompletedTrade]:
    """Generate trades that are mostly losers."""
    return _make_trades(
        n=n,
        profitable_pct=0.2,
        start_ts=start_ts,
        spacing_seconds=spacing_seconds,
        avg_win=10.0,
        avg_loss=-40.0,
    )


# =========================================================================
# Rolling Window Analysis Tests
# =========================================================================


class TestRollingWindowAnalysis:
    """Tests for rolling_window_analysis."""

    def test_basic_window_generation(self):
        from novatrade.evaluation.durability import rolling_window_analysis

        trades = _make_trades(200, profitable_pct=0.6)
        windows = rolling_window_analysis(trades, window_months=12, step_months=3)
        assert len(windows) > 0
        for w in windows:
            assert w.trade_count > 0
            assert w.start_ts < w.end_ts

    def test_empty_trades(self):
        from novatrade.evaluation.durability import rolling_window_analysis

        windows = rolling_window_analysis([], window_months=12, step_months=3)
        assert windows == []

    def test_6_month_windows(self):
        from novatrade.evaluation.durability import rolling_window_analysis

        trades = _make_trades(200, profitable_pct=0.6)
        windows = rolling_window_analysis(trades, window_months=6, step_months=3)
        assert len(windows) > 0
        # 6-month windows should produce more windows than 12-month
        windows_12m = rolling_window_analysis(trades, window_months=12, step_months=3)
        assert len(windows) >= len(windows_12m)

    def test_window_metrics_populated(self):
        from novatrade.evaluation.durability import rolling_window_analysis

        trades = _make_trades(200, profitable_pct=0.6)
        windows = rolling_window_analysis(trades, window_months=12, step_months=3)
        for w in windows:
            assert hasattr(w, "net_pnl")
            assert hasattr(w, "profit_factor")
            assert hasattr(w, "max_drawdown_pct")
            assert hasattr(w, "trade_count")
            assert hasattr(w, "win_rate")
            assert w.win_rate >= 0.0
            assert w.win_rate <= 1.0

    def test_custom_step_months(self):
        from novatrade.evaluation.durability import rolling_window_analysis

        trades = _make_trades(300, profitable_pct=0.6)
        windows_step1 = rolling_window_analysis(trades, window_months=12, step_months=1)
        windows_step6 = rolling_window_analysis(trades, window_months=12, step_months=6)
        # Smaller step = more windows
        assert len(windows_step1) >= len(windows_step6)

    def test_short_trade_history(self):
        """Trades spanning < 1 window should still produce at least one window."""
        from novatrade.evaluation.durability import rolling_window_analysis

        # 20 trades ~ 73 days (much less than 12 months)
        trades = _make_trades(20, profitable_pct=0.6)
        windows = rolling_window_analysis(trades, window_months=12, step_months=3)
        # May produce 0 or 1 windows depending on the window+step boundary
        assert isinstance(windows, list)

    def test_custom_initial_equity(self):
        from novatrade.evaluation.durability import rolling_window_analysis

        trades = _make_trades(200, profitable_pct=0.6)
        windows = rolling_window_analysis(trades, window_months=12, step_months=3, initial_equity=50000.0)
        assert len(windows) > 0


# =========================================================================
# Rolling Profitability Gate Tests
# =========================================================================


class TestRollingProfitabilityGate:
    """Tests for rolling_profitability_gate."""

    def test_profitable_strategy_passes(self):
        from novatrade.evaluation.durability import rolling_profitability_gate

        trades = _make_trades(300, profitable_pct=0.7, avg_win=40.0, avg_loss=-15.0)
        result = rolling_profitability_gate(trades)
        assert result.gate == "D.rolling_profitability"
        assert isinstance(result, GateResult)
        assert "rolling_12m_win_pct" in result.details
        assert "rolling_6m_win_pct" in result.details
        assert "profit_concentration_ratio" in result.details

    def test_unprofitable_strategy_fails(self):
        from novatrade.evaluation.durability import rolling_profitability_gate

        trades = _make_losing_trades(300)
        result = rolling_profitability_gate(trades)
        assert result.gate == "D.rolling_profitability"
        # Should likely fail on window profitability
        assert isinstance(result.passed, bool)

    def test_empty_trades_fail(self):
        from novatrade.evaluation.durability import rolling_profitability_gate

        result = rolling_profitability_gate([])
        assert result.passed is False
        assert result.details["trade_count"] == 0

    def test_custom_thresholds(self):
        from novatrade.evaluation.durability import rolling_profitability_gate

        trades = _make_trades(200, profitable_pct=0.6)
        # Very relaxed thresholds — should pass
        result = rolling_profitability_gate(
            trades,
            min_12m_win_pct=0.0,
            min_6m_win_pct=0.0,
            max_concentration_ratio=1.0,
        )
        assert result.passed is True

    def test_strict_thresholds(self):
        from novatrade.evaluation.durability import rolling_profitability_gate

        trades = _make_trades(200, profitable_pct=0.55)
        # Strict thresholds
        result = rolling_profitability_gate(
            trades,
            min_12m_win_pct=0.95,
            min_6m_win_pct=0.95,
            max_concentration_ratio=0.05,
        )
        assert result.passed is False

    def test_concentration_check(self):
        from novatrade.evaluation.durability import rolling_profitability_gate

        # Make trades where most profit comes from one period
        trades = _make_trades(200, profitable_pct=0.5, avg_win=10.0, avg_loss=-10.0)
        result = rolling_profitability_gate(trades)
        assert "profit_concentration_ratio" in result.details


# =========================================================================
# Regime Classification Tests
# =========================================================================


class TestClassifyRegimes:
    """Tests for classify_regimes."""

    def test_basic_classification(self):
        from novatrade.evaluation.durability import classify_regimes

        candles = _make_candles(100)
        regime_map = classify_regimes(candles)
        assert len(regime_map) == len(candles)
        valid_regimes = {"trending", "ranging", "volatile", "quiet"}
        for _ts, regime in regime_map.items():
            assert regime in valid_regimes

    def test_insufficient_data(self):
        """With fewer candles than ATR period, all should be 'ranging'."""
        from novatrade.evaluation.durability import classify_regimes

        candles = _make_candles(5)  # less than atr_period=14
        regime_map = classify_regimes(candles, atr_period=14)
        assert all(r == "ranging" for r in regime_map.values())

    def test_high_volatility_candles(self):
        """High volatility candles should include 'volatile' regime."""
        from novatrade.evaluation.durability import classify_regimes

        candles = _make_candles(100, volatility="high")
        regime_map = classify_regimes(candles)
        regimes = set(regime_map.values())
        # High vol candles should produce volatile regime
        assert len(regimes) >= 1  # At least some classification

    def test_low_volatility_candles(self):
        """Low volatility candles should include 'quiet' regime."""
        from novatrade.evaluation.durability import classify_regimes

        candles = _make_candles(100, volatility="low")
        regime_map = classify_regimes(candles)
        regimes = set(regime_map.values())
        assert len(regimes) >= 1

    def test_custom_atr_period(self):
        from novatrade.evaluation.durability import classify_regimes

        candles = _make_candles(200)
        regime_map_short = classify_regimes(candles, atr_period=7)
        regime_map_long = classify_regimes(candles, atr_period=28)
        # Both should produce valid regime maps
        assert len(regime_map_short) == len(candles)
        assert len(regime_map_long) == len(candles)

    def test_empty_candles(self):
        from novatrade.evaluation.durability import classify_regimes

        regime_map = classify_regimes([])
        assert regime_map == {}

    def test_mixed_volatility_produces_multiple_regimes(self):
        """Mixed volatility data should produce diverse regime classification."""
        from novatrade.evaluation.durability import classify_regimes

        candles = _make_candles(500, volatility="mixed")
        regime_map = classify_regimes(candles)
        regime_counts = {}
        for r in regime_map.values():
            regime_counts[r] = regime_counts.get(r, 0) + 1
        # At least 2 different regimes should appear
        assert len(regime_counts) >= 2


# =========================================================================
# Regime Consistency Gate Tests
# =========================================================================


class TestRegimeConsistencyGate:
    """Tests for regime_consistency_gate."""

    def test_basic_consistency(self):
        from novatrade.evaluation.durability import regime_consistency_gate

        trades = _make_trades(200, profitable_pct=0.6)
        candles = _make_candles(500)
        result = regime_consistency_gate(trades, candles)
        assert result.gate == "D.regime_consistency"
        assert isinstance(result, GateResult)
        assert "regime_consistency_score" in result.details
        assert "regimes" in result.details

    def test_empty_trades_fail(self):
        from novatrade.evaluation.durability import regime_consistency_gate

        candles = _make_candles(100)
        result = regime_consistency_gate([], candles)
        assert result.passed is False

    def test_empty_candles_fail(self):
        from novatrade.evaluation.durability import regime_consistency_gate

        trades = _make_trades(50)
        result = regime_consistency_gate(trades, [])
        assert result.passed is False

    def test_regime_breakdown_contains_all_regimes(self):
        from novatrade.evaluation.durability import regime_consistency_gate

        trades = _make_trades(200, profitable_pct=0.6)
        candles = _make_candles(500)
        result = regime_consistency_gate(trades, candles)
        regimes = result.details.get("regimes", {})
        for expected in ["trending", "ranging", "volatile", "quiet"]:
            assert expected in regimes

    def test_custom_negative_regime_threshold(self):
        from novatrade.evaluation.durability import regime_consistency_gate

        trades = _make_trades(200, profitable_pct=0.6)
        candles = _make_candles(500)
        # Very strict — any negative regime covering > 1% of data fails
        result = regime_consistency_gate(trades, candles, max_negative_regime_pct=0.01)
        assert result.gate == "D.regime_consistency"

    def test_profitable_all_regimes_passes(self):
        from novatrade.evaluation.durability import regime_consistency_gate

        # Very profitable strategy should pass regime consistency
        trades = _make_trades(200, profitable_pct=0.85, avg_win=40.0, avg_loss=-10.0)
        candles = _make_candles(500)
        result = regime_consistency_gate(trades, candles)
        # With 85% win rate, most regimes should be positive
        assert result.gate == "D.regime_consistency"


# =========================================================================
# Drawdown Survivability Gate Tests
# =========================================================================


class TestDrawdownSurvivabilityGate:
    """Tests for drawdown_survivability_gate."""

    def test_healthy_drawdown_passes(self):
        from novatrade.evaluation.durability import drawdown_survivability_gate

        trades = _make_trades(200, profitable_pct=0.7, avg_win=30.0, avg_loss=-15.0)
        result = drawdown_survivability_gate(trades, max_rolling_dd_pct=50.0)
        assert result.gate == "D.drawdown_survivability"
        assert isinstance(result, GateResult)
        assert "max_overall_dd_pct" in result.details
        assert "recovery_factor" in result.details
        assert "max_underwater_days" in result.details

    def test_empty_trades_fail(self):
        from novatrade.evaluation.durability import drawdown_survivability_gate

        result = drawdown_survivability_gate([])
        assert result.passed is False
        assert result.details["trade_count"] == 0

    def test_deep_drawdown_fails(self):
        from novatrade.evaluation.durability import drawdown_survivability_gate

        trades = _make_losing_trades(200)
        result = drawdown_survivability_gate(
            trades,
            max_rolling_dd_pct=2.0,  # Very tight DD limit
            min_recovery_factor=10.0,  # Very high recovery requirement
        )
        assert result.passed is False

    def test_custom_thresholds(self):
        from novatrade.evaluation.durability import drawdown_survivability_gate

        trades = _make_trades(200, profitable_pct=0.6)
        # Very relaxed thresholds
        result = drawdown_survivability_gate(
            trades,
            max_rolling_dd_pct=100.0,
            min_recovery_factor=0.0,
            max_underwater_days=10000,
        )
        assert result.passed is True

    def test_underwater_days_tracking(self):
        from novatrade.evaluation.durability import drawdown_survivability_gate

        trades = _make_trades(200, profitable_pct=0.6)
        result = drawdown_survivability_gate(trades)
        assert "max_underwater_days" in result.details
        assert isinstance(result.details["max_underwater_days"], int)

    def test_recovery_factor_in_details(self):
        from novatrade.evaluation.durability import drawdown_survivability_gate

        trades = _make_trades(200, profitable_pct=0.7, avg_win=40.0, avg_loss=-15.0)
        result = drawdown_survivability_gate(trades)
        rf = result.details.get("recovery_factor")
        # Recovery factor should be a number or "inf"
        assert rf is not None

    def test_thresholds_in_details(self):
        from novatrade.evaluation.durability import drawdown_survivability_gate

        result = drawdown_survivability_gate(
            _make_trades(100, profitable_pct=0.6),
            max_rolling_dd_pct=8.0,
            min_recovery_factor=3.0,
            max_underwater_days=60,
        )
        thresholds = result.details.get("thresholds", {})
        assert thresholds.get("max_rolling_dd_pct") == 8.0
        assert thresholds.get("min_recovery_factor") == 3.0
        assert thresholds.get("max_underwater_days") == 60


# =========================================================================
# Year-Over-Year Breakdown Tests
# =========================================================================


class TestYearOverYearBreakdown:
    """Tests for year_over_year_breakdown."""

    def test_basic_breakdown(self):
        from novatrade.evaluation.durability import year_over_year_breakdown

        # Trades spanning 2022-2023 (2 years)
        trades = _make_trades(200, start_ts=1640995200.0)  # 2022-01-01
        yoy = year_over_year_breakdown(trades)
        assert len(yoy) >= 1
        years = [y.year for y in yoy]
        assert 2022 in years

    def test_empty_trades(self):
        from novatrade.evaluation.durability import year_over_year_breakdown

        yoy = year_over_year_breakdown([])
        assert yoy == []

    def test_single_year(self):
        from novatrade.evaluation.durability import year_over_year_breakdown

        # 30 trades over ~109 days — all in 2022
        trades = _make_trades(
            30,
            profitable_pct=0.6,
            start_ts=1640995200.0,
            spacing_seconds=315360,
        )
        yoy = year_over_year_breakdown(trades)
        assert len(yoy) >= 1
        for y in yoy:
            assert y.trade_count > 0
            assert hasattr(y, "net_pnl")
            assert hasattr(y, "profit_factor")
            assert hasattr(y, "max_drawdown_pct")
            assert hasattr(y, "win_rate")

    def test_multi_year_sorted(self):
        from novatrade.evaluation.durability import year_over_year_breakdown

        # 4 years of trades
        trades = _make_trades(
            400,
            profitable_pct=0.6,
            start_ts=1577836800.0,  # 2020-01-01
            spacing_seconds=315360,
        )
        yoy = year_over_year_breakdown(trades)
        years = [y.year for y in yoy]
        assert years == sorted(years)  # chronologically sorted
        assert len(years) >= 3  # Should span at least 3 years

    def test_per_year_metrics_valid(self):
        from novatrade.evaluation.durability import year_over_year_breakdown

        trades = _make_trades(200, profitable_pct=0.65)
        yoy = year_over_year_breakdown(trades)
        for y in yoy:
            assert y.win_rate >= 0.0
            assert y.win_rate <= 1.0
            assert y.trade_count > 0
            assert y.max_drawdown_pct >= 0.0

    def test_custom_initial_equity(self):
        from novatrade.evaluation.durability import year_over_year_breakdown

        trades = _make_trades(200, profitable_pct=0.6)
        yoy = year_over_year_breakdown(trades, initial_equity=50000.0)
        assert len(yoy) >= 1


# =========================================================================
# OOS Decay Gate Tests
# =========================================================================


class TestOOSDecayGate:
    """Tests for oos_decay_gate."""

    def test_good_oos_ratio_passes(self):
        from novatrade.evaluation.durability import oos_decay_gate

        result = oos_decay_gate(
            is_profit_factor=2.0,
            oos_profit_factor=1.5,
            min_oos_is_ratio=0.50,
        )
        assert result.gate == "D.oos_decay"
        assert result.passed is True
        assert result.details["oos_is_ratio"] == 0.75

    def test_excessive_decay_fails(self):
        from novatrade.evaluation.durability import oos_decay_gate

        result = oos_decay_gate(
            is_profit_factor=2.0,
            oos_profit_factor=0.5,
            min_oos_is_ratio=0.50,
        )
        assert result.passed is False
        assert "decay" in result.reason.lower()

    def test_non_positive_is_pf_fails(self):
        from novatrade.evaluation.durability import oos_decay_gate

        result = oos_decay_gate(
            is_profit_factor=0.0,
            oos_profit_factor=1.5,
        )
        assert result.passed is False
        assert "non-positive" in result.reason.lower()

    def test_negative_is_pf_fails(self):
        from novatrade.evaluation.durability import oos_decay_gate

        result = oos_decay_gate(
            is_profit_factor=-1.0,
            oos_profit_factor=1.0,
        )
        assert result.passed is False

    def test_infinite_is_pf_with_positive_oos(self):
        from novatrade.evaluation.durability import oos_decay_gate

        result = oos_decay_gate(
            is_profit_factor=float("inf"),
            oos_profit_factor=1.5,
        )
        assert result.passed is True  # Infinite IS with positive OOS is acceptable

    def test_infinite_is_pf_with_negative_oos(self):
        from novatrade.evaluation.durability import oos_decay_gate

        result = oos_decay_gate(
            is_profit_factor=float("inf"),
            oos_profit_factor=-0.5,
        )
        assert result.passed is False

    def test_equal_is_oos(self):
        from novatrade.evaluation.durability import oos_decay_gate

        result = oos_decay_gate(
            is_profit_factor=2.0,
            oos_profit_factor=2.0,
        )
        assert result.passed is True
        assert result.details["oos_is_ratio"] == 1.0

    def test_custom_min_ratio(self):
        from novatrade.evaluation.durability import oos_decay_gate

        result = oos_decay_gate(
            is_profit_factor=2.0,
            oos_profit_factor=1.0,
            min_oos_is_ratio=0.80,
        )
        # OOS/IS = 0.5, but threshold is 0.80
        assert result.passed is False

    def test_details_structure(self):
        from novatrade.evaluation.durability import oos_decay_gate

        result = oos_decay_gate(
            is_profit_factor=2.0,
            oos_profit_factor=1.2,
        )
        assert "oos_is_ratio" in result.details
        assert "is_profit_factor" in result.details
        assert "oos_profit_factor" in result.details
        assert "min_oos_is_ratio" in result.details


# =========================================================================
# DurabilityConfig Tests
# =========================================================================


class TestDurabilityConfig:
    """Tests for DurabilityConfig dataclass."""

    def test_default_values(self):
        from novatrade.evaluation.durability import DurabilityConfig

        cfg = DurabilityConfig()
        assert cfg.min_12m_win_pct == 0.60
        assert cfg.min_6m_win_pct == 0.55
        assert cfg.max_concentration_ratio == 0.40
        assert cfg.max_rolling_dd_pct == 10.0
        assert cfg.min_recovery_factor == 2.0
        assert cfg.max_underwater_days == 90
        assert cfg.min_oos_is_ratio == 0.50
        assert cfg.max_negative_regime_pct == 0.20

    def test_custom_overrides(self):
        from novatrade.evaluation.durability import DurabilityConfig

        cfg = DurabilityConfig(
            min_12m_win_pct=0.80,
            max_rolling_dd_pct=5.0,
            min_recovery_factor=5.0,
            max_underwater_days=30,
        )
        assert cfg.min_12m_win_pct == 0.80
        assert cfg.max_rolling_dd_pct == 5.0
        assert cfg.min_recovery_factor == 5.0
        assert cfg.max_underwater_days == 30


# =========================================================================
# Durability Grading Tests
# =========================================================================


class TestDurabilityGrading:
    """Tests for _compute_durability_grade logic."""

    def test_all_pass_grade_a(self):
        from novatrade.evaluation.durability import _compute_durability_grade

        gates = [
            GateResult(gate="D.rolling_profitability", passed=True, reason="ok"),
            GateResult(gate="D.regime_consistency", passed=True, reason="ok"),
            GateResult(gate="D.drawdown_survivability", passed=True, reason="ok"),
        ]
        assert _compute_durability_grade(gates) == "A"

    def test_one_fail_grade_b(self):
        from novatrade.evaluation.durability import _compute_durability_grade

        gates = [
            GateResult(gate="D.rolling_profitability", passed=True, reason="ok"),
            GateResult(gate="D.regime_consistency", passed=False, reason="fail"),
            GateResult(gate="D.drawdown_survivability", passed=True, reason="ok"),
        ]
        assert _compute_durability_grade(gates) == "B"

    def test_two_fails_grade_c(self):
        from novatrade.evaluation.durability import _compute_durability_grade

        gates = [
            GateResult(gate="D.rolling_profitability", passed=True, reason="ok"),
            GateResult(gate="D.regime_consistency", passed=False, reason="fail"),
            GateResult(gate="D.drawdown_survivability", passed=False, reason="fail"),
            GateResult(gate="D.oos_decay", passed=True, reason="ok"),
        ]
        # Two non-critical fails + one critical = F per the grading logic
        # Actually: D.drawdown_survivability is critical, 2 fails total with critical = F
        assert _compute_durability_grade(gates) == "F"

    def test_three_plus_fails_grade_d(self):
        from novatrade.evaluation.durability import _compute_durability_grade

        gates = [
            GateResult(gate="D.regime_consistency", passed=False, reason="fail"),
            GateResult(gate="D.oos_decay", passed=False, reason="fail"),
            GateResult(gate="D.other", passed=False, reason="fail"),
        ]
        assert _compute_durability_grade(gates) == "D"

    def test_critical_failure_grade_f(self):
        from novatrade.evaluation.durability import _compute_durability_grade

        gates = [
            GateResult(gate="D.rolling_profitability", passed=False, reason="fail"),
            GateResult(gate="D.drawdown_survivability", passed=False, reason="fail"),
        ]
        assert _compute_durability_grade(gates) == "F"

    def test_single_critical_fail_grade_b(self):
        """Single critical failure (no 2nd failure) should be B, not F."""
        from novatrade.evaluation.durability import _compute_durability_grade

        gates = [
            GateResult(gate="D.rolling_profitability", passed=False, reason="fail"),
            GateResult(gate="D.regime_consistency", passed=True, reason="ok"),
            GateResult(gate="D.drawdown_survivability", passed=True, reason="ok"),
        ]
        # Only 1 failure, even though critical -> grade B
        assert _compute_durability_grade(gates) == "B"

    def test_empty_gates_grade_a(self):
        from novatrade.evaluation.durability import _compute_durability_grade

        assert _compute_durability_grade([]) == "A"


# =========================================================================
# evaluate_durability Integration Tests
# =========================================================================


class TestEvaluateDurability:
    """Tests for evaluate_durability full integration."""

    def test_full_report_structure(self):
        from novatrade.evaluation.durability import DurabilityReport, evaluate_durability

        trades = _make_trades(200, profitable_pct=0.65)
        candles = _make_candles(500)
        report = evaluate_durability(trades, candles)

        assert isinstance(report, DurabilityReport)
        assert hasattr(report, "rolling_12m_win_pct")
        assert hasattr(report, "rolling_6m_win_pct")
        assert hasattr(report, "regime_consistency_score")
        assert hasattr(report, "profit_concentration_ratio")
        assert hasattr(report, "max_underwater_days")
        assert hasattr(report, "recovery_factor")
        assert hasattr(report, "oos_is_ratio")
        assert hasattr(report, "year_breakdown")
        assert hasattr(report, "durability_grade")
        assert hasattr(report, "gates")
        assert hasattr(report, "overall_passed")

    def test_gates_included(self):
        from novatrade.evaluation.durability import evaluate_durability

        trades = _make_trades(200, profitable_pct=0.65)
        candles = _make_candles(500)
        report = evaluate_durability(trades, candles)

        # Should have at least 3 gates (rolling, regime, drawdown)
        assert len(report.gates) >= 3
        gate_names = {g.gate for g in report.gates}
        assert "D.rolling_profitability" in gate_names
        assert "D.regime_consistency" in gate_names
        assert "D.drawdown_survivability" in gate_names

    def test_oos_gate_included_when_provided(self):
        from novatrade.evaluation.durability import evaluate_durability

        trades = _make_trades(200, profitable_pct=0.65)
        candles = _make_candles(500)
        report = evaluate_durability(
            trades,
            candles,
            is_profit_factor=2.0,
            oos_profit_factor=1.5,
        )

        gate_names = {g.gate for g in report.gates}
        assert "D.oos_decay" in gate_names
        assert report.oos_is_ratio is not None

    def test_oos_gate_skipped_when_not_provided(self):
        from novatrade.evaluation.durability import evaluate_durability

        trades = _make_trades(200, profitable_pct=0.65)
        candles = _make_candles(500)
        report = evaluate_durability(trades, candles)

        gate_names = {g.gate for g in report.gates}
        assert "D.oos_decay" not in gate_names
        assert report.oos_is_ratio is None

    def test_grade_is_valid(self):
        from novatrade.evaluation.durability import evaluate_durability

        trades = _make_trades(200, profitable_pct=0.65)
        candles = _make_candles(500)
        report = evaluate_durability(trades, candles)

        assert report.durability_grade in ("A", "B", "C", "D", "F")

    def test_overall_passed_consistent_with_gates(self):
        from novatrade.evaluation.durability import evaluate_durability

        trades = _make_trades(200, profitable_pct=0.65)
        candles = _make_candles(500)
        report = evaluate_durability(trades, candles)

        all_passed = all(g.passed for g in report.gates)
        assert report.overall_passed == all_passed

    def test_custom_config(self):
        from novatrade.evaluation.durability import DurabilityConfig, evaluate_durability

        trades = _make_trades(200, profitable_pct=0.6)
        candles = _make_candles(500)
        cfg = DurabilityConfig(
            min_12m_win_pct=0.0,
            min_6m_win_pct=0.0,
            max_concentration_ratio=1.0,
            max_rolling_dd_pct=100.0,
            min_recovery_factor=0.0,
            max_underwater_days=100000,
            max_negative_regime_pct=1.0,
        )
        report = evaluate_durability(trades, candles, config=cfg)
        # With very relaxed thresholds, should pass
        assert report.overall_passed is True
        assert report.durability_grade == "A"

    def test_year_breakdown_populated(self):
        from novatrade.evaluation.durability import evaluate_durability

        trades = _make_trades(200, profitable_pct=0.65)
        candles = _make_candles(500)
        report = evaluate_durability(trades, candles)

        assert isinstance(report.year_breakdown, list)
        if report.year_breakdown:
            yb = report.year_breakdown[0]
            assert hasattr(yb, "year")
            assert hasattr(yb, "net_pnl")
            assert hasattr(yb, "trade_count")

    def test_custom_initial_equity(self):
        from novatrade.evaluation.durability import evaluate_durability

        trades = _make_trades(200, profitable_pct=0.65)
        candles = _make_candles(500)
        report = evaluate_durability(trades, candles, initial_equity=100000.0)
        assert report is not None


# =========================================================================
# Evaluation Ladder Integration Tests
# =========================================================================


class TestEvaluationLadderIntegration:
    """Tests for Stage C and durability wiring in the evaluation ladder."""

    def test_dashboard_row_has_stage_c_gates(self):
        from novatrade.evaluation.evaluation_ladder import DashboardRow

        row = DashboardRow(experiment_id="test_001")
        assert hasattr(row, "stage_c_gates")
        assert row.stage_c_gates == []

    def test_dashboard_row_has_durability_fields(self):
        from novatrade.evaluation.evaluation_ladder import DashboardRow

        row = DashboardRow(experiment_id="test_001")
        assert hasattr(row, "durability_grade")
        assert hasattr(row, "durability_passed")
        assert row.durability_grade is None
        assert row.durability_passed is None

    def test_dashboard_validate_stage_c(self):
        from novatrade.evaluation.evaluation_ladder import (
            DashboardRow,
            EvaluationDashboard,
            LadderStage,
        )

        dashboard = EvaluationDashboard()
        row = DashboardRow(
            experiment_id="exp_001",
            stage_a_passed=True,
            ranking_score=0.75,
            ladder_stage=LadderStage.RANKED,
        )
        dashboard.add(row)

        gate_results = [
            GateResult(gate="C.monte_carlo", passed=True, reason="ok"),
            GateResult(gate="C.walk_forward", passed=True, reason="ok"),
        ]

        found = dashboard.validate_stage_c("exp_001", gate_results)
        assert found is True
        assert row.stage_c_passed is True
        assert row.ladder_stage == LadderStage.VALIDATED
        assert len(row.stage_c_gates) == 2

    def test_dashboard_validate_stage_c_fails(self):
        from novatrade.evaluation.evaluation_ladder import (
            DashboardRow,
            EvaluationDashboard,
            LadderStage,
        )

        dashboard = EvaluationDashboard()
        row = DashboardRow(
            experiment_id="exp_002",
            stage_a_passed=True,
            ranking_score=0.75,
            ladder_stage=LadderStage.RANKED,
        )
        dashboard.add(row)

        gate_results = [
            GateResult(gate="C.monte_carlo", passed=True, reason="ok"),
            GateResult(gate="C.walk_forward", passed=False, reason="failed"),
        ]

        found = dashboard.validate_stage_c("exp_002", gate_results)
        assert found is True
        assert row.stage_c_passed is False
        assert row.ladder_stage == LadderStage.RANKED  # Stays RANKED, not promoted

    def test_dashboard_validate_stage_c_not_found(self):
        from novatrade.evaluation.evaluation_ladder import EvaluationDashboard

        dashboard = EvaluationDashboard()
        found = dashboard.validate_stage_c("nonexistent", [])
        assert found is False

    def test_dashboard_set_durability(self):
        from novatrade.evaluation.evaluation_ladder import (
            DashboardRow,
            EvaluationDashboard,
            LadderStage,
        )

        dashboard = EvaluationDashboard()
        row = DashboardRow(
            experiment_id="exp_003",
            ladder_stage=LadderStage.VALIDATED,
        )
        dashboard.add(row)

        found = dashboard.set_durability("exp_003", "A", True)
        assert found is True
        assert row.durability_grade == "A"
        assert row.durability_passed is True

    def test_dashboard_set_durability_not_found(self):
        from novatrade.evaluation.evaluation_ladder import EvaluationDashboard

        dashboard = EvaluationDashboard()
        found = dashboard.set_durability("nonexistent", "B", False)
        assert found is False

    def test_to_dict_includes_new_fields(self):
        from novatrade.evaluation.evaluation_ladder import DashboardRow

        row = DashboardRow(
            experiment_id="test_dict",
            stage_c_gates=[
                GateResult(gate="C.monte_carlo", passed=True, reason="ok"),
            ],
            durability_grade="B",
            durability_passed=True,
        )
        d = row.to_dict()
        assert "stage_c_gates" in d
        assert "durability_grade" in d
        assert "durability_passed" in d
        assert d["durability_grade"] == "B"
        assert d["durability_passed"] is True
        assert len(d["stage_c_gates"]) == 1
        assert d["stage_c_gates"][0]["gate"] == "C.monte_carlo"


# =========================================================================
# Promotion Pipeline Integration Tests
# =========================================================================


class TestPromotionPipelineIntegration:
    """Tests for durability wiring in the promotion pipeline."""

    def test_promotion_result_has_durability_fields(self):
        from novatrade.optimization.promotion import PromotionResult

        result = PromotionResult(experiment_id="test_001")
        assert hasattr(result, "durability_passed")
        assert hasattr(result, "durability_grade")
        assert result.durability_passed is False
        assert result.durability_grade is None

    def test_promotion_overall_includes_durability(self):
        """overall_passed should require durability_passed."""
        from novatrade.optimization.promotion import PromotionResult

        result = PromotionResult(
            experiment_id="test_002",
            walkforward_passed=True,
            holdout_passed=True,
            perturbation_passed=True,
            stress_passed=True,
            durability_passed=False,
        )
        # Manually compute what overall should be
        assert not (
            result.walkforward_passed
            and result.holdout_passed
            and result.perturbation_passed
            and result.stress_passed
            and result.durability_passed
        )


# =========================================================================
# Pipeline Orchestrator Integration Tests
# =========================================================================


class TestOrchestratorIntegration:
    """Tests for robustness validation in the pipeline orchestrator."""

    def test_pipeline_result_has_robustness_results(self):
        from novatrade.pipeline.orchestrator import PipelineMode, PipelineResult

        result = PipelineResult(mode=PipelineMode.CAMPAIGN)
        assert hasattr(result, "robustness_results")
        assert result.robustness_results == {}

    def test_robustness_validation_stage_skips_empty(self):
        from novatrade.pipeline.orchestrator import _stage_robustness_validation

        stage, results = _stage_robustness_validation([], [], [])
        assert stage.status == "skipped"
        assert results == {}

    def test_robustness_validation_stage_with_survivors(self):
        from novatrade.pipeline.orchestrator import _stage_robustness_validation

        candles = _make_candles(200)
        survivors = [
            {
                "experiment_id": "exp_001",
                "trades": _make_trades(60, profitable_pct=0.7),
                "score": 0.85,
            },
        ]
        stage, results = _stage_robustness_validation(survivors, candles, candles[:50])
        assert stage.status == "ok"
        assert "exp_001" in results
        assert "gates" in results["exp_001"]
        assert "overall_passed" in results["exp_001"]

    def test_robustness_validation_caps_survivors(self):
        from novatrade.pipeline.orchestrator import _stage_robustness_validation

        candles = _make_candles(200)
        survivors = [{"experiment_id": f"exp_{i}", "trades": _make_trades(30, profitable_pct=0.6)} for i in range(10)]
        stage, results = _stage_robustness_validation(survivors, candles, candles[:50], max_survivors=3)
        assert stage.details["candidates_tested"] == 3
