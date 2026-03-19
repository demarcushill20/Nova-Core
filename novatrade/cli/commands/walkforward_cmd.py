"""CLI command for walk-forward validation.

Loads a strategy config from YAML and candle data from CSV or snapshot,
runs walk-forward validation, and prints a summary.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from novatrade.cli.commands.data import (
    CANDLE_DIR,
    load_candles_csv,
)
from novatrade.cli.config_schema import StrategyConfig
from novatrade.optimization.walkforward import (
    WalkForwardConfig,
    WalkForwardResult,
    run_walk_forward,
)

log = logging.getLogger("novatrade.cli.commands.walkforward_cmd")


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


def format_walkforward_report(result: WalkForwardResult) -> str:
    """Render walk-forward results as a human-readable summary."""
    lines: list[str] = ["=" * 60, "  Walk-Forward Validation Report", "=" * 60, ""]

    if not result.windows:
        lines.append("No windows generated (insufficient data).")
        return "\n".join(lines)

    lines.append(f"Windows: {len(result.windows)}")
    lines.append("")
    lines.append(f"{'#':>3}  {'IS Score':>10}  {'OOS Score':>10}  {'Ratio':>8}  {'Trades':>7}  {'Result':>6}")
    lines.append("-" * 56)

    for w in result.windows:
        tag = "PASS" if w.passed else "FAIL"
        lines.append(
            f"{w.window_index:>3}  {w.is_scout_score:>10.4f}  "
            f"{w.oos_scout_score:>10.4f}  {w.oos_is_ratio:>8.4f}  "
            f"{w.test_trade_count:>7}  {tag:>6}"
        )

    lines.append("")
    lines.append(f"Median OOS score:    {result.median_oos_score:.4f}")
    lines.append(f"Median OOS/IS ratio: {result.median_oos_is_ratio:.4f}")
    lines.append(f"All windows passed:  {result.all_windows_passed}")

    if result.holdout_score is not None:
        lines.append("")
        lines.append(f"Holdout score:       {result.holdout_score:.4f}")
        lines.append(f"Holdout/IS ratio:    {result.holdout_is_ratio:.4f}")
        lines.append(f"Holdout passed:      {result.holdout_passed}")

    lines.append("")
    verdict = "PASS" if result.overall_passed else "FAIL"
    lines.append(f"Overall verdict:     {verdict}")
    lines.append("=" * 60)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


def build_parser(subparsers: argparse._SubParsersAction | None = None) -> argparse.ArgumentParser:
    """Create the argument parser for the walkforward command."""
    if subparsers is not None:
        parser = subparsers.add_parser(
            "walkforward",
            help="Run walk-forward validation on a strategy config",
        )
    else:
        parser = argparse.ArgumentParser(
            description="Walk-forward validation for NovaTrade strategies",
        )

    parser.add_argument("config", type=Path, help="Path to strategy YAML config")
    parser.add_argument(
        "--symbol",
        default="EURUSD",
        help="Symbol for candle loading (default: EURUSD)",
    )
    parser.add_argument(
        "--candle-dir",
        type=Path,
        default=CANDLE_DIR,
        help="Directory containing {SYMBOL}_H1.csv and {SYMBOL}_H4.csv",
    )
    parser.add_argument("--train-bars", type=int, default=None)
    parser.add_argument("--test-bars", type=int, default=None)
    parser.add_argument("--step-bars", type=int, default=None)
    parser.add_argument("--holdout-bars", type=int, default=None)
    parser.add_argument(
        "--min-oos-ratio",
        type=float,
        default=None,
        help="Minimum OOS/IS ratio to pass (default: 0.6)",
    )
    return parser


def run_command(args: argparse.Namespace) -> int:
    """Execute the walk-forward command and print results.

    Returns:
        0 on pass, 1 on fail or error.
    """
    # Load config
    config = StrategyConfig.from_yaml(args.config)
    log.info("Loaded config: %s v%s", config.name, config.version)

    # Load candles
    candle_dir = Path(args.candle_dir)
    h1_path = candle_dir / f"{args.symbol}_H1.csv"
    h4_path = candle_dir / f"{args.symbol}_H4.csv"

    if not h1_path.exists() or not h4_path.exists():
        log.error("Candle files not found: %s / %s", h1_path, h4_path)
        return 1

    h1_candles = load_candles_csv(h1_path, symbol=args.symbol, timeframe="H1")
    h4_candles = load_candles_csv(h4_path, symbol=args.symbol, timeframe="H4")
    log.info("Loaded %d H1 + %d H4 candles", len(h1_candles), len(h4_candles))

    # Build WF config with overrides
    wf_kwargs: dict[str, int | float] = {}
    if args.train_bars is not None:
        wf_kwargs["train_bars"] = args.train_bars
    if args.test_bars is not None:
        wf_kwargs["test_bars"] = args.test_bars
    if args.step_bars is not None:
        wf_kwargs["step_bars"] = args.step_bars
    if args.holdout_bars is not None:
        wf_kwargs["holdout_bars"] = args.holdout_bars
    if args.min_oos_ratio is not None:
        wf_kwargs["min_oos_is_ratio"] = args.min_oos_ratio

    wf_config = WalkForwardConfig(**wf_kwargs)  # type: ignore[arg-type]

    # Run
    result = run_walk_forward(config, h1_candles, h4_candles, wf_config)

    # Report
    report = format_walkforward_report(result)
    print(report)

    return 0 if result.overall_passed else 1


def main() -> None:
    """Standalone entry point."""
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    sys.exit(run_command(args))


if __name__ == "__main__":
    main()
