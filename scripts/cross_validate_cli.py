#!/usr/bin/env python3
"""Cross-validation CLI — run multi-engine backtest comparison.

Usage:
    # Run all available engines on EURUSD H1 data
    python scripts/cross_validate_cli.py run data/eurusd_h1.csv --h4 data/eurusd_h4.csv

    # Run specific engines
    python scripts/cross_validate_cli.py run data/eurusd_h1.csv --engines nova,backtesting_py

    # List available engines
    python scripts/cross_validate_cli.py engines

    # Custom thresholds
    python scripts/cross_validate_cli.py run data/eurusd_h1.csv --win-rate-threshold 3.0
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from novatrade.backtest.cross_validation import (
    CrossValidationRunner,
    DivergenceThresholds,
    EngineId,
)
from novatrade.backtest.cross_validation.comparator import (
    format_consensus_report,
    format_consensus_telegram,
)
from novatrade.backtest.environment import DEFAULT_ENVIRONMENT
from novatrade.data.loader import load_candles


def cmd_run(args: argparse.Namespace) -> None:
    """Run cross-validation."""
    # Load data
    print(f"Loading H1 data from {args.h1_data}...")
    h1_candles = load_candles(args.h1_data)
    print(f"  {len(h1_candles)} H1 candles loaded")

    h4_candles = []
    if args.h4:
        print(f"Loading H4 data from {args.h4}...")
        h4_candles = load_candles(args.h4)
        print(f"  {len(h4_candles)} H4 candles loaded")

    # Parse engines
    engines = None
    if args.engines:
        engine_names = [e.strip() for e in args.engines.split(",")]
        engines = []
        for name in engine_names:
            try:
                engines.append(EngineId(name))
            except ValueError:
                print(f"Unknown engine: {name}. Valid: {[e.value for e in EngineId]}")
                sys.exit(1)

    # Thresholds
    thresholds = DivergenceThresholds()
    if args.win_rate_threshold:
        thresholds.win_rate_abs = args.win_rate_threshold
    if args.pnl_threshold:
        thresholds.net_pnl_pct = args.pnl_threshold

    # Environment
    env = DEFAULT_ENVIRONMENT

    # Run
    runner = CrossValidationRunner(engines=engines, thresholds=thresholds)
    print(f"\nRunning cross-validation...")
    report = runner.run(h1_candles, h4_candles, env)

    # Output
    if args.json:
        # Simplified JSON output
        out = {
            "agreement_score": report.agreement_score,
            "engines": len(report.results),
            "failed": report.failed_engines,
            "summary": report.summary,
            "divergences": [
                {
                    "metric": d.metric_name,
                    "engines": f"{d.engine_a.value} vs {d.engine_b.value}",
                    "values": [d.value_a, d.value_b],
                    "severity": d.severity.value,
                }
                for d in report.divergences
            ],
        }
        print(json.dumps(out, indent=2))
    else:
        print("\n" + format_consensus_report(report))

    if args.telegram:
        print("\n--- Telegram message ---")
        print(format_consensus_telegram(report))


def cmd_engines(args: argparse.Namespace) -> None:
    """List available engines."""
    from novatrade.backtest.cross_validation.runner import ADAPTER_REGISTRY, get_adapter

    print("Available backtesting engines:\n")
    for eid, cls in ADAPTER_REGISTRY.items():
        adapter = cls()
        available = adapter.is_available()
        status = "INSTALLED" if available else "NOT INSTALLED"
        version = adapter.engine_version if available else "-"
        print(f"  {eid.value:20s}  {status:15s}  {version}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="NovaTrade multi-engine backtesting cross-validation"
    )
    sub = parser.add_subparsers(dest="command")

    # run
    p_run = sub.add_parser("run", help="Run cross-validation")
    p_run.add_argument("h1_data", help="Path to H1 candle data (CSV/JSON)")
    p_run.add_argument("--h4", help="Path to H4 candle data (CSV/JSON)")
    p_run.add_argument("--engines", help="Comma-separated engine list (default: all available)")
    p_run.add_argument("--win-rate-threshold", type=float, help="Win rate divergence threshold (pp)")
    p_run.add_argument("--pnl-threshold", type=float, help="P&L divergence threshold (%%)")
    p_run.add_argument("--json", action="store_true", help="Output JSON instead of markdown")
    p_run.add_argument("--telegram", action="store_true", help="Also print Telegram message")

    # engines
    sub.add_parser("engines", help="List available engines")

    args = parser.parse_args()
    if args.command == "run":
        cmd_run(args)
    elif args.command == "engines":
        cmd_engines(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
