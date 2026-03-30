#!/usr/bin/env python3
"""M5 timeframe autoresearch — Karpathy-style evolutionary optimization.

Loads M5+H1 snapshot data, runs evolutionary autoresearch with M5-specific
parameter bounds, validates top results with walk-forward analysis, and saves
the best config as YAML for deployment.

Usage:
    python scripts/run_m5_autoresearch.py --snapshot-id <ID> [options]

    # List available snapshots
    python scripts/run_m5_autoresearch.py --list-snapshots

    # Full run with defaults
    python scripts/run_m5_autoresearch.py --snapshot-id abc123 --population 60 --generations 50

    # Quick test run
    python scripts/run_m5_autoresearch.py --snapshot-id abc123 --population 20 --generations 10
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Ensure nova-core is on the path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Limit BLAS threads — backtests are single-threaded; extra threads just waste RAM
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

import yaml  # type: ignore[import-untyped]

from novatrade.backtest.environment import BacktestEnvironment
from novatrade.cli.config_schema import StrategyConfig
from novatrade.evaluation.champion_fitness import compute_champion_fitness
from novatrade.optimization.autoresearch import (
    AutoResearchConfig,
    AutoResearchResult,
    Organism,
    run_autoresearch,
)
from novatrade.optimization.walkforward import (
    WalkForwardConfig,
    WalkForwardResult,
    run_walk_forward,
)
from novatrade.storage.data_snapshot import DataSnapshot, list_snapshots, load_snapshot

log = logging.getLogger("m5_autoresearch")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_SNAPSHOT_DIR = Path("/home/nova/nova-core/data/snapshots")
DEFAULT_OUTPUT_DIR = Path("/home/nova/nova-core/data/configs")

# M5 walk-forward config: windows sized for M5 bar counts
# 52560 = ~6 months M5 (288 bars/day * 182.5 days)
# 17520 = ~2 months M5 (288 bars/day * 60.8 days)
# 8760  = ~1 month M5  (288 bars/day * 30.4 days)
# 26280 = ~3 months M5 holdout
M5_WALKFORWARD_CONFIG = WalkForwardConfig(
    train_bars=52560,
    test_bars=17520,
    step_bars=8760,
    embargo_bars=60,  # ~5 hours gap at M5 resolution
    holdout_bars=26280,
    min_oos_is_ratio=0.6,
    min_holdout_ratio=0.7,
    timeframe_ratio=12,  # M5:H1 = 12:1
)

# Checkpoint file stem
CHECKPOINT_STEM = "m5_autoresearch_checkpoint"


# ---------------------------------------------------------------------------
# Interrupt handler — save best-so-far on Ctrl-C
# ---------------------------------------------------------------------------

_interrupted = False
_best_so_far: Organism | None = None
_output_dir: Path = DEFAULT_OUTPUT_DIR


def _handle_interrupt(signum: int, frame: object) -> None:
    """Signal handler: set flag so main loop can save and exit gracefully."""
    global _interrupted
    _interrupted = True
    log.warning("Interrupt received — will save best-so-far and exit after current generation.")


# ---------------------------------------------------------------------------
# Snapshot discovery
# ---------------------------------------------------------------------------


def find_m5_snapshots(snapshot_dir: Path) -> list[DataSnapshot]:
    """Return snapshots whose primary timeframe is M5."""
    all_snaps = list_snapshots(snapshot_dir)
    return [s for s in all_snaps if s.primary_tf == "M5"]


def print_available_snapshots(snapshot_dir: Path) -> None:
    """Print all available snapshots, highlighting M5 ones."""
    all_snaps = list_snapshots(snapshot_dir)
    if not all_snaps:
        print(f"No snapshots found in {snapshot_dir}")
        return

    print(f"\nAvailable snapshots in {snapshot_dir}:")
    print(f"{'ID':<20} {'TF':<10} {'Symbol':<10} {'Bars':<15} {'Date Range':<25} {'M5?'}")
    print("-" * 90)
    for s in all_snaps:
        is_m5 = s.primary_tf == "M5"
        marker = " <-- M5" if is_m5 else ""
        print(
            f"{s.snapshot_id:<20} {s.primary_tf}/{s.higher_tf:<7} {s.symbol:<10} "
            f"{s.primary_bars:>6}+{s.higher_bars:<6} "
            f"{s.date_range_start} -> {s.date_range_end}{marker}"
        )


# ---------------------------------------------------------------------------
# Organism -> StrategyConfig conversion
# ---------------------------------------------------------------------------


def organism_to_strategy_config(org: Organism) -> StrategyConfig:
    """Convert an Organism's params+features into a StrategyConfig."""
    config_kwargs = copy.deepcopy(org.params)
    config_kwargs["use_ema_stack_filter"] = org.features.get("use_ema_stack_filter", False)
    config_kwargs["ema_confirm_bars"] = org.features.get("ema_confirm_bars", 0)
    config_kwargs["session_filter"] = org.features.get("session_filter")
    config_kwargs["use_volatility_filter"] = org.features.get("use_volatility_filter", False)
    return StrategyConfig.model_validate(config_kwargs)


