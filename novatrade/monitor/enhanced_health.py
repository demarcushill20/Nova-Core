"""Enhanced health monitoring with comprehensive broker diagnostics.

Extends the basic health monitoring with deep broker connectivity,
trading permissions, and account status diagnostics using the
BrokerDiagnostic tool to investigate issues like "tradeAllowed: false".
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from novatrade.adapter.base import MT5Adapter
from novatrade.models import (
    HealthSnapshot,
    HealthState,
    HealthStatus,
)
from utils.broker_diagnostic import BrokerDiagnostic

log = logging.getLogger("novatrade.monitor.enhanced_health")


class EnhancedHealthMonitor:
    """Comprehensive health monitoring with broker diagnostics."""

    def __init__(self, adapter: MT5Adapter):
        self.adapter = adapter
        self.last_diagnostic_time: float | None = None
        self.last_diagnostic_result: dict[str, Any] | None = None
        self.diagnostic_cache_ttl = 300  # 5 minutes

    async def take_health_snapshot_with_diagnostics(
        self, force_diagnostic: bool = False
    ) -> tuple[HealthSnapshot, dict[str, Any] | None]:
        """Take health snapshot with optional comprehensive diagnostics.

        Args:
            force_diagnostic: If True, always run broker diagnostic regardless of cache

        Returns:
            Tuple of (HealthSnapshot, diagnostic_results or None)
        """
        # First take standard health snapshot
        try:
            health = await self.adapter.health_check()
        except Exception as exc:
            log.error("health snapshot: adapter health_check failed: %s", exc)
            snapshot = HealthSnapshot(
                adapter_health=HealthStatus(
                    state=HealthState.DOWN,
                    message=str(exc),
                ),
                error=f"health_check failed: {exc}",
            )
            return snapshot, None

        # Get account and positions
        try:
            account = await self.adapter.get_account()
            positions = await self.adapter.get_positions()
            total_pnl = sum(p.unrealized_pnl for p in positions)

            snapshot = HealthSnapshot(
                adapter_health=health,
                account=account,
                position_count=len(positions),
                total_unrealized_pnl=total_pnl,
            )
        except Exception as exc:
            log.error("health snapshot: account/position check failed: %s", exc)
            snapshot = HealthSnapshot(
                adapter_health=health,
                error=f"account/position check failed: {exc}",
            )
            return snapshot, None

        # Determine if we should run broker diagnostics
        should_run_diagnostic = (
            force_diagnostic
            or health.state == HealthState.DOWN
            or (hasattr(account, "trade_allowed") and not getattr(account, "trade_allowed", True))
            or self._should_refresh_diagnostic()
        )

        diagnostic_result = None
        if should_run_diagnostic:
            diagnostic_result = await self._run_broker_diagnostic()

        return snapshot, diagnostic_result

    def _should_refresh_diagnostic(self) -> bool:
        """Check if cached diagnostic results need refreshing."""
        if self.last_diagnostic_time is None:
            return True

        now = datetime.now(timezone.utc).timestamp()
        return (now - self.last_diagnostic_time) > self.diagnostic_cache_ttl

    async def _run_broker_diagnostic(self) -> dict[str, Any] | None:
        """Run comprehensive broker diagnostic."""
        try:
            # Get adapter credentials for diagnostic
            adapter_config = getattr(self.adapter, "_config", None)
            if not adapter_config:
                log.warning("Cannot run broker diagnostic: adapter config not accessible")
                return None

            account_id = getattr(adapter_config, "account_id", None)
            token = getattr(adapter_config, "token", None)

            if not account_id or not token:
                log.warning("Cannot run broker diagnostic: missing account_id or token")
                return None

            # Run diagnostic with timeout
            diagnostic = BrokerDiagnostic(account_id, token)
            result = await asyncio.wait_for(
                diagnostic.run_full_diagnostic(),
                timeout=60.0,  # 1 minute timeout
            )

            # Cache results
            self.last_diagnostic_time = datetime.now(timezone.utc).timestamp()
            self.last_diagnostic_result = result

            log.info("Broker diagnostic completed: %d tests run", len(result.get("tests", {})))

            # Log critical findings
            summary = result.get("summary", {})
            if summary.get("critical_issues"):
                log.error("Broker diagnostic found critical issues: %s", summary["critical_issues"])

            return result

        except asyncio.TimeoutError:
            log.warning("Broker diagnostic timed out after 60 seconds")
            return None
        except Exception as exc:
            log.error("Broker diagnostic failed: %s", exc)
            return None

    async def diagnose_trade_permission_issue(self) -> dict[str, Any]:
        """Specifically diagnose trading permission issues like 'tradeAllowed: false'."""
        log.info("Running focused trade permission diagnostic...")

        # Force fresh diagnostic run
        _, diagnostic_result = await self.take_health_snapshot_with_diagnostics(force_diagnostic=True)

        if not diagnostic_result:
            return {
                "error": "Failed to run broker diagnostic",
                "recommendation": "Check adapter configuration and connectivity",
            }

        # Extract trade permission specific findings
        tests = diagnostic_result.get("tests", {})
        summary = diagnostic_result.get("summary", {})

        trade_permission_analysis = {
            "timestamp": diagnostic_result.get("timestamp"),
            "account_id": diagnostic_result.get("account_id"),
            "connectivity": tests.get("connectivity", {}).get("status"),
            "authentication": tests.get("authentication", {}).get("status"),
            "trading_permissions": tests.get("trading_permissions", {}),
            "account_status": tests.get("account_status", {}),
            "critical_issues": summary.get("critical_issues", []),
            "recommendations": summary.get("recommendations", []),
        }

        return trade_permission_analysis

    def get_cached_diagnostic(self) -> dict[str, Any] | None:
        """Return last cached diagnostic result if still valid."""
        if not self._should_refresh_diagnostic():
            return self.last_diagnostic_result
        return None


async def enhanced_health_check(adapter: MT5Adapter, include_diagnostic: bool = False) -> dict[str, Any]:
    """Convenience function for enhanced health check.

    Args:
        adapter: MT5Adapter instance
        include_diagnostic: Whether to include broker diagnostics

    Returns:
        Combined health and diagnostic results
    """
    monitor = EnhancedHealthMonitor(adapter)
    snapshot, diagnostic = await monitor.take_health_snapshot_with_diagnostics(force_diagnostic=include_diagnostic)

    return {
        "health_snapshot": {
            "adapter_health": {
                "state": snapshot.adapter_health.state.value if snapshot.adapter_health else "unknown",
                "message": snapshot.adapter_health.message if snapshot.adapter_health else "no data",
            },
            "account": {
                "balance": snapshot.account.balance if snapshot.account else 0,
                "equity": snapshot.account.equity if snapshot.account else 0,
            },
            "positions": {
                "count": snapshot.position_count or 0,
                "total_pnl": snapshot.total_unrealized_pnl or 0,
            },
            "error": snapshot.error,
        },
        "broker_diagnostic": diagnostic,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
