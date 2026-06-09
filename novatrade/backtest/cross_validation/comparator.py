"""Cross-engine comparison and consensus computation."""

from __future__ import annotations

import statistics
import time

from novatrade.backtest.cross_validation.types import (
    ConsensusReport,
    DivergenceSeverity,
    DivergenceThresholds,
    EngineResult,
    MetricDivergence,
    NormalizedMetrics,
)

# Metrics compared as absolute difference (percentage points or units)
_ABSOLUTE_METRICS = {
    "win_rate": "win_rate_abs",
    "max_drawdown_pct": "max_drawdown_abs",
    "sharpe_ratio": "sharpe_ratio_abs",
    "profit_factor": "profit_factor_abs",
}

# Metrics compared as percentage difference
_PERCENTAGE_METRICS = {
    "total_trades": "total_trades_pct",
    "net_pnl_usd": "net_pnl_pct",
    "net_pnl_pips": "net_pnl_pct",
}

# Human-readable explanations for typical divergences
_DIVERGENCE_EXPLANATIONS = {
    "total_trades": (
        "Trade count differences often stem from fill model quirks"
        " — stop-order vs close-price fills, or pending order expiry handling."
    ),
    "win_rate": "Win rate divergence may indicate different trailing stop or exit logic between engines.",
    "net_pnl_usd": "P&L differences accumulate from spread/commission modelling, fill prices, and compounding effects.",
    "net_pnl_pips": "Pip-based P&L may differ due to position sizing and cost accounting approaches.",
    "max_drawdown_pct": "Drawdown depends on equity curve granularity — bar-level vs trade-level sampling.",
    "sharpe_ratio": "Sharpe computation varies: daily vs bar-level returns, annualisation factor, risk-free rate.",
    "profit_factor": "Profit factor is sensitive to small P&L differences on breakeven trades.",
}


