"""Tests for utils/session_counter.py — daily session circuit breaker.

Phase 7B additions: budget classification, reserved pools, carryover caps, warning thresholds.
"""

import json
import threading

import pytest


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    """Redirect STATE_DIR and STATE_FILE to temp for every test."""
    import utils.session_counter as mod

    monkeypatch.setattr(mod, "STATE_DIR", tmp_path)
    monkeypatch.setattr(mod, "STATE_FILE", tmp_path / "daily_session_count.json")


class TestIncrement:
    def test_first_increment_returns_one(self):
        from utils.session_counter import increment

        assert increment() == 1

    def test_sequential_increments(self):
        from utils.session_counter import increment

        assert increment() == 1
        assert increment() == 2
        assert increment() == 3

    def test_persists_to_disk(self, tmp_path):
        from utils.session_counter import increment

        increment()
        increment()

        data = json.loads((tmp_path / "daily_session_count.json").read_text())
        assert data["count"] == 2


class TestCanDispatch:
    def test_under_cap_allowed(self):
        from utils.session_counter import can_dispatch

        ok, reason = can_dispatch()
        assert ok is True
        assert reason == "OK"

    def test_at_cap_blocked(self, monkeypatch):
        import utils.session_counter as mod
        from utils.session_counter import can_dispatch, increment

        monkeypatch.setattr(mod, "MAX_DAILY_SESSIONS", 3)

        increment()
        increment()
        increment()

        ok, reason = can_dispatch()
        assert ok is False
        assert "3/3" in reason

    def test_over_cap_blocked(self, monkeypatch):
        import utils.session_counter as mod
        from utils.session_counter import can_dispatch, increment

        monkeypatch.setattr(mod, "MAX_DAILY_SESSIONS", 2)

        for _ in range(5):
            increment()

        ok, reason = can_dispatch()
        assert ok is False


class TestDayRoll:
    def test_rolls_on_new_day(self, tmp_path):
        """Counter resets when the date changes."""

        # Write state file with yesterday's date
        state = {"date": "2020-01-01", "count": 99}
        (tmp_path / "daily_session_count.json").write_text(json.dumps(state))

        from utils.session_counter import get_count

        # Should auto-roll since 2020-01-01 != today
        assert get_count() == 0

    def test_preserves_same_day(self, tmp_path, monkeypatch):
        """Counter is preserved within the same UTC day."""
        import utils.session_counter as mod

        today = mod._today_utc()
        state = {"date": today, "count": 15}
        (tmp_path / "daily_session_count.json").write_text(json.dumps(state))

        from utils.session_counter import get_count

        assert get_count() == 15


class TestGetCount:
    def test_zero_when_no_file(self):
        from utils.session_counter import get_count

        assert get_count() == 0

    def test_reflects_increments(self):
        from utils.session_counter import get_count, increment

        increment()
        increment()
        assert get_count() == 2


class TestCorruptStateFile:
    def test_handles_malformed_json(self, tmp_path):
        """Corrupt state file is treated as count=0."""
        (tmp_path / "daily_session_count.json").write_text("NOT JSON")

        from utils.session_counter import get_count

        assert get_count() == 0

    def test_handles_missing_fields(self, tmp_path):
        """State file with missing fields is treated as count=0."""
        (tmp_path / "daily_session_count.json").write_text("{}")

        from utils.session_counter import get_count

        # Missing "date" triggers day-roll → reset to 0
        assert get_count() == 0


class TestThreadSafety:
    def test_concurrent_increments(self):
        """Concurrent increments from multiple threads produce correct final count."""
        from utils.session_counter import get_count, increment

        errors: list[Exception] = []
        barrier = threading.Barrier(10)

        def _worker():
            try:
                barrier.wait(timeout=5)
                for _ in range(10):
                    increment()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"Thread errors: {errors}"
        assert get_count() == 100


