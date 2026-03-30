"""Health diagnostic command for NovaTrade CLI.

Provides comprehensive broker connectivity and trading permission diagnostics
to investigate issues like 'tradeAllowed: false' blocking trade execution.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path

import typer
from rich import print as rprint
from rich.console import Console

from novatrade.adapter.metaapi_provider import MetaApiAdapter
from novatrade.config import NovaTradeCfg
from novatrade.monitor.enhanced_health import EnhancedHealthMonitor

log = logging.getLogger("novatrade.cli.health_diagnostic")
console = Console()


def health_diagnostic(
    config: Path = typer.Option(  # noqa: B008
        None,
        "--config",
        "-c",
        help="Path to config file (default: novatrade_config.toml)",
    ),
    full_diagnostic: bool = typer.Option(
        False,
        "--full-diagnostic",
        help="Run comprehensive broker diagnostic (may take 30-60 seconds)",
    ),
    trade_permission_focus: bool = typer.Option(
        False,
        "--trade-permission-focus",
        help="Focus on trade permission issues like 'tradeAllowed: false'",
    ),
    output: Path = typer.Option(  # noqa: B008
        None,
        "--output",
        "-o",
        help="Save results to file (JSON format)",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Enable verbose logging",
    ),
) -> None:
    """Run comprehensive health diagnostics for NovaTrade.

    This command performs deep connectivity and trading permission analysis
    to investigate issues preventing trade execution.

    Examples:
        # Quick health check
        novatrade health-diagnostic

        # Full diagnostic with trade permission focus
        novatrade health-diagnostic --full-diagnostic --trade-permission-focus

        # Save results to file
        novatrade health-diagnostic --full-diagnostic -o diagnostic_results.json
    """
    if verbose:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)

    try:
        asyncio.run(_run_diagnostic(config, full_diagnostic, trade_permission_focus, output))
    except KeyboardInterrupt:
        rprint("\n[yellow]Diagnostic cancelled by user[/yellow]")
        raise typer.Exit(1) from None
    except Exception as exc:
        rprint(f"[red]Diagnostic failed: {exc}[/red]")
        raise typer.Exit(1) from exc


async def _run_diagnostic(
    config_path: Path | None,
    full_diagnostic: bool,
    trade_permission_focus: bool,
    output_path: Path | None,
) -> None:
    """Run the diagnostic workflow."""
    try:
        # Load configuration
        if config_path:
            cfg = NovaTradeCfg.load(config_path)
        else:
            cfg = NovaTradeCfg.load()

        log.info("Loaded configuration for account %s", cfg.metaapi.account_id[:8] + "...")

        # Create adapter
        adapter = MetaApiAdapter(cfg.metaapi)

        # Initialize enhanced health monitor
        monitor = EnhancedHealthMonitor(adapter)

        rprint("🔍 Starting NovaTrade Health Diagnostic...")
        rprint()

        # Run specific diagnostic based on flags
        if trade_permission_focus:
            rprint("🎯 Focusing on trade permission issues...")
            results = await monitor.diagnose_trade_permission_issue()

            # Display trade permission specific results
            _display_trade_permission_results(results)

        elif full_diagnostic:
            rprint("🔬 Running comprehensive broker diagnostic...")
            snapshot, diagnostic = await monitor.take_health_snapshot_with_diagnostics(force_diagnostic=True)

            results = {
                "health_snapshot": _format_health_snapshot(snapshot),
                "broker_diagnostic": diagnostic,
                "timestamp": datetime.utcnow().isoformat(),
            }

            # Display results
            _display_health_results(snapshot)
            if diagnostic:
                _display_diagnostic_results(diagnostic)

        else:
            rprint("📊 Running standard health check...")
            snapshot, cached_diagnostic = await monitor.take_health_snapshot_with_diagnostics()

            results = {
                "health_snapshot": _format_health_snapshot(snapshot),
                "cached_diagnostic": cached_diagnostic,
                "timestamp": datetime.utcnow().isoformat(),
            }

            _display_health_results(snapshot)
            if cached_diagnostic:
                rprint("\n💾 Using cached diagnostic results:")
                _display_diagnostic_results(cached_diagnostic)

        # Save to file if requested
        if output_path:
            with output_path.open("w") as f:
                json.dump(results, f, indent=2, default=str)
            rprint(f"\n💾 Results saved to {output_path}")

        # Cleanup
        await adapter.close()  # type: ignore[attr-defined]

    except Exception as exc:
        log.error("Health diagnostic failed: %s", exc, exc_info=True)
        rprint(f"❌ Diagnostic failed: {exc}")
        raise typer.Exit(1) from exc


def _format_health_snapshot(snapshot) -> dict:
    """Format health snapshot for JSON serialization."""
    return {
        "adapter_health": {
            "state": snapshot.adapter_health.state.value if snapshot.adapter_health else None,
            "message": snapshot.adapter_health.message if snapshot.adapter_health else None,
        },
        "account": {
            "balance": snapshot.account.balance if snapshot.account else None,
            "equity": snapshot.account.equity if snapshot.account else None,
        },
        "position_count": snapshot.position_count,
        "total_unrealized_pnl": snapshot.total_unrealized_pnl,
        "error": snapshot.error,
    }


def _display_health_results(snapshot) -> None:
    """Display health snapshot results."""
    rprint("📋 Health Snapshot Results:")

    if snapshot.adapter_health:
        state_color = "green" if snapshot.adapter_health.state.value == "UP" else "red"
        adapter_text = f"[{state_color}]{snapshot.adapter_health.state.value}[/{state_color}]"
        rprint(f"  • Adapter: {adapter_text}")
        if snapshot.adapter_health.message:
            rprint(f"    Message: {snapshot.adapter_health.message}")

    if snapshot.account:
        rprint(f"  • Balance: ${snapshot.account.balance:,.2f}")
        rprint(f"  • Equity: ${snapshot.account.equity:,.2f}")

    if snapshot.position_count is not None:
        rprint(f"  • Open Positions: {snapshot.position_count}")
        if snapshot.total_unrealized_pnl:
            pnl_color = "green" if snapshot.total_unrealized_pnl >= 0 else "red"
            pnl_text = f"[{pnl_color}]${snapshot.total_unrealized_pnl:.2f}[/{pnl_color}]"
            rprint(f"  • Total P&L: {pnl_text}")

    if snapshot.error:
        rprint(f"  • [red]Error:[/red] {snapshot.error}")


def _display_diagnostic_results(diagnostic: dict) -> None:
    """Display broker diagnostic results."""
    rprint("\n🔬 Broker Diagnostic Results:")

    tests = diagnostic.get("tests", {})
    summary = diagnostic.get("summary", {})

    # Show test results
    for test_name, test_result in tests.items():
        status = test_result.get("status", "unknown")
        status_color = "green" if status == "pass" else "red" if status == "fail" else "yellow"
        status_text = f"[{status_color}]{status}[/{status_color}]"
        rprint(f"  • {test_name.replace('_', ' ').title()}: {status_text}")

        if test_result.get("message"):
            rprint(f"    {test_result['message']}")

    # Show critical issues
    critical_issues = summary.get("critical_issues", [])
    if critical_issues:
        rprint("\n🚨 [red]Critical Issues:[/red]")
        for issue in critical_issues:
            rprint(f"  • {issue}")

    # Show recommendations
    recommendations = summary.get("recommendations", [])
    if recommendations:
        rprint("\n💡 [yellow]Recommendations:[/yellow]")
        for rec in recommendations:
            rprint(f"  • {rec}")


def _display_trade_permission_results(results: dict) -> None:
    """Display trade permission focused results."""
    rprint("🎯 Trade Permission Analysis:")

    connectivity = results.get("connectivity")
    if connectivity:
        conn_color = "green" if connectivity == "pass" else "red"
        conn_text = f"[{conn_color}]{connectivity}[/{conn_color}]"
        rprint(f"  • Connectivity: {conn_text}")

    auth = results.get("authentication")
    if auth:
        auth_color = "green" if auth == "pass" else "red"
        auth_text = f"[{auth_color}]{auth}[/{auth_color}]"
        rprint(f"  • Authentication: {auth_text}")

    trading_perms = results.get("trading_permissions", {})
    if trading_perms:
        trade_status = trading_perms.get("status", "unknown")
        trade_color = "green" if trade_status == "pass" else "red"
        trade_text = f"[{trade_color}]{trade_status}[/{trade_color}]"
        rprint(f"  • Trading Permissions: {trade_text}")

        if trading_perms.get("trade_allowed") is not None:
            trade_allowed = trading_perms["trade_allowed"]
            allowed_color = "green" if trade_allowed else "red"
            allowed_text = f"[{allowed_color}]{trade_allowed!s}[/{allowed_color}]"
            rprint(f"    tradeAllowed: {allowed_text}")

    critical_issues = results.get("critical_issues", [])
    if critical_issues:
        rprint("\n🚨 [red]Critical Issues:[/red]")
        for issue in critical_issues:
            rprint(f"  • {issue}")

    recommendations = results.get("recommendations", [])
    if recommendations:
        rprint("\n💡 [yellow]Recommendations:[/yellow]")
        for rec in recommendations:
            rprint(f"  • {rec}")
