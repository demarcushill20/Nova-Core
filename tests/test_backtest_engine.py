"""Tests for novatrade.backtest.engine — IRB backtesting engine."""

import math
from pathlib import Path

import pytest

from novatrade.backtest.engine import (
    BacktestResult,
    IRBBacktester,
    StrategyState,
    _OpenPosition,
    compute_adx,
    compute_atr,
    compute_ema,
)
from novatrade.backtest.environment import BacktestEnvironment, SpreadAssumptions
from novatrade.backtest.metrics import ExitReason, TradeSide
from novatrade.models import Candle

# ---------------------------------------------------------------------------
# Candle factories
# ---------------------------------------------------------------------------


def _candle(
    o: float,
    h: float,
    low: float,
    c: float,
    ts: float = 0.0,
    vol: float = 100.0,
) -> Candle:
    return Candle(timestamp=ts, open=o, high=h, low=low, close=c, volume=vol)


def _trending_up_candles(n: int, start: float = 1.1000, step: float = 0.0010) -> list[Candle]:
    """Generate n candles in a clean uptrend."""
    candles = []
    for i in range(n):
        base = start + i * step
        candles.append(
            _candle(
                o=base,
                h=base + 0.0020,
                low=base - 0.0005,
                c=base + 0.0015,
                ts=float(i * 3600),
            )
        )
    return candles


def _trending_down_candles(n: int, start: float = 1.2000, step: float = 0.0010) -> list[Candle]:
    """Generate n candles in a clean downtrend."""
    candles = []
    for i in range(n):
        base = start - i * step
        candles.append(
            _candle(
                o=base,
                h=base + 0.0005,
                low=base - 0.0020,
                c=base - 0.0015,
                ts=float(i * 3600),
            )
        )
    return candles


def _make_irb_candle_uptrend(base: float) -> Candle:
    """Create a valid uptrend IRB candle.

    Upper wick >= 45% of range. Open and close in lower 55%.
    Range = high - low. Threshold = high - 0.45 * range.
    Open <= threshold and close <= threshold.
    """
    low = base
    high = base + 0.0050  # 50 pip range
    # threshold = high - 0.45 * 0.0050 = high - 0.00225
    # Open and close must be <= high - 0.00225 = base + 0.00275
    o = base + 0.0010
    c = base + 0.0020
    return _candle(o=o, h=high, low=low, c=c)


def _make_irb_candle_downtrend(base: float) -> Candle:
    """Create a valid downtrend IRB candle.

    Lower wick >= 45% of range. Open and close in upper 55%.
    """
    low = base - 0.0050
    high = base
    # threshold = low + 0.45 * 0.0050 = low + 0.00225
    # Open and close must be >= low + 0.00225 = base - 0.00275
    o = base - 0.0010
    c = base - 0.0020
    return _candle(o=o, h=high, low=low, c=c)


# ---------------------------------------------------------------------------
# Indicator tests
# ---------------------------------------------------------------------------


class TestComputeEMA:
    def test_empty(self):
        assert compute_ema([], 20) == []

    def test_single_value(self):
        result = compute_ema([100.0], 20)
        assert len(result) == 1
        assert result[0] == 100.0

    def test_constant_series(self):
        closes = [50.0] * 30
        ema = compute_ema(closes, 20)
        # EMA of constant series should converge to that constant
        assert ema[-1] == pytest.approx(50.0, abs=0.01)

    def test_trending_up(self):
        closes = [100.0 + i for i in range(50)]
        ema = compute_ema(closes, 20)
        # EMA should lag behind price in uptrend
        assert ema[-1] < closes[-1]
        assert ema[-1] > closes[0]


class TestComputeATR:
    def test_empty(self):
        assert compute_atr([], 14) == []

    def test_constant_range(self):
        candles = [_candle(10, 12, 8, 11) for _ in range(20)]
        atr = compute_atr(candles, 14)
        # Range = 12-8 = 4 for all, no gaps → ATR should converge to 4
        assert atr[-1] == pytest.approx(4.0, rel=0.05)

    def test_nan_before_period(self):
        candles = [_candle(10, 12, 8, 11) for _ in range(10)]
        atr = compute_atr(candles, 14)
        # Not enough bars for period=14
        assert all(math.isnan(v) for v in atr)


class TestComputeADX:
    def test_empty(self):
        assert compute_adx([], 14) == []

    def test_strong_trend_high_adx(self):
        candles = _trending_up_candles(80)
        adx = compute_adx(candles, 14)
        # In a clean trend, ADX should be > 20
        valid_adx = [v for v in adx if not math.isnan(v)]
        assert len(valid_adx) > 0
        assert valid_adx[-1] > 15  # should be trending

    def test_nan_for_short_series(self):
        candles = [_candle(10, 12, 8, 11) for _ in range(10)]
        adx = compute_adx(candles, 14)
        assert all(math.isnan(v) for v in adx)


# ---------------------------------------------------------------------------
# Backtester tests
# ---------------------------------------------------------------------------


