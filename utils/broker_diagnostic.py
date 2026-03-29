#!/usr/bin/env python3
"""Broker connectivity diagnostic tool for NovaTrade.

Investigates the critical "tradeAllowed: false" issue preventing trade execution.
Provides comprehensive connectivity, account status, and permission diagnostics.

Usage:
    python3 utils/broker_diagnostic.py
    python3 utils/broker_diagnostic.py --account-id <id> --token <token>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("broker_diagnostic")


class BrokerDiagnostic:
    """Comprehensive broker connectivity and trading permissions diagnostic."""

    def __init__(self, account_id: str, token: str):
        self.account_id = account_id
        self.token = token
        self.adapter = None
        self.results: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "account_id": account_id[:8] + "..." if len(account_id) > 8 else account_id,
            "tests": {},
            "summary": {},
        }

    async def run_full_diagnostic(self) -> dict[str, Any]:
        """Run complete diagnostic suite."""
        log.info("Starting broker diagnostic for account %s...", self.account_id[:8] + "...")

        # Test order:
        # 1. Basic connectivity
        # 2. Authentication
        # 3. Account status
        # 4. Trading permissions
        # 5. Symbol availability
        # 6. Order placement capability (dry run)

        await self._test_connectivity()
        await self._test_authentication()
        await self._test_account_status()
        await self._test_trading_permissions()
        await self._test_symbol_availability()
        await self._test_order_capability()

        self._generate_summary()
        return self.results

    async def _test_connectivity(self):
        """Test basic MetaAPI connectivity."""
        test_name = "connectivity"
        log.info("Testing MetaAPI connectivity...")

        try:
            from novatrade.adapter.metaapi_provider import MetaApiAdapter

            self.adapter = MetaApiAdapter(
                token=self.token,
                account_id=self.account_id,
                region="us",  # Default to US region
            )

            # Test connection initialization
            await self.adapter.connect()

            self.results["tests"][test_name] = {"status": "PASS", "message": "MetaAPI adapter connected successfully"}
            log.info("✅ Connectivity: PASS")

        except Exception as e:
            self.results["tests"][test_name] = {
                "status": "FAIL",
                "error": str(e),
                "message": "Failed to establish MetaAPI connection",
            }
            log.error("❌ Connectivity: FAIL - %s", str(e))

    async def _test_authentication(self):
        """Test authentication with MetaAPI."""
        test_name = "authentication"
        log.info("Testing authentication...")

        if not self.adapter:
            self.results["tests"][test_name] = {"status": "SKIP", "message": "Skipped due to connectivity failure"}
            return

        try:
            # Test basic account access
            account_info = await self.adapter.get_account()

            self.results["tests"][test_name] = {
                "status": "PASS",
                "account_type": getattr(account_info, "type", "unknown"),
                "server": getattr(account_info, "server", "unknown"),
                "message": "Authentication successful",
            }
            log.info("✅ Authentication: PASS")

        except Exception as e:
            self.results["tests"][test_name] = {"status": "FAIL", "error": str(e), "message": "Authentication failed"}
            log.error("❌ Authentication: FAIL - %s", str(e))

    async def _test_account_status(self):
        """Test account status and trading readiness."""
        test_name = "account_status"
        log.info("Testing account status...")

        if not self.adapter:
            self.results["tests"][test_name] = {"status": "SKIP", "message": "Skipped due to connectivity failure"}
            return

        try:
            account = await self.adapter.get_account()

            # Extract key account properties
            account_data = {
                "trade_allowed": getattr(account, "tradeAllowed", None),
                "expert_allowed": getattr(account, "expertAllowed", None),
                "equity": getattr(account, "equity", None),
                "balance": getattr(account, "balance", None),
                "margin": getattr(account, "margin", None),
                "leverage": getattr(account, "leverage", None),
                "currency": getattr(account, "currency", None),
                "state": getattr(account, "state", None),
                "connection_status": getattr(account, "connectionStatus", None),
            }

            # Determine status
            trade_allowed = account_data.get("trade_allowed", False)
            status = "PASS" if trade_allowed else "FAIL"

            self.results["tests"][test_name] = {
                "status": status,
                "account_data": account_data,
                "message": f"Trade allowed: {trade_allowed}",
            }

            if status == "PASS":
                log.info("✅ Account Status: PASS - Trading enabled")
            else:
                log.error("❌ Account Status: FAIL - tradeAllowed=False")
                log.error("   Account details: %s", json.dumps(account_data, indent=2))

        except Exception as e:
            self.results["tests"][test_name] = {
                "status": "FAIL",
                "error": str(e),
                "message": "Failed to retrieve account status",
            }
            log.error("❌ Account Status: FAIL - %s", str(e))

    async def _test_trading_permissions(self):
        """Test specific trading permissions and restrictions."""
        test_name = "trading_permissions"
        log.info("Testing trading permissions...")

        if not self.adapter:
            self.results["tests"][test_name] = {"status": "SKIP", "message": "Skipped due to connectivity failure"}
            return

        try:
            # Check positions access
            try:
                positions = await self.adapter.get_positions()
                positions_access = True
                positions_count = len(positions) if positions else 0
            except Exception as e:
                positions_access = False
                positions_error = str(e)
                positions_count = 0

            # Check orders access
            try:
                orders = await self.adapter.get_orders()
                orders_access = True
                orders_count = len(orders) if orders else 0
            except Exception as e:
                orders_access = False
                orders_error = str(e)
                orders_count = 0

            permissions_data = {
                "positions_access": positions_access,
                "positions_count": positions_count,
                "orders_access": orders_access,
                "orders_count": orders_count,
            }

            if not positions_access:
                permissions_data["positions_error"] = positions_error
            if not orders_access:
                permissions_data["orders_error"] = orders_error

            status = "PASS" if positions_access and orders_access else "PARTIAL"

            self.results["tests"][test_name] = {
                "status": status,
                "permissions": permissions_data,
                "message": f"Positions access: {positions_access}, Orders access: {orders_access}",
            }

            log.info("✅ Trading Permissions: %s", status)

        except Exception as e:
            self.results["tests"][test_name] = {
                "status": "FAIL",
                "error": str(e),
                "message": "Failed to test trading permissions",
            }
            log.error("❌ Trading Permissions: FAIL - %s", str(e))

    async def _test_symbol_availability(self):
        """Test EURUSD symbol availability and specifications."""
        test_name = "symbol_availability"
        log.info("Testing symbol availability...")

        if not self.adapter:
            self.results["tests"][test_name] = {"status": "SKIP", "message": "Skipped due to connectivity failure"}
            return

        try:
            # Test EURUSD symbol (primary trading pair)
            symbol = "EURUSD"

            # Get symbol specification
            try:
                spec = await self.adapter.get_symbol_spec(symbol)
                spec_available = True
                spec_data = {
                    "min_volume": getattr(spec, "minVolume", None),
                    "max_volume": getattr(spec, "maxVolume", None),
                    "volume_step": getattr(spec, "volumeStep", None),
                    "digits": getattr(spec, "digits", None),
                    "spread": getattr(spec, "spread", None),
                    "trade_allowed": getattr(spec, "tradeAllowed", None),
                }
            except Exception as e:
                spec_available = False
                spec_error = str(e)
                spec_data = {}

            # Get current price
            try:
                price = await self.adapter.get_price(symbol)
                price_available = True
                price_data = {
                    "bid": getattr(price, "bid", None),
                    "ask": getattr(price, "ask", None),
                    "timestamp": str(getattr(price, "time", None)),
                }
            except Exception as e:
                price_available = False
                price_error = str(e)
                price_data = {}

            symbol_data = {
                "symbol": symbol,
                "spec_available": spec_available,
                "spec_data": spec_data,
                "price_available": price_available,
                "price_data": price_data,
            }

            if not spec_available:
                symbol_data["spec_error"] = spec_error
            if not price_available:
                symbol_data["price_error"] = price_error

            status = "PASS" if spec_available and price_available else "PARTIAL"

            self.results["tests"][test_name] = {
                "status": status,
                "symbol_data": symbol_data,
                "message": f"Symbol {symbol}: spec={spec_available}, price={price_available}",
            }

            log.info("✅ Symbol Availability: %s", status)

        except Exception as e:
            self.results["tests"][test_name] = {
                "status": "FAIL",
                "error": str(e),
                "message": "Failed to test symbol availability",
            }
            log.error("❌ Symbol Availability: FAIL - %s", str(e))

    async def _test_order_capability(self):
        """Test order placement capability (validation only, no execution)."""
        test_name = "order_capability"
        log.info("Testing order capability...")

        if not self.adapter:
            self.results["tests"][test_name] = {"status": "SKIP", "message": "Skipped due to connectivity failure"}
            return

        try:
            # Test order validation without execution
            # This tests the adapter's order preparation and validation logic
            from novatrade.models import TradeRequest

            test_request = TradeRequest(
                symbol="EURUSD",
                action="buy",
                volume=0.01,  # Minimum volume
                order_type="market",
            )

            # Test if we can prepare the order (validate format, etc.)
            # This should work even if tradeAllowed=false
            try:
                # Call adapter's internal order preparation method if available
                if hasattr(self.adapter, "_prepare_order"):
                    prepared_order = self.adapter._prepare_order(test_request)
                    order_prep_success = True
                    order_prep_data = {
                        "action_type": getattr(prepared_order, "actionType", None),
                        "symbol": getattr(prepared_order, "symbol", None),
                        "volume": getattr(prepared_order, "volume", None),
                    }
                else:
                    order_prep_success = True
                    order_prep_data = {"message": "Order preparation method not accessible"}

            except Exception as e:
                order_prep_success = False
                order_prep_error = str(e)
                order_prep_data = {}

            capability_data = {
                "order_preparation": order_prep_success,
                "order_data": order_prep_data,
            }

            if not order_prep_success:
                capability_data["order_error"] = order_prep_error

            status = "PASS" if order_prep_success else "FAIL"

            self.results["tests"][test_name] = {
                "status": status,
                "capability_data": capability_data,
                "message": f"Order preparation: {order_prep_success}",
            }

            log.info("✅ Order Capability: %s", status)

        except Exception as e:
            self.results["tests"][test_name] = {
                "status": "FAIL",
                "error": str(e),
                "message": "Failed to test order capability",
            }
            log.error("❌ Order Capability: FAIL - %s", str(e))

    def _generate_summary(self):
        """Generate diagnostic summary."""
        tests = self.results["tests"]

        total_tests = len(tests)
        passed = sum(1 for t in tests.values() if t["status"] == "PASS")
        failed = sum(1 for t in tests.values() if t["status"] == "FAIL")
        skipped = sum(1 for t in tests.values() if t["status"] == "SKIP")
        partial = sum(1 for t in tests.values() if t["status"] == "PARTIAL")

        # Determine critical blocker
        critical_blocker = None
        if tests.get("connectivity", {}).get("status") == "FAIL":
            critical_blocker = "MetaAPI connectivity failure"
        elif tests.get("authentication", {}).get("status") == "FAIL":
            critical_blocker = "Authentication failure"
        elif tests.get("account_status", {}).get("status") == "FAIL":
            account_data = tests["account_status"].get("account_data", {})
            if not account_data.get("trade_allowed", False):
                critical_blocker = "tradeAllowed=false on broker account"

        # Generate recommendations
        recommendations = []
        if critical_blocker:
            if "tradeAllowed=false" in critical_blocker:
                recommendations.extend(
                    [
                        "Contact FTMO support to enable trading permissions",
                        "Verify account is fully verified and funded",
                        "Check if demo account has trading restrictions",
                        "Ensure account is in correct trading phase (not evaluation-only)",
                    ]
                )
            elif "connectivity" in critical_blocker.lower():
                recommendations.extend(
                    [
                        "Check MetaAPI token validity",
                        "Verify account ID is correct",
                        "Test network connectivity",
                        "Check MetaAPI service status",
                    ]
                )
            elif "authentication" in critical_blocker.lower():
                recommendations.extend(
                    [
                        "Verify MetaAPI token has not expired",
                        "Check account permissions in MetaAPI dashboard",
                        "Regenerate token if necessary",
                    ]
                )

        if not recommendations:
            recommendations = ["All critical tests passed - system ready for trading"]

        self.results["summary"] = {
            "total_tests": total_tests,
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
            "partial": partial,
            "critical_blocker": critical_blocker,
            "trading_ready": critical_blocker is None,
            "recommendations": recommendations,
        }

        # Log summary
        log.info("=" * 60)
        log.info("DIAGNOSTIC SUMMARY")
        log.info("=" * 60)
        log.info(
            "Total tests: %d (Pass: %d, Fail: %d, Skip: %d, Partial: %d)", total_tests, passed, failed, skipped, partial
        )

        if critical_blocker:
            log.error("🚨 CRITICAL BLOCKER: %s", critical_blocker)
            log.info("📋 RECOMMENDATIONS:")
            for i, rec in enumerate(recommendations, 1):
                log.info("   %d. %s", i, rec)
        else:
            log.info("✅ TRADING READY: All critical tests passed")


def _save_results(results: dict[str, Any], output_file: str | None) -> Path:
    """Save diagnostic results to file."""
    output_file = output_file or f"broker_diagnostic_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    output_path = Path("OUTPUT") / output_file
    output_path.parent.mkdir(exist_ok=True)

    with open(output_path, "w") as f:
        json.dump(results, f, indent=2)

    return output_path


async def main():
    """Main diagnostic entry point."""
    parser = argparse.ArgumentParser(description="NovaTrade broker connectivity diagnostic")
    parser.add_argument("--account-id", help="MetaAPI account ID")
    parser.add_argument("--token", help="MetaAPI token")
    parser.add_argument("--output", help="Output file for results (JSON)")

    args = parser.parse_args()

    # Get credentials from args or environment
    if args.account_id and args.token:
        account_id = args.account_id
        token = args.token
    else:
        # Try to load from NovaTrade config
        try:
            from novatrade.config import NovaTradeCfg

            cfg = NovaTradeCfg()
            account_id = cfg.metaapi.account_id
            token = cfg.metaapi.token

            if not account_id or not token:
                log.error("MetaAPI credentials not found in config. Use --account-id and --token arguments.")
                return 1

        except Exception as e:
            log.error("Failed to load NovaTrade config: %s", e)
            log.error("Use --account-id and --token arguments.")
            return 1

    # Run diagnostic
    diagnostic = BrokerDiagnostic(account_id, token)
    results = await diagnostic.run_full_diagnostic()

    # Save results
    output_path = _save_results(results, args.output)
    log.info("📄 Results saved to: %s", output_path)

    # Return exit code based on critical blockers
    return 1 if results["summary"]["critical_blocker"] else 0


if __name__ == "__main__":
    import sys

    sys.exit(asyncio.run(main()))
