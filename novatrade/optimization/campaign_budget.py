"""Campaign budget enforcement — bounded campaigns instead of infinite loops.

Trading research must not run indefinitely. This module enforces explicit
resource budgets per campaign, providing:
  - Experiment count limits
  - Wall-clock time limits
  - Crash/failure limits
  - Stagnation detection (stop after N experiments with no improvement)
  - Automatic checkpoint intervals

Design rationale (from operator review):
  Running forever makes sense for a toy research repo. For trading research,
  bounded campaigns with explicit budgets give you budget control, less
  overfitting drift, easier postmortems, and cleaner champion/challenger
  comparisons.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class StopReason(Enum):
    """Why a campaign was stopped."""

    RUNNING = "running"
    EXPERIMENT_LIMIT = "experiment_limit"
    WALL_CLOCK_LIMIT = "wall_clock_limit"
    CRASH_LIMIT = "crash_limit"
    STAGNATION = "stagnation"
    MANUAL_STOP = "manual_stop"


@dataclass
class CampaignBudget:
    """Resource budget for a single campaign.

    Defaults are conservative and production-oriented. The operator can
    override any limit when launching a campaign.
    """

    max_experiments: int = 200
    """Stop after this many experiments (successful or not)."""

    max_wall_clock_seconds: float = 6 * 3600  # 6 hours
    """Stop after this much wall-clock time (seconds)."""

    max_crashes: int = 20
    """Stop after this many experiment failures/crashes."""

    max_stagnant_experiments: int = 10
    """Stop after N experiments with no improvement to best score."""

    checkpoint_interval: int = 25
    """Save a campaign checkpoint every N experiments."""

    def to_dict(self) -> dict:
        return {
            "max_experiments": self.max_experiments,
            "max_wall_clock_seconds": self.max_wall_clock_seconds,
            "max_crashes": self.max_crashes,
            "max_stagnant_experiments": self.max_stagnant_experiments,
            "checkpoint_interval": self.checkpoint_interval,
        }


@dataclass
class CampaignState:
    """Mutable runtime state tracking budget consumption.

    Created at campaign start, updated after each experiment. The
    should_stop() method checks all budget limits and returns a
    StopReason if any are exceeded.
    """

    campaign_id: str
    budget: CampaignBudget
    started_at: float = field(default_factory=time.monotonic)
    started_at_utc: str = field(default_factory=lambda: datetime.now(tz=timezone.utc).isoformat())

    # Counters
    total_experiments: int = 0
    successful_experiments: int = 0
    crashed_experiments: int = 0
    experiments_since_improvement: int = 0
    best_score: float = -1.0
    best_experiment_id: str = ""

    # Checkpoint tracking
    experiments_since_checkpoint: int = 0
    checkpoints_created: int = 0

    def record_experiment(
        self,
        experiment_id: str,
        score: float | None,
        crashed: bool = False,
    ) -> None:
        """Record the outcome of one experiment.

        Args:
            experiment_id: ID of the completed experiment.
            score: Scout score (None if crashed/invalid).
            crashed: True if the experiment failed with an error.
        """
        self.total_experiments += 1
        self.experiments_since_checkpoint += 1

        if crashed:
            self.crashed_experiments += 1
            self.experiments_since_improvement += 1
            return

        self.successful_experiments += 1

        if score is not None and score > self.best_score:
            self.best_score = score
            self.best_experiment_id = experiment_id
            self.experiments_since_improvement = 0
        else:
            self.experiments_since_improvement += 1

    def should_stop(self) -> StopReason:
        """Check all budget limits.

        Returns StopReason.RUNNING if the campaign should continue,
        otherwise the specific reason to stop.
        """
        if self.total_experiments >= self.budget.max_experiments:
            return StopReason.EXPERIMENT_LIMIT

        elapsed = time.monotonic() - self.started_at
        if elapsed >= self.budget.max_wall_clock_seconds:
            return StopReason.WALL_CLOCK_LIMIT

        if self.crashed_experiments >= self.budget.max_crashes:
            return StopReason.CRASH_LIMIT

        if self.experiments_since_improvement >= self.budget.max_stagnant_experiments:
            return StopReason.STAGNATION

        return StopReason.RUNNING

    def needs_checkpoint(self) -> bool:
        """True if a checkpoint should be saved now."""
        return self.experiments_since_checkpoint >= self.budget.checkpoint_interval

    def mark_checkpoint(self) -> None:
        """Record that a checkpoint was saved."""
        self.experiments_since_checkpoint = 0
        self.checkpoints_created += 1

    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self.started_at

    @property
    def remaining_experiments(self) -> int:
        return max(0, self.budget.max_experiments - self.total_experiments)

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.budget.max_wall_clock_seconds - self.elapsed_seconds)

    def summary(self) -> dict:
        """Snapshot of campaign progress for logging/reporting."""
        reason = self.should_stop()
        return {
            "campaign_id": self.campaign_id,
            "status": reason.value,
            "total_experiments": self.total_experiments,
            "successful": self.successful_experiments,
            "crashed": self.crashed_experiments,
            "stagnant_streak": self.experiments_since_improvement,
            "best_score": round(self.best_score, 4) if self.best_score >= 0 else None,
            "best_experiment_id": self.best_experiment_id or None,
            "elapsed_seconds": round(self.elapsed_seconds, 1),
            "remaining_experiments": self.remaining_experiments,
            "checkpoints_created": self.checkpoints_created,
            "started_at_utc": self.started_at_utc,
        }


def create_campaign(
    campaign_id: str,
    budget: CampaignBudget | None = None,
) -> CampaignState:
    """Initialize a new campaign with budget enforcement.

    Args:
        campaign_id: Unique identifier for this campaign.
        budget: Resource budget (defaults to CampaignBudget() if None).

    Returns:
        Mutable CampaignState to track progress.
    """
    return CampaignState(
        campaign_id=campaign_id,
        budget=budget or CampaignBudget(),
    )
