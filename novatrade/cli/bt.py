"""NovaTrade Backtesting & AutoResearch CLI.

Usage:
    novatrade-bt run --config configs/strategies/irb_baseline.yaml
    novatrade-bt run --config configs/strategies/irb_baseline.yaml --snapshot <id> --json
    novatrade-bt data fetch --symbol EURUSD --days 730
    novatrade-bt data freeze --symbol EURUSD
    novatrade-bt data list
    novatrade-bt sweep --config ... --experiments 100
    novatrade-bt campaign start --config ... --doctrine configs/strategies/doctrine.md
    novatrade-bt campaign status
    novatrade-bt campaign stop
    novatrade-bt walkforward --config ...
    novatrade-bt leaderboard
    novatrade-bt promote --experiment-id <id>
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import typer

if TYPE_CHECKING:
    from novatrade.config import NovaTradeCfg

from novatrade.cli.commands.data import (
    CANDLE_DIR,
    SNAPSHOT_DIR,
    aggregate_h1_to_h4,
    freeze_candles,
    generate_synthetic_candles,
    load_candles_csv,
    save_candles_csv,
)
from novatrade.storage.data_snapshot import list_snapshots, load_snapshot

# ---------------------------------------------------------------------------
# App & sub-apps
# ---------------------------------------------------------------------------

app = typer.Typer(
    name="novatrade-bt",
    help="NovaTrade Backtesting & AutoResearch Engine v2",
    no_args_is_help=True,
)
data_app = typer.Typer(help="Historical data management", no_args_is_help=True)
campaign_app = typer.Typer(help="AutoResearch campaign management", no_args_is_help=True)
doctrine_app = typer.Typer(help="Strategy doctrine management", no_args_is_help=True)
pipeline_app = typer.Typer(help="End-to-end strategy pipeline", no_args_is_help=True)
app.add_typer(data_app, name="data")
app.add_typer(campaign_app, name="campaign")
app.add_typer(doctrine_app, name="doctrine")
app.add_typer(pipeline_app, name="pipeline")


# ---------------------------------------------------------------------------
# data subcommands
# ---------------------------------------------------------------------------


@data_app.command("fetch")
def data_fetch(
    symbol: str = typer.Option("EURUSD", help="Instrument symbol"),
    days: int = typer.Option(730, help="Number of days of data to generate"),
    timeframe: str = typer.Option("H1", help="Primary timeframe (H1 candles + H4 derived)"),
    seed: int = typer.Option(42, help="RNG seed for reproducibility"),
) -> None:
    """Fetch (generate synthetic) historical candle data.

    Generates H1 candles via random walk and derives H4 candles by aggregation.
    Saves both as CSV in data/candles/.
    """
    typer.echo(f"Generating {days} days of synthetic {symbol} data (seed={seed})...")

    # Generate H1 candles
    h1_candles = generate_synthetic_candles(
        symbol=symbol,
        timeframe="H1",
        days=days,
        seed=seed,
    )

    # Derive H4 from H1 (ensures consistency)
    h4_candles = aggregate_h1_to_h4(h1_candles)

    # Save CSVs
    h1_path = CANDLE_DIR / f"{symbol}_H1.csv"
    h4_path = CANDLE_DIR / f"{symbol}_H4.csv"

    h1_count = save_candles_csv(h1_candles, h1_path)
    h4_count = save_candles_csv(h4_candles, h4_path)

    # Summary
    first_ts = h1_candles[0].timestamp
    last_ts = h1_candles[-1].timestamp
    from datetime import datetime, timezone

    start_dt = datetime.fromtimestamp(first_ts, tz=timezone.utc).strftime("%Y-%m-%d")
    end_dt = datetime.fromtimestamp(last_ts, tz=timezone.utc).strftime("%Y-%m-%d")

    typer.echo(f"  Symbol:    {symbol}")
    typer.echo(f"  Period:    {start_dt} to {end_dt}")
    typer.echo(f"  H1 bars:   {h1_count:,}")
    typer.echo(f"  H4 bars:   {h4_count:,}")
    typer.echo(f"  H1 file:   {h1_path}")
    typer.echo(f"  H4 file:   {h4_path}")
    typer.echo(f"  Open:      {h1_candles[0].open:.5f}")
    typer.echo(f"  Close:     {h1_candles[-1].close:.5f}")
    typer.echo("Done.")


@data_app.command("fetch-real")
def data_fetch_real(
    symbol: str = typer.Option("EURUSD", help="Instrument symbol"),
    days: int = typer.Option(730, help="Number of days of history to fetch"),
    no_snapshot: bool = typer.Option(False, help="Skip creating a frozen snapshot"),
) -> None:
    """Fetch real historical candle data from MetaApi (FTMO/broker).

    Connects to MetaApi, paginates the historical candle API to fetch
    H1 data for the specified date range, derives H4 by aggregation,
    saves both as CSV, and optionally creates a reproducible Parquet snapshot.
    """
    import asyncio

    from novatrade.data.historical_fetcher import fetch_and_save

    typer.echo(f"Fetching {days} days of real {symbol} data via MetaApi...")

    result = asyncio.run(
        fetch_and_save(
            symbol=symbol,
            days=days,
            snapshot=not no_snapshot,
            verbose=True,
        )
    )

    typer.echo(f"\n  Symbol:      {symbol}")
    typer.echo(f"  Period:      {result['start_date'][:10]} to {result['end_date'][:10]}")
    typer.echo(f"  H1 bars:     {result['h1_count']:,}")
    typer.echo(f"  H4 bars:     {result['h4_count']:,}")
    typer.echo(f"  H1 file:     {result['h1_path']}")
    typer.echo(f"  H4 file:     {result['h4_path']}")
    if "snapshot_id" in result:
        typer.echo(f"  Snapshot ID: {result['snapshot_id']}")
    typer.echo("Done — real data ready for backtesting.")


@data_app.command("freeze")
def data_freeze(
    symbol: str = typer.Option("EURUSD", help="Symbol to freeze"),
    candle_dir: Path = typer.Option(CANDLE_DIR, help="Directory with CSV candles"),  # noqa: B008
    snapshot_dir: Path = typer.Option(SNAPSHOT_DIR, help="Directory for Parquet snapshots"),  # noqa: B008
) -> None:
    """Freeze current candle cache as an immutable Parquet snapshot."""
    typer.echo(f"Freezing {symbol} candles from {candle_dir}...")

    snapshot_id = freeze_candles(
        symbol=symbol,
        candle_dir=candle_dir,
        snapshot_dir=snapshot_dir,
    )

    typer.echo(f"  Snapshot ID: {snapshot_id}")
    typer.echo(f"  Location:    {snapshot_dir}/{snapshot_id}_*.parquet")
    typer.echo("Snapshot frozen. Use this ID for reproducible backtests.")


@data_app.command("list")
def data_list(
    snapshot_dir: Path = typer.Option(SNAPSHOT_DIR, help="Directory to scan"),  # noqa: B008
) -> None:
    """List all available frozen data snapshots."""
    snapshots = list_snapshots(snapshot_dir)

    if not snapshots:
        typer.echo("No snapshots found.")
        return

    typer.echo(f"Found {len(snapshots)} snapshot(s):\n")
    for s in snapshots:
        typer.echo(f"  [{s.snapshot_id}]  {s.symbol}  {s.primary_tf}/{s.higher_tf}")
        typer.echo(f"    Bars: {s.primary_bars:,} / {s.higher_bars:,}")
        typer.echo(f"    Range: {s.date_range_start} to {s.date_range_end}")
        typer.echo(f"    Created: {s.created_at}")
        typer.echo()


# ---------------------------------------------------------------------------
# run command
# ---------------------------------------------------------------------------


@app.command("run")
def run_backtest(
    config: Path = typer.Option(  # noqa: B008
        "configs/strategies/irb_baseline.yaml",
        "--config",
        "-c",
        help="Path to strategy YAML config",
    ),
    snapshot_id: str = typer.Option(
        "",
        "--snapshot",
        "-s",
        help="Frozen dataset snapshot ID (loads from data/snapshots/)",
    ),
    output_json: bool = typer.Option(
        False,
        "--json",
        help="Output result as JSON instead of markdown report",
    ),
    symbol: str = typer.Option(
        "EURUSD",
        help="Symbol (used when loading from CSV, ignored with --snapshot)",
    ),
    db_path: Path = typer.Option(  # noqa: B008
        Path("/home/nova/nova-core/data/experiments.db"),
        "--db",
        help="Path to experiment SQLite database",
    ),
    campaign_id: str = typer.Option(
        "",
        "--campaign",
        help="Campaign ID to associate this run with",
    ),
) -> None:
    """Run a single backtest with gated evaluation.

    Loads candle data (from snapshot or CSV), applies the strategy config,
    runs the IRB backtester, evaluates gates A/B, computes scout score,
    and prints a report.
    """
    from novatrade.cli.commands.report import format_run_report
    from novatrade.cli.commands.run import execute_backtest
    from novatrade.cli.config_schema import StrategyConfig
    from novatrade.storage.data_snapshot import fingerprint_candles
    from novatrade.storage.experiment_db import ExperimentDB

    # --- Load strategy config ---
    config_path = Path(config)
    if not config_path.exists():
        typer.echo(f"Error: config file not found: {config_path}", err=True)
        raise typer.Exit(code=1)

    try:
        strategy_config = StrategyConfig.from_yaml(config_path)
    except Exception as exc:
        typer.echo(f"Error loading config: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(f"Config: {strategy_config.name} v{strategy_config.version} [{strategy_config.content_hash()[:8]}]")

    # --- Load candle data ---
    if snapshot_id:
        # Load from frozen Parquet snapshot
        typer.echo(f"Loading snapshot: {snapshot_id}")
        try:
            h1_candles, h4_candles, snap_meta = load_snapshot(snapshot_id, SNAPSHOT_DIR)
        except FileNotFoundError as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        except ValueError as exc:
            typer.echo(f"Error: snapshot corrupted — {exc}", err=True)
            raise typer.Exit(code=1) from exc

        dataset_hash = snapshot_id
        typer.echo(
            f"  Loaded {len(h1_candles):,} H1 + {len(h4_candles):,} H4 bars "
            f"({snap_meta.date_range_start} to {snap_meta.date_range_end})"
        )
    else:
        # Load from CSV files
        h1_path = CANDLE_DIR / f"{symbol}_H1.csv"
        h4_path = CANDLE_DIR / f"{symbol}_H4.csv"

        if not h1_path.exists() or not h4_path.exists():
            typer.echo(
                f"Error: candle CSVs not found at {CANDLE_DIR}/{symbol}_H1.csv. Run 'novatrade-bt data fetch' first.",
                err=True,
            )
            raise typer.Exit(code=1)

        typer.echo(f"Loading candles from CSV: {symbol}")
        h1_candles = load_candles_csv(h1_path, symbol=symbol, timeframe="H1")
        h4_candles = load_candles_csv(h4_path, symbol=symbol, timeframe="H4")

        # Compute dataset hash from candle fingerprints
        import hashlib

        combined = hashlib.sha256()
        combined.update(fingerprint_candles(h1_candles).encode())
        combined.update(fingerprint_candles(h4_candles).encode())
        dataset_hash = combined.hexdigest()[:16]

        typer.echo(f"  Loaded {len(h1_candles):,} H1 + {len(h4_candles):,} H4 bars")

    # --- Open DB (optional) ---
    db = None
    if db_path:
        try:
            db = ExperimentDB(db_path)
        except Exception as exc:
            typer.echo(f"Warning: could not open DB at {db_path}: {exc}", err=True)

    # --- Execute backtest ---
    typer.echo("Running backtest...")
    result = execute_backtest(
        config=strategy_config,
        h1_candles=h1_candles,
        h4_candles=h4_candles,
        campaign_id=campaign_id or None,
        dataset_hash=dataset_hash,
        db=db,
    )

    if db is not None:
        db.close()

    # --- Output ---
    if output_json:
        output = {
            "experiment_id": result.experiment_id,
            "status": result.status,
            "scout_score": result.scout_score,
            "gates_passed": result.gate_results.passed,
            "gate_failed_at": result.gate_results.failed_at,
            "trade_count": (result.metrics.total_completed_trades if result.metrics else 0),
            "profit_factor": (result.metrics.profit_factor if result.metrics else None),
            "max_drawdown_pct": (result.metrics.max_drawdown_pct if result.metrics else None),
            "notes": result.notes,
        }
        typer.echo(json.dumps(output, indent=2))
    else:
        report = format_run_report(result)
        typer.echo(report)

    # Exit code reflects status
    if result.status == "crashed":
        raise typer.Exit(code=2)


# ---------------------------------------------------------------------------
# live command (full-Python pipeline, no TradingView)
# ---------------------------------------------------------------------------


async def _run_live(
    cfg: NovaTradeCfg,
    poll_interval: float,
    shadow: bool,
    dry_run: bool,
) -> None:
    from novatrade.runtime.runner import build_live_stack

    live_loop = await build_live_stack(
        cfg=cfg,
        poll_interval=poll_interval,
        shadow=shadow,
        dry_run=dry_run,
    )

    typer.echo(f"Live loop ready — symbols={cfg.symbols} timeframe={cfg.timeframes[0]} shadow={shadow}")
    typer.echo("Starting... (Ctrl+C to stop)")

    try:
        await live_loop.run()
    except KeyboardInterrupt:
        live_loop.stop()


@app.command("live")
def run_live(
    symbols: str = typer.Option("EURUSD", help="Comma-separated symbols"),
    timeframe: str = typer.Option("H1", help="Primary timeframe"),
    poll_interval: float = typer.Option(2.0, help="Tick poll interval in seconds"),
    shadow: bool = typer.Option(False, help="Shadow mode — log signals, no orders"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Use DryRunAdapter"),
) -> None:
    """Start the live trading loop (full Python pipeline, no TradingView).

    Connects to MetaApi, pre-seeds historical data, and runs the
    three-loop async orchestrator: tick pipeline, order execution,
    and health monitoring.
    """
    import asyncio
    import logging

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    from novatrade.config import NovaTradeCfg

    cfg = NovaTradeCfg.load()
    cfg.symbols = [s.strip() for s in symbols.split(",")]
    cfg.timeframes = [timeframe]

    try:
        asyncio.run(_run_live(cfg, poll_interval, shadow, dry_run))
    except RuntimeError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    except KeyboardInterrupt:
        typer.echo("\nShutdown requested.")


# ---------------------------------------------------------------------------
# sweep command
# ---------------------------------------------------------------------------


@app.command("sweep")
def sweep(
    config: Path = typer.Option(..., "--config", "-c", help="Base strategy YAML config"),  # noqa: B008
    experiments: int = typer.Option(100, "--experiments", "-n", help="Number of experiments"),
    method: str = typer.Option("random", help="Search method: random | latin_hypercube"),
    seed: int = typer.Option(None, help="RNG seed for reproducibility"),
    symbol: str = typer.Option("EURUSD", help="Symbol for CSV loading"),
    snapshot_id: str = typer.Option("", "--snapshot", "-s", help="Frozen snapshot ID"),
    output_json: bool = typer.Option(False, "--json", help="Output as JSON"),
    top_n: int = typer.Option(10, "--top", help="Number of top results to show"),
) -> None:
    """Run a parameter sweep over strategy config space."""
    from novatrade.cli.commands.sweep_cmd import execute_sweep_command

    execute_sweep_command(
        config_path=Path(config),
        n_experiments=experiments,
        method=method,
        seed=seed,
        symbol=symbol,
        snapshot_id=snapshot_id,
        output_json=output_json,
        top_n=top_n,
    )


# ---------------------------------------------------------------------------
# walkforward command
# ---------------------------------------------------------------------------


@app.command("walkforward")
def walkforward(
    config: Path = typer.Option(..., "--config", "-c", help="Strategy YAML config"),  # noqa: B008
    symbol: str = typer.Option("EURUSD", help="Symbol for CSV loading"),
    snapshot_id: str = typer.Option("", "--snapshot", "-s", help="Frozen snapshot ID"),
    train_bars: int = typer.Option(4380, help="Training window size in H1 bars"),
    test_bars: int = typer.Option(1460, help="Test window size in H1 bars"),
    step_bars: int = typer.Option(730, help="Rolling step size in H1 bars"),
    holdout_bars: int = typer.Option(2190, help="Holdout set size in H1 bars"),
    min_oos_ratio: float = typer.Option(0.6, help="Minimum OOS/IS ratio to pass"),
) -> None:
    """Run walk-forward analysis on the given strategy config."""
    from novatrade.cli.commands.walkforward_cmd import format_walkforward_report
    from novatrade.cli.config_schema import StrategyConfig
    from novatrade.optimization.walkforward import WalkForwardConfig, run_walk_forward
    from novatrade.storage.data_snapshot import load_snapshot

    # Load config
    config_path = Path(config)
    if not config_path.exists():
        typer.echo(f"Error: config not found: {config_path}", err=True)
        raise typer.Exit(code=1)

    try:
        strategy_config = StrategyConfig.from_yaml(config_path)
    except Exception as exc:
        typer.echo(f"Error loading config: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(f"Config: {strategy_config.name} v{strategy_config.version} [{strategy_config.content_hash()[:8]}]")

    # Load candles
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

    # Build WF config
    wf_config = WalkForwardConfig(
        train_bars=train_bars,
        test_bars=test_bars,
        step_bars=step_bars,
        holdout_bars=holdout_bars,
        min_oos_is_ratio=min_oos_ratio,
    )

    # Run
    typer.echo("Running walk-forward validation...")
    result = run_walk_forward(strategy_config, h1_candles, h4_candles, wf_config)

    # Report
    report = format_walkforward_report(result)
    typer.echo(report)

    if not result.overall_passed:
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# leaderboard command
# ---------------------------------------------------------------------------


@app.command("leaderboard")
def leaderboard() -> None:
    """Show the experiment leaderboard ranked by fitness score."""
    typer.echo("Leaderboard command -- Phase 4")


# ---------------------------------------------------------------------------
# promote command
# ---------------------------------------------------------------------------


@app.command("promote")
def promote(
    experiment_id: str = typer.Option(..., help="Experiment ID to promote"),
    config: Path = typer.Option(..., "--config", "-c", help="Strategy YAML config"),  # noqa: B008
    symbol: str = typer.Option("EURUSD", help="Symbol for CSV loading"),
    snapshot_id: str = typer.Option("", "--snapshot", "-s", help="Frozen snapshot ID"),
    db_path: Path = typer.Option(  # noqa: B008
        Path("/home/nova/nova-core/data/experiments.db"),
        "--db",
        help="Experiment database path",
    ),
) -> None:
    """Run the full promotion pipeline on an experiment champion."""
    from novatrade.cli.config_schema import StrategyConfig
    from novatrade.optimization.promotion import run_promotion_pipeline
    from novatrade.storage.data_snapshot import load_snapshot
    from novatrade.storage.experiment_db import ExperimentDB

    # Load config
    config_path = Path(config)
    if not config_path.exists():
        typer.echo(f"Error: config not found: {config_path}", err=True)
        raise typer.Exit(code=1)

    try:
        strategy_config = StrategyConfig.from_yaml(config_path)
    except Exception as exc:
        typer.echo(f"Error loading config: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    # Look up baseline score from DB
    baseline_score = 0.0
    try:
        db = ExperimentDB(db_path)
        record = db.get(experiment_id)
        if record and record.scout_score is not None:
            baseline_score = record.scout_score
            # Reconstruct config from stored params if available
            typer.echo(f"Experiment {experiment_id}: scout_score={baseline_score:.4f}")
        else:
            typer.echo(f"Warning: experiment {experiment_id} not found in DB", err=True)
        db.close()
    except Exception as exc:
        typer.echo(f"Warning: DB lookup failed: {exc}", err=True)

    # Load candles
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

    # Run promotion pipeline
    typer.echo(f"Running promotion pipeline for {experiment_id}...")
    result = run_promotion_pipeline(
        config=strategy_config,
        h1_candles=h1_candles,
        h4_candles=h4_candles,
        experiment_id=experiment_id,
        baseline_score=baseline_score,
    )

    # Report
    typer.echo("")
    typer.echo("=" * 56)
    typer.echo("  Promotion Pipeline Results")
    typer.echo("=" * 56)
    typer.echo(f"  Walk-forward:  {'PASS' if result.walkforward_passed else 'FAIL'}")
    typer.echo(f"  Holdout:       {'PASS' if result.holdout_passed else 'FAIL'}")
    typer.echo(f"  Perturbation:  {'PASS' if result.perturbation_passed else 'FAIL'}")
    typer.echo(f"  Stress:        {'PASS' if result.stress_passed else 'FAIL'}")
    typer.echo(f"  Overall:       {'PROMOTED' if result.overall_passed else 'REJECTED'}")
    if result.promotion_score is not None:
        typer.echo(f"  Score:         {result.promotion_score:.4f}")
    typer.echo("=" * 56)

    if not result.overall_passed:
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# campaign subcommands
# ---------------------------------------------------------------------------


@campaign_app.command("start")
def campaign_start(
    config: Path = typer.Option(..., "--config", "-c", help="Base strategy YAML config"),  # noqa: B008
    doctrine: Path = typer.Option("", help="Campaign doctrine Markdown file"),  # noqa: B008
    budget: int = typer.Option(200, "--budget", "-n", help="Maximum experiments"),
    stagnation: int = typer.Option(20, help="Experiments without improvement before stop"),
    wall_clock: float = typer.Option(21600.0, help="Max wall clock seconds (default 6h)"),
    symbol: str = typer.Option("EURUSD", help="Symbol for CSV loading"),
    snapshot_id: str = typer.Option("", "--snapshot", "-s", help="Frozen snapshot ID"),
    db_path: Path = typer.Option(  # noqa: B008
        Path("/home/nova/nova-core/data/experiments.db"),
        "--db",
        help="Experiment database path",
    ),
    walkforward: bool = typer.Option(True, help="Run WF validation on new bests"),
) -> None:
    """Start an AutoResearch campaign — bounded Karpathy loop."""
    from novatrade.cli.commands.campaign_cmd import start_campaign
    from novatrade.optimization.campaign_engine import CampaignConfig
    from novatrade.storage.data_snapshot import fingerprint_candles, load_snapshot
    from novatrade.storage.experiment_db import ExperimentDB
    from novatrade.storage.experiment_identity import compute_doctrine_hash

    # Build campaign config
    campaign_cfg = CampaignConfig(
        max_experiments=budget,
        stagnation_limit=stagnation,
        max_wall_clock_seconds=wall_clock,
        walkforward_on_new_best=walkforward,
    )

    # Load candles
    if snapshot_id:
        typer.echo(f"Loading snapshot: {snapshot_id}")
        h1_candles, h4_candles, _meta = load_snapshot(snapshot_id, SNAPSHOT_DIR)
        dataset_hash = snapshot_id
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

        import hashlib

        combined = hashlib.sha256()
        combined.update(fingerprint_candles(h1_candles).encode())
        combined.update(fingerprint_candles(h4_candles).encode())
        dataset_hash = combined.hexdigest()[:16]

    # Doctrine hash
    doctrine_hash = ""
    doctrine_path = Path(doctrine) if str(doctrine) else None
    if doctrine_path and doctrine_path.exists():
        doctrine_hash = compute_doctrine_hash(doctrine_path)
        typer.echo(f"Doctrine: {doctrine_path} [{doctrine_hash[:8]}]")

    # Open DB
    db = None
    try:
        db = ExperimentDB(db_path)
    except Exception as exc:
        typer.echo(f"Warning: could not open DB: {exc}", err=True)

    # Campaign dir
    campaign_dir = Path("/home/nova/nova-core/data/campaigns") / campaign_cfg.campaign_id

    # Run
    result = start_campaign(
        config_path=Path(config),
        campaign_config=campaign_cfg,
        h1_candles=h1_candles,
        h4_candles=h4_candles,
        dataset_hash=dataset_hash,
        doctrine_hash=doctrine_hash,
        campaign_dir=campaign_dir,
        db=db,
    )

    if db is not None:
        db.close()

    if result.best_experiment_id is None:
        raise typer.Exit(code=1)


@campaign_app.command("status")
def campaign_status(
    campaign_dir: Path = typer.Option(  # noqa: B008
        Path("/home/nova/nova-core/data/campaigns"),
        help="Root campaigns directory",
    ),
    campaign_id: str = typer.Option("", help="Specific campaign ID (default: latest)"),
) -> None:
    """Show status of an AutoResearch campaign from its latest checkpoint."""
    from novatrade.optimization.campaign_engine import load_latest_checkpoint

    # Find campaign dir
    root = Path(campaign_dir)
    if campaign_id:
        target = root / campaign_id
    else:
        # Find most recently modified campaign
        dirs = sorted(root.glob("campaign-*"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not dirs:
            typer.echo("No campaigns found.")
            raise typer.Exit(code=1)
        target = dirs[0]

    typer.echo(f"Campaign: {target.name}")

    cp = load_latest_checkpoint(target)
    if cp is None:
        typer.echo("No checkpoints found.")
        raise typer.Exit(code=1)

    typer.echo(f"  Experiments:  {cp.experiment_count}")
    typer.echo(f"  Best score:   {cp.best_scout_score:.4f}")
    typer.echo(f"  Best ID:      {cp.best_experiment_id}")
    typer.echo(f"  Stagnation:   {cp.stagnation_count}")
    typer.echo(f"  Crashes:      {cp.crash_count}")
    typer.echo(f"  Elapsed:      {cp.elapsed_seconds:.1f}s")
    typer.echo(f"  Checkpoint:   {cp.created_at}")


@campaign_app.command("stop")
def campaign_stop() -> None:
    """Stop the active AutoResearch campaign.

    For now, campaigns run synchronously. This command is a placeholder
    for when we add async/background campaign support. To stop a running
    campaign, send SIGINT (Ctrl+C) to the process.
    """
    typer.echo(
        "Campaign stop: campaigns currently run synchronously. "
        "Use Ctrl+C to stop a running campaign. "
        "Async background campaigns will be added in a future phase."
    )


# ---------------------------------------------------------------------------
# doctrine subcommands
# ---------------------------------------------------------------------------

from novatrade.cli.commands.doctrine_cmd import doctrine_create, doctrine_show  # noqa: E402
from novatrade.cli.commands.health_diagnostic import health_diagnostic  # noqa: E402

doctrine_app.command("create")(doctrine_create)
doctrine_app.command("show")(doctrine_show)
app.command("health-diagnostic")(health_diagnostic)


# ---------------------------------------------------------------------------
# pipeline subcommands
# ---------------------------------------------------------------------------


@pipeline_app.command("run")
def pipeline_run(
    data: Path = typer.Option(..., "--data", "-d", help="Path to candle data file"),  # noqa: B008
    pair: str = typer.Option("EURUSD", help="Currency pair"),
    timeframe: str = typer.Option("H1", help="Primary timeframe"),
    concept: str = typer.Option("", help="Strategy concept text (auto-generates doctrine)"),
    doctrine: Path = typer.Option("", help="Pre-existing doctrine YAML path"),  # noqa: B008
    mode: str = typer.Option("single", help="Pipeline mode: single, sweep, campaign"),
    experiments: int = typer.Option(200, "-n", help="Number of experiments (sweep/campaign)"),
    output_json: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """Run an end-to-end strategy pipeline (single backtest, sweep, or campaign)."""
    from novatrade.pipeline.orchestrator import PipelineConfig, PipelineMode, run_pipeline
    from novatrade.pipeline.reporter import format_pipeline_json, format_pipeline_report

    # Map mode string to enum
    try:
        pipeline_mode = PipelineMode(mode)
    except ValueError:
        typer.echo(f"Error: invalid mode '{mode}'. Choose from: single, sweep, campaign", err=True)
        raise typer.Exit(code=1) from None

    # Build pipeline config
    config = PipelineConfig(
        data_path=Path(data),
        pair=pair,
        timeframe=timeframe,
        concept=concept or "",
        doctrine_path=Path(doctrine) if str(doctrine) else None,
        mode=pipeline_mode,
        experiments=experiments,
    )

    # Execute pipeline
    result = run_pipeline(config)

    # Output
    if output_json:
        typer.echo(format_pipeline_json(result))
    else:
        typer.echo(format_pipeline_report(result))

    # Exit code 1 if errors
    if result.errors:
        raise typer.Exit(code=1)


@pipeline_app.command("campaign")
def pipeline_campaign(
    data: Path = typer.Option(..., "--data", "-d", help="Path to candle data file"),  # noqa: B008
    pair: str = typer.Option("EURUSD", help="Currency pair"),
    timeframe: str = typer.Option("H1", help="Primary timeframe"),
    concept: str = typer.Option("", help="Strategy concept text"),
    doctrine: Path = typer.Option("", help="Doctrine YAML path"),  # noqa: B008
    experiments: int = typer.Option(200, "-n", help="Number of experiments"),
) -> None:
    """Run an end-to-end campaign pipeline (convenience alias for 'pipeline run --mode campaign')."""
    from novatrade.pipeline.orchestrator import PipelineConfig, PipelineMode, run_pipeline
    from novatrade.pipeline.reporter import format_pipeline_report

    config = PipelineConfig(
        data_path=Path(data),
        pair=pair,
        timeframe=timeframe,
        concept=concept or "",
        doctrine_path=Path(doctrine) if str(doctrine) else None,
        mode=PipelineMode.CAMPAIGN,
        experiments=experiments,
    )

    result = run_pipeline(config)
    typer.echo(format_pipeline_report(result))

    if result.errors:
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app()
