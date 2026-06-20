"""Tests for the shadow carry job — must produce dollar-neutral target weights and
NEVER place an order."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "carry_basket_shadow", ROOT / "services" / "carry-basket" / "carry_basket_shadow.py"
)
assert _spec is not None and _spec.loader is not None
shadow = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(shadow)


def test_shadow_entry_is_dollar_neutral():
    S, R = shadow.load_panel()
    entry = shadow.build_shadow_entry(S, R)
    weights = entry["target_weights"]
    assert abs(sum(weights.values())) < 1e-9
    assert 0.0 < entry["derisk_scalar"] <= 1.0
    assert "sim_pnl_bps" in entry and "as_of" in entry


def test_shadow_is_orders_disabled_by_default():
    assert shadow.ORDERS_ENABLED is False


def test_derisk_scalar_uses_hml_series_not_dollar_basket():
    # the shadow's scalar must equal the de-risk the BACKTEST applies for the latest month,
    # i.e. derisk_scalar(carry_returns_raw(...)) — NOT the equal-weight dollar-basket mean
    from novatrade.strategies.carry_basket import CarryConfig, carry_returns_raw, derisk_scalar

    S, R = shadow.load_panel()
    cfg = CarryConfig()
    expected = derisk_scalar(carry_returns_raw(S, R, cfg), cfg)
    entry = shadow.build_shadow_entry(S, R)
    assert abs(entry["derisk_scalar"] - round(expected, 4)) < 1e-9


def test_ledger_upsert_dedups_as_of(tmp_path, monkeypatch):
    import json

    led = tmp_path / "ledger.jsonl"
    monkeypatch.setenv("CBS_LEDGER", str(led))
    shadow.main()
    shadow.main()  # second run same month
    lines = [json.loads(x) for x in led.read_text().splitlines() if x.strip()]
    as_ofs = [e["as_of"] for e in lines]
    assert len(as_ofs) == len(set(as_ofs))  # no duplicate as_of months
