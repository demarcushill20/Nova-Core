"""Dry-run adapter stub — Phase 8.

Provides a safe adapter that never touches a real broker.  All order
operations return deterministic, successful-looking results with
``dry_run=True`` markers so that evidence, reconciliation, and the
Trading Agent can exercise their full paths without side effects.

The DryRunAdapter wraps any real MT5Adapter and intercepts mutating
calls (place_order, modify_order, close_position, cancel_order) while
passing through read-only calls (health_check, get_account,
get_positions, get_orders, get_symbol_price, get_candles) to the
underlying adapter **or** to built-in stubs if no real adapter is
available.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from novatrade.adapter.base import MT5Adapter
from novatrade.models import (
    AccountMode,
    AccountState,
    Candle,
    HealthState,
    HealthStatus,
    OrderRequest,
    OrderResult,
    OrderStatus,
    PendingOrder,
    Position,
    SymbolPrice,
)

log = logging.getLogger("novatrade.runtime.dry_run")

# ---------------------------------------------------------------------------
# Simulated state tracking
# ---------------------------------------------------------------------------


@dataclass
class DryRunState:
    """Minimal state tracker for dry-run simulated order lifecycle."""

    next_order_id: int = 1
    pending_orders: dict[str, PendingOrder] = field(default_factory=dict)
    positions: dict[str, Position] = field(default_factory=dict)
    fills: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# DryRunAdapter
# ---------------------------------------------------------------------------


class DryRunAdapter(MT5Adapter):
    """Adapter that simulates broker operations for dry-run testing.

    Read-only operations delegate to *inner* if provided, otherwise
    return safe defaults.  Mutating operations always simulate locally.
    """

    def __init__(self, inner: MT5Adapter | None = None) -> None:
        self._inner = inner
        self._state = DryRunState()
        self._connected = True

    @property
    def dry_run(self) -> bool:
        return True

    # -- Connection ------------------------------------------------------------

    async def connect(self) -> HealthStatus:
        if self._inner:
            return await self._inner.connect()
        self._connected = True
        return HealthStatus(
            state=HealthState.OK,
            connected=True,
            latency_ms=1.0,
            message="dry-run adapter connected",
        )

    async def disconnect(self) -> None:
        if self._inner:
            await self._inner.disconnect()
        self._connected = False

    async def health_check(self) -> HealthStatus:
        if self._inner:
            return await self._inner.health_check()
        return HealthStatus(
            state=HealthState.OK,
            connected=self._connected,
            latency_ms=1.0,
            message="dry-run adapter healthy",
        )

    # -- Read-only (delegate or stub) ------------------------------------------

    async def get_account(self) -> AccountState:
        if self._inner:
            return await self._inner.get_account()
        return AccountState(
            balance=100_000.0,
            equity=100_000.0,
            margin=0.0,
            free_margin=100_000.0,
            currency="USD",
            leverage=100,
            mode=AccountMode.DEMO,
            open_positions=len(self._state.positions),
        )

    def get_equity(self) -> float | None:
        if self._inner:
            return self._inner.get_equity()
        return 100_000.0

    async def get_positions(self) -> list[Position]:
        if self._inner:
            return await self._inner.get_positions()
        return list(self._state.positions.values())

    async def get_orders(self) -> list[PendingOrder]:
        if self._inner:
            return await self._inner.get_orders()
        return list(self._state.pending_orders.values())

    async def get_symbol_price(self, symbol: str) -> SymbolPrice:
        if self._inner:
            return await self._inner.get_symbol_price(symbol)
        return SymbolPrice(symbol=symbol, bid=1.08500, ask=1.08520)

    async def get_candles(self, symbol: str, timeframe: str, count: int = 100) -> list[Candle]:
        if self._inner:
            return await self._inner.get_candles(symbol, timeframe, count)
        return []

    # -- Mutating (always simulated) -------------------------------------------

    async def place_order(self, request: OrderRequest) -> OrderResult:
        oid = f"DRY-{self._state.next_order_id:06d}"
        self._state.next_order_id += 1

        # Track as pending order
        pending = PendingOrder(
            order_id=oid,
            symbol=request.symbol,
            side=request.side,
            order_type=request.order_type,
            volume=request.volume,
            open_price=request.price or 0.0,
            stop_loss=request.stop_loss,
            created_time=time.time(),
        )
        self._state.pending_orders[oid] = pending
        log.info(
            "[DRY-RUN] place_order: %s %s %s vol=%.2f @ %.5f → %s",
            request.symbol,
            request.side.value,
            request.order_type.value,
            request.volume,
            request.price or 0.0,
            oid,
        )
        return OrderResult(
            ok=True,
            order_id=oid,
            status=OrderStatus.PENDING,
            fill_price=0.0,
            filled_volume=0.0,
        )

    async def modify_order(
        self,
        order_id: str,
        *,
        stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> OrderResult:
        log.info(
            "[DRY-RUN] modify_order: %s sl=%s tp=%s",
            order_id,
            stop_loss,
            take_profit,
        )
        return OrderResult(ok=True, order_id=order_id, status=OrderStatus.FILLED)

    async def close_position(self, position_id: str, volume: float | None = None) -> OrderResult:
        self._state.positions.pop(position_id, None)
        log.info("[DRY-RUN] close_position: %s vol=%s", position_id, volume)
        return OrderResult(ok=True, order_id=position_id, status=OrderStatus.FILLED)

    async def cancel_order(self, order_id: str) -> OrderResult:
        self._state.pending_orders.pop(order_id, None)
        log.info("[DRY-RUN] cancel_order: %s", order_id)
        return OrderResult(ok=True, order_id=order_id, status=OrderStatus.CANCELLED)

    # -- Dry-run simulation helpers --------------------------------------------

    def simulate_fill(self, order_id: str, fill_price: float = 0.0) -> bool:
        """Simulate a pending order becoming a filled position.

        Returns True if the order was found and converted.
        """
        pending = self._state.pending_orders.pop(order_id, None)
        if pending is None:
            return False
        price = fill_price or pending.open_price
        pos = Position(
            position_id=order_id,
            symbol=pending.symbol,
            side=pending.side,
            volume=pending.volume,
            open_price=price,
            current_price=price,
            unrealized_pnl=0.0,
            stop_loss=pending.stop_loss,
        )
        self._state.positions[order_id] = pos
        self._state.fills.append(order_id)
        log.info("[DRY-RUN] simulate_fill: %s @ %.5f", order_id, price)
        return True

    def simulate_broker_close(self, position_id: str) -> bool:
        """Simulate the broker closing a position (SL hit)."""
        pos = self._state.positions.pop(position_id, None)
        if pos is None:
            return False
        log.info("[DRY-RUN] simulate_broker_close: %s", position_id)
        return True

    def reset(self) -> None:
        """Reset all simulated state."""
        self._state = DryRunState()
