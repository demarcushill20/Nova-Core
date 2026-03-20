"""NovaTrade Data Quality and Multi-Format Ingestion (Phase 2).

Public API:
    - validate_candles: Run quality checks on candle data
    - auto_fix_candles: Auto-fix common data quality issues
    - DataQualityReport: Pydantic model for quality check results
    - load_candles: Multi-format candle loader
    - detect_format: Auto-detect data file format
    - CandleFormat: Enum of supported formats
    - derive_timeframe: Aggregate candles to higher timeframes
    - DERIVATION_MAP: Valid source -> target timeframe mappings
"""

from novatrade.data.loader import CandleFormat, detect_format, load_candles
from novatrade.data.quality import DataQualityReport, auto_fix_candles, validate_candles
from novatrade.data.timeframe import DERIVATION_MAP, derive_timeframe

__all__ = [
    "DERIVATION_MAP",
    "CandleFormat",
    "DataQualityReport",
    "auto_fix_candles",
    "derive_timeframe",
    "detect_format",
    "load_candles",
    "validate_candles",
]
