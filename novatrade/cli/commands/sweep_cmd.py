"""CLI command for parameter sweep execution.

Wires the sweep engine into the novatrade-bt CLI: loads config and candle
data, runs the parameter sweep, and prints a results table.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import typer

if TYPE_CHECKING:
    from novatrade.optimization.sweep_engine import SweepExperiment, SweepResult

log = logging.getLogger("novatrade.cli.commands.sweep_cmd")


def execute_sweep_command(
    config_path: Path,
    n_experiments: int = 100,
    method: str = "random",
    seed: int | None = None,
    symbol: str = "EURUSD",
    snapshot_id: str = "",
    output_json: bool = False,
    top_n: int = 10,
) -> None:
    """Run a parameter sweep and print results.

    Loads strategy config and candle data, then runs the sweep engine
    with the specified method and experiment count.

    Args:
        config_path: Path to base strategy YAML config.
        n_experiments: Number of experiments to run.
        method: Search method ("random" or "latin_hypercube").
        seed: RNG seed for reproducibility.
        symbol: Symbol for CSV loading (ignored with snapshot_id).
        snapshot_id: Frozen dataset snapshot ID.
        output_json: If True, output JSON instead of table.
        top_n: Number of top results to display.
    """
    from novatrade.cli.commands.data import CANDLE_DIR, SNAPSHOT_DIR, load_candles_csv
    from novatrade.cli.config_schema import StrategyConfig
    from novatrade.optimization.sweep_engine import SweepConfig, run_sweep
    from novatrade.storage.data_snapshot import load_snapshot

    # --- Load config ---
    if not config_path.exists():
        typer.echo(f"Error: config not found: {config_path}", err=True)
        raise typer.Exit(code=1)

    try:
        base_config = StrategyConfig.from_yaml(config_path)
    except Exception as exc:
        typer.echo(f"Error loading config: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(f"Config: {base_config.name} v{base_config.version} [{base_config.content_hash()[:8]}]")

    # --- Load candles ---
    if snapshot_id:
        typer.echo(f"Loading snapshot: {snapshot_id}")
        h1_candles, h4_candles, _meta = load_snapshot(snapshot_id, SNAPSHOT_DIR)
    else:
        h1_path = CANDLE_DIR / f"{symbol}_H1.csv"
        h4_path = CANDLE_DIR / f"{symbol}_H4.csv"
        if not h1_path.exists() or not h4_path.exists():
            typer.echo(
                "Error: candle CSVs not found. Run 'novatrade-bt data fetch' first.",
                err=True,
            )
            raise typer.Exit(code=1)
        h1_candles = load_candles_csv(h1_path, symbol=symbol, timeframe="H1")
        h4_candles = load_candles_csv(h4_path, symbol=symbol, timeframe="H4")

    typer.echo(f"Data: {len(h1_candles):,} H1 + {len(h4_candles):,} H4 bars")

    # --- Run sweep ---
    sweep_cfg = SweepConfig(
        n_experiments=n_experiments,
        method=method,
        seed=seed,
    )

    typer.echo(f"Running {method} sweep: {n_experiments} experiments (seed={seed})...")

    result = run_sweep(base_config, h1_candles, h4_candles, sweep_cfg)

    # --- Output ---
    if output_json:
        _print_json(result, top_n)
    else:
        _print_table(result, top_n)


def _print_table(result: SweepResult, top_n: int) -> None:
    """Print a human-readable results table."""
    typer.echo(f"\n{'=' * 70}")
    typer.echo(f"  Sweep Results  |  {result.total_experiments} experiments  |  {result.elapsed_seconds:.1f}s")
    typer.echo(f"  Ranked: {result.ranked_count}  |  Gated: {result.gated_count}  |  Crashed: {result.crashed_count}")
    typer.echo(f"{'=' * 70}")

    if result.best:
        typer.echo(
            f"\n  Best score: {result.best.scout_score:.4f}  "
            f"(#{result.best.index}, {result.best.trade_count} trades, "
            f"PF={result.best.profit_factor if result.best.profit_factor is not None else 'N/A'})"
        )

    # Top-N table
    shown = [e for e in result.experiments if e.status == "ranked"][:top_n]
    if not shown:
        typer.echo("\n  No ranked experiments.")
        return

    typer.echo(f"\n  {'Rank':<6}{'Score':<10}{'Trades':<10}{'PF':<10}{'Status':<10}")
    typer.echo(f"  {'-' * 46}")
    for rank, exp in enumerate(shown, 1):
        pf_str = f"{exp.profit_factor:.2f}" if exp.profit_factor is not None else "N/A"
        typer.echo(f"  {rank:<6}{exp.scout_score:<10.4f}{exp.trade_count:<10}{pf_str:<10}{exp.status:<10}")

    # Best config params
    if result.best:
        typer.echo("\n  Best config parameters:")
        for k, v in sorted(result.best.config.items()):
            typer.echo(f"    {k}: {v}")

    typer.echo()


def _print_json(result: SweepResult, top_n: int) -> None:
    """Print results as JSON."""
    top = result.experiments[:top_n]
    output = {
        "total_experiments": result.total_experiments,
        "ranked_count": result.ranked_count,
        "gated_count": result.gated_count,
        "crashed_count": result.crashed_count,
        "elapsed_seconds": result.elapsed_seconds,
        "best": _exp_to_dict(result.best) if result.best else None,
        "top_experiments": [_exp_to_dict(e) for e in top],
    }
    typer.echo(json.dumps(output, indent=2))


def _exp_to_dict(exp: SweepExperiment) -> dict:
    """Convert a SweepExperiment to a serialisable dict."""
    return {
        "index": exp.index,
        "scout_score": exp.scout_score,
        "trade_count": exp.trade_count,
        "profit_factor": exp.profit_factor,
        "status": exp.status,
        "config": exp.config,
    }