class TestLoadCheckpointDoesNotIncrement:
    """Regression test: load_checkpoint must NOT modify retry_count.

    This verifies the Layer 1 fix — orphan recovery now uses load_checkpoint
    instead of increment_retry to read checkpoint state.
    """

    def test_load_does_not_modify_count(self, tmp_path, monkeypatch):
        import utils.task_checkpoint as cp_mod

        monkeypatch.setattr(cp_mod, "CHECKPOINT_DIR", tmp_path / "checkpoints")

        from utils.task_checkpoint import (
            TaskCheckpoint,
            _now_iso,
            load_checkpoint,
            save_checkpoint,
        )

        cp = TaskCheckpoint(
            task_id="test_no_increment",
            task_file="test_no_increment.md",
            status="dispatched",
            started_at=_now_iso(),
            last_updated=_now_iso(),
            retry_count=2,
        )
        save_checkpoint(cp)

        # Load multiple times — count must remain 2
        for _ in range(5):
            loaded = load_checkpoint("test_no_increment")
            assert loaded is not None
            assert loaded.retry_count == 2


# ─── Phase 7B: Budget Classification Tests ───


class TestCategoryIncrement:
    """increment() tracks per-category counts."""

    def test_default_category_is_adhoc(self, tmp_path):
        from utils.session_counter import increment

        increment()
        data = json.loads((tmp_path / "daily_session_count.json").read_text())
        assert data["categories"]["adhoc"] == 1

    def test_explicit_category_tracked(self, tmp_path):
        from utils.session_counter import increment

        increment("production")
        increment("production")
        increment("research")

        data = json.loads((tmp_path / "daily_session_count.json").read_text())
        assert data["categories"]["production"] == 2
        assert data["categories"]["research"] == 1
        assert data["count"] == 3

    def test_unknown_category_falls_back_to_adhoc(self, tmp_path):
        from utils.session_counter import increment

        increment("totally_unknown_category")
        data = json.loads((tmp_path / "daily_session_count.json").read_text())
        assert data["categories"]["adhoc"] == 1


class TestReservedProductionPool:
    """Production shift blocks have reserved budget that non-production cannot consume."""

    def test_production_allowed_when_non_prod_exhausted(self, monkeypatch):
        """Production can still dispatch even when non-prod pool is full."""
        import utils.session_counter as mod
        from utils.session_counter import can_dispatch, increment

        monkeypatch.setattr(mod, "MAX_DAILY_SESSIONS", 10)
        monkeypatch.setattr(mod, "RESERVED_PRODUCTION", 4)
        # shared pool = 10 - 4 = 6

        # Fill up non-production pool (6 adhoc)
        for _ in range(6):
            increment("adhoc")

        # Non-production should be blocked
        ok, reason = can_dispatch("adhoc")
        assert ok is False
        assert "Non-production pool exhausted" in reason

        # Production should still be allowed
        ok, reason = can_dispatch("production")
        assert ok is True

    def test_production_blocked_at_hard_cap(self, monkeypatch):
        """Even production is blocked at the absolute hard cap."""
        import utils.session_counter as mod
        from utils.session_counter import can_dispatch, increment

        monkeypatch.setattr(mod, "MAX_DAILY_SESSIONS", 5)
        monkeypatch.setattr(mod, "RESERVED_PRODUCTION", 2)

        for _ in range(5):
            increment("production")

        ok, reason = can_dispatch("production")
        assert ok is False
        assert "hard cap" in reason.lower()

    def test_non_production_respects_shared_pool(self, monkeypatch):
        """Non-production tasks cannot use reserved production slots."""
        import utils.session_counter as mod
        from utils.session_counter import can_dispatch, increment

        monkeypatch.setattr(mod, "MAX_DAILY_SESSIONS", 10)
        monkeypatch.setattr(mod, "RESERVED_PRODUCTION", 4)
        # shared pool = 6

        for _ in range(5):
            increment("research")
        # 5/6 used — still OK
        ok, _ = can_dispatch("research")
        assert ok is True

        increment("research")
        # 6/6 used — blocked
        ok, reason = can_dispatch("research")
        assert ok is False
        assert "Non-production pool exhausted" in reason