class CrossValidator:
    """Compares results across engines and produces a consensus report."""

    def __init__(self, thresholds: DivergenceThresholds | None = None):
        self.thresholds = thresholds or DivergenceThresholds()

    def compare(self, results: list[EngineResult]) -> ConsensusReport:
        """Compare all engine results and produce a consensus report."""
        valid = [r for r in results if r.error is None]
        failed = [f"{r.engine.value}: {r.error}" for r in results if r.error is not None]

        if len(valid) < 2:
            return ConsensusReport(
                results=results,
                failed_engines=failed,
                summary=f"Need at least 2 successful engines for comparison. Got {len(valid)}.",
                timestamp=time.time(),
                thresholds=self.thresholds,
            )

        divergences = self._find_divergences(valid)
        consensus = self._compute_consensus_metrics(valid)
        score = self._compute_agreement_score(divergences)
        summary = self._generate_summary(valid, divergences, score, failed)

        return ConsensusReport(
            results=results,
            divergences=divergences,
            consensus_metrics=consensus,
            agreement_score=score,
            thresholds=self.thresholds,
            failed_engines=failed,
            summary=summary,
            timestamp=time.time(),
        )

    def _find_divergences(self, results: list[EngineResult]) -> list[MetricDivergence]:
        """Compare each metric pair-wise across all engines."""
        divergences: list[MetricDivergence] = []
        for i in range(len(results)):
            for j in range(i + 1, len(results)):
                divergences.extend(self._compare_pair(results[i], results[j]))
        return divergences

    def _compare_pair(self, a: EngineResult, b: EngineResult) -> list[MetricDivergence]:
        """Compare all metrics between two engines."""
        divergences: list[MetricDivergence] = []

        for metric_name, threshold_attr in _PERCENTAGE_METRICS.items():
            d = self._check_metric_pct(metric_name, a, b, getattr(self.thresholds, threshold_attr))
            if d:
                divergences.append(d)

        for metric_name, threshold_attr in _ABSOLUTE_METRICS.items():
            d = self._check_metric_abs(metric_name, a, b, getattr(self.thresholds, threshold_attr))
            if d:
                divergences.append(d)

        return divergences

    def _check_metric_pct(
        self,
        metric_name: str,
        a: EngineResult,
        b: EngineResult,
        threshold: float,
    ) -> MetricDivergence | None:
        val_a = getattr(a.metrics, metric_name, None)
        val_b = getattr(b.metrics, metric_name, None)
        # Bug #3 fix: Skip comparison for missing metrics (None values)
        # This prevents missing metrics from being scored as divergent
        if val_a is None or val_b is None:
            return None

        # Percentage difference relative to the larger absolute value
        denom = max(abs(val_a), abs(val_b), 1e-10)
        pct_diff = abs(val_a - val_b) / denom * 100

        if pct_diff <= threshold * 0.5:
            severity = DivergenceSeverity.INFO
        elif pct_diff <= threshold:
            severity = DivergenceSeverity.WARNING
        else:
            severity = DivergenceSeverity.CRITICAL

        return MetricDivergence(
            metric_name=metric_name,
            engine_a=a.engine,
            engine_b=b.engine,
            value_a=val_a,
            value_b=val_b,
            difference=pct_diff,
            severity=severity,
            explanation=_DIVERGENCE_EXPLANATIONS.get(metric_name, ""),
        )

    def _check_metric_abs(
        self,
        metric_name: str,
        a: EngineResult,
        b: EngineResult,
        threshold: float,
    ) -> MetricDivergence | None:
        val_a = getattr(a.metrics, metric_name, None)
        val_b = getattr(b.metrics, metric_name, None)
        # Bug #3 fix: Skip comparison for missing metrics (None values)
        # This prevents missing metrics from being scored as divergent
        if val_a is None or val_b is None:
            return None

        abs_diff = abs(val_a - val_b)

        if abs_diff <= threshold * 0.5:
            severity = DivergenceSeverity.INFO
        elif abs_diff <= threshold:
            severity = DivergenceSeverity.WARNING
        else:
            severity = DivergenceSeverity.CRITICAL

        return MetricDivergence(
            metric_name=metric_name,
            engine_a=a.engine,
            engine_b=b.engine,
            value_a=val_a,
            value_b=val_b,
            difference=abs_diff,
            severity=severity,
            explanation=_DIVERGENCE_EXPLANATIONS.get(metric_name, ""),
        )

    def _compute_consensus_metrics(self, results: list[EngineResult]) -> NormalizedMetrics:
        """Compute median of each metric across engines (robust to outliers)."""
        consensus = NormalizedMetrics()
        metric_fields = [
            "total_trades",
            "long_trades",
            "short_trades",
            "win_rate",
            "net_pnl_pips",
            "net_pnl_usd",
            "profit_factor",
            "max_drawdown_pct",
            "max_drawdown_usd",
            "sharpe_ratio",
            "sortino_ratio",
            "avg_trade_pips",
            "avg_hold_bars",
            "exposure_pct",
            "max_consecutive_wins",
            "max_consecutive_losses",
        ]

        for fname in metric_fields:
            values = []
            for r in results:
                v = getattr(r.metrics, fname, None)
                if v is not None:
                    values.append(float(v))
            if values:
                median_val = statistics.median(values)
                # Preserve int type for count fields
                if fname in (
                    "total_trades",
                    "long_trades",
                    "short_trades",
                    "max_consecutive_wins",
                    "max_consecutive_losses",
                ):
                    setattr(consensus, fname, round(median_val))
                else:
                    setattr(consensus, fname, round(median_val, 4))

        return consensus

    def _compute_agreement_score(self, divergences: list[MetricDivergence]) -> float:
        """Compute 0–100 score. 100 = perfect agreement."""
        if not divergences:
            return 100.0

        penalties = {
            DivergenceSeverity.INFO: 0,
            DivergenceSeverity.WARNING: 3,
            DivergenceSeverity.CRITICAL: 10,
        }
        total_penalty = sum(penalties.get(d.severity, 0) for d in divergences)
        max_penalty = len(divergences) * 10
        score = max(0.0, 100.0 - (total_penalty / max_penalty * 100))
        return round(score, 1)

    def _generate_summary(
        self,
        results: list[EngineResult],
        divergences: list[MetricDivergence],
        score: float,
        failed: list[str],
    ) -> str:
        engines = [r.engine.value for r in results]
        critical = [d for d in divergences if d.severity == DivergenceSeverity.CRITICAL]
        warnings = [d for d in divergences if d.severity == DivergenceSeverity.WARNING]

        lines = [
            f"Cross-validation across {len(results)} engines: {', '.join(engines)}",
            f"Agreement score: {score}/100",
        ]

        if failed:
            lines.append(f"Failed engines: {'; '.join(failed)}")

        if critical:
            lines.append(f"CRITICAL divergences ({len(critical)}):")
            for d in critical:
                lines.append(
                    f"  - {d.metric_name}: {d.engine_a.value}={d.value_a}, "
                    f"{d.engine_b.value}={d.value_b} (diff={d.difference:.1f})"
                )

        if warnings:
            lines.append(f"Warnings ({len(warnings)}):")
            for d in warnings:
                lines.append(f"  - {d.metric_name}: {d.engine_a.value}={d.value_a}, {d.engine_b.value}={d.value_b}")

        if not critical and not warnings:
            lines.append("All metrics within acceptable thresholds.")

        return "\n".join(lines)


