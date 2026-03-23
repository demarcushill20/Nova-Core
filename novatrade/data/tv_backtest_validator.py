"""TradingView backtest validation via browser snapshot metrics extraction.

Operator-triggered workflow: opens TradingView, extracts Strategy Tester
metrics from DOM snapshots, and compares against tv-cli / Python backtest
results to detect drift between environments.

NOT autonomous — requires operator to compile + apply Pine Script manually.
Browser automation handles navigation and metric extraction only.

Usage (from a skill / Claude session):
    from novatrade.data.tv_backtest_validator import (
        TVSnapshotMetrics,
        extract_metrics_from_snapshot,
        compare_backtests,
        ValidationReport,
    )

    # Parse metrics from Playwright accessibility-tree snapshot text
    metrics = extract_metrics_from_snapshot(snapshot_text)

    # Compare against tv-cli backtest
    report = compare_backtests(
        source=metrics,
        reference=reference_metrics,
        thresholds=DriftThresholds(),
    )
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

log = logging.getLogger("novatrade.data.tv_backtest_validator")


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class DriftSeverity(str, Enum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


@dataclass
class DriftThresholds:
    """Configurable thresholds for acceptable metric drift."""

    trade_count_pct: float = 5.0  # % difference in total trades
    net_profit_pct: float = 10.0  # % difference in net profit
    win_rate_abs: float = 2.0  # absolute pp difference in win rate
    profit_factor_pct: float = 15.0  # % difference in profit factor
    max_drawdown_pct: float = 15.0  # % difference in max drawdown
    sharpe_ratio_abs: float = 0.3  # absolute difference in Sharpe

    # Warn thresholds (below fail, above ok)
    warn_factor: float = 0.5  # warn at 50% of fail threshold


@dataclass
class TVSnapshotMetrics:
    """Metrics extracted from a TradingView Strategy Tester DOM snapshot."""

    net_profit: float | None = None
    net_profit_pct: float | None = None
    total_trades: int | None = None
    win_rate: float | None = None
    profit_factor: float | None = None
    sharpe_ratio: float | None = None
    sortino_ratio: float | None = None
    max_drawdown: float | None = None
    max_drawdown_pct: float | None = None
    avg_trade: float | None = None
    avg_win: float | None = None
    avg_loss: float | None = None
    largest_win: float | None = None
    largest_loss: float | None = None
    commission_paid: float | None = None
    total_long_trades: int | None = None
    total_short_trades: int | None = None
    winning_trades: int | None = None
    losing_trades: int | None = None
    raw_text: str = ""
    extracted_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class MetricDrift:
    """Drift analysis for a single metric."""

    metric: str
    source_value: float | None
    reference_value: float | None
    absolute_diff: float | None
    relative_diff_pct: float | None
    severity: DriftSeverity
    threshold_used: float


@dataclass
class ValidationReport:
    """Complete drift validation report."""

    overall: DriftSeverity
    drifts: list[MetricDrift]
    source_label: str
    reference_label: str
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    notes: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.overall != DriftSeverity.FAIL

    @property
    def failed_metrics(self) -> list[MetricDrift]:
        return [d for d in self.drifts if d.severity == DriftSeverity.FAIL]

    @property
    def warned_metrics(self) -> list[MetricDrift]:
        return [d for d in self.drifts if d.severity == DriftSeverity.WARN]

    def to_dict(self) -> dict[str, Any]:
        return {
            "overall": self.overall.value,
            "passed": self.passed,
            "source": self.source_label,
            "reference": self.reference_label,
            "timestamp": self.timestamp,
            "drifts": [
                {
                    "metric": d.metric,
                    "source": d.source_value,
                    "reference": d.reference_value,
                    "abs_diff": d.absolute_diff,
                    "rel_diff_pct": d.relative_diff_pct,
                    "severity": d.severity.value,
                    "threshold": d.threshold_used,
                }
                for d in self.drifts
            ],
            "failed": [d.metric for d in self.failed_metrics],
            "warned": [d.metric for d in self.warned_metrics],
            "notes": self.notes,
        }

    def save(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), indent=2))
        log.info("Validation report saved to %s", p)
        return p


# ---------------------------------------------------------------------------
# Metrics extraction from Playwright snapshot text
# ---------------------------------------------------------------------------

# TradingView Strategy Tester uses these labels in its accessibility tree.
# The snapshot text from Playwright's browser_snapshot is an accessibility
# tree dump where values follow their labels.

_METRIC_PATTERNS: dict[str, tuple[str, type]] = {
    "net_profit": (r"Net Profit\s*[:\-]?\s*[$€£]?\s*([\-\d,]+\.?\d*)", float),
    "net_profit_pct": (r"Net Profit\s*.*?([\-\d,]+\.?\d*)\s*%", float),
    "total_trades": (r"Total (?:Closed )?Trades\s*[:\-]?\s*(\d+)", int),
    "win_rate": (r"(?:Percent Profitable|Win Rate)\s*[:\-]?\s*([\d.]+)\s*%?", float),
    "profit_factor": (r"Profit Factor\s*[:\-]?\s*([\d.]+)", float),
    "sharpe_ratio": (r"Sharpe Ratio\s*[:\-]?\s*([\-\d.]+)", float),
    "sortino_ratio": (r"Sortino Ratio\s*[:\-]?\s*([\-\d.]+)", float),
    "max_drawdown": (r"Max Drawdown\s*[:\-]?\s*[$€£]?\s*([\-\d,]+\.?\d*)", float),
    "max_drawdown_pct": (r"Max Drawdown\s*.*?([\-\d.]+)\s*%", float),
    "avg_trade": (r"Avg Trade\s*[:\-]?\s*[$€£]?\s*([\-\d,]+\.?\d*)", float),
    "avg_win": (r"Avg Win(?:ning)? Trade\s*[:\-]?\s*[$€£]?\s*([\-\d,]+\.?\d*)", float),
    "avg_loss": (r"Avg Los(?:ing)? Trade\s*[:\-]?\s*[$€£]?\s*([\-\d,]+\.?\d*)", float),
    "largest_win": (r"Largest Win(?:ning)? Trade\s*[:\-]?\s*[$€£]?\s*([\-\d,]+\.?\d*)", float),
    "largest_loss": (r"Largest Los(?:ing)? Trade\s*[:\-]?\s*[$€£]?\s*([\-\d,]+\.?\d*)", float),
    "commission_paid": (r"Commission Paid\s*[:\-]?\s*[$€£]?\s*([\-\d,]+\.?\d*)", float),
    "total_long_trades": (r"(?:Total )?Long Trades\s*[:\-]?\s*(\d+)", int),
    "total_short_trades": (r"(?:Total )?Short Trades\s*[:\-]?\s*(\d+)", int),
    "winning_trades": (r"(?:Total )?Win(?:ning)? Trades\s*[:\-]?\s*(\d+)", int),
    "losing_trades": (r"(?:Total )?Los(?:ing)? Trades\s*[:\-]?\s*(\d+)", int),
}


def _parse_number(raw: str, target_type: type) -> float | int | None:
    """Parse a number string, removing commas and whitespace."""
    cleaned = raw.replace(",", "").replace(" ", "").strip()
    if not cleaned:
        return None
    try:
        return target_type(cleaned)
    except (ValueError, TypeError):
        return None


def extract_metrics_from_snapshot(snapshot_text: str) -> TVSnapshotMetrics:
    """Extract backtest metrics from a Playwright accessibility-tree snapshot.

    The snapshot text is the output of mcp__playwright__browser_snapshot,
    which returns an accessibility tree of the page. TradingView's Strategy
    Tester tab contains labeled metric values that we parse with regex.

    Args:
        snapshot_text: Raw text from browser_snapshot or any text containing
                      TradingView Strategy Tester metrics.

    Returns:
        TVSnapshotMetrics with extracted values (None for unmatched metrics).
    """
    metrics = TVSnapshotMetrics(raw_text=snapshot_text)
    matched = 0

    for attr, (pattern, target_type) in _METRIC_PATTERNS.items():
        match = re.search(pattern, snapshot_text, re.IGNORECASE)
        if match:
            value = _parse_number(match.group(1), target_type)
            if value is not None:
                setattr(metrics, attr, value)
                matched += 1

    log.info("Extracted %d/%d metrics from snapshot", matched, len(_METRIC_PATTERNS))
    return metrics


def extract_metrics_from_js_result(js_data: dict[str, Any]) -> TVSnapshotMetrics:
    """Extract metrics from a browser_evaluate JavaScript result.

    Alternative to snapshot parsing: run JavaScript in the page to extract
    exact numeric values from TradingView's internal data structures.

    Args:
        js_data: Dictionary returned by browser_evaluate JS execution.

    Returns:
        TVSnapshotMetrics with extracted values.
    """
    metrics = TVSnapshotMetrics()

    field_map = {
        "netProfit": ("net_profit", float),
        "netProfitPercent": ("net_profit_pct", float),
        "totalTrades": ("total_trades", int),
        "totalClosedTrades": ("total_trades", int),
        "percentProfitable": ("win_rate", float),
        "winRate": ("win_rate", float),
        "profitFactor": ("profit_factor", float),
        "sharpeRatio": ("sharpe_ratio", float),
        "sortinoRatio": ("sortino_ratio", float),
        "maxDrawDown": ("max_drawdown", float),
        "maxDrawdown": ("max_drawdown", float),
        "maxDrawdownPercent": ("max_drawdown_pct", float),
        "avgTrade": ("avg_trade", float),
        "avgWinTrade": ("avg_win", float),
        "avgLosTrade": ("avg_loss", float),
        "largestWinTrade": ("largest_win", float),
        "largestLosTrade": ("largest_loss", float),
        "commissionPaid": ("commission_paid", float),
    }

    for js_key, (attr, target_type) in field_map.items():
        value = js_data.get(js_key)
        if value is not None:
            try:
                setattr(metrics, attr, target_type(value))
            except (ValueError, TypeError):
                pass

    return metrics


# ---------------------------------------------------------------------------
# Drift comparison
# ---------------------------------------------------------------------------


def _compute_drift(
    metric_name: str,
    source_val: float | int | None,
    ref_val: float | int | None,
    threshold: float,
    mode: str = "relative",  # "relative" (%) or "absolute"
    warn_factor: float = 0.5,
) -> MetricDrift:
    """Compute drift between two metric values."""
    if source_val is None or ref_val is None:
        return MetricDrift(
            metric=metric_name,
            source_value=source_val,
            reference_value=ref_val,
            absolute_diff=None,
            relative_diff_pct=None,
            severity=DriftSeverity.OK,  # can't compare
            threshold_used=threshold,
        )

    abs_diff = abs(float(source_val) - float(ref_val))

    if mode == "relative":
        # Relative % difference
        if ref_val == 0:
            rel_diff = 100.0 if source_val != 0 else 0.0
        else:
            rel_diff = (abs_diff / abs(float(ref_val))) * 100
        compare_val = rel_diff
    else:
        # Absolute difference
        rel_diff = None
        compare_val = abs_diff

    if compare_val >= threshold:
        severity = DriftSeverity.FAIL
    elif compare_val >= threshold * warn_factor:
        severity = DriftSeverity.WARN
    else:
        severity = DriftSeverity.OK

    return MetricDrift(
        metric=metric_name,
        source_value=float(source_val) if source_val is not None else None,
        reference_value=float(ref_val) if ref_val is not None else None,
        absolute_diff=abs_diff,
        relative_diff_pct=rel_diff,
        severity=severity,
        threshold_used=threshold,
    )


def compare_backtests(
    source: TVSnapshotMetrics,
    reference: TVSnapshotMetrics,
    thresholds: DriftThresholds | None = None,
    source_label: str = "tv_browser",
    reference_label: str = "tv_cli",
) -> ValidationReport:
    """Compare two sets of backtest metrics and produce a drift report.

    Args:
        source: Metrics from browser snapshot (or any source).
        reference: Metrics from tv-cli or Python backtest (ground truth).
        thresholds: Acceptable drift thresholds. Defaults to DriftThresholds().
        source_label: Label for the source metrics.
        reference_label: Label for the reference metrics.

    Returns:
        ValidationReport with per-metric drift analysis.
    """
    t = thresholds or DriftThresholds()
    drifts: list[MetricDrift] = []

    # Define comparison spec: (metric_name, source_attr, ref_attr, threshold, mode)
    comparisons = [
        ("total_trades", "total_trades", "total_trades", t.trade_count_pct, "relative"),
        ("net_profit", "net_profit", "net_profit", t.net_profit_pct, "relative"),
        ("win_rate", "win_rate", "win_rate", t.win_rate_abs, "absolute"),
        ("profit_factor", "profit_factor", "profit_factor", t.profit_factor_pct, "relative"),
        ("max_drawdown", "max_drawdown", "max_drawdown", t.max_drawdown_pct, "relative"),
        ("sharpe_ratio", "sharpe_ratio", "sharpe_ratio", t.sharpe_ratio_abs, "absolute"),
    ]

    notes: list[str] = []

    for name, src_attr, ref_attr, threshold, mode in comparisons:
        src_val = getattr(source, src_attr, None)
        ref_val = getattr(reference, ref_attr, None)

        drift = _compute_drift(name, src_val, ref_val, threshold, mode, t.warn_factor)
        drifts.append(drift)

        if src_val is None and ref_val is not None:
            notes.append(f"{name}: source value missing")
        elif src_val is not None and ref_val is None:
            notes.append(f"{name}: reference value missing")

    # Overall severity = worst individual drift
    severities = [d.severity for d in drifts]
    if DriftSeverity.FAIL in severities:
        overall = DriftSeverity.FAIL
    elif DriftSeverity.WARN in severities:
        overall = DriftSeverity.WARN
    else:
        overall = DriftSeverity.OK

    return ValidationReport(
        overall=overall,
        drifts=drifts,
        source_label=source_label,
        reference_label=reference_label,
        notes=notes,
    )


def metrics_from_tv_cli_report(report: Any) -> TVSnapshotMetrics:
    """Convert a TVBacktestReport (from tv-cli) into TVSnapshotMetrics for comparison.

    This bridges the gap between the tv-cli dataclass format and the
    generic comparison interface.

    Args:
        report: A TVBacktestReport instance from tradingview_fetcher.

    Returns:
        TVSnapshotMetrics populated from the report.
    """
    perf = report.performance
    return TVSnapshotMetrics(
        net_profit=perf.net_profit,
        net_profit_pct=perf.net_profit_pct,
        total_trades=perf.total_trades,
        win_rate=perf.win_rate,
        profit_factor=perf.profit_factor,
        sharpe_ratio=perf.sharpe_ratio,
        sortino_ratio=perf.sortino_ratio,
        max_drawdown=perf.max_drawdown,
        avg_trade=perf.avg_trade,
        avg_win=perf.avg_win,
        avg_loss=perf.avg_loss,
        largest_win=perf.largest_win,
        largest_loss=perf.largest_loss,
        commission_paid=perf.commission_paid,
    )
