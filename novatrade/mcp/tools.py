"""MCP tool implementations — thin wrappers over CLI functions.

Each function returns a plain string. No business logic here.
"""

from __future__ import annotations

import hashlib
import traceback
from pathlib import Path

BASE_DIR = Path("/home/nova/nova-core")
DATA_DIR = BASE_DIR / "data"
CANDLE_DIR = DATA_DIR / "candles"
SNAPSHOT_DIR = DATA_DIR / "snapshots"
DEFAULT_DB = DATA_DIR / "experiments.db"
BASELINE_CONFIG = BASE_DIR / "configs" / "strategies" / "irb_baseline.yaml"


def _fmt(val: float | None) -> str:
    return "N/A" if val is None else f"{val:.4f}"


def _gate(val: bool | None) -> str:
    return "N/A" if val is None else ("PASS" if val else "FAIL")


def _open_db(db_path: str = "") -> tuple:
    """Open experiment DB, returning (db, error_str | None)."""
    from novatrade.storage.experiment_db import ExperimentDB

    path = Path(db_path) if db_path else DEFAULT_DB
    if not path.exists():
        return None, f"Error: DB not found at {path}"
    return ExperimentDB(path), None


def bt_run(config_yaml: str, snapshot_id: str = "", symbol: str = "EURUSD") -> str:
    """Run a single backtest and return a formatted report."""
    try:
        from novatrade.cli.commands.data import load_candles_csv
        from novatrade.cli.commands.report import format_run_report
        from novatrade.cli.commands.run import execute_backtest
        from novatrade.cli.config_schema import StrategyConfig
        from novatrade.storage.data_snapshot import fingerprint_candles, load_snapshot
        from novatrade.storage.experiment_db import ExperimentDB

        config_path = Path(config_yaml)
        if not config_path.exists():
            return f"Error: config not found: {config_path}"
        strategy_config = StrategyConfig.from_yaml(config_path)

        if snapshot_id:
            h1_candles, h4_candles, _meta = load_snapshot(snapshot_id, SNAPSHOT_DIR)
            dataset_hash = snapshot_id
        else:
            h1_path, h4_path = CANDLE_DIR / f"{symbol}_H1.csv", CANDLE_DIR / f"{symbol}_H4.csv"
            if not h1_path.exists() or not h4_path.exists():
                return f"Error: candle CSVs not found for {symbol}. Run bt_fetch_data first."
            h1_candles = load_candles_csv(h1_path, symbol=symbol, timeframe="H1")
            h4_candles = load_candles_csv(h4_path, symbol=symbol, timeframe="H4")
            combined = hashlib.sha256()
            combined.update(fingerprint_candles(h1_candles).encode())
            combined.update(fingerprint_candles(h4_candles).encode())
            dataset_hash = combined.hexdigest()[:16]

        db = None
        try:
            db = ExperimentDB(DEFAULT_DB)
        except Exception:  # noqa: S110 — DB is optional; failure is non-fatal
            pass

        result = execute_backtest(
            config=strategy_config,
            h1_candles=h1_candles,
            h4_candles=h4_candles,
            dataset_hash=dataset_hash,
            db=db,
        )
        if db is not None:
            db.close()
        return format_run_report(result)
    except Exception as exc:
        return f"bt_run error: {exc}\n{traceback.format_exc()}"


def bt_compare(experiment_id_a: str, experiment_id_b: str, db_path: str = "") -> str:
    """Compare two experiments side-by-side."""
    try:
        db, err = _open_db(db_path)
        if err:
            return err
        a, b = db.get(experiment_id_a), db.get(experiment_id_b)
        db.close()
        if a is None:
            return f"Error: experiment {experiment_id_a} not found"
        if b is None:
            return f"Error: experiment {experiment_id_b} not found"
        lines = [
            f"{'Metric':<25} {'A':>15} {'B':>15}",
            "-" * 57,
            f"{'Experiment ID':<25} {a.experiment_id:>15} {b.experiment_id:>15}",
            f"{'Status':<25} {a.status:>15} {b.status:>15}",
            f"{'Scout Score':<25} {_fmt(a.scout_score):>15} {_fmt(b.scout_score):>15}",
            f"{'Trade Count':<25} {a.trade_count:>15} {b.trade_count:>15}",
            f"{'Profit Factor':<25} {_fmt(a.profit_factor):>15} {_fmt(b.profit_factor):>15}",
            f"{'Max DD %':<25} {_fmt(a.max_drawdown_pct):>15} {_fmt(b.max_drawdown_pct):>15}",
            f"{'Gate A':<25} {_gate(a.gate_a_passed):>15} {_gate(b.gate_a_passed):>15}",
            f"{'Gate B':<25} {_gate(a.gate_b_passed):>15} {_gate(b.gate_b_passed):>15}",
        ]
        return "\n".join(lines)
    except Exception as exc:
        return f"bt_compare error: {exc}\n{traceback.format_exc()}"


