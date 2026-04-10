"""V5 live strategy parity tests against the vault Python reference.

These tests validate that the live strategy wrapper (``novatrade/strategies/irb.py``)
plus the live engine (``novatrade/strategy/live_engine.py``) implement the V5
"Rob Hoffman IRB v5 - Relaxed Reliable Build (5min Champion)" strategy
identically to the canonical Python reference stored in the operator vault at
``nova-vault/100-trading-strategies/Rob Hoffman IRB v5 – Relaxed Reliable Build (Python) 1.md``.

These tests are deliberately self-contained and do not depend on the vault
file being present at test time — they encode the *behavioural contracts*
extracted from the reference (V5 trend filter, partial-at-1R, breakeven
move, ATR runner trail, time stop, max trades/day, cooldown bars, pending
revalidation).

Run with::

    pytest tests/test_v5_live_parity.py -v
"""

from __future__ import annotations

from typing import Any

from novatrade.backtest.environment import BacktestEnvironment
from novatrade.models import Candle
from novatrade.strategies.irb import IRBStrategy
from novatrade.strategy.live_engine import (
    LiveConfig,
    LiveState,
    LiveStrategyEngine,
    SignalType,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _v5_env(**overrides: Any) -> BacktestEnvironment:
    """Build a BacktestEnvironment matching the V5 M5 champion config.

    Mirrors ``configs/strategies/irb_v5_m5_champion.yaml``. Any overrides
    are applied last so individual tests can tune specific knobs.
    """
    base: dict[str, Any] = dict(
        # v5 trend filter
        use_simple_trend_filter=True,
        ema_period=20,
        ema_fast_period=20,
        ema_slow_period=50,
        ema_slope_lookback=5,
        # IRB geometry
        irb_threshold=0.45,
        # ATR / signal range
        atr_period=14,
        adx_period=14,
        adx_threshold=0.0,
        overextension_threshold=2.5,
        min_signal_atr_mult=0.0,
        # Trade management
        partial_exit_enabled=True,
        partial_exit_pct=50.0,
        partial_r_target=1.0,
        breakeven_r=1.0,
        trail_atr_multiplier=2.0,
        time_stop_bars=20,
        # Frequency controls
        max_trades_per_day=3,
        cooldown_bars=1,
        revalidate_pending=True,
        # MTF
        mtf_lookback=1,
        # Other v4 features off
        use_volatility_filter=False,
        use_ema_stack_filter=False,
        ema_confirm_bars=0,
        max_consecutive_losses=0,
        warmup_bars=60,
        # Sizing — keep simple for parity tests
        initial_equity=100_000.0,
        risk_fraction=0.01,
    )
    base.update(overrides)
    return BacktestEnvironment(**base)


def _candle(
    ts: float,
    o: float,
    h: float,
    lo: float,
    c: float,
    *,
    timeframe: str = "M5",
    symbol: str = "EURUSD",
) -> Candle:
    return Candle(
        timestamp=ts,
        open=o,
        high=h,
        low=lo,
        close=c,
        volume=100.0,
        symbol=symbol,
        timeframe=timeframe,
    )


_BAR_RNG = 0.00020  # roughly the ATR of the synthetic warmup data


def _bullish_warmup(n: int = 80, start: float = 1.1000, step: float = 0.00010) -> list[Candle]:
    """Generate ``n`` bullish M5 candles. Each bar advances by ``step``.

    Bar range is held near _BAR_RNG so the IRB-eligible bars constructed by
    ``_bullish_irb_bar`` stay inside the 2.5x ATR overextension limit.
    """
    out = []
    half = _BAR_RNG / 2
    for i in range(n):
        o = start + i * step
        c = o + step * 0.6
        h = max(o, c) + half
        lo = min(o, c) - half
        out.append(_candle(ts=float(i * 300), o=o, h=h, lo=lo, c=c))
    return out


def _bearish_warmup(n: int = 80, start: float = 1.2000, step: float = 0.00010) -> list[Candle]:
    out = []
    half = _BAR_RNG / 2
    for i in range(n):
        o = start - i * step
        c = o - step * 0.6
        h = max(o, c) + half
        lo = min(o, c) - half
        out.append(_candle(ts=float(i * 300), o=o, h=h, lo=lo, c=c))
    return out


def _bullish_irb_bar(prev_close: float, atr_proxy: float, ts: float) -> Candle:
    """Construct a bullish IRB candle (open & close in lower 45% of range).

    The vault Pine code defines:
        upThreshold = high - bar_range * 0.45
        bullIRB = open <= upThreshold and close <= upThreshold
    i.e. both open and close sit in the lower 55% of the bar's range.

    The bar's range is sized at 2x the ATR proxy so it stays under the
    2.5x overextension threshold while still being meaningfully larger
    than typical warmup bars.
    """
    rng = max(atr_proxy * 2.0, _BAR_RNG * 2.0)
    lo = prev_close - rng * 0.55
    hi = prev_close + rng * 0.45
    # open and close in lower zone (below upThreshold = hi - 0.45*rng)
    o = lo + rng * 0.05
    c = lo + rng * 0.15
    return _candle(ts=ts, o=o, h=hi, lo=lo, c=c)


def _bearish_irb_bar(prev_close: float, atr_proxy: float, ts: float) -> Candle:
    rng = max(atr_proxy * 2.0, _BAR_RNG * 2.0)
    hi = prev_close + rng * 0.55
    lo = prev_close - rng * 0.45
    # open and close in upper zone (above downThreshold = lo + 0.45*rng)
    o = hi - rng * 0.05
    c = hi - rng * 0.15
    return _candle(ts=ts, o=o, h=hi, lo=lo, c=c)


def _make_engine(env: BacktestEnvironment) -> LiveStrategyEngine:
    strat = IRBStrategy(env)
    cfg = LiveConfig(symbol="EURUSD", primary_timeframe="M5", higher_timeframe="H1")
    return LiveStrategyEngine(strat, env, config=cfg)


# ---------------------------------------------------------------------------
# 1. Indicator parity
# ---------------------------------------------------------------------------


def test_v5_compute_indicators_includes_ema_fast_and_ema_slow():
    """compute_indicators must compute ema_fast/ema_slow when V5 mode is on.

    The vault reference uses EMA20 + EMA50 for the simple trend check.
    Without these arrays the V5 trend filter has no inputs and signals
    would silently never fire.
    """
    env = _v5_env()
    strategy = IRBStrategy(env)
    candles = _bullish_warmup(80)

    indicators = strategy.compute_indicators(candles)

    assert "ema_fast" in indicators
    assert "ema_slow" in indicators
    assert len(indicators["ema_fast"]) == len(candles)
    assert len(indicators["ema_slow"]) == len(candles)

    # Bullish series → EMA20 should be above EMA50 once warmed up
    assert indicators["ema_fast"][-1] > indicators["ema_slow"][-1]


def test_v4_compute_indicators_does_not_break_when_v5_disabled():
    """V4 callers must not see ema_fast/ema_slow keys (they don't expect them)."""
    env = BacktestEnvironment(use_simple_trend_filter=False, warmup_bars=60)
    strategy = IRBStrategy(env)
    candles = _bullish_warmup(80)

    indicators = strategy.compute_indicators(candles)

    assert "ema" in indicators
    assert "atr" in indicators
    assert "adx" in indicators
    assert "ema_fast" not in indicators


# ---------------------------------------------------------------------------
# 2. V5 trend filter behaviour
# ---------------------------------------------------------------------------


def test_v5_trend_filter_skips_adx():
    """In V5 mode, ADX < adx_threshold must NOT block signals.

    The vault Pine reference has no ADX filter; its only trend gate is
    EMA20 > EMA50 + EMA20 rising. The live wrapper must mirror that.
    """
    env = _v5_env(adx_threshold=99.0)  # impossibly high ADX bar
    strategy = IRBStrategy(env)

    # Build a buffer with a final bullish IRB bar
    warmup = _bullish_warmup(80)
    irb_bar = _bullish_irb_bar(prev_close=warmup[-1].close, atr_proxy=_BAR_RNG, ts=80 * 300)
    candles = warmup + [irb_bar]

    indicators = strategy.compute_indicators(candles)
    sig = strategy.check_entry(len(candles) - 1, candles, indicators, h4_candles=None)

    # If ADX were checked, this would return None; V5 mode bypasses ADX
    assert sig is not None
    assert sig.side == "LONG"


def test_v5_trend_filter_blocks_when_ema_fast_below_ema_slow():
    """V5 LONG must require EMA20 > EMA50."""
    env = _v5_env()
    strategy = IRBStrategy(env)

    warmup = _bearish_warmup(80)  # downtrend → ema_fast < ema_slow
    irb_bar = _bullish_irb_bar(prev_close=warmup[-1].close, atr_proxy=_BAR_RNG, ts=80 * 300)
    candles = warmup + [irb_bar]

    indicators = strategy.compute_indicators(candles)
    sig = strategy.check_entry(len(candles) - 1, candles, indicators, h4_candles=None)

    assert sig is None  # bullish IRB rejected because trend is bearish


def test_v5_trend_filter_short_signal():
    """V5 SHORT requires EMA20 < EMA50 + EMA20 falling + bearish IRB."""
    env = _v5_env()
    strategy = IRBStrategy(env)

    warmup = _bearish_warmup(80)
    irb_bar = _bearish_irb_bar(prev_close=warmup[-1].close, atr_proxy=_BAR_RNG, ts=80 * 300)
    candles = warmup + [irb_bar]

    indicators = strategy.compute_indicators(candles)
    sig = strategy.check_entry(len(candles) - 1, candles, indicators, h4_candles=None)

    assert sig is not None
    assert sig.side == "SHORT"


# ---------------------------------------------------------------------------
# 3. HTF EMA slope filter
# ---------------------------------------------------------------------------


def test_v5_htf_filter_blocks_long_when_htf_falling():
    """When HTF EMA is falling, V5 longs must be blocked even if local trend is up."""
    env = _v5_env()
    strategy = IRBStrategy(env)

    warmup = _bullish_warmup(80)
    irb_bar = _bullish_irb_bar(prev_close=warmup[-1].close, atr_proxy=_BAR_RNG, ts=80 * 300)
    candles = warmup + [irb_bar]

    # HTF candles: strongly DOWNTREND so HTF EMA is falling
    htf_candles = _bearish_warmup(40, start=1.5000, step=0.0010)

    indicators = strategy.compute_indicators(candles)
    sig = strategy.check_entry(len(candles) - 1, candles, indicators, h4_candles=htf_candles)

    assert sig is None  # HTF is falling → long rejected


def test_v5_htf_filter_passes_long_when_htf_rising():
    env = _v5_env()
    strategy = IRBStrategy(env)

    warmup = _bullish_warmup(80)
    irb_bar = _bullish_irb_bar(prev_close=warmup[-1].close, atr_proxy=_BAR_RNG, ts=80 * 300)
    candles = warmup + [irb_bar]

    # HTF candles: rising
    htf_candles = _bullish_warmup(40, start=1.0500, step=0.0010)

    indicators = strategy.compute_indicators(candles)
    sig = strategy.check_entry(len(candles) - 1, candles, indicators, h4_candles=htf_candles)

    assert sig is not None
    assert sig.side == "LONG"


# ---------------------------------------------------------------------------
# 4. v5 frequency controls
# ---------------------------------------------------------------------------


def test_v5_max_trades_per_day_blocks_after_limit():
    """When max_trades_per_day is reached, no further entries this UTC day."""
    env = _v5_env(max_trades_per_day=2)
    engine = _make_engine(env)

    # Seed warmup
    engine.seed_history(_bullish_warmup(80))

    # Manually mark trades_today as having hit the limit
    engine._trades_today = 2
    from datetime import datetime, timezone

    engine._current_day_key = datetime.fromtimestamp(0, tz=timezone.utc).date()

    # Feed an IRB-eligible bar
    last_close = engine._h1_candles[-1].close
    irb_bar = _bullish_irb_bar(prev_close=last_close, atr_proxy=_BAR_RNG, ts=300.0)
    signals = engine.on_bar(irb_bar, "M5")

    entry_signals = [s for s in signals if s.signal_type == SignalType.ENTRY]
    assert len(entry_signals) == 0  # blocked by max_trades_per_day


def test_v5_cooldown_blocks_immediate_reentry():
    """After going flat, no new entries within cooldown_bars."""
    env = _v5_env(cooldown_bars=3)
    engine = _make_engine(env)
    engine.seed_history(_bullish_warmup(80))

    # Simulate a recent flatten
    engine._last_flat_bar = len(engine._h1_candles) - 1

    last_close = engine._h1_candles[-1].close
    irb_bar = _bullish_irb_bar(prev_close=last_close, atr_proxy=_BAR_RNG, ts=300.0)
    signals = engine.on_bar(irb_bar, "M5")

    entry_signals = [s for s in signals if s.signal_type == SignalType.ENTRY]
    assert len(entry_signals) == 0


def test_v5_day_boundary_resets_trades_today():
    """When a new UTC day arrives, _trades_today resets to 0."""
    env = _v5_env(max_trades_per_day=2)
    engine = _make_engine(env)
    engine.seed_history(_bullish_warmup(80))

    engine._trades_today = 2
    from datetime import datetime, timezone

    engine._current_day_key = datetime.fromtimestamp(0, tz=timezone.utc).date()

    # Bar timestamped on the next day
    next_day_ts = 86400.0 * 2  # day 2 UTC
    next_bar = _candle(
        ts=next_day_ts,
        o=engine._h1_candles[-1].close,
        h=engine._h1_candles[-1].close + 0.0001,
        lo=engine._h1_candles[-1].close - 0.0001,
        c=engine._h1_candles[-1].close + 0.00005,
    )
    engine.on_bar(next_bar, "M5")

    assert engine._trades_today == 0


# ---------------------------------------------------------------------------
# 5. v5 trade management
# ---------------------------------------------------------------------------


def test_v5_open_position_initializes_v5_fields():
    """When a V5 entry fills, the OpenPosition must seed initial_stop, tranches, and watermarks."""
    env = _v5_env(partial_exit_enabled=True, partial_exit_pct=50.0)
    engine = _make_engine(env)
    engine.seed_history(_bullish_warmup(80))

    # Manually inject a pending order then simulate fill
    from novatrade.strategy.live_engine import PendingOrder

    engine._pending = PendingOrder(
        side="LONG",
        entry_price=1.1500,
        stop_loss=1.1480,
        volume=1.00,
        bar_index=len(engine._h1_candles) - 1,
    )
    engine._state = LiveState.PENDING_LONG

    # Feed a bar that crosses the entry price
    fill_bar = _candle(ts=99000.0, o=1.1495, h=1.1510, lo=1.1490, c=1.1505)
    engine.on_bar(fill_bar, "M5")

    pos = engine._position
    assert pos is not None
    assert pos.initial_stop == 1.1480
    assert pos.initial_volume == 1.00
    assert pos.tp_qty_open == 0.50  # 50% of 1.00
    assert pos.runner_qty_open == 0.50
    assert not pos.partial_taken
    assert not pos.breakeven_active
    assert pos.highest_since_entry == 1.1510  # bar high
    assert pos.lowest_since_entry == 1.1490  # bar low


def test_v5_partial_exit_at_1r_emits_partial_exit_signal():
    """When price reaches entry + 1R, the engine must emit a PARTIAL_EXIT for tp_qty."""
    env = _v5_env(partial_exit_enabled=True, partial_exit_pct=50.0, partial_r_target=1.0)
    engine = _make_engine(env)
    engine.seed_history(_bullish_warmup(80))

    # Inject filled position
    from novatrade.strategy.live_engine import OpenPosition

    entry_price = 1.1500
    initial_stop = 1.1480
    risk = entry_price - initial_stop  # 0.0020
    tp_target = entry_price + risk  # 1.1520

    engine._position = OpenPosition(
        side="LONG",
        entry_price=entry_price,
        stop_loss=initial_stop,
        volume=1.00,
        entry_bar=len(engine._h1_candles) - 1,
        initial_stop=initial_stop,
        initial_volume=1.00,
        tp_qty_open=0.50,
        runner_qty_open=0.50,
        highest_since_entry=entry_price,
        lowest_since_entry=entry_price,
    )
    engine._state = LiveState.LONG

    # Bar that hits the partial TP target
    bar = _candle(ts=99000.0, o=entry_price, h=tp_target + 0.0002, lo=entry_price - 0.0001, c=tp_target)
    signals = engine.on_bar(bar, "M5")

    partials = [s for s in signals if s.signal_type == SignalType.PARTIAL_EXIT]
    assert len(partials) == 1
    assert partials[0].volume == 0.50
    assert partials[0].exit_reason == "partial_tp"
    assert engine._position.partial_taken is True
    assert engine._position.tp_qty_open == 0.0


def test_v5_breakeven_activates_after_1r_advance():
    """After price advances by 1R, breakeven_active flips and runner stop tightens."""
    env = _v5_env(partial_exit_enabled=False, breakeven_r=1.0, trail_atr_multiplier=2.0)
    engine = _make_engine(env)
    engine.seed_history(_bullish_warmup(80))

    from novatrade.strategy.live_engine import OpenPosition

    entry_price = 1.1500
    initial_stop = 1.1480
    risk = entry_price - initial_stop  # 0.0020
    be_trigger = entry_price + risk  # 1.1520

    engine._position = OpenPosition(
        side="LONG",
        entry_price=entry_price,
        stop_loss=initial_stop,
        volume=1.00,
        entry_bar=len(engine._h1_candles) - 1,
        initial_stop=initial_stop,
        initial_volume=1.00,
        tp_qty_open=0.0,
        runner_qty_open=1.00,
        highest_since_entry=entry_price,
        lowest_since_entry=entry_price,
    )
    engine._state = LiveState.LONG

    # Bar that hits be_trigger
    bar = _candle(ts=99000.0, o=entry_price, h=be_trigger + 0.0001, lo=entry_price - 0.0001, c=be_trigger)
    engine.on_bar(bar, "M5")

    assert engine._position.breakeven_active is True


def test_v5_time_stop_emits_exit_at_max_bars():
    """Time stop fires at time_stop_bars regardless of price."""
    env = _v5_env(time_stop_bars=5, partial_exit_enabled=False)
    engine = _make_engine(env)
    engine.seed_history(_bullish_warmup(80))

    from novatrade.strategy.live_engine import OpenPosition

    engine._position = OpenPosition(
        side="LONG",
        entry_price=1.1500,
        stop_loss=1.1480,
        volume=1.00,
        entry_bar=len(engine._h1_candles) - 1,
        initial_stop=1.1480,
        initial_volume=1.00,
        tp_qty_open=0.0,
        runner_qty_open=1.00,
        highest_since_entry=1.1505,
        lowest_since_entry=1.1495,
        bars_held=4,  # one more bar will hit time stop
    )
    engine._state = LiveState.LONG

    # Neutral bar — no SL hit, no partial hit
    bar = _candle(ts=99000.0, o=1.1500, h=1.1502, lo=1.1498, c=1.1500)
    signals = engine.on_bar(bar, "M5")

    exits = [s for s in signals if s.signal_type == SignalType.EXIT]
    assert len(exits) == 1
    assert exits[0].exit_reason == "time_stop"


def test_v5_runner_stops_out_below_initial_stop():
    """Before breakeven, the runner uses initial_stop. SL hit triggers EXIT."""
    env = _v5_env(partial_exit_enabled=False, breakeven_r=1.0)
    engine = _make_engine(env)
    engine.seed_history(_bullish_warmup(80))

    from novatrade.strategy.live_engine import OpenPosition

    engine._position = OpenPosition(
        side="LONG",
        entry_price=1.1500,
        stop_loss=1.1480,
        volume=1.00,
        entry_bar=len(engine._h1_candles) - 1,
        initial_stop=1.1480,
        initial_volume=1.00,
        tp_qty_open=0.0,
        runner_qty_open=1.00,
        highest_since_entry=1.1502,
        lowest_since_entry=1.1498,
    )
    engine._state = LiveState.LONG

    # Bar low pierces the initial stop
    bar = _candle(ts=99000.0, o=1.1495, h=1.1496, lo=1.1475, c=1.1478)
    signals = engine.on_bar(bar, "M5")

    exits = [s for s in signals if s.signal_type == SignalType.EXIT]
    assert len(exits) == 1
    assert exits[0].exit_reason == "stop_loss"
    assert exits[0].exit_price == 1.1480  # closed at the stop, not bar.low


# ---------------------------------------------------------------------------
# 6. revalidate_pending
# ---------------------------------------------------------------------------


def test_v5_revalidate_pending_cancels_when_trend_flips():
    """When the trend flips against the pending side, the pending must be cancelled."""
    env = _v5_env(revalidate_pending=True)
    engine = _make_engine(env)

    # Start with a strong uptrend so a LONG pending is created
    warmup = _bullish_warmup(80)
    engine.seed_history(warmup)

    # Inject a pending LONG manually so we don't have to walk a fresh signal in
    from novatrade.strategy.live_engine import PendingOrder

    engine._pending = PendingOrder(
        side="LONG",
        entry_price=warmup[-1].close + 0.0050,
        stop_loss=warmup[-1].close - 0.0020,
        volume=1.00,
        bar_index=len(engine._h1_candles) - 1,
    )
    engine._state = LiveState.PENDING_LONG

    # Now feed a long sequence of bearish bars to flip the EMA20<EMA50 condition
    bearish_seq = _bearish_warmup(40, start=warmup[-1].close, step=0.0030)
    last_signals = []
    for b in bearish_seq:
        last_signals = engine.on_bar(b, "M5")
        if any(s.signal_type == SignalType.CANCEL_PENDING for s in last_signals):
            break

    # At some point during the bearish flip, revalidation must cancel the pending
    cancellations = [s for s in last_signals if s.signal_type == SignalType.CANCEL_PENDING]
    if not cancellations:
        # Walk all signals from all bars to confirm at least one cancel was emitted
        # (this guards against reading only the final bar's output)
        all_signals = []
        engine2 = _make_engine(env)
        engine2.seed_history(warmup)
        engine2._pending = PendingOrder(
            side="LONG",
            entry_price=warmup[-1].close + 0.0050,
            stop_loss=warmup[-1].close - 0.0020,
            volume=1.00,
            bar_index=len(engine2._h1_candles) - 1,
        )
        engine2._state = LiveState.PENDING_LONG
        for b in bearish_seq:
            all_signals.extend(engine2.on_bar(b, "M5"))
        cancellations = [s for s in all_signals if s.signal_type == SignalType.CANCEL_PENDING]

    assert len(cancellations) >= 1
    assert cancellations[0].exit_reason == "revalidate_failed"


# ---------------------------------------------------------------------------
# 7. v4 backward compatibility
# ---------------------------------------------------------------------------


def test_v4_path_unchanged_when_use_simple_trend_filter_off():
    """The legacy V4 normalized-slope filter must still function when V5 is off."""
    env = BacktestEnvironment(
        use_simple_trend_filter=False,
        adx_threshold=15.0,
        trend_slope_threshold=0.0,  # very permissive so a signal CAN fire
        warmup_bars=60,
        atr_period=14,
        ema_period=20,
        adx_period=14,
        irb_threshold=0.45,
        overextension_threshold=2.5,
        use_volatility_filter=False,
    )
    strategy = IRBStrategy(env)
    candles = _bullish_warmup(80)
    indicators = strategy.compute_indicators(candles)

    # Should not crash, should not require ema_fast/ema_slow
    assert "ema_fast" not in indicators
    assert "ema_slow" not in indicators

    # check_exit must still operate normally on V4 path with a position dict
    pos = {
        "side": "LONG",
        "entry_price": candles[-2].close,
        "stop_loss": candles[-2].close - 0.0010,
        "entry_bar": len(candles) - 5,
        "current_stop": candles[-2].close - 0.0010,
        "best_close": candles[-2].close,
    }
    # This must not raise — V5 short-circuit only triggers when use_simple_trend_filter=True
    result = strategy.check_exit(len(candles) - 1, candles, indicators, position=pos)
    # Result may be None or an ExitSignal — both are acceptable; we only assert it doesn't crash
    assert result is None or hasattr(result, "reason")


def test_v5_check_exit_short_circuits_to_none():
    """In V5 mode, strategy.check_exit must return None (live engine handles V5 exits)."""
    env = _v5_env()
    strategy = IRBStrategy(env)
    candles = _bullish_warmup(80)
    indicators = strategy.compute_indicators(candles)

    pos = {
        "side": "LONG",
        "entry_price": candles[-2].close,
        "stop_loss": candles[-2].close - 0.0010,
        "entry_bar": len(candles) - 5,
        "current_stop": candles[-2].close - 0.0010,
        "best_close": candles[-2].close,
    }
    result = strategy.check_exit(len(candles) - 1, candles, indicators, position=pos)
    assert result is None