class TestCarryoverCap:
    """Carryover tasks from prior day have their own sub-cap."""

    def test_carryover_capped_independently(self, monkeypatch):
        import utils.session_counter as mod
        from utils.session_counter import can_dispatch, increment

        monkeypatch.setattr(mod, "MAX_DAILY_SESSIONS", 40)
        monkeypatch.setattr(mod, "RESERVED_PRODUCTION", 15)
        monkeypatch.setattr(mod, "MAX_CARRYOVER", 3)

        increment("carryover")
        increment("carryover")
        increment("carryover")

        ok, reason = can_dispatch("carryover")
        assert ok is False
        assert "Carryover cap reached" in reason

    def test_carryover_does_not_starve_production(self, monkeypatch):
        """Even with carryover cap hit, production is still allowed."""
        import utils.session_counter as mod
        from utils.session_counter import can_dispatch, increment

        monkeypatch.setattr(mod, "MAX_DAILY_SESSIONS", 40)
        monkeypatch.setattr(mod, "RESERVED_PRODUCTION", 15)
        monkeypatch.setattr(mod, "MAX_CARRYOVER", 3)

        for _ in range(3):
            increment("carryover")

        ok, _ = can_dispatch("production")
        assert ok is True


class TestWarningThresholds:
    """Warning thresholds are emitted at 70%, 85%, 95% of budget."""

    def test_warnings_emitted_at_thresholds(self, monkeypatch, tmp_path):
        import utils.session_counter as mod
        from utils.session_counter import increment

        monkeypatch.setattr(mod, "MAX_DAILY_SESSIONS", 10)
        monkeypatch.setattr(mod, "RESERVED_PRODUCTION", 3)

        # 70% of 10 = 7 sessions
        for _ in range(7):
            increment("adhoc")

        data = json.loads((tmp_path / "daily_session_count.json").read_text())
        assert "70%" in data["warnings_emitted"]

    def test_warnings_not_duplicated(self, monkeypatch, tmp_path):
        import utils.session_counter as mod
        from utils.session_counter import increment

        monkeypatch.setattr(mod, "MAX_DAILY_SESSIONS", 10)
        monkeypatch.setattr(mod, "RESERVED_PRODUCTION", 3)

        for _ in range(10):
            increment("adhoc")

        data = json.loads((tmp_path / "daily_session_count.json").read_text())
        # Each threshold should appear exactly once
        assert data["warnings_emitted"].count("70%") == 1
        assert data["warnings_emitted"].count("85%") == 1
        assert data["warnings_emitted"].count("95%") == 1


class TestGetStatus:
    """get_status() returns a complete budget snapshot."""

    def test_status_fields(self, monkeypatch):
        import utils.session_counter as mod
        from utils.session_counter import get_status, increment

        monkeypatch.setattr(mod, "MAX_DAILY_SESSIONS", 20)
        monkeypatch.setattr(mod, "RESERVED_PRODUCTION", 5)
        monkeypatch.setattr(mod, "MAX_CARRYOVER", 3)

        increment("production")
        increment("research")
        increment("carryover")

        status = get_status()
        assert status["total"] == 3
        assert status["max"] == 20
        assert status["pct_used"] == 15.0
        assert status["categories"]["production"] == 1
        assert status["categories"]["research"] == 1
        assert status["categories"]["carryover"] == 1
        assert status["production_remaining"] == 17
        assert status["shared_pool_remaining"] == 13  # (20 - 5) - 2 non-prod
        assert status["carryover_remaining"] == 2


class TestDayRollWithCategories:
    """Day rollover resets all category counters."""

    def test_categories_reset_on_new_day(self, tmp_path):
        state = {
            "date": "2020-01-01",
            "count": 30,
            "categories": {"production": 10, "research": 10, "adhoc": 5, "carryover": 5, "test": 0},
            "warnings_emitted": ["70%", "85%"],
        }
        (tmp_path / "daily_session_count.json").write_text(json.dumps(state))

        from utils.session_counter import get_status

        status = get_status()
        assert status["total"] == 0
        assert all(v == 0 for v in status["categories"].values())
        assert status["warnings_emitted"] == []


class TestBackwardsCompatibility:
    """Old state files without categories are handled gracefully."""

    def test_old_format_upgraded(self, tmp_path):
        import utils.session_counter as mod

        today = mod._today_utc()
        state = {"date": today, "count": 5}
        (tmp_path / "daily_session_count.json").write_text(json.dumps(state))

        from utils.session_counter import get_status

        status = get_status()
        assert status["total"] == 5
        assert "categories" in status
