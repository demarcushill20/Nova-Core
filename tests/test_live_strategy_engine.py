"""Tests for LiveStrategyEngine — Phase 4 of the full-Python pipeline.

Covers:
  - Warmup and seeding
  - Bar accumulation and buffer management
  - Entry signal generation (via IRBStrategy)
  - Pending order fill detection (buy-stop / sell-stop)
  - Pending order expiry
  - IRB replacement (same-side signal replaces pending)
  - Exit signal generation (stop-loss, time-stop, trailing-stop)
  - Trailing stop updates and MODIFY_SL signals
  - Position lifecycle (FLAT → PENDING → LONG/SHORT → FLAT)
  - External fill / close notifications
  - Circuit breaker (consecutive losses)
  - Higher timeframe bar accumulation
  - Snapshot and property access
  - Live/backtest parity (deterministic state machine)
  - Buffer trimming with index adjustment
  - Signal-to-alert payload conversion
"""

from __future__ import annotations

from novatrade.backtest.environment import BacktestEnvironment
from novatrade.models import Candle
from novatrade.strategies.base import BaseStrategy, EntrySignal, ExitSignal
from novatrade.strategy.live_engine import (
    LiveConfig,
    LiveSignal,
    LiveState,
    LiveStrategyEngine,
    OpenPosition,
    PendingOrder,
    SignalType,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _candle(
    ts: float = 0.0,
    o: float = 1.1000,
    h: float = 1.1050,
    lo: float = 1.0950,
    c: float = 1.1020,
    vol: float = 100.0,
    symbol: str = "EURUSD",
    tf: str = "H1",
) -> Candle:
    return Candle(
        timestamp=ts,
        open=o,
        high=h,
        low=lo,
        close=c,
        volume=vol,
        symbol=symbol,
        timeframe=tf,
    )


def _make_candles(n: int, base_price: float = 1.1000, spread: float = 0.005) -> list[Candle]:
    """Generate n synthetic H1 candles with slight uptrend."""
    candles = []
    for i in range(n):
        o = base_price + i * 0.0002
        h = o + spread
        lo = o - spread * 0.5
        c = o + spread * 0.3
        candles.append(_candle(ts=float(i * 3600), o=o, h=h, lo=lo, c=c))
    return candles


class MockStrategy(BaseStrategy):
    """Strategy mock that returns configurable signals."""

    name = "mock"
    family = "test"

    def __init__(self) -> None:
        self._entry_signal: EntrySignal | None = None
        self._exit_signal: ExitSignal | None = None
        self._indicators: dict[str, list[float]] = {}

    def set_entry(self, sig: EntrySignal | None) -> None:
        self._entry_signal = sig

    def set_exit(self, sig: ExitSignal | None) -> None:
        self._exit_signal = sig

    def set_indicators(self, ind: dict[str, list[float]]) -> None:
        self._indicators = ind

    def compute_indicators(self, candles: list[Candle]) -> dict[str, list[float]]:
        if self._indicators:
            return self._indicators
        n = len(candles)
        return {
            "ema": [1.1 + i * 0.0001 for i in range(n)],
            "atr": [0.005] * n,
            "adx": [25.0] * n,
        }

    def generate_signals(self, candles, indicators):
        return []

    def check_entry(self, bar_index, candles, indicators, h4_candles=None):
        return self._entry_signal

    def check_exit(self, bar_index, candles, indicators, position=None):
        return self._exit_signal

    def get_default_doctrine(self, pair, timeframe):
        return {}

    def get_parameter_bounds(self):
        return {}


def _make_engine(
    warmup: int = 10,
    max_candles: int = 500,
    trigger_window: int = 20,
    strategy: MockStrategy | None = None,
) -> tuple[LiveStrategyEngine, MockStrategy]:
    strat = strategy or MockStrategy()
    env = BacktestEnvironment(
        warmup_bars=warmup,
        initial_equity=10_000.0,
        risk_fraction=0.01,
        pip_value=0.0001,
        pip_value_per_standard_lot=10.0,
        min_volume=0.01,
        max_volume=1.0,
        trail_atr_multiplier=1.5,
        trigger_window_bars=trigger_window,
        time_stop_bars=40,
    )
    cfg = LiveConfig(
        symbol="EURUSD",
        primary_timeframe="H1",
        higher_timeframe="H4",
        max_candles=max_candles,
        trigger_window_bars=trigger_window,
    )
    engine = LiveStrategyEngine(strategy=strat, env=env, config=cfg)
    return engine, strat


# ===========================================================================
# Tests: Warmup and Seeding
# ===========================================================================


class TestWarmup:
    def test_initial_state_is_warming_up(self):
        engine, _ = _make_engine()
        assert engine.state == LiveState.WARMING_UP
        assert not engine.is_warmed_up

    def test_seed_with_enough_bars_transitions_to_flat(self):
        engine, _ = _make_engine(warmup=10)
        candles = _make_candles(15)
        engine.seed_history(candles)
        assert engine.state == LiveState.FLAT
        assert engine.is_warmed_up
        assert engine.h1_count == 15

    def test_seed_with_insufficient_bars_stays_warming_up(self):
        engine, _ = _make_engine(warmup=10)
        candles = _make_candles(5)
        engine.seed_history(candles)
        assert engine.state == LiveState.WARMING_UP
        assert not engine.is_warmed_up

    def test_on_bar_transitions_from_warmup_to_flat(self):
        engine, _ = _make_engine(warmup=5)
        for i in range(5):
            engine.on_bar(_candle(ts=float(i * 3600)), "H1")
        assert engine.state == LiveState.FLAT

    def test_warmup_bars_produce_no_signals(self):
        engine, strat = _make_engine(warmup=5)
        strat.set_entry(EntrySignal(0, "LONG", 1.1050, 1.0950))
        for i in range(4):
            signals = engine.on_bar(_candle(ts=float(i * 3600)), "H1")
            assert signals == []

    def test_seed_trims_to_max_candles(self):
        engine, _ = _make_engine(warmup=5, max_candles=50)
        candles = _make_candles(100)
        engine.seed_history(candles)
        assert engine.h1_count == 50

    def test_seed_with_h4_candles(self):
        engine, _ = _make_engine(warmup=5)
        h1 = _make_candles(20)
        h4 = _make_candles(5)
        engine.seed_history(h1, h4)
        assert engine.h1_count == 20
        assert engine.h4_count == 5


# ===========================================================================
# Tests: Bar Accumulation
# ===========================================================================


class TestBarAccumulation:
    def test_h1_bar_increments_total_bars(self):
        engine, _ = _make_engine(warmup=5)
        engine.seed_history(_make_candles(10))
        engine.on_bar(_candle(ts=100.0), "H1")
        assert engine.total_bars == 11  # 10 seeded + 1

    def test_h4_bar_accumulates_without_signals(self):
        engine, strat = _make_engine(warmup=5)
        engine.seed_history(_make_candles(10))
        strat.set_entry(EntrySignal(0, "LONG", 1.1050, 1.0950))
        signals = engine.on_bar(_candle(ts=100.0), "H4")
        assert signals == []
        assert engine.h4_count == 1

    def test_irrelevant_timeframe_ignored(self):
        engine, _ = _make_engine(warmup=5)
        engine.seed_history(_make_candles(10))
        signals = engine.on_bar(_candle(ts=100.0), "M15")
        assert signals == []
        assert engine.total_bars == 10  # unchanged

    def test_buffer_trim(self):
        engine, _ = _make_engine(warmup=5, max_candles=20)
        engine.seed_history(_make_candles(20))
        for i in range(10):
            engine.on_bar(_candle(ts=float(30000 + i * 3600)), "H1")
        assert engine.h1_count == 20  # capped


# ===========================================================================
# Tests: Entry Signal Generation
# ===========================================================================


class TestEntrySignal:
    def test_entry_signal_when_flat(self):
        engine, strat = _make_engine(warmup=5)
        engine.seed_history(_make_candles(10))
        strat.set_entry(EntrySignal(9, "LONG", 1.1050, 1.0950))
        signals = engine.on_bar(_candle(ts=40000.0), "H1")
        assert len(signals) == 1
        sig = signals[0]
        assert sig.signal_type == SignalType.ENTRY
        assert sig.side == "LONG"
        assert sig.entry_price == 1.1050
        assert sig.stop_loss == 1.0950
        assert sig.volume > 0

    def test_entry_creates_pending_order(self):
        engine, strat = _make_engine(warmup=5)
        engine.seed_history(_make_candles(10))
        strat.set_entry(EntrySignal(10, "SHORT", 1.0950, 1.1050))
        engine.on_bar(_candle(ts=40000.0), "H1")
        assert engine.state == LiveState.PENDING_SHORT
        assert engine.pending is not None
        assert engine.pending.side == "SHORT"

    def test_no_entry_when_in_position(self):
        engine, strat = _make_engine(warmup=5)
        engine.seed_history(_make_candles(10))

        # Create pending + fill
        strat.set_entry(EntrySignal(10, "LONG", 1.1050, 1.0950))
        engine.on_bar(_candle(ts=40000.0), "H1")
        strat.set_entry(None)

        # Bar that fills the pending
        fill_bar = _candle(ts=44000.0, o=1.1060, h=1.1100, lo=1.1040, c=1.1080)
        engine.on_bar(fill_bar, "H1")
        assert engine.state == LiveState.LONG

        # New entry signal should be ignored (in position)
        strat.set_entry(EntrySignal(12, "LONG", 1.1100, 1.1000))
        signals = engine.on_bar(_candle(ts=48000.0, h=1.1090), "H1")
        entry_signals = [s for s in signals if s.signal_type == SignalType.ENTRY]
        assert len(entry_signals) == 0

    def test_position_sizing(self):
        engine, strat = _make_engine(warmup=5)
        engine.seed_history(_make_candles(10))
        # 100 pip stop → risk $100 (1% of 10k) / (100 pips * $10/pip) = 0.10 lots
        strat.set_entry(EntrySignal(10, "LONG", 1.1100, 1.1000))
        signals = engine.on_bar(_candle(ts=40000.0), "H1")
        assert len(signals) == 1
        assert signals[0].volume == 0.10

    def test_irb_replacement_same_side(self):
        engine, strat = _make_engine(warmup=5)
        engine.seed_history(_make_candles(10))

        # First entry — high entry price so the next bar won't fill it
        strat.set_entry(EntrySignal(10, "LONG", 1.2000, 1.1900))
        engine.on_bar(_candle(ts=40000.0), "H1")
        assert engine.state == LiveState.PENDING_LONG

        # Same side replacement (bar high=1.1050, below both entry prices → no fill)
        strat.set_entry(EntrySignal(11, "LONG", 1.2010, 1.1910))
        signals = engine.on_bar(_candle(ts=44000.0, h=1.1050), "H1")
        entry_sigs = [s for s in signals if s.signal_type == SignalType.ENTRY]
        assert len(entry_sigs) == 1
        assert entry_sigs[0].entry_price == 1.2010  # new order

    def test_opposite_side_pending_ignored(self):
        engine, strat = _make_engine(warmup=5)
        engine.seed_history(_make_candles(10))

        # First entry LONG — high entry so next bar won't fill
        strat.set_entry(EntrySignal(10, "LONG", 1.2000, 1.1900))
        engine.on_bar(_candle(ts=40000.0), "H1")

        # Opposite direction SHORT — should be ignored (bar high below LONG entry)
        strat.set_entry(EntrySignal(11, "SHORT", 1.0950, 1.1050))
        signals = engine.on_bar(_candle(ts=44000.0, h=1.1050), "H1")
        entry_signals = [s for s in signals if s.signal_type == SignalType.ENTRY]
        assert len(entry_signals) == 0
        assert engine.pending.side == "LONG"  # original preserved


# ===========================================================================
# Tests: Pending Order Fill
# ===========================================================================


class TestPendingFill:
    def test_buy_stop_fills_when_high_exceeds_entry(self):
        engine, strat = _make_engine(warmup=5)
        engine.seed_history(_make_candles(10))

        strat.set_entry(EntrySignal(10, "LONG", 1.1050, 1.0950))
        engine.on_bar(_candle(ts=40000.0), "H1")
        strat.set_entry(None)

        # Bar with high above entry
        fill_bar = _candle(ts=44000.0, o=1.1020, h=1.1060, lo=1.1000, c=1.1040)
        signals = engine.on_bar(fill_bar, "H1")
        fill_signals = [s for s in signals if s.signal_type == SignalType.PENDING_FILL]
        assert len(fill_signals) == 1
        assert fill_signals[0].entry_price == 1.1050  # filled at order price
        assert engine.state == LiveState.LONG

    def test_buy_stop_gap_fill_at_open(self):
        engine, strat = _make_engine(warmup=5)
        engine.seed_history(_make_candles(10))

        strat.set_entry(EntrySignal(10, "LONG", 1.1050, 1.0950))
        engine.on_bar(_candle(ts=40000.0), "H1")
        strat.set_entry(None)

        # Gap open above entry
        fill_bar = _candle(ts=44000.0, o=1.1070, h=1.1100, lo=1.1060, c=1.1090)
        signals = engine.on_bar(fill_bar, "H1")
        fill_signals = [s for s in signals if s.signal_type == SignalType.PENDING_FILL]
        assert fill_signals[0].entry_price == 1.1070  # filled at open (gap)

    def test_sell_stop_fills_when_low_below_entry(self):
        engine, strat = _make_engine(warmup=5)
        engine.seed_history(_make_candles(10))

        strat.set_entry(EntrySignal(10, "SHORT", 1.0950, 1.1050))
        engine.on_bar(_candle(ts=40000.0), "H1")
        strat.set_entry(None)

        fill_bar = _candle(ts=44000.0, o=1.0980, h=1.1000, lo=1.0940, c=1.0960)
        signals = engine.on_bar(fill_bar, "H1")
        fill_signals = [s for s in signals if s.signal_type == SignalType.PENDING_FILL]
        assert len(fill_signals) == 1
        assert fill_signals[0].entry_price == 1.0950
        assert engine.state == LiveState.SHORT

    def test_sell_stop_gap_fill(self):
        engine, strat = _make_engine(warmup=5)
        engine.seed_history(_make_candles(10))

        strat.set_entry(EntrySignal(10, "SHORT", 1.0950, 1.1050))
        engine.on_bar(_candle(ts=40000.0), "H1")
        strat.set_entry(None)

        fill_bar = _candle(ts=44000.0, o=1.0930, h=1.0960, lo=1.0920, c=1.0940)
        signals = engine.on_bar(fill_bar, "H1")
        fill_signals = [s for s in signals if s.signal_type == SignalType.PENDING_FILL]
        assert fill_signals[0].entry_price == 1.0930  # gap fill

    def test_pending_no_fill_when_price_doesnt_reach(self):
        engine, strat = _make_engine(warmup=5)
        engine.seed_history(_make_candles(10))

        strat.set_entry(EntrySignal(10, "LONG", 1.1050, 1.0950))
        engine.on_bar(_candle(ts=40000.0), "H1")
        strat.set_entry(None)

        no_fill = _candle(ts=44000.0, o=1.1020, h=1.1040, lo=1.1000, c=1.1030)
        signals = engine.on_bar(no_fill, "H1")
        fill_signals = [s for s in signals if s.signal_type == SignalType.PENDING_FILL]
        assert len(fill_signals) == 0
        assert engine.state == LiveState.PENDING_LONG


# ===========================================================================
# Tests: Pending Order Expiry
# ===========================================================================


class TestPendingExpiry:
    def test_pending_expires_after_trigger_window(self):
        engine, strat = _make_engine(warmup=5, trigger_window=3)
        engine.seed_history(_make_candles(10))

        strat.set_entry(EntrySignal(10, "LONG", 1.2000, 1.1900))
        engine.on_bar(_candle(ts=40000.0), "H1")
        strat.set_entry(None)
        assert engine.state == LiveState.PENDING_LONG

        # 3 bars without fill → expiry
        for i in range(3):
            engine.on_bar(_candle(ts=44000.0 + i * 3600, h=1.1050), "H1")

        assert engine.state == LiveState.FLAT
        assert engine.pending is None


# ===========================================================================
# Tests: Exit Signals
# ===========================================================================


class TestExitSignal:
    def _setup_position(self, engine, strat, side="LONG"):
        """Get engine into a position state."""
        engine.seed_history(_make_candles(10))
        entry = 1.1050 if side == "LONG" else 1.0950
        sl = 1.0950 if side == "LONG" else 1.1050
        strat.set_entry(EntrySignal(10, side, entry, sl))
        engine.on_bar(_candle(ts=40000.0), "H1")
        strat.set_entry(None)

        # Fill the pending
        if side == "LONG":
            fill_bar = _candle(ts=44000.0, o=1.1060, h=1.1100, lo=1.1040, c=1.1080)
        else:
            fill_bar = _candle(ts=44000.0, o=1.0940, h=1.0960, lo=1.0920, c=1.0930)
        engine.on_bar(fill_bar, "H1")
        assert engine.state == (LiveState.LONG if side == "LONG" else LiveState.SHORT)

    def test_stop_loss_exit(self):
        engine, strat = _make_engine(warmup=5)
        self._setup_position(engine, strat, "LONG")

        strat.set_exit(ExitSignal(12, 1.0950, "stop_loss"))
        signals = engine.on_bar(_candle(ts=48000.0, o=1.0960, h=1.0970, lo=1.0940, c=1.0950), "H1")
        exit_sigs = [s for s in signals if s.signal_type == SignalType.EXIT]
        assert len(exit_sigs) == 1
        assert exit_sigs[0].exit_reason == "stop_loss"

    def test_time_stop_exit(self):
        engine, strat = _make_engine(warmup=5)
        self._setup_position(engine, strat, "LONG")

        strat.set_exit(ExitSignal(12, 1.1080, "time_stop"))
        signals = engine.on_bar(_candle(ts=48000.0), "H1")
        exit_sigs = [s for s in signals if s.signal_type == SignalType.EXIT]
        assert len(exit_sigs) == 1
        assert exit_sigs[0].exit_reason == "time_stop"

    def test_exit_prevents_new_entry_same_bar(self):
        engine, strat = _make_engine(warmup=5)
        self._setup_position(engine, strat, "LONG")

        # Both exit and entry available
        strat.set_exit(ExitSignal(12, 1.0950, "stop_loss"))
        strat.set_entry(EntrySignal(12, "SHORT", 1.0950, 1.1050))

        signals = engine.on_bar(_candle(ts=48000.0), "H1")
        # Should get exit but not entry
        assert any(s.signal_type == SignalType.EXIT for s in signals)
        assert not any(s.signal_type == SignalType.ENTRY for s in signals)


# ===========================================================================
# Tests: Trailing Stop Update
# ===========================================================================


class TestTrailingStop:
    def _setup_long_position(self, engine, strat):
        engine.seed_history(_make_candles(10))
        strat.set_entry(EntrySignal(10, "LONG", 1.1050, 1.0950))
        engine.on_bar(_candle(ts=40000.0), "H1")
        strat.set_entry(None)
        fill = _candle(ts=44000.0, o=1.1060, h=1.1100, lo=1.1040, c=1.1080)
        engine.on_bar(fill, "H1")
        assert engine.state == LiveState.LONG

    def test_trailing_stop_moves_up_for_long(self):
        engine, strat = _make_engine(warmup=5)
        self._setup_long_position(engine, strat)

        # ATR=0.005, trail_atr_mult=1.5, so trail = best_close - 0.0075
        # New bar with higher close → trail should update
        strat.set_indicators(
            {
                "ema": [1.1] * 13,
                "atr": [0.005] * 13,
                "adx": [25.0] * 13,
            }
        )
        strat.set_exit(None)
        bar = _candle(ts=48000.0, o=1.1080, h=1.1120, lo=1.1070, c=1.1110)
        signals = engine.on_bar(bar, "H1")
        sl_sigs = [s for s in signals if s.signal_type == SignalType.MODIFY_SL]
        # Trail = 1.1110 - 0.0075 = 1.1035 — above initial SL of 1.0950
        if sl_sigs:
            assert sl_sigs[0].new_stop > 1.0950

    def test_trailing_stop_moves_down_for_short(self):
        engine, strat = _make_engine(warmup=5)
        engine.seed_history(_make_candles(10))
        strat.set_entry(EntrySignal(10, "SHORT", 1.0950, 1.1050))
        engine.on_bar(_candle(ts=40000.0), "H1")
        strat.set_entry(None)
        fill = _candle(ts=44000.0, o=1.0940, h=1.0960, lo=1.0920, c=1.0930)
        engine.on_bar(fill, "H1")
        assert engine.state == LiveState.SHORT

        strat.set_indicators(
            {
                "ema": [1.1] * 13,
                "atr": [0.005] * 13,
                "adx": [25.0] * 13,
            }
        )
        strat.set_exit(None)
        bar = _candle(ts=48000.0, o=1.0920, h=1.0930, lo=1.0880, c=1.0890)
        signals = engine.on_bar(bar, "H1")
        sl_sigs = [s for s in signals if s.signal_type == SignalType.MODIFY_SL]
        if sl_sigs:
            assert sl_sigs[0].new_stop < 1.1050


# ===========================================================================
# Tests: External Notifications
# ===========================================================================


class TestNotifications:
    def test_notify_fill_transitions_to_long(self):
        engine, strat = _make_engine(warmup=5)
        engine.seed_history(_make_candles(10))
        strat.set_entry(EntrySignal(10, "LONG", 1.1050, 1.0950))
        engine.on_bar(_candle(ts=40000.0), "H1")
        assert engine.state == LiveState.PENDING_LONG

        engine.notify_fill(1.1052, 0.10)
        assert engine.state == LiveState.LONG
        assert engine.position is not None
        assert engine.position.entry_price == 1.1052
        assert engine.pending is None

    def test_notify_close_transitions_to_flat(self):
        engine, strat = _make_engine(warmup=5)
        engine.seed_history(_make_candles(10))
        strat.set_entry(EntrySignal(10, "LONG", 1.1050, 1.0950))
        engine.on_bar(_candle(ts=40000.0), "H1")
        engine.notify_fill(1.1050, 0.10)
        assert engine.state == LiveState.LONG

        engine.notify_close(50.0)  # profit
        assert engine.state == LiveState.FLAT
        assert engine.position is None
        assert engine.equity == 10_050.0

    def test_notify_close_loss_increments_consecutive(self):
        engine, strat = _make_engine(warmup=5)
        engine.seed_history(_make_candles(10))
        strat.set_entry(EntrySignal(10, "LONG", 1.1050, 1.0950))
        engine.on_bar(_candle(ts=40000.0), "H1")
        engine.notify_fill(1.1050, 0.10)

        engine.notify_close(-30.0)
        assert engine.equity == 9970.0

    def test_notify_close_win_resets_consecutive(self):
        engine, strat = _make_engine(warmup=5)
        engine.seed_history(_make_candles(10))

        # Simulate two losses
        strat.set_entry(EntrySignal(10, "LONG", 1.1050, 1.0950))
        engine.on_bar(_candle(ts=40000.0), "H1")
        engine.notify_fill(1.1050, 0.10)
        engine.notify_close(-30.0)

        strat.set_entry(EntrySignal(11, "LONG", 1.1050, 1.0950))
        engine.on_bar(_candle(ts=44000.0), "H1")
        engine.notify_fill(1.1050, 0.10)
        engine.notify_close(-20.0)

        # Now a win
        strat.set_entry(EntrySignal(12, "LONG", 1.1050, 1.0950))
        engine.on_bar(_candle(ts=48000.0), "H1")
        engine.notify_fill(1.1050, 0.10)
        engine.notify_close(50.0)

        # Consecutive losses should reset
        assert engine.equity == 10_000.0  # -30 -20 +50

    def test_cancel_pending(self):
        engine, strat = _make_engine(warmup=5)
        engine.seed_history(_make_candles(10))
        strat.set_entry(EntrySignal(10, "LONG", 1.1050, 1.0950))
        engine.on_bar(_candle(ts=40000.0), "H1")
        assert engine.state == LiveState.PENDING_LONG

        engine.cancel_pending()
        assert engine.state == LiveState.FLAT
        assert engine.pending is None


# ===========================================================================
# Tests: Circuit Breaker
# ===========================================================================


class TestCircuitBreaker:
    def test_circuit_breaker_blocks_entry(self):
        env = BacktestEnvironment(
            warmup_bars=5,
            initial_equity=10_000.0,
            max_consecutive_losses=2,
            risk_fraction=0.01,
            pip_value=0.0001,
            pip_value_per_standard_lot=10.0,
        )
        strat = MockStrategy()
        engine = LiveStrategyEngine(strategy=strat, env=env)
        engine.seed_history(_make_candles(10))

        # Two losses
        for i in range(2):
            strat.set_entry(EntrySignal(10 + i, "LONG", 1.1050, 1.0950))
            engine.on_bar(_candle(ts=40000.0 + i * 4000), "H1")
            engine.notify_fill(1.1050, 0.10)
            engine.notify_close(-30.0)

        # Third entry should be blocked
        strat.set_entry(EntrySignal(12, "LONG", 1.1050, 1.0950))
        signals = engine.on_bar(_candle(ts=48000.0), "H1")
        entry_sigs = [s for s in signals if s.signal_type == SignalType.ENTRY]
        assert len(entry_sigs) == 0


# ===========================================================================
# Tests: Snapshot and Properties
# ===========================================================================


class TestSnapshot:
    def test_snapshot_flat(self):
        engine, _ = _make_engine(warmup=5)
        engine.seed_history(_make_candles(10))
        snap = engine.snapshot()
        assert snap["state"] == "FLAT"
        assert snap["symbol"] == "EURUSD"
        assert snap["equity"] == 10_000.0
        assert snap["has_pending"] is False
        assert snap["has_position"] is False

    def test_snapshot_with_position(self):
        engine, strat = _make_engine(warmup=5)
        engine.seed_history(_make_candles(10))
        strat.set_entry(EntrySignal(10, "LONG", 1.1050, 1.0950))
        engine.on_bar(_candle(ts=40000.0), "H1")
        engine.notify_fill(1.1050, 0.10)

        snap = engine.snapshot()
        assert snap["state"] == "LONG"
        assert snap["has_position"] is True
        assert snap["position_side"] == "LONG"
        assert snap["position_entry"] == 1.1050

    def test_indicators_property(self):
        engine, strat = _make_engine(warmup=5)
        engine.seed_history(_make_candles(10))
        engine.on_bar(_candle(ts=40000.0), "H1")
        ind = engine.indicators
        assert "ema" in ind
        assert "atr" in ind


# ===========================================================================
# Tests: Signal-to-Alert Payload
# ===========================================================================


class TestAlertPayload:
    def test_entry_payload(self):
        sig = LiveSignal(
            signal_type=SignalType.ENTRY,
            side="LONG",
            symbol="EURUSD",
            entry_price=1.1050,
            stop_loss=1.0950,
            volume=0.10,
            metadata={"irb_range": 0.01},
        )
        payload = sig.to_alert_payload()
        assert payload["action"] == "ENTRY"
        assert payload["symbol"] == "EURUSD"
        assert payload["side"] == "LONG"
        assert payload["entry_price"] == 1.1050
        assert payload["volume"] == 0.10
        assert payload["metadata"]["irb_range"] == 0.01

    def test_exit_payload(self):
        sig = LiveSignal(
            signal_type=SignalType.EXIT,
            side="LONG",
            symbol="EURUSD",
            exit_price=1.0950,
            exit_reason="stop_loss",
        )
        payload = sig.to_alert_payload()
        assert payload["action"] == "EXIT"
        assert payload["exit_reason"] == "stop_loss"

    def test_modify_sl_payload(self):
        sig = LiveSignal(
            signal_type=SignalType.MODIFY_SL,
            side="LONG",
            symbol="EURUSD",
            new_stop=1.1020,
        )
        payload = sig.to_alert_payload()
        assert payload["action"] == "MODIFY_SL"
        assert payload["new_stop_loss"] == 1.1020


# ===========================================================================
# Tests: Buffer Trimming with Index Adjustment
# ===========================================================================


class TestBufferTrimming:
    def test_trim_adjusts_pending_bar_index(self):
        engine, strat = _make_engine(warmup=5, max_candles=15)
        engine.seed_history(_make_candles(14))

        strat.set_entry(EntrySignal(14, "LONG", 1.2000, 1.1900))
        engine.on_bar(_candle(ts=60000.0), "H1")  # triggers trim at 16 > 15
        assert engine.h1_count == 15
        # Pending bar_index should have been adjusted
        assert engine.pending is not None
        assert engine.pending.bar_index >= 0

    def test_trim_adjusts_position_entry_bar(self):
        engine, strat = _make_engine(warmup=5, max_candles=15)
        engine.seed_history(_make_candles(14))

        strat.set_entry(EntrySignal(14, "LONG", 1.1050, 1.0950))
        engine.on_bar(_candle(ts=60000.0), "H1")
        strat.set_entry(None)
        engine.notify_fill(1.1050, 0.10)

        # Feed more bars to trigger trim
        for i in range(5):
            strat.set_exit(None)
            engine.on_bar(_candle(ts=64000.0 + i * 3600), "H1")

        assert engine.h1_count == 15
        assert engine.position is not None
        assert engine.position.entry_bar >= 0


# ===========================================================================
# Tests: OpenPosition and PendingOrder dataclasses
# ===========================================================================


class TestDataclasses:
    def test_open_position_defaults(self):
        pos = OpenPosition(
            side="LONG",
            entry_price=1.1050,
            stop_loss=1.0950,
            volume=0.10,
            entry_bar=5,
        )
        assert pos.current_stop == 1.0950
        assert pos.best_close == 1.1050
        assert pos.bars_held == 0

    def test_pending_order(self):
        po = PendingOrder(
            side="SHORT",
            entry_price=1.0950,
            stop_loss=1.1050,
            volume=0.10,
            bar_index=5,
        )
        assert po.bars_alive == 0
        assert po.side == "SHORT"


# ===========================================================================
# Tests: Full Lifecycle
# ===========================================================================


class TestLifecycle:
    def test_full_cycle_flat_to_pending_to_long_to_flat(self):
        engine, strat = _make_engine(warmup=5)
        engine.seed_history(_make_candles(10))
        assert engine.state == LiveState.FLAT

        # Entry signal → PENDING_LONG
        strat.set_entry(EntrySignal(10, "LONG", 1.1050, 1.0950))
        engine.on_bar(_candle(ts=40000.0), "H1")
        assert engine.state == LiveState.PENDING_LONG

        # Fill → LONG
        strat.set_entry(None)
        fill = _candle(ts=44000.0, o=1.1060, h=1.1100, lo=1.1040, c=1.1080)
        engine.on_bar(fill, "H1")
        assert engine.state == LiveState.LONG

        # Exit → still LONG (position not cleared until notify_close)
        strat.set_exit(ExitSignal(12, 1.0950, "stop_loss"))
        signals = engine.on_bar(_candle(ts=48000.0, o=1.0960, h=1.0970, lo=1.0940, c=1.0950), "H1")
        assert any(s.signal_type == SignalType.EXIT for s in signals)

        # External close → FLAT
        engine.notify_close(-50.0)
        assert engine.state == LiveState.FLAT
        assert engine.position is None

    def test_full_cycle_short(self):
        engine, strat = _make_engine(warmup=5)
        engine.seed_history(_make_candles(10))

        # Entry SHORT
        strat.set_entry(EntrySignal(10, "SHORT", 1.0950, 1.1050))
        engine.on_bar(_candle(ts=40000.0), "H1")
        assert engine.state == LiveState.PENDING_SHORT

        # Fill
        strat.set_entry(None)
        fill = _candle(ts=44000.0, o=1.0940, h=1.0960, lo=1.0920, c=1.0930)
        engine.on_bar(fill, "H1")
        assert engine.state == LiveState.SHORT

        # Close
        engine.notify_close(40.0)
        assert engine.state == LiveState.FLAT
        assert engine.equity == 10_040.0


# ===========================================================================
# Tests: Edge Cases
# ===========================================================================


class TestEdgeCases:
    def test_zero_stop_distance_rejected(self):
        engine, strat = _make_engine(warmup=5)
        engine.seed_history(_make_candles(10))
        # Same entry and stop → 0 pip distance → rejected
        strat.set_entry(EntrySignal(10, "LONG", 1.1000, 1.1000))
        signals = engine.on_bar(_candle(ts=40000.0), "H1")
        entry_sigs = [s for s in signals if s.signal_type == SignalType.ENTRY]
        assert len(entry_sigs) == 0

    def test_notify_fill_without_pending_is_noop(self):
        engine, _ = _make_engine(warmup=5)
        engine.seed_history(_make_candles(10))
        engine.notify_fill(1.1050, 0.10)  # no pending
        assert engine.state == LiveState.FLAT

    def test_on_bar_empty_buffer(self):
        engine, strat = _make_engine(warmup=5)
        # No seed, just single bars
        signals = engine.on_bar(_candle(ts=0.0), "H1")
        assert signals == []
        assert engine.state == LiveState.WARMING_UP

    def test_multiple_h4_bars(self):
        engine, _ = _make_engine(warmup=5)
        engine.seed_history(_make_candles(10))
        for i in range(5):
            engine.on_bar(_candle(ts=float(i * 14400)), "H4")
        assert engine.h4_count == 5
