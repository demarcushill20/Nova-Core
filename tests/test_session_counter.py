"""Tests for utils/session_counter.py — daily session circuit breaker."""

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
