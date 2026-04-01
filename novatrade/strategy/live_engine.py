"""Live Strategy Engine — bar-by-bar bridge from live data to strategy signals.

This is the live equivalent of ``IRBBacktester``.  It receives completed bars
from ``BarAggregator``, maintains rolling candle buffers, runs the strategy's
``check_entry`` / ``check_exit`` on each new primary-timeframe bar, and emits
``LiveSignal`` objects that the execution layer (``TradingAgent``) can act on.

The state machine mirrors the backtest engine exactly (FLAT → PENDING → LONG/
SHORT → FLAT) so that live behaviour matches backtested behaviour.

Usage::

    engine = LiveStrategyEngine(strategy=IRBStrategy(env), env=env)
    engine.seed_history(h1_history, h4_history)

    # On each completed bar from BarAggregator:
    signals = engine.on_bar(bar, timeframe="H1")
    for sig in signals:
        trading_agent.process_alert(sig.to_alert_payload())
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from novatrade.backtest.environment import BacktestEnvironment
from novatrade.models import Candle
from novatrade.strategies.base import BaseStrategy, EntrySignal, ExitSignal

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LiveConfig:
    """Configuration for the LiveStrategyEngine."""

    symbol: str = "EURUSD"
    primary_timeframe: str = "H1"
    higher_timeframe: str = "H4"
    max_candles: int = 500  # rolling buffer cap
    trigger_window_bars: int = 20  # pending order expiry


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------


class LiveState(Enum):
    """Engine state — mirrors the backtest StrategyState."""

    WARMING_UP = "WARMING_UP"
    FLAT = "FLAT"
    PENDING_LONG = "PENDING_LONG"
    PENDING_SHORT = "PENDING_SHORT"
    LONG = "LONG"
    SHORT = "SHORT"


# ---------------------------------------------------------------------------
# Internal position / order tracking
# ---------------------------------------------------------------------------


@dataclass
class PendingOrder:
    """A pending stop order waiting for fill."""

    side: str  # "LONG" or "SHORT"
    entry_price: float
    stop_loss: float
    volume: float
    bar_index: int  # buffer index when placed
    bars_alive: int = 0


@dataclass
class OpenPosition:
    """Lightweight position state for exit management."""

    side: str  # "LONG" or "SHORT"
    entry_price: float
    stop_loss: float
    volume: float
    entry_bar: int  # buffer index at entry
    current_stop: float = 0.0
    best_close: float = 0.0
    bars_held: int = 0

    def __post_init__(self) -> None:
        if self.current_stop == 0.0:
            self.current_stop = self.stop_loss
        if self.best_close == 0.0:
            self.best_close = self.entry_price


# ---------------------------------------------------------------------------
# Signal output
# ---------------------------------------------------------------------------


class SignalType(Enum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"
    MODIFY_SL = "MODIFY_SL"
    CANCEL_PENDING = "CANCEL_PENDING"
    PENDING_FILL = "PENDING_FILL"


@dataclass
class LiveSignal:
    """Signal emitted by the LiveStrategyEngine.

    Downstream consumers (TradingAgent) translate these into OrderIntents.
    """

    signal_type: SignalType
    side: str  # "LONG" or "SHORT"
    symbol: str
    entry_price: float = 0.0
    stop_loss: float = 0.0
    volume: float = 0.0
    exit_price: float = 0.0
    exit_reason: str = ""
    new_stop: float = 0.0
    timestamp: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)
    _db_signal_id: int | None = field(default=None, repr=False)

    def to_alert_payload(self) -> dict[str, Any]:
        """Convert to a dict compatible with TradingAgent.process_alert."""
        payload: dict[str, Any] = {
            "symbol": self.symbol,
            "side": self.side,
            "signal_type": self.signal_type.value,
        }
        if self.signal_type == SignalType.ENTRY:
            payload.update(
                {
                    "action": "ENTRY",
                    "entry_price": self.entry_price,
                    "stop_loss": self.stop_loss,
                    "volume": self.volume,
                }
            )
        elif self.signal_type == SignalType.EXIT:
            payload.update(
                {
                    "action": "EXIT",
                    "exit_price": self.exit_price,
                    "exit_reason": self.exit_reason,
                }
            )
        elif self.signal_type == SignalType.MODIFY_SL:
            payload.update(
                {
                    "action": "MODIFY_SL",
                    "new_stop_loss": self.new_stop,
                }
            )
        if self.metadata:
            payload["metadata"] = self.metadata
        return payload


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class LiveStrategyEngine:
    """Bar-by-bar live strategy engine.

    Receives completed bars, maintains rolling candle buffers, runs the
    strategy interface, and emits signals.  The state machine exactly mirrors
    ``IRBBacktester`` for live/backtest parity.
    """

    def __init__(
        self,
        strategy: BaseStrategy,
        env: BacktestEnvironment,
        config: LiveConfig | None = None,
    ) -> None:
        self._strategy = strategy
        self._env = env
        self._config = config or LiveConfig(
            symbol=env.symbol_display,
            trigger_window_bars=env.trigger_window_bars,
        )

        # Candle buffers (rolling)
        self._h1_candles: list[Candle] = []
        self._h4_candles: list[Candle] = []

        # State
        self._state = LiveState.WARMING_UP
        self._pending: PendingOrder | None = None
        self._position: OpenPosition | None = None

        # Counters
        self._total_bars: int = 0
        self._equity: float = env.initial_equity
        self._consecutive_losses: int = 0

        # Cached indicators (recomputed each H1 bar)
        self._indicators: dict[str, list[float]] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def seed_history(
        self,
        h1_candles: list[Candle],
        h4_candles: list[Candle] | None = None,
    ) -> None:
        """Pre-seed candle buffers with historical data for warmup.

        Call this once before processing live bars.  If enough bars are
        provided (>= warmup_bars), the engine transitions to FLAT immediately.
        """
        self._h1_candles = list(h1_candles[-self._config.max_candles :])
        self._h4_candles = list((h4_candles or [])[-self._config.max_candles :])
        self._total_bars = len(self._h1_candles)

        if len(self._h1_candles) >= self._env.warmup_bars:
            self._state = LiveState.FLAT
            self._indicators = self._strategy.compute_indicators(self._h1_candles)
            log.info(
                "Seeded %d H1 + %d H4 candles — engine READY",
                len(self._h1_candles),
                len(self._h4_candles),
            )
        else:
            log.info(
                "Seeded %d H1 candles — need %d for warmup",
                len(self._h1_candles),
                self._env.warmup_bars,
            )

    def on_bar(self, bar: Candle, timeframe: str) -> list[LiveSignal]:
        """Process a completed bar and return any generated signals.

        Args:
            bar: Completed OHLCV bar from BarAggregator.
            timeframe: Bar timeframe ("H1", "H4", etc.).

        Returns:
            List of LiveSignal objects (may be empty).
        """
        # Higher timeframe: just accumulate, no signal generation
        if timeframe == self._config.higher_timeframe:
            self._h4_candles.append(bar)
            self._trim(self._h4_candles)
            return []

        # Only process primary timeframe
        if timeframe != self._config.primary_timeframe:
            return []

        # Append to buffer
        self._h1_candles.append(bar)
        self._trim(self._h1_candles)
        self._total_bars += 1

        # Check warmup
        if len(self._h1_candles) < self._env.warmup_bars:
            return []

        if self._state == LiveState.WARMING_UP:
            self._state = LiveState.FLAT

        # Recompute indicators on full buffer
        self._indicators = self._strategy.compute_indicators(self._h1_candles)

        i = len(self._h1_candles) - 1
        signals: list[LiveSignal] = []

        # 1. Check pending order fill
        if self._pending is not None:
            self._pending.bars_alive += 1
            fill_sig = self._check_pending_fill(i, bar)
            if fill_sig is not None:
                signals.append(fill_sig)

        # 2. Manage existing position (exit checks, trailing stop)
        if self._position is not None and self._state in (
            LiveState.LONG,
            LiveState.SHORT,
        ):
            self._position.bars_held += 1

            exit_sig = self._check_exit(i, bar)
            if exit_sig is not None:
                signals.append(exit_sig)
                return signals  # exited — don't evaluate new entry this bar

            sl_sig = self._update_trailing_stop(i, bar)
            if sl_sig is not None:
                signals.append(sl_sig)

        # 3. Check pending order expiry
        if self._pending is not None and self._pending.bars_alive >= self._config.trigger_window_bars:
            log.debug("Pending order expired after %d bars", self._pending.bars_alive)
            self._pending = None
            self._state = LiveState.FLAT

        # 4. Check new entry (when FLAT, or when PENDING for IRB replacement)
        if self._state in (
            LiveState.FLAT,
            LiveState.PENDING_LONG,
            LiveState.PENDING_SHORT,
        ):
            # Circuit breaker
            if self._env.max_consecutive_losses > 0 and self._consecutive_losses >= self._env.max_consecutive_losses:
                return signals

            entry_sig = self._check_entry(i)
            if entry_sig is not None:
                signals.append(entry_sig)

        # Inject bar_close_time into all emitted signals for shadow-mode matching
        for sig in signals:
            if "bar_close_time" not in sig.metadata:
                sig.metadata["bar_close_time"] = bar.timestamp

        return signals

    def notify_fill(self, fill_price: float, volume: float) -> None:
        """Called by execution layer when a pending order is filled by broker.

        This handles the case where the broker fills the order rather than the
        engine detecting a fill from bar data (live latency).
        """
        if self._pending is None:
            return

        i = len(self._h1_candles) - 1
        self._position = OpenPosition(
            side=self._pending.side,
            entry_price=fill_price,
            stop_loss=self._pending.stop_loss,
            volume=volume,
            entry_bar=i,
        )
        self._state = LiveState.LONG if self._pending.side == "LONG" else LiveState.SHORT
        self._pending = None
        log.info("External fill: %s at %.5f vol=%.2f", self._position.side, fill_price, volume)

    def notify_close(self, pnl: float) -> None:
        """Called by execution layer when a position is closed.

        Updates internal state and consecutive loss tracking.
        """
        self._position = None
        self._state = LiveState.FLAT
        self._equity += pnl

        if pnl < 0:
            self._consecutive_losses += 1
        else:
            self._consecutive_losses = 0

        log.info("Position closed: pnl=%.2f equity=%.2f", pnl, self._equity)

    def cancel_pending(self) -> None:
        """Cancel any pending order."""
        self._pending = None
        if self._state in (LiveState.PENDING_LONG, LiveState.PENDING_SHORT):
            self._state = LiveState.FLAT

    def recover_position_state(
        self,
        side: str,
        entry_price: float,
        stop_loss: float,
        volume: float,
    ) -> None:
        """Adopt a broker position discovered on startup.

        Sets the engine to LONG/SHORT to match broker reality.
        Called when the broker has an open position that the engine
        doesn't know about (e.g. after service restart).
        """
        i = len(self._h1_candles) - 1
        self._position = OpenPosition(
            side=side,
            entry_price=entry_price,
            stop_loss=stop_loss,
            volume=volume,
            entry_bar=max(i, 0),
        )
        self._state = LiveState.LONG if side == "LONG" else LiveState.SHORT
        self._pending = None
        log.info(
            "recover_position_state: -> %s at %.5f sl=%.5f vol=%.2f",
            self._state.value,
            entry_price,
            stop_loss,
            volume,
        )

    @property
    def state(self) -> LiveState:
        return self._state

    @property
    def is_warmed_up(self) -> bool:
        return self._state != LiveState.WARMING_UP

    @property
    def position(self) -> OpenPosition | None:
        return self._position

    @property
    def pending(self) -> PendingOrder | None:
        return self._pending

    @property
    def total_bars(self) -> int:
        return self._total_bars

    @property
    def equity(self) -> float:
        return self._equity

    @property
    def h1_count(self) -> int:
        return len(self._h1_candles)

    @property
    def h4_count(self) -> int:
        return len(self._h4_candles)

    @property
    def indicators(self) -> dict[str, list[float]]:
        return dict(self._indicators)

    def snapshot(self) -> dict[str, Any]:
        """Return a point-in-time snapshot of engine state."""
        return {
            "state": self._state.value,
            "symbol": self._config.symbol,
            "total_bars": self._total_bars,
            "h1_buffered": len(self._h1_candles),
            "h4_buffered": len(self._h4_candles),
            "equity": self._equity,
            "consecutive_losses": self._consecutive_losses,
            "has_pending": self._pending is not None,
            "has_position": self._position is not None,
            "position_side": self._position.side if self._position else None,
            "position_entry": self._position.entry_price if self._position else None,
            "position_stop": self._position.current_stop if self._position else None,
            "position_bars_held": self._position.bars_held if self._position else None,
        }

    # ------------------------------------------------------------------
    # Internal: entry evaluation
    # ------------------------------------------------------------------

    def _check_entry(self, i: int) -> LiveSignal | None:
        """Evaluate entry conditions at the latest bar."""
        signal: EntrySignal | None = self._strategy.check_entry(
            i,
            self._h1_candles,
            self._indicators,
            self._h4_candles if self._h4_candles else None,
        )
        if signal is None:
            return None

        # Position sizing (same formula as backtest engine)
        stop_distance_pips = abs(signal.entry_price - signal.stop_loss) / self._env.pip_value
        if stop_distance_pips <= 0:
            return None

        risk_dollars = self._equity * self._env.risk_fraction
        volume = risk_dollars / (stop_distance_pips * self._env.pip_value_per_standard_lot)
        volume = max(self._env.min_volume, min(self._env.max_volume, round(volume, 2)))

        # Handle existing pending (IRB replacement)
        if self._pending is not None:
            if self._pending.side == signal.side:
                self._pending = None  # replace
            else:
                return None  # opposite direction — ignore

        # Place pending stop order
        self._pending = PendingOrder(
            side=signal.side,
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            volume=volume,
            bar_index=i,
        )
        self._state = LiveState.PENDING_LONG if signal.side == "LONG" else LiveState.PENDING_SHORT

        log.info(
            "ENTRY signal: %s %s entry=%.5f sl=%.5f vol=%.2f",
            self._config.symbol,
            signal.side,
            signal.entry_price,
            signal.stop_loss,
            volume,
        )

        return LiveSignal(
            signal_type=SignalType.ENTRY,
            side=signal.side,
            symbol=self._config.symbol,
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            volume=volume,
            metadata=signal.metadata,
        )

    # ------------------------------------------------------------------
    # Internal: pending order fill detection
    # ------------------------------------------------------------------

    def _check_pending_fill(self, i: int, bar: Candle) -> LiveSignal | None:
        """Check if the pending stop order fills on this bar."""
        p = self._pending
        if p is None:
            return None

        filled = False
        fill_price = p.entry_price

        if p.side == "LONG":
            if bar.high >= p.entry_price:
                filled = True
                if bar.open >= p.entry_price:
                    fill_price = bar.open
        else:
            if bar.low <= p.entry_price:
                filled = True
                if bar.open <= p.entry_price:
                    fill_price = bar.open

        if not filled:
            return None

        # Open position
        self._position = OpenPosition(
            side=p.side,
            entry_price=fill_price,
            stop_loss=p.stop_loss,
            volume=p.volume,
            entry_bar=i,
        )
        self._state = LiveState.LONG if p.side == "LONG" else LiveState.SHORT
        self._pending = None

        log.info(
            "FILL: %s %s at %.5f (order was %.5f)",
            self._config.symbol,
            p.side,
            fill_price,
            p.entry_price,
        )

        return LiveSignal(
            signal_type=SignalType.PENDING_FILL,
            side=p.side,
            symbol=self._config.symbol,
            entry_price=fill_price,
            stop_loss=p.stop_loss,
            volume=p.volume,
        )

    # ------------------------------------------------------------------
    # Internal: exit evaluation
    # ------------------------------------------------------------------

    def _check_exit(self, i: int, bar: Candle) -> LiveSignal | None:
        """Check exit conditions for the open position."""
        pos = self._position
        if pos is None:
            return None

        # Use strategy's check_exit for stop-loss, time-stop, trailing-stop
        position_dict = {
            "side": pos.side,
            "entry_price": pos.entry_price,
            "stop_loss": pos.stop_loss,
            "entry_bar": pos.entry_bar,
            "current_stop": pos.current_stop,
            "best_close": pos.best_close,
        }

        exit_sig: ExitSignal | None = self._strategy.check_exit(i, self._h1_candles, self._indicators, position_dict)
        if exit_sig is None:
            return None

        log.info(
            "EXIT: %s %s reason=%s price=%.5f",
            self._config.symbol,
            pos.side,
            exit_sig.reason,
            exit_sig.exit_price,
        )

        # Don't clear position here — wait for notify_close from execution layer.
        # But update state to signal intent.
        return LiveSignal(
            signal_type=SignalType.EXIT,
            side=pos.side,
            symbol=self._config.symbol,
            exit_price=exit_sig.exit_price,
            exit_reason=exit_sig.reason,
            metadata=exit_sig.metadata,
        )

    # ------------------------------------------------------------------
    # Internal: trailing stop management
    # ------------------------------------------------------------------

    def _update_trailing_stop(self, i: int, bar: Candle) -> LiveSignal | None:
        """Update trailing stop and emit MODIFY_SL if it moves."""
        pos = self._position
        if pos is None:
            return None

        atr = self._indicators.get("atr", [])
        if i >= len(atr) or math.isnan(atr[i]) or atr[i] <= 0:
            return None

        atr_val = atr[i]
        old_stop = pos.current_stop

        if pos.side == "LONG":
            pos.best_close = max(pos.best_close, bar.close)
            new_trail = pos.best_close - self._env.trail_atr_multiplier * atr_val
            if new_trail > pos.current_stop:
                pos.current_stop = new_trail
        else:
            pos.best_close = min(pos.best_close, bar.close)
            new_trail = pos.best_close + self._env.trail_atr_multiplier * atr_val
            if new_trail < pos.current_stop:
                pos.current_stop = new_trail

        if pos.current_stop != old_stop:
            log.debug(
                "Trailing stop moved: %.5f → %.5f",
                old_stop,
                pos.current_stop,
            )
            return LiveSignal(
                signal_type=SignalType.MODIFY_SL,
                side=pos.side,
                symbol=self._config.symbol,
                new_stop=pos.current_stop,
                metadata={"old_stop": old_stop, "atr": atr_val},
            )

        return None

    # ------------------------------------------------------------------
    # Internal: buffer management
    # ------------------------------------------------------------------

    def _trim(self, buf: list[Candle]) -> None:
        """Trim buffer to max_candles."""
        cap = self._config.max_candles
        if len(buf) > cap:
            excess = len(buf) - cap
            del buf[:excess]
            # Adjust position/pending bar indices if they reference the H1 buffer
            if buf is self._h1_candles:
                if self._pending is not None:
                    self._pending.bar_index -= excess
                if self._position is not None:
                    self._position.entry_bar -= excess
