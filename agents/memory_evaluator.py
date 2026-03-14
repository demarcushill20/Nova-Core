"""Phase 4 — Memory Candidate Evaluator.

Deterministic policy-based evaluation of memory candidates. Answers:
- Is this worth storing at all?
- Which layer is appropriate?
- Is it promotion-eligible?
- What blocks promotion if anything?

Operates on CanonicalMemoryObject instances after ingestion but before
durable persistence. Integrates into the router's store() path.

Usage:
    from agents.memory_evaluator import evaluate_candidate, EvalDecision

    decision = evaluate_candidate(obj)
    if decision.action == "reject":
        # Don't store
    elif decision.action == "keep_in_current_layer":
        # Store in assigned layer
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Decision actions — what the evaluator can decide
# ---------------------------------------------------------------------------

VALID_EVAL_ACTIONS = frozenset(
    {
        "reject",  # Do not store. Too noisy, empty, or invalid.
        "working_only",  # Store in working layer only. Transient.
        "keep_in_current_layer",  # Store as-is. Not promotion-eligible yet.
        "keep_and_mark_promotable",  # Store and flag as promotion-eligible.
    }
)


# ---------------------------------------------------------------------------
# Evaluation result
# ---------------------------------------------------------------------------


@dataclass
class EvalDecision:
    """Result of evaluating a memory candidate."""

    action: str  # One of VALID_EVAL_ACTIONS
    reason: str  # Human-readable justification
    importance_score: float  # 0.0–1.0, higher = more worth storing
    novelty_score: float  # 0.0–1.0, higher = more novel
    durability_score: float  # 0.0–1.0, higher = more durable value
    promotion_eligibility: str  # eligible, ineligible, needs_review, already_promoted
    promotion_blocker: str | None  # What prevents promotion, if anything
    adjusted_layer: str | None  # Non-None if evaluator recommends layer change

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Scoring rules — deterministic, inspectable
# ---------------------------------------------------------------------------

# Event types that are inherently transient (low durability)
_TRANSIENT_EVENT_TYPES = frozenset(
    {
        "heartbeat_cycle",
        "session_end",
    }
)

# Event types that represent completed outcomes (higher importance)
_OUTCOME_EVENT_TYPES = frozenset(
    {
        "task_completed",
        "task_failed",
        "plan_created",
        "plan_revised",
        "research_completed",
        "bug_fixed",
        "code_changed",
    }
)

# Event types that represent durable knowledge (high durability)
_DURABLE_EVENT_TYPES = frozenset(
    {
        "workflow_learning_promoted",
        "agent_pattern_promoted",
        "decision_made",
        "user_preference",
    }
)

# Confidence → numeric weight for score computation
_CONFIDENCE_WEIGHTS = {
    "high": 1.0,
    "medium": 0.6,
    "low": 0.3,
}

# Minimum summary length for episodic storage (below this, working-only)
_MIN_EPISODIC_SUMMARY_LEN = 20

# Minimum importance score to persist in episodic layer
_MIN_EPISODIC_IMPORTANCE = 0.3

# Minimum importance score to be promotion-eligible
_MIN_PROMOTABLE_IMPORTANCE = 0.5

# Minimum durability score to be promotion-eligible
_MIN_PROMOTABLE_DURABILITY = 0.5


# ---------------------------------------------------------------------------
# Core evaluation function
# ---------------------------------------------------------------------------


def evaluate_candidate(obj: Any) -> EvalDecision:
    """Evaluate a CanonicalMemoryObject for storage worthiness.

    Deterministic policy: no ML, no heuristic guessing. Uses event_type,
    confidence, summary length, layer, and content signals to produce
    an explicit decision with scores and reasons.

    Args:
        obj: A CanonicalMemoryObject (or duck-typed equivalent with the
             same fields).

    Returns:
        EvalDecision with action, scores, and reasoning.
    """
    event_type = obj.event_type
    confidence = obj.confidence
    current_layer = obj.current_layer
    summary = obj.summary or ""
    title = obj.title or ""

    # --- Compute scores ---
    importance = _compute_importance(event_type, confidence, summary, title)
    novelty = _compute_novelty(event_type, summary)
    durability = _compute_durability(event_type, current_layer, confidence)

    # --- Apply decision rules ---

    # Rule 1: Empty or near-empty content is rejected
    if len(summary.strip()) < 5 and len(title.strip()) < 5:
        return EvalDecision(
            action="reject",
            reason="content_too_sparse: title and summary both near-empty",
            importance_score=importance,
            novelty_score=novelty,
            durability_score=durability,
            promotion_eligibility="ineligible",
            promotion_blocker="empty content",
            adjusted_layer=None,
        )

    # Rule 2: Transient events are working-only
    if event_type in _TRANSIENT_EVENT_TYPES:
        return EvalDecision(
            action="working_only",
            reason=f"transient_event: {event_type} is inherently short-lived",
            importance_score=importance,
            novelty_score=novelty,
            durability_score=durability,
            promotion_eligibility="eligible" if importance >= _MIN_PROMOTABLE_IMPORTANCE else "ineligible",
            promotion_blocker="transient event type" if importance < _MIN_PROMOTABLE_IMPORTANCE else None,
            adjusted_layer="working",
        )

    # Rule 3: Already-promoted events (semantic/procedural provenance) are kept as-is
    if event_type in _DURABLE_EVENT_TYPES:
        return EvalDecision(
            action="keep_in_current_layer",
            reason=f"durable_event: {event_type} represents verified knowledge",
            importance_score=importance,
            novelty_score=novelty,
            durability_score=durability,
            promotion_eligibility="already_promoted" if current_layer in ("semantic", "procedural") else "needs_review",
            promotion_blocker=None,
            adjusted_layer=None,
        )

    # Rule 4: Low-importance episodic candidates are downgraded to working-only
    if current_layer == "episodic" and importance < _MIN_EPISODIC_IMPORTANCE:
        return EvalDecision(
            action="working_only",
            reason=f"low_importance: score {importance:.2f} < {_MIN_EPISODIC_IMPORTANCE} threshold for episodic",
            importance_score=importance,
            novelty_score=novelty,
            durability_score=durability,
            promotion_eligibility="ineligible",
            promotion_blocker=f"importance {importance:.2f} below episodic threshold",
            adjusted_layer="working",
        )

    # Rule 5: Episodic candidates with thin summaries are downgraded
    if current_layer == "episodic" and len(summary.strip()) < _MIN_EPISODIC_SUMMARY_LEN:
        return EvalDecision(
            action="working_only",
            reason=f"thin_summary: {len(summary.strip())} chars < {_MIN_EPISODIC_SUMMARY_LEN} min for episodic",
            importance_score=importance,
            novelty_score=novelty,
            durability_score=durability,
            promotion_eligibility="ineligible",
            promotion_blocker="summary too thin for episodic persistence",
            adjusted_layer="working",
        )

    # Rule 6: High-value outcome events get promotable flag
    if (
        event_type in _OUTCOME_EVENT_TYPES
        and importance >= _MIN_PROMOTABLE_IMPORTANCE
        and durability >= _MIN_PROMOTABLE_DURABILITY
    ):
        return EvalDecision(
            action="keep_and_mark_promotable",
            reason=f"promotable_outcome: {event_type} with importance={importance:.2f} durability={durability:.2f}",
            importance_score=importance,
            novelty_score=novelty,
            durability_score=durability,
            promotion_eligibility="eligible",
            promotion_blocker=None,
            adjusted_layer=None,
        )

    # Rule 7: Default — keep in current layer, not yet promotable
    promo_blocker = None
    if importance < _MIN_PROMOTABLE_IMPORTANCE:
        promo_blocker = f"importance {importance:.2f} below promotable threshold"
    elif durability < _MIN_PROMOTABLE_DURABILITY:
        promo_blocker = f"durability {durability:.2f} below promotable threshold"

    return EvalDecision(
        action="keep_in_current_layer",
        reason=f"standard_keep: {event_type} in {current_layer} layer",
        importance_score=importance,
        novelty_score=novelty,
        durability_score=durability,
        promotion_eligibility="ineligible" if promo_blocker else "eligible",
        promotion_blocker=promo_blocker,
        adjusted_layer=None,
    )


# ---------------------------------------------------------------------------
# Score computation helpers — deterministic, no ML
# ---------------------------------------------------------------------------


def _compute_importance(
    event_type: str,
    confidence: str,
    summary: str,
    title: str,
) -> float:
    """Compute importance score (0.0–1.0).

    Based on: event type weight + confidence weight + content density.
    """
    # Base weight from event type
    if event_type in _DURABLE_EVENT_TYPES:
        base = 0.8
    elif event_type in _OUTCOME_EVENT_TYPES:
        base = 0.6
    elif event_type in _TRANSIENT_EVENT_TYPES:
        base = 0.2
    else:
        base = 0.4

    # Confidence modifier
    conf_weight = _CONFIDENCE_WEIGHTS.get(confidence, 0.5)

    # Content density: longer summaries with more substance score higher
    summary_len = len(summary.strip())
    if summary_len >= 200:
        content_bonus = 0.2
    elif summary_len >= 50:
        content_bonus = 0.1
    else:
        content_bonus = 0.0

    score = (base * 0.5) + (conf_weight * 0.3) + (content_bonus * 0.2)
    return round(min(1.0, max(0.0, score)), 2)


def _compute_novelty(event_type: str, summary: str) -> float:
    """Compute novelty score (0.0–1.0).

    Phase 4 conservative implementation: novelty is inferred from event type
    only. True novelty detection (comparing against existing memories)
    requires Phase 5+ retrieval-augmented evaluation.
    """
    # Outcome events are assumed novel (first occurrence of that specific task)
    if event_type in _OUTCOME_EVENT_TYPES:
        return 0.7
    # Durable events have already been deduplicated by promotion pipeline
    if event_type in _DURABLE_EVENT_TYPES:
        return 0.5
    # Transient events repeat frequently
    if event_type in _TRANSIENT_EVENT_TYPES:
        return 0.2
    return 0.4


def _compute_durability(
    event_type: str,
    current_layer: str,
    confidence: str,
) -> float:
    """Compute durability score (0.0–1.0).

    How long-lived the value of this memory is expected to be.
    """
    # Layer-based baseline
    layer_base = {
        "working": 0.1,
        "episodic": 0.4,
        "semantic": 0.7,
        "procedural": 0.9,
    }.get(current_layer, 0.3)

    # Event type modifier
    if event_type in _DURABLE_EVENT_TYPES:
        type_mod = 0.3
    elif event_type in _OUTCOME_EVENT_TYPES:
        type_mod = 0.1
    else:
        type_mod = 0.0

    # Confidence modifier
    conf_mod = _CONFIDENCE_WEIGHTS.get(confidence, 0.5) * 0.1

    score = layer_base + type_mod + conf_mod
    return round(min(1.0, max(0.0, score)), 2)
