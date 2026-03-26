"""Async tick stream from broker price polling.

Phase 2.1 of the Full-Python NovaTrade pipeline. Provides a continuous
async generator that polls the broker for current prices and yields
deduplicated Tick objects.

Public API:
    - TickBatchPoller: Async tick stream via adapter polling
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from novatrade.data.tick import Tick

if TYPE_CHECKING:
    from novatrade.adapter.base import MT5Adapter

log = logging.getLogger("novatrade.data.price_feed")


class TickBatchPoller:
    """Polls broker for current prices and yields deduplicated Tick objects.

    Wraps ``MT5Adapter.get_symbol_price()`` in an async loop with:
    - Configurable poll interval (default 0.5s)
    - Deduplication: skips if bid/ask unchanged from last poll
    - Zero-price guard (MetaApi occasionally returns 0.0)
    - Elapsed-time-aware sleep (subtracts poll duration from interval)
    - Graceful shutdown via ``stop()``

    Usage::

        poller = TickBatchPoller(adapter, ["EURUSD"], interval=0.5)
        async for tick in poller.stream():
            process(tick)
    """

    def __init__(
        self,
        adapter: MT5Adapter,
        symbols: list[str],
        interval: float = 0.5,
        broker_map: dict[str, str] | None = None,
    ) -> None:
        if interval <= 0:
            raise ValueError(f"interval must be > 0, got {interval}")
        if not symbols:
            raise ValueError("symbols list must not be empty")

        self._adapter = adapter
        self._symbols = list(symbols)
        self._interval = interval
        self._broker_map = broker_map or {}  # display_symbol → broker_symbol
        self._running = False
        self._last_prices: dict[str, tuple[float, float]] = {}  # symbol -> (bid, ask)
        self.ticks_yielded: int = 0
        self.polls: int = 0
        self.errors: int = 0
        self._consecutive_errors: int = 0
        self._CONSECUTIVE_ERROR_THRESHOLD: int = 10  # trigger resub after N errors

    @property
    def running(self) -> bool:
        return self._running

    @property
    def symbols(self) -> list[str]:
        return list(self._symbols)

    @property
    def interval(self) -> float:
        return self._interval

    def stop(self) -> None:
        """Signal the poller to stop after the current iteration."""
        self._running = False

    async def stream(self) -> AsyncIterator[Tick]:
        """Async generator yielding deduplicated Tick objects.

        Polls each symbol in sequence, yields ticks where bid/ask changed.
        Runs until ``stop()`` is called.

        Yields:
            Tick objects for each price change detected.
        """
        self._running = True
        log.info(
            "TickBatchPoller starting: symbols=%s interval=%.2fs",
            self._symbols,
            self._interval,
        )

        try:
            while self._running:
                loop_start = time.monotonic()

                for symbol in self._symbols:
                    if not self._running:
                        break

                    try:
                        self.polls += 1
                        broker_sym = self._broker_map.get(symbol, symbol)
                        price = await self._adapter.get_symbol_price(broker_sym)

                        # Zero-price guard
                        if price.bid <= 0 or price.ask <= 0:
                            log.warning(
                                "Zero/negative price for %s: bid=%.5f ask=%.5f — skipping",
                                symbol,
                                price.bid,
                                price.ask,
                            )
                            continue

                        # Deduplication: skip if bid/ask unchanged
                        last = self._last_prices.get(symbol)
                        if last is not None and last == (price.bid, price.ask):
                            continue

                        self._last_prices[symbol] = (price.bid, price.ask)
                        self._consecutive_errors = 0  # reset on success

                        tick = Tick(
                            symbol=symbol,
                            timestamp=price.timestamp,
                            bid=price.bid,
                            ask=price.ask,
                        )
                        self.ticks_yielded += 1
                        yield tick

                    except Exception:
                        self.errors += 1
                        self._consecutive_errors += 1
                        log.exception("Error polling %s — continuing", symbol)

                        # Trigger resubscription after consecutive errors
                        if self._consecutive_errors >= self._CONSECUTIVE_ERROR_THRESHOLD:
                            log.warning(
                                "TickBatchPoller: %d consecutive errors — requesting resubscription",
                                self._consecutive_errors,
                            )
                            try:
                                if hasattr(self._adapter, "subscribe_to_market_data"):
                                    broker_syms = [self._broker_map.get(s, s) for s in self._symbols]
                                    await self._adapter.subscribe_to_market_data(broker_syms)
                                    log.info("Resubscription triggered after error threshold")
                                    self._consecutive_errors = 0
                            except Exception:
                                log.exception("Resubscription failed")

                # Elapsed-time-aware sleep
                elapsed = time.monotonic() - loop_start
                sleep_time = max(0, self._interval - elapsed)
                if sleep_time > 0 and self._running:
                    await asyncio.sleep(sleep_time)
        finally:
            self._running = False
            log.info(
                "TickBatchPoller stopped: %d ticks yielded, %d polls, %d errors",
                self.ticks_yielded,
                self.polls,
                self.errors,
            )

    @property
    def stats(self) -> dict:
        """Current poller statistics."""
        return {
            "running": self._running,
            "symbols": self._symbols,
            "interval": self._interval,
            "ticks_yielded": self.ticks_yielded,
            "polls": self.polls,
            "errors": self.errors,
        }
