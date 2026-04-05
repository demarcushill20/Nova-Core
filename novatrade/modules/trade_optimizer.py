#!/usr/bin/env python3
"""Live trade optimization analyzer for NovaTrade.

Analyzes recent trading patterns from live evidence to identify optimization
opportunities and provide actionable recommendations for strategy improvement.

This module provides:
- Pattern recognition in trade execution
- Stop loss effectiveness analysis
- Entry timing optimization insights
- Risk management efficiency scoring
- Actionable optimization recommendations
"""

from __future__ import annotations

import json
import logging
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("novatrade.modules.trade_optimizer")


@dataclass
class TradePattern:
    """Identified trading pattern with performance metrics."""

    pattern_type: str
    occurrence_count: int
    avg_duration_minutes: float
    success_rate: float
    avg_profit_pips: float
    confidence: float


@dataclass
class OptimizationRecommendation:
    """Actionable optimization recommendation."""

    category: str  # 'entry_timing', 'risk_management', 'exit_strategy'
    priority: str  # 'high', 'medium', 'low'
    description: str
    expected_impact: str
    implementation_effort: str
    supporting_data: dict[str, Any]


@dataclass
class TradeOptimizationReport:
    """Comprehensive optimization analysis report."""

    analysis_period: str
    trades_analyzed: int
    patterns_identified: list[TradePattern]
    recommendations: list[OptimizationRecommendation]
    overall_efficiency_score: float
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class LiveTradeOptimizer:
    """Analyzes live trading patterns to suggest optimizations."""

    def __init__(self, evidence_file: Path):
        self.evidence_file = evidence_file
        self.trades_data: list[dict] = []

    def load_recent_trades(self, hours: int = 24) -> list[dict]:
        """Load recent trade data from evidence file."""
        try:
            cutoff_time = datetime.now(timezone.utc).timestamp() - (hours * 3600)

            with open(self.evidence_file) as f:
                lines = f.readlines()

            recent_trades = []
            for line in lines:
                try:
                    event = json.loads(line.strip())
                    if event.get("timestamp", 0) > cutoff_time and event.get("event_type") == "EXECUTION":
                        recent_trades.append(event)
                except json.JSONDecodeError:
                    continue

            log.info(f"Loaded {len(recent_trades)} recent trade events")
            return recent_trades

        except FileNotFoundError:
            log.warning(f"Evidence file not found: {self.evidence_file}")
            return []

    def analyze_stop_loss_effectiveness(self, trades: list[dict]) -> OptimizationRecommendation | None:
        """Analyze stop loss modification patterns and effectiveness."""
        sl_modifications = [
            trade for trade in trades if trade.get("data", {}).get("trading_agent_event") == "SL_MODIFIED"
        ]

        if len(sl_modifications) < 3:
            return None

        # Analyze SL modification frequency
        position_modifications: dict[str, int] = {}
        for trade in sl_modifications:
            pos_id = trade.get("data", {}).get("position_id", "unknown")
            position_modifications[pos_id] = position_modifications.get(pos_id, 0) + 1

        avg_modifications = statistics.mean(position_modifications.values())

        if avg_modifications > 5:
            return OptimizationRecommendation(
                category="risk_management",
                priority="medium",
                description=(
                    f"High SL modification frequency detected ({avg_modifications:.1f} avg per position). "
                    "Consider widening initial stop loss to reduce noise trading."
                ),
                expected_impact="Reduced transaction costs and improved trade execution",
                implementation_effort="Low - adjust initial SL distance in strategy config",
                supporting_data={
                    "avg_modifications_per_position": avg_modifications,
                    "positions_analyzed": len(position_modifications),
                },
            )

        return None

    def analyze_rollover_denials(self, trades: list[dict]) -> OptimizationRecommendation | None:
        """Analyze risk denials during rollover periods."""
        rollover_denials = [
            trade
            for trade in trades
            if (
                trade.get("data", {}).get("trading_agent_event") == "RISK_DENIED"
                and "rollover dead zone" in str(trade.get("data", {}).get("reason", ""))
            )
        ]

        if len(rollover_denials) >= 3:
            return OptimizationRecommendation(
                category="entry_timing",
                priority="high",
                description=(
                    f"Multiple rollover zone denials detected ({len(rollover_denials)} trades). "
                    "Strategy may be generating signals during suboptimal hours."
                ),
                expected_impact="Improved fill quality and reduced slippage risk",
                implementation_effort="Medium - adjust signal generation timing or rollover window",
                supporting_data={
                    "rollover_denials": len(rollover_denials),
                    "denial_rate": len(rollover_denials) / len(trades) if trades else 0,
                },
            )

        return None

    def analyze_position_lifecycle(self, trades: list[dict]) -> TradePattern | None:
        """Analyze complete position lifecycles for patterns."""
        # Group trades by position ID
        positions: dict[str, list[dict]] = {}
        for trade in trades:
            pos_id = trade.get("data", {}).get("position_id")
            if pos_id:
                positions.setdefault(pos_id, []).append(trade)

        if len(positions) < 1:
            return None

        # Analyze position durations
        durations = []
        for _pos_id, pos_trades in positions.items():
            pos_trades.sort(key=lambda x: x.get("timestamp", 0))
            if len(pos_trades) >= 2:
                start_time = pos_trades[0].get("timestamp", 0)
                end_time = pos_trades[-1].get("timestamp", 0)
                duration_minutes = (end_time - start_time) / 60
                durations.append(duration_minutes)

        if durations:
            avg_duration = statistics.mean(durations)
            return TradePattern(
                pattern_type="position_duration",
                occurrence_count=len(durations),
                avg_duration_minutes=avg_duration,
                success_rate=0.0,  # Would need P&L data to calculate
                avg_profit_pips=0.0,  # Would need P&L data to calculate
                confidence=0.8 if len(durations) >= 3 else 0.6,
            )

        return None

    def generate_optimization_report(self, hours: int = 24) -> TradeOptimizationReport:
        """Generate comprehensive optimization analysis report."""
        trades = self.load_recent_trades(hours)

        patterns = []
        recommendations = []

        # Analyze patterns
        duration_pattern = self.analyze_position_lifecycle(trades)
        if duration_pattern:
            patterns.append(duration_pattern)

        # Generate recommendations
        sl_rec = self.analyze_stop_loss_effectiveness(trades)
        if sl_rec:
            recommendations.append(sl_rec)

        rollover_rec = self.analyze_rollover_denials(trades)
        if rollover_rec:
            recommendations.append(rollover_rec)

        # Calculate efficiency score (simplified)
        denied_trades = len([t for t in trades if t.get("data", {}).get("trading_agent_event") == "RISK_DENIED"])
        total_signals = len([t for t in trades if "trading_agent_event" in t.get("data", {})])

        efficiency_score = max(0, 100 - (denied_trades / max(total_signals, 1)) * 100) if total_signals > 0 else 100

        return TradeOptimizationReport(
            analysis_period=f"Last {hours} hours",
            trades_analyzed=len(trades),
            patterns_identified=patterns,
            recommendations=recommendations,
            overall_efficiency_score=efficiency_score,
        )

    def save_report(self, report: TradeOptimizationReport, output_dir: Path) -> Path:
        """Save optimization report to file."""
        output_dir.mkdir(exist_ok=True)

        timestamp = report.timestamp.strftime("%Y%m%d_%H%M%S")
        filename = f"trade_optimization_report_{timestamp}.json"
        filepath = output_dir / filename

        report_dict = {
            "analysis_period": report.analysis_period,
            "trades_analyzed": report.trades_analyzed,
            "patterns_identified": [
                {
                    "pattern_type": p.pattern_type,
                    "occurrence_count": p.occurrence_count,
                    "avg_duration_minutes": p.avg_duration_minutes,
                    "success_rate": p.success_rate,
                    "avg_profit_pips": p.avg_profit_pips,
                    "confidence": p.confidence,
                }
                for p in report.patterns_identified
            ],
            "recommendations": [
                {
                    "category": r.category,
                    "priority": r.priority,
                    "description": r.description,
                    "expected_impact": r.expected_impact,
                    "implementation_effort": r.implementation_effort,
                    "supporting_data": r.supporting_data,
                }
                for r in report.recommendations
            ],
            "overall_efficiency_score": report.overall_efficiency_score,
            "timestamp": report.timestamp.isoformat(),
            "generated_by": "LiveTradeOptimizer v1.0",
        }

        with open(filepath, "w") as f:
            json.dump(report_dict, f, indent=2)

        log.info(f"Optimization report saved to: {filepath}")
        return filepath