# ---------------------------------------------------------------------------
# YAML output
# ---------------------------------------------------------------------------


def save_champion_yaml(
    org: Organism,
    wf_result: WalkForwardResult | None,
    output_dir: Path,
    *,
    generations: int,
    population: int,
    snapshot_id: str,
) -> Path:
    """Save the champion organism as a deployment-ready YAML config.

    Format is compatible with StrategyConfig.from_yaml().
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / "irb_m5_champion.yaml"

    config = organism_to_strategy_config(org)

    # Build the full output dict
    data: dict = {
        "strategy": "IRB",
        "version": "m5_optimized",
        "timeframe": {
            "primary": "M5",
            "higher": "H1",
        },
        # All strategy parameters (what StrategyConfig.from_yaml reads)
        "parameters": config.to_environment_kwargs(),
        # Features / discrete toggles
        "features": {
            "use_ema_stack_filter": org.features.get("use_ema_stack_filter", False),
            "session_filter": org.features.get("session_filter"),
            "trail_mode": org.features.get("trail_mode", "atr"),
            "use_breakeven": org.features.get("use_breakeven", False),
            "use_volatility_filter": org.features.get("use_volatility_filter", False),
            "use_circuit_breaker": org.features.get("use_circuit_breaker", False),
            "use_trail_delay": org.features.get("use_trail_delay", False),
        },
        # Optimization metadata
        "optimization": {
            "method": "evolutionary",
            "generations": generations,
            "population": population,
            "in_sample_scout_score": round(org.scout_score, 6),
            "in_sample_profit_factor": round(org.profit_factor, 4),
            "in_sample_trade_count": org.trade_count,
            "in_sample_net_pips": round(org.net_pips, 1),
            "in_sample_max_dd_pct": round(org.max_dd_pct, 2),
            "snapshot_id": snapshot_id,
            "optimized_at": datetime.now(tz=timezone.utc).isoformat(),
        },
    }

    # Add walk-forward results if available
    if wf_result is not None:
        data["optimization"]["walk_forward_oos_sharpe"] = round(wf_result.median_oos_score, 6)
        data["optimization"]["walk_forward_is_score"] = round(wf_result.median_is_score, 6)
        data["optimization"]["walk_forward_oos_is_ratio"] = round(wf_result.median_oos_is_ratio, 4)
        data["optimization"]["walk_forward_holdout_score"] = (
            round(wf_result.holdout_score, 6) if wf_result.holdout_score is not None else None
        )
        data["optimization"]["walk_forward_passed"] = wf_result.overall_passed
        data["optimization"]["walk_forward_windows"] = len(wf_result.windows)

    # Convert numpy types to native Python for YAML serialization
    def _to_native(obj):
        if hasattr(obj, "item"):  # numpy scalar
            return obj.item()
        if isinstance(obj, dict):
            return {_to_native(k): _to_native(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [_to_native(v) for v in obj]
        return obj

    data = _to_native(data)

    with out_path.open("w") as f:
        yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)

    return out_path


# ---------------------------------------------------------------------------
# Checkpoint save/load for crash recovery
# ---------------------------------------------------------------------------


def save_checkpoint(
    result: AutoResearchResult,
    output_dir: Path,
    generation: int,
) -> Path:
    """Save intermediate autoresearch results as JSON checkpoint."""
    output_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_path = output_dir / f"{CHECKPOINT_STEM}_gen{generation:04d}.json"

    data = {
        "generation": generation,
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        "best_scout_score": result.best.scout_score if result.best else 0.0,
        "best_profit_factor": result.best.profit_factor if result.best else 0.0,
        "best_trade_count": result.best.trade_count if result.best else 0,
        "best_net_pips": result.best.net_pips if result.best else 0.0,
        "viable_count": result.viable_count,
        "total_backtests": result.total_backtests,
        "elapsed_seconds": result.elapsed_seconds,
        "best_params": result.best.params if result.best else {},
        "best_features": result.best.features if result.best else {},
        "history": result.history,
        "top_10": [
            {
                "organism_id": o.organism_id,
                "scout_score": o.scout_score,
                "profit_factor": o.profit_factor,
                "trade_count": o.trade_count,
                "net_pips": o.net_pips,
                "max_dd_pct": o.max_dd_pct,
                "params": o.params,
                "features": o.features,
            }
            for o in result.top_10
        ],
    }

    checkpoint_path.write_text(json.dumps(data, indent=2))
    return checkpoint_path


# ---------------------------------------------------------------------------
# Walk-forward validation of top organisms
# ---------------------------------------------------------------------------


def validate_top_organisms(
    organisms: list[Organism],
    m5_candles: list,
    h1_candles: list,
    m5_env: BacktestEnvironment,
    wf_config: WalkForwardConfig,
) -> list[tuple[Organism, WalkForwardResult]]:
    """Run walk-forward validation on a list of organisms.

    Returns list of (organism, wf_result) tuples sorted by OOS performance.
    """
    results: list[tuple[Organism, WalkForwardResult]] = []

    for i, org in enumerate(organisms):
        log.info(
            "Walk-forward validating organism %d/%d (score=%.4f, PF=%.2f, trades=%d)",
            i + 1,
            len(organisms),
            org.scout_score,
            org.profit_factor,
            org.trade_count,
        )

        try:
            config = organism_to_strategy_config(org)
            wf_result = run_walk_forward(
                config=config,
                h1_candles=m5_candles,  # primary TF candles (M5 in this case)
                h4_candles=h1_candles,  # higher TF candles (H1 in this case)
                wf_config=wf_config,
                base_environment=m5_env,
            )

            degradation = wf_result.median_oos_is_ratio
            log.info(
                "  -> OOS score=%.4f, IS score=%.4f, OOS/IS ratio=%.4f, holdout=%.4f, passed=%s",
                wf_result.median_oos_score,
                wf_result.median_is_score,
                degradation,
                wf_result.holdout_score if wf_result.holdout_score is not None else 0.0,
                wf_result.overall_passed,
            )
            results.append((org, wf_result))

        except Exception as exc:
            log.warning("Walk-forward failed for organism %s: %s", org.organism_id, exc)
            continue

    # Sort by OOS score (what matters for live trading), not IS score
    results.sort(key=lambda x: x[1].median_oos_score, reverse=True)
    return results


# ---------------------------------------------------------------------------
# Results printing
# ---------------------------------------------------------------------------


def print_evolution_summary(result: AutoResearchResult) -> None:
    """Print generation-by-generation best fitness progression."""
    print("\n" + "=" * 80)
    print("EVOLUTION SUMMARY")
    print("=" * 80)
    print(f"Generations run: {result.generations_run}")
    print(f"Total backtests: {result.total_backtests}")
    print(f"Elapsed: {result.elapsed_seconds:.1f}s")
    print(f"Viable organisms (final gen): {result.viable_count}")
    print()

    if result.history:
        hdr = (
            f"{'Gen':>4} {'Best Score':>11} {'Best PF':>8} {'Trades':>7} "
            f"{'Pips':>8} {'Viable':>7} {'Median':>8} {'Ever Best':>10}"
        )
        print(hdr)
        print("-" * 75)
        for h in result.history:
            print(
                f"{h['generation']:>4} {h['best_score']:>11.4f} {h['best_pf']:>8.2f} "
                f"{h['best_trades']:>7} {h['best_pips']:>+8.0f} "
                f"{h['viable_count']:>7} {h['median_score']:>8.4f} {h['best_ever_score']:>10.4f}"
            )


def print_top_configs(
    result: AutoResearchResult,
    wf_results: list[tuple[Organism, WalkForwardResult]] | None = None,
) -> None:
    """Print detailed top configs with IS and OOS metrics."""
    print("\n" + "=" * 80)
    print("TOP CONFIGS (by in-sample scout score)")
    print("=" * 80)

    for i, org in enumerate(result.top_10[:10]):
        features_str = (
            f"stack={'Y' if org.features.get('use_ema_stack_filter') else 'N'} "
            f"trail={org.features.get('trail_mode', '?')} "
            f"session={org.features.get('session_filter', 'all') or 'all'} "
            f"BE={'Y' if org.features.get('use_breakeven') else 'N'} "
            f"vol={'Y' if org.features.get('use_volatility_filter') else 'N'} "
            f"CB={'Y' if org.features.get('use_circuit_breaker') else 'N'} "
            f"delay={'Y' if org.features.get('use_trail_delay') else 'N'}"
        )
        print(f"\n  #{i + 1}: {org.organism_id}")
        print(
            f"      Scout={org.scout_score:.4f}  PF={org.profit_factor:.2f}  "
            f"Trades={org.trade_count}  Pips={org.net_pips:+.0f}  DD={org.max_dd_pct:.1f}%"
        )
        print(f"      Features: {features_str}")
        print(
            f"      Key params: IRB={org.params.get('irb_threshold', '?'):.3f}  "
            f"EMA={org.params.get('ema_period', '?')}  "
            f"ATR={org.params.get('atr_period', '?')}  "
            f"Trail={org.params.get('trail_atr_multiplier', '?'):.2f}  "
            f"TrigWin={org.params.get('trigger_window_bars', '?')}  "
            f"TimeStop={org.params.get('time_stop_bars', '?')}  "
            f"Warmup={org.params.get('warmup_bars', '?')}"
        )

    if wf_results:
        print("\n" + "=" * 80)
        print("WALK-FORWARD VALIDATION RESULTS (ranked by OOS performance)")
        print("=" * 80)

        for i, (org, wf) in enumerate(wf_results):
            passed_str = "PASS" if wf.overall_passed else "FAIL"
            holdout_str = f"{wf.holdout_score:.4f}" if wf.holdout_score is not None else "N/A"
            holdout_ratio_str = f"{wf.holdout_is_ratio:.4f}" if wf.holdout_is_ratio is not None else "N/A"
            degradation = wf.median_oos_is_ratio

            print(f"\n  #{i + 1}: {org.organism_id}  [{passed_str}]")
            print(f"      IS  score (median): {wf.median_is_score:.4f}")
            print(f"      OOS score (median): {wf.median_oos_score:.4f}")
            overfit = "(acceptable)" if degradation >= 0.6 else "(OVERFITTING)"
            print(f"      OOS/IS ratio:       {degradation:.4f}  {overfit}")
            print(f"      Holdout score:      {holdout_str}")
            print(f"      Holdout/IS ratio:   {holdout_ratio_str}")
            passed_ct = sum(1 for w in wf.windows if w.passed)
            print(f"      Windows:            {len(wf.windows)} ({passed_ct}/{len(wf.windows)} passed)")

            # Per-window detail
            for w in wf.windows:
                wp = "PASS" if w.passed else "FAIL"
                print(
                    f"        W{w.window_index}: IS={w.is_scout_score:.4f} "
                    f"OOS={w.oos_scout_score:.4f} ratio={w.oos_is_ratio:.4f} [{wp}]"
                )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    """Build CLI argument parser."""
    parser = argparse.ArgumentParser(
        description="M5 Timeframe Autoresearch — Karpathy-style evolutionary optimization",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # List available snapshots
  python scripts/run_m5_autoresearch.py --list-snapshots

  # Run with defaults
  python scripts/run_m5_autoresearch.py --snapshot-id abc123

  # Quick test
  python scripts/run_m5_autoresearch.py --snapshot-id abc123 --population 20 --generations 10 --top-n 2

  # Production run with higher population
  python scripts/run_m5_autoresearch.py --snapshot-id abc123 --population 100 --generations 80 --seed 7
""",
    )

    parser.add_argument(
        "--snapshot-dir", type=Path, default=DEFAULT_SNAPSHOT_DIR, help="Directory containing snapshots"
    )
    parser.add_argument(
        "--snapshot-id", type=str, default=None, help="Snapshot ID to load (required unless --list-snapshots)"
    )
    parser.add_argument("--list-snapshots", action="store_true", help="List available snapshots and exit")
    parser.add_argument("--population", type=int, default=60, help="Population size per generation (default: 60)")
    parser.add_argument("--generations", type=int, default=50, help="Number of generations (default: 50)")
    parser.add_argument("--seed", type=int, default=42, help="Random seed (default: 42)")
    parser.add_argument(
        "--top-n", type=int, default=5, help="How many top results to walk-forward validate (default: 5)"
    )
    parser.add_argument(
        "--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="Output directory for champion YAML"
    )
    parser.add_argument(
        "--skip-walkforward", action="store_true", help="Skip walk-forward validation (faster, less reliable)"
    )
    parser.add_argument(
        "--checkpoint-every", type=int, default=10, help="Save checkpoint every N generations (default: 10)"
    )
    parser.add_argument("--min-trades", type=int, default=50, help="Minimum trades for viable strategy (default: 50)")
    parser.add_argument(
        "--target-score", type=float, default=0.25, help="Early stop if scout score hits this (default: 0.25)"
    )
    parser.add_argument("-v", "--verbose", action="store_true", default=True, help="Verbose output (default: True)")
    parser.add_argument("-q", "--quiet", action="store_true", help="Suppress per-generation output")
    return parser