def format_consensus_report(report: ConsensusReport) -> str:
    """Render ConsensusReport as markdown."""
    lines = [
        "# Cross-Validation Consensus Report",
        "",
        report.summary,
        "",
        "## Engine Results",
        "",
    ]

    for r in report.results:
        status = "OK" if r.error is None else f"FAILED: {r.error}"
        lines.append(f"### {r.engine.value} ({r.engine_version})")
        lines.append(f"- Status: {status}")
        if r.error is None:
            m = r.metrics
            lines.append(f"- Trades: {m.total_trades}")
            lines.append(f"- Win rate: {m.win_rate:.1f}%")
            lines.append(f"- Net P&L: ${m.net_pnl_usd:,.2f}" if m.net_pnl_usd else "- Net P&L: N/A")
            lines.append(f"- Max DD: {m.max_drawdown_pct:.1f}%" if m.max_drawdown_pct else "- Max DD: N/A")
            lines.append(f"- Sharpe: {m.sharpe_ratio:.2f}" if m.sharpe_ratio else "- Sharpe: N/A")
            lines.append(f"- Profit Factor: {m.profit_factor:.2f}" if m.profit_factor else "- PF: N/A")
            lines.append(f"- Time: {r.elapsed_seconds:.2f}s")
            if r.config_notes:
                lines.append(f"- Notes: {'; '.join(r.config_notes)}")
        lines.append("")

    if report.divergences:
        lines.append("## Divergences")
        lines.append("")
        for d in report.divergences:
            icon = {"critical": "!!!", "warning": "!", "info": "."}[d.severity.value]
            lines.append(
                f"- [{icon}] **{d.metric_name}**: "
                f"{d.engine_a.value}={d.value_a} vs {d.engine_b.value}={d.value_b} "
                f"(diff={d.difference:.2f}, {d.severity.value})"
            )
            if d.explanation:
                lines.append(f"  > {d.explanation}")
        lines.append("")

    cm = report.consensus_metrics
    lines.extend(
        [
            "## Consensus Metrics (median)",
            "",
            "| Metric | Value |",
            "|--------|-------|",
            f"| Trades | {cm.total_trades} |",
            f"| Win Rate | {cm.win_rate:.1f}% |" if cm.win_rate is not None else "| Win Rate | N/A |",
            f"| Net P&L | ${cm.net_pnl_usd:,.2f} |" if cm.net_pnl_usd is not None else "| Net P&L | N/A |",
            f"| Max DD | {cm.max_drawdown_pct:.1f}% |" if cm.max_drawdown_pct is not None else "| Max DD | N/A |",
            f"| Sharpe | {cm.sharpe_ratio:.2f} |" if cm.sharpe_ratio is not None else "| Sharpe | N/A |",
            f"| PF | {cm.profit_factor:.2f} |" if cm.profit_factor is not None else "| PF | N/A |",
            "",
            f"**Agreement Score: {report.agreement_score}/100**",
        ]
    )

    return "\n".join(lines)


def format_consensus_telegram(report: ConsensusReport) -> str:
    """Render concise ConsensusReport for Telegram."""
    engines = len(report.results)
    ok = len([r for r in report.results if r.error is None])
    crit = len([d for d in report.divergences if d.severity == DivergenceSeverity.CRITICAL])

    cm = report.consensus_metrics
    lines = [
        f"Cross-Val: {ok}/{engines} engines | Score: {report.agreement_score}/100",
    ]
    if cm.total_trades is not None:
        lines.append(f"Trades: {cm.total_trades} | WR: {cm.win_rate:.0f}%")
    if cm.net_pnl_usd is not None:
        lines.append(f"P&L: ${cm.net_pnl_usd:,.0f} | DD: {cm.max_drawdown_pct:.1f}%")
    if crit:
        lines.append(f"CRITICAL divergences: {crit}")

    return "\n".join(lines)
