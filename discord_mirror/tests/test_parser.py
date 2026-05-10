import json
import os
from pathlib import Path

import pytest
from discord_mirror.models import Direction, SignalAction
from discord_mirror.parser import SignalParser

pytestmark = pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="needs ANTHROPIC_API_KEY",
)


@pytest.fixture
def fixtures():
    return json.loads((Path(__file__).parent / "fixtures" / "sample_messages.json").read_text())


@pytest.fixture
def parser():
    return SignalParser(api_key=os.environ["ANTHROPIC_API_KEY"])


async def test_parses_open_signal_msg1(parser, fixtures):
    msg = next(f for f in fixtures if f["id"] == "msg1")
    result = await parser.parse(msg["content"])
    assert result.signal is not None
    assert result.signal.action == SignalAction.OPEN
    assert result.signal.direction == Direction.BUY
    assert result.signal.symbol == "GOLD"
    assert result.signal.sl == 4535.0
    assert len(result.signal.tps) == 8


async def test_parses_tp_hit(parser, fixtures):
    msg = next(f for f in fixtures if f["id"] == "msg3")
    result = await parser.parse(msg["content"])
    assert result.status is not None
    assert result.status.kind == "TP_HIT"
    assert result.status.tp_index == 1


async def test_parses_all_tps(parser, fixtures):
    msg = next(f for f in fixtures if f["id"] == "msg4")
    result = await parser.parse(msg["content"])
    assert result.status is not None
    assert result.status.kind == "ALL_TPS_HIT"


async def test_parses_chitchat_as_none(parser, fixtures):
    msg = next(f for f in fixtures if f["id"] == "msg5")
    result = await parser.parse(msg["content"])
    assert result.signal is None
    assert result.status is None
