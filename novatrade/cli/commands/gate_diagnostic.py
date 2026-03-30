"""Execution Gate Diagnostic for NovaTrade.

Provides gate-by-gate validation, configuration source tracing, and real-time
execution gate status to troubleshoot trade rejection issues.
"""

import asyncio
import json
import logging
from datetime import datetime
from typing import Any

from novatrade.adapter.metaapi_provider import MetaApiAdapter
from novatrade.config import NovaTradeCfg
from novatrade.models import AccountState, HealthStatus, OrderRequest, OrderSide, OrderType, Position, SymbolPrice
from novatrade.risk.pre_trade_gate import PreTradeGate

log = logging.getLogger("novatrade.cli.gate_diagnostic")


class ExecutionGateDiagnostic:
    """Comprehensive execution gate diagnostics and validation."""

    def __init__(self, config: NovaTradeCfg):
        self.config = config
        self.gate = PreTradeGate(config)
        self.adapter: MetaApiAdapter | None = None

    async def diagnose_all_gates(self, symbol: str = "EURUSD") -> dict[str, Any]:
        """Run comprehensive gate-by-gate diagnostic."""
        log.info("Running comprehensive execution gate diagnostic...")

        # Create test order request
        test_request = OrderRequest(
            symbol=symbol,
            side=OrderSide.BUY,
            volume=0.01,  # Minimal volume
            order_type=OrderType.MARKET,
            stop_loss=None,
            take_profit=None
        )

        # Get current state
        try:
            if not self.adapter:
                self.adapter = MetaApiAdapter(self.config.metaapi)
                await self.adapter.connect()

            account = await self.adapter.get_account_state()
            positions = await self.adapter.get_positions()
            health = await self.adapter.health_check()
            price = await self.adapter.get_symbol_price(symbol)

        except Exception as e:
            log.error(f"Error getting system state: {e}")
            # Use mock data for diagnostic
            account = self._create_mock_account_state()
            positions = []
            health = HealthStatus(
                connected=True,
                message="Mock health for diagnostic",
                last_tick_age=30.0
            )
            price = None

        # Run full gate evaluation
        decision = self.gate.evaluate(
            request=test_request,
            account=account,
            positions=positions,
            health=health,
            price=price
        )

        # Build detailed gate analysis
        gate_analysis = self._build_gate_analysis(decision, test_request, account, positions, health, price)

        # Add configuration source tracing
        config_trace = self._trace_configuration_sources()

        # Add real-time status
        runtime_status = await self._get_runtime_status()

        return {
            "timestamp": datetime.utcnow().isoformat(),
            "overall_decision": {
                "verdict": decision.verdict.value,
                "allowed": not decision.denied,
                "reason": decision.reason,
                "failed_gates": len([c for c in decision.checks if not c.passed])
            },
            "gate_analysis": gate_analysis,
            "configuration_trace": config_trace,
            "runtime_status": runtime_status,
            "test_request": {
                "symbol": test_request.symbol,
                "side": test_request.side.value,
                "volume": test_request.volume,
                "type": test_request.order_type.value
            },
            "system_state": {
                "account_balance": account.balance if account else None,
                "account_equity": account.equity if account else None,
                "account_mode": account.mode.value if account else None,
                "open_positions": len(positions),
                "health_connected": health.connected if health else False,
                "price_available": price is not None
            }
        }

    def _build_gate_analysis(
        self,
        decision,
        request: OrderRequest,
        account: AccountState,
        positions: list[Position],
        health: HealthStatus,
        price: SymbolPrice
    ) -> dict[str, dict[str, Any]]:
        """Build detailed analysis for each gate."""

        gate_map = {
            "kill_switch": "Kill Switch Status",
            "dry_run": "Dry Run Mode",
            "account_mode": "Account Mode Validation",
            "trade_allowed": "Trade Allowed Status",
            "health": "System Health Check",
            "feed_health": "Price Feed Health",
            "symbol": "Symbol Validation",
            "volume": "Volume Validation",
            "lot_size": "Lot Size Consistency",
            "request_count": "Request Rate Limiting",
            "stop_loss": "Stop Loss Validation",
            "sl_distance": "Stop Loss Distance",
            "volume_sizing": "Volume Sizing Rules",
            "max_positions": "Maximum Positions",
            "daily_count": "Daily Trade Count",
            "cooldown": "Trade Cooldown",
            "duplicate": "Duplicate Position Check",
            "drawdown": "Drawdown Protection",
            "daily_loss": "FTMO Daily Loss",
            "spread": "Spread Validation",
            "weekend": "Weekend Trading Block",
            "news": "News Calendar Block",
            "gap_spread": "Gap Trading Prevention",
            "rollover": "Rollover Dead Zone",
            "london_fix": "London Fix Block",
            "trading_days": "FTMO Trading Days"
        }

        analysis = {}

        for check in decision.checks:
            gate_name = check.rule.lower().replace(" ", "_").replace("-", "_")

            # Map specific check names to our gate categories
            if "kill" in check.rule.lower():
                gate_key = "kill_switch"
            elif "dry" in check.rule.lower() and "run" in check.rule.lower():
                gate_key = "dry_run"
            elif "account" in check.rule.lower() and "mode" in check.rule.lower():
                gate_key = "account_mode"
            elif "trade" in check.rule.lower() and "allowed" in check.rule.lower():
                gate_key = "trade_allowed"
            elif check.rule.lower() == "health":
                gate_key = "health"
            elif "feed" in check.rule.lower():
                gate_key = "feed_health"
            elif check.rule.lower() == "symbol":
                gate_key = "symbol"
            elif check.rule.lower() == "volume" and "sizing" not in check.rule.lower():
                gate_key = "volume"
            elif "lot" in check.rule.lower():
                gate_key = "lot_size"
            elif "request" in check.rule.lower():
                gate_key = "request_count"
            elif "stop" in check.rule.lower() and "distance" not in check.rule.lower():
                gate_key = "stop_loss"
            elif "distance" in check.rule.lower():
                gate_key = "sl_distance"
            elif "sizing" in check.rule.lower():
                gate_key = "volume_sizing"
            elif "max" in check.rule.lower() and "position" in check.rule.lower():
                gate_key = "max_positions"
            elif "daily" in check.rule.lower() and "count" in check.rule.lower():
                gate_key = "daily_count"
            elif "cooldown" in check.rule.lower():
                gate_key = "cooldown"
            elif "duplicate" in check.rule.lower():
                gate_key = "duplicate"
            elif "drawdown" in check.rule.lower():
                gate_key = "drawdown"
            elif "daily" in check.rule.lower() and "loss" in check.rule.lower():
                gate_key = "daily_loss"
            elif "spread" in check.rule.lower() and "gap" not in check.rule.lower():
                gate_key = "spread"
            elif "weekend" in check.rule.lower():
                gate_key = "weekend"
            elif "news" in check.rule.lower():
                gate_key = "news"
            elif "gap" in check.rule.lower():
                gate_key = "gap_spread"
            elif "rollover" in check.rule.lower():
                gate_key = "rollover"
            elif "london" in check.rule.lower():
                gate_key = "london_fix"
            elif "days" in check.rule.lower():
                gate_key = "trading_days"
            else:
                gate_key = gate_name

            analysis[gate_key] = {
                "name": gate_map.get(gate_key, check.rule),
                "rule": check.rule,
                "passed": check.passed,
                "status": "PASS" if check.passed else "FAIL",
                "reason": check.reason if hasattr(check, 'reason') else None,
                "impact": "BLOCKING" if not check.passed else "NONE",
                "configuration_source": self._get_gate_config_source(gate_key),
                "current_value": self._get_gate_current_value(gate_key, account, positions, health, price),
                "threshold": self._get_gate_threshold(gate_key),
                "remediation": self._get_gate_remediation(gate_key, check.passed)
            }

        return analysis

    def _get_gate_config_source(self, gate_key: str) -> str:
        """Get configuration source for a specific gate."""
        source_map = {
            "dry_run": f"config.dry_run = {self.config.dry_run}",
            "account_mode": f"config.metaapi.account_mode = {self.config.metaapi.account_id[-4:]}",
            "max_positions": f"config.risk.max_positions = {self.config.risk.max_positions}",
            "daily_loss": f"config.ftmo.max_daily_loss_pct = {self.config.ftmo.max_daily_loss_pct}%",
            "volume_sizing": f"config.risk.max_position_size_pct = {self.config.risk.max_position_size_pct}%",
            "drawdown": f"config.risk.max_daily_drawdown_pct = {self.config.risk.max_daily_drawdown_pct}%",
            "cooldown": f"config.risk.cooldown_seconds = {self.config.risk.cooldown_seconds}s",
            "daily_count": f"config.risk.max_daily_trades = {self.config.risk.max_daily_trades}",
        }
        return source_map.get(gate_key, "config.* (check gate source)")

    def _get_gate_current_value(
        self,
        gate_key: str,
        account: AccountState,
        positions: list[Position],
        health: HealthStatus,
        price: SymbolPrice
    ) -> Any:
        """Get current value for gate evaluation."""
        value_map = {
            "dry_run": self.config.dry_run,
            "account_mode": account.mode.value if account else "unknown",
            "max_positions": len(positions),
            "health": health.connected if health else False,
            "feed_health": f"{health.last_tick_age:.1f}s ago" if health else "unknown",
            "trade_allowed": account.trade_allowed if account and hasattr(account, 'trade_allowed') else "unknown",
            "drawdown": f"{account.equity/account.balance*100:.1f}%" if account and account.balance > 0 else "unknown",
        }
        return value_map.get(gate_key, "dynamic")

    def _get_gate_threshold(self, gate_key: str) -> str:
        """Get threshold/limit for gate."""
        threshold_map = {
            "dry_run": "false (to allow trades)",
            "account_mode": "DEMO (MVP restriction)",
            "max_positions": f"<= {self.config.risk.max_positions}",
            "health": "connected = true",
            "feed_health": "< 300s staleness",
            "trade_allowed": "true",
            "daily_loss": f"< {self.config.ftmo.max_daily_loss_pct}%",
            "drawdown": f"< {self.config.risk.max_daily_drawdown_pct}%",
            "cooldown": f">= {self.config.risk.cooldown_seconds}s between trades",
            "daily_count": f"< {self.config.risk.max_daily_trades} per day",
        }
        return threshold_map.get(gate_key, "see rule")

    def _get_gate_remediation(self, gate_key: str, passed: bool) -> str:
        """Get remediation advice for failed gates."""
        if passed:
            return "None required"

        remediation_map = {
            "dry_run": "Set NOVATRADE_DRY_RUN=false in environment or config",
            "account_mode": "Switch to DEMO account or remove MVP restriction",
            "max_positions": "Close some positions or increase max_positions limit",
            "health": "Check broker connectivity and system health",
            "feed_health": "Restart service to refresh price feeds",
            "trade_allowed": "Check broker account permissions and margin requirements",
            "daily_loss": "Stop trading for today - daily loss limit exceeded",
            "drawdown": "Stop trading - drawdown protection triggered",
            "cooldown": "Wait for cooldown period to expire",
            "daily_count": "Reached daily trade limit - wait until tomorrow",
        }
        return remediation_map.get(gate_key, "Check configuration and system state")

    def _trace_configuration_sources(self) -> dict[str, Any]:
        """Trace configuration loading sources."""
        return {
            "config_file_sources": {
                "primary": "/etc/novacore/novatrade.env",
                "override": "configs/novatrade.override.env"
            },
            "key_settings": {
                "dry_run": {
                    "value": self.config.dry_run,
                    "source": "NovaTradeCfg.load()",
                    "env_var": "NOVATRADE_DRY_RUN"
                },
                "launch_mode": {
                    "value": getattr(self.config, 'launch_mode', 'unknown'),
                    "source": "NovaTradeCfg.load()",
                    "env_var": "NOVATRADE_LAUNCH_MODE"
                },
                "max_positions": {
                    "value": self.config.risk.max_positions,
                    "source": "config.risk.*",
                    "env_var": "NOVATRADE_MAX_POSITIONS"
                }
            },
            "account_configuration": {
                "metaapi_token_set": bool(self.config.metaapi.token),
                "account_id_set": bool(self.config.metaapi.account_id),
                "ftmo_enabled": self.config.ftmo.enabled,
                "symbols": self.config.symbols
            }
        }

    async def _get_runtime_status(self) -> dict[str, Any]:
        """Get real-time runtime status."""
        try:
            import requests
            response = requests.get('http://localhost:8877/status', timeout=5)
            if response.status_code == 200:
                status = response.json()
                return {
                    "service_responsive": True,
                    "pipeline_state": status.get('pipeline', 'unknown'),
                    "feed_health": status.get('feed_health', {}),
                    "uptime": status.get('uptime', 0),
                    "last_activity": status.get('last_activity', 'unknown')
                }
            else:
                return {
                    "service_responsive": False,
                    "error": f"HTTP {response.status_code}"
                }
        except Exception as e:
            return {
                "service_responsive": False,
                "error": str(e)
            }

    def _create_mock_account_state(self) -> AccountState:
        """Create mock account state for diagnostic when adapter unavailable."""
        from novatrade.models import AccountMode
        return AccountState(
            balance=100000.0,
            equity=100000.0,
            margin=0.0,
            free_margin=100000.0,
            mode=AccountMode.DEMO,
            trade_allowed=True,
            currency="USD"
        )

    async def generate_gate_status_endpoint_response(self) -> dict[str, Any]:
        """Generate response suitable for real-time HTTP endpoint."""
        try:
            diagnostic = await self.diagnose_all_gates()

            # Extract key status for real-time monitoring
            failed_gates = [
                {
                    "gate": name,
                    "rule": info["rule"],
                    "reason": info.get("reason", "Failed"),
                    "remediation": info["remediation"]
                }
                for name, info in diagnostic["gate_analysis"].items()
                if info["status"] == "FAIL"
            ]

            return {
                "timestamp": diagnostic["timestamp"],
                "overall_status": "BLOCKED" if failed_gates else "READY",
                "execution_allowed": diagnostic["overall_decision"]["allowed"],
                "failed_gates_count": len(failed_gates),
                "failed_gates": failed_gates,
                "service_health": diagnostic["runtime_status"]["service_responsive"],
                "feed_health": diagnostic["runtime_status"]["feed_health"],
                "quick_summary": {
                    "dry_run": diagnostic["gate_analysis"].get("dry_run", {}).get("passed", True),
                    "account_mode": diagnostic["gate_analysis"].get("account_mode", {}).get("passed", True),
                    "health": diagnostic["gate_analysis"].get("health", {}).get("passed", True),
                    "feed_health": diagnostic["gate_analysis"].get("feed_health", {}).get("passed", True),
                    "positions": diagnostic["system_state"]["open_positions"]
                }
            }
        except Exception as e:
            log.error(f"Error generating gate status: {e}")
            return {
                "timestamp": datetime.utcnow().isoformat(),
                "overall_status": "ERROR",
                "error": str(e),
                "execution_allowed": False,
                "failed_gates_count": 0,
                "failed_gates": [],
                "service_health": False,
                "feed_health": {},
                "quick_summary": {}
            }


