import pytest
from discord_mirror.models import Direction, ParsedSignal, SignalAction, StatusUpdate
from pydantic import ValidationError


def test_open_signal_round_trips():
    s = ParsedSignal(
        action=SignalAction.OPEN,
        direction=Direction.BUY,
        symbol="GOLD",
        sl=4535.0,
        tps=[4546.0, 4550.0, 4555.0],
        confidence=0.95,
    )
    assert s.action == SignalAction.OPEN
    assert s.tps == [4546.0, 4550.0, 4555.0]


def test_open_requires_tps():
    with pytest.raises(ValidationError):
        ParsedSignal(
            action=SignalAction.OPEN,
            direction=Direction.BUY,
            symbol="GOLD",
            sl=4535.0,
            tps=[],
        )


def test_open_requires_sl():
    with pytest.raises(ValidationError):
        ParsedSignal(
            action=SignalAction.OPEN,
            direction=Direction.BUY,
            symbol="GOLD",
            sl=None,
            tps=[4546.0],
        )


def test_status_update_tp_hit():
    u = StatusUpdate(kind="TP_HIT", tp_index=1, raw_text="TP1 SMACKED!!!")
    assert u.tp_index == 1


def test_tp_hit_requires_index():
    with pytest.raises(ValidationError):
        StatusUpdate(kind="TP_HIT", tp_index=None, raw_text="?")
