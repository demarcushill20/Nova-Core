"""NovaTrade Data Quality, Multi-Format Ingestion, and Live Data Pipeline.

Public API:
    - validate_candles: Run quality checks on candle data
    - auto_fix_candles: Auto-fix common data quality issues
    - DataQualityReport: Pydantic model for quality check results
    - load_candles: Multi-format candle loader
    - detect_format: Auto-detect data file format
    - CandleFormat: Enum of supported formats
    - derive_timeframe: Aggregate candles to higher timeframes
    - DERIVATION_MAP: Valid source -> target timeframe mappings
    - Tick: Immutable price tick from broker
    - TickBuffer: Per-symbol FIFO tick buffer
    - OHLCVProvider: Abstract OHLCV data provider
    - MetaApiOHLCVProvider: Live broker candle provider
    - CSVOHLCVProvider: CSV file candle provider
    - CachedOHLCVProvider: SQLite-cached candle provider
    - TickBatchPoller: Async tick stream via adapter polling
    - BarAggregator: Multi-timeframe tick-to-bar converter
"""

from novatrade.data.bar_aggregator import TIMEFRAME_SECONDS, BarAggregator
from novatrade.data.loader import CandleFormat, detect_format, load_candles
from novatrade.data.ohlcv_provider import (
    CachedOHLCVProvider,
    CSVOHLCVProvider,
    MetaApiOHLCVProvider,
    OHLCVProvider,
)
from novatrade.data.price_feed import TickBatchPoller
from novatrade.data.quality import DataQualityReport, auto_fix_candles, validate_candles
from novatrade.data.tick import Tick, TickBuffer
from novatrade.data.timeframe import DERIVATION_MAP, derive_timeframe

__all__ = [
    "DERIVATION_MAP",
    "TIMEFRAME_SECONDS",
    "BarAggregator",
    "CSVOHLCVProvider",
    "CachedOHLCVProvider",
    "CandleFormat",
    "DataQualityReport",
    "MetaApiOHLCVProvider",
    "OHLCVProvider",
    "Tick",
    "TickBatchPoller",
    "TickBuffer",
    "auto_fix_candles",
    "derive_timeframe",
    "detect_format",
    "load_candles",
    "validate_candles",
]