def _select_champion(
    result: AutoResearchResult,
    wf_results: list[tuple[Organism, WalkForwardResult]] | None,
) -> tuple[Organism | None, WalkForwardResult | None]:
    """Pick the best organism, preferring walk-forward validated ones."""
    champion = result.best
    champion_wf: WalkForwardResult | None = None

    if wf_results:
        passing = [(org, wf) for org, wf in wf_results if wf.overall_passed]
        if passing:
            champion, champion_wf = passing[0]
            log.info(
                "Champion (WF-validated): %s OOS=%.4f IS=%.4f ratio=%.4f",
                champion.organism_id,
                champion_wf.median_oos_score,
                champion_wf.median_is_score,
                champion_wf.median_oos_is_ratio,
            )
        else:
            champion, champion_wf = wf_results[0]
            log.warning(
                "No organism passed walk-forward validation. "
                "Using best OOS: %s (OOS=%.4f, ratio=%.4f). "
                "CAUTION: this config may be overfit.",
                champion.organism_id,
                champion_wf.median_oos_score,
                champion_wf.median_oos_is_ratio,
            )

    return champion, champion_wf


def main() -> int:
    parser = _build_parser()

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    # --- List snapshots mode ---
    if args.list_snapshots:
        print_available_snapshots(args.snapshot_dir)
        m5_snaps = find_m5_snapshots(args.snapshot_dir)
        if m5_snaps:
            print(f"\nM5 snapshots available: {len(m5_snaps)}")
        else:
            print("\nNo M5 snapshots found. Run Phase 2 (data fetcher) first.")
        return 0

    # --- Validate args ---
    if not args.snapshot_id:
        print("ERROR: --snapshot-id is required. Use --list-snapshots to see available IDs.")
        return 1

    # --- Register interrupt handler ---
    global _output_dir
    _output_dir = args.output_dir
    signal.signal(signal.SIGINT, _handle_interrupt)

    # --- Load snapshot ---
    log.info("Loading snapshot %s from %s ...", args.snapshot_id, args.snapshot_dir)
    try:
        primary_candles, higher_candles, snapshot_meta = load_snapshot(args.snapshot_id, args.snapshot_dir)
    except FileNotFoundError as exc:
        print(f"\nERROR: Snapshot not found: {exc}")
        print()
        print_available_snapshots(args.snapshot_dir)
        return 1
    except ValueError as exc:
        print(f"\nERROR: Snapshot data issue: {exc}")
        return 1

    # Validate this is M5 data
    if snapshot_meta.primary_tf != "M5":
        log.warning(
            "Snapshot primary TF is %s, not M5. Proceeding anyway but results "
            "may not be meaningful for M5 optimization.",
            snapshot_meta.primary_tf,
        )

    m5_candles = primary_candles
    h1_candles = higher_candles

    log.info(
        "Loaded %d M5 + %d H1 candles (%s -> %s, %s)",
        len(m5_candles),
        len(h1_candles),
        snapshot_meta.date_range_start,
        snapshot_meta.date_range_end,
        snapshot_meta.symbol,
    )

    # --- Data sufficiency check ---
    min_m5_bars = (
        M5_WALKFORWARD_CONFIG.train_bars + M5_WALKFORWARD_CONFIG.test_bars + M5_WALKFORWARD_CONFIG.holdout_bars
    )
    if len(m5_candles) < min_m5_bars and not args.skip_walkforward:
        log.warning(
            "Only %d M5 bars available, walk-forward needs at least %d. "
            "Consider --skip-walkforward or acquiring more data.",
            len(m5_candles),
            min_m5_bars,
        )

    # --- Create M5 environment ---
    m5_env = BacktestEnvironment.for_m5()
    log.info(
        "M5 environment: primary=%s, higher=%s, ratio=%d, bars/day=%d",
        m5_env.primary_timeframe,
        m5_env.higher_timeframe,
        m5_env.h1_to_h4_ratio,
        m5_env.h1_bars_per_day,
    )

    # --- Configure autoresearch ---
    ar_config = AutoResearchConfig(
        population_size=args.population,
        generations=args.generations,
        seed=args.seed,
        min_trades=args.min_trades,
        target_scout_score=args.target_score,
        verbose=not args.quiet,
    )

    log.info(
        "Autoresearch config: pop=%d, gens=%d, seed=%d, min_trades=%d, target=%.3f",
        ar_config.population_size,
        ar_config.generations,
        ar_config.seed,
        ar_config.min_trades,
        ar_config.target_scout_score,
    )

    # --- Run evolutionary autoresearch ---
    print("\n" + "=" * 80)
    print(f"M5 AUTORESEARCH  |  pop={args.population}  gens={args.generations}  seed={args.seed}")
    print(f"Data: {len(m5_candles)} M5 bars + {len(h1_candles)} H1 bars")
    print("Bounds: M5_PARAMETER_BOUNDS (bar params scaled 6x from H1)")
    print("=" * 80 + "\n")

    t0 = time.monotonic()

    # We run the autoresearch loop with M5-specific bounds and environment.
    # The run_autoresearch API accepts parameter_bounds and base_environment
    # so it will use M5 ranges for random init and mutation.
    # Champion fitness scorer: ranks by multi-horizon consistency
    # (monthly profitability, drawdown, PF stability, worst rolling 1yr, Calmar)
    # instead of the default scout_score heuristic.
    result = run_autoresearch(
        h1_candles=m5_candles,  # primary TF candles (M5)
        h4_candles=h1_candles,  # higher TF candles (H1)
        config=ar_config,
        parameter_bounds=StrategyConfig.M5_PARAMETER_BOUNDS,  # type: ignore[arg-type]
        base_environment=m5_env,
        fitness_fn=compute_champion_fitness,
    )

    total_elapsed = time.monotonic() - t0
    result.elapsed_seconds = round(total_elapsed, 1)

    # --- Save checkpoint of final state ---
    checkpoint_path = save_checkpoint(result, args.output_dir, result.generations_run)
    log.info("Final checkpoint saved: %s", checkpoint_path)

    # --- Check for interrupt ---
    if _interrupted:
        log.warning("Run was interrupted. Saving best-so-far.")

    # --- Check for no viable results ---
    if result.best is None or result.best.scout_score <= 0:
        print("\n" + "!" * 80)
        print("WARNING: Autoresearch produced no viable results (all negative/zero fitness).")
        print("Possible causes:")
        print("  - Not enough data (M5 needs ~100k+ bars for meaningful optimization)")
        print("  - Parameter bounds too restrictive or too wide")
        print("  - min_trades threshold too high for available data")
        print("  - Strategy fundamentally unprofitable on M5 for this pair/period")
        print("!" * 80)
        print_evolution_summary(result)
        return 1

    # --- Print evolution summary ---
    print_evolution_summary(result)

    # --- Walk-forward validation ---
    wf_results: list[tuple[Organism, WalkForwardResult]] | None = None

    if not args.skip_walkforward and not _interrupted:
        top_organisms = result.top_10[: args.top_n]
        if not top_organisms:
            log.warning("No organisms to validate — skipping walk-forward.")
        else:
            print(f"\n{'=' * 80}")
            print(f"WALK-FORWARD VALIDATION  |  top-{len(top_organisms)} organisms  |  M5 windows")
            print(
                f"  train={M5_WALKFORWARD_CONFIG.train_bars} test={M5_WALKFORWARD_CONFIG.test_bars} "
                f"step={M5_WALKFORWARD_CONFIG.step_bars} holdout={M5_WALKFORWARD_CONFIG.holdout_bars}"
            )
            print(f"{'=' * 80}\n")

            wf_results = validate_top_organisms(
                organisms=top_organisms,
                m5_candles=m5_candles,
                h1_candles=h1_candles,
                m5_env=m5_env,
                wf_config=M5_WALKFORWARD_CONFIG,
            )
    elif args.skip_walkforward:
        log.info("Walk-forward validation skipped (--skip-walkforward).")

    # --- Print top configs ---
    print_top_configs(result, wf_results)

    # --- Determine champion ---
    champion, champion_wf = _select_champion(result, wf_results)
    if champion is None:
        log.error("No champion organism found.")
        return 1

    # --- Save champion YAML ---
    yaml_path = save_champion_yaml(
        org=champion,
        wf_result=champion_wf,
        output_dir=args.output_dir,
        generations=args.generations,
        population=args.population,
        snapshot_id=args.snapshot_id,
    )

    # --- Final summary ---
    print(f"\n{'=' * 80}")
    print("CHAMPION CONFIG SAVED")
    print(f"{'=' * 80}")
    print(f"  File:         {yaml_path}")
    print(f"  Organism:     {champion.organism_id}")
    print(f"  Scout score:  {champion.scout_score:.4f}")
    print(f"  Profit factor:{champion.profit_factor:.2f}")
    print(f"  Trades:       {champion.trade_count}")
    print(f"  Net pips:     {champion.net_pips:+.0f}")
    print(f"  Max DD:       {champion.max_dd_pct:.1f}%")
    if champion_wf:
        print(f"  WF OOS score: {champion_wf.median_oos_score:.4f}")
        print(f"  WF OOS/IS:    {champion_wf.median_oos_is_ratio:.4f}")
        print(f"  WF passed:    {champion_wf.overall_passed}")
    print(f"  Total time:   {total_elapsed:.1f}s ({total_elapsed / 60:.1f} min)")
    print(f"  Backtests:    {result.total_backtests}")
    print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