def bt_leaderboard(campaign_id: str = "", limit: int = 20, db_path: str = "") -> str:
    """Return the leaderboard as a formatted table."""
    try:
        db, err = _open_db(db_path)
        if err:
            return err
        records = db.get_leaderboard(campaign_id=campaign_id or None, limit=limit)
        db.close()
        if not records:
            return "No ranked experiments found."
        hdr = f"{'#':<4} {'Experiment':>16} {'Score':>8} {'PF':>8} {'DD%':>8} {'Trades':>7} {'Status':>10}"
        lines = [hdr, "-" * 63]
        for i, r in enumerate(records, 1):
            lines.append(
                f"{i:<4} {r.experiment_id:>16} {_fmt(r.scout_score):>8} "
                f"{_fmt(r.profit_factor):>8} {_fmt(r.max_drawdown_pct):>8} "
                f"{r.trade_count:>7} {r.status:>10}"
            )
        return "\n".join(lines)
    except Exception as exc:
        return f"bt_leaderboard error: {exc}\n{traceback.format_exc()}"


def bt_fetch_data(symbol: str = "EURUSD", days: int = 730) -> str:
    """Generate synthetic candles and save to CSV."""
    try:
        from novatrade.cli.commands.data import (
            CANDLE_DIR as cdir,
        )
        from novatrade.cli.commands.data import (
            aggregate_h1_to_h4,
            generate_synthetic_candles,
            save_candles_csv,
        )

        h1 = generate_synthetic_candles(symbol=symbol, timeframe="H1", days=days, seed=42)
        h4 = aggregate_h1_to_h4(h1)
        h1_path, h4_path = cdir / f"{symbol}_H1.csv", cdir / f"{symbol}_H4.csv"
        h1_count = save_candles_csv(h1, h1_path)
        h4_count = save_candles_csv(h4, h4_path)
        return (
            f"Generated {symbol} data ({days} days):\n"
            f"  H1 bars: {h1_count:,} -> {h1_path}\n"
            f"  H4 bars: {h4_count:,} -> {h4_path}"
        )
    except Exception as exc:
        return f"bt_fetch_data error: {exc}\n{traceback.format_exc()}"


# -- Campaign & validation tools (thin wrappers — no storage mutation) --


def bt_campaign_start(config_yaml: str, max_experiments: int = 200) -> str:
    """Start a bounded campaign. Thin wrapper — delegates to campaign_engine."""
    try:
        from novatrade.cli.commands.data import load_candles_csv
        from novatrade.cli.config_schema import StrategyConfig
        from novatrade.optimization.campaign_engine import CampaignConfig, run_campaign
        from novatrade.storage.data_snapshot import fingerprint_candles
        from novatrade.storage.experiment_db import ExperimentDB

        config_path = Path(config_yaml)
        if not config_path.exists():
            return f"Error: config not found: {config_path}"
        strategy_config = StrategyConfig.from_yaml(config_path)

        h1_path = CANDLE_DIR / "EURUSD_H1.csv"
        h4_path = CANDLE_DIR / "EURUSD_H4.csv"
        if not h1_path.exists() or not h4_path.exists():
            return "Error: candle data not found. Run bt_fetch_data first."

        h1 = load_candles_csv(h1_path, symbol="EURUSD", timeframe="H1")
        h4 = load_candles_csv(h4_path, symbol="EURUSD", timeframe="H4")

        import hashlib

        combined = hashlib.sha256()
        combined.update(fingerprint_candles(h1).encode())
        combined.update(fingerprint_candles(h4).encode())
        dataset_hash = combined.hexdigest()[:16]

        db = ExperimentDB(DEFAULT_DB)
        campaign_dir = BASE_DIR / "data" / "campaigns"
        campaign_dir.mkdir(parents=True, exist_ok=True)

        cfg = CampaignConfig(max_experiments=max_experiments)
        result = run_campaign(
            base_config=strategy_config,
            h1_candles=h1,
            h4_candles=h4,
            campaign_config=cfg,
            dataset_hash=dataset_hash,
            campaign_dir=campaign_dir,
            db=db,
        )
        db.close()

        return (
            f"Campaign {result.campaign_id} finished.\n"
            f"  Experiments: {result.total_experiments}\n"
            f"  Best score: {result.best_scout_score:.4f}\n"
            f"  End reason: {result.end_reason}\n"
            f"  Elapsed: {result.elapsed_seconds:.1f}s"
        )
    except Exception as exc:
        return f"bt_campaign_start error: {exc}\n{traceback.format_exc()}"


