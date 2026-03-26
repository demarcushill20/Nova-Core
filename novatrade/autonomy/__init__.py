"""NovaCore Autonomy Expansion — progress scoring and self-assessment.

This package provides a multi-dimensional scoring system that continuously
evaluates system health, execution pipeline status, strategy validity,
risk engine integrity, and performance stability.
"""

from novatrade.autonomy.progress_scorer import ProgressScorer
from novatrade.autonomy.schemas import (
    AlertLevel,
    DimensionScore,
    ProgressReport,
    ScoreTrend,
    ScoringConfig,
    SubMetric,
)

__all__ = [
    "AlertLevel",
    "DimensionScore",
    "ProgressReport",
    "ProgressScorer",
    "ScoreTrend",
    "ScoringConfig",
    "SubMetric",
]
