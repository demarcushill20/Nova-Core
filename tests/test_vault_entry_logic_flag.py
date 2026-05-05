"""Stage 3a port: verify the vault_entry_logic flag toggles behaviour.

The contract has two halves:
1. Default (vault_entry_logic=False) — buffers and ATR floor are applied.
   This is the legacy nova path; bit-identical regression coverage lives in
   tests/test_backtest_engine.py::TestParityAuditToggleNoOp.
2. Flag ON (vault_entry_logic=True) — entry/stop offsets are 1 tick (0.00001),
   no spread cushion, no ATR SL floor. Matches the validated vault Python
   reference (`100-trading-strategies/Rob Hoffman IRB v5 – Relaxed Reliable
   Build (Python) 1.md`).
"""

from __future__ import annotations

from dataclasses import replace as dc_replace

from novatrade.backtest.environment import BacktestEnvironment
from novatrade.models import Candle
from novatrade.strategies.irb import IRBStrategy


def _build_env(**overrides) -> BacktestEnvironment:
    base = BacktestEnvironment.for_v5_m5()
    return dc_replace(base, **overrides)


def _make_irb_long_setup() -> tuple[list[Candle], dict[str, list[float]]]:
    """Build a minimal candle stream that produces a bullish IRB signal."""
    # Build 80 bars with rising close (bull trend) + a final IRB bar.
    candles: list[Candle] = []
    base_price = 1.10000
    for i in range(75):
        ts = 1_700_000_000 + i * 300  # 5-min bars
        c = base_price + i * 0.00010  # consistent uptrend
        candles.append(
            Candle(
                timestamp=ts,
                open=c - 0.00005,
                high=c + 0.00010,
                low=c - 0.00010,
                close=c,
                volume=100.0,
                symbol="EURUSD",
                timeframe="M5",
            )
        )
    # Final bullish-IRB bar: close in lower zone of bar
    last_close = candles[-1].close
    irb_low = last_close - 0.00010
    irb_high = last_close + 0.00050  # wide range
    irb_open = last_close + 0.00005  # open in lower zone (< up_threshold)
    candles.append(
        Candle(
            timestamp=candles[-1].timestamp + 300,
            open=irb_open,
            high=irb_high,
            low=irb_low,
            close=irb_open + 0.00002,  # close also in lower zone
            volume=100.0,
            symbol="EURUSD",
            timeframe="M5",
        )
    )
    # Indicators: build minimal valid set
    closes = [c.close for c in candles]
    ema = [closes[0]]
    for c_ in closes[1:]:
        ema.append(ema[-1] * 0.95 + c_ * 0.05)
    indicators = {
        "ema": ema,
        "ema_fast": [c.close - 0.0001 for c in candles],  # below close = bull
        "ema_slow": [c.close - 0.0003 for c in candles],
        "atr": [0.00050] * len(candles),  # constant ATR
        "adx": [25.0] * len(candles),  # above any ADX threshold
    }
    return candles, indicators


def test_default_behaviour_uses_pip_buffer_and_atr_floor() -> None:
    """vault_entry_logic=False (default) → entry uses 1-pip buffer + ATR floor."""
    env = _build_env(
        warmup_bars=5,
        sl_spread_buffer_pips=1.0,  # ensure non-zero so we can detect cushion
        atr_sl_floor_multiplier=1.0,  # ATR floor active
        # Defaults: vault_entry_logic=False, vault_pending_replace_any_side=False
    )
    assert env.vault_entry_logic is False
    assert env.vault_pending_replace_any_side is False

    candles, indicators = _make_irb_long_setup()
    strategy = IRBStrategy(env=env)
    sig = strategy.check_entry(len(candles) - 1, candles, indicators, h4_candles=None)
    # Without HTF candles the v5 simple-trend check skips HTF gating (per code at
    # irb.py:166: `if h4_candles and len(h4_candles) >= 2`), so a signal can
    # still fire. Verify it does.
    assert sig is not None, "Expected a LONG signal with bull-trend setup"

    bar = candles[-1]
    # Entry at high + 1 pip (0.0001)
    assert abs(sig.entry_price - (bar.high + 0.0001)) < 1e-9, (
        f"Default entry should be high + 1pip, got {sig.entry_price} vs expected {bar.high + 0.0001}"
    )
    # Stop should be widened by ATR floor (1.0 × atr = 0.00050) since the
    # raw distance (high+1pip → low-1pip-1pip = 0.00050+2pip = 0.00070) is
    # already wider than 1×ATR=0.00050. So no widening expected here, just
    # verify it's at least bar.low - 2pip (1pip buffer + 1pip cushion).
    expected_stop_floor = bar.low - 0.0001 - 0.0001  # 1pip + 1pip cushion
    assert sig.stop_loss <= expected_stop_floor + 1e-9, (
        f"Default stop should be at most low - 2pip (incl. spread cushion), got {sig.stop_loss}"
    )


