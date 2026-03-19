"""Core backtest execution — single run with full artifact capture and gated evaluation.

Orchestrates the end-to-end lifecycle of a single backtest experiment:
  1. Build BacktestEnvironment from StrategyConfig
  2. Run the IRB backtester
  3. Compute metrics over the broadest available evaluation window
  4. Apply Stage A / Stage B gates sequentially
  5. Compute scout score if both gates pass
  6. Persist artifacts and experiment record

This module is intentionally pure — execute_backtest takes data in and returns
a result dataclass.  Side effects (DB writes, artifact saves) are gated on
optional arguments the caller provides.
"""

from __future__ import annotations

import json
import logging
import traceback
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from novatrade.backtest.engine import BacktestResult, IRBBacktester
from novatrade.backtest.environment import DEFAULT_ENVIRONMENT
from novatrade.backtest.metrics import (
    EVAL_WINDOWS,
    BacktestMetrics,
    compute_metrics,
)
from novatrade.cli.config_schema import StrategyConfig
from novatrade.evaluation.fill_model import DEFAULT_FILL_MODEL
from novatrade.evaluation.fitness import compute_scout_score
from novatrade.evaluation.gates import (
    GateResults,
    evaluate_stage_a,
    evaluate_stage_b,
)
from novatrade.models import Candle
from novatrade.storage.artifacts import save_config_snapshot, save_trade_log
from novatrade.storage.experiment_db import ExperimentDB, ExperimentRecord
from novatrade.storage.experiment_identity import compute_experiment_id, get_engine_sha

log = logging.getLogger("novatrade.cli.commands.run")


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class BacktestRunResult:
    """Complete outcome of a single backtest run."""

    experiment_id: str
    scout_score: float | None
    gate_results: GateResults
    metrics: BacktestMetrics | None
    status: str
    backtest_result: BacktestResult | None
    notes: str = ""


# ---------------------------------------------------------------------------
# Main execution function
# ---------------------------------------------------------------------------