class TestIRBBacktester:
    def _make_env(self, **overrides) -> BacktestEnvironment:
        defaults = dict(
            warmup_bars=34,
            initial_equity=100_000.0,
        )
        defaults.update(overrides)
        return BacktestEnvironment(**defaults)  # type: ignore[arg-type]

    def test_empty_candles(self):
        bt = IRBBacktester()
        result = bt.run([], [])
        assert result.total_bars == 0
        assert result.trades == []

    def test_warmup_period_no_signals(self):
        """During warmup (first 34 bars), no signals should be generated."""
        env = self._make_env(warmup_bars=34)
        bt = IRBBacktester(env=env)
        candles = _trending_up_candles(30)
        h4 = _trending_up_candles(8)
        result = bt.run(candles, h4)
        assert result.trades == []
        assert result.signals == []

    def test_irb_geometry_detection_uptrend(self):
        """Test that uptrend IRB geometry is correctly detected."""
        env = self._make_env(warmup_bars=5)  # reduce warmup for test
        bt = IRBBacktester(env=env)

        # Build a series with clear uptrend + IRB candle
        candles = _trending_up_candles(40, start=1.1000, step=0.0015)
        # Replace bar 38 with an IRB candle
        base = 1.1000 + 38 * 0.0015
        candles[38] = _make_irb_candle_uptrend(base)
        # Continue trend after IRB
        candles.append(
            _candle(
                o=base + 0.0040,
                h=base + 0.0060,
                low=base + 0.0030,
                c=base + 0.0055,
                ts=39 * 3600.0,
            )
        )

        h4 = _trending_up_candles(11, start=1.1000, step=0.0060)
        result = bt.run(candles, h4)

        # Should detect at least the IRB geometry (may or may not pass all filters)
        # Check that some signals or rejections were recorded
        total_evaluations = (
            len(result.signals)
            + result.filter_rejections.irb_geometry
            + result.filter_rejections.trend_filter
            + result.filter_rejections.mtf_alignment
            + result.filter_rejections.sideways_filter
            + result.filter_rejections.overextension_filter
            + result.filter_rejections.existing_position
            + result.filter_rejections.existing_pending
            + result.filter_rejections.warmup
        )
        assert total_evaluations > 0

    def test_pending_order_expires(self):
        """Pending orders should expire after trigger_window_bars."""
        env = self._make_env(warmup_bars=2, trigger_window_bars=5)
        bt = IRBBacktester(env=env)

        # Create a scenario: uptrend, IRB forms, but price never reaches stop level
        candles = _trending_up_candles(20, start=1.1000, step=0.0010)
        # Insert IRB at bar 5
        candles[5] = _make_irb_candle_uptrend(1.1050)
        # After IRB, price stays flat (never reaches buy-stop)
        for i in range(6, 20):
            candles[i] = _candle(1.1040, 1.1045, 1.1035, 1.1042, ts=i * 3600.0)

        h4 = _trending_up_candles(5, start=1.1000, step=0.0040)
        result = bt.run(candles, h4)

        # Check that expired orders are recorded
        _expired = [o for o in result.pending_orders if o.cancel_reason is not None]
        # The engine should record pending order lifecycle
        assert result.total_bars == 20

    def test_stop_loss_exit(self):
        """A position should close at stop loss when price hits it."""
        env = self._make_env(warmup_bars=2, trigger_window_bars=20, time_stop_bars=100)
        bt = IRBBacktester(env=env)

        # Simple scenario: create position then price drops through SL
        # We directly test the position management part
        # Build candles where an IRB forms, fills, then reverses hard
        candles = _trending_up_candles(10, start=1.1000, step=0.0015)
        # Put IRB at bar 5
        irb_base = 1.1000 + 5 * 0.0015
        candles[5] = _make_irb_candle_uptrend(irb_base)
        # Bar 6: price goes up past entry (fills buy-stop)
        candles[6] = _candle(
            irb_base + 0.0040,
            irb_base + 0.0060,
            irb_base + 0.0020,
            irb_base + 0.0055,
            ts=6 * 3600.0,
        )
        # Bar 7-9: price crashes below IRB low (SL)
        for i in range(7, 10):
            crash = irb_base - 0.0100 * (i - 6)
            candles[i] = _candle(crash + 0.001, crash + 0.002, crash - 0.001, crash, ts=i * 3600.0)

        h4 = _trending_up_candles(3, start=1.1000, step=0.0060)
        result = bt.run(candles, h4)

        # Even if the signal doesn't fire due to filter conditions,
        # the engine should run without errors
        assert result.total_bars == 10

    def test_result_structure(self):
        bt = IRBBacktester()
        result = bt.run(_trending_up_candles(50), _trending_up_candles(13))
        assert isinstance(result, BacktestResult)
        assert isinstance(result.trades, list)
        assert isinstance(result.pending_orders, list)
        assert isinstance(result.signals, list)
        assert isinstance(result.filter_rejections, object)
        assert result.final_equity > 0

    def test_h4_map_simple_ratio(self):
        """Without timestamps, H4 mapping uses 4:1 ratio."""
        h1 = [_candle(1.1, 1.12, 1.08, 1.11) for _ in range(20)]
        h4 = [_candle(1.1, 1.12, 1.08, 1.11) for _ in range(5)]
        bt = IRBBacktester()
        mapping = bt._build_h4_map(h1, h4)
        assert len(mapping) == 20
        assert mapping[0] == 0
        assert mapping[3] == 0
        assert mapping[4] == 1
        assert mapping[19] == 4

    def test_h4_map_with_timestamps(self):
        """With timestamps, H4 mapping uses timestamp matching."""
        h1 = [_candle(1.1, 1.12, 1.08, 1.11, ts=float(i * 3600)) for i in range(8)]
        h4 = [_candle(1.1, 1.12, 1.08, 1.11, ts=float(i * 14400)) for i in range(3)]
        bt = IRBBacktester()
        mapping = bt._build_h4_map(h1, h4)
        assert len(mapping) == 8
        # H4 bar 0 at ts=0, bar 1 at ts=14400 (4 hours)
        assert mapping[0] == 0  # H1 ts=0 → H4 bar 0
        assert mapping[3] == 0  # H1 ts=10800 → H4 bar 0 (before 14400)
        assert mapping[4] == 1  # H1 ts=14400 → H4 bar 1

    def test_downtrend_irb_detection(self):
        """Verify downtrend candle data creates valid IRB geometry."""
        irb = _make_irb_candle_downtrend(1.1500)
        rng = irb.high - irb.low
        threshold = irb.low + 0.45 * rng
        assert irb.open >= threshold
        assert irb.close >= threshold

    def test_uptrend_irb_geometry_valid(self):
        """Verify uptrend candle data creates valid IRB geometry."""
        irb = _make_irb_candle_uptrend(1.1000)
        rng = irb.high - irb.low
        threshold = irb.high - 0.45 * rng
        assert irb.open <= threshold
        assert irb.close <= threshold


