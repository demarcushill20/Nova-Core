"""Cross-validation shared types.

All adapters produce EngineResult; the comparator consumes them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class EngineId(Enum):
    """Identifies a backtesting engine."""

    NOVA = "nova"
    VAULT = "vault"
    BACKTESTING_PY = "backtesting_py"
    VECTORBT = "vectorbt"
    NAUTILUS = "nautilus"


class MetricAvailability(Enum):
    AVAILABLE = "available"
    NOT_COMPUTED = "not_computed"
    NOT_APPLICABLE = "not_applicable"


# ------------------------------------------------------------------
# Normalised trade / metrics
# ------------------------------------------------------------------


@dataclass
class NormalizedTrade:
    """Engine-agnostic trade record for cross-validation.

    Intentionally simpler than CompletedTrade — only the fields
    that are comparable across all engines.
    """

    trade_id: int
    side: str  # "LONG" or "SHORT"
    entry_bar: int
    exit_bar: int
    entry_price: float
    exit_price: float
    stop_loss: float
    pnl_pips: float
    pnl_usd: float
    hold_bars: int
    exit_reason: str  # "stop_loss", "trailing_stop", "time_stop"


@dataclass
class NormalizedMetrics:
    """Subset of metrics comparable across engines.

    Each field is Optional to handle engines that don't compute it.
    The ``availability`` dict tracks why a metric is missing.
    """

    total_trades: int | None = None
    long_trades: int | None = None
    short_trades: int | None = None
    win_rate: float | None = None
    net_pnl_pips: float | None = None
    net_pnl_usd: float | None = None
    profit_factor: float | None = None
    max_drawdown_pct: float | None = None
    max_drawdown_usd: float | None = None
    sharpe_ratio: float | None = None
    sortino_ratio: float | None = None
    avg_trade_pips: float | None = None
    avg_hold_bars: float | None = None
    exposure_pct: float | None = None
    max_consecutive_wins: int | None = None
    max_consecutive_losses: int | None = None
    availability: dict[str, MetricAvailability] = field(default_factory=dict)


# ------------------------------------------------------------------
# Engine result
# ------------------------------------------------------------------


@dataclass
class EngineResult:
    """Complete result from one engine run."""

    engine: EngineId
    engine_version: str
    trades: list[NormalizedTrade] = field(default_factory=list)
    metrics: NormalizedMetrics = field(default_factory=NormalizedMetrics)
    elapsed_seconds: float = 0.0
    error: str | None = None
    raw_result: Any = None
    config_notes: list[str] = field(default_factory=list)


# ------------------------------------------------------------------
# Divergence detection
# ------------------------------------------------------------------


@dataclass
class DivergenceThresholds:
    """Configurable thresholds for flagging metric divergences."""

    total_trades_pct: float = 10.0
    win_rate_abs: float = 5.0
    net_pnl_pct: float = 15.0
    max_drawdown_abs: float = 3.0
    sharpe_ratio_abs: float = 0.3
    profit_factor_abs: float = 0.3


class DivergenceSeverity(Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass
class MetricDivergence:
    """A single metric divergence between two engines."""

    metric_name: str
    engine_a: EngineId
    engine_b: EngineId
    value_a: float | None
    value_b: float | None
    difference: float | None
    severity: DivergenceSeverity
    explanation: str


# ------------------------------------------------------------------
# Consensus report
# ------------------------------------------------------------------


@dataclass
class ConsensusReport:
    """Final cross-validation output."""

    results: list[EngineResult] = field(default_factory=list)
    divergences: list[MetricDivergence] = field(default_factory=list)
    consensus_metrics: NormalizedMetrics = field(default_factory=NormalizedMetrics)
    agreement_score: float = 0.0
    thresholds: DivergenceThresholds = field(default_factory=DivergenceThresholds)
    failed_engines: list[str] = field(default_factory=list)
    summary: str = ""
    timestamp: float = 0.0
