#!/usr/bin/env python3
"""NovaTrade Operations Chain — unified operational workflow orchestrator.

Chains individual NovaTrade operational steps into end-to-end workflows:

  deploy    — full deployment: validate → connect → preflight → launch gate → start service
  health    — health chain:   service status → connection → account → positions → risk
  restart   — restart chain:  stop → verify flat → start → health
  status    — status chain:   service state → connection → balance → positions → metrics
  teardown  — shutdown chain: stop service → verify disconnect → report

Usage:
    python3 scripts/novatrade_chain.py deploy
    python3 scripts/novatrade_chain.py deploy --dry-run
    python3 scripts/novatrade_chain.py health
    python3 scripts/novatrade_chain.py restart
    python3 scripts/novatrade_chain.py status
    python3 scripts/novatrade_chain.py teardown
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Ensure repo root is importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from novatrade.config import NovaTradeCfg

log = logging.getLogger("novatrade.chain")

ENV_FILE = Path("/etc/novacore/novatrade.env")
SERVICE_NAME = "novacore-novatrade"
EVIDENCE_DIR = Path("/home/nova/nova-core/OUTPUT/novatrade")
LOGS_DIR = Path("/home/nova/nova-core/LOGS/novatrade")


# ---------------------------------------------------------------------------
# Step result model
# ---------------------------------------------------------------------------


@dataclass
class StepResult:
    """Result of a single chain step."""

    name: str
    status: str = "PASS"  # PASS | FAIL | WARN | SKIP
    duration_ms: int = 0
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class ChainResult:
    """Result of a full chain execution."""

    chain: str
    steps: list[StepResult] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""
    overall: str = "PASS"  # PASS | FAIL | PARTIAL

    def add(self, step: StepResult) -> None:
        self.steps.append(step)
        if step.status == "FAIL":
            self.overall = "FAIL"
        elif step.status == "WARN" and self.overall == "PASS":
            self.overall = "PARTIAL"

    @property
    def passed(self) -> int:
        return sum(1 for s in self.steps if s.status == "PASS")

    @property
    def failed(self) -> int:
        return sum(1 for s in self.steps if s.status == "FAIL")

    def summary_line(self) -> str:
        total = len(self.steps)
        return f"{self.chain}: {self.passed}/{total} steps passed — {self.overall}"

    def report(self) -> str:
        lines = [
            f"{'=' * 60}",
            f"  NovaTrade Chain: {self.chain}",
            f"  Started:  {self.started_at}",
            f"  Finished: {self.finished_at}",
            f"  Result:   {self.overall}",
            f"{'=' * 60}",
            "",
        ]
        for i, step in enumerate(self.steps, 1):
            icon = {"PASS": "+", "FAIL": "!", "WARN": "~", "SKIP": "-"}.get(step.status, "?")
            lines.append(f"  [{icon}] Step {i}: {step.name} — {step.status} ({step.duration_ms}ms)")
            if step.message:
                lines.append(f"      {step.message}")
            if step.details:
                for k, v in step.details.items():
                    lines.append(f"      {k}: {v}")
            lines.append("")
        lines.append(f"  {self.summary_line()}")
        lines.append(f"{'=' * 60}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Step helpers
# ---------------------------------------------------------------------------


def _timed(name: str):
    """Context manager that yields a _Timer with .result StepResult."""

    class _Timer:
        def __init__(self):
            self.result = StepResult(name=name)
            self._start = 0.0

        def __enter__(self):
            self._start = time.monotonic()
            return self

        def __exit__(self, *exc):
            elapsed = time.monotonic() - self._start
            self.result.duration_ms = int(elapsed * 1000)

    return _Timer()


def _run_systemctl(action: str, service: str = SERVICE_NAME) -> tuple[int, str]:
    """Run a systemctl command, return (returncode, output)."""
    try:
        proc = subprocess.run(
            ["systemctl", action, service],
            capture_output=True,
            text=True,
            timeout=30,
        )
        output = (proc.stdout + proc.stderr).strip()
        return proc.returncode, output
    except subprocess.TimeoutExpired:
        return 1, "systemctl timed out"
    except FileNotFoundError:
        return 1, "systemctl not found"


def _service_is_active() -> bool:
    code, _ = _run_systemctl("is-active")
    return code == 0


# ---------------------------------------------------------------------------
# Shared adapter context
# ---------------------------------------------------------------------------


class _AdapterCtx:
    """Holds a shared MetaApi adapter for the duration of a chain."""

    def __init__(self, cfg: NovaTradeCfg):
        self._cfg = cfg
        self.adapter = None
        self._connected = False

    async def connect(self):
        if self._connected:
            return
        from novatrade.adapter.metaapi_provider import MetaApiAdapter

        self.adapter = MetaApiAdapter(self._cfg.metaapi)
        await self.adapter.connect()
        self._connected = True

    async def disconnect(self):
        if self._connected and self.adapter:
            await self.adapter.disconnect()
            self._connected = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.disconnect()


# ---------------------------------------------------------------------------
# Chain steps (async)
# ---------------------------------------------------------------------------


async def step_validate_env(cfg: NovaTradeCfg) -> StepResult:
    """Validate environment and configuration."""
    with _timed("Validate environment") as t:
        r = t.result
        errors = cfg.validate()
        if errors:
            r.status = "FAIL"
            r.message = f"{len(errors)} config error(s)"
            r.details = {"errors": errors}
        else:
            r.message = f"Config OK — mode={cfg.mode.value}, provider={cfg.provider}, dry_run={cfg.dry_run}"
            r.details = {
                "symbols": cfg.symbols,
                "dry_run": cfg.dry_run,
                "ftmo_enabled": cfg.ftmo.enabled,
                "account_id": (cfg.metaapi.account_id[:8] + "...") if cfg.metaapi.account_id else "NOT SET",
            }
    return r


async def step_test_connection(ctx: _AdapterCtx) -> StepResult:
    """Test MetaApi connection and account access."""
    with _timed("Test MetaApi connection") as t:
        r = t.result
        try:
            await ctx.connect()
            if ctx.adapter is None:
                raise RuntimeError("adapter not connected")

            account = await ctx.adapter.get_account()
            r.details["balance"] = account.balance
            r.details["equity"] = account.equity
            r.details["currency"] = account.currency
            r.details["leverage"] = account.leverage

            positions = await ctx.adapter.get_positions()
            r.details["open_positions"] = len(positions)

            r.message = (
                f"Connected — balance={account.balance:.2f} {account.currency}, "
                f"equity={account.equity:.2f}, positions={len(positions)}"
            )
        except Exception as e:
            r.status = "FAIL"
            r.message = f"Connection failed: {e}"
    return r


async def step_quote_symbols(ctx: _AdapterCtx, cfg: NovaTradeCfg) -> StepResult:
    """Get live quotes for configured symbols."""
    with _timed("Quote symbols") as t:
        r = t.result
        try:
            await ctx.connect()
            if ctx.adapter is None:
                raise RuntimeError("adapter not connected")

            quotes = {}
            for symbol in cfg.symbols:
                broker_symbol = cfg.ftmo.resolve_symbol(symbol) if cfg.ftmo.enabled else symbol
                try:
                    price = await ctx.adapter.get_symbol_price(broker_symbol)
                    quotes[symbol] = {
                        "bid": price.bid,
                        "ask": price.ask,
                        "spread": f"{price.spread:.1f}",
                        "broker_symbol": broker_symbol,
                    }
                except Exception as e:
                    quotes[symbol] = {"error": str(e), "broker_symbol": broker_symbol}

            failed = [s for s, q in quotes.items() if "error" in q]
            if failed:
                r.status = "WARN"
                r.message = f"{len(failed)}/{len(quotes)} symbol quotes failed"
            else:
                r.message = f"All {len(quotes)} symbols quoted successfully"
            r.details = quotes
        except Exception as e:
            r.status = "FAIL"
            r.message = f"Quote retrieval failed: {e}"
    return r


async def step_preflight(cfg: NovaTradeCfg) -> StepResult:
    """Run FTMO preflight checks."""
    with _timed("FTMO preflight") as t:
        r = t.result
        try:
            proc = subprocess.run(  # noqa: ASYNC221
                [sys.executable, "scripts/novatrade_ftmo.py", "preflight", "--env-file", str(ENV_FILE)],
                capture_output=True,
                text=True,
                timeout=60,
                cwd="/home/nova/nova-core",
            )
            r.details["exit_code"] = proc.returncode
            output = proc.stdout + proc.stderr

            pass_count = output.count("PASS")
            fail_count = output.count("FAIL")
            r.details["pass_count"] = pass_count
            r.details["fail_count"] = fail_count

            if proc.returncode == 0 and fail_count == 0:
                r.message = f"Preflight {pass_count}/{pass_count + fail_count} PASS"
            elif fail_count > 0:
                r.status = "FAIL"
                r.message = f"Preflight {pass_count}/{pass_count + fail_count} — {fail_count} failures"
                fail_lines = [ln.strip() for ln in output.splitlines() if "FAIL" in ln]
                r.details["failures"] = fail_lines[:5]
            else:
                r.status = "WARN"
                r.message = f"Preflight returned code {proc.returncode}"
        except subprocess.TimeoutExpired:
            r.status = "FAIL"
            r.message = "Preflight timed out (60s)"
        except Exception as e:
            r.status = "FAIL"
            r.message = f"Preflight error: {e}"
    return r


async def step_launch_gate() -> StepResult:
    """Evaluate launch readiness gate."""
    with _timed("Launch gate evaluation") as t:
        r = t.result
        try:
            from novatrade.runtime.launch_gate import resolve_launch_mode, validate_startup

            cfg = NovaTradeCfg.load(env_file=ENV_FILE)
            mode = resolve_launch_mode()
            startup_checks = validate_startup(cfg, mode)

            r.details["launch_mode"] = mode.value
            r.details["startup_ok"] = startup_checks.ok

            if not startup_checks.ok:
                r.status = "WARN"
                all_issues = startup_checks.errors + startup_checks.warnings
                r.message = f"Launch mode={mode.value}, {len(all_issues)} issue(s)"
                r.details["warnings"] = all_issues[:5]
            else:
                r.message = f"Launch gate OK — mode={mode.value}"
        except Exception as e:
            r.status = "FAIL"
            r.message = f"Launch gate error: {e}"
    return r


def step_service_status() -> StepResult:
    """Check systemd service status."""
    with _timed("Service status") as t:
        r = t.result
        active = _service_is_active()
        _, status_text = _run_systemctl("status")

        r.details["active"] = active
        for line in status_text.splitlines():
            line = line.strip()
            if line.startswith("Active:"):
                r.details["status_line"] = line
                break

        if active:
            r.message = "Service is active (running)"
        else:
            r.status = "WARN"
            r.message = "Service is not running"
    return r


def step_start_service() -> StepResult:
    """Start the NovaTrade systemd service."""
    with _timed("Start service") as t:
        r = t.result
        if _service_is_active():
            r.status = "SKIP"
            r.message = "Service already running"
            return r

        code, output = _run_systemctl("start")
        if code == 0:
            time.sleep(2)
            if _service_is_active():
                r.message = "Service started successfully"
            else:
                r.status = "FAIL"
                r.message = "Service started but is not active"
                r.details["output"] = output
        else:
            r.status = "FAIL"
            r.message = f"Failed to start service (code {code})"
            r.details["output"] = output
    return r


def step_stop_service() -> StepResult:
    """Stop the NovaTrade systemd service."""
    with _timed("Stop service") as t:
        r = t.result
        if not _service_is_active():
            r.status = "SKIP"
            r.message = "Service already stopped"
            return r

        code, output = _run_systemctl("stop")
        if code == 0:
            time.sleep(2)
            if not _service_is_active():
                r.message = "Service stopped successfully"
            else:
                r.status = "FAIL"
                r.message = "Stop issued but service still active"
        else:
            r.status = "FAIL"
            r.message = f"Failed to stop service (code {code})"
            r.details["output"] = output
    return r


async def step_check_positions(ctx: _AdapterCtx) -> StepResult:
    """Check open positions and P&L."""
    with _timed("Check positions") as t:
        r = t.result
        try:
            await ctx.connect()
            if ctx.adapter is None:
                raise RuntimeError("adapter not connected")

            positions = await ctx.adapter.get_positions()
            account = await ctx.adapter.get_account()

            total_pl = sum(getattr(p, "profit", 0) or 0 for p in positions)
            r.details["open_positions"] = len(positions)
            r.details["total_unrealized_pl"] = round(total_pl, 2)
            r.details["equity"] = account.equity
            r.details["balance"] = account.balance

            if positions:
                r.status = "WARN"
                r.message = f"{len(positions)} open position(s), unrealized P&L: {total_pl:+.2f}"
                for p in positions:
                    sym = getattr(p, "symbol", "?")
                    vol = getattr(p, "volume", 0)
                    pl = getattr(p, "profit", 0) or 0
                    r.details[f"pos_{sym}"] = f"volume={vol}, P&L={pl:+.2f}"
            else:
                r.message = f"FLAT — no open positions, balance={account.balance:.2f}"
        except Exception as e:
            r.status = "FAIL"
            r.message = f"Position check failed: {e}"
    return r


async def step_risk_status(cfg: NovaTradeCfg) -> StepResult:
    """Check risk engine status and drawdown config."""
    with _timed("Risk engine status") as t:
        r = t.result
        try:
            from novatrade.risk.hard_risk_supervisor import HardLimits, HardRiskSupervisor

            supervisor = HardRiskSupervisor(limits=HardLimits())

            r.details["daily_dd_limit"] = f"{cfg.risk.max_daily_drawdown_pct}%"
            r.details["total_dd_limit"] = f"{cfg.risk.max_total_drawdown_pct}%"
            r.details["kill_switch_halted"] = supervisor.halted
            r.details["max_positions"] = cfg.risk.max_positions
            r.details["max_vol_per_trade"] = cfg.risk.max_volume_per_trade
            r.details["anti_ea_rollover"] = cfg.risk.rollover_dead_zone_enabled
            r.details["anti_ea_jitter"] = cfg.risk.entry_jitter_enabled
            r.details["anti_ea_lot_var"] = cfg.risk.lot_micro_variation_enabled

            if supervisor.halted:
                r.status = "FAIL"
                r.message = "KILL SWITCH ACTIVE — trading halted"
            else:
                r.message = (
                    f"Risk OK — DD limits {cfg.risk.max_daily_drawdown_pct}%/"
                    f"{cfg.risk.max_total_drawdown_pct}%, max positions={cfg.risk.max_positions}"
                )
        except Exception as e:
            r.status = "FAIL"
            r.message = f"Risk status check failed: {e}"
    return r


def step_check_evidence() -> StepResult:
    """Check evidence directory and recent trade logs."""
    with _timed("Evidence check") as t:
        r = t.result
        evidence_file = EVIDENCE_DIR / "evidence.jsonl"

        if not evidence_file.exists():
            r.status = "WARN"
            r.message = "No evidence file found (no trades recorded yet)"
            return r

        try:
            lines = evidence_file.read_text().strip().splitlines()
            r.details["total_records"] = len(lines)

            now = time.time()
            recent = 0
            for line in reversed(lines[-50:]):
                try:
                    record = json.loads(line)
                    ts = record.get("timestamp", 0)
                    if isinstance(ts, str) or now - ts < 86400:
                        recent += 1
                except json.JSONDecodeError:
                    pass

            r.details["recent_24h"] = recent
            r.message = f"{len(lines)} evidence records, {recent} in last 24h"
        except Exception as e:
            r.status = "WARN"
            r.message = f"Evidence read error: {e}"
    return r


# ---------------------------------------------------------------------------
# Chains — share a single adapter connection per chain
# ---------------------------------------------------------------------------


async def chain_deploy(cfg: NovaTradeCfg, dry_run: bool = False) -> ChainResult:
    """Full deployment chain: validate → connect → preflight → gate → start."""
    cr = ChainResult(chain="deploy")
    cr.started_at = dt.datetime.now(dt.timezone.utc).isoformat()

    cr.add(await step_validate_env(cfg))
    if cr.overall == "FAIL":
        cr.finished_at = dt.datetime.now(dt.timezone.utc).isoformat()
        return cr

    async with _AdapterCtx(cfg) as ctx:
        cr.add(await step_test_connection(ctx))
        if cr.overall == "FAIL":
            cr.finished_at = dt.datetime.now(dt.timezone.utc).isoformat()
            return cr

        cr.add(await step_quote_symbols(ctx, cfg))

    cr.add(await step_preflight(cfg))
    if cr.overall == "FAIL":
        cr.finished_at = dt.datetime.now(dt.timezone.utc).isoformat()
        return cr

    cr.add(await step_launch_gate())

    if dry_run:
        cr.add(StepResult(name="Start service", status="SKIP", message="Skipped (--dry-run)"))
    else:
        cr.add(step_start_service())

    if not dry_run and cr.overall != "FAIL":
        cr.add(step_service_status())

    cr.finished_at = dt.datetime.now(dt.timezone.utc).isoformat()
    return cr


async def chain_health(cfg: NovaTradeCfg) -> ChainResult:
    """Health chain: service → connection → account → positions → risk."""
    cr = ChainResult(chain="health")
    cr.started_at = dt.datetime.now(dt.timezone.utc).isoformat()

    cr.add(step_service_status())
    cr.add(await step_validate_env(cfg))

    if cfg.metaapi.token and cfg.metaapi.account_id:
        async with _AdapterCtx(cfg) as ctx:
            cr.add(await step_test_connection(ctx))
            cr.add(await step_check_positions(ctx))
    else:
        cr.add(StepResult(name="MetaApi checks", status="SKIP", message="No MetaApi credentials configured"))

    cr.add(await step_risk_status(cfg))
    cr.add(step_check_evidence())

    cr.finished_at = dt.datetime.now(dt.timezone.utc).isoformat()
    return cr


async def chain_restart(cfg: NovaTradeCfg) -> ChainResult:
    """Restart chain: stop → verify flat → start → health check."""
    cr = ChainResult(chain="restart")
    cr.started_at = dt.datetime.now(dt.timezone.utc).isoformat()

    if cfg.metaapi.token and cfg.metaapi.account_id:
        async with _AdapterCtx(cfg) as ctx:
            cr.add(await step_check_positions(ctx))

    cr.add(step_stop_service())
    cr.add(step_start_service())
    cr.add(step_service_status())

    cr.finished_at = dt.datetime.now(dt.timezone.utc).isoformat()
    return cr


async def chain_status(cfg: NovaTradeCfg) -> ChainResult:
    """Status chain: service → connection → balance → positions → evidence."""
    cr = ChainResult(chain="status")
    cr.started_at = dt.datetime.now(dt.timezone.utc).isoformat()

    cr.add(step_service_status())

    if cfg.metaapi.token and cfg.metaapi.account_id:
        async with _AdapterCtx(cfg) as ctx:
            cr.add(await step_test_connection(ctx))
            cr.add(await step_check_positions(ctx))
            cr.add(await step_quote_symbols(ctx, cfg))
    else:
        cr.add(StepResult(name="MetaApi status", status="SKIP", message="No MetaApi credentials"))

    cr.add(step_check_evidence())

    cr.finished_at = dt.datetime.now(dt.timezone.utc).isoformat()
    return cr


async def chain_teardown(cfg: NovaTradeCfg) -> ChainResult:
    """Teardown chain: stop service → verify disconnect → report."""
    cr = ChainResult(chain="teardown")
    cr.started_at = dt.datetime.now(dt.timezone.utc).isoformat()

    if cfg.metaapi.token and cfg.metaapi.account_id:
        async with _AdapterCtx(cfg) as ctx:
            cr.add(await step_check_positions(ctx))

    cr.add(step_stop_service())
    cr.add(step_service_status())
    cr.add(step_check_evidence())

    cr.finished_at = dt.datetime.now(dt.timezone.utc).isoformat()
    return cr


# ---------------------------------------------------------------------------
# Evidence logging
# ---------------------------------------------------------------------------


def _log_chain_result(result: ChainResult) -> None:
    """Write chain execution to log file."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M%S")
    log_path = LOGS_DIR / f"chain_{result.chain}_{ts}.log"
    log_path.write_text(result.report())
    log.info("Chain log written to %s", log_path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="NovaTrade Operations Chain — unified workflow orchestrator",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument("--env-file", default=str(ENV_FILE), help="env file path")

    sub = parser.add_subparsers(dest="chain", required=True)

    deploy_p = sub.add_parser("deploy", help="Full deployment chain")
    deploy_p.add_argument("--dry-run", action="store_true", help="validate only, do not start service")

    sub.add_parser("health", help="Health check chain")
    sub.add_parser("restart", help="Restart chain")
    sub.add_parser("status", help="Status overview chain")
    sub.add_parser("teardown", help="Clean shutdown chain")

    return parser


async def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    # Keep chain logger at INFO for progress messages
    logging.getLogger("novatrade.chain").setLevel(logging.INFO)

    env_path = Path(args.env_file) if args.env_file else None
    cfg = NovaTradeCfg.load(env_file=env_path)

    chain_map = {
        "deploy": lambda: chain_deploy(cfg, dry_run=getattr(args, "dry_run", False)),
        "health": lambda: chain_health(cfg),
        "restart": lambda: chain_restart(cfg),
        "status": lambda: chain_status(cfg),
        "teardown": lambda: chain_teardown(cfg),
    }

    runner = chain_map.get(args.chain)
    if not runner:
        print(f"Unknown chain: {args.chain}")
        return 1

    result = await runner()
    print(result.report())
    _log_chain_result(result)

    return 0 if result.overall == "PASS" else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
