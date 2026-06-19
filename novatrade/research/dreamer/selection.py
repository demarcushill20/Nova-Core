"""Task-1059 — drawdown-penalized scoring, worst-case checkpoint selection, and
the cross-fold consistency gate for the DreamerV3 gold bot.

This is the deterministic *model-selection* layer the spec describes (sections
13-15). None of it requires GPU compute: given the reward/drawdown numbers a
training run already produces, these are pure functions, so they are
implemented and unit-tested now — ahead of the 5M-step training loop itself.

Section 13 — drawdown-penalized score
-------------------------------------
A checkpoint is graded on a *single window* (training tail OR validation) by::

    score = cumulative_reward - drawdown_penalty
    drawdown_penalty = drawdown_penalty_weight * max_drawdown

So a model that earns reward while courting a dangerous drawdown is punished.

Section 13 — worst-case checkpoint selection
--------------------------------------------
We do **not** pick the checkpoint with the best *average* score. We pick the one
whose *weaker* score is strongest — best worst-case across the two windows::

    checkpoint_score = min(train_tail_score, validation_score)
    best_checkpoint  = argmax_over_checkpoints(checkpoint_score)

A model must be good on BOTH the training tail and validation. Crushing one
while failing the other is rejected. This is the anti-overfitting rule.

Section 14 — overfitting region (handled in config, enforced here only softly)
-----------------------------------------------------------------------------
Training runs UP TO ``max_training_steps`` (~5M) but the selector above — driven
by validation — decides the champion, rather than a hard-coded "2M is best".
``expected_good_region`` is a documented prior; ``flag_outside_good_region``
warns (never blocks) if the winner sits far from it.

Section 15 — cross-fold consistency gate
-----------------------------------------
After every walk-forward fold finishes, require that *enough* folds were
independently profitable::

    profitable_ratio = (#folds with profit > 0) / (#folds)
    gate passes iff profitable_ratio >= min_profitable_fold_ratio

If only one fold made money, the edge is luck — reject. If the gate passes,
promote the model from the **last** fold as champion (it was trained on the most
recent data, i.e. closest to live).
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import DEFAULT, DreamerConfig

# --------------------------------------------------------------------------- #
# Section 13 — scoring
# --------------------------------------------------------------------------- #


def drawdown_penalty(max_drawdown: float, weight: float = DEFAULT.drawdown_penalty_weight) -> float:
    """Penalty term for a window's max drawdown.

    ``max_drawdown`` is the magnitude of the worst peak-to-trough decline in the
    equity/reward curve for the window, expressed as a non-negative number.
    """
    if max_drawdown < 0:
        raise ValueError(f"max_drawdown must be >= 0 (got {max_drawdown})")
    if weight < 0:
        raise ValueError(f"weight must be >= 0 (got {weight})")
    return weight * max_drawdown


def window_score(
    cumulative_reward: float,
    max_drawdown: float,
    weight: float = DEFAULT.drawdown_penalty_weight,
) -> float:
    """Drawdown-penalized score for a single window (training tail or validation).

    ``score = cumulative_reward - drawdown_penalty_weight * max_drawdown``.
    """
    return cumulative_reward - drawdown_penalty(max_drawdown, weight)


# --------------------------------------------------------------------------- #
# Section 13 — worst-case checkpoint selection
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Checkpoint:
    """One saved checkpoint with its two already-computed window scores.

    ``train_tail_score`` and ``validation_score`` are each a
    :func:`window_score` (i.e. already drawdown-penalized). Keeping them
    pre-scored lets the selector stay agnostic to how the windows were graded.
    """

    step: int
    train_tail_score: float
    validation_score: float

    @property
    def checkpoint_score(self) -> float:
        """Worst-case score: the weaker of the two windows."""
        return min(self.train_tail_score, self.validation_score)


def select_champion_checkpoint(checkpoints: list[Checkpoint]) -> Checkpoint:
    """Pick the checkpoint with the strongest worst-case (min) score.

    Tie-break: prefer the *earlier* step. An earlier checkpoint has seen fewer
    gradient updates and is the less-overfit of two equally-strong picks — this
    biases selection toward the spec's "useful learning region" without
    hard-coding a step count.
    """
    if not checkpoints:
        raise ValueError("no checkpoints to select from")
    # max by worst-case score; on a tie, min step wins (negate via sort key).
    return max(checkpoints, key=lambda c: (c.checkpoint_score, -c.step))


def flag_outside_good_region(
    checkpoint: Checkpoint,
    config: DreamerConfig = DEFAULT,
) -> bool:
    """True if the chosen checkpoint sits OUTSIDE the expected good region.

    Advisory only — the selector never blocks on this. It exists so a results
    banner can warn "champion at 4.2M steps is well past the ~1-2M good region,
    inspect for overfit" without overriding the validation-driven choice.
    """
    lo, hi = config.expected_good_region
    return not (lo <= checkpoint.step <= hi)


# --------------------------------------------------------------------------- #
# Section 15 — cross-fold consistency gate
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ConsistencyResult:
    """Outcome of the cross-fold consistency gate."""

    passed: bool
    profitable_folds: int
    total_folds: int
    profitable_ratio: float
    required_ratio: float


def consistency_gate(
    fold_profits: list[float],
    min_profitable_fold_ratio: float = DEFAULT.min_profitable_fold_ratio,
) -> ConsistencyResult:
    """Require enough independently-profitable walk-forward folds.

    ``fold_profits`` is one net result per fold (reward, R, or currency — only
    its sign matters here). A fold counts as profitable iff its result is
    strictly > 0. The gate passes iff the profitable ratio meets the threshold.
    """
    total = len(fold_profits)
    if total == 0:
        raise ValueError("consistency gate needs at least one fold")
    if not 0.0 < min_profitable_fold_ratio <= 1.0:
        raise ValueError(f"min_profitable_fold_ratio must be in (0, 1] (got {min_profitable_fold_ratio})")
    profitable = sum(1 for p in fold_profits if p > 0)
    ratio = profitable / total
    return ConsistencyResult(
        passed=ratio >= min_profitable_fold_ratio,
        profitable_folds=profitable,
        total_folds=total,
        profitable_ratio=ratio,
        required_ratio=min_profitable_fold_ratio,
    )


def promote_champion_fold(
    fold_models: list,
    fold_profits: list[float],
    min_profitable_fold_ratio: float = DEFAULT.min_profitable_fold_ratio,
):
    """Promote the LAST fold's model as champion iff the consistency gate passes.

    Returns ``(champion_model, ConsistencyResult)``. ``champion_model`` is the
    model from the final fold (trained on the most recent data, closest to
    live) when the gate passes, else ``None``.
    """
    if len(fold_models) != len(fold_profits):
        raise ValueError(f"fold_models ({len(fold_models)}) and fold_profits ({len(fold_profits)}) length mismatch")
    result = consistency_gate(fold_profits, min_profitable_fold_ratio)
    champion = fold_models[-1] if result.passed else None
    return champion, result
