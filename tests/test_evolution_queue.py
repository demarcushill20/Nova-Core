"""Tests for skills.evolution_queue — evolution scheduling and anti-loop protection.

~30 tests covering:
- EvolutionRequest creation, serialization, round-trip
- EvolutionQueue enqueue/dequeue, priority ordering, dedup
- Queue persistence (save/load via tmp_path)
- Circuit breaker (trip, freeze, unfreeze, persistence)
- Cooldown enforcement
- Concurrency limits
- Audit trail
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from skills.evolution_queue import (
    CIRCUIT_BREAKER_MAX_FIXES,
    CIRCUIT_BREAKER_WINDOW,
    COOLDOWN_BETWEEN_EVOLUTIONS,
    MAX_CONCURRENT_EVOLUTIONS,
    MAX_QUEUE_SIZE,
    EvolutionQueue,
    EvolutionRequest,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request(
    skill_name: str = "test-skill",
    evolution_type: str = "FIX",
    priority: int = 3,
    **kwargs,
) -> EvolutionRequest:
    """Create an EvolutionRequest with sensible defaults."""
    return EvolutionRequest(
        skill_id=kwargs.pop("skill_id", f"sk_{skill_name}_001"),
        skill_name=skill_name,
        evolution_type=evolution_type,
        direction=kwargs.pop("direction", "fix the broken thing"),
        priority=priority,
        task_id=kwargs.pop("task_id", "task_001"),
        context=kwargs.pop("context", "test context"),
        **kwargs,
    )


@pytest.fixture
def queue(tmp_path: Path) -> EvolutionQueue:
    """Return an EvolutionQueue backed by a temp directory."""
    return EvolutionQueue(state_dir=str(tmp_path))


# ===========================================================================
# TestEvolutionRequest
# ===========================================================================


class TestEvolutionRequest:
    """EvolutionRequest creation and serialization."""

    def test_creation_with_defaults(self) -> None:
        req = EvolutionRequest(
            skill_id="sk_001",
            skill_name="my-skill",
            evolution_type="FIX",
            direction="fix error handling",
        )
        assert req.skill_id == "sk_001"
        assert req.skill_name == "my-skill"
        assert req.evolution_type == "FIX"
        assert req.direction == "fix error handling"
        assert req.priority == 3
        assert req.task_id == ""
        assert req.context == ""
        assert req.attempt_count == 0
        assert req.queued_at > 0

    def test_to_dict_contains_all_fields(self) -> None:
        req = _make_request()
        d = req.to_dict()
        assert "skill_id" in d
        assert "skill_name" in d
        assert "evolution_type" in d
        assert "direction" in d
        assert "priority" in d
        assert "task_id" in d
        assert "context" in d
        assert "queued_at" in d
        assert "attempt_count" in d

    def test_round_trip_to_dict_from_dict(self) -> None:
        original = _make_request(
            skill_name="round-trip",
            evolution_type="DERIVED",
            priority=1,
            task_id="task_rt",
            context="round trip context",
        )
        d = original.to_dict()
        restored = EvolutionRequest.from_dict(d)
        assert restored.skill_name == original.skill_name
        assert restored.evolution_type == original.evolution_type
        assert restored.priority == original.priority
        assert restored.task_id == original.task_id
        assert restored.context == original.context
        assert restored.queued_at == original.queued_at

    def test_from_dict_ignores_extra_keys(self) -> None:
        d = _make_request().to_dict()
        d["extra_field"] = "should be ignored"
        d["another_extra"] = 42
        restored = EvolutionRequest.from_dict(d)
        assert restored.skill_name == "test-skill"
        assert not hasattr(restored, "extra_field")


# ===========================================================================
# TestEvolutionQueue — basic operations
# ===========================================================================


class TestEvolutionQueue:
    """Queue enqueue/dequeue, priority, dedup, limits."""

    def test_enqueue_adds_to_queue(self, queue: EvolutionQueue) -> None:
        req = _make_request()
        assert queue.enqueue(req) is True
        assert queue.queue_size() == 1

    def test_enqueue_priority_ordering(self, queue: EvolutionQueue) -> None:
        low = _make_request(skill_name="low", priority=5)
        high = _make_request(skill_name="high", priority=1)
        mid = _make_request(skill_name="mid", priority=3)

        queue.enqueue(low)
        queue.enqueue(high)
        queue.enqueue(mid)

        first = queue.dequeue()
        assert first is not None
        assert first.skill_name == "high"

        second = queue.dequeue()
        assert second is not None
        assert second.skill_name == "mid"

    def test_enqueue_dedup_same_skill_and_type(self, queue: EvolutionQueue) -> None:
        req1 = _make_request(skill_name="dup-skill", evolution_type="FIX")
        req2 = _make_request(
            skill_name="dup-skill",
            evolution_type="FIX",
            direction="different direction",
        )

        assert queue.enqueue(req1) is True
        assert queue.enqueue(req2) is False
        assert queue.queue_size() == 1

    def test_enqueue_allows_different_types_same_skill(self, queue: EvolutionQueue) -> None:
        fix = _make_request(skill_name="multi", evolution_type="FIX")
        derived = _make_request(skill_name="multi", evolution_type="DERIVED")

        assert queue.enqueue(fix) is True
        assert queue.enqueue(derived) is True
        assert queue.queue_size() == 2

    def test_enqueue_when_queue_full(self, queue: EvolutionQueue) -> None:
        for i in range(MAX_QUEUE_SIZE):
            req = _make_request(skill_name=f"skill-{i}")
            assert queue.enqueue(req) is True

        overflow = _make_request(skill_name="overflow")
        assert queue.enqueue(overflow) is False
        assert queue.queue_size() == MAX_QUEUE_SIZE

    def test_dequeue_returns_none_when_empty(self, queue: EvolutionQueue) -> None:
        assert queue.dequeue() is None

    def test_dequeue_respects_max_concurrent(self, queue: EvolutionQueue) -> None:
        for i in range(MAX_CONCURRENT_EVOLUTIONS + 2):
            queue.enqueue(_make_request(skill_name=f"skill-{i}"))

        # Dequeue up to the max
        dequeued = []
        for _ in range(MAX_CONCURRENT_EVOLUTIONS + 1):
            item = queue.dequeue()
            if item is not None:
                dequeued.append(item)

        assert len(dequeued) == MAX_CONCURRENT_EVOLUTIONS

        # Complete one, then another should be available
        queue.complete_evolution(dequeued[0], success=True)
        # Need to clear cooldown for the completed skill
        # But a different skill should be available
        next_item = queue.dequeue()
        assert next_item is not None

    def test_complete_evolution_decrements_active_count(self, queue: EvolutionQueue) -> None:
        req = _make_request()
        queue.enqueue(req)
        dequeued = queue.dequeue()
        assert dequeued is not None

        status_before = queue.get_queue_status()
        assert status_before["active_evolutions"] == 1

        queue.complete_evolution(dequeued, success=True)
        status_after = queue.get_queue_status()
        assert status_after["active_evolutions"] == 0

    def test_clear_queue(self, queue: EvolutionQueue) -> None:
        for i in range(5):
            queue.enqueue(_make_request(skill_name=f"skill-{i}"))
        assert queue.queue_size() == 5

        cleared = queue.clear_queue()
        assert cleared == 5
        assert queue.queue_size() == 0

    def test_get_queue_status(self, queue: EvolutionQueue) -> None:
        queue.enqueue(_make_request(skill_name="s1", priority=2))
        queue.enqueue(_make_request(skill_name="s2", priority=1))

        status = queue.get_queue_status()
        assert status["queued"] == 2
        assert status["active_evolutions"] == 0
        assert status["max_concurrent"] == MAX_CONCURRENT_EVOLUTIONS
        assert status["max_queue_size"] == MAX_QUEUE_SIZE
        assert len(status["items"]) == 2
        # First item should be highest priority
        assert status["items"][0]["skill_name"] == "s2"


# ===========================================================================
# TestQueuePersistence
# ===========================================================================


class TestQueuePersistence:
    """Queue and history persistence across reload."""

    def test_queue_survives_reload(self, tmp_path: Path) -> None:
        q1 = EvolutionQueue(state_dir=str(tmp_path))
        q1.enqueue(_make_request(skill_name="persistent-skill"))
        assert q1.queue_size() == 1

        # Create new queue instance pointing to same dir
        q2 = EvolutionQueue(state_dir=str(tmp_path))
        assert q2.queue_size() == 1
        item = q2.dequeue()
        assert item is not None
        assert item.skill_name == "persistent-skill"

    def test_queue_file_created(self, tmp_path: Path) -> None:
        q = EvolutionQueue(state_dir=str(tmp_path))
        q.enqueue(_make_request())
        assert (tmp_path / "evolution_queue.json").exists()

    def test_history_file_created_on_complete(self, tmp_path: Path) -> None:
        q = EvolutionQueue(state_dir=str(tmp_path))
        req = _make_request()
        q.enqueue(req)
        dequeued = q.dequeue()
        assert dequeued is not None
        q.complete_evolution(dequeued, success=True)
        assert (tmp_path / "evolution_history.json").exists()

    def test_corrupted_queue_file_handled(self, tmp_path: Path) -> None:
        queue_path = tmp_path / "evolution_queue.json"
        queue_path.write_text("not valid json {{{{")
        q = EvolutionQueue(state_dir=str(tmp_path))
        assert q.queue_size() == 0  # gracefully falls back to empty

    def test_corrupted_history_file_handled(self, tmp_path: Path) -> None:
        history_path = tmp_path / "evolution_history.json"
        history_path.write_text("corrupted!")
        q = EvolutionQueue(state_dir=str(tmp_path))
        assert q.get_frozen_skills() == []  # gracefully empty


# ===========================================================================
# TestCircuitBreaker
# ===========================================================================


class TestCircuitBreaker:
    """Circuit breaker trips, freezes, unfreezes, persists."""

    def test_circuit_breaker_trips_after_max_fixes(self, queue: EvolutionQueue) -> None:
        skill = "fragile-skill"
        for i in range(CIRCUIT_BREAKER_MAX_FIXES):
            req = _make_request(
                skill_name=skill,
                evolution_type="FIX",
                skill_id=f"sk_{i}",
            )
            queue.enqueue(req)
            dequeued = queue.dequeue()
            assert dequeued is not None
            queue.complete_evolution(dequeued, success=False)
            # Clear cooldown so next enqueue isn't rejected for that reason
            queue._last_evolution.pop(skill, None)

        assert queue.is_frozen(skill) is True

    def test_circuit_breaker_does_not_trip_before_threshold(self, queue: EvolutionQueue) -> None:
        skill = "ok-skill"
        for i in range(CIRCUIT_BREAKER_MAX_FIXES - 1):
            req = _make_request(
                skill_name=skill,
                evolution_type="FIX",
                skill_id=f"sk_{i}",
            )
            queue.enqueue(req)
            dequeued = queue.dequeue()
            assert dequeued is not None
            queue.complete_evolution(dequeued, success=False)
            queue._last_evolution.pop(skill, None)

        assert queue.is_frozen(skill) is False

    def test_circuit_breaker_cleans_old_timestamps(self, queue: EvolutionQueue) -> None:
        skill = "aging-skill"
        old_time = time.time() - CIRCUIT_BREAKER_WINDOW - 100

        # Inject old fix timestamps directly
        with queue._lock:
            queue._fix_timestamps[skill] = [old_time, old_time + 1]

        # Now add one recent fix
        req = _make_request(skill_name=skill, evolution_type="FIX")
        queue.enqueue(req)
        dequeued = queue.dequeue()
        assert dequeued is not None
        queue.complete_evolution(dequeued, success=False)

        # Old timestamps should be cleaned, only 1 recent fix
        assert len(queue._fix_timestamps.get(skill, [])) == 1
        assert queue.is_frozen(skill) is False

    def test_is_frozen_returns_false_by_default(self, queue: EvolutionQueue) -> None:
        assert queue.is_frozen("nonexistent") is False

    def test_unfreeze_skill(self, queue: EvolutionQueue) -> None:
        # Freeze a skill manually
        with queue._lock:
            queue._frozen_skills.add("frozen-skill")

        assert queue.is_frozen("frozen-skill") is True
        assert queue.unfreeze_skill("frozen-skill") is True
        assert queue.is_frozen("frozen-skill") is False

    def test_unfreeze_nonexistent_returns_false(self, queue: EvolutionQueue) -> None:
        assert queue.unfreeze_skill("never-frozen") is False

    def test_frozen_skill_rejects_new_enqueue(self, queue: EvolutionQueue) -> None:
        with queue._lock:
            queue._frozen_skills.add("blocked-skill")

        req = _make_request(skill_name="blocked-skill")
        assert queue.enqueue(req) is False
        assert queue.queue_size() == 0

    def test_frozen_skill_skipped_during_dequeue(self, queue: EvolutionQueue) -> None:
        # Enqueue two skills, then freeze the first
        queue.enqueue(_make_request(skill_name="will-freeze", priority=1))
        queue.enqueue(_make_request(skill_name="will-work", priority=2))

        with queue._lock:
            queue._frozen_skills.add("will-freeze")

        dequeued = queue.dequeue()
        assert dequeued is not None
        assert dequeued.skill_name == "will-work"

    def test_circuit_breaker_persists_across_reload(self, tmp_path: Path) -> None:
        q1 = EvolutionQueue(state_dir=str(tmp_path))
        now = time.time()
        with q1._lock:
            q1._frozen_skills.add("persisted-freeze")
            # Must have >= CIRCUIT_BREAKER_MAX_FIXES recent timestamps
            # to remain frozen after load-time pruning
            q1._fix_timestamps["persisted-freeze"] = [now - 100, now - 50, now]
            q1._save_history()

        q2 = EvolutionQueue(state_dir=str(tmp_path))
        assert q2.is_frozen("persisted-freeze") is True
        assert "persisted-freeze" in q2._fix_timestamps

    def test_expired_freezes_pruned_on_load(self, tmp_path: Path) -> None:
        """Frozen skills with all timestamps outside the 24h window
        must be unfrozen on reload — otherwise the pipeline deadlocks."""
        q1 = EvolutionQueue(state_dir=str(tmp_path))
        old_ts = time.time() - CIRCUIT_BREAKER_WINDOW - 3600  # 25h ago
        with q1._lock:
            q1._frozen_skills.add("stale-skill")
            q1._fix_timestamps["stale-skill"] = [old_ts - 200, old_ts - 100, old_ts]
            q1._save_history()

        q2 = EvolutionQueue(state_dir=str(tmp_path))
        assert q2.is_frozen("stale-skill") is False
        assert "stale-skill" not in q2._fix_timestamps

    def test_partial_expiry_keeps_freeze_if_still_above_threshold(self, tmp_path: Path) -> None:
        """If some timestamps expired but enough remain, keep the freeze."""
        q1 = EvolutionQueue(state_dir=str(tmp_path))
        now = time.time()
        old_ts = now - CIRCUIT_BREAKER_WINDOW - 3600  # expired
        with q1._lock:
            q1._frozen_skills.add("mixed-skill")
            q1._fix_timestamps["mixed-skill"] = [
                old_ts,  # expired
                now - 100,  # active
                now - 50,  # active
                now,  # active
            ]
            q1._save_history()

        q2 = EvolutionQueue(state_dir=str(tmp_path))
        assert q2.is_frozen("mixed-skill") is True
        assert len(q2._fix_timestamps["mixed-skill"]) == 3  # expired one pruned

    def test_get_frozen_skills(self, queue: EvolutionQueue) -> None:
        with queue._lock:
            queue._frozen_skills.add("a")
            queue._frozen_skills.add("b")

        frozen = queue.get_frozen_skills()
        assert set(frozen) == {"a", "b"}


# ===========================================================================
# TestCooldown
# ===========================================================================


class TestCooldown:
    """Cooldown enforcement between evolutions of the same skill."""

    def test_cooldown_rejects_immediate_re_enqueue(self, queue: EvolutionQueue) -> None:
        req = _make_request(skill_name="hot-skill")
        queue.enqueue(req)
        dequeued = queue.dequeue()
        assert dequeued is not None
        queue.complete_evolution(dequeued, success=True)

        # Immediately try to enqueue again — should be in cooldown
        req2 = _make_request(
            skill_name="hot-skill",
            evolution_type="DERIVED",
            direction="improve it",
        )
        assert queue.enqueue(req2) is False

    def test_cooldown_allows_after_expiry(self, queue: EvolutionQueue) -> None:
        req = _make_request(skill_name="cooled-skill")
        queue.enqueue(req)
        dequeued = queue.dequeue()
        assert dequeued is not None
        queue.complete_evolution(dequeued, success=True)

        # Simulate cooldown expiry
        queue._last_evolution["cooled-skill"] = time.time() - COOLDOWN_BETWEEN_EVOLUTIONS - 1

        req2 = _make_request(
            skill_name="cooled-skill",
            evolution_type="DERIVED",
            direction="now improved",
        )
        assert queue.enqueue(req2) is True

    def test_is_in_cooldown(self, queue: EvolutionQueue) -> None:
        assert queue.is_in_cooldown("no-history") is False

        queue._last_evolution["recent"] = time.time()
        assert queue.is_in_cooldown("recent") is True

        queue._last_evolution["old"] = time.time() - COOLDOWN_BETWEEN_EVOLUTIONS - 1
        assert queue.is_in_cooldown("old") is False

    def test_dequeue_skips_cooldown_skills(self, queue: EvolutionQueue) -> None:
        queue.enqueue(_make_request(skill_name="cooling", priority=1))
        queue.enqueue(_make_request(skill_name="ready", priority=2))

        # Put "cooling" in cooldown
        queue._last_evolution["cooling"] = time.time()

        dequeued = queue.dequeue()
        assert dequeued is not None
        assert dequeued.skill_name == "ready"


# ===========================================================================
# TestAuditTrail
# ===========================================================================


class TestAuditTrail:
    """Audit trail JSONL persistence."""

    def test_save_history_entry_writes_jsonl(self, tmp_path: Path) -> None:
        q = EvolutionQueue(state_dir=str(tmp_path))
        req = _make_request(skill_name="audited-skill", task_id="task_audit")
        q.enqueue(req)
        dequeued = q.dequeue()
        assert dequeued is not None
        q.complete_evolution(dequeued, success=True)

        audit_path = tmp_path / "evolution_audit.jsonl"
        assert audit_path.exists()

        lines = audit_path.read_text().strip().split("\n")
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["skill_name"] == "audited-skill"
        assert entry["task_id"] == "task_audit"
        assert entry["type"] == "FIX"
        assert entry["success"] is True
        assert "timestamp" in entry

    def test_multiple_entries_accumulate(self, tmp_path: Path) -> None:
        q = EvolutionQueue(state_dir=str(tmp_path))

        for i in range(3):
            req = _make_request(skill_name=f"skill-{i}")
            q.enqueue(req)
            dequeued = q.dequeue()
            assert dequeued is not None
            q.complete_evolution(dequeued, success=(i % 2 == 0))
            # Clear cooldown for dedup-free enqueue
            q._last_evolution.clear()

        audit_path = tmp_path / "evolution_audit.jsonl"
        lines = audit_path.read_text().strip().split("\n")
        assert len(lines) == 3

        entries = [json.loads(line) for line in lines]
        assert entries[0]["success"] is True
        assert entries[1]["success"] is False
        assert entries[2]["success"] is True

    def test_audit_entry_includes_attempt_count(self, tmp_path: Path) -> None:
        q = EvolutionQueue(state_dir=str(tmp_path))
        req = _make_request(attempt_count=2)
        q.enqueue(req)
        dequeued = q.dequeue()
        assert dequeued is not None
        q.complete_evolution(dequeued, success=False)

        audit_path = tmp_path / "evolution_audit.jsonl"
        entry = json.loads(audit_path.read_text().strip())
        assert entry["attempt_count"] == 2