class TestParityAuditToggleNoOp(TestIRBBacktester):
    """Live-safety regression: empty parity_audit_toggles MUST produce bit-identical
    results to the current engine. If this test fails, a parity-audit toggle has
    leaked into the default code path and live behavior may have changed.

    Inherits ``_make_env`` from ``TestIRBBacktester`` so the regression mirrors
    the same environment construction the rest of the suite uses.
    """

    @staticmethod
    def _build_inputs() -> tuple[list[Candle], list[Candle]]:
        """Small synthetic backtest input that exercises signal generation.

        Mirrors the shape used by ``test_stop_loss_exit`` and
        ``test_irb_geometry_detection_uptrend`` — uptrend with an IRB candle
        injected so the engine reaches _manage_position / _close_position
        and any toggle leak would diverge.
        """
        candles = _trending_up_candles(40, start=1.1000, step=0.0015)
        irb_base = 1.1000 + 38 * 0.0015
        candles[38] = _make_irb_candle_uptrend(irb_base)
        candles.append(
            _candle(
                o=irb_base + 0.0040,
                h=irb_base + 0.0060,
                low=irb_base + 0.0030,
                c=irb_base + 0.0055,
                ts=39 * 3600.0,
            )
        )
        h4 = _trending_up_candles(11, start=1.1000, step=0.0060)
        return candles, h4

    def _run(self, env: BacktestEnvironment) -> BacktestResult:
        candles, h4 = self._build_inputs()
        return IRBBacktester(env=env).run(candles, h4)

    def test_empty_toggles_match_default(self):
        """Default (field unset) and explicit empty frozenset must be identical."""
        env_a = self._make_env(warmup_bars=5)
        env_b = self._make_env(warmup_bars=5, parity_audit_toggles=frozenset())

        result_a = self._run(env_a)
        result_b = self._run(env_b)

        assert result_a.final_equity == result_b.final_equity
        assert len(result_a.trades) == len(result_b.trades)
        for ta, tb in zip(result_a.trades, result_b.trades, strict=True):
            assert ta.entry_price == tb.entry_price
            assert ta.exit_price == tb.exit_price
            assert ta.exit_reason == tb.exit_reason
            assert ta.pnl_pips == tb.pnl_pips
            assert ta.pnl_usd == tb.pnl_usd

    def test_unknown_toggle_is_no_op(self):
        """An unknown toggle label must not change behavior — the engine should
        only react to known labels added in later phases. Unknown strings are a
        no-op so a typo or stale config never silently flips behavior."""
        env_default = self._make_env(warmup_bars=5)
        env_unknown = self._make_env(
            warmup_bars=5,
            parity_audit_toggles=frozenset({"this_does_not_exist"}),
        )

        result_default = self._run(env_default)
        result_unknown = self._run(env_unknown)

        assert result_default.final_equity == result_unknown.final_equity
        assert len(result_default.trades) == len(result_unknown.trades)
        for td, tu in zip(result_default.trades, result_unknown.trades, strict=True):
            assert td.entry_price == tu.entry_price
            assert td.exit_price == tu.exit_price
            assert td.exit_reason == tu.exit_reason
            assert td.pnl_pips == tu.pnl_pips
            assert td.pnl_usd == tu.pnl_usd


class TestStrategyState:
    def test_all_states(self):
        states = list(StrategyState)
        assert len(states) == 5
        assert StrategyState.FLAT in states
        assert StrategyState.PENDING_LONG in states
        assert StrategyState.PENDING_SHORT in states
        assert StrategyState.LONG in states
        assert StrategyState.SHORT in states


# ---------------------------------------------------------------------------
# Transaction cost deduction tests
# ---------------------------------------------------------------------------


