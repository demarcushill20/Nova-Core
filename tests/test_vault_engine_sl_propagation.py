"""Test that VaultLiveEngine emits MODIFY_SL signals when runner stop tightens.

The vault strategy computes a dynamic runner stop (breakeven + ATR trail) each
bar. VaultLiveEngine must propagate these changes as MODIFY_SL signals so the
trading agent can update the broker's stop-loss order accordingly.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timezone

import pandas as pd
import pytest

from novatrade.backtest.environment import BacktestEnvironment
from novatrade.models import Candle
from novatrade.strategy.live_engine import LiveConfig, LiveState, SignalType
from novatrade.strategy.vault_engine import VaultLiveEngine


def _build_env() -> BacktestEnvironment:
    base = BacktestEnvironment.for_v5_m5()
    return replace(
        base,
        primary_timeframe="M5",
        higher_timeframe="H1",
        risk_fraction=0.0033,
        max_volume=1.0,
        min_volume=0.01,
        partial_exit_enabled=True,
        partial_exit_pct=50.0,
        partial_r_target=1.0,
        breakeven_r=1.0,
        trail_atr_multiplier=2.0,
        time_stop_bars=20,
        trigger_window_bars=12,
        cooldown_bars=1,
        max_trades_per_day=999,
        overextension_threshold=2.5,
        min_signal_atr_mult=0.0,
    )


def _make_candle(ts: float, o: float, h: float, lo: float, c: float) -> Candle:
    return Candle(timestamp=ts, open=o, high=h, low=lo, close=c, volume=100.0, symbol="EURUSD", timeframe="M5")


def _load_real_candles(weeks: int = 4):
    """Load real M5 + H1 candles for a realistic scenario."""
    from pathlib import Path

    repo = Path("/home/nova/nova-core")
    m5_csv = repo / "data/candles/EURUSD_M5_10yr.csv"
    h1_csv = repo / "data/candles/EURUSD_H1_10yr.csv"
    if not m5_csv.exists():
        pytest.skip("M5 CSV not available")

    raw_m5 = pd.read_csv(m5_csv)
    raw_m5["timestamp"] = pd.to_datetime(raw_m5["timestamp"])
    cutoff = raw_m5["timestamp"].max() - pd.DateOffset(weeks=weeks)
    raw_m5 = raw_m5[raw_m5["timestamp"] >= cutoff].reset_index(drop=True)

    raw_h1 = pd.read_csv(h1_csv)
    raw_h1["timestamp"] = pd.to_datetime(raw_h1["timestamp"])
    raw_h1 = raw_h1[raw_h1["timestamp"] >= cutoff].reset_index(drop=True)

    m5_candles = []
    for _, row in raw_m5.iterrows():
        ts = row["timestamp"].replace(tzinfo=timezone.utc).timestamp()
        m5_candles.append(
            Candle(
                timestamp=ts,
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row.get("volume", 0) or 0),
                symbol="EURUSD",
                timeframe="M5",
            )
        )

    h1_candles = []
    for _, row in raw_h1.iterrows():
        ts = row["timestamp"].replace(tzinfo=timezone.utc).timestamp()
        h1_candles.append(
            Candle(
                timestamp=ts,
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row.get("volume", 0) or 0),
                symbol="EURUSD",
                timeframe="H1",
            )
        )

    return m5_candles, h1_candles


def test_modify_sl_emitted_on_runner_stop_tightening():
    """Stream real candles and verify MODIFY_SL signals are emitted.

    Over a multi-week window, any trade that reaches breakeven should trigger
    at least one MODIFY_SL as the ATR trail ratchets the stop.
    """
    m5_candles, h1_candles = _load_real_candles(weeks=1)
    assert len(m5_candles) > 100

    env = _build_env()
    live_cfg = LiveConfig(
        symbol="EURUSD",
        primary_timeframe="M5",
        higher_timeframe="H1",
        trigger_window_bars=12,
        enable_market_hours_check=False,
    )
    engine = VaultLiveEngine(env, live_cfg)

    seed_n = max(60, env.warmup_bars)
    seed_m5 = m5_candles[:seed_n]
    seed_cutoff = seed_m5[-1].timestamp
    seed_h1 = [b for b in h1_candles if b.timestamp <= seed_cutoff]
    engine.seed_history(seed_m5, seed_h1)

    h1_iter = iter([b for b in h1_candles if b.timestamp > seed_cutoff])
    next_h1 = next(h1_iter, None)

    modify_sl_signals = []
    entry_signals = []
    for m5 in m5_candles[seed_n:]:
        while next_h1 is not None and next_h1.timestamp <= m5.timestamp:
            engine.on_bar(next_h1, "H1")
            next_h1 = next(h1_iter, None)
        sigs = engine.on_bar(m5, "M5")
        for s in sigs:
            if s.signal_type == SignalType.MODIFY_SL:
                modify_sl_signals.append(s)
            elif s.signal_type == SignalType.ENTRY:
                entry_signals.append(s)

    assert len(entry_signals) > 0, "Expected at least one entry signal over 4 weeks"
    assert len(modify_sl_signals) > 0, (
        f"Expected MODIFY_SL signals but got none ({len(entry_signals)} entries occurred)"
    )

    for sig in modify_sl_signals:
        assert sig.new_stop > 0, "MODIFY_SL must have a valid new_stop"
        assert sig.metadata.get("old_stop") is not None, "MODIFY_SL must include old_stop in metadata"
        assert sig.metadata.get("engine") == "vault"


def test_modify_sl_stop_only_tightens():
    """Verify that MODIFY_SL only fires when the stop moves favourably."""
    m5_candles, h1_candles = _load_real_candles(weeks=1)
    env = _build_env()
    live_cfg = LiveConfig(
        symbol="EURUSD",
        primary_timeframe="M5",
        higher_timeframe="H1",
        trigger_window_bars=12,
        enable_market_hours_check=False,
    )
    engine = VaultLiveEngine(env, live_cfg)

    seed_n = max(60, env.warmup_bars)
    engine.seed_history(m5_candles[:seed_n], [b for b in h1_candles if b.timestamp <= m5_candles[seed_n - 1].timestamp])

    h1_iter = iter([b for b in h1_candles if b.timestamp > m5_candles[seed_n - 1].timestamp])
    next_h1 = next(h1_iter, None)

    for m5 in m5_candles[seed_n:]:
        while next_h1 is not None and next_h1.timestamp <= m5.timestamp:
            engine.on_bar(next_h1, "H1")
            next_h1 = next(h1_iter, None)
        sigs = engine.on_bar(m5, "M5")
        for s in sigs:
            if s.signal_type == SignalType.MODIFY_SL:
                old = s.metadata["old_stop"]
                new = s.new_stop
                side = s.side
                if side == "LONG":
                    assert new > old, f"LONG MODIFY_SL must tighten: {old} → {new}"
                else:
                    assert new < old, f"SHORT MODIFY_SL must tighten: {old} → {new}"


def test_current_stop_cleared_on_notify_close():
    """notify_close must reset _current_stop so stale values don't leak."""
    env = _build_env()
    engine = VaultLiveEngine(env)
    engine._current_stop = 1.12345
    engine.notify_close(pnl=50.0)
    assert engine._current_stop is None


def test_current_stop_set_on_recover():
    """recover_position_state must initialize _current_stop from the SL."""
    env = _build_env()
    engine = VaultLiveEngine(env)
    # Need some candles so recovery doesn't fail
    engine._primary_candles = [_make_candle(1000.0, 1.1, 1.11, 1.09, 1.105)]
    engine._live_state = LiveState.FLAT
    engine.recover_position_state(
        side="LONG",
        entry_price=1.10000,
        stop_loss=1.09500,
        volume=0.10,
    )
    assert engine._current_stop == 1.09500
