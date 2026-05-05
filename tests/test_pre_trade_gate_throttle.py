"""BASE_RISK_PCT must honor NOVATRADE_RISK_FRACTION_OVERRIDE env var.

Bug context (2026-04-30): the runner reads NOVATRADE_RISK_FRACTION_OVERRIDE
to throttle env.risk_fraction (used by backtest engine and live strategy
engines). But pre_trade_gate.py hardcoded BASE_RISK_PCT=0.0033 — so the
TradingAgent's auto-sizing path never honored the throttle. A 0.0005 throttle
intent was being ignored in live trading; positions sized at 0.33%.

Fix: pre_trade_gate.BASE_RISK_PCT now reads the same env var at module load
and applies bounds (0, 0.05] matching the runner.

This test pins the contract so future edits don't silently drop the throttle.
"""

from __future__ import annotations

import importlib


def _reload_module(monkeypatch, env_value: str | None) -> float:
    """Reload pre_trade_gate with the given env var value, return BASE_RISK_PCT."""
    if env_value is None:
        monkeypatch.delenv("NOVATRADE_RISK_FRACTION_OVERRIDE", raising=False)
    else:
        monkeypatch.setenv("NOVATRADE_RISK_FRACTION_OVERRIDE", env_value)

    import novatrade.risk.pre_trade_gate as ptg

    importlib.reload(ptg)
    return ptg.BASE_RISK_PCT


def test_default_when_no_override(monkeypatch) -> None:
    """No env var → default 0.0033 (matches validated baseline)."""
    val = _reload_module(monkeypatch, None)
    assert val == 0.0033, f"Default BASE_RISK_PCT should be 0.0033, got {val}"


def test_throttle_to_0_0005(monkeypatch) -> None:
    """Standard L2 throttle → 0.0005."""
    val = _reload_module(monkeypatch, "0.0005")
    assert val == 0.0005, f"Override 0.0005 should yield 0.0005, got {val}"


def test_throttle_to_0_001(monkeypatch) -> None:
    """Stage 3d-1 ramp level → 0.001."""
    val = _reload_module(monkeypatch, "0.001")
    assert val == 0.001


def test_garbage_input_falls_back_to_default(monkeypatch) -> None:
    """Non-numeric env value → default."""
    val = _reload_module(monkeypatch, "not_a_number")
    assert val == 0.0033


def test_out_of_bounds_falls_back_to_default(monkeypatch) -> None:
    """Override outside (0, 0.05] → default."""
    # Negative
    val = _reload_module(monkeypatch, "-0.001")
    assert val == 0.0033, f"Negative override should fall back to default, got {val}"
    # Zero
    val = _reload_module(monkeypatch, "0")
    assert val == 0.0033, f"Zero override should fall back to default, got {val}"
    # Too large (>5%)
    val = _reload_module(monkeypatch, "0.10")
    assert val == 0.0033, f"Override >0.05 should fall back to default, got {val}"


def test_throttle_propagates_to_position_sizer_calc(monkeypatch) -> None:
    """End-to-end: throttle env var → BASE_RISK_PCT → position sizer reduces volume."""
    monkeypatch.setenv("NOVATRADE_RISK_FRACTION_OVERRIDE", "0.0005")
    import novatrade.risk.pre_trade_gate as ptg

    importlib.reload(ptg)
    from novatrade.risk.position_sizer import PositionSizer

    sizer = PositionSizer(min_lot=0.01, max_lot=50.0)
    equity = 96_896.62
    entry = 1.17147
    stop = 1.17120
    # With BASE_RISK_PCT=0.0005, expected volume should be ~1.7 lots (capped by max_lot=50)
    vol_throttled = sizer.calculate(equity=equity, entry=entry, stop=stop, risk_pct=ptg.BASE_RISK_PCT)
    # With default 0.0033, would be ~11.4 lots
    vol_default = sizer.calculate(equity=equity, entry=entry, stop=stop, risk_pct=0.0033)

    assert vol_throttled < vol_default, f"Throttled volume {vol_throttled} should be less than default {vol_default}"
    # Specifically: ratio should match risk ratio
    expected_ratio = 0.0005 / 0.0033
    actual_ratio = vol_throttled / vol_default
    assert abs(actual_ratio - expected_ratio) < 0.01, (
        f"Volume ratio {actual_ratio} should match risk ratio {expected_ratio}"
    )
