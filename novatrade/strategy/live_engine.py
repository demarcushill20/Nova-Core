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
import random
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
    # v5 trade management state (mirrors vault Python reference)
    initial_stop: float = 0.0
    initial_volume: float = 0.0
    tp_qty_open: float = 0.0
    runner_qty_open: float = 0.0
    partial_taken: bool = False
    breakeven_active: bool = False
    highest_since_entry: float = 0.0
    lowest_since_entry: float = 0.0

    def __post_init__(self) -> None:
        if self.current_stop == 0.0:
            self.current_stop = self.stop_loss
        if self.best_close == 0.0:
            self.best_close = self.entry_price
        if self.initial_stop == 0.0:
            self.initial_stop = self.stop_loss
        if self.initial_volume == 0.0:
            self.initial_volume = self.volume
        # highest/lowest seeded by engine when fill occurs (not here, since we
        # don't have bar data yet).


# ---------------------------------------------------------------------------
# Signal output
# ---------------------------------------------------------------------------


class SignalType(Enum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"
    MODIFY_SL = "MODIFY_SL"
    CANCEL_PENDING = "CANCEL_PENDING"
    PENDING_FILL = "PENDING_FILL"
    PARTIAL_EXIT = "PARTIAL_EXIT"  # v5: close a fraction of open volume


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
        self._last_sl_modification_bar: int | None = None

        # Cached indicators (recomputed each H1 bar)
        self._indicators: dict[str, list[float]] = {}

        # v5 frequency controls (mirrors vault Python reference)
        self._trades_today: int = 0
        self._current_day_key: Any = None  # date object
        self._last_flat_bar: int | None = None

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

        # v5 day boundary detection — reset trades_today on new UTC day
        if self._env.use_simple_trend_filter and bar.timestamp > 0:
            from datetime import datetime as _dt
            from datetime import timezone as _tz

            day_key = _dt.fromtimestamp(bar.timestamp, tz=_tz.utc).date()
            if self._current_day_key != day_key:
                if self._current_day_key is not None:
                    log.info(
                        "v5 day boundary: resetting trades_today (was %d) for %s",
                        self._trades_today,
                        day_key,
                    )
                self._trades_today = 0
                self._current_day_key = day_key

        # Check warmup
        if len(self._h1_candles) < self._env.warmup_bars:
            return []

        if self._state == LiveState.WARMING_UP:
            self._state = LiveState.FLAT

        # Recompute indicators on full buffer
        self._indicators = self._strategy.compute_indicators(self._h1_candles)

        i = len(self._h1_candles) - 1
        signals: list[LiveSignal] = []

        # v5: revalidate pending order against current trend before fill check.
        # Mirrors the vault Python reference's "still_valid" gate which cancels
        # the pending if the trend has flipped or the entry context is no longer
        # valid (cooldown / max_trades / session).
        if self._env.use_simple_trend_filter and self._env.revalidate_pending and self._pending is not None:
            cancel_sig = self._revalidate_pending(i, bar)
            if cancel_sig is not None:
                signals.append(cancel_sig)

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

            if self._env.use_simple_trend_filter:
                # v5 path: partial-at-1R, breakeven move, ATR runner trail, time stop
                v5_signals = self._check_v5_exit(i, bar)
                signals.extend(v5_signals)
                # If a final EXIT was emitted, stop further processing this bar
                if any(s.signal_type == SignalType.EXIT for s in v5_signals):
                    return signals
            else:
                # v4 path: delegate to strategy.check_exit + separate trailing stop
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

            # v5 frequency controls (mirrors vault Python reference entry_context_ok)
            if self._env.use_simple_trend_filter:
                if self._env.max_trades_per_day > 0 and self._trades_today >= self._env.max_trades_per_day:
                    log.debug(
                        "v5 entry gate: trades_today=%d >= max_trades_per_day=%d — blocked",
                        self._trades_today,
                        self._env.max_trades_per_day,
                    )
                    return signals
                if (
                    self._env.cooldown_bars > 0
                    and self._last_flat_bar is not None
                    and (i - self._last_flat_bar) <= self._env.cooldown_bars
                ):
                    log.debug(
                        "v5 entry gate: in cooldown (i=%d, last_flat=%d, cooldown=%d) — blocked",
                        i,
                        self._last_flat_bar,
                        self._env.cooldown_bars,
                    )
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
        last_bar = self._h1_candles[i] if i >= 0 else None
        partial_pct = self._env.partial_exit_pct if self._env.partial_exit_enabled else 0.0
        tp_qty = round(volume * (partial_pct / 100.0), 2) if partial_pct > 0 else 0.0
        runner_qty = max(0.0, volume - tp_qty)
        self._position = OpenPosition(
            side=self._pending.side,
            entry_price=fill_price,
            stop_loss=self._pending.stop_loss,
            volume=volume,
            entry_bar=i,
            initial_stop=self._pending.stop_loss,
            initial_volume=volume,
            tp_qty_open=tp_qty,
            runner_qty_open=runner_qty,
            highest_since_entry=last_bar.high if last_bar else fill_price,
            lowest_since_entry=last_bar.low if last_bar else fill_price,
        )
        self._state = LiveState.LONG if self._pending.side == "LONG" else LiveState.SHORT
        self._pending = None
        # v5: count this fill against today's trade budget
        if self._env.use_simple_trend_filter:
            self._trades_today += 1
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

        # v5: track the bar where we went flat for cooldown enforcement
        if self._env.use_simple_trend_filter:
            self._last_flat_bar = max(0, len(self._h1_candles) - 1)

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
        last_bar = self._h1_candles[i] if i >= 0 else None
        # Conservative recovery: assume partial has not been taken yet so the
        # full volume is treated as the runner. This is safe because broker
        # truth (post-trade verifier) reconciles partial state separately.
        self._position = OpenPosition(
            side=side,
            entry_price=entry_price,
            stop_loss=stop_loss,
            volume=volume,
            entry_bar=max(i, 0),
            initial_stop=stop_loss,
            initial_volume=volume,
            tp_qty_open=0.0,
            runner_qty_open=volume,
            highest_since_entry=last_bar.high if last_bar else entry_price,
            lowest_since_entry=last_bar.low if last_bar else entry_price,
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

    @property
    def h1_candles(self) -> list[Candle]:
        """Read-only access to the H1 candle buffer for regime classification."""
        return list(self._h1_candles)

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
        partial_pct = self._env.partial_exit_pct if self._env.partial_exit_enabled else 0.0
        tp_qty = round(p.volume * (partial_pct / 100.0), 2) if partial_pct > 0 else 0.0
        runner_qty = max(0.0, p.volume - tp_qty)
        self._position = OpenPosition(
            side=p.side,
            entry_price=fill_price,
            stop_loss=p.stop_loss,
            volume=p.volume,
            entry_bar=i,
            initial_stop=p.stop_loss,
            initial_volume=p.volume,
            tp_qty_open=tp_qty,
            runner_qty_open=runner_qty,
            highest_since_entry=bar.high,
            lowest_since_entry=bar.low,
        )
        self._state = LiveState.LONG if p.side == "LONG" else LiveState.SHORT
        self._pending = None
        # v5: count this fill against today's trade budget
        if self._env.use_simple_trend_filter:
            self._trades_today += 1

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
    # Internal: v5 pending order revalidation
    # ------------------------------------------------------------------

    def _revalidate_pending(self, i: int, bar: Candle) -> LiveSignal | None:
        """Re-check trend conditions for the pending order each bar.

        Mirrors the vault Python reference's ``still_valid`` gate. If the
        local EMA20/EMA50 trend has flipped against the pending side, cancel
        the pending and emit a CANCEL_PENDING signal.

        Cooldown / max_trades / session checks happen at the entry gate
        (Phase 3 entry block); this method only validates trend invalidation
        because trend flip is the only condition the vault reference re-checks
        on each bar.
        """
        p = self._pending
        if p is None:
            return None

        ema_fast = self._indicators.get("ema_fast", [])
        ema_slow = self._indicators.get("ema_slow", [])
        if i >= len(ema_fast) or i >= len(ema_slow):
            return None
        ef = ema_fast[i]
        es = ema_slow[i]
        if math.isnan(ef) or math.isnan(es):
            return None

        bull_trend = ef > es
        bear_trend = ef < es

        still_valid = (p.side == "LONG" and bull_trend) or (p.side == "SHORT" and bear_trend)
        if still_valid:
            return None

        log.info(
            "v5 revalidate_pending: %s pending invalidated by trend flip — cancelling",
            p.side,
        )
        cancelled_side = p.side
        self._pending = None
        if self._state in (LiveState.PENDING_LONG, LiveState.PENDING_SHORT):
            self._state = LiveState.FLAT
        return LiveSignal(
            signal_type=SignalType.CANCEL_PENDING,
            side=cancelled_side,
            symbol=self._config.symbol,
            exit_reason="revalidate_failed",
        )

    # ------------------------------------------------------------------
    # Internal: v5 exit management (vault Python reference parity)
    # ------------------------------------------------------------------

    def _check_v5_exit(self, i: int, bar: Candle) -> list[LiveSignal]:
        """V5 exit lifecycle: partial-at-1R, breakeven move, ATR runner trail, time stop.

        Mirrors the vault Python reference (Rob Hoffman IRB v5 - Relaxed Reliable
        Build, Python edition). Critical: uses the previous bar's ATR for the
        runner trail to avoid lookahead bias, exactly like the reference.
        """
        pos = self._position
        if pos is None:
            return []

        e = self._env
        atr_arr = self._indicators.get("atr", [])
        # Vault uses df.iloc[i-1]["runner_atr"] — previous bar's ATR.
        prev_idx = i - 1
        if prev_idx < 0 or prev_idx >= len(atr_arr) or math.isnan(atr_arr[prev_idx]):
            prev_atr = 0.0
        else:
            prev_atr = atr_arr[prev_idx]

        signals: list[LiveSignal] = []
        pip = e.pip_value

        # Update high/low watermarks for the current bar
        if pos.side == "LONG":
            pos.highest_since_entry = max(pos.highest_since_entry, bar.high)
        else:
            pos.lowest_since_entry = min(pos.lowest_since_entry, bar.low)

        if pos.side == "LONG":
            risk_unit = max(pos.entry_price - pos.initial_stop, pip)
            partial_price = pos.entry_price + risk_unit * e.partial_r_target
            be_trigger = pos.entry_price + risk_unit * e.breakeven_r
            if not pos.breakeven_active and pos.highest_since_entry >= be_trigger:
                pos.breakeven_active = True
            atr_runner_stop = pos.highest_since_entry - e.trail_atr_multiplier * prev_atr
            runner_stop = max(pos.entry_price, atr_runner_stop) if pos.breakeven_active else pos.initial_stop
        else:
            risk_unit = max(pos.initial_stop - pos.entry_price, pip)
            partial_price = pos.entry_price - risk_unit * e.partial_r_target
            be_trigger = pos.entry_price - risk_unit * e.breakeven_r
            if not pos.breakeven_active and pos.lowest_since_entry <= be_trigger:
                pos.breakeven_active = True
            atr_runner_stop = pos.lowest_since_entry + e.trail_atr_multiplier * prev_atr
            runner_stop = min(pos.entry_price, atr_runner_stop) if pos.breakeven_active else pos.initial_stop

        # 1) Partial TP cross
        if e.partial_exit_enabled and not pos.partial_taken and pos.tp_qty_open > 0:
            tp_hit = (pos.side == "LONG" and bar.high >= partial_price) or (
                pos.side == "SHORT" and bar.low <= partial_price
            )
            if tp_hit:
                partial_volume = pos.tp_qty_open
                signals.append(
                    LiveSignal(
                        signal_type=SignalType.PARTIAL_EXIT,
                        side=pos.side,
                        symbol=self._config.symbol,
                        exit_price=partial_price,
                        volume=partial_volume,
                        exit_reason="partial_tp",
                        metadata={
                            "partial_r_target": e.partial_r_target,
                            "remaining_volume": pos.runner_qty_open,
                        },
                    )
                )
                pos.partial_taken = True
                pos.tp_qty_open = 0.0
                pos.volume = pos.runner_qty_open
                log.info(
                    "v5 PARTIAL_EXIT: %s %.5f vol=%.2f remaining=%.2f",
                    pos.side,
                    partial_price,
                    partial_volume,
                    pos.runner_qty_open,
                )

        # 2) Stop / runner-stop cross — emit final EXIT if breached
        stop_hit = (pos.side == "LONG" and bar.low <= runner_stop) or (pos.side == "SHORT" and bar.high >= runner_stop)
        if stop_hit:
            reason = "runner_stop" if pos.breakeven_active else "stop_loss"
            signals.append(
                LiveSignal(
                    signal_type=SignalType.EXIT,
                    side=pos.side,
                    symbol=self._config.symbol,
                    exit_price=runner_stop,
                    exit_reason=reason,
                    metadata={
                        "breakeven_active": pos.breakeven_active,
                        "atr_at_exit": prev_atr,
                    },
                )
            )
            log.info(
                "v5 EXIT: %s %s at %.5f (be_active=%s)",
                pos.side,
                reason,
                runner_stop,
                pos.breakeven_active,
            )
            return signals

        # 3) Time stop
        if e.time_stop_bars > 0 and pos.bars_held >= e.time_stop_bars:
            signals.append(
                LiveSignal(
                    signal_type=SignalType.EXIT,
                    side=pos.side,
                    symbol=self._config.symbol,
                    exit_price=bar.close,
                    exit_reason="time_stop",
                    metadata={"bars_held": pos.bars_held},
                )
            )
            log.info("v5 EXIT: %s time_stop at bar %d (held=%d)", pos.side, i, pos.bars_held)
            return signals

        # 4) Runner-stop tightening — emit MODIFY_SL when breakeven becomes active
        # or the ATR trail has ratcheted higher.
        if runner_stop != pos.current_stop and (
            (pos.side == "LONG" and runner_stop > pos.current_stop)
            or (pos.side == "SHORT" and runner_stop < pos.current_stop)
        ):
            old_stop = pos.current_stop
            pos.current_stop = runner_stop
            self._last_sl_modification_bar = i
            signals.append(
                LiveSignal(
                    signal_type=SignalType.MODIFY_SL,
                    side=pos.side,
                    symbol=self._config.symbol,
                    new_stop=runner_stop,
                    metadata={
                        "old_stop": old_stop,
                        "atr": prev_atr,
                        "breakeven_active": pos.breakeven_active,
                    },
                )
            )

        return signals

    # ------------------------------------------------------------------
    # Internal: trailing stop management
    # ------------------------------------------------------------------

    def _update_trailing_stop(self, i: int, bar: Candle) -> LiveSignal | None:
        """Update trailing stop with stepped threshold, cooldown, and jitter to reduce SL modification frequency."""
        pos = self._position
        if pos is None:
            return None

        atr = self._indicators.get("atr", [])
        if i >= len(atr) or math.isnan(atr[i]) or atr[i] <= 0:
            return None

        # Check cooldown period
        if (
            self._last_sl_modification_bar is not None
            and i - self._last_sl_modification_bar < self._env.trail_cooldown_bars
        ):
            return None

        atr_val = atr[i]
        old_stop = pos.current_stop

        if pos.side == "LONG":
            pos.best_close = max(pos.best_close, bar.close)
            new_trail = pos.best_close - self._env.trail_atr_multiplier * atr_val

            # Apply stepped threshold - only update if improvement exceeds step size
            if new_trail > pos.current_stop:
                step_improvement = new_trail - pos.current_stop
                if step_improvement >= self._env.trail_step_pips * self._env.pip_value:
                    pos.current_stop = new_trail
        else:
            pos.best_close = min(pos.best_close, bar.close)
            new_trail = pos.best_close + self._env.trail_atr_multiplier * atr_val

            # Apply stepped threshold - only update if improvement exceeds step size
            if new_trail < pos.current_stop:
                step_improvement = pos.current_stop - new_trail
                if step_improvement >= self._env.trail_step_pips * self._env.pip_value:
                    pos.current_stop = new_trail

        if pos.current_stop != old_stop:
            self._last_sl_modification_bar = i

            log.debug(
                "Trailing stop moved: %.5f → %.5f (step=%.1f pips, bars_since_last=%s)",
                old_stop,
                pos.current_stop,
                abs(pos.current_stop - old_stop) / self._env.pip_value,
                i - (self._last_sl_modification_bar or i) if self._last_sl_modification_bar else "first",
            )

            # Add timing jitter (±5-15s) to avoid FTMO pattern detection
            jitter_seconds = random.uniform(5.0, 15.0)  # noqa: S311

            return LiveSignal(
                signal_type=SignalType.MODIFY_SL,
                side=pos.side,
                symbol=self._config.symbol,
                new_stop=pos.current_stop,
                metadata={
                    "old_stop": old_stop,
                    "atr": atr_val,
                    "step_pips": abs(pos.current_stop - old_stop) / self._env.pip_value,
                    "jitter_seconds": jitter_seconds,
                    "cooldown_bars": self._env.trail_cooldown_bars,
                },
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
