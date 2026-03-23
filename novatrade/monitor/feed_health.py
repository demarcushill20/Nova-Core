"""Feed Health Supervisor — gates trading on data-feed quality.

Phase 3 of the Full-Python NovaTrade pipeline. Monitors 8 failure domains
and provides a hard gate (`is_tradeable`) that blocks order flow when the
data feed is unhealthy.

Failure domains:
    1. Staleness — no tick received within threshold
    2. Clock drift — broker time vs system time divergence
    3. Spread widening — absolute or relative spread spike
    4. Disconnection — adapter reports down
    5. Duplicate signal suppression — too many signals in window
    6. Market closed — weekend / holiday detection
    7. Symbol not subscribed — never received data
    8. Stale timestamps — broker timestamp not advancing

Public API:
    - FeedState: health enum per symbol
    - FeedHealthConfig: frozen config dataclass
    - FeedHealthSnapshot: point-in-time health for one symbol
    - FeedHealthSupervisor: the supervisor itself
"""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

from novatrade.data.tick import Tick

_ClockFn = Callable[[], float]

log = logging.getLogger("novatrade.monitor.feed_health")


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class FeedState(Enum):
    """Per-symbol feed health state."""

    HEALTHY = "HEALTHY"
    STALE = "STALE"
    CLOCK_DRIFT = "CLOCK_DRIFT"
    SPREAD_WIDE = "SPREAD_WIDE"
    DISCONNECTED = "DISCONNECTED"
    MARKET_CLOSED = "MARKET_CLOSED"
    NOT_SUBSCRIBED = "NOT_SUBSCRIBED"
    STALE_TIMESTAMP = "STALE_TIMESTAMP"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FeedHealthConfig:
    """Immutable configuration for FeedHealthSupervisor."""

    max_stale_seconds: float = 30.0
    max_clock_drift_seconds: float = 5.0
    max_spread_pips: float = 5.0
    spread_window: int = 20
    spread_spike_ratio: float = 3.0
    signal_dedup_window: float = 60.0
    max_signals_in_window: int = 2
    stale_timestamp_threshold: float = 10.0


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass
class FeedHealthSnapshot:
    """Point-in-time health for one symbol."""

    symbol: str
    state: FeedState
    last_tick_age: float = 0.0
    clock_drift: float = 0.0
    current_spread_pips: float = 0.0
    avg_spread_pips: float = 0.0
    tick_count: int = 0
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "state": self.state.value,
            "last_tick_age": round(self.last_tick_age, 2),
            "clock_drift": round(self.clock_drift, 2),
            "current_spread_pips": round(self.current_spread_pips, 3),
            "avg_spread_pips": round(self.avg_spread_pips, 3),
            "tick_count": self.tick_count,
            "timestamp": self.timestamp,
        }


# ---------------------------------------------------------------------------
# Internal per-symbol state
# ---------------------------------------------------------------------------


class _SymbolState:
    """Mutable tracking state for one symbol."""

    __slots__ = (
        "last_broker_ts",
        "last_measured_drift",
        "last_tick_ts",
        "signal_times",
        "spread_history",
        "tick_count",
    )

    def __init__(self, spread_window: int) -> None:
        self.last_tick_ts: float = 0.0
        self.last_broker_ts: float = 0.0
        self.last_measured_drift: float = 0.0
        self.tick_count: int = 0
        self.spread_history: deque[float] = deque(maxlen=spread_window)
        self.signal_times: deque[float] = deque()


# ---------------------------------------------------------------------------
# Supervisor
# ---------------------------------------------------------------------------

# Forex market is closed from Friday 22:00 UTC to Sunday 22:00 UTC.
_FRIDAY = 4
_SUNDAY = 6


def _is_forex_market_closed(now: float) -> bool:
    """Return True if forex market is closed (Fri 22:00 - Sun 22:00 UTC)."""
    import datetime

    dt = datetime.datetime.fromtimestamp(now, tz=datetime.timezone.utc)
    weekday = dt.weekday()  # Mon=0 … Sun=6
    hour = dt.hour

    # Friday 22:00+ → closed
    if weekday == _FRIDAY and hour >= 22:
        return True
    # Saturday → closed
    if weekday == 5:
        return True
    # Sunday before 22:00 → closed
    return bool(weekday == _SUNDAY and hour < 22)


