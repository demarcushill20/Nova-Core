from datetime import datetime, timezone

import pytest
from discord_mirror.storage import Storage


@pytest.fixture
async def store(tmp_path):
    s = Storage(tmp_path / "test.db")
    await s.init()
    yield s
    await s.close()


async def test_log_raw_message_round_trips(store):
    msg_id = await store.log_raw_message(
        discord_message_id="111",
        channel_id="222",
        author="J",
        content="BUY GOLD\nSL 4535\nTP1 4546",
        ts=datetime(2026, 4, 30, 12, 15, tzinfo=timezone.utc),
    )
    assert msg_id > 0
    rows = await store.list_recent_raw_messages(limit=10)
    assert len(rows) == 1
    assert rows[0]["content"].startswith("BUY GOLD")


async def test_log_parsed_signal(store):
    raw_id = await store.log_raw_message(
        discord_message_id="333",
        channel_id="222",
        author="J",
        content="BUY GOLD\nSL 4535\nTP1 4546",
        ts=datetime.now(timezone.utc),
    )
    sid = await store.log_parsed_signal(
        raw_id,
        {
            "action": "OPEN",
            "direction": "BUY",
            "symbol": "GOLD",
            "sl": 4535.0,
            "tps": [4546.0],
            "confidence": 0.9,
        },
    )
    assert sid > 0
    rows = await store.list_open_signals()
    assert len(rows) == 1
    assert rows[0]["symbol"] == "GOLD"


async def test_log_metaapi_fill_and_query(store):
    raw_id = await store.log_raw_message(
        discord_message_id="r",
        channel_id="c",
        author="a",
        content="x",
        ts=datetime.now(timezone.utc),
    )
    sid = await store.log_parsed_signal(
        raw_id,
        {
            "action": "OPEN",
            "direction": "BUY",
            "symbol": "GOLD",
            "sl": 4480.0,
            "tps": [4520.0],
            "confidence": 0.9,
        },
    )
    fill_id = await store.log_metaapi_fill(
        signal_id=sid,
        broker_order_id="ord-1",
        direction="BUY",
        symbol="XAUUSD",
        entry=4500.0,
        sl=4480.0,
        tp=4520.0,
        lot=0.05,
    )
    assert fill_id > 0
    open_trades = await store.list_open_trades_for_signal(sid)
    assert len(open_trades) == 1
    assert open_trades[0]["broker_order_id"] == "ord-1"
    await store.update_trade_state(fill_id, state="BE", sl=4500.0)
    open_after = await store.list_open_trades_for_signal(sid)
    assert open_after == []  # state changed away from OPEN
