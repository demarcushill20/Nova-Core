"""In-memory expected-position tracker for NovaTrade reconciliation.

Tracks positions that NovaTrade expects to exist at the broker based on
successful ExecutionResults.  This is the MVP "expected state" source —
intentionally in-memory, resets on process restart.

For production use, this should be backed by persistent storage (Redis,
SQLite, etc.).
"""

from __future__ import annotations

import logging

from novatrade.models import (
    ExecutionOutcome,
    ExecutionResult,
    Position,
)

log = logging.getLogger("novatrade.monitor.position_tracker")


class PositionTracker:
    """In-memory tracker of positions NovaTrade expects at the broker.

    Feed it ExecutionResults after each execution attempt.  Query it
    during reconciliation to compare against broker state.

    Limitations (MVP):
    - Resets on process restart
    - Does not track partial closes or modifications
    - Position IDs come from OrderResult.order_id (may differ from
      broker position ID depending on provider)
    """

    def __init__(self) -> None:
        self._expected: dict[str, Position] = {}

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