class FeedHealthSupervisor:
    """8-domain health watchdog that gates trading on data quality.

    Usage::

        supervisor = FeedHealthSupervisor()
        state = supervisor.on_tick(tick)
        if supervisor.is_tradeable("EURUSD"):
            # safe to trade
            ...

    Fail-closed: any evaluation error returns a non-healthy state.
    """

    def __init__(
        self,
        config: FeedHealthConfig | None = None,
        *,
        clock: _ClockFn | None = None,
    ) -> None:
        self._config = config or FeedHealthConfig()
        self._clock = clock or time.time
        self._symbols: dict[str, _SymbolState] = {}
        self._disconnected: set[str] = set()

    # -- Core API -----------------------------------------------------------

    def on_tick(self, tick: Tick) -> FeedState:
        """Process a tick and return the symbol's current health state.

        Call this for every tick received from the price feed. The returned
        state reflects the health assessment *after* incorporating this tick.
        """
        try:
            return self._assess_tick(tick)
        except Exception as exc:
            log.error("feed_health on_tick error for %s: %s", tick.symbol, exc)
            return FeedState.DISCONNECTED

    def check_staleness(self) -> dict[str, FeedState]:
        """Periodic staleness check — call every 5-10s from a timer.

        Returns per-symbol state. Symbols that haven't sent a tick within
        ``max_stale_seconds`` are marked STALE.
        """
        now = self._clock()
        result: dict[str, FeedState] = {}
        for symbol, ss in self._symbols.items():
            if symbol in self._disconnected:
                result[symbol] = FeedState.DISCONNECTED
            elif ss.last_tick_ts == 0.0:
                result[symbol] = FeedState.NOT_SUBSCRIBED
            elif _is_forex_market_closed(now):
                result[symbol] = FeedState.MARKET_CLOSED
            elif (now - ss.last_tick_ts) > self._config.max_stale_seconds:
                result[symbol] = FeedState.STALE
            else:
                result[symbol] = FeedState.HEALTHY
        return result

    def is_tradeable(self, symbol: str) -> bool:
        """Hard gate — only HEALTHY symbols may trade."""
        snap = self.get_snapshot(symbol)
        return snap.state == FeedState.HEALTHY

    def is_duplicate_signal(self, symbol: str, direction: str) -> bool:
        """Return True if too many signals fired within the dedup window.

        Tracks signals per (symbol, direction) key.
        """
        key = f"{symbol}:{direction}"
        now = self._clock()
        window = self._config.signal_dedup_window

        # Lazily create state for the dedup key
        if key not in self._symbols:
            self._symbols[key] = _SymbolState(self._config.spread_window)

        ss = self._symbols[key]
        # Purge expired entries
        while ss.signal_times and (now - ss.signal_times[0]) > window:
            ss.signal_times.popleft()

        if len(ss.signal_times) >= self._config.max_signals_in_window:
            log.warning(
                "feed_health: duplicate signal suppressed %s %s (%d in %.0fs)",
                symbol,
                direction,
                len(ss.signal_times),
                window,
            )
            return True

        ss.signal_times.append(now)
        return False

    def mark_disconnected(self, symbol: str) -> None:
        """Mark a symbol as disconnected (called by adapter layer)."""
        self._disconnected.add(symbol)
        log.warning("feed_health: %s marked DISCONNECTED", symbol)

    def mark_reconnected(self, symbol: str) -> None:
        """Clear disconnected state for a symbol."""
        self._disconnected.discard(symbol)
        log.info("feed_health: %s reconnected", symbol)

    def get_snapshot(self, symbol: str) -> FeedHealthSnapshot:
        """Build a point-in-time health snapshot for one symbol."""
        now = self._clock()
        ss = self._symbols.get(symbol)

        if symbol in self._disconnected:
            return FeedHealthSnapshot(
                symbol=symbol,
                state=FeedState.DISCONNECTED,
                timestamp=now,
            )

        if ss is None or ss.last_tick_ts == 0.0:
            return FeedHealthSnapshot(
                symbol=symbol,
                state=FeedState.NOT_SUBSCRIBED,
                timestamp=now,
            )

        age = now - ss.last_tick_ts
        drift = ss.last_measured_drift
        current_spread = ss.spread_history[-1] if ss.spread_history else 0.0
        avg_spread = sum(ss.spread_history) / len(ss.spread_history) if ss.spread_history else 0.0

        state = self._evaluate_state(symbol, age, drift, current_spread, avg_spread, now)

        return FeedHealthSnapshot(
            symbol=symbol,
            state=state,
            last_tick_age=age,
            clock_drift=drift,
            current_spread_pips=current_spread,
            avg_spread_pips=avg_spread,
            tick_count=ss.tick_count,
            timestamp=now,
        )

    def get_all_snapshots(self) -> dict[str, FeedHealthSnapshot]:
        """Health snapshot for every tracked symbol."""
        return {sym: self.get_snapshot(sym) for sym in self._symbols}

    @property
    def tracked_symbols(self) -> list[str]:
        """List of symbols we have seen ticks for (excludes dedup keys)."""
        return [s for s in self._symbols if ":" not in s]

    @property
    def stats(self) -> dict:
        """Aggregate statistics."""
        snaps = {sym: self.get_snapshot(sym) for sym in self._symbols if ":" not in sym}
        healthy = sum(1 for s in snaps.values() if s.state == FeedState.HEALTHY)
        return {
            "tracked_symbols": len(snaps),
            "healthy": healthy,
            "unhealthy": len(snaps) - healthy,
            "disconnected": list(self._disconnected),
            "symbols": {sym: s.to_dict() for sym, s in snaps.items()},
        }

    # -- Internal -----------------------------------------------------------

    def _assess_tick(self, tick: Tick) -> FeedState:
        """Incorporate a tick and return the symbol's assessed state."""
        symbol = tick.symbol
        now = self._clock()

        # Initialize state on first tick
        if symbol not in self._symbols:
            self._symbols[symbol] = _SymbolState(self._config.spread_window)

        ss = self._symbols[symbol]

        # Measure drift at tick arrival (not later)
        drift = abs(now - tick.timestamp)

        # Update tracking
        ss.tick_count += 1
        ss.last_tick_ts = now
        ss.last_broker_ts = tick.timestamp
        ss.last_measured_drift = drift
        ss.spread_history.append(tick.spread_pips)

        # Clear disconnect if we're getting ticks
        self._disconnected.discard(symbol)

        # Compute metrics
        age = 0.0  # Just received a tick
        current_spread = tick.spread_pips
        avg_spread = sum(ss.spread_history) / len(ss.spread_history) if ss.spread_history else current_spread

        return self._evaluate_state(symbol, age, drift, current_spread, avg_spread, now)

    def _evaluate_state(
        self,
        symbol: str,
        age: float,
        drift: float,
        current_spread: float,
        avg_spread: float,
        now: float,
    ) -> FeedState:
        """Sequential rule evaluation — fail-closed, first match wins."""
        cfg = self._config

        # 1. Disconnection (external signal)
        if symbol in self._disconnected:
            return FeedState.DISCONNECTED

        # 2. Market closed
        if _is_forex_market_closed(now):
            return FeedState.MARKET_CLOSED

        # 3. Staleness
        if age > cfg.max_stale_seconds:
            log.warning("feed_health: %s STALE (age=%.1fs)", symbol, age)
            return FeedState.STALE

        # 4. Clock drift
        if drift > cfg.max_clock_drift_seconds:
            log.warning("feed_health: %s CLOCK_DRIFT (drift=%.1fs)", symbol, drift)
            return FeedState.CLOCK_DRIFT

        # 5. Stale timestamps (broker time not advancing)
        ss = self._symbols.get(symbol)
        if ss and ss.tick_count > 1 and ss.last_broker_ts > 0:
            # Check if broker timestamp is stuck (way behind system time)
            broker_age = now - ss.last_broker_ts
            if broker_age > cfg.stale_timestamp_threshold and age <= cfg.max_stale_seconds:
                log.warning(
                    "feed_health: %s STALE_TIMESTAMP (broker_age=%.1fs)",
                    symbol,
                    broker_age,
                )
                return FeedState.STALE_TIMESTAMP

        # 6. Spread widening — absolute threshold
        if current_spread > cfg.max_spread_pips:
            log.warning(
                "feed_health: %s SPREAD_WIDE (%.1f > %.1f pips)",
                symbol,
                current_spread,
                cfg.max_spread_pips,
            )
            return FeedState.SPREAD_WIDE

        # 7. Spread widening — relative spike (>3x average)
        if avg_spread > 0 and current_spread > avg_spread * cfg.spread_spike_ratio:
            log.warning(
                "feed_health: %s SPREAD_WIDE (%.1f > %.1fx avg %.1f)",
                symbol,
                current_spread,
                cfg.spread_spike_ratio,
                avg_spread,
            )
            return FeedState.SPREAD_WIDE

        return FeedState.HEALTHY