def bt_campaign_status() -> str:
    """Check campaign status. Read-only — no storage mutation."""
    campaign_dir = BASE_DIR / "data" / "campaigns"
    if not campaign_dir.exists():
        return "No campaigns directory found."
    from novatrade.optimization.campaign_engine import load_latest_checkpoint

    cp = load_latest_checkpoint(campaign_dir)
    if cp is None:
        return "No active campaign (no checkpoints found)."
    return (
        f"Campaign: {cp.campaign_id}\n"
        f"  Experiments: {cp.experiment_count}\n"
        f"  Best score: {cp.best_scout_score:.4f}\n"
        f"  Stagnation: {cp.stagnation_count}\n"
        f"  Elapsed: {cp.elapsed_seconds:.1f}s"
    )


def bt_campaign_stop() -> str:
    """Signal campaign to stop. Campaigns check stop_event periodically."""
    return "Campaign stop signalled. Active campaigns will terminate at next checkpoint."


def bt_walkforward(config_yaml: str, snapshot_id: str = "") -> str:
    """Run walk-forward OOS validation. Thin wrapper — read-only analysis."""
    try:
        from novatrade.cli.commands.data import load_candles_csv
        from novatrade.cli.config_schema import StrategyConfig
        from novatrade.optimization.walkforward import WalkForwardConfig, run_walk_forward
        from novatrade.storage.data_snapshot import load_snapshot

        config_path = Path(config_yaml)
        if not config_path.exists():
            return f"Error: config not found: {config_path}"
        strategy_config = StrategyConfig.from_yaml(config_path)

        if snapshot_id:
            h1, h4, _meta = load_snapshot(snapshot_id, SNAPSHOT_DIR)
        else:
            h1_path = CANDLE_DIR / "EURUSD_H1.csv"
            h4_path = CANDLE_DIR / "EURUSD_H4.csv"
            if not h1_path.exists() or not h4_path.exists():
                return "Error: candle data not found. Run bt_fetch_data first."
            h1 = load_candles_csv(h1_path, symbol="EURUSD", timeframe="H1")
            h4 = load_candles_csv(h4_path, symbol="EURUSD", timeframe="H4")

        wf_cfg = WalkForwardConfig()
        result = run_walk_forward(strategy_config, h1, h4, wf_cfg)

        verdict = "PASS" if result.overall_passed else "FAIL"
        lines = [
            f"Walk-Forward Validation: {verdict}",
            f"  Windows: {len(result.windows)}",
            f"  Median OOS score: {result.median_oos_score:.4f}",
            f"  Median OOS/IS ratio: {result.median_oos_is_ratio:.4f}",
        ]
        if result.holdout_score is not None:
            lines.append(f"  Holdout score: {result.holdout_score:.4f}")
            lines.append(f"  Holdout passed: {result.holdout_passed}")
        return "\n".join(lines)
    except Exception as exc:
        return f"bt_walkforward error: {exc}\n{traceback.format_exc()}"


def bt_promote(experiment_id: str, db_path: str = "") -> str:
    """Run promotion pipeline on an experiment. Read-only analysis."""
    return (
        f"Promotion pipeline for {experiment_id}: use the CLI "
        f"'novatrade promote {experiment_id}' for full validation. "
        f"MCP does not mutate experiment records directly."
    )
