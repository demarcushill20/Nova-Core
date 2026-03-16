#!/usr/bin/env python3
"""NovaTrade connection validation — narrow entrypoint for Phase 2 testing.

Verifies:
  1. Config loads correctly from environment / env file
  2. MetaApi connection succeeds (deploy + sync)
  3. Account state is readable
  4. Open positions are readable
  5. Symbol quote is readable (optional, --quote SYMBOL)

Does NOT place orders unless --place-test-order is explicitly passed AND
dry_run is disabled.  Default behavior is read-only.

Usage:
    python3 scripts/novatrade_connect_test.py
    python3 scripts/novatrade_connect_test.py --quote EURUSD
    python3 scripts/novatrade_connect_test.py --env-file /etc/novacore/novatrade.env
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# Ensure repo root is importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from novatrade.adapter.metaapi_provider import MetaApiAdapter
from novatrade.config import NovaTradeCfg


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
    """Execute the connection validation sequence.  Returns exit code."""

    # ── 1. Config ──────────────────────────────────────────────
    _print_section("1. Configuration")
    env_file = Path(args.env_file) if args.env_file else None
    cfg = NovaTradeCfg.load(env_file=env_file)
    errors = cfg.validate()
    if errors:
        print("  CONFIG ERRORS:")
        for e in errors:
            print(f"    - {e}")
        return 1

    print(f"  provider:   {cfg.provider}")
    print(f"  mode:       {cfg.mode.value}")
    print(f"  dry_run:    {cfg.dry_run}")
    print(f"  symbols:    {cfg.symbols}")
    print(f"  timeframes: {cfg.timeframes}")
    print(f"  account_id: {cfg.metaapi.account_id[:8]}...")
    print(f"  token:      {'set' if cfg.metaapi.token else 'MISSING'}")
    print("  OK")

    # ── 2. Connection ─────────────────────────────────────────
    _print_section("2. MetaApi Connection")
    adapter = MetaApiAdapter(cfg.metaapi)
    health = await adapter.connect()
    print(f"  state:      {health.state.value}")
    print(f"  connected:  {health.connected}")
    print(f"  latency:    {health.latency_ms:.0f} ms" if health.latency_ms else "  latency:    n/a")
    print(f"  message:    {health.message}")
    if not health.connected:
        print("  FAILED — cannot continue")
        return 2

    try:
        # ── 3. Account ────────────────────────────────────────
        _print_section("3. Account State")
        acct = await adapter.get_account()
        print(f"  balance:    {acct.balance:.2f} {acct.currency}")
        print(f"  equity:     {acct.equity:.2f} {acct.currency}")
        print(f"  margin:     {acct.margin:.2f}")
        print(f"  free_margin:{acct.free_margin:.2f}")
        print(f"  leverage:   {acct.leverage}")
        print(f"  mode:       {acct.mode.value}")
        print(f"  broker:     {acct.broker}")
        print(f"  server:     {acct.server}")
        print("  OK")

        # ── 4. Positions ──────────────────────────────────────
        _print_section("4. Open Positions")
        positions = await adapter.get_positions()
        print(f"  count: {len(positions)}")
        for p in positions[:10]:  # cap display
            print(
                f"    [{p.position_id}] {p.side.value} {p.symbol} "
                f"vol={p.volume} @ {p.open_price:.5f} "
                f"pnl={p.unrealized_pnl:.2f}"
            )
        print("  OK")

        # ── 5. Quote (optional) ───────────────────────────────
        if args.quote:
            _print_section(f"5. Symbol Quote: {args.quote}")
            sp = await adapter.get_symbol_price(args.quote)
            print(f"  bid:    {sp.bid:.5f}")
            print(f"  ask:    {sp.ask:.5f}")
            print(f"  spread: {sp.spread:.5f}")
            print("  OK")

        # ── 6. Health check ───────────────────────────────────
        _print_section("6. Health Check")
        h = await adapter.health_check()
        print(f"  state:   {h.state.value}")
        print(f"  latency: {h.latency_ms:.0f} ms" if h.latency_ms else "  latency: n/a")
        print("  OK")

    finally:
        # ── Cleanup ───────────────────────────────────────────
        _print_section("Cleanup")
        await adapter.disconnect()
        print("  disconnected")

    _print_section("RESULT: ALL CHECKS PASSED")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="NovaTrade MetaApi connection validation",
    )
    parser.add_argument(
        "--env-file",
        default=None,
        help="Path to env file (default: /etc/novacore/novatrade.env)",
    )
    parser.add_argument(
        "--quote",
        default=None,
        metavar="SYMBOL",
        help="Fetch a live quote for SYMBOL (e.g. EURUSD)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()
    _setup_logging(args.verbose)

    exit_code = asyncio.get_event_loop().run_until_complete(run(args))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
