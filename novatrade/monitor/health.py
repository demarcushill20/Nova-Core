"""Lightweight health monitoring for NovaTrade.

One-shot health snapshot: adapter health + account state + position summary.
Provider-neutral — only uses the MT5Adapter abstract interface.
"""

from __future__ import annotations

import logging

from novatrade.adapter.base import MT5Adapter
from novatrade.models import (
    AccountState,
    HealthSnapshot,
    HealthState,
    HealthStatus,
    Position,
)

log = logging.getLogger("novatrade.monitor.health")


async def take_health_snapshot(
    adapter: MT5Adapter,
    *,
    account: AccountState | None = None,
    positions: list[Position] | None = None,
) -> HealthSnapshot:
    """Capture a point-in-time health snapshot from the adapter.

    Safe to call at any time.  Never raises — returns a snapshot with
    error details on failure.

    To avoid burning MetaAPI rate-limit credits, callers may pre-fetch
    ``account`` and ``positions`` once per monitor cycle and pass them in.
    When provided, those values are used as-is and no broker API call is
    made for them.
    """
    # -- 1. Adapter health ---------------------------------------------------
    try:
        health = await adapter.health_check()
    except Exception as exc:
        log.error("health snapshot: adapter health_check failed: %s", exc)
        return HealthSnapshot(
            adapter_health=HealthStatus(
                state=HealthState.DOWN,
                message=str(exc),
            ),
            error=f"health_check failed: {exc}",
        )

    if health.state == HealthState.DOWN:
        log.warning("health snapshot: adapter DOWN — %s", health.message)
        return HealthSnapshot(adapter_health=health, error=health.message)

    # -- 2. Account state (use pre-fetched value if available) ---------------
    if account is None:
        try:
            account = await adapter.get_account()
        except Exception as exc:
            log.error("health snapshot: get_account failed: %s", exc)
            return HealthSnapshot(
                adapter_health=health,
                error=f"get_account failed: {exc}",
            )

    # -- 3. Positions summary (use pre-fetched value if available) -----------
    if positions is None:
        try:
            positions = await adapter.get_positions()
        except Exception as exc:
            log.error("health snapshot: get_positions failed: %s", exc)
            return HealthSnapshot(
                adapter_health=health,
                account=account,
                error=f"get_positions failed: {exc}",
            )

    total_pnl = sum(p.unrealized_pnl for p in positions)

    log.info(
        "health snapshot: %s balance=%.2f equity=%.2f positions=%d pnl=%.2f",
        health.state.value,
        account.balance,
        account.equity,
        len(positions),
        total_pnl,
    )

    return HealthSnapshot(
        adapter_health=health,
        account=account,
        position_count=len(positions),
        total_unrealized_pnl=total_pnl,
    )
