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
