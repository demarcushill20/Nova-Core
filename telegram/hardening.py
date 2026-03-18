"""Production hardening for CEO Nova Telegram bot.

Rate limiting, metrics collection, circuit breaker, and response caching.
"""

from __future__ import annotations

import json
import logging
import time
from collections import defaultdict
from pathlib import Path

_log = logging.getLogger("telegram_bot.hardening")

STATE_DIR = Path("/home/nova/nova-core/STATE")
METRICS_FILE = STATE_DIR / "ceo_metrics.json"


# ── Rate Limiter ─────────────────────────────────────────────────────────


class RateLimiter:
    """Token bucket rate limiter with per-chat and global limits."""

    def __init__(
        self,
        per_chat_limit: int = 10,
        per_chat_window: int = 60,
        global_limit: int = 30,
        global_window: int = 60,
    ) -> None:
        self.per_chat_limit = per_chat_limit
        self.per_chat_window = per_chat_window
        self.global_limit = global_limit
        self.global_window = global_window
        self._chat_timestamps: dict[str, list[float]] = defaultdict(list)
        self._global_timestamps: list[float] = []

    def _prune(self, timestamps: list[float], window: int) -> list[float]:
        cutoff = time.time() - window
        return [t for t in timestamps if t > cutoff]

    def check(self, chat_id: str) -> tuple[bool, str]:
        """Check if a request is allowed.

        Returns (allowed, reason). If not allowed, reason explains why.
        """
        time.time()

        # Global limit
        self._global_timestamps = self._prune(self._global_timestamps, self.global_window)
        if len(self._global_timestamps) >= self.global_limit:
            return False, "global"

        # Per-chat limit
        self._chat_timestamps[chat_id] = self._prune(self._chat_timestamps[chat_id], self.per_chat_window)
        if len(self._chat_timestamps[chat_id]) >= self.per_chat_limit:
            return False, "per_chat"

        return True, ""

    def record(self, chat_id: str) -> None:
        """Record a request (call after check passes)."""
        now = time.time()
        self._global_timestamps.append(now)
        self._chat_timestamps[chat_id].append(now)


RATE_LIMIT_MESSAGE = (
    "I'm getting a lot of messages right now — give me a moment to catch up. Try again in about 30 seconds."
)


# ── Circuit Breaker ──────────────────────────────────────────────────────


class CircuitBreaker:
    """Trip after consecutive failures, auto-recover after cooldown."""

    def __init__(
        self,
        failure_threshold: int = 5,
        cooldown_seconds: int = 120,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self._consecutive_failures: int = 0
        self._tripped_at: float = 0.0

    def is_open(self) -> bool:
        """True if the circuit is tripped (should not make calls)."""
        if self._consecutive_failures < self.failure_threshold:
            return False
        # Check if cooldown has elapsed
        if time.time() - self._tripped_at > self.cooldown_seconds:
            self._consecutive_failures = 0
            _log.info("Circuit breaker reset after cooldown")
            return False
        return True

    def record_success(self) -> None:
        self._consecutive_failures = 0

    def record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.failure_threshold:
            self._tripped_at = time.time()
            _log.warning(
                "Circuit breaker TRIPPED after %d failures (cooldown=%ds)",
                self._consecutive_failures,
                self.cooldown_seconds,
            )

    @property
    def failure_count(self) -> int:
        return self._consecutive_failures


CIRCUIT_OPEN_MESSAGE = (
    "I'm having trouble connecting to my brain right now. I'll be back online shortly — try again in a couple minutes."
)


# ── Metrics Collector ────────────────────────────────────────────────────


class MetricsCollector:
    """Track conversation metrics for observability."""

    def __init__(self) -> None:
        self._data = {
            "total_messages": 0,
            "conversation_messages": 0,
            "task_delegations": 0,
            "cache_hits": 0,
            "cache_misses": 0,
            "rate_limited": 0,
            "circuit_breaks": 0,
            "errors": 0,
            "total_response_time_ms": 0,
            "response_count": 0,
            "started_at": time.time(),
        }

    def record_message(self) -> None:
        self._data["total_messages"] += 1

    def record_conversation(self) -> None:
        self._data["conversation_messages"] += 1

    def record_delegation(self) -> None:
        self._data["task_delegations"] += 1

    def record_cache_hit(self) -> None:
        self._data["cache_hits"] += 1

    def record_cache_miss(self) -> None:
        self._data["cache_misses"] += 1

    def record_rate_limit(self) -> None:
        self._data["rate_limited"] += 1

    def record_circuit_break(self) -> None:
        self._data["circuit_breaks"] += 1

    def record_error(self) -> None:
        self._data["errors"] += 1

    def record_response_time(self, ms: float) -> None:
        self._data["total_response_time_ms"] += ms
        self._data["response_count"] += 1

    @property
    def avg_response_time_ms(self) -> float:
        if self._data["response_count"] == 0:
            return 0.0
        return self._data["total_response_time_ms"] / self._data["response_count"]

    def snapshot(self) -> dict:
        """Return a copy of current metrics."""
        snap = dict(self._data)
        snap["avg_response_time_ms"] = round(self.avg_response_time_ms, 1)
        snap["uptime_seconds"] = round(time.time() - snap["started_at"])
        return snap

    def persist(self) -> None:
        """Write metrics to disk."""
        try:
            METRICS_FILE.parent.mkdir(parents=True, exist_ok=True)
            METRICS_FILE.write_text(
                json.dumps(self.snapshot(), indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError as e:
            _log.warning("Failed to persist metrics: %s", e)


# ── Response Cache ───────────────────────────────────────────────────────


class ResponseCache:
    """Simple LRU cache with TTL for conversation responses."""

    def __init__(self, max_size: int = 50, ttl_seconds: int = 300) -> None:
        self.max_size = max_size
        self.ttl_seconds = ttl_seconds
        self._cache: dict[str, tuple[str, float]] = {}  # key → (response, timestamp)

    def _make_key(self, prompt: str, context: str) -> str:
        """Create a cache key from prompt + context."""
        # Simple hash — exact match only
        return f"{hash(prompt)}:{hash(context)}"

    def get(self, prompt: str, context: str = "") -> str | None:
        """Get a cached response, or None if miss/expired."""
        key = self._make_key(prompt, context)
        entry = self._cache.get(key)
        if entry is None:
            return None
        response, ts = entry
        if time.time() - ts > self.ttl_seconds:
            del self._cache[key]
            return None
        return response

    def put(self, prompt: str, response: str, context: str = "") -> None:
        """Cache a response."""
        # Evict oldest if at capacity
        if len(self._cache) >= self.max_size:
            oldest_key = min(self._cache, key=lambda k: self._cache[k][1])
            del self._cache[oldest_key]
        key = self._make_key(prompt, context)
        self._cache[key] = (response, time.time())

    @property
    def size(self) -> int:
        return len(self._cache)
