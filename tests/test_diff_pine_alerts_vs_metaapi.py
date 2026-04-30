"""Tests for scripts/diff_pine_alerts_vs_metaapi.py — reconcile() core, no MetaApi I/O."""

from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "scripts" / "diff_pine_alerts_vs_metaapi.py"


def _mod():
    spec = importlib.util.spec_from_file_location("diff_pine", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_alert_without_deal():
    out = _mod().reconcile(
        [
            {
                "action": "PLACE_STOP_ORDER",
                "bar_close_time": 1000,
                "side": "BUY",
                "entry_price": 1.10,
                "volume": 0.1,
            }
        ],
        [],
    )
    assert out and out[0]["classification"] == "alert_without_deal"


def test_deal_without_alert():
    out = _mod().reconcile(
        [],
        [
            {
                "id": "d1",
                "type": "DEAL_TYPE_BUY",
                "time": 1000.0,
                "volume": 0.1,
                "price": 1.10,
                "comment": "manual",
            }
        ],
    )
    assert out and out[0]["classification"] == "deal_without_alert"


def test_partial_tp_classified_as_expected_v1_divergence():
    out = _mod().reconcile(
        [],
        [
            {
                "id": "d2",
                "type": "DEAL_TYPE_SELL",
                "time": 1500.0,
                "volume": 0.05,
                "price": 1.11,
                "comment": "PARTIAL_TP",
            }
        ],
    )
    assert out and out[0]["classification"] == "expected_v1_divergence"


def test_match_no_divergence():
    out = _mod().reconcile(
        [
            {
                "action": "PLACE_STOP_ORDER",
                "bar_close_time": 1_000_000,
                "side": "BUY",
                "entry_price": 1.10,
                "volume": 0.1,
            }
        ],
        [
            {
                "id": "d3",
                "type": "DEAL_TYPE_BUY",
                "time": 1000.5,
                "volume": 0.1,
                "price": 1.10,
                "comment": "PLACE_STOP_ORDER",
            }
        ],
    )
    assert out == []