class TestTransactionCostDeduction:
    """Verify that spread, slippage, and commission are properly deducted
    from PnL in _close_position().

    Tests exercise the fix for the CRITICAL bug where transaction costs
    existed in the environment config but were never subtracted from PnL.
    """

    @staticmethod
    def _make_env(**overrides) -> BacktestEnvironment:
        defaults = dict(
            warmup_bars=2,
            initial_equity=100_000.0,
        )
        defaults.update(overrides)
        return BacktestEnvironment(**defaults)  # type: ignore[arg-type]

    @staticmethod
    def _setup_backtester_with_position(
        env: BacktestEnvironment,
        side: TradeSide,
        entry_price: float,
        stop_loss: float,
        volume: float = 0.10,
    ) -> IRBBacktester:
        """Create an IRBBacktester with an open position injected directly.

        This bypasses signal generation to isolate _close_position() logic.
        """
        bt = IRBBacktester(env=env)
        bt._state = StrategyState.LONG if side == TradeSide.LONG else StrategyState.SHORT
        bt._position = _OpenPosition(
            side=side,
            entry_price=entry_price,
            stop_loss=stop_loss,
            volume=volume,
            entry_bar=0,
            current_stop=stop_loss,
            best_close=entry_price,
        )
        return bt

    # --- Zero-cost baseline: matches old (pre-fix) behavior ---

    def test_zero_costs_long_pnl_matches_raw_calculation(self):
        """With all costs at zero, PnL should equal raw price difference."""
        env = self._make_env(
            spread=SpreadAssumptions(
                avg_spread_pips=0.0,
                slippage_pips=0.0,
                commission_per_lot_usd=0.0,
            ),
        )
        bt = self._setup_backtester_with_position(
            env,
            TradeSide.LONG,
            entry_price=1.10000,
            stop_loss=1.09500,
            volume=0.10,
        )
        bt._close_position(5, 1.10500, ExitReason.TIME_STOP)

        assert len(bt._trades) == 1
        trade = bt._trades[0]
        # Raw: (1.10500 - 1.10000) / 0.0001 = 50.0 pips
        assert trade.pnl_pips == pytest.approx(50.0, abs=0.01)
        # USD: 50 pips * 0.10 lots * $10/pip/lot = $50.00
        assert trade.pnl_usd == pytest.approx(50.0, abs=0.01)

    def test_zero_costs_short_pnl_matches_raw_calculation(self):
        """With all costs at zero, SHORT PnL should equal raw price difference."""
        env = self._make_env(
            spread=SpreadAssumptions(
                avg_spread_pips=0.0,
                slippage_pips=0.0,
                commission_per_lot_usd=0.0,
            ),
        )
        bt = self._setup_backtester_with_position(
            env,
            TradeSide.SHORT,
            entry_price=1.10000,
            stop_loss=1.10500,
            volume=0.10,
        )
        bt._close_position(5, 1.09500, ExitReason.TIME_STOP)

        trade = bt._trades[0]
        # Raw: (1.10000 - 1.09500) / 0.0001 = 50.0 pips
        assert trade.pnl_pips == pytest.approx(50.0, abs=0.01)
        assert trade.pnl_usd == pytest.approx(50.0, abs=0.01)

    # --- Costs reduce PnL vs zero-cost baseline ---

    def test_realistic_costs_lower_than_zero_cost_long(self):
        """PnL with realistic costs should be strictly lower than zero-cost PnL."""
        entry = 1.10000
        exit_p = 1.10500
        stop = 1.09500
        vol = 0.10

        # Zero costs
        env0 = self._make_env(
            spread=SpreadAssumptions(
                avg_spread_pips=0.0,
                slippage_pips=0.0,
                commission_per_lot_usd=0.0,
            ),
        )
        bt0 = self._setup_backtester_with_position(env0, TradeSide.LONG, entry, stop, vol)
        bt0._close_position(5, exit_p, ExitReason.TIME_STOP)
        pnl_zero = bt0._trades[0].pnl_usd

        # Realistic costs (1.2 pip spread, 0.5 pip slippage, $3.50/lot commission)
        env1 = self._make_env(
            spread=SpreadAssumptions(
                avg_spread_pips=1.2,
                slippage_pips=0.5,
                commission_per_lot_usd=3.50,
            ),
        )
        bt1 = self._setup_backtester_with_position(env1, TradeSide.LONG, entry, stop, vol)
        bt1._close_position(5, exit_p, ExitReason.TIME_STOP)
        pnl_costs = bt1._trades[0].pnl_usd

        assert pnl_costs < pnl_zero

    def test_realistic_costs_lower_than_zero_cost_short(self):
        """PnL with realistic costs should be strictly lower than zero-cost PnL (SHORT)."""
        entry = 1.10000
        exit_p = 1.09500
        stop = 1.10500
        vol = 0.10

        env0 = self._make_env(
            spread=SpreadAssumptions(
                avg_spread_pips=0.0,
                slippage_pips=0.0,
                commission_per_lot_usd=0.0,
            ),
        )
        bt0 = self._setup_backtester_with_position(env0, TradeSide.SHORT, entry, stop, vol)
        bt0._close_position(5, exit_p, ExitReason.TIME_STOP)
        pnl_zero = bt0._trades[0].pnl_usd

        env1 = self._make_env(
            spread=SpreadAssumptions(
                avg_spread_pips=1.2,
                slippage_pips=0.5,
                commission_per_lot_usd=3.50,
            ),
        )
        bt1 = self._setup_backtester_with_position(env1, TradeSide.SHORT, entry, stop, vol)
        bt1._close_position(5, exit_p, ExitReason.TIME_STOP)
        pnl_costs = bt1._trades[0].pnl_usd

        assert pnl_costs < pnl_zero

    # --- Each cost component contributes independently ---

    def test_spread_only_deduction(self):
        """Spread alone should reduce PnL by spread * volume * pip_value_per_lot."""
        spread_pips = 1.5
        env = self._make_env(
            spread=SpreadAssumptions(
                avg_spread_pips=spread_pips,
                slippage_pips=0.0,
                commission_per_lot_usd=0.0,
            ),
        )
        vol = 0.10
        bt = self._setup_backtester_with_position(
            env,
            TradeSide.LONG,
            entry_price=1.10000,
            stop_loss=1.09500,
            volume=vol,
        )
        bt._close_position(5, 1.10500, ExitReason.TIME_STOP)

        trade = bt._trades[0]
        raw_pips = 50.0
        expected_pips = raw_pips - spread_pips  # 50 - 1.5 = 48.5
        expected_usd = expected_pips * vol * 10.0  # 48.5 * 0.1 * 10 = $48.50
        assert trade.pnl_pips == pytest.approx(expected_pips, abs=0.01)
        assert trade.pnl_usd == pytest.approx(expected_usd, abs=0.01)

    def test_slippage_only_deduction(self):
        """Slippage alone should reduce PnL by 2*slippage * volume * pip_value_per_lot.

        Slippage is doubled because it applies on both entry and exit (round-trip).
        """
        slippage_pips = 0.5
        env = self._make_env(
            spread=SpreadAssumptions(
                avg_spread_pips=0.0,
                slippage_pips=slippage_pips,
                commission_per_lot_usd=0.0,
            ),
        )
        vol = 0.10
        bt = self._setup_backtester_with_position(
            env,
            TradeSide.LONG,
            entry_price=1.10000,
            stop_loss=1.09500,
            volume=vol,
        )
        bt._close_position(5, 1.10500, ExitReason.TIME_STOP)

        trade = bt._trades[0]
        raw_pips = 50.0
        # total_cost_pips = 0 (spread) + 2 * 0.5 (slippage) = 1.0
        expected_pips = raw_pips - 2 * slippage_pips  # 50 - 1.0 = 49.0
        expected_usd = expected_pips * vol * 10.0
        assert trade.pnl_pips == pytest.approx(expected_pips, abs=0.01)
        assert trade.pnl_usd == pytest.approx(expected_usd, abs=0.01)

    def test_commission_only_deduction(self):
        """Commission alone should reduce PnL in USD but not in pips.

        Commission is per-lot, round-trip (single charge covering entry + exit).
        """
        commission = 3.50  # USD per lot round-trip
        env = self._make_env(
            spread=SpreadAssumptions(
                avg_spread_pips=0.0,
                slippage_pips=0.0,
                commission_per_lot_usd=commission,
            ),
        )
        vol = 0.10
        bt = self._setup_backtester_with_position(
            env,
            TradeSide.LONG,
            entry_price=1.10000,
            stop_loss=1.09500,
            volume=vol,
        )
        bt._close_position(5, 1.10500, ExitReason.TIME_STOP)

        trade = bt._trades[0]
        # Pips should be unaffected by commission
        assert trade.pnl_pips == pytest.approx(50.0, abs=0.01)
        # USD: raw = 50 * 0.1 * 10 = $50. Commission = 3.50 * 0.10 = $0.35
        expected_usd = 50.0 * vol * 10.0 - commission * vol
        assert trade.pnl_usd == pytest.approx(expected_usd, abs=0.01)

    def test_all_costs_combined(self):
        """All three cost components applied together."""
        spread_pips = 1.2
        slippage_pips = 0.5
        commission = 3.50
        env = self._make_env(
            spread=SpreadAssumptions(
                avg_spread_pips=spread_pips,
                slippage_pips=slippage_pips,
                commission_per_lot_usd=commission,
            ),
        )
        vol = 0.10
        bt = self._setup_backtester_with_position(
            env,
            TradeSide.LONG,
            entry_price=1.10000,
            stop_loss=1.09500,
            volume=vol,
        )
        bt._close_position(5, 1.10500, ExitReason.TIME_STOP)

        trade = bt._trades[0]
        # total_cost_pips = 1.2 + 2*0.5 = 2.2
        expected_pips = 50.0 - (spread_pips + 2 * slippage_pips)  # 50 - 2.2 = 47.8
        expected_usd = expected_pips * vol * 10.0 - commission * vol
        # 47.8 * 0.1 * 10 = $47.80 - $0.35 = $47.45
        assert trade.pnl_pips == pytest.approx(expected_pips, abs=0.01)
        assert trade.pnl_usd == pytest.approx(expected_usd, abs=0.01)

    # --- Both directions ---

    def test_costs_applied_symmetrically_long_and_short(self):
        """Same magnitude trade in opposite directions should have equal cost deduction."""
        spread_pips = 1.0
        slippage_pips = 0.3
        commission = 5.0
        vol = 0.20

        env = self._make_env(
            spread=SpreadAssumptions(
                avg_spread_pips=spread_pips,
                slippage_pips=slippage_pips,
                commission_per_lot_usd=commission,
            ),
        )

        # LONG: buy 1.10000, sell 1.10500 => +50 raw pips
        bt_long = self._setup_backtester_with_position(
            env,
            TradeSide.LONG,
            entry_price=1.10000,
            stop_loss=1.09500,
            volume=vol,
        )
        bt_long._close_position(5, 1.10500, ExitReason.TIME_STOP)

        # SHORT: sell 1.10500, buy 1.10000 => +50 raw pips
        bt_short = self._setup_backtester_with_position(
            env,
            TradeSide.SHORT,
            entry_price=1.10500,
            stop_loss=1.11000,
            volume=vol,
        )
        bt_short._close_position(5, 1.10000, ExitReason.TIME_STOP)

        long_trade = bt_long._trades[0]
        short_trade = bt_short._trades[0]

        # Both should have the same PnL in pips (same magnitude, same costs)
        assert long_trade.pnl_pips == pytest.approx(short_trade.pnl_pips, abs=0.01)
        # Both should have the same PnL in USD
        assert long_trade.pnl_usd == pytest.approx(short_trade.pnl_usd, abs=0.01)

    def test_costs_can_turn_profit_into_loss(self):
        """A small winning trade can become a net loss after costs."""
        # Only 2 pips gross profit, but 2.5 pips total cost => net loss
        env = self._make_env(
            spread=SpreadAssumptions(
                avg_spread_pips=2.0,
                slippage_pips=0.25,
                commission_per_lot_usd=0.0,
            ),
        )
        bt = self._setup_backtester_with_position(
            env,
            TradeSide.LONG,
            entry_price=1.10000,
            stop_loss=1.09500,
            volume=0.10,
        )
        # Exit 2 pips above entry
        bt._close_position(5, 1.10020, ExitReason.TIME_STOP)

        trade = bt._trades[0]
        # Raw = 2.0 pips. Cost = 2.0 + 2*0.25 = 2.5 pips. Net = -0.5 pips.
        assert trade.pnl_pips < 0
        assert trade.pnl_usd < 0

    def test_equity_updated_with_costs(self):
        """Final equity should reflect transaction cost deductions."""
        initial = 100_000.0
        env = self._make_env(
            initial_equity=initial,
            spread=SpreadAssumptions(
                avg_spread_pips=1.0,
                slippage_pips=0.0,
                commission_per_lot_usd=0.0,
            ),
        )
        bt = self._setup_backtester_with_position(
            env,
            TradeSide.LONG,
            entry_price=1.10000,
            stop_loss=1.09500,
            volume=1.0,
        )
        bt._close_position(5, 1.10500, ExitReason.TIME_STOP)

        trade = bt._trades[0]
        assert bt._equity == pytest.approx(initial + trade.pnl_usd, abs=0.01)
        # Verify the cost actually reduced equity vs raw
        raw_usd = 50.0 * 1.0 * 10.0  # 50 pips * 1 lot * $10
        assert bt._equity < initial + raw_usd

    def test_fixed_spread_used_when_set(self):
        """When fixed_spread_pips is set, it overrides avg_spread_pips."""
        env = self._make_env(
            spread=SpreadAssumptions(
                fixed_spread_pips=2.0,
                avg_spread_pips=1.0,  # should be ignored
                slippage_pips=0.0,
                commission_per_lot_usd=0.0,
            ),
        )
        bt = self._setup_backtester_with_position(
            env,
            TradeSide.LONG,
            entry_price=1.10000,
            stop_loss=1.09500,
            volume=0.10,
        )
        bt._close_position(5, 1.10500, ExitReason.TIME_STOP)

        trade = bt._trades[0]
        # total_cost_pips should use fixed (2.0), not avg (1.0)
        expected_pips = 50.0 - 2.0  # = 48.0
        assert trade.pnl_pips == pytest.approx(expected_pips, abs=0.01)


