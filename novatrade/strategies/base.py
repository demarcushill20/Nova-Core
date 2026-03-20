"""Abstract base strategy interface for the multi-strategy architecture.

All strategy types implement this ABC so the backtest engine, optimizer, and
pipeline can treat them uniformly without knowing the concrete strategy type.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from novatrade.models import Candle

# ---------------------------------------------------------------------------
# Signal types returned by strategy check_entry / check_exit
# ---------------------------------------------------------------------------


@dataclass
class EntrySignal:
    """Entry signal produced by a strategy."""

    bar_index: int
    side: str  # "LONG" or "SHORT"
    entry_price: float
    stop_loss: float
    confidence: float = 1.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExitSignal:
    """Exit signal produced by a strategy."""

    bar_index: int
    exit_price: float
    reason: str  # "stop_loss", "trailing_stop", "time_stop", "signal_exit"
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class BaseStrategy(ABC):
    """Abstract base class for all trading strategies.

    Every concrete strategy must implement all abstract methods. The engine
    calls these in order:

    1. ``compute_indicators(candles)`` -- pre-compute all needed indicators
    2. ``generate_signals(candles, indicators)`` -- scan for entry/exit signals
    3. ``check_entry(bar_index, ...)`` -- per-bar entry check
    4. ``check_exit(bar_index, ...)`` -- per-bar exit check for open positions

    Additionally, strategies expose their default doctrine and parameter bounds
    for the optimizer pipeline.
    """

    name: str = ""
    family: str = ""  # "reversal", "mean_reversion", "breakout", "trend_following"

    @abstractmethod
    def compute_indicators(self, candles: list[Candle]) -> dict[str, list[float]]:
        """Compute all indicators needed for signal generation.

        Args:
            candles: Chronologically ordered OHLCV candles.

        Returns:
            Dictionary mapping indicator name to list of values (same length
            as candles, NaN-padded where warmup is insufficient).
        """

    @abstractmethod
    def generate_signals(self, candles: list[Candle], indicators: dict[str, list[float]]) -> list[EntrySignal]:
        """Generate entry signals from candles and pre-computed indicators.

        Args:
            candles: Chronologically ordered OHLCV candles.
            indicators: Output of ``compute_indicators``.

        Returns:
            List of EntrySignal instances for every bar where conditions are met.
        """

    @abstractmethod
    def check_entry(
        self,
        bar_index: int,
        candles: list[Candle],
        indicators: dict[str, list[float]],
        h4_candles: list[Candle] | None = None,
    ) -> EntrySignal | None:
        """Check if entry conditions are met at bar_index.

        Args:
            bar_index: Current bar position in the candle array.
            candles: Full candle array.
            indicators: Pre-computed indicators from ``compute_indicators``.
            h4_candles: Optional higher-timeframe candles for MTF strategies.

        Returns:
            EntrySignal if conditions are met, None otherwise.
        """

    @abstractmethod
    def check_exit(
        self,
        bar_index: int,
        candles: list[Candle],
        indicators: dict[str, list[float]],
        position: dict[str, Any] | None = None,
    ) -> ExitSignal | None:
        """Check if exit conditions are met for an open position.

        Args:
            bar_index: Current bar position in the candle array.
            candles: Full candle array.
            indicators: Pre-computed indicators from ``compute_indicators``.
            position: Dictionary with position state (side, entry_price,
                      stop_loss, entry_bar, best_close, current_stop, etc.).

        Returns:
            ExitSignal if exit conditions are met, None otherwise.
        """

    @abstractmethod
    def get_default_doctrine(self, pair: str, timeframe: str) -> dict[str, Any]:
        """Return default doctrine params for this strategy type.

        Args:
            pair: Currency pair (e.g. "EURUSD").
            timeframe: Primary timeframe (e.g. "H1").

        Returns:
            Dictionary suitable for constructing a StrategyDoctrine.
        """

    @abstractmethod
    def get_parameter_bounds(self) -> dict[str, tuple[float, float]]:
        """Return parameter bounds for optimization.

        Returns:
            Dictionary mapping parameter name to (min_value, max_value) tuple.
        """
