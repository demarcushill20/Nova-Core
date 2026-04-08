"""CLI entry point for NovaTrade validation tools."""

import argparse
import sys
from pathlib import Path

from .mtf_validator import MTFSignalValidator
from .trade_validator import _run_cli as run_trade_validator_cli


def main() -> None:
    """Main CLI entry point for validation tools."""
    parser = argparse.ArgumentParser(
        prog="python -m novatrade.validation",
        description="NovaTrade validation tools",
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Trade validation command (existing)
    subparsers.add_parser("trade", help="Post-trade validation (legacy default)")

    # MTF validation command (new)
    mtf_parser = subparsers.add_parser("mtf", help="Multi-timeframe signal validation")
    mtf_parser.add_argument("--symbols", nargs="+", default=["EURUSD"], help="Symbols to validate")
    mtf_parser.add_argument("--m5-signals", type=Path, help="M5 signals JSONL file")
    mtf_parser.add_argument("--h1-signals", type=Path, help="H1 signals JSONL file")
    mtf_parser.add_argument("--output", type=Path, help="Output JSONL file")
    mtf_parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")

    args = parser.parse_args()

    if args.command is None or args.command == "trade":
        # Default to trade validator for backward compatibility
        run_trade_validator_cli()
    elif args.command == "mtf":
        run_mtf_validation(args)
    else:
        parser.print_help()
        sys.exit(1)


def run_mtf_validation(args) -> None:
    """Run MTF validation."""
    import logging

    if args.verbose:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

    validator = MTFSignalValidator(
        m5_signals_path=args.m5_signals,
        h1_signals_path=args.h1_signals,
        output_path=args.output,
    )

    print(f"Running MTF validation for symbols: {args.symbols}")

    reports = validator.run_validation(args.symbols)
    summary = validator.generate_summary(reports)

    print("\n=== MTF Validation Summary ===")
    print(f"Total reports: {summary['total_reports']}")

    if summary["total_reports"] > 0:
        print(f"Direction coherence pass rate: {summary['direction_coherence_pass_rate']:.1%}")
        print(f"EMA alignment pass rate: {summary['ema_alignment_pass_rate']:.1%}")
        print(f"ADX confirmation pass rate: {summary['adx_confirmation_pass_rate']:.1%}")
        print(f"Timing sequence pass rate: {summary['timing_sequence_pass_rate']:.1%}")
        print(f"Overall pass rate: {summary['overall_pass_rate']:.1%}")
        print(f"Average coherence score: {summary['average_coherence_score']:.3f}")
        print(f"High coherence signals (≥0.8): {summary['high_coherence_signals']}")
        print(f"Low coherence signals (<0.5): {summary['low_coherence_signals']}")

        # Color-coded status
        overall_pass_rate = summary["overall_pass_rate"]
        if overall_pass_rate >= 0.9:
            status = "✅ EXCELLENT"
        elif overall_pass_rate >= 0.7:
            status = "✅ GOOD"
        elif overall_pass_rate >= 0.5:
            status = "⚠️  ACCEPTABLE"
        else:
            status = "❌ NEEDS ATTENTION"

        print(f"\nMTF Validation Status: {status}")
    else:
        print("⚠️  No signal data found for validation")

    print(f"\nResults written to: {validator.output_path}")


if __name__ == "__main__":
    main()
