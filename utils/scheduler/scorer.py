"""Task Scoring Engine for the Intelligent Dynamic Block Scheduler.

Implements a multi-factor objective function that converts WorkUnit fields
into a single composite score for ranking.  Pure and deterministic — no
randomness, no LLM calls.

Scoring pipeline:
  1. Weighted base score (urgency, strategic_value, unblock_value,
     risk_reduction)
  2. Deferment age bonus (starvation prevention)
  3. Commitment level adjustment
  4. Confidence modifier (penalise low confidence, reward high)
  5. Success probability factor
  6. Priority multiplier
  7. Time cost divisor (expected_duration + context_switch_cost + uncertainty)
"""

from __future__ import annotations

import logging
from collections import defaultdict, deque
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from utils.scheduler.work_unit import WorkUnit

log = logging.getLogger("nova.scheduler.scorer")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_DEFAULT_WEIGHTS: dict[str, float] = {
    "urgency": 0.30,
    "strategic_value": 0.25,
    "unblock_value": 0.25,
    "risk_reduction": 0.20,
}
# TODO: Phase 7+ will add goal_alignment from dependency graph

_DEFAULT_PRIORITY_MULT: dict[str, float] = {
    "high": 1.3,
    "medium": 1.0,
    "low": 0.7,
}

_DEFAULT_COMMITMENT_BONUS: dict[str, float] = {
    "hard": 0.15,
    "soft": 0.0,
    "opportunistic": -0.10,
}

_DEFAULT_AGING_RATE: dict[str, float] = {
    "hard": 1.0,
    "soft": 0.5,
    "opportunistic": 0.2,
}


class ScoringConfig(BaseModel):
    """Configuration for the task scoring engine."""

    model_config = ConfigDict(extra="forbid")

    weights: dict[str, float] = Field(default_factory=lambda: dict(_DEFAULT_WEIGHTS))
    priority_multiplier: dict[str, float] = Field(
        default_factory=lambda: dict(_DEFAULT_PRIORITY_MULT),
    )
    commitment_bonus: dict[str, float] = Field(
        default_factory=lambda: dict(_DEFAULT_COMMITMENT_BONUS),
    )
    aging_rate_per_day: dict[str, float] = Field(
        default_factory=lambda: dict(_DEFAULT_AGING_RATE),
    )
    aging_max_bonus: float = Field(default=30.0, ge=0.0)
    confidence_low_penalty: float = Field(default=0.3, ge=0.0, le=1.0)
    confidence_high_bonus: float = Field(default=1.1, ge=1.0)
    duration_weight: float = Field(default=0.1, ge=0.0)

    @model_validator(mode="after")
    def normalize_weights(self) -> ScoringConfig:
        """Warn and auto-normalize if weights don't sum to ~1.0."""
        total = sum(self.weights.values())
        if total > 0 and not (0.9 <= total <= 1.1):
            log.warning(
                "Scoring weights sum to %.3f (expected ~1.0) — auto-normalizing",
                total,
            )
            self.weights = {k: v / total for k, v in self.weights.items()}
        return self


def load_scoring_config(config_path: Path | None = None) -> ScoringConfig:
    """Load scoring config from YAML, falling back to defaults.

    File values override the defaults — missing keys keep their default
    values so a partial YAML file is fine.
    """
    if config_path is None:
        config_path = Path(__file__).resolve().parents[2] / "config" / "scheduler_weights.yaml"

    if not config_path.exists():
        log.debug("Config file %s not found — using defaults", config_path)
        return ScoringConfig()

    try:
        raw = yaml.safe_load(config_path.read_text()) or {}
    except (yaml.YAMLError, OSError, ValueError, KeyError) as exc:
        log.warning("Failed to parse %s: %s — using defaults", config_path, exc)
        return ScoringConfig()

    scorer = raw.get("scorer", {})
    if not isinstance(scorer, dict):
        return ScoringConfig()

    kwargs: dict = {}

    if "weights" in scorer and isinstance(scorer["weights"], dict):
        merged = dict(_DEFAULT_WEIGHTS)
        merged.update(scorer["weights"])
        kwargs["weights"] = merged

    if "priority_multiplier" in scorer and isinstance(scorer["priority_multiplier"], dict):
        merged = dict(_DEFAULT_PRIORITY_MULT)
        merged.update(scorer["priority_multiplier"])
        kwargs["priority_multiplier"] = merged

    if "commitment_bonus" in scorer and isinstance(scorer["commitment_bonus"], dict):
        merged = dict(_DEFAULT_COMMITMENT_BONUS)
        merged.update(scorer["commitment_bonus"])
        kwargs["commitment_bonus"] = merged

    aging = scorer.get("aging", {})
    if isinstance(aging, dict):
        if "rate_per_day" in aging and isinstance(aging["rate_per_day"], dict):
            merged = dict(_DEFAULT_AGING_RATE)
            merged.update(aging["rate_per_day"])
            kwargs["aging_rate_per_day"] = merged
        if "max_bonus" in aging:
            kwargs["aging_max_bonus"] = float(aging["max_bonus"])

    conf = scorer.get("confidence", {})
    if isinstance(conf, dict):
        if "low_confidence_penalty" in conf:
            kwargs["confidence_low_penalty"] = float(conf["low_confidence_penalty"])
        if "high_confidence_bonus" in conf:
            kwargs["confidence_high_bonus"] = float(conf["high_confidence_bonus"])

    if "duration_weight" in scorer:
        kwargs["duration_weight"] = float(scorer["duration_weight"])

    return ScoringConfig(**kwargs)


