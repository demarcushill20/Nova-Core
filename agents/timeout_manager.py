"""P11.5 — Timeout, Cancellation and Error Recovery.

Robust failure handling for multi-agent workflows:
  - Per-agent deadline enforcement (graceful → force cancel)
  - Retry with exponential backoff + jitter
  - Cascading failure prevention via per-role circuit breakers
  - Retry storm detection (halt if too many retries in a window)
  - Compensation log for operator-reviewed rollback

Integrates with AgentSpawner (P11.1) for agent lifecycle management.
"""

from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from agents.validation import validate_id

logger = logging.getLogger(__name__)

BASE = Path("/home/nova/nova-core")
STATE = BASE / "STATE"
COMPENSATIONS_DIR = STATE / "compensations"


# ---------------------------------------------------------------------------
# Retry configuration
# ---------------------------------------------------------------------------


@dataclass
class RetryConfig:
    """Retry parameters for a single agent/node."""

    max_retries: int = 2
    base_delay_s: float = 1.0
    max_delay_s: float = 30.0
    jitter_factor: float = 0.5  # 0.0 = no jitter, 1.0 = full random
    retryable_errors: set[str] = field(
        default_factory=lambda: {
            "TimeoutError",
            "asyncio.TimeoutError",
            "ConnectionError",
            "OSError",
            "TemporaryError",
        }
    )

    def is_retryable(self, error_type: str) -> bool:
        """Check if an error type is eligible for retry."""
        return error_type in self.retryable_errors

    def delay_for_attempt(self, attempt: int) -> float:
        """Calculate delay with exponential backoff + jitter."""
        base = min(self.base_delay_s * (2**attempt), self.max_delay_s)
        jitter = random.uniform(0, base * self.jitter_factor)  # noqa: S311
        return base + jitter


# ---------------------------------------------------------------------------
# Per-role circuit breaker (lightweight)
# ---------------------------------------------------------------------------


@dataclass
class RoleCircuitBreaker:
    """Simple circuit breaker for a single agent role.

    Opens when failure_rate exceeds threshold within the window.
    """

    role: str
    window_s: float = 300.0  # 5-minute window
    failure_threshold: float = 0.5  # 50% failure rate opens the breaker
    min_calls: int = 3  # need at least this many calls to trigger

    _successes: list[float] = field(default_factory=list, repr=False)
    _failures: list[float] = field(default_factory=list, repr=False)
    _is_open: bool = False
    _opened_at: float | None = None

    @property
    def is_open(self) -> bool:
        # Auto-close after half the window
        if self._is_open and self._opened_at is not None and time.time() - self._opened_at > self.window_s / 2:
            self._is_open = False
            self._opened_at = None
        return self._is_open

    def _prune(self) -> None:
        """Remove events outside the window."""
        cutoff = time.time() - self.window_s
        self._successes = [t for t in self._successes if t > cutoff]
        self._failures = [t for t in self._failures if t > cutoff]

    def record_success(self) -> None:
        self._successes.append(time.time())
        self._prune()

    def record_failure(self) -> None:
        self._failures.append(time.time())
        self._prune()
        total = len(self._successes) + len(self._failures)
        if total >= self.min_calls:
            rate = len(self._failures) / total
            if rate >= self.failure_threshold:
                self._is_open = True
                self._opened_at = time.time()
                logger.warning(
                    "Circuit breaker OPEN for role %s: %.0f%% failure rate (%d/%d)",
                    self.role,
                    rate * 100,
                    len(self._failures),
                    total,
                )

    @property
    def failure_rate(self) -> float:
        self._prune()
        total = len(self._successes) + len(self._failures)
        if total == 0:
            return 0.0
        return len(self._failures) / total

    def to_dict(self) -> dict:
        return {
            "role": self.role,
            "is_open": self.is_open,
            "failure_rate": self.failure_rate,
            "successes": len(self._successes),
            "failures": len(self._failures),
        }


# ---------------------------------------------------------------------------
# Retry storm detector
# ---------------------------------------------------------------------------


@dataclass
class RetryStormDetector:
    """Detects excessive retries across the system.

    If more than `max_retries_in_window` retries happen within `window_s`,
    signals a halt.
    """

    window_s: float = 60.0
    max_retries_in_window: int = 10

    _retry_timestamps: list[float] = field(default_factory=list, repr=False)

    def record_retry(self) -> None:
        self._retry_timestamps.append(time.time())
        self._prune()

    def _prune(self) -> None:
        cutoff = time.time() - self.window_s
        self._retry_timestamps = [t for t in self._retry_timestamps if t > cutoff]

    @property
    def is_storm(self) -> bool:
        self._prune()
        return len(self._retry_timestamps) >= self.max_retries_in_window

    @property
    def retry_count(self) -> int:
        self._prune()
        return len(self._retry_timestamps)