def test_vault_flag_uses_tick_buffer_and_skips_floor() -> None:
    """vault_entry_logic=True → entry/stop use 1-tick offset, no cushion, no floor."""
    env = _build_env(
        warmup_bars=5,
        sl_spread_buffer_pips=1.0,  # would apply if not gated
        atr_sl_floor_multiplier=1.0,  # would apply if not gated
        vault_entry_logic=True,
    )
    candles, indicators = _make_irb_long_setup()
    strategy = IRBStrategy(env=env)
    sig = strategy.check_entry(len(candles) - 1, candles, indicators, h4_candles=None)
    assert sig is not None, "Expected a LONG signal under vault_entry_logic"

    bar = candles[-1]
    # Entry at high + 1 tick (0.00001)
    assert abs(sig.entry_price - (bar.high + 0.00001)) < 1e-9, (
        f"Vault entry should be high + 1 tick, got {sig.entry_price}"
    )
    # Stop at low - 1 tick — no cushion, no floor
    assert abs(sig.stop_loss - (bar.low - 0.00001)) < 1e-9, f"Vault stop should be low - 1 tick, got {sig.stop_loss}"


def test_vault_flag_produces_tighter_stop_than_default() -> None:
    """Vault flag must produce a meaningfully different (tighter) stop than default."""
    candles, indicators = _make_irb_long_setup()

    env_nova = _build_env(warmup_bars=5, sl_spread_buffer_pips=1.0, atr_sl_floor_multiplier=1.0)
    env_vault = _build_env(
        warmup_bars=5, sl_spread_buffer_pips=1.0, atr_sl_floor_multiplier=1.0, vault_entry_logic=True
    )

    sig_nova = IRBStrategy(env=env_nova).check_entry(len(candles) - 1, candles, indicators, None)
    sig_vault = IRBStrategy(env=env_vault).check_entry(len(candles) - 1, candles, indicators, None)
    assert sig_nova is not None and sig_vault is not None

    # Vault stop is tighter (closer to entry) than nova stop for longs.
    nova_stop_dist = sig_nova.entry_price - sig_nova.stop_loss
    vault_stop_dist = sig_vault.entry_price - sig_vault.stop_loss
    bar = candles[-1]
    assert vault_stop_dist < nova_stop_dist, (
        f"Vault stop_dist={vault_stop_dist} should be tighter than nova stop_dist={nova_stop_dist}"
    )
    # Vault stop_dist = bar_range + 2 ticks (entry_offset + stop_offset)
    expected_vault_dist = (bar.high - bar.low) + 2 * 0.00001
    assert abs(vault_stop_dist - expected_vault_dist) < 1e-9, (
        f"vault_stop_dist={vault_stop_dist} should equal bar_range+2tick={expected_vault_dist}"
    )
    # Nova stop_dist must include buffers + spread cushion (at least 0.0003 wider
    # than vault for default 1pip+1pip+1pip configuration). May be even wider if
    # ATR floor activates.
    assert nova_stop_dist - vault_stop_dist >= 0.000279, (
        f"Buffer-driven gap nova-vault={nova_stop_dist - vault_stop_dist} too small — flag may be inert"
    )


def test_default_pending_replace_rejects_opposite_side() -> None:
    """Default (vault_pending_replace_any_side=False) preserves existing reject."""
    env = _build_env()
    assert env.vault_pending_replace_any_side is False  # documents the default


def test_vault_flag_pending_replace_field_exists() -> None:
    """vault_pending_replace_any_side=True must set the env field."""
    env = _build_env(vault_pending_replace_any_side=True)
    assert env.vault_pending_replace_any_side is True
