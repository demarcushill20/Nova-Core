"""Langfuse quality score integration for NovaCore.

Phase 7 Step 7.10 — Records quality scores to Langfuse Scores API
for trend tracking and regression detection.

Requires LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY environment variables.
Degrades gracefully when Langfuse is unavailable.

Usage:
    from utils.langfuse_quality import record_quality_score, check_quality_regression

    # Record a score to Langfuse for trend tracking
    record_quality_score(
        trace_id="abc123",
        category="heartbeat",
        score=0.85,
        rating="B",
        comment="Good but slow execution",
    )

    # Check if a score represents a regression
    result = check_quality_regression("heartbeat", 0.45)
    if result["regression"]:
        print(f"Regression! Score {result['score']} below threshold {result['threshold']}")
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

log = logging.getLogger("novacore.langfuse_quality")

# ---------------------------------------------------------------------------
# Lazy singleton — thread-safe, no import-time side effects
# ---------------------------------------------------------------------------
_langfuse = None
_langfuse_lock = threading.Lock()
_langfuse_init_attempted = False


def _get_langfuse():
    """Lazy-initialize Langfuse client (thread-safe).

    Returns the Langfuse client or None if unavailable.
    Re-uses the existing client from langfuse_tracing if available.
    """
    global _langfuse, _langfuse_init_attempted

    if _langfuse is not None:
        return _langfuse

    with _langfuse_lock:
        # Double-check after acquiring lock
        if _langfuse is not None:
            return _langfuse
        if _langfuse_init_attempted:
            return None

        _langfuse_init_attempted = True

        # Try to re-use the singleton from langfuse_tracing first
        try:
            from utils.langfuse_tracing import get_langfuse

            client = get_langfuse()
            if client is not None:
                _langfuse = client
                return _langfuse
        except Exception:
            pass

        # Fallback: create our own client
        try:
            from langfuse import Langfuse

            _langfuse = Langfuse()
            return _langfuse
        except Exception as exc:
            log.debug("Langfuse not available: %s", exc)
            return None


def _reset_client():
    """Reset the cached client (for testing only)."""
    global _langfuse, _langfuse_init_attempted
    with _langfuse_lock:
        _langfuse = None
        _langfuse_init_attempted = False


# ---------------------------------------------------------------------------
# Score recording
# ---------------------------------------------------------------------------


def record_quality_score(
    trace_id: str,
    category: str,
    score: float,
    rating: str = "",
    comment: str = "",
    metadata: dict | None = None,
) -> bool:
    """Record a quality score in Langfuse for trend tracking.

    Args:
        trace_id: Langfuse trace ID to attach the score to.
        category: Quality category (e.g., "heartbeat", "codegen", "research").
        score: Numeric score between 0.0 and 1.0.
        rating: Optional letter grade (A/B/C/D/F).
        comment: Optional human-readable explanation.
        metadata: Optional metadata dict to attach.

    Returns:
        True if successfully recorded, False otherwise.
    """
    client = _get_langfuse()
    if client is None:
        log.debug("Langfuse unavailable — quality score not recorded")
        return False

    try:
        client.create_score(
            trace_id=trace_id,
            name=f"quality_{category}",
            value=score,
            data_type="NUMERIC",
            comment=comment or f"Auto-scored by Phase 7 quality pipeline ({rating})",
            metadata={
                "category": category,
                "rating": rating,
                "recorded_at": datetime.now(timezone.utc).isoformat(),
                **(metadata or {}),
            },
        )
        return True
    except Exception as exc:
        log.warning("Failed to record quality score to Langfuse: %s", exc)
        return False


def record_batch_scores(scores: list[dict]) -> int:
    """Record multiple quality scores in batch.

    Each dict in *scores* is passed as kwargs to ``record_quality_score``.

    Returns:
        Count of successfully recorded scores.
    """
    success = 0
    for entry in scores:
        if record_quality_score(**entry):
            success += 1
    return success


# ---------------------------------------------------------------------------
# Quality trend retrieval (placeholder)
# ---------------------------------------------------------------------------


def get_quality_trend(category: str, days: int = 7) -> dict | None:
    """Get quality score trend for a category over N days.

    Returns a dict with ``average``, ``min``, ``max``, ``count``, ``trend``
    keys, or None if Langfuse is unavailable.

    Note: Full implementation requires Langfuse SDK score query support which
    is not yet available in the v4 SDK.  Currently returns None.
    """
    client = _get_langfuse()
    if client is None:
        return None
    # TODO: Implement when Langfuse SDK supports score queries
    return None


# ---------------------------------------------------------------------------
# Regression detection (local, no Langfuse dependency)
# ---------------------------------------------------------------------------

REGRESSION_THRESHOLDS: dict[str, float] = {
    "heartbeat": 0.60,
    "codegen": 0.70,
    "research": 0.60,
    "plan": 0.65,
    "bugfix": 0.65,
    "novatrade": 0.70,
}


def check_quality_regression(category: str, score: float) -> dict:
    """Check if a score represents a quality regression.

    Uses per-category thresholds. Unknown categories fall back to 0.60.

    Returns:
        dict with keys: regression, threshold, margin, category, score.
    """
    threshold = REGRESSION_THRESHOLDS.get(category, 0.60)
    return {
        "regression": score < threshold,
        "threshold": threshold,
        "margin": round(score - threshold, 4),
        "category": category,
        "score": score,
    }
