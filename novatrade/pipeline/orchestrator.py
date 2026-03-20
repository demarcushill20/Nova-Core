"""Unified pipeline orchestrator — concept to ranked results in one call.

Chains data loading, validation, doctrine translation, config generation,
and execution (single / sweep / campaign) into a single ``run_pipeline()``
entry point.  Each stage is timed and captured in the PipelineResult for
full auditability.

Usage::

    from novatrade.pipeline import run_pipeline, PipelineConfig, PipelineMode

    result = run_pipeline(PipelineConfig(
        data_path="data/candles/EURUSD_H1.csv",
        concept="IRB reversal on EURUSD H1",
        mode=PipelineMode.CAMPAIGN,
        experiments=200,
    ))
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

log = logging.getLogger("novatrade.pipeline.orchestrator")


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


class PipelineMode(Enum):
    SINGLE = "single"  # one backtest
    SWEEP = "sweep"  # N random experiments
    CAMPAIGN = "campaign"  # full AutoResearch


@dataclass
class PipelineStage:
    """Record of a single pipeline stage execution."""

    name: str
    status: str = "ok"  # "ok" | "warning" | "error" | "skipped"
    duration_ms: int = 0
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class PipelineConfig:
    """All inputs needed to run the pipeline end-to-end."""

    data_path: Path | str
    pair: str = "EURUSD"
    timeframe: str = "H1"
    concept: str = ""  # natural language (auto-generates doctrine)
    doctrine_path: Path | str | None = None  # pre-existing doctrine YAML
    mode: PipelineMode = PipelineMode.SINGLE
    experiments: int = 200  # for sweep / campaign
    db_path: Path = Path("/home/nova/nova-core/data/experiments.db")
    output_dir: Path = Path("/home/nova/nova-core/OUTPUT")
    validate_data: bool = True
    auto_fix_data: bool = True

    def __post_init__(self) -> None:
        self.data_path = Path(self.data_path)
        self.db_path = Path(self.db_path)
        self.output_dir = Path(self.output_dir)
        if self.doctrine_path is not None:
            self.doctrine_path = Path(self.doctrine_path)
        if self.experiments < 1:
            self.experiments = 1
        if self.experiments > 10_000:
            self.experiments = 10_000


@dataclass
class PipelineResult:
    """Aggregated outcome of a full pipeline run."""

    mode: PipelineMode
    doctrine_name: str = ""
    total_experiments: int = 0
    gate_pass_rate: float = 0.0
    best_score: float = 0.0
    best_experiment_id: str = ""
    top_survivors: list[dict[str, Any]] = field(default_factory=list)
    data_quality_issues: int = 0
    stages: list[PipelineStage] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Timeframe helpers
# ---------------------------------------------------------------------------

_TF_MAP: dict[str, str] = {
    "M5": "M15",
    "M15": "H1",
    "M30": "H1",
    "H1": "H4",
    "H4": "D1",
    "D1": "W1",
}


def _derive_higher_tf(primary: str) -> str:
    """Return the next-higher timeframe for multi-timeframe confirmation."""
    return _TF_MAP.get(primary, "H4")


def _candle_hash(candles: list) -> str:
    """Cheap SHA-256 fingerprint over candle timestamps + closes."""
    h = hashlib.sha256()
    for c in candles:
        h.update(f"{c.timestamp},{c.close}".encode())
    return h.hexdigest()[:16]


# ---------------------------------------------------------------------------
# Stage runners (each returns a PipelineStage + artefact)
# ---------------------------------------------------------------------------


def _stage_load_data(
    config: PipelineConfig,
) -> tuple[PipelineStage, list, list]:
    """Load candle data from config.data_path.

    Tries the parallel-built ``novatrade.data.loader.load_candles`` first,
    then falls back to the existing ``novatrade.cli.commands.data`` CSV loader.
    Returns (stage, h1_candles, h4_candles).
    """
    t0 = time.monotonic()
    stage = PipelineStage(name="load_data")
    h1_candles: list = []
    h4_candles: list = []

    data_path = Path(config.data_path)

    try:
        # Attempt new data loader first (Phase 1/2 parallel build)
        from novatrade.data.loader import load_candles  # type: ignore[import-not-found]

        h1_candles = load_candles(data_path, symbol=config.pair, timeframe=config.timeframe)
        stage.details["loader"] = "novatrade.data.loader"
    except ImportError:
        # Fallback to existing CSV loader
        from novatrade.cli.commands.data import load_candles_csv

        h1_candles = load_candles_csv(data_path, symbol=config.pair, timeframe=config.timeframe)
        stage.details["loader"] = "novatrade.cli.commands.data (CSV fallback)"

    stage.details["h1_bars"] = len(h1_candles)

    # Derive H4 candles from H1 if primary TF is H1
    if config.timeframe == "H1" and h1_candles:
        from novatrade.cli.commands.data import aggregate_h1_to_h4

        h4_candles = aggregate_h1_to_h4(h1_candles)
        stage.details["h4_bars"] = len(h4_candles)
        stage.details["h4_source"] = "aggregated_from_h1"
    elif config.timeframe == "H4" and h1_candles:
        # H4 provided directly; try to find H1 or use H4 as-is
        h4_candles = h1_candles
        h1_candles = []  # no H1 available
        stage.details["note"] = "H4 provided as primary; H1 not available"
    else:
        # Try loading companion H4 file
        h4_path = data_path.parent / f"{config.pair}_H4.csv"
        if h4_path.exists():
            from novatrade.cli.commands.data import load_candles_csv

            h4_candles = load_candles_csv(h4_path, symbol=config.pair, timeframe="H4")
            stage.details["h4_bars"] = len(h4_candles)
            stage.details["h4_source"] = "companion_file"
        else:
            stage.details["h4_source"] = "not_found"

    if not h1_candles and not h4_candles:
        stage.status = "error"
        stage.details["error"] = "No candle data loaded"
    else:
        stage.status = "ok"

    stage.duration_ms = int((time.monotonic() - t0) * 1000)
    return stage, h1_candles, h4_candles


def _stage_validate_data(
    config: PipelineConfig,
    h1_candles: list,
    h4_candles: list,
) -> tuple[PipelineStage, list, list, int]:
    """Validate and optionally auto-fix candle data.

    Tries ``novatrade.data.quality.validate_candles`` (parallel build),
    falls back to basic sanity checks if unavailable.
    Returns (stage, h1_candles, h4_candles, issue_count).
    """
    t0 = time.monotonic()
    stage = PipelineStage(name="validate_data")
    issue_count = 0

    if not config.validate_data:
        stage.status = "skipped"
        stage.details["reason"] = "validation disabled"
        stage.duration_ms = int((time.monotonic() - t0) * 1000)
        return stage, h1_candles, h4_candles, 0

    try:
        from novatrade.data.quality import validate_candles  # type: ignore[import-not-found]

        result = validate_candles(h1_candles)
        h1_candles = result.candles if hasattr(result, "candles") else h1_candles
        issue_count = result.issue_count if hasattr(result, "issue_count") else 0
        stage.details["validator"] = "novatrade.data.quality"
        stage.details["issues"] = issue_count
        if hasattr(result, "fixes_applied"):
            stage.details["fixes_applied"] = result.fixes_applied
    except ImportError:
        # Fallback: basic sanity checks
        issues = _basic_candle_validation(h1_candles)
        issue_count = len(issues)
        stage.details["validator"] = "basic_fallback"
        stage.details["issues"] = issue_count
        if issues:
            stage.details["issue_list"] = issues[:10]  # cap for readability

        if config.auto_fix_data and h1_candles:
            h1_candles, fixes = _basic_candle_fixes(h1_candles)
            stage.details["fixes_applied"] = fixes

    if issue_count > 0:
        stage.status = "warning"
    else:
        stage.status = "ok"

    stage.duration_ms = int((time.monotonic() - t0) * 1000)
    return stage, h1_candles, h4_candles, issue_count


def _basic_candle_validation(candles: list) -> list[str]:
    """Lightweight candle sanity checks (fallback when quality module missing)."""
    issues: list[str] = []
    if not candles:
        issues.append("Empty candle list")
        return issues

    for i, c in enumerate(candles):
        if c.high < c.low:
            issues.append(f"Bar {i}: high ({c.high}) < low ({c.low})")
        if c.open <= 0 or c.close <= 0:
            issues.append(f"Bar {i}: non-positive open/close")
        if c.volume < 0:
            issues.append(f"Bar {i}: negative volume")
        if i > 0 and c.timestamp <= candles[i - 1].timestamp:
            issues.append(f"Bar {i}: timestamp not increasing")

    return issues


def _basic_candle_fixes(candles: list) -> tuple[list, int]:
    """Basic auto-fix: remove bars with high < low or non-positive prices."""
    original_len = len(candles)
    fixed = [c for c in candles if c.high >= c.low and c.open > 0 and c.close > 0 and c.volume >= 0]
    # Re-sort by timestamp
    fixed.sort(key=lambda c: c.timestamp)
    removed = original_len - len(fixed)
    return fixed, removed


def _stage_parse_doctrine(
    config: PipelineConfig,
) -> tuple[PipelineStage, Any]:
    """Load or translate a StrategyDoctrine.

    Returns (stage, doctrine_or_None).
    """
    t0 = time.monotonic()
    stage = PipelineStage(name="parse_doctrine")
    doctrine = None

    try:
        if config.doctrine_path is not None:
            from novatrade.doctrine.schema import StrategyDoctrine

            doctrine = StrategyDoctrine.from_yaml(Path(config.doctrine_path))
            stage.details["source"] = "yaml_file"
            stage.details["path"] = str(config.doctrine_path)
        elif config.concept:
            from novatrade.doctrine.translator import (
                concept_to_doctrine,
                detect_strategy_family,
            )

            family = detect_strategy_family(config.concept)
            doctrine = concept_to_doctrine(config.concept, pair=config.pair, timeframe=config.timeframe)
            stage.details["source"] = "concept_translation"
            stage.details["family"] = family
            stage.details["concept"] = config.concept[:120]
        else:
            # No doctrine and no concept -- use a default IRB doctrine
            from novatrade.doctrine.translator import concept_to_doctrine

            doctrine = concept_to_doctrine("IRB reversal", pair=config.pair, timeframe=config.timeframe)
            stage.details["source"] = "default_irb"

        if doctrine is not None:
            stage.details["doctrine_name"] = doctrine.name
            stage.details["mutable_param_count"] = len(doctrine.mutable_params)
            stage.status = "ok"
        else:
            stage.status = "error"
            stage.details["error"] = "Failed to produce doctrine"

    except Exception as exc:
        stage.status = "error"
        stage.details["error"] = str(exc)
        log.exception("Doctrine parsing failed")

    stage.duration_ms = int((time.monotonic() - t0) * 1000)
    return stage, doctrine


def _stage_generate_config(
    doctrine: Any,
) -> tuple[PipelineStage, Any]:
    """Convert doctrine to an initial StrategyConfig.

    Returns (stage, config_or_None).
    """
    t0 = time.monotonic()
    stage = PipelineStage(name="generate_config")
    config = None

    try:
        from novatrade.doctrine.bridge import doctrine_to_config

        config = doctrine_to_config(doctrine)
        stage.details["config_name"] = config.name
        stage.details["config_hash"] = config.content_hash()
        stage.status = "ok"
    except Exception as exc:
        stage.status = "error"
        stage.details["error"] = str(exc)
        log.exception("Config generation failed")

    stage.duration_ms = int((time.monotonic() - t0) * 1000)
    return stage, config


def _stage_execute_single(
    config: Any,
    h1_candles: list,
    h4_candles: list,
    dataset_hash: str,
    doctrine_hash: str,
    pipeline_cfg: PipelineConfig,
) -> tuple[PipelineStage, PipelineResult]:
    """Run a single backtest and populate PipelineResult."""
    t0 = time.monotonic()
    stage = PipelineStage(name="execute_single")
    result = PipelineResult(mode=PipelineMode.SINGLE)

    try:
        from novatrade.cli.commands.run import execute_backtest
        from novatrade.storage.experiment_db import ExperimentDB

        db = ExperimentDB(pipeline_cfg.db_path)
        run_result = execute_backtest(
            config=config,
            h1_candles=h1_candles,
            h4_candles=h4_candles,
            dataset_hash=dataset_hash,
            doctrine_hash=doctrine_hash,
            db=db,
        )

        result.total_experiments = 1
        result.best_experiment_id = run_result.experiment_id
        result.best_score = run_result.scout_score or 0.0

        passed = run_result.status in ("ranked", "valid")
        result.gate_pass_rate = 1.0 if passed else 0.0

        if run_result.metrics:
            m = run_result.metrics
            result.top_survivors = [
                {
                    "experiment_id": run_result.experiment_id,
                    "score": run_result.scout_score or 0.0,
                    "profit_factor": m.profit_factor,
                    "max_dd": m.max_drawdown_pct,
                    "trade_count": m.total_completed_trades,
                    "status": run_result.status,
                }
            ]

        stage.details["experiment_id"] = run_result.experiment_id
        stage.details["status"] = run_result.status
        stage.details["scout_score"] = run_result.scout_score
        stage.status = "ok"

    except Exception as exc:
        stage.status = "error"
        stage.details["error"] = str(exc)
        result.errors.append(f"Single backtest failed: {exc}")
        log.exception("Single backtest execution failed")

    stage.duration_ms = int((time.monotonic() - t0) * 1000)
    return stage, result


def _stage_execute_sweep(
    config: Any,
    h1_candles: list,
    h4_candles: list,
    experiments: int,
) -> tuple[PipelineStage, PipelineResult]:
    """Run N random parameter sweeps and populate PipelineResult."""
    t0 = time.monotonic()
    stage = PipelineStage(name="execute_sweep")
    result = PipelineResult(mode=PipelineMode.SWEEP)

    try:
        from novatrade.optimization.sweep_engine import SweepConfig, run_sweep

        sweep_config = SweepConfig(
            n_experiments=experiments,
            method="latin_hypercube",
        )
        sweep_result = run_sweep(config, h1_candles, h4_candles, sweep_config)

        result.total_experiments = sweep_result.total_experiments
        ranked = sweep_result.ranked_count
        total = sweep_result.total_experiments
        result.gate_pass_rate = ranked / total if total > 0 else 0.0

        if sweep_result.best:
            result.best_score = sweep_result.best.scout_score
            result.best_experiment_id = f"sweep_{sweep_result.best.index}"

        # Top 5 survivors
        top5 = [e for e in sweep_result.experiments if e.status == "ranked"][:5]
        result.top_survivors = [
            {
                "experiment_id": f"sweep_{e.index}",
                "score": e.scout_score,
                "profit_factor": e.profit_factor,
                "max_dd": None,  # sweep engine doesn't track DD per experiment
                "trade_count": e.trade_count,
                "status": e.status,
            }
            for e in top5
        ]

        stage.details["total"] = sweep_result.total_experiments
        stage.details["ranked"] = sweep_result.ranked_count
        stage.details["gated"] = sweep_result.gated_count
        stage.details["crashed"] = sweep_result.crashed_count
        stage.details["elapsed_s"] = sweep_result.elapsed_seconds
        stage.status = "ok"

    except Exception as exc:
        stage.status = "error"
        stage.details["error"] = str(exc)
        result.errors.append(f"Sweep execution failed: {exc}")
        log.exception("Sweep execution failed")

    stage.duration_ms = int((time.monotonic() - t0) * 1000)
    return stage, result


def _stage_execute_campaign(
    config: Any,
    doctrine: Any,
    h1_candles: list,
    h4_candles: list,
    dataset_hash: str,
    doctrine_hash: str,
    pipeline_cfg: PipelineConfig,
) -> tuple[PipelineStage, PipelineResult]:
    """Run a full AutoResearch campaign and populate PipelineResult."""
    t0 = time.monotonic()
    stage = PipelineStage(name="execute_campaign")
    result = PipelineResult(mode=PipelineMode.CAMPAIGN)

    try:
        from novatrade.optimization.campaign_engine import CampaignConfig, run_campaign
        from novatrade.storage.experiment_db import ExperimentDB

        # Build CampaignConfig from doctrine campaign settings
        max_experiments = pipeline_cfg.experiments
        stagnation_limit = 50
        wall_clock_seconds = 14400.0  # 4 hours default

        if doctrine is not None and hasattr(doctrine, "campaign"):
            dc = doctrine.campaign
            max_experiments = min(pipeline_cfg.experiments, dc.max_experiments)
            stagnation_limit = dc.stagnation_limit
            wall_clock_seconds = dc.wall_clock_limit_hours * 3600.0

        campaign_dir = Path(pipeline_cfg.output_dir) / "campaigns"
        campaign_dir.mkdir(parents=True, exist_ok=True)

        db = ExperimentDB(pipeline_cfg.db_path)

        campaign_config = CampaignConfig(
            max_experiments=max_experiments,
            max_wall_clock_seconds=wall_clock_seconds,
            stagnation_limit=stagnation_limit,
        )

        campaign_result = run_campaign(
            base_config=config,
            h1_candles=h1_candles,
            h4_candles=h4_candles,
            campaign_config=campaign_config,
            dataset_hash=dataset_hash,
            doctrine_hash=doctrine_hash,
            campaign_dir=campaign_dir,
            db=db,
        )

        result.total_experiments = campaign_result.total_experiments
        result.best_score = campaign_result.best_scout_score
        result.best_experiment_id = campaign_result.best_experiment_id or ""

        # Gate pass rate from status counts
        status = campaign_result.status_counts
        total = campaign_result.total_experiments
        ranked = status.get("ranked", 0) + status.get("valid", 0)
        result.gate_pass_rate = ranked / total if total > 0 else 0.0

        # Build top survivors from improvement history
        result.top_survivors = [
            {
                "experiment_id": imp["experiment_id"],
                "score": imp["scout_score"],
                "profit_factor": None,
                "max_dd": None,
                "trade_count": None,
                "status": "ranked",
            }
            for imp in campaign_result.improvements[-5:]
        ]

        stage.details["campaign_id"] = campaign_result.campaign_id
        stage.details["total"] = campaign_result.total_experiments
        stage.details["end_reason"] = campaign_result.end_reason
        stage.details["status_counts"] = campaign_result.status_counts
        stage.details["elapsed_s"] = campaign_result.elapsed_seconds
        stage.details["improvements"] = len(campaign_result.improvements)
        stage.status = "ok"

    except Exception as exc:
        stage.status = "error"
        stage.details["error"] = str(exc)
        result.errors.append(f"Campaign execution failed: {exc}")
        log.exception("Campaign execution failed")

    stage.duration_ms = int((time.monotonic() - t0) * 1000)
    return stage, result


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def run_pipeline(config: PipelineConfig) -> PipelineResult:
    """Execute the full NovaTrade evaluation pipeline.

    Chains: load data -> validate -> derive timeframes -> parse doctrine ->
    generate config -> execute (single/sweep/campaign) -> collect results.

    Args:
        config: Pipeline configuration specifying data, mode, and parameters.

    Returns:
        PipelineResult with stage timings, gate pass rates, and top survivors.
    """
    log.info(
        "Starting pipeline: mode=%s pair=%s tf=%s experiments=%d",
        config.mode.value,
        config.pair,
        config.timeframe,
        config.experiments,
    )

    stages: list[PipelineStage] = []
    errors: list[str] = []

    # --- Stage 1: Load data ------------------------------------------------
    stage_load, h1_candles, h4_candles = _stage_load_data(config)
    stages.append(stage_load)

    if stage_load.status == "error":
        errors.append(stage_load.details.get("error", "Data load failed"))
        return PipelineResult(
            mode=config.mode,
            stages=stages,
            errors=errors,
        )

    # --- Stage 2: Validate data --------------------------------------------
    stage_val, h1_candles, h4_candles, quality_issues = _stage_validate_data(config, h1_candles, h4_candles)
    stages.append(stage_val)

    if stage_val.status == "error":
        errors.append(stage_val.details.get("error", "Validation failed"))

    # --- Stage 3: Parse doctrine -------------------------------------------
    stage_doc, doctrine = _stage_parse_doctrine(config)
    stages.append(stage_doc)

    if stage_doc.status == "error" or doctrine is None:
        errors.append(stage_doc.details.get("error", "Doctrine parsing failed"))
        return PipelineResult(
            mode=config.mode,
            data_quality_issues=quality_issues,
            stages=stages,
            errors=errors,
        )

    doctrine_name = doctrine.name if hasattr(doctrine, "name") else ""
    doctrine_hash = doctrine.content_hash() if hasattr(doctrine, "content_hash") else ""

    # --- Stage 4: Generate config ------------------------------------------
    stage_cfg, strategy_config = _stage_generate_config(doctrine)
    stages.append(stage_cfg)

    if stage_cfg.status == "error" or strategy_config is None:
        errors.append(stage_cfg.details.get("error", "Config generation failed"))
        return PipelineResult(
            mode=config.mode,
            doctrine_name=doctrine_name,
            data_quality_issues=quality_issues,
            stages=stages,
            errors=errors,
        )

    # Compute dataset hash for reproducibility
    dataset_hash = _candle_hash(h1_candles) if h1_candles else ""

    # --- Stage 5: Execute --------------------------------------------------
    if config.mode == PipelineMode.SINGLE:
        stage_exec, result = _stage_execute_single(
            strategy_config,
            h1_candles,
            h4_candles,
            dataset_hash,
            doctrine_hash,
            config,
        )
    elif config.mode == PipelineMode.SWEEP:
        stage_exec, result = _stage_execute_sweep(
            strategy_config,
            h1_candles,
            h4_candles,
            config.experiments,
        )
    elif config.mode == PipelineMode.CAMPAIGN:
        stage_exec, result = _stage_execute_campaign(
            strategy_config,
            doctrine,
            h1_candles,
            h4_candles,
            dataset_hash,
            doctrine_hash,
            config,
        )
    else:
        stage_exec = PipelineStage(name="execute", status="error", details={"error": f"Unknown mode: {config.mode}"})
        result = PipelineResult(mode=config.mode)
        errors.append(f"Unknown pipeline mode: {config.mode}")

    stages.append(stage_exec)

    # --- Assemble final result ---------------------------------------------
    result.doctrine_name = doctrine_name
    result.data_quality_issues = quality_issues
    result.stages = stages
    result.errors = errors + result.errors

    log.info(
        "Pipeline complete: mode=%s experiments=%d best=%.4f gate_pass=%.1f%%",
        result.mode.value,
        result.total_experiments,
        result.best_score,
        result.gate_pass_rate * 100,
    )

    return result