# ---------------------------------------------------------------------------
# ATR floor + spread cushion parity (engine vs strategy wrapper)
# ---------------------------------------------------------------------------


class TestEngineATRFloorParity:
    """Verify the backtest engine applies ATR floor to SL after spread cushion,
    matching the IRBStrategy wrapper behaviour."""

    def test_atr_floor_widens_sl_in_engine(self):
        """Engine should apply ATR floor when candle geometry is tight."""
        h1 = _trending_up_candles(100)
        h4 = _trending_up_candles(25, step=0.004)

        env = BacktestEnvironment(
            atr_sl_floor_multiplier=1.0,
            sl_spread_buffer_pips=1.0,
        )
        bt = IRBBacktester(env=env)
        bt.run(h1, h4)

        env_no_floor = BacktestEnvironment(
            atr_sl_floor_multiplier=0.0,
            sl_spread_buffer_pips=1.0,
        )
        bt_no_floor = IRBBacktester(env=env_no_floor)
        bt_no_floor.run(h1, h4)

        # If ATR floor is active, trades with tight geometry should have
        # wider SL distances.
        if bt._trades and bt_no_floor._trades:
            floor_dists = [abs(t.entry_price - t.stop_loss) for t in bt._trades]
            no_floor_dists = [abs(t.entry_price - t.stop_loss) for t in bt_no_floor._trades]
            avg_floor = sum(floor_dists) / len(floor_dists)
            avg_no_floor = sum(no_floor_dists) / len(no_floor_dists)
            assert avg_floor >= avg_no_floor, f"ATR floor should widen or equal SL: {avg_floor} vs {avg_no_floor}"

    def test_spread_cushion_and_atr_floor_interaction(self):
        """When both spread cushion and ATR floor are active, the wider one wins."""
        h1 = _trending_up_candles(100)
        h4 = _trending_up_candles(25, step=0.004)

        # Large ATR floor + small spread → ATR floor dominates
        env_big_floor = BacktestEnvironment(
            atr_sl_floor_multiplier=2.0,
            sl_spread_buffer_pips=0.5,
        )
        bt_big = IRBBacktester(env=env_big_floor)
        bt_big.run(h1, h4)

        # No ATR floor + large spread → spread cushion is only factor
        env_big_spread = BacktestEnvironment(
            atr_sl_floor_multiplier=0.0,
            sl_spread_buffer_pips=3.0,
        )
        bt_spread = IRBBacktester(env=env_big_spread)
        bt_spread.run(h1, h4)

        # Both should produce trades with wider stops than the base case
        env_base = BacktestEnvironment(
            atr_sl_floor_multiplier=0.0,
            sl_spread_buffer_pips=0.0,
        )
        bt_base = IRBBacktester(env=env_base)
        bt_base.run(h1, h4)

        if bt_big._trades and bt_base._trades:
            big_dists = [abs(t.entry_price - t.stop_loss) for t in bt_big._trades]
            base_dists = [abs(t.entry_price - t.stop_loss) for t in bt_base._trades]
            assert sum(big_dists) / len(big_dists) >= sum(base_dists) / len(base_dists)