# ---------------------------------------------------------------------------
# Compensation log
# ---------------------------------------------------------------------------


@dataclass
class CompensationEntry:
    """A record of an action that may need rollback."""

    workflow_id: str
    node_id: str
    action: str  # what was done
    rollback_instruction: str  # how to undo it
    timestamp: float = field(default_factory=time.time)
    status: str = "pending"  # pending | applied | skipped

    def to_dict(self) -> dict:
        return asdict(self)


class CompensationLog:
    """Append-only log of compensating transactions for a workflow."""

    def __init__(self, persist_dir: Path | None = None):
        self._dir = persist_dir or COMPENSATIONS_DIR
        self._dir.mkdir(parents=True, exist_ok=True)
        self._entries: list[CompensationEntry] = []

    def record(self, entry: CompensationEntry) -> None:
        self._entries.append(entry)
        self._persist(entry)

    def get_entries(self, workflow_id: str | None = None) -> list[CompensationEntry]:
        if workflow_id is None:
            return list(self._entries)
        return [e for e in self._entries if e.workflow_id == workflow_id]

    def _persist(self, entry: CompensationEntry) -> None:
        try:
            validate_id(entry.workflow_id, "workflow_id")
            path = self._dir / f"{entry.workflow_id}.jsonl"
            with open(path, "a") as f:
                f.write(json.dumps(entry.to_dict(), default=str) + "\n")
        except OSError as e:
            logger.error("Failed to persist compensation entry: %s", e)

    def load(self, workflow_id: str) -> list[CompensationEntry]:
        path = self._dir / f"{workflow_id}.jsonl"
        if not path.exists():
            return []
        entries = []
        for line in path.read_text().splitlines():
            if line.strip():
                try:
                    d = json.loads(line)
                    known = {f.name for f in CompensationEntry.__dataclass_fields__.values()}
                    entries.append(CompensationEntry(**{k: v for k, v in d.items() if k in known}))
                except (json.JSONDecodeError, TypeError):
                    continue
        # Populate in-memory entries to stay in sync with disk
        self._entries.extend(entries)
        return entries


# ---------------------------------------------------------------------------
# Timeout manager
# ---------------------------------------------------------------------------


class TimeoutManager:
    """Centralized timeout and error recovery management.

    Coordinates retry logic, circuit breakers, storm detection, and
    compensation logging for a workflow.
    """

    def __init__(
        self,
        retry_config: RetryConfig | None = None,
        compensation_dir: Path | None = None,
    ):
        self.retry_config = retry_config or RetryConfig()
        self._breakers: dict[str, RoleCircuitBreaker] = {}
        self._storm_detector = RetryStormDetector()
        self._compensation = CompensationLog(persist_dir=compensation_dir)

    def get_breaker(self, role: str) -> RoleCircuitBreaker:
        """Get or create a circuit breaker for a role."""
        if role not in self._breakers:
            self._breakers[role] = RoleCircuitBreaker(role=role)
        return self._breakers[role]

    def should_retry(self, role: str, error_type: str, attempt: int) -> bool:
        """Determine if a retry should be attempted.

        Returns False if:
          - Max retries exceeded
          - Error is not retryable
          - Circuit breaker is open for this role
          - Retry storm detected
        """
        if attempt >= self.retry_config.max_retries:
            return False
        if not self.retry_config.is_retryable(error_type):
            return False
        if self.get_breaker(role).is_open:
            logger.warning("Circuit breaker open for %s — not retrying", role)
            return False
        if self._storm_detector.is_storm:
            logger.warning("Retry storm detected — not retrying %s", role)
            return False
        return True

    def record_success(self, role: str) -> None:
        """Record a successful agent execution."""
        self.get_breaker(role).record_success()

    def record_failure(self, role: str) -> None:
        """Record a failed agent execution."""
        self.get_breaker(role).record_failure()
        self._storm_detector.record_retry()

    def get_retry_delay(self, attempt: int) -> float:
        """Get the delay before the next retry attempt."""
        return self.retry_config.delay_for_attempt(attempt)

    @property
    def is_retry_storm(self) -> bool:
        return self._storm_detector.is_storm

    @property
    def compensation_log(self) -> CompensationLog:
        return self._compensation

    def get_metrics(self) -> dict[str, Any]:
        return {
            "breakers": {role: b.to_dict() for role, b in self._breakers.items()},
            "retry_storm": self._storm_detector.is_storm,
            "retry_count_in_window": self._storm_detector.retry_count,
            "compensation_entries": len(self._compensation._entries),
        }
