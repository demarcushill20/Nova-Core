"""Campaign-based AutoResearch engine — the Karpathy loop for trading.

Bounded, auditable experiment loop with keep/discard ratchet.
Terminates on budget, stagnation, crashes, wall clock, or manual stop.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from novatrade.cli.commands.run import BacktestRunResult, execute_backtest
from novatrade.cli.config_schema import StrategyConfig
from novatrade.optimization.strategies import (
    ExperimentStrategy,
    StrategyBudget,
    generate_combination_candidate,
    generate_explore_candidate,
    generate_local_candidate,
    select_strategy,
)
from novatrade.optimization.walkforward import WalkForwardConfig, run_walk_forward
from novatrade.storage.experiment_db import ExperimentDB

log = logging.getLogger("novatrade.optimization.campaign_engine")

_END_REASONS = frozenset(
    {
        "budget_exhausted",
        "stagnation",
        "max_crashes",
        "wall_clock",
        "manual_stop",
    }
)


@dataclass
class CampaignConfig:
    """Configuration for an AutoResearch campaign."""

    campaign_id: str = ""
    max_experiments: int = 200
    max_wall_clock_seconds: float = 21600.0
    stagnation_limit: int = 10  # Aligned with CampaignBudget.max_stagnant_experiments
    max_crashes: int = 5
    checkpoint_interval: int = 25
    strategy_budget: StrategyBudget | None = None
    walkforward_on_new_best: bool = True
    wf_config: WalkForwardConfig | None = None

    def __post_init__(self) -> None:
        if not self.campaign_id:
            self.campaign_id = f"campaign-{uuid.uuid4().hex[:8]}"
        if self.max_experiments > 10_000:
            self.max_experiments = 10_000
        if self.max_wall_clock_seconds > 86_400:
            self.max_wall_clock_seconds = 86_400


@dataclass
class CampaignCheckpoint:
    """Serialisable snapshot of campaign state."""

    campaign_id: str
    experiment_count: int
    best_experiment_id: str
    best_scout_score: float
    best_config: dict
    stagnation_count: int
    crash_count: int
    elapsed_seconds: float
    created_at: str = field(
        default_factory=lambda: datetime.now(tz=timezone.utc).isoformat(),
    )


@dataclass
class CampaignResult:
    """Final outcome of a completed campaign."""

    campaign_id: str
    total_experiments: int
    best_experiment_id: str | None
    best_scout_score: float
    best_config: dict | None
    end_reason: str
    status_counts: dict[str, int] = field(default_factory=dict)
    elapsed_seconds: float = 0.0
    checkpoints: list[CampaignCheckpoint] = field(default_factory=list)
    improvements: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.end_reason not in _END_REASONS:
            raise ValueError(f"Invalid end_reason '{self.end_reason}'")


def save_checkpoint(checkpoint: CampaignCheckpoint, campaign_dir: Path) -> Path:
    """Write checkpoint JSON to campaign_dir/checkpoints/."""
    cp_dir = Path(campaign_dir) / "checkpoints"
    cp_dir.mkdir(parents=True, exist_ok=True)
    out_path = cp_dir / f"cp_{checkpoint.experiment_count:05d}.json"
    data = {
        "campaign_id": checkpoint.campaign_id,
        "experiment_count": checkpoint.experiment_count,
        "best_experiment_id": checkpoint.best_experiment_id,
        "best_scout_score": checkpoint.best_scout_score,
        "best_config": checkpoint.best_config,
        "stagnation_count": checkpoint.stagnation_count,
        "crash_count": checkpoint.crash_count,
        "elapsed_seconds": checkpoint.elapsed_seconds,
        "created_at": checkpoint.created_at,
    }
    encoded = json.dumps(data, indent=2)
    fd, tmp_path = tempfile.mkstemp(dir=str(cp_dir), suffix=".tmp")
    closed = False
    try:
        os.write(fd, encoded.encode())
        os.close(fd)
        closed = True
        os.replace(tmp_path, str(out_path))
    except BaseException:
        if not closed:
            os.close(fd)
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise
    return out_path


def load_latest_checkpoint(campaign_dir: Path) -> CampaignCheckpoint | None:
    """Load the most recent checkpoint from campaign_dir/checkpoints/."""
    cp_dir = Path(campaign_dir) / "checkpoints"
    if not cp_dir.exists():
        return None
    files = sorted(cp_dir.glob("cp_*.json"))
    if not files:
        return None
    return CampaignCheckpoint(**json.loads(files[-1].read_text()))


def run_campaign(
    base_config: StrategyConfig,
    h1_candles: list,
    h4_candles: list,
    campaign_config: CampaignConfig | None = None,
    dataset_hash: str = "",
    doctrine_hash: str = "",
    campaign_dir: Path | None = None,
    db: ExperimentDB | None = None,
    stop_event: threading.Event | None = None,
) -> CampaignResult:
    """Run a bounded AutoResearch campaign loop."""
    cfg = campaign_config or CampaignConfig()
    stop = stop_event or threading.Event()
    if campaign_dir is not None:
        Path(campaign_dir).mkdir(parents=True, exist_ok=True)

    best_score: float = float("-inf")
    best_config_dict: dict = base_config.to_environment_kwargs()
    best_experiment_id: str = ""
    stagnation = crashes = experiment_index = 0
    status_counts: dict[str, int] = {}
    checkpoints: list[CampaignCheckpoint] = []
    improvements: list[dict] = []
    recent_good: list[dict] = []
    t0 = time.monotonic()

    wf_cfg = cfg.wf_config or WalkForwardConfig(
        train_bars=2190,
        test_bars=730,
        step_bars=365,
        embargo_bars=5,
        holdout_bars=1095,
    )

    log.info(
        "Starting %s: max=%d, stagnation=%d, wall=%.0fs",
        cfg.campaign_id,
        cfg.max_experiments,
        cfg.stagnation_limit,
        cfg.max_wall_clock_seconds,
    )

    if not h1_candles or not h4_candles:
        log.warning("Empty candle data — aborting")
        return CampaignResult(
            campaign_id=cfg.campaign_id,
            total_experiments=0,
            best_experiment_id=None,
            best_scout_score=0.0,
            best_config=None,
            end_reason="budget_exhausted",
        )

    end_reason = "budget_exhausted"

    while experiment_index < cfg.max_experiments:
        elapsed = time.monotonic() - t0
        if elapsed >= cfg.max_wall_clock_seconds:
            end_reason = "wall_clock"
            break
        if stagnation >= cfg.stagnation_limit:
            end_reason = "stagnation"
            break
        if crashes >= cfg.max_crashes:
            end_reason = "max_crashes"
            break
        if stop.is_set():
            end_reason = "manual_stop"
            break

        strategy = select_strategy(
            experiment_index,
            budget=cfg.strategy_budget,
            total_experiments=cfg.max_experiments,
        )
        candidate_dict = _generate_candidate(strategy, best_config_dict, recent_good)

        try:
            candidate_config = base_config.model_copy(update=candidate_dict)
        except Exception as exc:
            log.warning("#%d config build failed: %s", experiment_index, exc)
            crashes += 1
            experiment_index += 1
            continue

        result = execute_backtest(
            config=candidate_config,
            h1_candles=h1_candles,
            h4_candles=h4_candles,
            campaign_id=cfg.campaign_id,
            dataset_hash=dataset_hash,
            doctrine_hash=doctrine_hash,
            campaign_dir=campaign_dir,
            db=db,
        )
        status_counts[result.status] = status_counts.get(result.status, 0) + 1

        if result.status == "crashed":
            crashes += 1
            stagnation += 1
            _log_progress(experiment_index, strategy, result, best_score, stagnation)
            experiment_index += 1
            continue

        score = result.scout_score if result.scout_score is not None else 0.0

        if result.status == "ranked" and score > best_score:
            wf_passed = True
            if cfg.walkforward_on_new_best and len(h1_candles) > 5000:
                wf_result = run_walk_forward(
                    candidate_config,
                    h1_candles,
                    h4_candles,
                    wf_cfg,
                )
                wf_passed = wf_result.overall_passed
                if not wf_passed:
                    log.info("#%d WF failed — score %.4f rejected", experiment_index, score)

            if wf_passed:
                best_score = score
                best_config_dict = candidate_dict
                best_experiment_id = result.experiment_id
                stagnation = 0
                improvements.append(
                    {
                        "experiment_id": result.experiment_id,
                        "scout_score": score,
                        "experiment_index": experiment_index,
                    }
                )
                recent_good.append(candidate_dict)
                if len(recent_good) > 10:
                    recent_good.pop(0)
                log.info("#%d NEW BEST: score=%.4f id=%s", experiment_index, score, result.experiment_id)
            else:
                stagnation += 1
        else:
            stagnation += 1
            _log_progress(experiment_index, strategy, result, best_score, stagnation)

        if (
            campaign_dir is not None
            and cfg.checkpoint_interval > 0
            and (experiment_index + 1) % cfg.checkpoint_interval == 0
        ):
            cp = CampaignCheckpoint(
                campaign_id=cfg.campaign_id,
                experiment_count=experiment_index + 1,
                best_experiment_id=best_experiment_id,
                best_scout_score=best_score if best_score > float("-inf") else 0.0,
                best_config=best_config_dict,
                stagnation_count=stagnation,
                crash_count=crashes,
                elapsed_seconds=round(time.monotonic() - t0, 2),
            )
            save_checkpoint(cp, campaign_dir)
            checkpoints.append(cp)

        experiment_index += 1

    total_elapsed = round(time.monotonic() - t0, 2)
    final_best = best_score if best_score > float("-inf") else 0.0
    log.info(
        "Finished: %d experiments, best=%.4f, reason=%s, elapsed=%.1fs",
        experiment_index,
        final_best,
        end_reason,
        total_elapsed,
    )

    return CampaignResult(
        campaign_id=cfg.campaign_id,
        total_experiments=experiment_index,
        best_experiment_id=best_experiment_id or None,
        best_scout_score=final_best,
        best_config=best_config_dict if best_experiment_id else None,
        end_reason=end_reason,
        status_counts=status_counts,
        elapsed_seconds=total_elapsed,
        checkpoints=checkpoints,
        improvements=improvements,
    )


def _generate_candidate(
    strategy: ExperimentStrategy,
    best_config: dict,
    recent_good: list[dict],
) -> dict:
    """Generate a candidate parameter dict based on the selected strategy."""
    if strategy == ExperimentStrategy.LOCAL:
        return generate_local_candidate(best_config)
    if strategy == ExperimentStrategy.EXPLORE:
        return generate_explore_candidate()
    if strategy == ExperimentStrategy.BAYESIAN:
        return generate_local_candidate(best_config, scale=0.02)
    if strategy == ExperimentStrategy.ABLATION:
        return generate_local_candidate(best_config, scale=0.03)
    if strategy == ExperimentStrategy.COMBINATION:
        parents = [best_config] + recent_good
        if len(parents) < 2:
            parents = parents + [generate_explore_candidate()]
        return generate_combination_candidate(parents)
    return generate_local_candidate(best_config)


def _log_progress(
    idx: int,
    strategy: ExperimentStrategy,
    result: BacktestRunResult,
    best_score: float,
    stagnation: int,
) -> None:
    """Log concise progress line every 10 experiments."""
    if idx % 10 == 0:
        score_str = f"{result.scout_score:.4f}" if result.scout_score is not None else "N/A"
        best_str = f"{best_score:.4f}" if best_score > float("-inf") else "N/A"
        log.info(
            "#%d %11s status=%-8s score=%s best=%s stagnation=%d",
            idx,
            strategy.value,
            result.status,
            score_str,
            best_str,
            stagnation,
        )