# ---------------------------------------------------------------------------
# Scored result
# ---------------------------------------------------------------------------


class ScoredTask(BaseModel):
    """A WorkUnit enriched with its computed scores."""

    model_config = ConfigDict(extra="forbid")

    work_unit: WorkUnit
    raw_score: float = Field(description="Weighted base score (0-100)")
    deferment_bonus: float = Field(description="Age-based starvation bonus")
    commitment_adjustment: float = Field(description="Commitment level delta")
    confidence_modifier: float = Field(description="Confidence multiplier")
    effective_time: float = Field(description="Time cost divisor (hours)")
    priority_multiplier: float = Field(description="Priority scaling factor")
    final_score: float = Field(description="Composite score (floored at 0)")
    normalized_score: float = Field(default=0.0, description="0-100 scale relative to batch")
    rank: int = Field(default=0, description="1-based rank within batch")


# ---------------------------------------------------------------------------
# Core scoring
# ---------------------------------------------------------------------------


def score_task(
    work_unit: WorkUnit,
    config: ScoringConfig | None = None,
) -> ScoredTask:
    """Score a single WorkUnit using the multi-factor objective function.

    Returns a ScoredTask with all intermediate values exposed for
    debuggability.

    Formula:
      value_score = raw_score * conf_mod * success_factor * priority_mult
      final_score = (value_score + deferment_bonus + commitment_adj) / effective_time

    Where effective_time = (expected_duration + context_switch_cost) / 60
                         + uncertainty_penalty, floored at 0.1 hours.
    """
    cfg = config or ScoringConfig()
    w = cfg.weights

    # 1. Base weighted score (0-1 range) — 4 factors, no goal_alignment
    # TODO: Phase 7+ will add goal_alignment from dependency graph
    base = (
        work_unit.urgency * w.get("urgency", 0.30)
        + work_unit.strategic_value * w.get("strategic_value", 0.25)
        + work_unit.unblock_value * w.get("unblock_value", 0.25)
        + work_unit.risk_reduction * w.get("risk_reduction", 0.20)
    )

    # 2. Deferment age bonus (starvation prevention)
    aging_rate = cfg.aging_rate_per_day.get(work_unit.commitment_level.value, 0.5)
    deferment_bonus = min(
        work_unit.deferment_age_days * aging_rate,
        cfg.aging_max_bonus,
    )

    # 3. Commitment adjustment
    commitment_adj = cfg.commitment_bonus.get(work_unit.commitment_level.value, 0.0)

    # 4. Confidence modifier
    if work_unit.confidence < 0.3:
        conf_mod = cfg.confidence_low_penalty
    elif work_unit.confidence > 0.8:
        conf_mod = cfg.confidence_high_bonus
    else:
        conf_mod = 1.0

    # 5. Success probability factor
    success_factor = work_unit.success_probability

    # 6. Priority multiplier
    priority_mult = cfg.priority_multiplier.get(work_unit.priority, 1.0)

    # 7. Time cost factor — score represents "value per hour of time invested"
    time_cost = (work_unit.estimated_expected_min + work_unit.context_switch_cost_min) / 60.0
    # Uncertainty adds to effective time cost
    uncertainty_spread = (work_unit.estimated_pessimistic_min - work_unit.estimated_optimistic_min) / 60.0
    uncertainty_penalty = uncertainty_spread * cfg.duration_weight
    effective_time = max(time_cost + uncertainty_penalty, 0.1)  # Floor to prevent division by zero

    # 8. Composite — confidence modifier applies only to base score (H4 fix)
    raw_score = base * 100  # Scale base to 0-100
    value_score = raw_score * conf_mod * success_factor * priority_mult

    # Deferment and commitment are added after conf_mod so starvation
    # prevention is not dampened by low confidence
    final_score = (value_score + deferment_bonus + commitment_adj * 100) / effective_time
    final_score = max(0.0, final_score)

    return ScoredTask(
        work_unit=work_unit,
        raw_score=round(raw_score, 4),
        deferment_bonus=round(deferment_bonus, 4),
        commitment_adjustment=round(commitment_adj, 4),
        confidence_modifier=round(conf_mod, 4),
        effective_time=round(effective_time, 4),
        priority_multiplier=round(priority_mult, 4),
        final_score=round(final_score, 4),
    )


