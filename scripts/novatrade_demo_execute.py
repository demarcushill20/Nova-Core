#!/usr/bin/env python3
"""NovaTrade demo execution — manual validation entrypoint.

Default behavior is READ-ONLY (dry_run gate blocks execution).
To actually place an order, you must pass --live AND set NOVATRADE_DRY_RUN=false
in the environment or env file.

Usage:
    # Dry-run check (default — shows what would happen, no order placed):
    python3 scripts/novatrade_demo_execute.py --symbol EURUSD --side BUY --volume 0.01 --sl 1.0900

    # Live execution (requires dry_run=false in config):
    python3 scripts/novatrade_demo_execute.py --symbol EURUSD --side BUY --volume 0.01 --sl 1.0900 --live

    # With env file:
    python3 scripts/novatrade_demo_execute.py --env-file /etc/novacore/novatrade.env \
        --symbol EURUSD --side BUY --volume 0.01 --sl 1.0900
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from novatrade.adapter.metaapi_provider import MetaApiAdapter
from novatrade.config import NovaTradeCfg
from novatrade.execution.demo_executor import DemoExecutor
from novatrade.models import OrderRequest, OrderSide, OrderType
from novatrade.monitor.position_tracker import PositionTracker
from novatrade.validation.evidence import EvidenceRecorder


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )


def _print_section(title: str) -> None:
    print(f"\n{'─' * 60}")
    print(f"  {title}")
    print(f"{'─' * 60}")


async def run(args: argparse.Namespace) -> int:
    # ── 1. Config ────────────────────────────────────────────
    _print_section("1. Configuration")
    env_file = Path(args.env_file) if args.env_file else None
    cfg = NovaTradeCfg.load(env_file=env_file)

    if not args.live:
        cfg.dry_run = True
        print("  mode: DRY RUN (pass --live to execute)")
    else:
        print(f"  mode: {'LIVE' if not cfg.dry_run else 'DRY RUN (config override)'}")

    errors = cfg.validate()
    if errors:
        print("  CONFIG ERRORS:")
        for e in errors:
            print(f"    - {e}")
        return 1

    print(f"  dry_run:  {cfg.dry_run}")
    print(f"  symbols:  {cfg.symbols}")
    print(f"  provider: {cfg.provider}")

    # ── 2. Build order request ───────────────────────────────
    _print_section("2. Order Request")
    side = OrderSide.BUY if args.side.upper() == "BUY" else OrderSide.SELL
    request = OrderRequest(
        symbol=args.symbol,
        side=side,
        order_type=OrderType.MARKET,
        volume=args.volume,
        stop_loss=args.sl,
        take_profit=args.tp,
        comment="novatrade-demo-execute",
    )
    print(f"  symbol:  {request.symbol}")
    print(f"  side:    {request.side.value}")
    print(f"  volume:  {request.volume}")
    print(f"  sl:      {request.stop_loss}")
    print(f"  tp:      {request.take_profit}")

    # ── 3. Connect ───────────────────────────────────────────
    _print_section("3. MetaApi Connection")
    adapter = MetaApiAdapter(cfg.metaapi)
    health = await adapter.connect()
    print(f"  connected: {health.connected}")
    print(f"  latency:   {health.latency_ms:.0f} ms" if health.latency_ms else "  latency:   n/a")
    if not health.connected:
        print("  FAILED — cannot continue")
        return 2

    try:
        # ── 4. Execute ───────────────────────────────────────
        _print_section("4. Execution")
        recorder = EvidenceRecorder()
        tracker = PositionTracker()
        executor = DemoExecutor(cfg, adapter, recorder=recorder, tracker=tracker)
        result = await executor.execute(request, health=health)

        print(f"  evidence:   {recorder.path}")
        if tracker.count > 0:
            print(f"  tracked:    {tracker.count} position(s)")

        print(f"  outcome:    {result.outcome.value}")
        print(f"  elapsed:    {result.elapsed_ms:.0f} ms")

        if result.risk_decision:
            failed = result.risk_decision.failed_checks
            if failed:
                print(f"  gate:       DENIED ({len(failed)} check(s) failed)")
                for c in failed:
                    print(f"    - {c.name}: {c.detail}")
            else:
                print(f"  gate:       ALLOW ({len(result.risk_decision.checks)} checks passed)")

        if result.order_result:
            r = result.order_result
            print(f"  order_id:   {r.order_id}")
            print(f"  status:     {r.status.value}")
            if r.error:
                print(f"  error:      {r.error}")

        if result.error and not result.order_result:
            print(f"  error:      {result.error}")

        # ── 5. Summary ──────────────────────────────────────
        _print_section("Result")
        if result.ok:
            print("  ORDER FILLED SUCCESSFULLY")
        elif result.denied:
            print("  ORDER DENIED BY RISK GATE")
        else:
            print(f"  ORDER FAILED: {result.outcome.value}")

        return 0 if result.ok or result.denied else 3

    finally:
        _print_section("Cleanup")
        await adapter.disconnect()
        print("  disconnected")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="NovaTrade demo execution — manual order validation",
    )
    parser.add_argument("--symbol", required=True, help="Trading symbol (e.g. EURUSD)")
    parser.add_argument("--side", required=True, choices=["BUY", "SELL", "buy", "sell"])
    parser.add_argument("--volume", type=float, required=True, help="Lot size (e.g. 0.01)")
    parser.add_argument("--sl", type=float, default=None, help="Stop loss price")
    parser.add_argument("--tp", type=float, default=None, help="Take profit price")
    parser.add_argument("--live", action="store_true", help="Actually execute (default is dry-run)")
    parser.add_argument("--env-file", default=None, help="Path to env file")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    _setup_logging(args.verbose)
    exit_code = asyncio.get_event_loop().run_until_complete(run(args))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