def execute_backtest(
    config: StrategyConfig,
    h1_candles: list[Candle],
    h4_candles: list[Candle],
    *,
    campaign_id: str | None = None,
    parent_experiment_id: str | None = None,
    dataset_hash: str = "",
    doctrine_hash: str = "",
    campaign_dir: Path | None = None,
    db: ExperimentDB | None = None,
) -> BacktestRunResult:
    """Run a single backtest with gated evaluation and optional artifact capture.

    This is the core orchestration function — pure in its primary path
    (config + candles in, result out).  Side effects (artifact writes, DB
    inserts) only happen when the caller opts in via ``campaign_dir`` and
    ``db`` parameters.

    Args:
        config: Strategy configuration (signal parameters).
        h1_candles: H1 OHLCV candle series in chronological order.
        h4_candles: H4 OHLCV candle series in chronological order.
        campaign_id: Optional campaign this experiment belongs to.
        parent_experiment_id: Optional parent experiment (for lineage).
        dataset_hash: SHA-256 fingerprint of the input dataset.
        doctrine_hash: SHA-256 hash of the doctrine file.
        campaign_dir: If provided, save config/trade artifacts here.
        db: If provided, insert an ExperimentRecord after evaluation.

    Returns:
        BacktestRunResult with experiment ID, status, gate results, metrics,
        and optional scout score.
    """
    gate_results = GateResults()
    notes_parts: list[str] = []
    stage_a_passed = False
    stage_b_passed = False

    # --- Guard: empty candle data ---
    if not h1_candles or not h4_candles:
        experiment_id = _compute_id(config, dataset_hash, doctrine_hash)
        return BacktestRunResult(
            experiment_id=experiment_id,
            scout_score=None,
            gate_results=gate_results,
            metrics=None,
            status="gated_a",
            backtest_result=None,
            notes="Empty candle data — h1 or h4 candles missing",
        )

    # --- Step 1: Build environment ---
    env_kwargs = config.to_environment_kwargs()
    try:
        environment = replace(DEFAULT_ENVIRONMENT, **env_kwargs)
    except TypeError as exc:
        experiment_id = _compute_id(config, dataset_hash, doctrine_hash)
        return BacktestRunResult(
            experiment_id=experiment_id,
            scout_score=None,
            gate_results=gate_results,
            metrics=None,
            status="crashed",
            backtest_result=None,
            notes=f"Environment construction failed: {exc}",
        )

    # --- Step 2+3: Create backtester and run ---
    try:
        bt = IRBBacktester(env=environment)
        backtest_result = bt.run(h1_candles, h4_candles)
    except Exception as exc:
        experiment_id = _compute_id(config, dataset_hash, doctrine_hash)
        log.exception("Backtest crashed")
        return BacktestRunResult(
            experiment_id=experiment_id,
            scout_score=None,
            gate_results=gate_results,
            metrics=None,
            status="crashed",
            backtest_result=None,
            notes=f"Backtest engine crashed: {exc}\n{traceback.format_exc()}",
        )

    # --- Step 4: Compute metrics ---
    # Use the broadest available window: index 2 = 1-year "broad"
    # Fall back to last window if EVAL_WINDOWS is shorter than expected.
    window_idx = min(2, len(EVAL_WINDOWS) - 1)
    eval_window = EVAL_WINDOWS[window_idx]

    metrics = compute_metrics(
        trades=backtest_result.trades,
        pending_orders=backtest_result.pending_orders,
        signals=backtest_result.signals,
        filter_rejections=backtest_result.filter_rejections,
        window=eval_window,
        total_bars=backtest_result.total_bars,
        initial_equity=environment.initial_equity,
    )

    # --- Step 5: Compute experiment ID ---
    engine_sha = get_engine_sha()
    fill_model_hash = DEFAULT_FILL_MODEL.version_hash()

    experiment_id = compute_experiment_id(
        config_hash=config.content_hash(),
        dataset_hash=dataset_hash,
        fill_model_hash=fill_model_hash,
        engine_sha=engine_sha,
        doctrine_hash=doctrine_hash,
    )

    # --- Step 6: Stage A gates ---
    stage_a_results = evaluate_stage_a(metrics)
    for r in stage_a_results:
        gate_results.results.append(r)

    stage_a_passed = all(r.passed for r in stage_a_results)

    # --- Step 7: Stage A failure path ---
    if not stage_a_passed:
        scout_score: float | None = None
        status = "gated_a"
        notes_parts.append(f"Stage A failed at: {gate_results.failed_at}")
    else:
        # --- Step 8: Stage B gates ---
        stage_b_results = evaluate_stage_b(environment)
        for r in stage_b_results:
            gate_results.results.append(r)

        stage_b_passed = all(r.passed for r in stage_b_results)

        # --- Step 9: Stage B failure path ---
        if not stage_b_passed:
            scout_score = None
            status = "gated_b"
            notes_parts.append(f"Stage B failed at: {gate_results.failed_at}")
        else:
            # --- Step 10: Both gates pass — compute scout score ---
            scout_score = compute_scout_score(metrics)
            status = "ranked" if scout_score is not None and scout_score > 0 else "valid"

    # --- Step 11: Save artifacts ---
    if campaign_dir is not None:
        try:
            config_dict = config.model_dump(mode="json")
            save_config_snapshot(config_dict, campaign_dir, experiment_id)
            trade_dicts = _trades_to_dicts(backtest_result)
            save_trade_log(trade_dicts, campaign_dir, experiment_id)
        except Exception as exc:
            log.warning("Artifact save failed: %s", exc)
            notes_parts.append(f"Artifact save warning: {exc}")

    # --- Step 12: Insert DB record ---
    if db is not None:
        try:
            gate_results_json = _gate_results_to_json(gate_results)
            metrics_json = json.dumps(metrics.to_dict()) if metrics else "{}"

            # Clamp infinite profit factor to None for SQLite storage
            pf = metrics.profit_factor if metrics else None
            if pf is not None and (pf == float("inf") or pf == float("-inf")):
                pf = None

            record = ExperimentRecord(
                experiment_id=experiment_id,
                parent_experiment_id=parent_experiment_id,
                campaign_id=campaign_id,
                strategy_config_hash=config.content_hash(),
                doctrine_hash=doctrine_hash,
                dataset_hash=dataset_hash,
                engine_sha=engine_sha,
                gate_a_passed=stage_a_passed,
                gate_b_passed=stage_b_passed if stage_a_passed else None,
                gate_c_passed=None,
                gate_results_json=gate_results_json,
                metrics_json=metrics_json,
                scout_score=scout_score,
                promotion_score=None,
                trade_count=metrics.total_completed_trades if metrics else 0,
                profit_factor=pf,
                max_drawdown_pct=metrics.max_drawdown_pct if metrics else None,
                status=status,
                notes="; ".join(notes_parts),
            )
            db.insert(record)
        except Exception as exc:
            log.warning("DB insert failed: %s", exc)
            notes_parts.append(f"DB insert warning: {exc}")

    # --- Step 13: Return result ---
    return BacktestRunResult(
        experiment_id=experiment_id,
        scout_score=scout_score,
        gate_results=gate_results,
        metrics=metrics,
        status=status,
        backtest_result=backtest_result,
        notes="; ".join(notes_parts),
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _compute_id(
    config: StrategyConfig,
    dataset_hash: str,
    doctrine_hash: str,
) -> str:
    """Compute experiment ID without running the backtest (for error paths)."""
    return compute_experiment_id(
        config_hash=config.content_hash(),
        dataset_hash=dataset_hash,
        fill_model_hash=DEFAULT_FILL_MODEL.version_hash(),
        engine_sha=get_engine_sha(),
        doctrine_hash=doctrine_hash,
    )


def _trades_to_dicts(result: BacktestResult) -> list[dict]:
    """Convert completed trades to serialisable dicts for artifact storage."""
    trades: list[dict] = []
    for t in result.trades:
        trades.append(
            {
                "trade_id": t.trade_id,
                "side": t.side.value,
                "entry_bar": t.entry_bar,
                "exit_bar": t.exit_bar,
                "entry_price": t.entry_price,
                "exit_price": t.exit_price,
                "stop_loss": t.stop_loss,
                "volume": t.volume,
                "exit_reason": t.exit_reason.value,
                "pnl_pips": round(t.pnl_pips, 2),
                "pnl_usd": round(t.pnl_usd, 2),
                "risk_r": round(t.risk_r, 4),
                "hold_bars": t.hold_bars,
            }
        )
    return trades


def _gate_results_to_json(gate_results: GateResults) -> str:
    """Serialise GateResults to a JSON string for DB storage."""
    data: list[dict[str, Any]] = []
    for r in gate_results.results:
        data.append(
            {
                "gate": r.gate,
                "passed": r.passed,
                "reason": r.reason,
                "details": r.details,
            }
        )
    return json.dumps(data)
