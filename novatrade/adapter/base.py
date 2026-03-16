"""Provider-neutral MT5 adapter interface.

Every broker adapter (cloud API, direct MT5, etc.) implements this ABC.
The contract guarantees that NovaTrade core logic never touches vendor
objects — all inputs and outputs use novatrade.models types.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from novatrade.models import (
    AccountState,
    Candle,
    HealthStatus,
    OrderRequest,
    OrderResult,
    Position,
    SymbolPrice,
)


class MT5Adapter(ABC):
    """Abstract boundary between NovaTrade and any MT5-compatible broker.

    Implementations must:
    - Translate vendor responses into NovaTrade-native models
    - Never expose SDK objects beyond this boundary
    - Handle reconnection internally
    - Support idempotency via OrderRequest.idempotency_key
    """

    # --- lifecycle -----------------------------------------------------------

    @abstractmethod
    async def connect(self) -> HealthStatus:
        """Establish connection to the broker.  Return health after attempt."""

    @abstractmethod
    async def disconnect(self) -> None:
        """Gracefully close the broker connection."""

    @abstractmethod
    async def health_check(self) -> HealthStatus:
        """Return current connection / service health."""

    # --- account -------------------------------------------------------------

    @abstractmethod
    async def get_account(self) -> AccountState:
        """Retrieve current account state snapshot."""

    # --- positions -----------------------------------------------------------

    @abstractmethod
    async def get_positions(self) -> list[Position]:
        """Return all open positions."""

    # --- market data ---------------------------------------------------------

    @abstractmethod
    async def get_symbol_price(self, symbol: str) -> SymbolPrice:
        """Get current bid/ask for *symbol*."""

    @abstractmethod
    async def get_candles(
        self,
        symbol: str,
        timeframe: str,
        count: int = 100,
    ) -> list[Candle]:
        """Retrieve the last *count* candles for *symbol* at *timeframe*."""

    # --- execution -----------------------------------------------------------

    @abstractmethod
    async def place_order(self, request: OrderRequest) -> OrderResult:
        """Submit an order.  Must respect idempotency_key if set."""

    @abstractmethod
    async def modify_order(
        self,
        order_id: str,
        *,
        stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> OrderResult:
        """Modify SL/TP on an existing order or position."""

    @abstractmethod
    async def close_position(
        self,
        position_id: str,
        volume: float | None = None,
    ) -> OrderResult:
        """Close (fully or partially) an open position."""