def main():
    """CLI entry point for trade optimization analysis."""
    import argparse

    parser = argparse.ArgumentParser(description="Analyze live trades for optimization opportunities")
    parser.add_argument(
        "--evidence-file",
        type=Path,
        default=Path("/home/nova/nova-core/OUTPUT/novatrade/live_evidence.jsonl"),
        help="Path to live evidence JSONL file",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("/home/nova/nova-core/OUTPUT/novatrade"),
        help="Output directory for reports",
    )
    parser.add_argument("--hours", type=int, default=24, help="Hours of data to analyze")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose logging")

    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.INFO)

    optimizer = LiveTradeOptimizer(args.evidence_file)
    report = optimizer.generate_optimization_report(args.hours)

    output_file = optimizer.save_report(report, args.output_dir)

    print("Trade Optimization Analysis Complete")
    print(f"Analysis Period: {report.analysis_period}")
    print(f"Trades Analyzed: {report.trades_analyzed}")
    print(f"Patterns Identified: {len(report.patterns_identified)}")
    print(f"Recommendations: {len(report.recommendations)}")
    print(f"Efficiency Score: {report.overall_efficiency_score:.1f}/100")
    print(f"Report saved: {output_file}")

    # Print recommendations
    if report.recommendations:
        print("\nOptimization Recommendations:")
        for i, rec in enumerate(report.recommendations, 1):
            print(f"\n{i}. [{rec.priority.upper()}] {rec.category}")
            print(f"   {rec.description}")
            print(f"   Expected Impact: {rec.expected_impact}")
            print(f"   Implementation: {rec.implementation_effort}")


if __name__ == "__main__":
    main()
