"""NovaCore Breaker Registry — Phase 6B Steps 6.11 + 6.12.

Centralized circuit breaker instances for each external dependency.
Each breaker has tuned thresholds, a rolling failure window, and
excluded exceptions for programming errors that should not trip the
circuit.

The ``on_open`` callback is wired to the self-healing degradation tier
system so that circuit breaker state transitions automatically adjust
the runtime's degradation level.

Stdlib only.  Thread-safe (each breaker has its own internal lock).
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from agents.circuit_breakers import SimpleCircuitBreaker
from utils.self_healing import (
    evaluate_circuit_breakers,
    get_degradation_tier,
    set_degradation_tier,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# on_open callback — wired to self_healing degradation (step 6.12)
# ---------------------------------------------------------------------------


def _on_breaker_open(name: str) -> None:
    """Callback when any breaker transitions to OPEN.

    Evaluates all open breakers and escalates the degradation tier if
    the suggested tier is worse than the current one.

    NOTE: We read ``b._state.value`` directly instead of ``b.state``
    because the calling breaker's ``record_failure`` still holds its
    lock when it fires this callback, and ``b.state`` would re-acquire
    the same (non-reentrant) lock, causing a deadlock.
    """
    open_breakers = [bname for bname, b in BREAKERS.items() if b._state.value == "OPEN"]
    suggested = evaluate_circuit_breakers(open_breakers)
    current = get_degradation_tier()
    if suggested.value > current.tier.value:
        log.warning(
            "circuit breaker '%s' opened — escalating degradation %s -> %s",
            name,
            current.tier.name,
            suggested.name,
        )
        set_degradation_tier(suggested, reason=f"circuit breaker {name} opened")
    else:
        log.warning("circuit breaker '%s' tripped to OPEN (tier %s unchanged)", name, current.tier.name)


def _make_on_open(name: str) -> Callable[[str], None]:
    """Return the shared on_open callback for all breakers."""
    return _on_breaker_open


# ---------------------------------------------------------------------------
# Excluded exceptions — programming errors that must not trip breakers
# ---------------------------------------------------------------------------

_EXCLUDED: tuple[type[Exception], ...] = (ValueError, KeyError, TypeError)

# ---------------------------------------------------------------------------
# Central breaker registry
# ---------------------------------------------------------------------------

BREAKERS: dict[str, SimpleCircuitBreaker] = {
    "mcp": SimpleCircuitBreaker(
        name="mcp",
        failure_threshold=5,
        reset_timeout_seconds=30.0,
        window_seconds=60.0,
        excluded_exceptions=_EXCLUDED,
        on_open=_make_on_open("mcp"),
    ),
    "claude_api": SimpleCircuitBreaker(
        name="claude_api",
        failure_threshold=3,
        reset_timeout_seconds=60.0,
        window_seconds=120.0,
        excluded_exceptions=_EXCLUDED,
        on_open=_make_on_open("claude_api"),
    ),
    "metaapi": SimpleCircuitBreaker(
        name="metaapi",
        failure_threshold=3,
        reset_timeout_seconds=120.0,
        window_seconds=60.0,
        excluded_exceptions=_EXCLUDED,
        on_open=_make_on_open("metaapi"),
    ),
    "webhook": SimpleCircuitBreaker(
        name="webhook",
        failure_threshold=10,
        reset_timeout_seconds=30.0,
        window_seconds=60.0,
        excluded_exceptions=_EXCLUDED,
        on_open=_make_on_open("webhook"),
    ),
    "redis": SimpleCircuitBreaker(
        name="redis",
        failure_threshold=5,
        reset_timeout_seconds=30.0,
        window_seconds=60.0,
        excluded_exceptions=_EXCLUDED,
        on_open=_make_on_open("redis"),
    ),
    "pinecone": SimpleCircuitBreaker(
        name="pinecone",
        failure_threshold=3,
        reset_timeout_seconds=60.0,
        window_seconds=60.0,
        excluded_exceptions=_EXCLUDED,
        on_open=_make_on_open("pinecone"),
    ),
    "neo4j": SimpleCircuitBreaker(
        name="neo4j",
        failure_threshold=3,
        reset_timeout_seconds=60.0,
        window_seconds=60.0,
        excluded_exceptions=_EXCLUDED,
        on_open=_make_on_open("neo4j"),
    ),
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_breaker(name: str) -> SimpleCircuitBreaker:
    """Return the breaker for *name*.  Raises ``KeyError`` if unknown."""
    return BREAKERS[name]
