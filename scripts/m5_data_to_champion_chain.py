#!/usr/bin/env python3
"""M5 Data-to-Champion Automated Pipeline Chain.

Watches for the M5 data fetch to complete, then automatically runs:
  1. Snapshot detection — find the new 2-year M5 snapshot
  2. Autoresearch — 60 pop x 50 gen evolutionary optimization
  3. Walk-forward validation — top 5 organisms
  4. Champion export — save best config as deployment YAML

Can also be run manually after data is already available.

Usage:
    # Auto-watch mode: wait for fetch, then run full pipeline
    python scripts/m5_data_to_champion_chain.py

    # Manual mode: run on a specific snapshot (skip wait)
    python scripts/m5_data_to_champion_chain.py --snapshot-id <ID>

    # Custom parameters
    python scripts/m5_data_to_champion_chain.py --population 100 --generations 80

    # Quick test (small pop/gen)
    python scripts/m5_data_to_champion_chain.py --quick
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from novatrade.backtest.environment import BacktestEnvironment
    from novatrade.optimization.autoresearch import AutoResearchResult

# Ensure repo root is importable
_project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_project_root))

# Limit BLAS threads — backtests are single-threaded
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("m5_chain")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SNAPSHOT_DIR = _project_root / "data" / "snapshots"
CONFIG_DIR = _project_root / "data" / "configs"
LOGS_DIR = _project_root / "LOGS"
OUTPUT_DIR = _project_root / "OUTPUT"

FETCH_PROCESS_NAME = "fetch_m5_snapshot"

# Minimum M5 bars to consider a snapshot "full" (2 years ~ 200k+)
MIN_FULL_M5_BARS = 50_000

# Polling interval while waiting for fetch (seconds)
POLL_INTERVAL = 30

# Max wait time for fetch (hours)
MAX_WAIT_HOURS = 8

# ---------------------------------------------------------------------------
# Interrupt handling
# ---------------------------------------------------------------------------

_interrupted = False


def _handle_interrupt(signum: int, frame: object) -> None:
    global _interrupted
    _interrupted = True
    log.warning("Interrupt received — will finish current step and save progress.")


signal.signal(signal.SIGINT, _handle_interrupt)

# ---------------------------------------------------------------------------
# Pipeline step tracking
# ---------------------------------------------------------------------------


class PipelineStep:
    """Track a single pipeline step with timing and status."""

    def __init__(self, name: str):
        self.name = name
        self.status = "PENDING"
        self.message = ""
        self.duration_s = 0.0
        self.details: dict = {}
        self._start = 0.0

    def start(self) -> PipelineStep:
        self._start = time.monotonic()
        self.status = "RUNNING"
        log.info("=" * 60)
        log.info("STEP: %s", self.name)
        log.info("=" * 60)
        return self

    def pass_(self, msg: str = "") -> PipelineStep:
        self.duration_s = time.monotonic() - self._start
        self.status = "PASS"
        self.message = msg
        log.info("  [PASS] %s (%.1fs) %s", self.name, self.duration_s, msg)
        return self

    def fail(self, msg: str = "") -> PipelineStep:
        self.duration_s = time.monotonic() - self._start
        self.status = "FAIL"
        self.message = msg
        log.error("  [FAIL] %s (%.1fs) %s", self.name, self.duration_s, msg)
        return self

    def skip(self, msg: str = "") -> PipelineStep:
        self.duration_s = 0.0
        self.status = "SKIP"
        self.message = msg
        log.info("  [SKIP] %s — %s", self.name, msg)
        return self


class Pipeline:
    """Track overall pipeline execution."""

    def __init__(self):
        self.steps: list[PipelineStep] = []
        self.started_at = datetime.now(tz=timezone.utc)
        self.finished_at: datetime | None = None

    def step(self, name: str) -> PipelineStep:
        s = PipelineStep(name)
        self.steps.append(s)
        return s

    def finish(self) -> None:
        self.finished_at = datetime.now(tz=timezone.utc)

    @property
    def overall(self) -> str:
        if any(s.status == "FAIL" for s in self.steps):
            return "FAIL"
        if all(s.status in ("PASS", "SKIP") for s in self.steps):
            return "PASS"
        return "PARTIAL"

    @property
    def total_duration_s(self) -> float:
        return sum(s.duration_s for s in self.steps)

    def report(self) -> str:
        lines = [
            "=" * 70,
            "  M5 DATA-TO-CHAMPION PIPELINE REPORT",
            f"  Started:  {self.started_at.isoformat()}",
            f"  Finished: {self.finished_at.isoformat() if self.finished_at else 'in progress'}",
            f"  Result:   {self.overall}",
            f"  Duration: {self.total_duration_s:.1f}s ({self.total_duration_s / 60:.1f} min)",
            "=" * 70,
            "",
        ]
        for i, s in enumerate(self.steps, 1):
            icon = {"PASS": "+", "FAIL": "!", "SKIP": "-", "RUNNING": "~", "PENDING": "?"}[s.status]
            lines.append(f"  [{icon}] Step {i}: {s.name} — {s.status} ({s.duration_s:.1f}s)")
            if s.message:
                lines.append(f"      {s.message}")
            for k, v in s.details.items():
                lines.append(f"      {k}: {v}")
            lines.append("")
        passed = sum(1 for s in self.steps if s.status == "PASS")
        total = len(self.steps)
        lines.append(f"  Pipeline: {passed}/{total} steps passed — {self.overall}")
        lines.append("=" * 70)
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Step 1: Wait for data fetch to complete
# ---------------------------------------------------------------------------


def _is_fetch_running() -> bool:
    """Check if the M5 data fetch process is still running."""
    try:
        result = subprocess.run(
            ["pgrep", "-f", FETCH_PROCESS_NAME],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def _get_fetch_progress() -> str:
    """Get a rough progress estimate from the Dukascopy cache."""
    cache_dir = _project_root / "data" / "dukascopy" / "EURUSD"
    if not cache_dir.exists():
        return "no cache found"
    try:
        # Count .bi5 files
        total_files = sum(1 for _ in cache_dir.rglob("*.bi5"))
        # 2 years = ~17520 hours
        pct = (total_files / 17520 * 100) if total_files > 0 else 0
        return f"{total_files}/~17520 hours ({pct:.0f}%)"
    except Exception:
        return "unknown"


def wait_for_data(step: PipelineStep, max_wait_hours: float) -> bool:
    """Wait for the M5 data fetch process to finish.

    Returns True if fetch completed (or wasn't running), False on timeout.
    """
    step.start()

    if not _is_fetch_running():
        step.pass_("Fetch process not running — data may already be available")
        return True

    log.info("Fetch process detected. Waiting for completion...")
    deadline = time.monotonic() + max_wait_hours * 3600
    last_log = 0.0

    while time.monotonic() < deadline:
        if _interrupted:
            step.fail("Interrupted while waiting for fetch")
            return False

        if not _is_fetch_running():
            step.pass_("Fetch process completed")
            return True

        now = time.monotonic()
        if now - last_log >= 300:  # Log progress every 5 minutes
            progress = _get_fetch_progress()
            log.info("  Still waiting for fetch... progress: %s", progress)
            last_log = now

        time.sleep(POLL_INTERVAL)

    step.fail(f"Timed out after {max_wait_hours}h waiting for fetch")
    return False


# ---------------------------------------------------------------------------
# Step 2: Find the M5 snapshot
# ---------------------------------------------------------------------------


def find_m5_snapshot(
    step: PipelineStep,
    snapshot_id: str | None = None,
    min_bars: int = MIN_FULL_M5_BARS,
) -> str | None:
    """Find the best M5 snapshot to use.

    If snapshot_id is provided, validates it exists and is M5.
    Otherwise, finds the latest M5 snapshot with >= min_bars.
    """
    step.start()

    from novatrade.storage.data_snapshot import list_snapshots

    all_snaps = list_snapshots(SNAPSHOT_DIR)
    m5_snaps = [s for s in all_snaps if s.primary_tf == "M5"]

    step.details["total_snapshots"] = len(all_snaps)
    step.details["m5_snapshots"] = len(m5_snaps)

    if snapshot_id:
        # Use specified snapshot
        match = [s for s in all_snaps if s.snapshot_id == snapshot_id]
        if not match:
            step.fail(f"Snapshot {snapshot_id} not found")
            return None
        snap = match[0]
        step.details["snapshot_id"] = snap.snapshot_id
        step.details["primary_tf"] = snap.primary_tf
        step.details["bars"] = snap.primary_bars
        step.details["date_range"] = f"{snap.date_range_start} -> {snap.date_range_end}"
        step.pass_(f"Using specified snapshot: {snap.snapshot_id} ({snap.primary_bars} bars)")
        return snap.snapshot_id

    # Auto-detect: find latest M5 snapshot with enough bars
    if not m5_snaps:
        step.fail("No M5 snapshots found in data/snapshots/")
        return None

    # Sort by bar count descending (largest first)
    m5_snaps.sort(key=lambda s: s.primary_bars, reverse=True)
    best = m5_snaps[0]

    step.details["snapshot_id"] = best.snapshot_id
    step.details["bars"] = best.primary_bars
    step.details["date_range"] = f"{best.date_range_start} -> {best.date_range_end}"

    if best.primary_bars < min_bars:
        log.warning(
            "Largest M5 snapshot has only %d bars (need %d for full run). "
            "Proceeding anyway but results may not be meaningful.",
            best.primary_bars,
            min_bars,
        )

    step.pass_(
        f"Found M5 snapshot: {best.snapshot_id} "
        f"({best.primary_bars} M5 + {best.higher_bars} H1 bars, "
        f"{best.date_range_start} -> {best.date_range_end})"
    )
    return best.snapshot_id


# ---------------------------------------------------------------------------
# Step 3: Run autoresearch
# ---------------------------------------------------------------------------


def run_autoresearch_step(
    step: PipelineStep,
    snapshot_id: str,
    population: int,
    generations: int,
    seed: int,
    checkpoint_every: int,
) -> tuple[AutoResearchResult, BacktestEnvironment] | None:
    """Run the full evolutionary autoresearch.

    Returns (AutoResearchResult, BacktestEnvironment) or None on failure.
    """
    step.start()

    from novatrade.backtest.environment import BacktestEnvironment
    from novatrade.cli.config_schema import StrategyConfig
    from novatrade.optimization.autoresearch import AutoResearchConfig, run_autoresearch
    from novatrade.storage.data_snapshot import load_snapshot

    # Load snapshot
    log.info("Loading snapshot %s ...", snapshot_id)
    try:
        primary_candles, higher_candles, meta = load_snapshot(snapshot_id, SNAPSHOT_DIR)
    except (FileNotFoundError, ValueError) as exc:
        step.fail(f"Failed to load snapshot: {exc}")
        return None

    step.details["m5_bars"] = len(primary_candles)
    step.details["h1_bars"] = len(higher_candles)
    step.details["date_range"] = f"{meta.date_range_start} -> {meta.date_range_end}"

    # Configure
    ar_config = AutoResearchConfig(
        population_size=population,
        generations=generations,
        seed=seed,
        min_trades=50,
        target_scout_score=0.25,
        verbose=True,
    )

    m5_env = BacktestEnvironment.for_m5()

    log.info(
        "Autoresearch config: pop=%d, gens=%d, seed=%d on %d M5 bars",
        population,
        generations,
        seed,
        len(primary_candles),
    )

    # Run
    result = run_autoresearch(
        h1_candles=primary_candles,
        h4_candles=higher_candles,
        config=ar_config,
        parameter_bounds=StrategyConfig.M5_PARAMETER_BOUNDS,  # type: ignore[arg-type]
        base_environment=m5_env,
    )

    step.details["generations_run"] = result.generations_run
    step.details["total_backtests"] = result.total_backtests
    step.details["viable_count"] = result.viable_count
    step.details["elapsed_seconds"] = result.elapsed_seconds

    if result.best is None or result.best.scout_score <= 0:
        step.fail(
            f"No viable results after {result.generations_run} generations "
            f"({result.total_backtests} backtests, {result.elapsed_seconds:.0f}s)"
        )
        return None

    step.details["best_score"] = round(result.best.scout_score, 4)
    step.details["best_pf"] = round(result.best.profit_factor, 2)
    step.details["best_trades"] = result.best.trade_count
    step.details["best_pips"] = round(result.best.net_pips, 1)
    step.details["top_10_count"] = len(result.top_10)

    # Save checkpoint
    from scripts.run_m5_autoresearch import save_checkpoint

    checkpoint_path = save_checkpoint(result, CONFIG_DIR, result.generations_run)
    step.details["checkpoint"] = str(checkpoint_path)

    step.pass_(
        f"Best: score={result.best.scout_score:.4f} PF={result.best.profit_factor:.2f} "
        f"trades={result.best.trade_count} pips={result.best.net_pips:+.0f} "
        f"({result.generations_run} gens, {result.total_backtests} backtests, "
        f"{result.elapsed_seconds:.0f}s)"
    )

    return result, m5_env


# ---------------------------------------------------------------------------
# Step 4: Walk-forward validation
# ---------------------------------------------------------------------------


def run_walkforward_step(
    step: PipelineStep,
    ar_result: AutoResearchResult,
    m5_env: BacktestEnvironment,
    snapshot_id: str,
    top_n: int,
) -> list | None:
    """Walk-forward validate top organisms.

    Returns list of (Organism, WalkForwardResult) or None on failure.
    """
    step.start()

    from novatrade.storage.data_snapshot import load_snapshot
    from scripts.run_m5_autoresearch import (
        M5_WALKFORWARD_CONFIG,
        validate_top_organisms,
    )

    # Load candles again (autoresearch doesn't expose them)
    primary_candles, higher_candles, _ = load_snapshot(snapshot_id, SNAPSHOT_DIR)

    top_organisms = ar_result.top_10[:top_n]
    step.details["candidates"] = len(top_organisms)

    if not top_organisms:
        step.fail("No organisms to validate")
        return None

    # Check data sufficiency
    min_bars = M5_WALKFORWARD_CONFIG.train_bars + M5_WALKFORWARD_CONFIG.test_bars + M5_WALKFORWARD_CONFIG.holdout_bars
    step.details["min_bars_needed"] = min_bars
    step.details["bars_available"] = len(primary_candles)

    if len(primary_candles) < min_bars:
        log.warning(
            "Only %d M5 bars, walk-forward needs %d. Skipping.",
            len(primary_candles),
            min_bars,
        )
        step.skip(f"Insufficient data: {len(primary_candles)} bars < {min_bars} needed")
        return None

    log.info(
        "Validating top %d organisms with walk-forward (train=%d, test=%d, holdout=%d)",
        len(top_organisms),
        M5_WALKFORWARD_CONFIG.train_bars,
        M5_WALKFORWARD_CONFIG.test_bars,
        M5_WALKFORWARD_CONFIG.holdout_bars,
    )

    wf_results = validate_top_organisms(
        organisms=top_organisms,
        m5_candles=primary_candles,
        h1_candles=higher_candles,
        m5_env=m5_env,
        wf_config=M5_WALKFORWARD_CONFIG,
    )

    if not wf_results:
        step.fail("All walk-forward validations failed")
        return None

    step.details["validated"] = len(wf_results)
    passing = sum(1 for _, wf in wf_results if wf.overall_passed)
    step.details["passing"] = passing

    best_org, best_wf = wf_results[0]
    step.details["best_oos_score"] = round(best_wf.median_oos_score, 4)
    step.details["best_oos_is_ratio"] = round(best_wf.median_oos_is_ratio, 4)
    step.details["best_passed"] = best_wf.overall_passed

    step.pass_(
        f"{passing}/{len(wf_results)} passed WF. "
        f"Best OOS={best_wf.median_oos_score:.4f}, ratio={best_wf.median_oos_is_ratio:.4f}"
    )

    return wf_results


# ---------------------------------------------------------------------------
# Step 5: Save champion config
# ---------------------------------------------------------------------------


def save_champion_step(
    step: PipelineStep,
    ar_result: AutoResearchResult,
    wf_results: list | None,
    snapshot_id: str,
    population: int,
    generations: int,
) -> Path | None:
    """Determine champion and save deployment YAML.

    Returns path to saved YAML or None on failure.
    """
    step.start()

    from scripts.run_m5_autoresearch import save_champion_yaml

    champion = ar_result.best
    champion_wf = None

    if wf_results:
        # Prefer WF-passing organism with best OOS score
        passing = [(org, wf) for org, wf in wf_results if wf.overall_passed]
        if passing:
            champion, champion_wf = passing[0]
            step.details["selection"] = "walk-forward validated (passed)"
        else:
            champion, champion_wf = wf_results[0]
            step.details["selection"] = "best OOS (none passed WF — CAUTION: may be overfit)"
    else:
        step.details["selection"] = "best in-sample (no walk-forward)"

    if champion is None:
        step.fail("No viable champion organism found")
        return None

    step.details["organism_id"] = champion.organism_id
    step.details["scout_score"] = round(champion.scout_score, 4)
    step.details["profit_factor"] = round(champion.profit_factor, 2)
    step.details["trade_count"] = champion.trade_count
    step.details["net_pips"] = round(champion.net_pips, 1)

    if champion_wf:
        step.details["wf_oos_score"] = round(champion_wf.median_oos_score, 4)
        step.details["wf_oos_is_ratio"] = round(champion_wf.median_oos_is_ratio, 4)
        step.details["wf_passed"] = champion_wf.overall_passed

    try:
        yaml_path = save_champion_yaml(
            org=champion,
            wf_result=champion_wf,
            output_dir=CONFIG_DIR,
            generations=generations,
            population=population,
            snapshot_id=snapshot_id,
        )
    except Exception as exc:
        step.fail(f"Failed to save champion YAML: {exc}")
        return None

    step.details["yaml_path"] = str(yaml_path)
    step.pass_(
        f"Saved {yaml_path.name} — "
        f"score={champion.scout_score:.4f} PF={champion.profit_factor:.2f} "
        f"trades={champion.trade_count}"
    )

    return yaml_path


# ---------------------------------------------------------------------------
# Step 6: Write pipeline report
# ---------------------------------------------------------------------------


def write_report(pipeline: Pipeline, yaml_path: Path | None) -> Path:
    """Write the full pipeline report to OUTPUT/."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    report_path = OUTPUT_DIR / f"m5_autoresearch_pipeline_{ts}.md"

    lines = [
        "# M5 Data-to-Champion Pipeline Report",
        "",
        f"**Date**: {pipeline.started_at.strftime('%Y-%m-%d %H:%M UTC')}",
        f"**Result**: {pipeline.overall}",
        f"**Duration**: {pipeline.total_duration_s:.1f}s ({pipeline.total_duration_s / 60:.1f} min)",
        "",
    ]

    if yaml_path:
        lines.extend(
            [
                "## Champion Config",
                f"**File**: `{yaml_path}`",
                "",
            ]
        )

    lines.extend(
        [
            "## Pipeline Steps",
            "",
        ]
    )

    for i, s in enumerate(pipeline.steps, 1):
        icon = {"PASS": "OK", "FAIL": "FAIL", "SKIP": "SKIP"}[s.status]
        lines.append(f"### Step {i}: {s.name} [{icon}] ({s.duration_s:.1f}s)")
        if s.message:
            lines.append(f"{s.message}")
        lines.append("")
        if s.details:
            lines.append("| Key | Value |")
            lines.append("|-----|-------|")
            for k, v in s.details.items():
                lines.append(f"| {k} | {v} |")
            lines.append("")

    lines.extend(
        [
            "## Execution Log",
            "",
            "```",
            pipeline.report(),
            "```",
        ]
    )

    report_path.write_text("\n".join(lines))
    log.info("Report written to %s", report_path)
    return report_path


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="M5 Data-to-Champion Pipeline — automated fetch→autoresearch→WF→deploy chain",
    )
    parser.add_argument(
        "--snapshot-id",
        type=str,
        default=None,
        help="Use specific snapshot (skip fetch wait)",
    )
    parser.add_argument(
        "--population",
        type=int,
        default=60,
        help="Population size (default: 60)",
    )
    parser.add_argument(
        "--generations",
        type=int,
        default=50,
        help="Generations (default: 50)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed (default: 42)",
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=5,
        help="Top N organisms for walk-forward (default: 5)",
    )
    parser.add_argument(
        "--checkpoint-every",
        type=int,
        default=10,
        help="Checkpoint frequency in generations (default: 10)",
    )
    parser.add_argument(
        "--max-wait-hours",
        type=float,
        default=MAX_WAIT_HOURS,
        help=f"Max hours to wait for fetch (default: {MAX_WAIT_HOURS})",
    )
    parser.add_argument(
        "--skip-wait",
        action="store_true",
        help="Skip waiting for fetch (use latest available snapshot)",
    )
    parser.add_argument(
        "--skip-walkforward",
        action="store_true",
        help="Skip walk-forward validation",
    )
    parser.add_argument(
        "--quick",
        action="store_true",
        help="Quick test: 10 pop, 5 gen, skip walk-forward",
    )
    parser.add_argument(
        "--min-bars",
        type=int,
        default=MIN_FULL_M5_BARS,
        help=f"Minimum M5 bars for 'full' dataset (default: {MIN_FULL_M5_BARS})",
    )

    args = parser.parse_args()

    if args.quick:
        args.population = 10
        args.generations = 5
        args.skip_walkforward = True
        log.info("=== QUICK TEST MODE: pop=10, gen=5, no walk-forward ===")

    # Initialize pipeline
    pipeline = Pipeline()
    yaml_path: Path | None = None

    log.info("=" * 70)
    log.info("M5 DATA-TO-CHAMPION PIPELINE")
    log.info("  Population: %d", args.population)
    log.info("  Generations: %d", args.generations)
    log.info("  Seed: %d", args.seed)
    log.info("  Top-N for WF: %d", args.top_n)
    log.info("  Skip WF: %s", args.skip_walkforward)
    log.info("=" * 70)

    # --- Step 1: Wait for fetch ---
    if args.snapshot_id or args.skip_wait:
        s1 = pipeline.step("Wait for data fetch")
        s1.skip("Skipped — using specified/latest snapshot")
    else:
        s1 = pipeline.step("Wait for data fetch")
        if not wait_for_data(s1, args.max_wait_hours):
            pipeline.finish()
            print(pipeline.report())
            write_report(pipeline, None)
            return 1

    if _interrupted:
        pipeline.finish()
        print(pipeline.report())
        write_report(pipeline, None)
        return 1

    # --- Step 2: Find M5 snapshot ---
    s2 = pipeline.step("Find M5 snapshot")
    snapshot_id = find_m5_snapshot(s2, args.snapshot_id, args.min_bars)
    if not snapshot_id:
        pipeline.finish()
        print(pipeline.report())
        write_report(pipeline, None)
        return 1

    if _interrupted:
        pipeline.finish()
        print(pipeline.report())
        write_report(pipeline, None)
        return 1

    # --- Step 3: Autoresearch ---
    s3 = pipeline.step(f"Autoresearch ({args.population} pop x {args.generations} gen)")
    ar_result_tuple = run_autoresearch_step(
        s3,
        snapshot_id,
        args.population,
        args.generations,
        args.seed,
        args.checkpoint_every,
    )
    if not ar_result_tuple:
        pipeline.finish()
        print(pipeline.report())
        write_report(pipeline, None)
        return 1

    ar_result, m5_env = ar_result_tuple

    if _interrupted:
        pipeline.finish()
        print(pipeline.report())
        write_report(pipeline, None)
        return 1

    # --- Step 4: Walk-forward ---
    wf_results = None
    if args.skip_walkforward:
        s4 = pipeline.step("Walk-forward validation")
        s4.skip("Skipped by --skip-walkforward")
    else:
        s4 = pipeline.step(f"Walk-forward validation (top {args.top_n})")
        wf_results = run_walkforward_step(
            s4,
            ar_result,
            m5_env,
            snapshot_id,
            args.top_n,
        )

    # --- Step 5: Save champion ---
    s5 = pipeline.step("Save champion config")
    yaml_path = save_champion_step(
        s5,
        ar_result,
        wf_results,
        snapshot_id,
        args.population,
        args.generations,
    )

    # --- Finalize ---
    pipeline.finish()

    print()
    print(pipeline.report())

    write_report(pipeline, yaml_path)

    # Log to LOGS/
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    log_path = LOGS_DIR / f"m5_pipeline_{ts}.log"
    log_path.write_text(pipeline.report())

    # Print final champion info
    if yaml_path and yaml_path.exists():
        print()
        print("=" * 70)
        print("  CHAMPION CONFIG READY FOR DEPLOYMENT")
        print(f"  File: {yaml_path}")
        print("=" * 70)

    return 0 if pipeline.overall == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
