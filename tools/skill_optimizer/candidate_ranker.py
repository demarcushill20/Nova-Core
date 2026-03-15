"""Candidate ranking for the skill optimization pipeline.

Ranks candidates by evaluating them on the train/dev split and computing
weighted scores. Selects the best candidate for holdout test evaluation.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from tools.skill_optimizer.candidate_generator import Candidate
from tools.skill_optimizer.evaluate_skill import EvalResult, evaluate_skill


@dataclass
class RankedCandidate:
    """A candidate with its evaluation results."""

    candidate: Candidate
    train_result: EvalResult | None = None
    dev_result: EvalResult | None = None
    train_score: float = 0.0
    dev_score: float = 0.0
    combined_score: float = 0.0
    rank: int = 0


def evaluate_candidate(
    candidate: Candidate,
    skill_path: Path,
    split: str = "train",
    model: str | None = None,
    **eval_kwargs,
) -> EvalResult:
    """Evaluate a candidate description on a dataset split."""
    return evaluate_skill(
        skill_name=candidate.skill_name,
        skill_path=skill_path,
        description=candidate.proposed_description,
        split=split,
        model=model,
        **eval_kwargs,
    )


def rank_candidates(
    candidates: list[Candidate],
    skill_path: Path,
    baseline_score: float = 0.0,
    model: str | None = None,
    use_dev: bool = False,
    **eval_kwargs,
) -> list[RankedCandidate]:
    """Evaluate and rank candidates by weighted score.

    Evaluates each valid candidate on the train split, optionally on dev,
    and returns them sorted by combined score (descending).

    Args:
        candidates: Valid candidates to evaluate
        skill_path: Path to the skill directory
        baseline_score: Baseline weighted score for comparison
        model: Model for evaluation
        use_dev: Also evaluate on dev split (if available)
        **eval_kwargs: Additional kwargs for evaluate_skill
    """
    ranked: list[RankedCandidate] = []

    for candidate in candidates:
        if not candidate.is_valid:
            continue

        rc = RankedCandidate(candidate=candidate)

        # Train evaluation
        train_result = evaluate_candidate(candidate, skill_path, split="train", model=model, **eval_kwargs)
        rc.train_result = train_result
        rc.train_score = train_result.metrics.weighted_score

        # Dev evaluation (optional)
        if use_dev:
            dev_result = evaluate_candidate(candidate, skill_path, split="dev", model=model, **eval_kwargs)
            rc.dev_result = dev_result
            rc.dev_score = dev_result.metrics.weighted_score

        # Combined score: train only if no dev, else average
        if use_dev and rc.dev_score > 0:
            rc.combined_score = (rc.train_score + rc.dev_score) / 2
        else:
            rc.combined_score = rc.train_score

        ranked.append(rc)

    # Sort by combined score descending, then by candidate_id for stability
    ranked.sort(key=lambda r: (-r.combined_score, r.candidate.candidate_id))

    # Assign ranks
    for i, rc in enumerate(ranked):
        rc.rank = i + 1

    return ranked


def select_best(
    ranked: list[RankedCandidate],
    baseline_score: float = 0.0,
    min_improvement: float = 0.02,
) -> RankedCandidate | None:
    """Select the best candidate that improves over baseline.

    Returns None if no candidate meets the minimum improvement threshold.
    """
    if not ranked:
        return None

    best = ranked[0]
    if best.combined_score >= baseline_score + min_improvement:
        return best

    return None
