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
