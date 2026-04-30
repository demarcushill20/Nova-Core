"""Structural tests for alerts_schema_v5_1.json."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

SCHEMA_PATH = Path(__file__).parent.parent / "docs" / "demo_test_run" / "alerts_schema_v5_1.json"


@pytest.fixture(scope="module")
def schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text())


def _signal(*, broker_symbol="EURUSD", campaign="ic-markets-demo-2026-q2") -> dict:
    return {
        "strategy_name": "Rob Hoffman IRB",
        "strategy_version": "5.1.0",
        "action": "PLACE_STOP_ORDER",
        "signal_type": "LONG_IRB",
        "irb_type": "UPTREND_IRB",
        "symbol": "EURUSD",
        "broker_symbol": broker_symbol,
        "timeframe": "M5",
        "side": "BUY",
        "order_type": "BUY_STOP",
        "entry_price": 1.0950,
        "stop_loss": 1.0900,
        "stop_distance_pips": 50.0,
        "volume": 0.10,
        "risk_dollars": 100.0,
        "bar_close_time": 1700000000000,
        "bar_ohlc_o": 1.0920,
        "bar_ohlc_h": 1.0950,
        "bar_ohlc_l": 1.0910,
        "bar_ohlc_c": 1.0945,
        "irb_range": 0.0040,
        "ema_10_h1": 1.093,
        "ema_20_h1": 1.0925,
        "ema_50_h1": 1.090,
        "trail_ema": 1.092,
        "trail_ema_period": 40,
        "ema_stack_h1": "BULL",
        "ema_confirm_bars": 3,
        "ema10_confirm_long": True,
        "ema10_confirm_short": False,
        "ema_slope": 0.5,
        "ema_20_h4": 1.090,
        "ema_20_h4_dir": "RISING",
        "adx_14": 25.0,
        "atr_14": 0.0030,
        "overextension_ratio": 1.2,
        "trigger_window_bars": 20,
        "strategy_state": "PENDING_LONG",
        "campaign": campaign,
    }


def test_schema_id_is_v5_1(schema):
    assert schema["$id"] == "novatrade-irb-alert-v5.1.0"


def test_signal_accepts_non_ftmo_broker(schema):
    jsonschema.validate(_signal(broker_symbol="EURUSD"), schema)


def test_signal_accepts_arbitrary_campaign(schema):
    jsonschema.validate(_signal(campaign="any-non-empty"), schema)


def test_trail_no_longer_requires_ema_fields(schema):
    jsonschema.validate(
        {
            "strategy_name": "Rob Hoffman IRB",
            "strategy_version": "5.1.0",
            "action": "MODIFY_SL",
            "symbol": "EURUSD",
            "side": "BUY",
            "old_stop": 1.09,
            "new_stop": 1.091,
            "best_close": 1.095,
            "bars_since_entry": 5,
            "campaign": "ic-markets-demo-2026-q2",
            "trail_method": "ATR",
            "trail_atr_mult": 2.0,
        },
        schema,
    )


def test_signal_rejects_empty_campaign(schema):
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(_signal(campaign=""), schema)
