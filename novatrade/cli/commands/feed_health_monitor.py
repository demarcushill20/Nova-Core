"""Enhanced Feed Health Monitor for NovaTrade.

Provides comprehensive feed health diagnostics, automatic recovery actions,
and real-time monitoring with integration to the rate limiting system.
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import requests

from novatrade.adapter.metaapi_provider import MetaApiAdapter
from novatrade.config import NovaTradeCfg
from novatrade.models import HealthStatus
from novatrade.monitor.rate_limit_guardian import get_global_guardian

log = logging.getLogger("novatrade.cli.feed_health_monitor")

@dataclass
class FeedHealthReport:
    """Feed health diagnostic report."""
    timestamp: float
    overall_status: str  # HEALTHY, STALE, CRITICAL, ERROR
    symbols: dict[str, dict[str, Any]]
    rate_limiting_status: dict[str, Any]
    recovery_actions: list[str]
    recommendations: list[str]
    service_uptime: float

    def is_healthy(self) -> bool:
        return self.overall_status == "HEALTHY"

    def needs_intervention(self) -> bool:
        return self.overall_status in ("CRITICAL", "ERROR")


class FeedHealthMonitor:
    """Enhanced feed health monitoring and recovery system."""

    # Health thresholds
    HEALTHY_THRESHOLD = 60.0      # < 60s = healthy
    STALE_THRESHOLD = 300.0       # 60-300s = stale
    CRITICAL_THRESHOLD = 1800.0   # > 1800s = critical

    def __init__(self, config: NovaTradeCfg):
        self.config = config
        self.rate_guardian = get_global_guardian()
        self.adapter: MetaApiAdapter | None = None

    async def get_service_status(self) -> dict[str, Any]:
        """Get current service status from HTTP endpoint."""
        try:
            response = requests.get('http://localhost:8877/status', timeout=5)
            if response.status_code == 200:
                return response.json()
            else:
                return {"error": f"HTTP {response.status_code}"}
        except Exception as e:
            return {"error": str(e)}

    async def get_adapter_health(self) -> HealthStatus | None:
        """Get health status directly from adapter."""
        try:
            if not self.adapter:
                self.adapter = MetaApiAdapter(self.config.metaapi)
                await self.adapter.connect()

            return await self.adapter.health_check()
        except Exception as e:
            log.error(f"Error getting adapter health: {e}")
            return None

    def assess_feed_health(self, last_tick_age: float) -> tuple[str, str, list[str]]:
        """Assess feed health status and generate recommendations.

        Returns:
            (status, reason, recommendations)
        """
        if last_tick_age < self.HEALTHY_THRESHOLD:
            return "HEALTHY", "Feed is current", []

        elif last_tick_age < self.STALE_THRESHOLD:
            return "STALE", f"Feed age {last_tick_age:.1f}s - monitoring", [
                "Monitor for improvement in next few minutes",
                "Check network connectivity",
            ]

        elif last_tick_age < self.CRITICAL_THRESHOLD:
            return "CRITICAL", f"Feed age {last_tick_age:.1f}s - needs attention", [
                "Service restart recommended",
                "Check MetaAPI subscription status",
                "Verify broker connectivity",
                "Check rate limiting status"
            ]

        else:
            return "ERROR", f"Feed age {last_tick_age:.1f}s - immediate action required", [
                "IMMEDIATE: Service restart required",
                "Check MetaAPI account status",
                "Verify network connectivity",
                "Check for API quota exhaustion",
                "Consider switching to backup data source"
            ]

    async def diagnose_feed_health(self) -> FeedHealthReport:
        """Run comprehensive feed health diagnostic."""
        log.info("Running comprehensive feed health diagnostic...")
        timestamp = time.time()

        # Get service status
        service_status = await self.get_service_status()

        # Initialize report
        overall_status = "ERROR"
        symbols_data = {}
        recovery_actions = []
        recommendations = []

        if "error" in service_status:
            overall_status = "ERROR"
            recommendations.append("Service is not responding - check if novacore-novatrade.service is running")
            return FeedHealthReport(
                timestamp=timestamp,
                overall_status=overall_status,
                symbols=symbols_data,
                rate_limiting_status={},
                recovery_actions=recovery_actions,
                recommendations=recommendations,
                service_uptime=0
            )

        # Extract feed health data
        feed_health = service_status.get("feed_health", {})
        symbols = feed_health.get("symbols", {})
        uptime = service_status.get("uptime_seconds", 0)

        # Analyze each symbol
        worst_status = "HEALTHY"
        for symbol, symbol_data in symbols.items():
            last_tick_age = symbol_data.get("last_tick_age", float('inf'))
            current_state = symbol_data.get("state", "UNKNOWN")

            # Assess health
            health_status, reason, symbol_recommendations = self.assess_feed_health(last_tick_age)

            symbols_data[symbol] = {
                "symbol": symbol,
                "state": current_state,
                "last_tick_age": last_tick_age,
                "health_status": health_status,
                "reason": reason,
                "current_spread_pips": symbol_data.get("current_spread_pips"),
                "tick_count": symbol_data.get("tick_count", 0),
                "recommendations": symbol_recommendations
            }

            # Track worst status
            status_priority = {"HEALTHY": 0, "STALE": 1, "CRITICAL": 2, "ERROR": 3}
            if status_priority.get(health_status, 3) > status_priority.get(worst_status, 0):
                worst_status = health_status

            # Aggregate recommendations
            recommendations.extend(symbol_recommendations)

        overall_status = worst_status

        # Get rate limiting status
        rate_status = {}
        try:
            rate_status = self.rate_guardian.generate_health_report()
        except Exception as e:
            log.error(f"Error getting rate limiting status: {e}")
            rate_status = {"error": str(e)}

        # Generate recovery actions based on status
        if overall_status == "ERROR":
            recovery_actions.extend([
                "sudo systemctl restart novacore-novatrade.service",
                "Check MetaAPI account status and quotas",
                "Verify network connectivity to mt-client-api-v1.london-a.agiliumtrade.ai",
                "Review logs in /var/log/novacore-novatrade/"
            ])
        elif overall_status == "CRITICAL":
            recovery_actions.extend([
                "Consider service restart if no improvement in 5 minutes",
                "Check MetaAPI subscription usage",
                "Monitor rate limiting guardian status"
            ])
        elif overall_status == "STALE":
            recovery_actions.extend([
                "Monitor for auto-recovery",
                "Check network stability",
                "Verify broker trading hours"
            ])

        # Remove duplicates
        recommendations = list(set(recommendations))
        recovery_actions = list(set(recovery_actions))

        return FeedHealthReport(
            timestamp=timestamp,
            overall_status=overall_status,
            symbols=symbols_data,
            rate_limiting_status=rate_status,
            recovery_actions=recovery_actions,
            recommendations=recommendations,
            service_uptime=uptime
        )

    async def continuous_monitoring(self, interval: float = 30.0, alert_threshold: int = 3) -> None:
        """Run continuous feed health monitoring with alerting."""
        log.info(f"Starting continuous feed health monitoring (interval={interval}s)")

        consecutive_critical = 0
        last_alert_time = 0.0
        alert_cooldown = 300.0  # 5 minutes between alerts

        while True:
            try:
                report = await self.diagnose_feed_health()

                # Log status
                log.info(f"Feed health: {report.overall_status} ({len(report.symbols)} symbols)")
                for symbol, data in report.symbols.items():
                    log.info(f"  {symbol}: {data['health_status']} (age={data['last_tick_age']:.1f}s)")

                # Track consecutive critical states
                if report.needs_intervention():
                    consecutive_critical += 1
                else:
                    consecutive_critical = 0

                # Alert if consistently critical
                now = time.time()
                if (consecutive_critical >= alert_threshold and
                    now - last_alert_time > alert_cooldown):

                    await self._send_alert(report)
                    last_alert_time = now

                # Auto-recovery for specific issues
                await self._attempt_auto_recovery(report)

            except Exception as e:
                log.error(f"Error in feed health monitoring: {e}")

            await asyncio.sleep(interval)

    async def _send_alert(self, report: FeedHealthReport) -> None:
        """Send alert for critical feed health issues."""
        message = "🚨 NovaTrade Feed Health Alert\n\n"
        message += f"Status: {report.overall_status}\n"
        message += f"Uptime: {report.service_uptime:.0f}s\n\n"

        for symbol, data in report.symbols.items():
            message += f"{symbol}: {data['health_status']} ({data['last_tick_age']:.0f}s old)\n"

        if report.recovery_actions:
            message += "\nRecovery Actions:\n"
            for action in report.recovery_actions[:3]:  # Top 3 actions
                message += f"• {action}\n"

        log.warning(f"ALERT: {message}")

        # Here you could integrate with Telegram notifications, email, etc.
        # For now, we log at WARNING level which should trigger alerts

    async def _attempt_auto_recovery(self, report: FeedHealthReport) -> None:
        """Attempt automatic recovery for known issues."""
        if not report.needs_intervention():
            return

        # Check if rate limiting is causing issues
        rate_status = report.rate_limiting_status
        if isinstance(rate_status, dict):
            connection_metrics = rate_status.get("connection_metrics", {})
            recent_failures = connection_metrics.get("failures_24h", 0)

            if recent_failures > 10:
                log.warning("High connection failures detected - rate limiting may be causing feed issues")
                # Could trigger additional rate limiting protections here

        # Check for stale subscriptions that need refresh
        for symbol, data in report.symbols.items():
            if data["last_tick_age"] > self.CRITICAL_THRESHOLD:
                log.warning(f"Symbol {symbol} critically stale - marking for resubscription")
                # Here we could trigger targeted resubscription

    async def generate_status_report(self, format: str = "json") -> str:
        """Generate a formatted status report."""
        report = await self.diagnose_feed_health()

        if format == "json":
            return json.dumps({
                "timestamp": report.timestamp,
                "overall_status": report.overall_status,
                "symbols": report.symbols,
                "rate_limiting_status": report.rate_limiting_status,
                "recovery_actions": report.recovery_actions,
                "recommendations": report.recommendations,
                "service_uptime": report.service_uptime
            }, indent=2, default=str)

        elif format == "text":
            lines = []
            lines.append("=" * 60)
            lines.append("NovaTrade Feed Health Report")
            lines.append("=" * 60)
            lines.append(f"Timestamp: {datetime.fromtimestamp(report.timestamp).isoformat()}")
            lines.append(f"Overall Status: {report.overall_status}")
            lines.append(f"Service Uptime: {report.service_uptime:.0f}s ({report.service_uptime/3600:.1f}h)")
            lines.append("")

            lines.append("SYMBOL HEALTH:")
            for symbol, data in report.symbols.items():
                status_icon = {"HEALTHY": "✅", "STALE": "⚠️", "CRITICAL": "❌", "ERROR": "💥"}.get(data["health_status"], "❓")
                lines.append(f"  {status_icon} {symbol}: {data['health_status']} ({data['last_tick_age']:.1f}s)")

            if report.recovery_actions:
                lines.append("")
                lines.append("RECOVERY ACTIONS:")
                for i, action in enumerate(report.recovery_actions, 1):
                    lines.append(f"  {i}. {action}")

            if report.recommendations:
                lines.append("")
                lines.append("RECOMMENDATIONS:")
                for rec in report.recommendations:
                    lines.append(f"  • {rec}")

            return "\n".join(lines)

        else:
            raise ValueError(f"Unsupported format: {format}")


async def main():
    """CLI entry point for feed health monitoring."""
    import argparse

    parser = argparse.ArgumentParser(description="NovaTrade Feed Health Monitor")
    parser.add_argument('--monitor', action='store_true', help='Run continuous monitoring')
    parser.add_argument('--interval', type=float, default=30.0, help='Monitoring interval in seconds')
    parser.add_argument('--format', choices=['json', 'text'], default='text', help='Output format')
    parser.add_argument('--once', action='store_true', help='Run once and exit (default)')

    args = parser.parse_args()

    try:
        config = NovaTradeCfg.load()
        monitor = FeedHealthMonitor(config)

        if args.monitor:
            # Continuous monitoring mode
            await monitor.continuous_monitoring(interval=args.interval)
        else:
            # Single diagnostic run
            report_text = await monitor.generate_status_report(format=args.format)
            print(report_text)

        return 0

    except Exception as e:
        log.error(f"Feed health monitoring failed: {e}")
        if args.format == 'json':
            print(json.dumps({"error": str(e)}, indent=2))
        else:
            print(f"❌ Feed health monitoring failed: {e}")
        return 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    exit(asyncio.run(main()))
