from unittest.mock import AsyncMock

from discord_mirror.models import StatusUpdate
from discord_mirror.state import SignalStateMachine
from discord_mirror.status_handler import StatusHandler


class FakeStorage:
    def __init__(self):
        self.trades = [
            {
                "id": 1,
                "broker_order_id": "o1",
                "direction": "BUY",
                "symbol": "XAUUSD",
                "entry": 4500.0,
                "sl": 4480.0,
                "tp": 4520.0,
                "state": "OPEN",
            },
            {
                "id": 2,
                "broker_order_id": "o2",
                "direction": "BUY",
                "symbol": "XAUUSD",
                "entry": 4500.0,
                "sl": 4480.0,
                "tp": 4530.0,
                "state": "OPEN",
            },
        ]
        self.updates = []

    async def list_open_trades_for_signal(self, sid):
        return [t for t in self.trades if t["state"] == "OPEN"]

    async def update_trade_state(self, tid, *, state, sl=None):
        self.updates.append((tid, state, sl))
        for t in self.trades:
            if t["id"] == tid:
                t["state"] = state
                if sl is not None:
                    t["sl"] = sl


async def test_tp1_hit_closes_lowest_and_moves_remaining_sl_to_be():
    storage = FakeStorage()
    adapter = AsyncMock()
    sm = SignalStateMachine(tp_count=2)
    h = StatusHandler(storage=storage, adapter=adapter)
    await h.handle(
        signal_id=99,
        sm=sm,
        update=StatusUpdate(kind="TP_HIT", tp_index=1, raw_text="TP1 SMACKED"),
    )
    adapter.close_position.assert_any_await("o1")
    adapter.modify_position_sl.assert_any_await("o2", 4500.0)


async def test_all_tps_closes_everything():
    storage = FakeStorage()
    adapter = AsyncMock()
    sm = SignalStateMachine(tp_count=2)
    h = StatusHandler(storage=storage, adapter=adapter)
    await h.handle(
        signal_id=99,
        sm=sm,
        update=StatusUpdate(kind="ALL_TPS_HIT", raw_text="ALL TPs SMASHED"),
    )
    adapter.close_position.assert_any_await("o1")
    adapter.close_position.assert_any_await("o2")


async def test_paper_mode_no_adapter_calls():
    storage = FakeStorage()
    sm = SignalStateMachine(tp_count=2)
    h = StatusHandler(storage=storage, adapter=None)
    await h.handle(
        signal_id=99,
        sm=sm,
        update=StatusUpdate(kind="TP_HIT", tp_index=1, raw_text="?"),
    )
    assert any(u[1] == "BE" for u in storage.updates)


async def test_sl_hit_closes_all_open_trades():
    storage = FakeStorage()
    adapter = AsyncMock()
    sm = SignalStateMachine(tp_count=2)
    h = StatusHandler(storage=storage, adapter=adapter)
    await h.handle(
        signal_id=99,
        sm=sm,
        update=StatusUpdate(kind="SL_HIT", raw_text="stopped out"),
    )
    # SL hit means broker already closed the trades; we just record state.
    closed_states = [u for u in storage.updates if u[1] == "CLOSED"]
    assert len(closed_states) == 2
