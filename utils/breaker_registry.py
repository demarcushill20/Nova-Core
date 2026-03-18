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
import os
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

    H4+H5 fix: The callback now fires outside the breaker's lock (H4),
    and we use the public ``b.is_open`` property (H5) which is a lock-free
    snapshot read, instead of accessing the private ``b._state.value``.
    """
    open_breakers = [bname for bname, b in BREAKERS.items() if b.is_open]
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
# M1 fix: configurable thresholds via environment variables
# ---------------------------------------------------------------------------

# Defaults per breaker: (failure_threshold, reset_timeout_seconds, window_seconds)
_DEFAULTS: dict[str, tuple[int, float, float]] = {
    "mcp": (5, 30.0, 60.0),
    "claude_api": (3, 60.0, 120.0),
    "metaapi": (3, 120.0, 60.0),
    "webhook": (10, 30.0, 60.0),
    "redis": (5, 30.0, 60.0),
    "pinecone": (3, 60.0, 60.0),
    "neo4j": (3, 60.0, 60.0),
}


def _env_int(name: str, default: int) -> int:
    """Read int from env, fall back to default."""
    val = os.environ.get(name)
    if val is not None:
        try:
            return int(val)
        except ValueError:
            log.warning("Invalid env %s=%r — using default %d", name, val, default)
    return default


def _env_float(name: str, default: float) -> float:
    """Read float from env, fall back to default."""
    val = os.environ.get(name)
    if val is not None:
        try:
            return float(val)
        except ValueError:
            log.warning("Invalid env %s=%r — using default %s", name, val, default)
    return default


def _breaker_cfg(name: str) -> tuple[int, float, float]:
    """Return (failure_threshold, reset_timeout, window) for *name*.

    Environment variable overrides:
        BREAKER_{NAME}_THRESHOLD   — failure threshold (int)
        BREAKER_{NAME}_RESET_SEC   — reset timeout in seconds (float)
        BREAKER_{NAME}_WINDOW_SEC  — rolling window in seconds (float)
    """
    ft, rt, ws = _DEFAULTS.get(name, (5, 60.0, 60.0))
    prefix = f"BREAKER_{name.upper()}"
    return (
        _env_int(f"{prefix}_THRESHOLD", ft),
        _env_float(f"{prefix}_RESET_SEC", rt),
        _env_float(f"{prefix}_WINDOW_SEC", ws),
    )


# ---------------------------------------------------------------------------
# Central breaker registry
# ---------------------------------------------------------------------------


def _build_breakers() -> dict[str, SimpleCircuitBreaker]:
    """Construct breakers with env-overridable thresholds (M1)."""
    result: dict[str, SimpleCircuitBreaker] = {}
    for name in _DEFAULTS:
        ft, rt, ws = _breaker_cfg(name)
        result[name] = SimpleCircuitBreaker(
            name=name,
            failure_threshold=ft,
            reset_timeout_seconds=rt,
            window_seconds=ws,
            excluded_exceptions=_EXCLUDED,
            on_open=_make_on_open(name),
        )
    return result


BREAKERS: dict[str, SimpleCircuitBreaker] = _build_breakers()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_breaker(name: str) -> SimpleCircuitBreaker:
    """Return the breaker for *name*.  Raises ``KeyError`` if unknown."""
    return BREAKERS[name]
