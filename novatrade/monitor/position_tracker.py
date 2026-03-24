"""Expected-position tracker for NovaTrade reconciliation.

Tracks positions that NovaTrade expects to exist at the broker based on
successful ExecutionResults.  Optionally backed by SQLite via StateStore
for persistence across process restarts.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from novatrade.models import (
    ExecutionOutcome,
    ExecutionResult,
    Position,
)

if TYPE_CHECKING:
    from novatrade.storage.state_store import StateStore

log = logging.getLogger("novatrade.monitor.position_tracker")


class PositionTracker:
    """Tracker of positions NovaTrade expects at the broker.

    Feed it ExecutionResults after each execution attempt.  Query it
    during reconciliation to compare against broker state.

    When a :class:`StateStore` is provided, positions are persisted to
    SQLite so they survive process restarts.  Without a store, the tracker
    operates in-memory only (backward compatible).

    Limitations:
    - Does not track partial closes or modifications
    - Position IDs come from OrderResult.order_id (may differ from
      broker position ID depending on provider)
    """

    def __init__(self, state_store: StateStore | None = None) -> None:
        self._state_store = state_store
        self._expected: dict[str, Position] = {}
        if state_store is not None:
            self._expected = state_store.load_expected_positions()
            if self._expected:
                log.info(
                    "position_tracker: restored %d positions from store",
                    len(self._expected),
                )

    def record(self, result: ExecutionResult) -> None:
        """Record an execution result.  Only FILLED outcomes are tracked."""
        if result.outcome != ExecutionOutcome.FILLED:
            return
        if result.order_result is None or result.risk_decision is None:
            return

        req = result.risk_decision.request
        if req is None:
            return

        order_id = result.order_result.order_id
        if not order_id:
            log.warning("position_tracker: FILLED result has no order_id — skipping")
            return

        pos = Position(
            position_id=order_id,
            symbol=req.symbol,
            side=req.side,
            volume=req.volume,
            open_price=result.order_result.fill_price or 0.0,
            stop_loss=req.stop_loss,
            take_profit=req.take_profit,
            open_time=result.timestamp,
            strategy_id=req.strategy_id,
            comment=req.comment,
        )
        self._expected[order_id] = pos
        if self._state_store is not None:
            self._state_store.save_expected_position(pos)
        log.info(
            "position_tracker: recorded %s %s %s vol=%.2f id=%s",
            pos.side.value,
            pos.symbol,
            req.order_type.value,
            pos.volume,
            order_id,
        )

    def remove(self, position_id: str) -> None:
        """Remove a position (e.g. after confirmed close)."""
        if position_id in self._expected:
            del self._expected[position_id]
            if self._state_store is not None:
                self._state_store.remove_expected_position(position_id)
            log.info("position_tracker: removed id=%s", position_id)

    @property
    def expected_positions(self) -> list[Position]:
        """Return a snapshot of all expected positions."""
        return list(self._expected.values())

    @property
    def count(self) -> int:
        return len(self._expected)

    def clear(self) -> None:
        """Clear all tracked positions."""
        self._expected.clear()
        if self._state_store is not None:
            self._state_store.clear_expected_positions()
