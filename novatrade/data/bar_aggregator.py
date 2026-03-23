"""Tick-to-bar aggregation with multi-timeframe support.

Phase 2.2 of the Full-Python NovaTrade pipeline. Converts a stream of Tick
objects into completed OHLCV Candle objects on deterministic bar-close
boundaries.

Key design:
    - Bar boundaries use ``floor(ts / period) * period`` — matches backtest engine
    - Multi-timeframe: emits bars for ALL configured timeframes from one tick stream
    - IRB uses H1 + H4, so both timeframes run parallel accumulators
    - ``on_tick()`` returns a list of ``(timeframe, Candle)`` tuples

Public API:
    - BarAggregator: Multi-timeframe tick-to-bar converter
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from novatrade.data.tick import Tick
from novatrade.models import Candle

log = logging.getLogger("novatrade.data.bar_aggregator")


# Timeframe -> seconds mapping
TIMEFRAME_SECONDS: dict[str, int] = {
    "M1": 60,
    "M5": 300,
    "M15": 900,
    "M30": 1800,
    "H1": 3600,
    "H4": 14400,
    "D1": 86400,
}


def bar_boundary(timestamp: float, period_seconds: int) -> float:
    """Compute the bar-open boundary for a given timestamp.

    Bar boundaries align to UTC epoch: ``floor(ts / period) * period``.
    This matches the backtest engine's bar alignment for parity.
    """
    return float(int(timestamp // period_seconds) * period_seconds)


@dataclass
class _BuildingBar:
    """Accumulator for an in-progress bar."""

    symbol: str
    timeframe: str
    bar_open_ts: float  # bar boundary timestamp
    open: float = 0.0
    high: float = -1e18
    low: float = 1e18
    close: float = 0.0
    volume: float = 0.0
    tick_count: int = 0

    def update(self, mid: float, volume: float = 0.0) -> None:
        """Incorporate a new tick's mid price."""
        if self.tick_count == 0:
            self.open = mid
        self.high = max(self.high, mid)
        self.low = min(self.low, mid)
        self.close = mid
        self.volume += volume
        self.tick_count += 1

    def to_candle(self) -> Candle:
        """Convert to a completed Candle."""
        return Candle(
            timestamp=self.bar_open_ts,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
            symbol=self.symbol,
            timeframe=self.timeframe,
        )


class BarAggregator:
    """Multi-timeframe tick-to-bar aggregator.

    Maintains parallel bar accumulators for each configured timeframe.
    On each tick, updates all accumulators and returns completed bars
    when a bar boundary is crossed.

    Usage::

        agg = BarAggregator(timeframes=["H1", "H4"])
        for tick in tick_stream:
            completed = agg.on_tick(tick)
            for tf, candle in completed:
                process(tf, candle)

    Bar boundaries use ``floor(ts / period) * period`` (UTC epoch aligned),
    matching the backtest engine for signal parity.
    """

    def __init__(self, timeframes: list[str] | None = None) -> None:
        if timeframes is None:
            timeframes = ["H1"]

        # Validate timeframes
        for tf in timeframes:
            if tf not in TIMEFRAME_SECONDS:
                raise ValueError(f"Unsupported timeframe: {tf}. Supported: {sorted(TIMEFRAME_SECONDS.keys())}")

        self._timeframes = list(timeframes)
        self._periods = {tf: TIMEFRAME_SECONDS[tf] for tf in timeframes}

        # (symbol, timeframe) -> BuildingBar
        self._bars: dict[tuple[str, str], _BuildingBar] = {}

        # Counters
        self.ticks_processed: int = 0
        self.bars_emitted: int = 0

    @property
    def timeframes(self) -> list[str]:
        return list(self._timeframes)

    def on_tick(self, tick: Tick) -> list[tuple[str, Candle]]:
        """Process a tick and return any completed bars.

        Args:
            tick: Incoming price tick.

        Returns:
            List of ``(timeframe, Candle)`` tuples for bars that completed
            on this tick. May be empty (no boundary crossed), contain one
            entry (e.g. H1 closed), or multiple (e.g. H1 + H4 both closed
            at the same boundary).
        """
        self.ticks_processed += 1
        mid = tick.mid
        completed: list[tuple[str, Candle]] = []

        for tf in self._timeframes:
            period = self._periods[tf]
            tick_boundary = bar_boundary(tick.timestamp, period)
            key = (tick.symbol, tf)

            current = self._bars.get(key)

            if current is None:
                # First tick for this symbol/timeframe — start new bar
                current = _BuildingBar(
                    symbol=tick.symbol,
                    timeframe=tf,
                    bar_open_ts=tick_boundary,
                )
                current.update(mid, tick.volume)
                self._bars[key] = current
                continue

            if tick_boundary == current.bar_open_ts:
                # Same bar period — accumulate
                current.update(mid, tick.volume)
            else:
                # Bar boundary crossed — emit completed bar, start new one
                if current.tick_count > 0:
                    completed.append((tf, current.to_candle()))
                    self.bars_emitted += 1
                    log.debug(
                        "Bar closed: %s %s @ %.0f (ticks=%d)",
                        tick.symbol,
                        tf,
                        current.bar_open_ts,
                        current.tick_count,
                    )

                # Handle gap: if tick_boundary > current.bar_open_ts + period,
                # we skipped bar periods. We only emit the bar we were building;
                # gap bars are not synthesized (consistent with backtest engine).
                new_bar = _BuildingBar(
                    symbol=tick.symbol,
                    timeframe=tf,
                    bar_open_ts=tick_boundary,
                )
                new_bar.update(mid, tick.volume)
                self._bars[key] = new_bar

        return completed

    def force_close(self, symbol: str | None = None) -> list[tuple[str, Candle]]:
        """Force-close all building bars and return them.

        Useful for shutdown or testing. Returns partial bars (bars with
        at least one tick).

        Args:
            symbol: If given, only close bars for this symbol.
                   If None, close all bars.

        Returns:
            List of ``(timeframe, Candle)`` tuples for all closed bars.
        """
        completed: list[tuple[str, Candle]] = []
        keys_to_remove: list[tuple[str, str]] = []

        for key, bar in self._bars.items():
            sym, tf = key
            if symbol is not None and sym != symbol:
                continue
            if bar.tick_count > 0:
                completed.append((tf, bar.to_candle()))
                self.bars_emitted += 1
            keys_to_remove.append(key)

        for key in keys_to_remove:
            del self._bars[key]

        return completed

    def building_bar(self, symbol: str, timeframe: str) -> _BuildingBar | None:
        """Return the current building bar for introspection (or None)."""
        return self._bars.get((symbol, timeframe))

    @property
    def active_bars(self) -> int:
        """Number of bars currently being built."""
        return len(self._bars)

    @property
    def stats(self) -> dict:
        """Aggregator statistics."""
        return {
            "timeframes": self._timeframes,
            "ticks_processed": self.ticks_processed,
            "bars_emitted": self.bars_emitted,
            "active_bars": self.active_bars,
        }