async def main():
    """CLI entry point for gate diagnostics."""
    import argparse

    parser = argparse.ArgumentParser(description="NovaTrade Execution Gate Diagnostic")
    parser.add_argument('--symbol', default='EURUSD', help='Symbol to test (default: EURUSD)')
    parser.add_argument('--json', action='store_true', help='Output JSON format')
    parser.add_argument('--status-only', action='store_true', help='Show only gate status summary')

    args = parser.parse_args()

    try:
        config = NovaTradeCfg.load()
        diagnostic = ExecutionGateDiagnostic(config)

        if args.status_only:
            result = await diagnostic.generate_gate_status_endpoint_response()
        else:
            result = await diagnostic.diagnose_all_gates(args.symbol)

        if args.json:
            print(json.dumps(result, indent=2, default=str))
        else:
            # Human-readable format
            print("\n" + "="*60)
            print("NovaTrade Execution Gate Diagnostic")
            print("="*60)

            if args.status_only:
                print(f"\n⚡ Overall Status: {result['overall_status']}")
                print(f"🎯 Execution Allowed: {result['execution_allowed']}")
                print(f"❌ Failed Gates: {result['failed_gates_count']}")

                if result['failed_gates']:
                    print("\n📋 BLOCKING ISSUES:")
                    for i, gate in enumerate(result['failed_gates'], 1):
                        print(f"  {i}. {gate['gate']}: {gate['reason']}")
                        print(f"     💡 Fix: {gate['remediation']}")
            else:
                print(f"\n⚡ Overall: {result['overall_decision']['verdict']}")
                print(f"🎯 Execution: {'ALLOWED' if result['overall_decision']['allowed'] else 'BLOCKED'}")
                print(f"❌ Failed Gates: {result['overall_decision']['failed_gates']}")

                print("\n📊 GATE STATUS:")
                for _name, info in result['gate_analysis'].items():
                    status_icon = "✅" if info['passed'] else "❌"
                    print(f"  {status_icon} {info['name']}: {info['status']}")
                    if not info['passed']:
                        print(f"     💡 {info['remediation']}")

        return 0

    except Exception as e:
        log.error(f"Diagnostic failed: {e}")
        if args.json:
            print(json.dumps({"error": str(e)}, indent=2))
        else:
            print(f"❌ Diagnostic failed: {e}")
        return 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    exit(asyncio.run(main()))
