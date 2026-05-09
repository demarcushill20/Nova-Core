from __future__ import annotations

import logging
from typing import Any, Protocol

from .models import StatusUpdate
from .state import SignalStateMachine, StateAction

log = logging.getLogger(__name__)


class _StorageProto(Protocol):
    async def list_open_trades_for_signal(self, signal_id: int) -> list[dict]: ...
    async def update_trade_state(self, trade_id: int, *, state: str, sl: float | None = None) -> None: ...


class _AdapterProto(Protocol):
    async def modify_position_sl(self, position_id: str, new_sl: float) -> None: ...
    async def close_position(self, position_id: str) -> None: ...


class StatusHandler:
    def __init__(self, *, storage: _StorageProto, adapter: _AdapterProto | Any | None):
        self.storage = storage
        self.adapter = adapter

    async def handle(self, *, signal_id: int, sm: SignalStateMachine, update: StatusUpdate) -> None:
        actions = self._dispatch(sm, update)
        if not actions:
            return
        open_trades = await self.storage.list_open_trades_for_signal(signal_id)
        for action in actions:
            await self._apply_action(action, open_trades)
        log.info("Handled %s for signal %s; %d actions", update.kind, signal_id, len(actions))

    def _dispatch(self, sm: SignalStateMachine, update: StatusUpdate) -> list[StateAction]:
        if update.kind == "TP_HIT":
            if update.tp_index is None:
                log.warning("TP_HIT update missing tp_index; skipping")
                return []
            return sm.on_tp_hit(update.tp_index)
        if update.kind == "ALL_TPS_HIT":
            return sm.on_all_tps_hit()
        if update.kind == "SL_HIT":
            return sm.on_sl_hit()
        return []

    async def _apply_action(self, action: StateAction, open_trades: list[dict]) -> None:
        if action.kind == "MOVE_SL_TO_BE":
            await self._move_sl_to_be(open_trades)
        elif action.kind in ("CLOSE_TP1", "CLOSE_TPN"):
            await self._close_tp(action, open_trades)
        elif action.kind == "CLOSE_ALL":
            await self._close_all(open_trades)
        elif action.kind == "RECORD_SL":
            for t in open_trades:
                await self.storage.update_trade_state(t["id"], state="CLOSED")

    async def _move_sl_to_be(self, open_trades: list[dict]) -> None:
        for t in open_trades:
            if t["state"] != "OPEN":
                continue
            if self.adapter is not None:
                try:
                    await self.adapter.modify_position_sl(t["broker_order_id"], t["entry"])
                except Exception:
                    log.exception("modify_position_sl failed for trade %s", t["id"])
                    continue
            await self.storage.update_trade_state(t["id"], state="BE", sl=t["entry"])

    async def _close_tp(self, action: StateAction, open_trades: list[dict]) -> None:
        if not open_trades:
            return
        is_buy = open_trades[0]["direction"] == "BUY"
        ordered = sorted(open_trades, key=lambda t: t["tp"], reverse=not is_buy)
        idx = (action.tp_index or 1) - 1
        target = ordered[idx] if idx < len(ordered) else ordered[-1]
        if self.adapter is not None:
            try:
                await self.adapter.close_position(target["broker_order_id"])
            except Exception:
                log.exception("close_position failed for trade %s", target["id"])
                return
        await self.storage.update_trade_state(target["id"], state="CLOSED")

    async def _close_all(self, open_trades: list[dict]) -> None:
        for t in open_trades:
            if self.adapter is not None:
                try:
                    await self.adapter.close_position(t["broker_order_id"])
                except Exception:
                    log.exception("close_position failed for trade %s", t["id"])
                    continue
            await self.storage.update_trade_state(t["id"], state="CLOSED")