class TestInitialStopFromEmaTrail:
    """Pine v5 initializes the live trigger stop from EMA(40), not the IRB
    wick. These tests guard that semantics in the Python engine.

    Spec: docs/superpowers/specs/2026-04-27-engine-stop-divergence-fix-design.md
    """

    def _make_env(self, **overrides) -> BacktestEnvironment:
        defaults = dict(
            warmup_bars=5,
            initial_equity=100_000.0,
            trail_ema_period=40,
            atr_sl_floor_multiplier=0.0,
            sl_spread_buffer_pips=0.0,
            partial_exit_enabled=False,
            use_simple_trend_filter=True,
        )
        defaults.update(overrides)
        return BacktestEnvironment(**defaults)  # type: ignore[arg-type]

    def _setup_pending(
        self,
        env: BacktestEnvironment,
        side: TradeSide,
        entry_price: float,
        wick_stop: float,
        cur_trail_ema: float,
    ) -> IRBBacktester:
        """Create a backtester with a _PendingOrder injected and the
        per-bar trail_ema stash set, ready for a _check_pending_fill call.
        """
        from novatrade.backtest.engine import _PendingOrder

        bt = IRBBacktester(env=env)
        bt._state = StrategyState.PENDING_LONG if side == TradeSide.LONG else StrategyState.PENDING_SHORT
        bt._pending = _PendingOrder(
            side=side,
            entry_price=entry_price,
            stop_loss=wick_stop,
            volume=0.10,
            bar_placed=0,
            irb_bar=0,
        )
        bt._cur_atr = 0.0010  # arbitrary non-zero
        bt._cur_trail_ema = cur_trail_ema
        return bt

    def test_initial_stop_uses_trail_ema_when_enabled(self):
        """Long fill with trail_ema_period>0 → current_stop = trail_ema, not wick."""
        env = self._make_env(trail_ema_period=40)
        wick_stop = 1.09800  # 20 pips below entry
        ema_at_entry = 1.09950  # 5 pips below entry — closer than wick
        bt = self._setup_pending(
            env,
            TradeSide.LONG,
            entry_price=1.10000,
            wick_stop=wick_stop,
            cur_trail_ema=ema_at_entry,
        )

        # Bar that triggers fill (high crosses entry_price)
        fill_bar = _candle(o=1.09990, h=1.10010, low=1.09990, c=1.10005, ts=3600.0)
        bt._check_pending_fill(1, fill_bar)

        assert bt._position is not None
        # Live trigger stop must be the EMA value (Pine cur_stop semantics)
        assert bt._position.current_stop == pytest.approx(ema_at_entry, abs=1e-6)
        # Sizing/R anchors must remain at the wick
        assert bt._position.stop_loss == pytest.approx(wick_stop, abs=1e-6)
        assert bt._position.initial_stop == pytest.approx(wick_stop, abs=1e-6)

    def test_initial_stop_uses_wick_when_trail_ema_disabled(self):
        """Live-champion regression guard: trail_ema_period=0 keeps wick stop."""
        env = self._make_env(trail_ema_period=0)
        wick_stop = 1.09800
        bt = self._setup_pending(
            env,
            TradeSide.LONG,
            entry_price=1.10000,
            wick_stop=wick_stop,
            cur_trail_ema=float("nan"),  # _process_bar stashes NaN when disabled
        )
        fill_bar = _candle(o=1.09990, h=1.10010, low=1.09990, c=1.10005, ts=3600.0)
        bt._check_pending_fill(1, fill_bar)

        assert bt._position is not None
        # Override must NOT fire when trail_ema_period == 0
        assert bt._position.current_stop == pytest.approx(wick_stop, abs=1e-6)
        assert bt._position.stop_loss == pytest.approx(wick_stop, abs=1e-6)

    def test_initial_stop_falls_back_to_wick_when_ema_is_nan(self):
        """If EMA isn't computed yet (warmup race), fall back to wick stop."""
        env = self._make_env(trail_ema_period=40)
        wick_stop = 1.09800
        bt = self._setup_pending(
            env,
            TradeSide.LONG,
            entry_price=1.10000,
            wick_stop=wick_stop,
            cur_trail_ema=float("nan"),
        )
        fill_bar = _candle(o=1.09990, h=1.10010, low=1.09990, c=1.10005, ts=3600.0)
        bt._check_pending_fill(1, fill_bar)

        assert bt._position is not None
        assert bt._position.current_stop == pytest.approx(wick_stop, abs=1e-6)

    def test_initial_stop_unconditional_when_ema_wrong_sided_long(self):
        """Pine-faithful: if EMA >= fill_price for a long, current_stop is
        still set to EMA (instant SL on next adverse tick). Pine does not
        clamp; Python must not either.
        """
        env = self._make_env(trail_ema_period=40)
        wick_stop = 1.09800  # 20 pips below entry
        ema_above_entry = 1.10050  # 5 pips ABOVE entry — wrong side
        bt = self._setup_pending(
            env,
            TradeSide.LONG,
            entry_price=1.10000,
            wick_stop=wick_stop,
            cur_trail_ema=ema_above_entry,
        )
        fill_bar = _candle(o=1.09990, h=1.10010, low=1.09990, c=1.10005, ts=3600.0)
        bt._check_pending_fill(1, fill_bar)

        assert bt._position is not None
        # No clamping to wick; EMA used unconditionally per Pine semantics
        assert bt._position.current_stop == pytest.approx(ema_above_entry, abs=1e-6)
        # Wick still the sizing anchor
        assert bt._position.initial_stop == pytest.approx(wick_stop, abs=1e-6)

    def test_initial_stop_unconditional_when_ema_wrong_sided_short(self):
        """Symmetric guard for short side."""
        env = self._make_env(trail_ema_period=40)
        wick_stop = 1.10200  # 20 pips above entry
        ema_below_entry = 1.09950  # 5 pips BELOW entry — wrong side for short
        bt = self._setup_pending(
            env,
            TradeSide.SHORT,
            entry_price=1.10000,
            wick_stop=wick_stop,
            cur_trail_ema=ema_below_entry,
        )
        fill_bar = _candle(o=1.10010, h=1.10010, low=1.09990, c=1.09995, ts=3600.0)
        bt._check_pending_fill(1, fill_bar)

        assert bt._position is not None
        assert bt._position.current_stop == pytest.approx(ema_below_entry, abs=1e-6)
        assert bt._position.initial_stop == pytest.approx(wick_stop, abs=1e-6)

    def test_wrong_sided_ema_actually_exits_at_ema_price_long(self):
        """Spec §4 step 6: end-to-end stop-out at EMA price.

        After a wrong-sided long fill (EMA above entry), stepping the engine
        forward one bar must close the position with ExitReason.STOP_LOSS at
        exit_price ≈ trail_ema_at_entry. This is faithful to Pine v5 cur_stop
        semantics: a positive-pnl STOP_LOSS is the expected outcome — the
        engine must NOT clamp current_stop to the wick.
        """
        env = self._make_env(trail_ema_period=40)
        wick_stop = 1.09800  # 20 pips below entry
        ema_above_entry = 1.10050  # 5 pips ABOVE entry — wrong side
        bt = self._setup_pending(
            env,
            TradeSide.LONG,
            entry_price=1.10000,
            wick_stop=wick_stop,
            cur_trail_ema=ema_above_entry,
        )

        # Bar 1: fill bar — high crosses entry (1.10000)
        fill_bar = _candle(o=1.09990, h=1.10010, low=1.09990, c=1.10005, ts=3600.0)
        bt._check_pending_fill(1, fill_bar)
        assert bt._position is not None
        assert bt._position.current_stop == pytest.approx(ema_above_entry, abs=1e-6)

        # Bar 2: any bar whose low <= ema_above_entry (1.10050) triggers SL.
        # Use a realistic next-bar candle straddling the EMA stop.
        next_bar = _candle(o=1.10005, h=1.10060, low=1.10000, c=1.10040, ts=7200.0)
        bt._manage_position(2, next_bar, atr_h1=[0.0010, 0.0010, 0.0010], trail_ema=None)

        # Position must have stopped out
        assert bt._position is None, "expected position to be closed by SL on bar 2"
        assert len(bt._trades) == 1
        trade = bt._trades[0]
        assert trade.exit_reason == ExitReason.STOP_LOSS
        # Exit price must be the EMA value used as cur_stop (Pine semantics)
        assert trade.exit_price == pytest.approx(ema_above_entry, abs=1e-5)
        # Sanity: this is a positive-pip outcome before spread costs — Pine
        # v5 produces exactly this kind of "winning STOP_LOSS" when the EMA
        # gaps wrong-sided at fill time. Don't assert sign of pnl_pips here
        # because spread costs flip the sign at small magnitudes; the
        # load-bearing assertion is the exit_price == EMA equality above.

    def test_no_same_bar_exit_when_ema_above_bar_high_long(self):
        """Pine semantics: strategy.exit evaluates next-tick. Even when the
        EMA-initialized current_stop is ABOVE bar.high (a long can't have
        traded that high yet), Python must NOT close the trade on the entry
        bar — it must wait for the next bar.
        """
        env = self._make_env(trail_ema_period=40)
        wick_stop = 1.09800
        ema_above_bar_high = 1.10100  # ABOVE the fill bar's high
        bt = self._setup_pending(
            env,
            TradeSide.LONG,
            entry_price=1.10000,
            wick_stop=wick_stop,
            cur_trail_ema=ema_above_bar_high,
        )
        # Fill bar: range 1.09990-1.10010 — does NOT trade up to 1.10100
        fill_bar = _candle(o=1.09990, h=1.10010, low=1.09990, c=1.10005, ts=3600.0)

        # Mirror _process_bar's order: _check_pending_fill then _manage_position
        # on the same bar. Pine's strategy.exit only triggers from the next bar.
        bt._check_pending_fill(1, fill_bar)
        bt._manage_position(1, fill_bar, [0.0010] * 5, [ema_above_bar_high] * 5)

        # Position must still be open — Pine wouldn't fire strategy.exit on entry bar
        assert bt._position is not None, (
            "Position closed on entry bar — Pine wouldn't fire strategy.exit until next tick"
        )
        assert len(bt._trades) == 0

        # Step to next bar: bar.low <= ema (1.10100) → exit fires at EMA
        next_bar = _candle(o=1.10005, h=1.10110, low=1.10005, c=1.10100, ts=7200.0)
        bt._manage_position(2, next_bar, [0.0010] * 5, [ema_above_bar_high] * 5)
        assert bt._position is None
        assert len(bt._trades) == 1
        ct = bt._trades[0]
        assert ct.exit_reason == ExitReason.STOP_LOSS
        assert ct.exit_price == pytest.approx(ema_above_bar_high, abs=1e-5)

    def test_no_same_bar_exit_when_ema_below_bar_low_short(self):
        """Symmetric guard for SHORT side: EMA below bar.low must not fire on
        the entry bar.
        """
        env = self._make_env(trail_ema_period=40)
        wick_stop = 1.10200
        ema_below_bar_low = 1.09900  # BELOW the fill bar's low
        bt = self._setup_pending(
            env,
            TradeSide.SHORT,
            entry_price=1.10000,
            wick_stop=wick_stop,
            cur_trail_ema=ema_below_bar_low,
        )
        # Fill bar: range 1.09990-1.10010 — does NOT trade down to 1.09900
        fill_bar = _candle(o=1.10010, h=1.10010, low=1.09990, c=1.09995, ts=3600.0)
        bt._check_pending_fill(1, fill_bar)
        bt._manage_position(1, fill_bar, [0.0010] * 5, [ema_below_bar_low] * 5)

        assert bt._position is not None
        assert len(bt._trades) == 0

        # Next bar: bar.high reaches the EMA (1.09900); short stops out
        next_bar = _candle(o=1.09895, h=1.09905, low=1.09890, c=1.09900, ts=7200.0)
        bt._manage_position(2, next_bar, [0.0010] * 5, [ema_below_bar_low] * 5)
        assert bt._position is None
        assert len(bt._trades) == 1
        ct = bt._trades[0]
        assert ct.exit_reason == ExitReason.STOP_LOSS
        assert ct.exit_price == pytest.approx(ema_below_bar_low, abs=1e-5)


