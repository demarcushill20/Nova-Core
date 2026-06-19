"""Task-1061 — fixed-size sliding walk-forward validation for the DreamerV3 gold bot.

The transcript is explicit that the research method is **NOT** one simple
train/validation/test split. It is a fixed-size *sliding* walk-forward:

    * train, validation and test windows all move forward together, and
    * old data drops off the back of the training window (fixed size, sliding —
      not anchored/expanding).

Each fold is three contiguous, non-overlapping windows::

    |<-- train_size -->|<- val_size ->|<- test_size ->|
    train              validation     test
    ^ fold start

The next fold's start advances by ``step`` (default ``test_size``, so the test
windows tile the timeline with no overlap and no gap). Because the train window
is a fixed size that slides, the oldest training bars are dropped as the fold
advances — exactly the "old data drops off the back" requirement.

Orchestration (sections 13-15, glued to :mod:`selection`)
---------------------------------------------------------
For every fold:

    1. train a model on the fold's TRAIN window (many checkpoints saved),
    2. score each checkpoint on its train-tail and validation windows
       (drawdown-penalized — :func:`selection.window_score`),
    3. pick the champion checkpoint by best worst-case score
       (:func:`selection.select_champion_checkpoint`),
    4. record that fold's validation profit.

After all folds: the cross-fold consistency gate
(:func:`selection.consistency_gate`) decides whether the edge is real. If it
passes, the **last** fold's model is promoted as champion and run **once** on
its held-out test window. Test data is never used for checkpoint selection or
hyperparameter tuning — it is touched exactly once, here, at the very end.

The GPU-dependent parts (train, evaluate) are injected as callables so this
orchestrator is pure and unit-testable now, ahead of the training loop — the
same decoupling pattern :mod:`execution` uses for the policy.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .config import DEFAULT, DreamerConfig
from .selection import (
    Checkpoint,
    ConsistencyResult,
    consistency_gate,
    select_champion_checkpoint,
)

# --------------------------------------------------------------------------- #
# Fold geometry
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Fold:
    """One walk-forward fold: three contiguous half-open ``[start, end)`` windows.

    Indices are positions into the decision-bar series (H1 closes). The windows
    are contiguous and non-overlapping: ``train_end == val_start`` and
    ``val_end == test_start``.
    """

    fold_index: int
    train_start: int
    train_end: int  # exclusive; == val_start
    val_end: int  # exclusive; == test_start
    test_end: int  # exclusive

    @property
    def val_start(self) -> int:
        return self.train_end

    @property
    def test_start(self) -> int:
        return self.val_end

    @property
    def train_len(self) -> int:
        return self.train_end - self.train_start

    @property
    def val_len(self) -> int:
        return self.val_end - self.val_start

    @property
    def test_len(self) -> int:
        return self.test_end - self.test_start

    def train_range(self) -> tuple[int, int]:
        return (self.train_start, self.train_end)

    def val_range(self) -> tuple[int, int]:
        return (self.val_start, self.val_end)

    def test_range(self) -> tuple[int, int]:
        return (self.test_start, self.test_end)


def generate_folds(
    n_samples: int,
    train_size: int,
    val_size: int,
    test_size: int,
    step: int | None = None,
) -> list[Fold]:
    """Fixed-size sliding walk-forward folds over ``n_samples`` decision bars.

    Every fold has a TRAIN window of exactly ``train_size`` bars (fixed size —
    as folds advance, the oldest bars fall off the back), followed by ``val_size``
    validation bars and ``test_size`` test bars. Consecutive folds advance their
    start by ``step`` (default ``test_size``: the test windows tile the timeline
    with neither overlap nor gap).

    Folds are emitted while a complete (train+val+test) block still fits; a final
    partial block is dropped rather than emitted short, so every fold is the same
    shape and comparable across the consistency gate.
    """
    for name, value in (
        ("train_size", train_size),
        ("val_size", val_size),
        ("test_size", test_size),
    ):
        if value <= 0:
            raise ValueError(f"{name} must be > 0 (got {value})")
    if step is None:
        step = test_size
    if step <= 0:
        raise ValueError(f"step must be > 0 (got {step})")

    block = train_size + val_size + test_size
    if n_samples < block:
        raise ValueError(
            f"n_samples ({n_samples}) too small for one fold of size {block} "
            f"(train {train_size} + val {val_size} + test {test_size})"
        )

    folds: list[Fold] = []
    start = 0
    while start + block <= n_samples:
        train_end = start + train_size
        val_end = train_end + val_size
        test_end = val_end + test_size
        folds.append(
            Fold(
                fold_index=len(folds),
                train_start=start,
                train_end=train_end,
                val_end=val_end,
                test_end=test_end,
            )
        )
        start += step
    return folds


# --------------------------------------------------------------------------- #
# Per-fold outcome (the contract a training run must return)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class FoldOutcome:
    """What training + checkpoint-scoring produced for one fold.

    ``checkpoints`` are the saved checkpoints already scored on their train-tail
    and validation windows. ``validation_profit`` is the fold's net validation
    result (sign is all the consistency gate cares about). ``model`` is the
    opaque trained model object (only the last fold's is ever promoted).
    """

    fold: Fold
    checkpoints: list[Checkpoint]
    validation_profit: float
    model: Any = None

    @property
    def selected_checkpoint(self) -> Checkpoint:
        """The champion checkpoint for this fold (best worst-case score)."""
        return select_champion_checkpoint(self.checkpoints)


# --------------------------------------------------------------------------- #
# Walk-forward result
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class WalkForwardResult:
    """Outcome of a full walk-forward run."""

    outcomes: list[FoldOutcome]
    consistency: ConsistencyResult
    champion_model: Any
    champion_checkpoint: Checkpoint | None
    test_result: Any  # whatever ``run_test`` returns; None when the gate fails

    @property
    def has_champion(self) -> bool:
        return self.champion_model is not None

    @property
    def fold_profits(self) -> list[float]:
        return [o.validation_profit for o in self.outcomes]


RunFoldFn = Callable[[Fold], FoldOutcome]
"""Train + score-checkpoints for one fold. Wrap the GPU training loop here."""

RunTestFn = Callable[[Fold, FoldOutcome], Any]
"""Run the promoted champion ONCE on its held-out test window."""


def run_walk_forward(
    folds: list[Fold],
    *,
    run_fold: RunFoldFn,
    run_test: RunTestFn,
    config: DreamerConfig = DEFAULT,
) -> WalkForwardResult:
    """Drive the fixed-size sliding walk-forward and apply the consistency gate.

    ``run_fold`` trains and scores checkpoints for each fold (test data must NOT
    be touched inside it). After every fold finishes, the consistency gate runs
    on the per-fold validation profits. Only if the gate passes is the last
    fold's model promoted and ``run_test`` invoked — exactly once, on held-out
    test data — to produce the final read.
    """
    if not folds:
        raise ValueError("no folds to run")

    outcomes = [run_fold(fold) for fold in folds]
    fold_profits = [o.validation_profit for o in outcomes]
    consistency = consistency_gate(fold_profits, config.min_profitable_fold_ratio)

    if consistency.passed:
        last = outcomes[-1]
        champion_model = last.model
        champion_checkpoint = last.selected_checkpoint
        test_result = run_test(last.fold, last)
    else:
        # Gate failed: return NO champion. More feature/reward/hyperparameter
        # research is required before any test-set read is justified.
        champion_model = None
        champion_checkpoint = None
        test_result = None

    return WalkForwardResult(
        outcomes=outcomes,
        consistency=consistency,
        champion_model=champion_model,
        champion_checkpoint=champion_checkpoint,
        test_result=test_result,
    )
