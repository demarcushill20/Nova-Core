#!/usr/bin/env python3
"""NovaTrade FTMO Free Trial — operator workflow.

Single entry point for FTMO demo/free-trial validation:
  preflight  — verify config + connectivity (no trades)
  dry-run    — attempt a dry-run execution (risk gate blocks, no real order)
  health     — live health + reconciliation check (read-only)
  verdict    — full evidence review + go/no-go verdict + readiness

All modes are READ-ONLY by default. No trades are placed unless the
operator explicitly passes --live to the dry-run subcommand AND has
dry_run=false in config.

Usage:
    python3 scripts/novatrade_ftmo.py preflight
    python3 scripts/novatrade_ftmo.py dry-run --symbol EURUSD --side BUY --volume 0.01 --sl 1.0900
    python3 scripts/novatrade_ftmo.py health
    python3 scripts/novatrade_ftmo.py verdict
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from novatrade.config import NovaTradeCfg
from novatrade.models import OrderSide, OrderType


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )


def _load_cfg(args: argparse.Namespace) -> NovaTradeCfg:
    env_path = Path(args.env_file) if args.env_file else None
    cfg = NovaTradeCfg.load(env_file=env_path)
    return cfg


def _campaign_label(cfg: NovaTradeCfg) -> str:
    if cfg.ftmo.enabled and cfg.ftmo.campaign_label:
        return cfg.ftmo.campaign_label
    if cfg.ftmo.enabled:
        return f"FTMO-{cfg.ftmo.challenge_type}"
    return ""


def _build_adapter(cfg: NovaTradeCfg):
    from novatrade.adapter.metaapi_provider import MetaApiAdapter

    return MetaApiAdapter(cfg.metaapi)


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


async def _cmd_preflight(cfg: NovaTradeCfg, args: argparse.Namespace) -> int:
    from novatrade.validation.ftmo_preflight import format_preflight, run_preflight

    errors = cfg.validate()
    if errors:
        print("CONFIG ERRORS:")
        for e in errors:
            print(f"  - {e}")
        return 1

    adapter = _build_adapter(cfg)
    try:
        result = await run_preflight(cfg, adapter)
    finally:
        try:
            await adapter.disconnect()
        except Exception:
            pass

    campaign = _campaign_label(cfg)
    if args.json_output:
        from dataclasses import asdict

        print(json.dumps(asdict(result), indent=2))
    else:
        print(format_preflight(result, campaign=campaign))

    return 0 if result.passed else 1


async def _cmd_dry_run(cfg: NovaTradeCfg, args: argparse.Namespace) -> int:
    from novatrade.execution.demo_executor import DemoExecutor
    from novatrade.models import OrderRequest
    from novatrade.monitor.position_tracker import PositionTracker
    from novatrade.validation.evidence import EvidenceRecorder

    if not args.live:
        cfg.dry_run = True  # enforce safety

    errors = cfg.validate()
    if errors:
        print("CONFIG ERRORS:")
        for e in errors:
            print(f"  - {e}")
        return 1

    # Resolve symbol through FTMO mapping
    symbol = args.symbol
    if cfg.ftmo.enabled:
        symbol = cfg.ftmo.resolve_symbol(symbol)

    try:
        side = OrderSide(args.side.upper())
    except ValueError:
        print(f"Invalid side: {args.side} (use BUY or SELL)")
        return 1

    request = OrderRequest(
        symbol=symbol,
        side=side,
        order_type=OrderType.MARKET,
        volume=args.volume,
        stop_loss=args.sl,
        take_profit=args.tp,
    )

    campaign = _campaign_label(cfg)
    recorder = EvidenceRecorder(campaign=campaign)
    tracker = PositionTracker()
    adapter = _build_adapter(cfg)

    try:
        health = await adapter.connect()
        if not health.connected:
            print(f"Connection failed: {health.message}")
            return 2

        executor = DemoExecutor(cfg, adapter, recorder=recorder, tracker=tracker)
        result = await executor.execute(request)
    finally:
        try:
            await adapter.disconnect()
        except Exception:
            pass

    mode_label = "LIVE DEMO" if args.live and not cfg.dry_run else "DRY RUN"
    print(f"\n{'─' * 44}")
    print(f"  FTMO {mode_label} Execution")
    print(f"{'─' * 44}")
    print(f"  symbol:   {request.symbol}")
    print(f"  side:     {request.side.value}")
    print(f"  volume:   {request.volume}")
    print(f"  outcome:  {result.outcome.value}")
    print(f"  elapsed:  {result.elapsed_ms:.0f}ms")
    if result.risk_decision and result.risk_decision.failed_checks:
        print("  gate:")
        for c in result.risk_decision.failed_checks:
            print(f"    - {c.name}: {c.detail}")
    if result.order_result and result.order_result.ok:
        print(f"  order_id: {result.order_result.order_id}")
        print(f"  fill:     {result.order_result.fill_price}")
    if result.error:
        print(f"  error:    {result.error}")
    print(f"  evidence: {recorder.path}")
    print()

    return 0 if result.ok or result.denied else 3


async def _cmd_health(cfg: NovaTradeCfg, args: argparse.Namespace) -> int:
    from novatrade.monitor.position_tracker import PositionTracker
    from novatrade.validation.evidence import EvidenceRecorder
    from novatrade.validation.reporting import format_report
    from novatrade.validation.review import run_validation_cycle

    errors = cfg.validate()
    if errors:
        print("CONFIG ERRORS:")
        for e in errors:
            print(f"  - {e}")
        return 1

    campaign = _campaign_label(cfg)
    recorder = EvidenceRecorder(campaign=campaign)
    tracker = PositionTracker()
    adapter = _build_adapter(cfg)

    try:
        health = await adapter.connect()
        if not health.connected:
            print(f"Connection failed: {health.message}")
            return 2

        report = await run_validation_cycle(adapter, tracker, recorder)
    finally:
        try:
            await adapter.disconnect()
        except Exception:
            pass

    if args.json_output:
        from dataclasses import asdict

        print(json.dumps(asdict(report), indent=2))
    else:
        print(format_report(report, campaign=campaign))

    return 0


async def _cmd_verdict(cfg: NovaTradeCfg, args: argparse.Namespace) -> int:
    from novatrade.validation.decision import evaluate_mvp, format_decision
    from novatrade.validation.evidence import EvidenceRecorder
    from novatrade.validation.readiness import build_readiness_package, format_readiness
    from novatrade.validation.reporting import format_report, generate_report

    campaign = _campaign_label(cfg)
    recorder = EvidenceRecorder(campaign=campaign)
    records = recorder.load()

    if not records:
        print(f"No evidence found at {recorder.path}")
        print("Run preflight and health checks first to generate evidence.")
        return 0

    report = generate_report(records)
    decision = evaluate_mvp(report)
    package = build_readiness_package(report, decision)

    if args.json_output:
        from dataclasses import asdict

        print(json.dumps(asdict(package), indent=2))
    else:
        print(format_report(report, campaign=campaign))
        print(format_decision(decision))
        print(format_readiness(package))

    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="NovaTrade FTMO Free Trial — operator workflow",
    )
    parser.add_argument("--env-file", default=None, help="Path to env file")
    parser.add_argument("-j", "--json", action="store_true", dest="json_output")
    parser.add_argument("-v", "--verbose", action="store_true")

    sub = parser.add_subparsers(dest="command", help="Subcommand")

    # preflight
    sub.add_parser("preflight", help="Verify config + connectivity (no trades)")

    # dry-run
    dr = sub.add_parser("dry-run", help="Dry-run execution attempt")
    dr.add_argument("--symbol", required=True)
    dr.add_argument("--side", required=True)
    dr.add_argument("--volume", type=float, required=True)
    dr.add_argument("--sl", type=float, default=None, help="Stop loss price")
    dr.add_argument("--tp", type=float, default=None, help="Take profit price")
    dr.add_argument("--live", action="store_true", help="Execute on demo account (else dry-run)")

    # health
    sub.add_parser("health", help="Live health + reconciliation check (read-only)")

    # verdict
    sub.add_parser("verdict", help="Evidence review + go/no-go verdict + readiness")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    if args.verbose:
        _setup_logging(args.verbose)

    cfg = _load_cfg(args)

    dispatch = {
        "preflight": _cmd_preflight,
        "dry-run": _cmd_dry_run,
        "health": _cmd_health,
        "verdict": _cmd_verdict,
    }

    handler = dispatch[args.command]
    exit_code = asyncio.get_event_loop().run_until_complete(handler(cfg, args))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
