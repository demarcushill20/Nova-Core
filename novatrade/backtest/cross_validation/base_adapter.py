"""Abstract base class for engine adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod

from novatrade.backtest.cross_validation.types import EngineId, EngineResult
from novatrade.backtest.environment import BacktestEnvironment
from novatrade.models import Candle


class BaseEngineAdapter(ABC):
    """ABC for engine adapters.

    Each adapter translates the IRB strategy into the target engine's
    paradigm, runs the backtest, and normalises results.
    """

    @property
    @abstractmethod
    def engine_id(self) -> EngineId:
        """Which engine this adapter wraps."""

    @property
    @abstractmethod
    def engine_version(self) -> str:
        """Version string of the wrapped engine."""

    @abstractmethod
    def is_available(self) -> bool:
        """Return True if the engine is installed and importable."""

    @abstractmethod
    def run(
        self,
        h1_candles: list[Candle],
        h4_candles: list[Candle],
        env: BacktestEnvironment,
    ) -> EngineResult:
        """Run the backtest and return normalised results."""

    # ------------------------------------------------------------------
    # Helpers available to all adapters
    # ------------------------------------------------------------------

    @staticmethod
    def _candles_to_dataframe(candles: list[Candle]):
        """Convert Candle list → pandas DataFrame with DatetimeIndex + OHLCV columns."""
        import pandas as pd
        from datetime import datetime, timezone

        data = {
            "Open": [c.open for c in candles],
            "High": [c.high for c in candles],
            "Low": [c.low for c in candles],
            "Close": [c.close for c in candles],
            "Volume": [c.volume for c in candles],
        }
        index = pd.DatetimeIndex(
            [datetime.fromtimestamp(c.timestamp, tz=timezone.utc) for c in candles]
        )
        return pd.DataFrame(data, index=index)
