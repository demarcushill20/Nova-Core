#!/usr/bin/env python3
"""MetaAPI Connection Recovery Tool.

Attempts to diagnose and fix MetaAPI connection issues without full service restart.
Integrates with the rate limiting guardian and provides detailed diagnostics.
"""

import asyncio
import logging
import sys
import time
from pathlib import Path
from typing import Any

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from novatrade.adapter.metaapi_provider import MetaApiAdapter
from novatrade.adapter.rate_limiting_integration import enhance_adapter_with_rate_limiting
from novatrade.config import NovaTradeCfg
from novatrade.monitor.rate_limit_guardian import RateLimitGuardian, get_global_guardian

log = logging.getLogger("scripts.fix_metaapi_connection")


class MetaAPIConnectionRecovery:
    """MetaAPI connection recovery and diagnostic tool."""

    def __init__(self, config: NovaTradeCfg):
        self.config = config
        self.rate_guardian: RateLimitGuardian = get_global_guardian()
        self.adapter: MetaApiAdapter = None

    async def diagnose_connection_issues(self) -> dict[str, Any]:
        """Comprehensive MetaAPI connection diagnostics."""
        log.info("Running MetaAPI connection diagnostics...")

        diagnostics = {
            "timestamp": time.time(),
            "config_validation": {},
            "rate_limiting_status": {},
            "connection_test": {},
            "subscription_status": {},
            "recommendations": [],
        }

        # 1. Config validation
        try:
            validation_errors = self.config.metaapi.validate()
            diagnostics["config_validation"] = {
                "valid": len(validation_errors) == 0,
                "errors": validation_errors,
                "account_id": self.config.metaapi.account_id[:8] + "..." if self.config.metaapi.account_id else None,
                "region": self.config.metaapi.region,
                "token_set": bool(self.config.metaapi.token),
            }
        except Exception as e:
            diagnostics["config_validation"] = {"error": str(e)}

        # 2. Rate limiting status
        try:
            diagnostics["rate_limiting_status"] = self.rate_guardian.generate_health_report()
        except Exception as e:
            diagnostics["rate_limiting_status"] = {"error": str(e)}

        # 3. Connection test
        try:
            adapter = MetaApiAdapter(self.config.metaapi)
            enhance_adapter_with_rate_limiting(adapter)  # Add rate limiting protection

            # Test basic connection
            start_time = time.time()
            status = await adapter.connect()
            connect_time = time.time() - start_time

            diagnostics["connection_test"] = {
                "connected": status.connected,
                "message": status.message,
                "connect_time_seconds": round(connect_time, 2),
                "adapter_connected": getattr(adapter, "_connected", False),
            }

            if status.connected:
                # Test account access
                try:
                    account = await adapter.get_account_state()
                    diagnostics["connection_test"]["account_accessible"] = True
                    diagnostics["connection_test"]["account_balance"] = account.balance
                    diagnostics["connection_test"]["account_mode"] = account.mode.value
                except Exception as e:
                    diagnostics["connection_test"]["account_accessible"] = False
                    diagnostics["connection_test"]["account_error"] = str(e)

                # Test price data access
                try:
                    price = await adapter.get_symbol_price("EURUSD")
                    diagnostics["connection_test"]["price_data_accessible"] = True
                    diagnostics["connection_test"]["price_age_seconds"] = (
                        time.time() - price.timestamp if price else None
                    )
                except Exception as e:
                    diagnostics["connection_test"]["price_data_accessible"] = False
                    diagnostics["connection_test"]["price_error"] = str(e)

            self.adapter = adapter

        except Exception as e:
            diagnostics["connection_test"] = {"error": str(e)}

        # 4. Generate recommendations
        recommendations = self._generate_recommendations(diagnostics)
        diagnostics["recommendations"] = recommendations

        return diagnostics

    def _generate_recommendations(self, diagnostics: dict[str, Any]) -> list[str]:
        """Generate actionable recommendations based on diagnostics."""
        recommendations = []

        # Config issues
        config_validation = diagnostics.get("config_validation", {})
        if not config_validation.get("valid", False):
            recommendations.append("Fix MetaAPI configuration errors before proceeding")
            for error in config_validation.get("errors", []):
                recommendations.append(f"  • {error}")

        # Rate limiting issues
        rate_status = diagnostics.get("rate_limiting_status", {})
        if isinstance(rate_status, dict):
            connection_metrics = rate_status.get("connection_metrics", {})
            failures_24h = connection_metrics.get("failures_24h", 0)

            if failures_24h > 20:
                recommendations.append("High connection failure rate detected - rate limiting may be active")
                recommendations.append("Consider waiting before retry attempts")

            cooldown_status = rate_status.get("cooldown_status", {})
            if cooldown_status.get("active", False):
                remaining = cooldown_status.get("remaining_seconds", 0)
                recommendations.append(f"Rate limit cooldown active - wait {remaining:.0f}s before next attempt")

        # Connection issues
        connection_test = diagnostics.get("connection_test", {})
        if not connection_test.get("connected", False):
            recommendations.append("Connection failed - check network and credentials")
            if "429" in str(connection_test.get("message", "")):
                recommendations.append("HTTP 429 detected - MetaAPI rate limiting active")
                recommendations.append("Service restart may be required to reset subscriptions")

        if not connection_test.get("account_accessible", True):
            recommendations.append("Account not accessible - check MetaAPI permissions")

        if not connection_test.get("price_data_accessible", True):
            recommendations.append("Price data not accessible - check symbol subscriptions")

        # Feed health issues
        price_age = connection_test.get("price_age_seconds")
        if price_age and price_age > 300:
            recommendations.append(f"Price data is stale ({price_age:.0f}s old) - resubscription needed")

        return recommendations

    async def attempt_recovery(self) -> tuple[bool, list[str]]:
        """Attempt to recover MetaAPI connection."""
        log.info("Attempting MetaAPI connection recovery...")

        recovery_actions = []
        success = False

        try:
            # Step 1: Check if we should attempt connection
            can_connect, reason = self.rate_guardian.should_attempt_connection()
            if not can_connect:
                recovery_actions.append(f"Connection blocked by rate guardian: {reason}")
                return False, recovery_actions

            # Step 2: Create enhanced adapter
            if not self.adapter:
                self.adapter = MetaApiAdapter(self.config.metaapi)
                enhance_adapter_with_rate_limiting(self.adapter)
                recovery_actions.append("Created rate-limiting enhanced adapter")

            # Step 3: Attempt connection with rate limiting protection
            log.info("Attempting connection with rate limiting protection...")
            status = await self.adapter.connect()

            if status.connected:
                self.rate_guardian.record_connection_success()
                recovery_actions.append("Connection successful")

                # Step 4: Test subscriptions
                try:
                    await self.adapter.subscribe_to_market_data(["EURUSD"])
                    recovery_actions.append("Market data subscription successful")

                    # Step 5: Verify price data
                    price = await self.adapter.get_symbol_price("EURUSD")
                    if price:
                        age = time.time() - price.timestamp
                        recovery_actions.append(f"Price data retrieved (age: {age:.1f}s)")
                        success = age < 60  # Consider successful if price is < 60s old
                    else:
                        recovery_actions.append("Warning: No price data available")

                except Exception as e:
                    self.rate_guardian.record_connection_failure()
                    recovery_actions.append(f"Subscription failed: {e}")

            else:
                self.rate_guardian.record_connection_failure()
                recovery_actions.append(f"Connection failed: {status.message}")

        except Exception as e:
            self.rate_guardian.record_connection_failure()
            recovery_actions.append(f"Recovery attempt failed: {e}")

        return success, recovery_actions

    async def reset_rate_limiting(self) -> list[str]:
        """Reset rate limiting counters and cooldowns."""
        log.info("Resetting rate limiting state...")

        actions = []
        try:
            # Reset connection metrics
            self.rate_guardian.reset_connection_metrics()
            actions.append("Reset connection failure counters")

            # Clear cooldown state
            self.rate_guardian.clear_cooldown()
            actions.append("Cleared rate limiting cooldowns")

            actions.append("Rate limiting state reset - retry connection now available")

        except Exception as e:
            actions.append(f"Error resetting rate limiting: {e}")

        return actions

    async def force_reconnection(self) -> tuple[bool, list[str]]:
        """Force a clean reconnection sequence."""
        log.info("Forcing clean reconnection...")

        actions = []
        success = False

        try:
            # Step 1: Clean disconnect if connected
            if self.adapter and hasattr(self.adapter, "_connection"):
                try:
                    await self.adapter._connection.close()
                    actions.append("Closed existing connection")
                except Exception as e:
                    actions.append(f"Error closing connection: {e}")

            # Step 2: Reset rate limiting
            reset_actions = await self.reset_rate_limiting()
            actions.extend(reset_actions)

            # Step 3: Create fresh adapter
            self.adapter = MetaApiAdapter(self.config.metaapi)
            enhance_adapter_with_rate_limiting(self.adapter)
            actions.append("Created fresh enhanced adapter")

            # Step 4: Attempt recovery
            recovery_success, recovery_actions = await self.attempt_recovery()
            actions.extend(recovery_actions)
            success = recovery_success

        except Exception as e:
            actions.append(f"Force reconnection failed: {e}")

        return success, actions


