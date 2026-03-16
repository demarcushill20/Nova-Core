"""FTMO Free Trial preflight checks for NovaTrade.

Validates that the NovaTrade configuration, adapter connection, and FTMO
account setup are correct before any demo validation work begins.

All checks are read-only — no trades, no state changes.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum

from novatrade.adapter.base import MT5Adapter
from novatrade.config import NovaTradeCfg


class CheckStatus(Enum):
    PASS = "PASS"  # noqa: S105
    WARN = "WARN"
    FAIL = "FAIL"


@dataclass
class PreflightCheck:
    name: str
    status: CheckStatus
    detail: str = ""


@dataclass
class PreflightResult:
    checks: list[PreflightCheck] = field(default_factory=list)
    elapsed_ms: float = 0.0

    @property
    def passed(self) -> bool:
        return all(c.status != CheckStatus.FAIL for c in self.checks)

    @property
    def pass_count(self) -> int:
        return sum(1 for c in self.checks if c.status == CheckStatus.PASS)

    @property
    def warn_count(self) -> int:
        return sum(1 for c in self.checks if c.status == CheckStatus.WARN)

    @property
    def fail_count(self) -> int:
        return sum(1 for c in self.checks if c.status == CheckStatus.FAIL)


def check_config(cfg: NovaTradeCfg) -> list[PreflightCheck]:
    """Validate config is correct for FTMO demo/free-trial."""
    checks: list[PreflightCheck] = []

    # Config loads without errors
    errors = cfg.validate()
    if errors:
        checks.append(PreflightCheck("config_valid", CheckStatus.FAIL, "; ".join(errors)))
    else:
        checks.append(PreflightCheck("config_valid", CheckStatus.PASS, "config validates OK"))

    # FTMO profile enabled
    if not cfg.ftmo.enabled:
        checks.append(
            PreflightCheck("ftmo_enabled", CheckStatus.WARN, "FTMO profile not enabled (set FTMO_ENABLED=true)")
        )
    else:
        checks.append(PreflightCheck("ftmo_enabled", CheckStatus.PASS, f"profile: {cfg.ftmo.broker_label}"))

    # Mode is DEMO
    from novatrade.models import AccountMode

    if cfg.mode != AccountMode.DEMO:
        checks.append(PreflightCheck("mode_demo", CheckStatus.FAIL, f"mode is {cfg.mode.value}, expected DEMO"))
    else:
        checks.append(PreflightCheck("mode_demo", CheckStatus.PASS, "DEMO mode"))

    # dry_run is default safe
    if cfg.dry_run:
        checks.append(PreflightCheck("dry_run_safe", CheckStatus.PASS, "dry_run=true (safe default)"))
    else:
        checks.append(
            PreflightCheck(
                "dry_run_safe",
                CheckStatus.WARN,
                "dry_run=false — orders will be placed on demo account",
            )
        )

    # Symbols configured
    if not cfg.symbols:
        checks.append(PreflightCheck("symbols_configured", CheckStatus.FAIL, "no symbols configured"))
    else:
        checks.append(PreflightCheck("symbols_configured", CheckStatus.PASS, f"symbols: {', '.join(cfg.symbols)}"))

    # Risk config compatible with prop-firm
    if not cfg.risk.require_stop_loss:
        checks.append(
            PreflightCheck(
                "risk_stop_loss",
                CheckStatus.WARN,
                "require_stop_loss=false — prop firms require SL on every position",
            )
        )
    else:
        checks.append(PreflightCheck("risk_stop_loss", CheckStatus.PASS, "stop loss required"))

    if cfg.risk.max_daily_drawdown_pct > 5.0:
        checks.append(
            PreflightCheck(
                "risk_drawdown",
                CheckStatus.WARN,
                f"max_daily_drawdown_pct={cfg.risk.max_daily_drawdown_pct}% "
                "exceeds FTMO 5% daily limit — tighten for safety",
            )
        )
    else:
        checks.append(
            PreflightCheck(
                "risk_drawdown",
                CheckStatus.PASS,
                f"daily drawdown limit: {cfg.risk.max_daily_drawdown_pct}%",
            )
        )

    return checks


async def check_connection(cfg: NovaTradeCfg, adapter: MT5Adapter) -> list[PreflightCheck]:
    """Validate adapter connectivity and FTMO account access (read-only)."""
    checks: list[PreflightCheck] = []

    # Connect
    try:
        health = await adapter.connect()
        if health.connected:
            checks.append(
                PreflightCheck(
                    "adapter_connect",
                    CheckStatus.PASS,
                    f"connected (latency: {health.latency_ms:.0f}ms)" if health.latency_ms else "connected",
                )
            )
        else:
            checks.append(PreflightCheck("adapter_connect", CheckStatus.FAIL, health.message or "connection failed"))
            return checks  # can't proceed without connection
    except Exception as exc:
        checks.append(PreflightCheck("adapter_connect", CheckStatus.FAIL, str(exc)))
        return checks

    # Account info
    try:
        account = await adapter.get_account()
        checks.append(
            PreflightCheck(
                "account_readable",
                CheckStatus.PASS,
                f"balance={account.balance} {account.currency}, broker={account.broker}, server={account.server}",
            )
        )
    except Exception as exc:
        checks.append(PreflightCheck("account_readable", CheckStatus.FAIL, str(exc)))

    # Positions readable
    try:
        positions = await adapter.get_positions()
        checks.append(PreflightCheck("positions_readable", CheckStatus.PASS, f"{len(positions)} open position(s)"))
    except Exception as exc:
        checks.append(PreflightCheck("positions_readable", CheckStatus.FAIL, str(exc)))

    # Symbol quotes — resolve through FTMO mapping if enabled
    for display_symbol in cfg.symbols:
        broker_symbol = cfg.ftmo.resolve_symbol(display_symbol) if cfg.ftmo.enabled else display_symbol
        try:
            price = await adapter.get_symbol_price(broker_symbol)
            detail = f"bid={price.bid}, ask={price.ask}, spread={price.spread:.1f}"
            if broker_symbol != display_symbol:
                detail = f"{display_symbol}→{broker_symbol}: {detail}"
            checks.append(PreflightCheck(f"symbol_{display_symbol}", CheckStatus.PASS, detail))
        except Exception as exc:
            error_msg = str(exc)
            if broker_symbol != display_symbol:
                error_msg = f"{display_symbol}→{broker_symbol}: {error_msg}"
            checks.append(PreflightCheck(f"symbol_{display_symbol}", CheckStatus.FAIL, error_msg))

    return checks


async def run_preflight(cfg: NovaTradeCfg, adapter: MT5Adapter) -> PreflightResult:
    """Run full FTMO preflight: config checks + connection checks."""
    t0 = time.monotonic()
    all_checks = check_config(cfg)
    conn_checks = await check_connection(cfg, adapter)
    all_checks.extend(conn_checks)
    elapsed = (time.monotonic() - t0) * 1000
    return PreflightResult(checks=all_checks, elapsed_ms=elapsed)


def format_preflight(result: PreflightResult, campaign: str = "") -> str:
    """Render a PreflightResult as a human-readable summary."""
    title = "FTMO Free Trial — Preflight Check"
    if campaign:
        title += f" [{campaign}]"
    lines = [title, "=" * 44, ""]

    status_icon = {"PASS": "[OK]", "WARN": "[!!]", "FAIL": "[XX]"}
    for check in result.checks:
        icon = status_icon.get(check.status.value, "   ")
        lines.append(f"  {icon} {check.name}")
        if check.detail:
            lines.append(f"        {check.detail}")

    lines.append("")
    lines.append(f"Result: {result.pass_count} pass, {result.warn_count} warn, {result.fail_count} fail")
    lines.append(f"Elapsed: {result.elapsed_ms:.0f}ms")
    lines.append("")

    if result.passed:
        lines.append("PREFLIGHT PASSED — ready for FTMO demo validation")
    else:
        lines.append("PREFLIGHT FAILED — fix FAIL items before proceeding")

    return "\n".join(lines)
