from datetime import datetime, timedelta, timezone

import pytest
from discord_mirror.parity import daily_report
from discord_mirror.storage import Storage


@pytest.fixture
async def store(tmp_path):
    s = Storage(tmp_path / "t.db")
    await s.init()
    yield s
    await s.close()


async def test_daily_report_empty(store):
    since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    report = await daily_report(store, since)
    assert report["raw_messages"] == 0
    assert report["open_signals"] == 0
    assert report["trades"] == 0
    assert report["trade_states"] == {}
    assert report["trade_modes"] == {}


async def test_daily_report_counts_pipeline(store):
    raw_id = await store.log_raw_message(
        discord_message_id="r1",
        channel_id="c",
        author="a",
        content="BUY GOLD\nSL 4480\nTP1 4520",
        ts=datetime.now(timezone.utc),
    )
    sid = await store.log_parsed_signal(
        raw_id,
        {
            "action": "OPEN",
            "direction": "BUY",
            "symbol": "GOLD",
            "sl": 4480.0,
            "tps": [4520.0, 4530.0],
            "confidence": 0.9,
        },
    )
    await store.log_paper_fill(
        signal_id=sid,
        direction="BUY",
        symbol="XAUUSD",
        entry=4500.0,
        sl=4480.0,
        tp=4520.0,
        lot=0.01,
    )
    await store.log_paper_fill(
        signal_id=sid,
        direction="BUY",
        symbol="XAUUSD",
        entry=4500.0,
        sl=4480.0,
        tp=4530.0,
        lot=0.01,
    )

    since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    report = await daily_report(store, since)
    assert report["raw_messages"] == 1
    assert report["open_signals"] == 1
    assert report["trades"] == 2
    assert report["total_lot"] == 0.02
    assert report["trade_states"] == {"OPEN": 2}
    assert report["trade_modes"] == {"paper": 2}
