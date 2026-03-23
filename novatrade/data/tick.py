"""Tick data model and per-symbol tick buffer.

Phase 1.1 of the Full-Python NovaTrade pipeline. Provides the foundational
data type for live price ingestion from MetaApi.

Public API:
    - Tick: Immutable price tick from broker
    - TickBuffer: Per-symbol FIFO buffer with configurable capacity
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Tick:
    """Single price tick from broker. Immutable value object.

    All prices are raw broker values (not pip-normalized).
    Timestamp is unix epoch seconds (broker time).
    """

    symbol: str
    timestamp: float
    bid: float
    ask: float
    last: float = 0.0
    volume: float = 0.0

    @property
    def mid(self) -> float:
        """Midpoint price: (bid + ask) / 2."""
        return (self.bid + self.ask) / 2.0

    @property
    def spread(self) -> float:
        """Raw spread: ask - bid."""
        return self.ask - self.bid

    @property
    def spread_pips(self) -> float:
        """Spread in pips. Uses 0.01 for JPY pairs, 0.0001 for others."""
        pip_size = 0.01 if "JPY" in self.symbol.upper() else 0.0001
        if pip_size == 0:
            return 0.0
        return self.spread / pip_size

    def is_valid(self) -> bool:
        """Basic sanity: positive prices, bid <= ask, non-zero timestamp."""
        return self.bid > 0 and self.ask > 0 and self.bid <= self.ask and self.timestamp > 0


class TickBuffer:
    """Per-symbol FIFO tick buffer with configurable capacity.

    Stores the most recent ``max_size`` ticks per symbol using a deque.
    Thread-safe for single-writer scenarios (asyncio event loop).

    Usage::

        buf = TickBuffer(max_size=500)
        buf.push(tick)
        latest = buf.latest("EURUSD")
        recent = buf.since("EURUSD", time.time() - 60)
    """

    def __init__(self, max_size: int = 500) -> None:
        if max_size < 1:
            raise ValueError(f"max_size must be >= 1, got {max_size}")
        self._max_size = max_size
        self._buffers: dict[str, deque[Tick]] = {}

    def push(self, tick: Tick) -> None:
        """Append a tick to the symbol's buffer. Evicts oldest if at capacity."""
        symbol = tick.symbol
        if symbol not in self._buffers:
            self._buffers[symbol] = deque(maxlen=self._max_size)
        self._buffers[symbol].append(tick)

    def latest(self, symbol: str) -> Tick | None:
        """Return the most recent tick for ``symbol``, or None if empty."""
        buf = self._buffers.get(symbol)
        if not buf:
            return None
        return buf[-1]

    def since(self, symbol: str, ts: float) -> list[Tick]:
        """Return all ticks for ``symbol`` with timestamp >= ``ts``."""
        buf = self._buffers.get(symbol)
        if not buf:
            return []
        return [t for t in buf if t.timestamp >= ts]

    def all_ticks(self, symbol: str) -> list[Tick]:
        """Return all buffered ticks for ``symbol`` (oldest first)."""
        buf = self._buffers.get(symbol)
        if not buf:
            return []
        return list(buf)

    def clear(self, symbol: str | None = None) -> None:
        """Clear buffer for one symbol, or all symbols if None."""
        if symbol is None:
            self._buffers.clear()
        elif symbol in self._buffers:
            self._buffers[symbol].clear()

    @property
    def symbols(self) -> list[str]:
        """List of symbols with buffered ticks."""
        return list(self._buffers.keys())

    def count(self, symbol: str) -> int:
        """Number of ticks buffered for ``symbol``."""
        buf = self._buffers.get(symbol)
        return len(buf) if buf else 0

    @property
    def max_size(self) -> int:
        return self._max_size

    @property
    def stats(self) -> dict:
        """Per-symbol buffer statistics."""
        return {
            "max_size": self._max_size,
            "symbols": {
                sym: {
                    "count": len(buf),
                    "oldest_ts": buf[0].timestamp if buf else None,
                    "newest_ts": buf[-1].timestamp if buf else None,
                }
                for sym, buf in self._buffers.items()
            },
        }