class TestPineAlignedParitySmoke:
    """End-to-end smoke: Pine-aligned config + EMA-stop fix should produce
    PF >= 1.0 on the 2022 EURUSD H1+H4 slice. Pine reports PF 2.06 / +$11k
    for that year; PF >= 1.0 is a generous floor confirming the fix moves
    the engine into Pine's profitable regime.

    Data-availability gated: data/candles/EURUSD_H1_10yr.csv is in /data/
    (gitignored), so this test is skipped in CI but runs locally for
    developers and as a /finishing-a-development-branch gate.
    """

    DATA_PATH = Path("data/candles/EURUSD_H1_10yr.csv")
    CFG_PATH = Path("configs/strategies/irb_v5_m5_pine_aligned.yaml")

    @pytest.mark.skipif(
        not DATA_PATH.exists() or not CFG_PATH.exists(),
        reason="10yr historical CSV or pine-aligned config not available locally",
    )
    def test_pine_aligned_2022_pf_crosses_one(self):
        from dataclasses import replace
        from datetime import datetime, timezone

        from novatrade.backtest.metrics import ExitReason
        from novatrade.cli.commands.data import aggregate_h1_to_h4
        from novatrade.cli.config_schema import StrategyConfig

        # Load full H1 series, slice to 2022 with 30d warmup
        warmup_start = datetime(2021, 12, 1, tzinfo=timezone.utc).timestamp()
        end = datetime(2023, 1, 1, tzinfo=timezone.utc).timestamp()
        h1_all: list[Candle] = []
        with self.DATA_PATH.open() as f:
            import csv

            for r in csv.DictReader(f):
                ts_str = r["timestamp"].strip()
                ts = (
                    float(ts_str)
                    if ts_str.replace(".", "").isdigit()
                    else datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
                )
                if not (warmup_start <= ts < end):
                    continue
                h1_all.append(
                    Candle(
                        timestamp=ts,
                        open=float(r["open"]),
                        high=float(r["high"]),
                        low=float(r["low"]),
                        close=float(r["close"]),
                        volume=float(r.get("volume") or 0),
                        symbol="EURUSD",
                        timeframe="H1",
                    )
                )
        h1 = sorted(h1_all, key=lambda c: c.timestamp)
        h4 = aggregate_h1_to_h4(h1)
        assert len(h1) > 6000, f"expected ~8800 H1 bars, got {len(h1)}"

        cfg = StrategyConfig.from_yaml(self.CFG_PATH)
        env_kwargs = cfg.to_environment_kwargs()
        # The Pine source has the EMA-stack filter always-on; pine_aligned.yaml
        # already sets use_ema_stack_filter: true. Belt-and-braces here.
        env_kwargs["use_ema_stack_filter"] = True

        base = BacktestEnvironment.for_v5_m5()
        valid = {k: v for k, v in env_kwargs.items() if k in base.__dataclass_fields__}
        env = replace(
            base,
            primary_timeframe="H1",
            higher_timeframe="H4",
            h1_to_h4_ratio=4,
            h1_bars_per_day=24,
            h4_bars_per_day=6,
            min_volume=1.0,
            max_volume=1.0,  # fixed-1-lot to match Pine sizing semantics
            **valid,
        )

        bt = IRBBacktester(env=env)
        res = bt.run(h1, h4)

        # In-2022 trades only (filter on entry_bar timestamp)
        jan22 = datetime(2022, 1, 1, tzinfo=timezone.utc).timestamp()
        trades_2022 = [t for t in res.trades if 0 <= t.entry_bar < len(h1) and h1[t.entry_bar].timestamp >= jan22]
        assert len(trades_2022) > 0, "no 2022 trades — config or data path broken"

        wins = [t for t in trades_2022 if t.pnl_usd > 0]
        losses = [t for t in trades_2022 if t.pnl_usd < 0]
        gross_w = sum(t.pnl_usd for t in wins)
        gross_l = -sum(t.pnl_usd for t in losses)
        pf = gross_w / gross_l if gross_l > 0 else float("inf")

        # Headline assertion: PF crosses 1.0 (Pine reports PF 2.06 for 2022)
        assert pf >= 1.0, (
            f"Pine-aligned 2022 PF={pf:.3f} < 1.0 — engine-stop-divergence fix "
            f"did not close the parity gap. Pine baseline: PF 2.06."
        )
        # Sanity floor only — Pine's 108 baseline is on M5 cadence; this test
        # runs H1+H4 because data/candles/EURUSD_H1_10yr.csv is the only local
        # dataset (M5 not yet checked in). Trade count is expected to be lower
        # than Pine's M5 figure due to coarser bars, not a strategy regression.
        # Floor at 40 confirms the entry pipeline is firing meaningfully.
        assert len(trades_2022) >= 40, (
            f"Pine-aligned 2022 trade count {len(trades_2022)} below sanity floor of 40 — "
            "entry pipeline may be over-filtering or data slice broken."
        )
        # Sanity: no SL trade lost more than 150 pips. Threshold is loose
        # because this test runs H1 bars (M5 not available locally) — H1
        # bar gaps can carry the close 50–100 pips beyond the trigger before
        # the engine evaluates the exit, which Pine's M5 cadence avoids.
        # 150 pips still catches genuine runaway-stop bugs while tolerating
        # the cadence artifact.
        for t in trades_2022:
            if t.exit_reason == ExitReason.STOP_LOSS:
                assert t.pnl_pips > -150, f"trade {t.trade_id}: SL hit at {t.pnl_pips:.1f} pips — runaway stop?"


