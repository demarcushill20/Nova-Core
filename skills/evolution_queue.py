"""Evolution scheduling, queueing, and anti-loop protection.

Manages the evolution pipeline:
- Post-analysis triggers (immediate, from EvolutionSuggestion)
- Metric monitor triggers (periodic, from get_skill_health)
- Anti-loop protection (circuit breaker, cooldown, dedup)
- Persistent queue with priority ordering

Phase 8 of Self-Evolving Skills.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

logger = logging.getLogger(__name__)

# Anti-loop constants
CIRCUIT_BREAKER_MAX_FIXES = 3  # max fixes in window before freeze
CIRCUIT_BREAKER_WINDOW = 86400  # 24 hours
COOLDOWN_BETWEEN_EVOLUTIONS = 7200  # 2 hours same skill
MAX_CONCURRENT_EVOLUTIONS = 2
MAX_QUEUE_SIZE = 50


@dataclass
class EvolutionRequest:
    """A queued evolution request."""

    skill_id: str
    skill_name: str
    evolution_type: str  # FIX, DERIVED, CAPTURED
    direction: str
    priority: int = 3  # 1=highest, 5=lowest
    task_id: str = ""
    context: str = ""
    queued_at: float = field(default_factory=time.time)
    attempt_count: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> EvolutionRequest:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


class EvolutionQueue:
    """Priority queue for evolution requests with anti-loop protection.

    Thread-safe. Persists queue to disk for restart survival.
    """

    def __init__(self, state_dir: str = "STATE") -> None:
        self._queue: list[EvolutionRequest] = []
        self._lock = Lock()
        self._state_dir = Path(state_dir)
        self._queue_path = self._state_dir / "evolution_queue.json"
        self._history_path = self._state_dir / "evolution_history.json"

        # Anti-loop state
        self._fix_timestamps: dict[str, list[float]] = {}  # skill_name -> [timestamps]
        self._frozen_skills: set[str] = set()  # skills frozen by circuit breaker
        self._last_evolution: dict[str, float] = {}  # skill_name -> last evolution timestamp
        self._active_evolutions: int = 0

        self._load_queue()
        self._load_history()

    def enqueue(self, request: EvolutionRequest) -> bool:
        """Add an evolution request to the queue.

        Returns True if enqueued, False if rejected (frozen, duplicate, full,
        or in cooldown).
        """
        with self._lock:
            # Check frozen
            if request.skill_name in self._frozen_skills:
                logger.warning(
                    "Skill %s is frozen by circuit breaker, rejecting",
                    request.skill_name,
                )
                return False

            # Check cooldown
            last = self._last_evolution.get(request.skill_name, 0.0)
            if time.time() - last < COOLDOWN_BETWEEN_EVOLUTIONS:
                logger.info(
                    "Skill %s is in cooldown (%.0fs remaining), rejecting",
                    request.skill_name,
                    COOLDOWN_BETWEEN_EVOLUTIONS - (time.time() - last),
                )
                return False

            # Check queue size
            if len(self._queue) >= MAX_QUEUE_SIZE:
                logger.warning(
                    "Evolution queue full (%d), rejecting", MAX_QUEUE_SIZE
                )
                return False

            # Dedup: skip if same skill + same type already queued
            for existing in self._queue:
                if (
                    existing.skill_name == request.skill_name
                    and existing.evolution_type == request.evolution_type
                ):
                    logger.info(
                        "Duplicate evolution request for %s/%s, skipping",
                        request.skill_name,
                        request.evolution_type,
                    )
                    return False

            self._queue.append(request)
            # Sort by priority (lower number = higher priority)
            self._queue.sort(key=lambda r: r.priority)
            self._save_queue()

            logger.info(
                "Enqueued %s for %s (priority=%d)",
                request.evolution_type,
                request.skill_name,
                request.priority,
            )
            return True

    def dequeue(self) -> EvolutionRequest | None:
        """Get the next evolution request from the queue.

        Returns None if queue is empty or max concurrent reached.
        Skips requests for skills that are now frozen or in cooldown.
        """
        with self._lock:
            if self._active_evolutions >= MAX_CONCURRENT_EVOLUTIONS:
                logger.debug(
                    "Max concurrent evolutions reached (%d)",
                    MAX_CONCURRENT_EVOLUTIONS,
                )
                return None

            if not self._queue:
                return None

            # Find the first eligible request (not frozen, not in cooldown)
            for i, request in enumerate(self._queue):
                if request.skill_name in self._frozen_skills:
                    continue
                last = self._last_evolution.get(request.skill_name, 0.0)
                if time.time() - last < COOLDOWN_BETWEEN_EVOLUTIONS:
                    continue
                # Found an eligible request
                self._queue.pop(i)
                self._active_evolutions += 1
                self._save_queue()
                return request

            return None

    def complete_evolution(
        self, request: EvolutionRequest, success: bool
    ) -> None:
        """Mark an evolution as completed. Updates circuit breaker state."""
        with self._lock:
            self._active_evolutions = max(0, self._active_evolutions - 1)

            # Record cooldown timestamp
            self._last_evolution[request.skill_name] = time.time()

            if request.evolution_type == "FIX":
                self._record_fix(request.skill_name)

            self._save_history_entry(request, success)
            self._save_history()

    def _record_fix(self, skill_name: str) -> None:
        """Record a fix attempt for circuit breaker tracking."""
        now = time.time()

        if skill_name not in self._fix_timestamps:
            self._fix_timestamps[skill_name] = []

        self._fix_timestamps[skill_name].append(now)

        # Clean old entries outside window
        cutoff = now - CIRCUIT_BREAKER_WINDOW
        self._fix_timestamps[skill_name] = [
            ts for ts in self._fix_timestamps[skill_name] if ts > cutoff
        ]

        # Check circuit breaker
        if len(self._fix_timestamps[skill_name]) >= CIRCUIT_BREAKER_MAX_FIXES:
            self._frozen_skills.add(skill_name)
            logger.warning(
                "Circuit breaker tripped for %s: %d fixes in %ds window",
                skill_name,
                len(self._fix_timestamps[skill_name]),
                CIRCUIT_BREAKER_WINDOW,
            )

    def unfreeze_skill(self, skill_name: str) -> bool:
        """Manually unfreeze a skill frozen by circuit breaker."""
        with self._lock:
            if skill_name in self._frozen_skills:
                self._frozen_skills.discard(skill_name)
                self._fix_timestamps.pop(skill_name, None)
                self._last_evolution.pop(skill_name, None)
                self._save_history()
                logger.info("Unfroze skill: %s", skill_name)
                return True
            return False

    def is_frozen(self, skill_name: str) -> bool:
        """Check if a skill is frozen by circuit breaker."""
        return skill_name in self._frozen_skills

    def is_in_cooldown(self, skill_name: str) -> bool:
        """Check if a skill is currently in cooldown."""
        last = self._last_evolution.get(skill_name, 0.0)
        return (time.time() - last) < COOLDOWN_BETWEEN_EVOLUTIONS

    def get_queue_status(self) -> dict:
        """Return queue status summary."""
        with self._lock:
            return {
                "queued": len(self._queue),
                "active_evolutions": self._active_evolutions,
                "frozen_skills": list(self._frozen_skills),
                "max_concurrent": MAX_CONCURRENT_EVOLUTIONS,
                "max_queue_size": MAX_QUEUE_SIZE,
                "items": [r.to_dict() for r in self._queue[:10]],
            }

    def get_frozen_skills(self) -> list[str]:
        """Return list of frozen skill names."""
        return list(self._frozen_skills)

    def clear_queue(self) -> int:
        """Clear all queued requests. Returns count of cleared items."""
        with self._lock:
            count = len(self._queue)
            self._queue.clear()
            self._save_queue()
            return count

    def queue_size(self) -> int:
        """Return current queue size."""
        return len(self._queue)

    # -----------------------------------------------------------------
    # Persistence
    # -----------------------------------------------------------------

    def _load_queue(self) -> None:
        """Load queue from persistent storage."""
        try:
            if self._queue_path.exists():
                data = json.loads(self._queue_path.read_text())
                self._queue = [EvolutionRequest.from_dict(d) for d in data]
        except Exception as e:
            logger.warning("Failed to load evolution queue: %s", e)
            self._queue = []

    def _save_queue(self) -> None:
        """Persist queue to disk."""
        try:
            self._state_dir.mkdir(parents=True, exist_ok=True)
            self._queue_path.write_text(
                json.dumps([r.to_dict() for r in self._queue], indent=2)
            )
        except Exception as e:
            logger.warning("Failed to save evolution queue: %s", e)

    def _load_history(self) -> None:
        """Load anti-loop history from persistent storage."""
        try:
            if self._history_path.exists():
                data = json.loads(self._history_path.read_text())
                self._fix_timestamps = {
                    k: v for k, v in data.get("fix_timestamps", {}).items()
                }
                self._frozen_skills = set(data.get("frozen_skills", []))
                self._last_evolution = {
                    k: v for k, v in data.get("last_evolution", {}).items()
                }
        except Exception as e:
            logger.warning("Failed to load evolution history: %s", e)

    def _save_history(self) -> None:
        """Persist anti-loop history to disk."""
        try:
            self._state_dir.mkdir(parents=True, exist_ok=True)
            data = {
                "fix_timestamps": self._fix_timestamps,
                "frozen_skills": list(self._frozen_skills),
                "last_evolution": self._last_evolution,
            }
            self._history_path.write_text(json.dumps(data, indent=2))
        except Exception as e:
            logger.warning("Failed to save evolution history: %s", e)

    def _save_history_entry(
        self, request: EvolutionRequest, success: bool
    ) -> None:
        """Append a completed evolution to the audit trail."""
        try:
            self._state_dir.mkdir(parents=True, exist_ok=True)
            audit_path = self._state_dir / "evolution_audit.jsonl"
            entry = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "skill_name": request.skill_name,
                "skill_id": request.skill_id,
                "type": request.evolution_type,
                "direction": request.direction,
                "task_id": request.task_id,
                "success": success,
                "attempt_count": request.attempt_count,
            }
            with open(audit_path, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as e:
            logger.warning("Failed to write evolution audit entry: %s", e)