async def main():
    """CLI entry point for MetaAPI connection recovery."""
    import argparse

    parser = argparse.ArgumentParser(description="MetaAPI Connection Recovery Tool")
    parser.add_argument("--diagnose", action="store_true", help="Run diagnostics only")
    parser.add_argument("--recover", action="store_true", help="Attempt connection recovery")
    parser.add_argument("--force", action="store_true", help="Force clean reconnection")
    parser.add_argument("--reset-rate-limits", action="store_true", help="Reset rate limiting state")
    parser.add_argument("--json", action="store_true", help="Output JSON format")

    args = parser.parse_args()

    # Set default action
    if not any([args.diagnose, args.recover, args.force, args.reset_rate_limits]):
        args.diagnose = True

    try:
        config = NovaTradeCfg.load()
        recovery_tool = MetaAPIConnectionRecovery(config)

        if args.diagnose:
            diagnostics = await recovery_tool.diagnose_connection_issues()

            if args.json:
                import json

                print(json.dumps(diagnostics, indent=2, default=str))
            else:
                print("\n" + "=" * 60)
                print("MetaAPI Connection Diagnostics")
                print("=" * 60)

                # Config validation
                config_val = diagnostics["config_validation"]
                config_icon = "✅" if config_val.get("valid", False) else "❌"
                print(f"\n{config_icon} Configuration: {'Valid' if config_val.get('valid', False) else 'Invalid'}")
                if config_val.get("errors"):
                    for error in config_val["errors"]:
                        print(f"    • {error}")

                # Connection test
                conn_test = diagnostics["connection_test"]
                if "error" not in conn_test:
                    conn_icon = "✅" if conn_test.get("connected", False) else "❌"
                    print(f"\n{conn_icon} Connection: {'Connected' if conn_test.get('connected', False) else 'Failed'}")
                    if not conn_test.get("connected", False):
                        print(f"    Message: {conn_test.get('message', 'Unknown error')}")

                    if conn_test.get("account_accessible") is not None:
                        acc_icon = "✅" if conn_test["account_accessible"] else "❌"
                        print(f"{acc_icon} Account Access: {'OK' if conn_test['account_accessible'] else 'Failed'}")

                    if conn_test.get("price_data_accessible") is not None:
                        price_icon = "✅" if conn_test["price_data_accessible"] else "❌"
                        print(f"{price_icon} Price Data: {'OK' if conn_test['price_data_accessible'] else 'Failed'}")

                        price_age = conn_test.get("price_age_seconds")
                        if price_age is not None:
                            print(f"    Price age: {price_age:.1f}s")

                # Recommendations
                recommendations = diagnostics["recommendations"]
                if recommendations:
                    print("\n💡 RECOMMENDATIONS:")
                    for i, rec in enumerate(recommendations, 1):
                        print(f"  {i}. {rec}")

        if args.reset_rate_limits:
            actions = await recovery_tool.reset_rate_limiting()
            print("\n🔄 Rate Limiting Reset:")
            for action in actions:
                print(f"  • {action}")

        if args.recover:
            success, actions = await recovery_tool.attempt_recovery()
            print(f"\n🔧 Connection Recovery: {'SUCCESS' if success else 'FAILED'}")
            for action in actions:
                print(f"  • {action}")

        if args.force:
            success, actions = await recovery_tool.force_reconnection()
            print(f"\n⚡ Force Reconnection: {'SUCCESS' if success else 'FAILED'}")
            for action in actions:
                print(f"  • {action}")

        return 0 if (not args.recover and not args.force) or success else 1

    except Exception as e:
        if args.json:
            import json

            print(json.dumps({"error": str(e)}, indent=2))
        else:
            print(f"❌ Error: {e}")
        return 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    exit(asyncio.run(main()))
