#!/usr/bin/env python3
"""NovaTrade Performance Dashboard - Real-time strategy analytics.

This script connects to the running NovaTrade service, analyzes performance metrics,
and generates actionable insights for strategy optimization. It can be run as a
standalone dashboard or integrated into monitoring workflows.

Usage:
    python3 -m novatrade.monitor.performance_dashboard
    python3 -m novatrade.monitor.performance_dashboard --json
    python3 -m novatrade.monitor.performance_dashboard --alerts-only
"""

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

import requests

from novatrade.monitor.performance_analyzer import PerformanceAnalyzer, AlertSeverity

log = logging.getLogger("novatrade.monitor.dashboard")


class PerformanceDashboard:
    """Real-time performance dashboard for NovaTrade."""

    def __init__(
        self,
        *,
        service_url: str = "http://localhost:8877",
        initial_equity: float = 100_000.0,
        output_file: Path | None = None,
    ) -> None:
        """Initialize performance dashboard.

        Args:
            service_url: URL of the running NovaTrade service.
            initial_equity: Starting account equity.
            output_file: Optional file to write JSON output.
        """
        self.service_url = service_url
        self.analyzer = PerformanceAnalyzer(initial_equity=initial_equity)
        self.output_file = output_file

    def fetch_status(self) -> dict[str, Any] | None:
        """Fetch current status from NovaTrade service."""
        try:
            response = requests.get(f"{self.service_url}/status", timeout=10)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            log.error("Failed to fetch status from %s: %s", self.service_url, e)
            return None

    def generate_text_report(self, summary: dict[str, Any]) -> str:
        """Generate human-readable text report."""
        lines = [
            "=" * 60,
            "NOVATRADE PERFORMANCE DASHBOARD",
            "=" * 60,
            "",
            f"📊 Performance Level: {summary['performance_level']}",
            f"💰 Account Equity: ${summary['equity']:,.2f}",
            f"📈 P&L: ${summary['equity_change']:+,.2f} ({summary['equity_change_pct']:+.2f}%)",
            f"📡 Signals Generated: {summary['total_signals']}",
            f"✅ Approval Rate: {summary['approval_rate']:.1%}",
            f"⚡ Execution Quality: {summary['execution_quality_score']}/100",
            f"🌐 Feed Health: {summary['feed_health_score']:.1%}",
            f"🔄 Consecutive Losses: {summary['consecutive_losses']}",
            f"⏱️ Uptime: {summary['uptime_hours']:.1f} hours",
            f"📊 Trend: {summary['trend']}",
            "",
        ]

        # Add alerts section
        if summary['alert_count'] > 0:
            lines.extend([
                "⚠️  PERFORMANCE ALERTS",
                "-" * 30,
            ])

            for alert in summary['alerts']:
                severity_emoji = {
                    'INFO': '💡',
                    'WARNING': '⚠️',
                    'CRITICAL': '🚨',
                }
                emoji = severity_emoji.get(alert['severity'], '⚠️')
                lines.append(f"{emoji} {alert['severity']}: {alert['message']}")

            lines.append("")
        else:
            lines.extend([
                "✅ No performance alerts",
                "",
            ])

        # Add recommendations
        lines.extend([
            "💡 RECOMMENDATIONS",
            "-" * 30,
        ])

        recommendations = self._generate_recommendations(summary)
        if recommendations:
            for rec in recommendations:
                lines.append(f"• {rec}")
        else:
            lines.append("• System performing optimally, no immediate actions needed")

        lines.extend([
            "",
            f"Last updated: {time.strftime('%Y-%m-%d %H:%M:%S UTC')}",
            "=" * 60,
        ])

        return "\n".join(lines)

    def _generate_recommendations(self, summary: dict[str, Any]) -> list[str]:
        """Generate actionable recommendations based on performance data."""
        recommendations = []

        # Performance-based recommendations
        if summary['performance_level'] in ['POOR', 'CRITICAL']:
            recommendations.append(
                "Consider reducing position size or pausing trading until performance improves"
            )

        if summary['consecutive_losses'] >= 3:
            recommendations.append(
                "Review recent market conditions and strategy parameters after consecutive losses"
            )

        if summary['approval_rate'] < 0.9:
            recommendations.append(
                "Low approval rate detected - check risk management settings and market conditions"
            )

        if summary['execution_quality_score'] < 80:
            recommendations.append(
                "Execution quality below optimal - monitor spreads and latency"
            )

        if summary['trend'] == 'DECLINING':
            recommendations.append(
                "Performance trend declining - consider strategy parameter optimization"
            )

        # Alert-based recommendations
        critical_alerts = [a for a in summary['alerts'] if a['severity'] == 'CRITICAL']
        if critical_alerts:
            recommendations.append(
                "Critical alerts present - immediate attention required"
            )

        return recommendations

    def run_analysis(self) -> dict[str, Any] | None:
        """Run complete performance analysis."""
        status_data = self.fetch_status()
        if not status_data:
            return None

        metrics = self.analyzer.analyze_status(status_data)
        summary = self.analyzer.generate_summary(metrics)

        # Add raw status for detailed analysis
        summary["raw_status"] = status_data
        summary["analysis_timestamp"] = time.time()

        return summary

    def save_output(self, summary: dict[str, Any]) -> None:
        """Save analysis output to file if configured."""
        if not self.output_file:
            return

        try:
            self.output_file.parent.mkdir(parents=True, exist_ok=True)
            with self.output_file.open('w') as f:
                json.dump(summary, f, indent=2)
            log.info("Analysis saved to %s", self.output_file)
        except OSError as e:
            log.error("Failed to save analysis to %s: %s", self.output_file, e)


def main() -> None:
    """Command-line interface for performance dashboard."""
    parser = argparse.ArgumentParser(
        description="NovaTrade Performance Dashboard - Real-time strategy analytics"
    )
    parser.add_argument(
        "--service-url",
        default="http://localhost:8877",
        help="URL of the running NovaTrade service (default: http://localhost:8877)"
    )
    parser.add_argument(
        "--initial-equity",
        type=float,
        default=100_000.0,
        help="Starting account equity for calculations (default: 100000.0)"
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output results in JSON format instead of human-readable text"
    )
    parser.add_argument(
        "--alerts-only",
        action="store_true",
        help="Only show alerts, skip full dashboard"
    )
    parser.add_argument(
        "--output-file",
        type=Path,
        help="Save analysis to JSON file"
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress informational logging"
    )

    args = parser.parse_args()

    # Configure logging
    level = logging.WARNING if args.quiet else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    dashboard = PerformanceDashboard(
        service_url=args.service_url,
        initial_equity=args.initial_equity,
        output_file=args.output_file,
    )

    summary = dashboard.run_analysis()
    if not summary:
        print("Error: Unable to fetch performance data from NovaTrade service")
        sys.exit(1)

    # Save output file if requested
    if args.output_file:
        dashboard.save_output(summary)

    # Generate output based on requested format
    if args.json:
        print(json.dumps(summary, indent=2))
    elif args.alerts_only:
        alerts = summary.get('alerts', [])
        if alerts:
            print(f"Found {len(alerts)} performance alerts:")
            for alert in alerts:
                severity_prefix = {
                    'INFO': '[INFO]',
                    'WARNING': '[WARN]',
                    'CRITICAL': '[CRIT]',
                }.get(alert['severity'], '[WARN]')
                print(f"{severity_prefix} {alert['message']}")
        else:
            print("No performance alerts")
    else:
        report = dashboard.generate_text_report(summary)
        print(report)


if __name__ == "__main__":
    main()