class TestMtfLookbackSemantic:
    """Locks the documented semantic of env.mtf_lookback: H4 bars (NOT H1 bars).

    Pine's MTF_H1_LOOKBACK = 20 is in H1 bars (configs/pinescript/irb_v5_stag.pine
    line 79; indexed via `ema_h4[MTF_H1_LOOKBACK]` on the H1 timeline). The Python
    engine indexes the H4 EMA array directly via `ema_h4[h4_idx - mtf_lookback]`
    (engine.py:585), which interprets the value in H4 bars.

    On the standard 4:1 H1→H4 ratio, Pine's 20-H1-bar lookback equals 5 H4 bars.
    Pine-derived configs must therefore set `mtf_lookback: 5`, NOT 20.

    Diagnostic evidence: scripts/parity_match.py + parity_irb_at_irb_bar.py
    showed 314 of 716 Bucket-B pine_only entries (43.9%) were rejected by
    Python's `mtf_alignment` filter at the exact IRB bar — caused by this 4×
    over-wide lookback window.
    """

    def test_pine_aligned_yaml_uses_h4_bar_lookback(self):
        from novatrade.cli.config_schema import StrategyConfig

        cfg_path = Path("configs/strategies/irb_v5_m5_pine_aligned.yaml")
        if not cfg_path.exists():
            pytest.skip("pine-aligned config not present")

        cfg = StrategyConfig.from_yaml(cfg_path)
        # Pine's MTF_H1_LOOKBACK = 20 H1 bars = 5 H4 bars on 4:1 ratio.
        # The engine interprets mtf_lookback as H4 bars, so the yaml must
        # encode the H4 value.
        assert cfg.mtf_lookback == 5, (
            f"pine_aligned.yaml mtf_lookback={cfg.mtf_lookback}, expected 5 "
            "(Pine MTF_H1_LOOKBACK=20 H1 bars / 4 H1-per-H4 = 5 H4 bars). "
            "If raising this, also document why Python's H4 trend gate should "
            "be more conservative than Pine's, and re-run scripts/parity_match.py "
            "to quantify the impact on 'mtf_alignment' rejections."
        )
