"""Multi-engine backtesting cross-validation framework.

Runs the same strategy across multiple backtesting engines (native + third-party)
and compares results to validate robustness. Divergences beyond configurable
thresholds are flagged for investigation.

Usage::

    from novatrade.backtest.cross_validation import CrossValidationRunner
    runner = CrossValidationRunner()
    report = runner.run(h1_candles, h4_candles, env)
    print(report.summary)
"""

from novatrade.backtest.cross_validation.types import (
    ConsensusReport,
    DivergenceSeverity,
    DivergenceThresholds,
    EngineId,
    EngineResult,
    MetricAvailability,
    MetricDivergence,
    NormalizedMetrics,
    NormalizedTrade,
)
from novatrade.backtest.cross_validation.base_adapter import BaseEngineAdapter
from novatrade.backtest.cross_validation.comparator import CrossValidator
from novatrade.backtest.cross_validation.runner import CrossValidationRunner

__all__ = [
    "BaseEngineAdapter",
    "ConsensusReport",
    "CrossValidationRunner",
    "CrossValidator",
    "DivergenceSeverity",
    "DivergenceThresholds",
    "EngineId",
    "EngineResult",
    "MetricAvailability",
    "MetricDivergence",
    "NormalizedMetrics",
    "NormalizedTrade",
]