# ---------------------------------------------------------------------------
# Dependency-aware unlock value propagation
# ---------------------------------------------------------------------------


def compute_unlock_values(work_units: list[WorkUnit]) -> dict[str, float]:
    """Compute how much downstream value each task unblocks.

    Builds the dependency graph (task -> dependents), then BFS from each
    task to sum the base scores of all transitively unblocked tasks.

    Returns a dict mapping task id -> unlock value normalised to 0-1.
    """
    if not work_units:
        return {}

    # Map id -> WorkUnit for quick lookup
    id_map: dict[str, WorkUnit] = {wu.id: wu for wu in work_units if wu.id}

    # Build adjacency: for each task, which other tasks depend on it?
    # (i.e. task X appears in the dependency_ids of task Y => X unblocks Y)
    dependents: dict[str, list[str]] = defaultdict(list)
    for wu in work_units:
        for dep_id in wu.dependency_ids:
            if dep_id in id_map:
                dependents[dep_id].append(wu.id)

    # BFS from each task to find all transitively unblocked tasks
    raw_unlock: dict[str, float] = {}

    for wu in work_units:
        if not wu.id:
            continue

        visited: set[str] = set()
        queue: deque[str] = deque()

        # Seed with direct dependents
        for dep in dependents.get(wu.id, []):
            if dep not in visited and dep != wu.id:
                visited.add(dep)
                queue.append(dep)

        # BFS
        total = 0.0
        while queue:
            current_id = queue.popleft()
            current_wu = id_map.get(current_id)
            if current_wu is not None:
                # H2 fix: include unblock_value and weight by success_probability
                total += (
                    (
                        current_wu.urgency
                        + current_wu.strategic_value
                        + current_wu.risk_reduction
                        + current_wu.unblock_value
                    )
                    / 4.0
                ) * current_wu.success_probability

            for child in dependents.get(current_id, []):
                if child not in visited and child != wu.id:
                    visited.add(child)
                    queue.append(child)

        raw_unlock[wu.id] = total

    # Normalise to 0-1
    max_val = max(raw_unlock.values()) if raw_unlock else 0.0
    if max_val <= 0.0:
        return {uid: 0.0 for uid in raw_unlock}

    return {uid: round(val / max_val, 4) for uid, val in raw_unlock.items()}


# ---------------------------------------------------------------------------
# Batch ranking
# ---------------------------------------------------------------------------


def rank_tasks(
    work_units: list[WorkUnit],
    config: ScoringConfig | None = None,
) -> list[ScoredTask]:
    """Score, rank, and normalise a batch of WorkUnits.

    1. Compute unlock values and create updated copies (no mutation of originals).
    2. Score each task individually.
    3. Sort descending by final_score.
    4. Assign 1-based ranks.
    5. Normalise scores: highest = 100, others proportional.
    """
    if not work_units:
        return []

    cfg = config or ScoringConfig()

    # 1. Compute unlock values and apply via copies (H1 fix — no input mutation)
    unlock_vals = compute_unlock_values(work_units)
    scoring_units: list[WorkUnit] = []
    for wu in work_units:
        if wu.id and wu.id in unlock_vals:
            new_uv = max(wu.unblock_value, unlock_vals[wu.id])
            if new_uv != wu.unblock_value:
                scoring_units.append(wu.model_copy(update={"unblock_value": new_uv}))
            else:
                scoring_units.append(wu)
        else:
            scoring_units.append(wu)

    # 2. Score each
    scored = [score_task(wu, cfg) for wu in scoring_units]

    # 3. Sort descending
    scored.sort(key=lambda s: s.final_score, reverse=True)

    # 4. Assign ranks
    for i, st in enumerate(scored, start=1):
        st.rank = i

    # 5. Normalise (highest = 100)
    max_score = scored[0].final_score if scored else 1.0
    if max_score <= 0.0:
        max_score = 1.0
    for st in scored:
        st.normalized_score = round((st.final_score / max_score) * 100, 2)

    return scored
