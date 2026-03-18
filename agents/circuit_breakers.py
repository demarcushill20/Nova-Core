"""Circuit breakers and action budgets for NovaCore agent runtime.

Provides fault-tolerant execution with automatic degradation and
per-task action budgets to limit blast radius.

Phase 2.3 — Security Hardening.
Phase 6B Step 6.10 — Rolling window, excluded exceptions, async, on_open.

Stdlib only with optional Redis persistence. Thread-safe.
"""

import collections
import threading
import time
from collections.abc import Callable
from enum import Enum

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class CircuitBreakerError(Exception):
    """Raised when a circuit breaker is open and rejects a call."""

    def __init__(self, name: str, state: str, trip_count: int):
        self.name = name
        self.breaker_state = state
        self.trip_count = trip_count
        super().__init__(f"Circuit breaker '{name}' is {state} (tripped {trip_count} time(s)) — call rejected")


# ---------------------------------------------------------------------------
# Circuit Breaker
# ---------------------------------------------------------------------------


class _State(Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class SimpleCircuitBreaker:
    """Lightweight circuit breaker (no external deps).

    States:
        CLOSED    — normal operation, failures are counted
        OPEN      — tripped, all calls rejected until reset_timeout elapses
        HALF_OPEN — one trial call allowed; success closes, failure re-opens

    Parameters:
        name:                   human-readable label for logging
        failure_threshold:      failures within window before tripping (default 5)
        reset_timeout_seconds:  seconds in OPEN before moving to HALF_OPEN (default 60)
        redis_client:           optional redis.Redis instance for shared state
        window_size:            max events tracked in the rolling window (default 10)
        window_seconds:         time window for counting failures (default 60.0)
        excluded_exceptions:    exception types that should NOT count as failures
        on_open:                callback invoked on CLOSED/HALF_OPEN -> OPEN transition
    """

    def __init__(
        self,
        name: str = "default",
        failure_threshold: int = 5,
        reset_timeout_seconds: float = 60.0,
        redis_client=None,
        *,
        window_size: int = 10,
        window_seconds: float = 60.0,
        excluded_exceptions: tuple[type[Exception], ...] = (),
        on_open: Callable[[str], None] | None = None,
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.reset_timeout_seconds = reset_timeout_seconds
        self.window_seconds = window_seconds
        self.excluded_exceptions = excluded_exceptions
        self.on_open = on_open

        self._lock = threading.Lock()
        self._state = _State.CLOSED
        # Rolling window: deque of (monotonic_timestamp, success_bool)
        self._window: collections.deque[tuple[float, bool]] = collections.deque(maxlen=window_size)
        self._trip_count = 0
        self._last_failure_time: float = 0.0
        self._half_open_permitted = False

        # Legacy compat: kept for Redis persistence and __repr__
        self._consecutive_failures = 0

        # Optional Redis persistence -----------------------------------------
        self._redis = redis_client
        if self._redis is None:
            try:
                import redis as _redis_mod  # noqa: F811

                _r = _redis_mod.Redis(host="localhost", port=6379, db=3, socket_connect_timeout=1)
                _r.ping()
                self._redis = _r
            except Exception:
                self._redis = None

        # Restore state from Redis if available
        self._redis_key = f"novacore:cb:{self.name}"
        self._restore_from_redis()

    # -- Redis helpers --------------------------------------------------------

    def _restore_from_redis(self) -> None:
        """Restore breaker state from Redis.

        On restore, synthetic failure entries are seeded into the rolling
        window to match the persisted failure count. This prevents data
        loss where ``_failure_count_unlocked()`` would return 0 after a
        restart (since the window deque itself is not persisted). The
        synthetic entries use ``_last_failure_time`` as their timestamp,
        so they age out naturally after ``window_seconds``.
        """
        if self._redis is None:
            return
        try:
            data = self._redis.hgetall(self._redis_key)
            if not data:
                return
            self._consecutive_failures = int(data.get(b"failures", 0))
            self._trip_count = int(data.get(b"trip_count", 0))
            self._last_failure_time = float(data.get(b"last_failure", 0.0))
            state_str = (data.get(b"state", b"CLOSED")).decode()
            self._state = _State(state_str)

            # Seed window with synthetic failures to match persisted count.
            # Uses last_failure_time so entries age out naturally.
            if self._consecutive_failures > 0 and self._last_failure_time > 0:
                for _ in range(self._consecutive_failures):
                    self._window.append((self._last_failure_time, False))
        except Exception:  # noqa: S110 — intentional: degrade to in-memory
            pass

    def _persist_to_redis(self) -> None:
        if self._redis is None:
            return
        try:
            self._redis.hset(
                self._redis_key,
                mapping={
                    "state": self._state.value,
                    "failures": str(self._consecutive_failures),
                    "trip_count": str(self._trip_count),
                    "last_failure": str(self._last_failure_time),
                },
            )
            # Auto-expire after 24h to avoid stale keys
            self._redis.expire(self._redis_key, 86400)
        except Exception:  # noqa: S110 — intentional: degrade to in-memory
            pass

    # -- Public API -----------------------------------------------------------

    @property
    def state(self) -> str:
        """Current breaker state as a string."""
        with self._lock:
            self._maybe_transition()
            return self._state.value

    @property
    def is_open(self) -> bool:
        """Return True if breaker is currently OPEN.

        Lock-free snapshot read for use in contexts where the caller already
        holds a lock that would deadlock with ``state`` (H5 fix). The read
        of an enum attribute is atomic on CPython due to the GIL.
        """
        return self._state == _State.OPEN

    @property
    def trip_count(self) -> int:
        """Number of times this breaker has tripped."""
        with self._lock:
            return self._trip_count

    @property
    def failure_count(self) -> int:
        """Failures within the current rolling window."""
        with self._lock:
            return self._failure_count_unlocked()

    def _failure_count_unlocked(self) -> int:
        """Count failures inside *window_seconds* (caller holds lock)."""
        now = time.monotonic()
        cutoff = now - self.window_seconds
        return sum(1 for ts, ok in self._window if not ok and ts >= cutoff)

    def record_failure(self) -> None:
        """Record a failure and potentially trip the breaker.

        H4 fix: on_open callback is fired AFTER releasing the lock to avoid
        holding the breaker lock during disk I/O (set_degradation_tier).
        """
        fire_callback = False
        with self._lock:
            now = time.monotonic()
            self._window.append((now, False))
            self._last_failure_time = now

            # Update legacy counter for Redis compat
            self._consecutive_failures = self._failure_count_unlocked()

            if self._consecutive_failures >= self.failure_threshold:
                was_open = self._state == _State.OPEN
                if not was_open:
                    self._trip_count += 1
                    fire_callback = True
                self._state = _State.OPEN
            self._persist_to_redis()

        # Fire on_open callback outside the lock to avoid blocking
        if fire_callback and self.on_open is not None:
            try:
                self.on_open(self.name)
            except Exception:  # noqa: S110 — intentional: callback must not break breaker
                pass

    def record_success(self) -> None:
        """Record a success. In HALF_OPEN this closes the breaker.

        Unlike the old consecutive-failure model, a single success does NOT
        wipe the rolling window — it merely adds a success event. The breaker
        only closes when the rolling failure count drops below threshold
        (which happens naturally as old failures age out) or when a success
        arrives while in HALF_OPEN state.
        """
        with self._lock:
            now = time.monotonic()
            self._window.append((now, True))

            # HALF_OPEN -> CLOSED on any success (standard CB semantics)
            if self._state == _State.HALF_OPEN:
                self._state = _State.CLOSED

            # Also close if rolling failure count dropped below threshold
            self._consecutive_failures = self._failure_count_unlocked()
            if self._consecutive_failures < self.failure_threshold:
                self._state = _State.CLOSED

            self._persist_to_redis()

    def _check_state_or_raise(self) -> None:
        """Check state under lock; raise if OPEN or HALF_OPEN without token.

        In HALF_OPEN state, only one trial call is permitted. The first
        caller claims the ``_half_open_permitted`` token; subsequent
        callers are rejected until the trial completes.
        """
        with self._lock:
            self._maybe_transition()
            if self._state == _State.OPEN:
                raise CircuitBreakerError(self.name, self._state.value, self._trip_count)
            if self._state == _State.HALF_OPEN:
                if self._half_open_permitted:
                    self._half_open_permitted = False  # Claim the trial token
                else:
                    # Another thread already claimed the trial — reject
                    raise CircuitBreakerError(self.name, self._state.value, self._trip_count)

    def _is_excluded(self, exc: Exception) -> bool:
        """Return True if *exc* is an excluded exception type."""
        return bool(self.excluded_exceptions and isinstance(exc, self.excluded_exceptions))

    def call(self, func, *args, **kwargs):
        """Execute *func* through the circuit breaker.

        Raises CircuitBreakerError if the breaker is OPEN.
        In HALF_OPEN state, one trial call is allowed.
        Excluded exceptions are re-raised without counting as failures.
        """
        self._check_state_or_raise()

        # Execute outside the lock to avoid holding it during I/O
        try:
            result = func(*args, **kwargs)
        except Exception as exc:
            if self._is_excluded(exc):
                raise
            self.record_failure()
            raise

        self.record_success()
        return result

    async def acall(self, func, *args, **kwargs):
        """Async version of :meth:`call` — awaits the coroutine.

        Same state-machine semantics; designed for NovaTrade's async runtime.
        """
        self._check_state_or_raise()

        try:
            result = await func(*args, **kwargs)
        except Exception as exc:
            if self._is_excluded(exc):
                raise
            self.record_failure()
            raise

        self.record_success()
        return result

    # -- Internal helpers -----------------------------------------------------

    def _maybe_transition(self) -> None:
        """Transition OPEN -> HALF_OPEN when reset timeout elapses.

        Must be called while holding self._lock. Sets the half-open trial
        token so that exactly one call is permitted through.
        """
        if self._state == _State.OPEN and self._last_failure_time > 0:
            elapsed = time.monotonic() - self._last_failure_time
            if elapsed >= self.reset_timeout_seconds:
                self._state = _State.HALF_OPEN
                self._half_open_permitted = True

    def __repr__(self) -> str:
        return (
            f"SimpleCircuitBreaker(name={self.name!r}, state={self.state}, "
            f"failures={self.failure_count}/{self.failure_threshold}, "
            f"trips={self._trip_count})"
        )


# ---------------------------------------------------------------------------
# Pre-configured breakers
# ---------------------------------------------------------------------------

claude_api_breaker = SimpleCircuitBreaker(
    name="claude_api",
    failure_threshold=5,
    reset_timeout_seconds=60.0,
)

tool_execution_breaker = SimpleCircuitBreaker(
    name="tool_execution",
    failure_threshold=3,
    reset_timeout_seconds=120.0,
)

_mcp_breakers: dict[str, SimpleCircuitBreaker] = {}
_mcp_breakers_lock = threading.Lock()


def mcp_breaker(server_name: str) -> SimpleCircuitBreaker:
    """Return a cached circuit breaker for the given MCP server.

    Creates one on first call per *server_name* (3 failures, 30s reset).
    """
    with _mcp_breakers_lock:
        if server_name not in _mcp_breakers:
            _mcp_breakers[server_name] = SimpleCircuitBreaker(
                name=f"mcp_{server_name}",
                failure_threshold=3,
                reset_timeout_seconds=30.0,
            )
        return _mcp_breakers[server_name]


# ---------------------------------------------------------------------------
# Action Budget
# ---------------------------------------------------------------------------

_DEFAULT_LIMITS: dict[str, int | None] = {
    "read": None,  # unlimited
    "write": 20,
    "execute": 10,
    "destructive": 3,
}

_DESTRUCTIVE_COOLDOWN_SECONDS = 30.0


class ActionBudget:
    """Per-task action budget to limit blast radius.

    Categories and default limits:
        read        — unlimited
        write       — 20
        execute     — 10
        destructive — 3 (with 30s cooldown between each)

    Usage::

        ok, reason = action_budget.check("write")
        if not ok:
            raise RuntimeError(reason)
        action_budget.record("write")
    """

    def __init__(self, limits: dict[str, int | None] | None = None):
        self._limits = dict(_DEFAULT_LIMITS)
        if limits:
            self._limits.update(limits)

        self._lock = threading.Lock()
        self._counters: dict[str, int] = {cat: 0 for cat in self._limits}
        self._last_destructive: float = 0.0

    def check(self, category: str) -> tuple[bool, str]:
        """Return (allowed, reason) for the given action category.

        Does NOT consume from the budget — call :meth:`record` after
        the action completes successfully.
        """
        with self._lock:
            if category not in self._limits:
                return False, f"Unknown action category: {category!r}"

            limit = self._limits[category]

            # Unlimited category
            if limit is None:
                return True, "ok"

            current = self._counters.get(category, 0)
            if current >= limit:
                return False, (f"Budget exhausted for {category!r}: {current}/{limit} actions used")

            # Destructive cooldown
            if category == "destructive" and self._last_destructive > 0:
                elapsed = time.monotonic() - self._last_destructive
                if elapsed < _DESTRUCTIVE_COOLDOWN_SECONDS:
                    remaining = _DESTRUCTIVE_COOLDOWN_SECONDS - elapsed
                    return False, (f"Destructive cooldown: {remaining:.1f}s remaining")

            return True, "ok"

    def record(self, category: str) -> None:
        """Increment the counter for *category*.

        Raises ValueError for unknown categories.
        """
        with self._lock:
            if category not in self._limits:
                raise ValueError(f"Unknown action category: {category!r}")
            self._counters[category] = self._counters.get(category, 0) + 1
            if category == "destructive":
                self._last_destructive = time.monotonic()

    def reset(self) -> None:
        """Reset all counters. Call at the start of each new task."""
        with self._lock:
            self._counters = {cat: 0 for cat in self._limits}
            self._last_destructive = 0.0

    def summary(self) -> dict:
        """Return usage vs limits for all categories.

        Example::

            {
                "read":        {"used": 42, "limit": None,  "remaining": None},
                "write":       {"used": 5,  "limit": 20,    "remaining": 15},
                "execute":     {"used": 2,  "limit": 10,    "remaining": 8},
                "destructive": {"used": 0,  "limit": 3,     "remaining": 3},
            }
        """
        with self._lock:
            result = {}
            for cat, limit in self._limits.items():
                used = self._counters.get(cat, 0)
                if limit is None:
                    remaining = None
                else:
                    remaining = max(0, limit - used)
                result[cat] = {
                    "used": used,
                    "limit": limit,
                    "remaining": remaining,
                }
            return result

    def __repr__(self) -> str:
        parts = []
        for cat, info in self.summary().items():
            lim = info["limit"] if info["limit"] is not None else "∞"
            parts.append(f"{cat}={info['used']}/{lim}")
        return f"ActionBudget({', '.join(parts)})"


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

action_budget = ActionBudget()
