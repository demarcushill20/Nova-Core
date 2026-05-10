from datetime import datetime, timezone

import pytest
from discord_mirror.config import (
    Config,
    DiscordCfg,
    LoggingCfg,
    RiskCfg,
    StateCfg,
    StorageCfg,
)
from discord_mirror.executor_paper import PaperExecutor
from discord_mirror.models import Direction, ParsedSignal, SignalAction
from discord_mirror.storage import Storage


@pytest.fixture
def cfg():
    return Config(
        discord=DiscordCfg(channel_id=1),
        risk=RiskCfg(account_risk_pct=0.01),
        symbol_map={"GOLD": "XAUUSD"},
        state=StateCfg(),
        storage=StorageCfg(db_path=":memory:"),
        logging=LoggingCfg(),
    )


async def test_paper_executor_logs_fills(tmp_path, cfg):
    s = Storage(tmp_path / "t.db")
    await s.init()
    raw_id = await s.log_raw_message(
        discord_message_id="r",
        channel_id="c",
        author="a",
        content="x",
        ts=datetime.now(timezone.utc),
    )
    sid = await s.log_parsed_signal(
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
    sig = ParsedSignal(
        action=SignalAction.OPEN,
        direction=Direction.BUY,
        symbol="GOLD",
        sl=4480.0,
        tps=[4520.0, 4530.0],
    )
    ex = PaperExecutor(storage=s, cfg=cfg, balance=10_000.0)
    n = await ex.execute(sid, sig)
    assert n == 2
    cur = await s._conn.execute("SELECT COUNT(*) FROM trades WHERE signal_id = ?", (sid,))
    row = await cur.fetchone()
    assert row is not None
    assert row[0] == 2
    await s.close()


async def test_paper_executor_skips_unknown_symbol(tmp_path, cfg):
    s = Storage(tmp_path / "t.db")
    await s.init()
    raw_id = await s.log_raw_message(
        discord_message_id="r",
        channel_id="c",
        author="a",
        content="x",
        ts=datetime.now(timezone.utc),
    )
    sid = await s.log_parsed_signal(
        raw_id,
        {
            "action": "OPEN",
            "direction": "BUY",
            "symbol": "FAKE",
            "sl": 4480.0,
            "tps": [4520.0],
            "confidence": 0.9,
        },
    )
    sig = ParsedSignal(
        action=SignalAction.OPEN,
        direction=Direction.BUY,
        symbol="FAKE",
        sl=4480.0,
        tps=[4520.0],
    )
    ex = PaperExecutor(storage=s, cfg=cfg, balance=10_000.0)
    assert await ex.execute(sid, sig) == 0
    await s.close()
