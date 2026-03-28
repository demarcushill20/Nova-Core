"""Tests for CRUISE mode (v87 P3)."""

import pytest

from utils.cruise_mode import (
    CRUISE_ENTRY_THRESHOLD,
    CRUISE_MAX_CONSECUTIVE,
    CruiseState,
    load_cruise_state,
    save_cruise_state,
    update_cruise_state,
)


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    """Redirect CRUISE state file to tmp."""
    state_file = tmp_path / "cruise_state.json"
    monkeypatch.setattr("utils.cruise_mode.STATE_DIR", tmp_path)
    monkeypatch.setattr("utils.cruise_mode.CRUISE_STATE_FILE", state_file)


class TestCruiseStateRoundTrip:
    def test_load_default_when_no_file(self):
        state = load_cruise_state()
        assert state.active is False
        assert state.consecutive_stable == 0

    def test_save_and_load(self, tmp_path):
        s = CruiseState(active=True, consecutive_stable=5, last_score=85.0)
        save_cruise_state(s)
        loaded = load_cruise_state()
        assert loaded.active is True
        assert loaded.consecutive_stable == 5
        assert loaded.last_score == 85.0

    def test_corrupted_file_returns_default(self, tmp_path):
        from utils.cruise_mode import CRUISE_STATE_FILE

        CRUISE_STATE_FILE.write_text("NOT JSON")
        state = load_cruise_state()
        assert state.active is False


class TestCruiseTransitions:
    def test_enters_cruise_after_threshold(self):
        for _i in range(CRUISE_ENTRY_THRESHOLD):
            state = update_cruise_state(85.0, "GREEN", "monitor")
        assert state.active is True
        assert state.entered_at != ""

    def test_stays_inactive_below_threshold(self):
        for _i in range(CRUISE_ENTRY_THRESHOLD - 1):
            state = update_cruise_state(85.0, "GREEN", "monitor")
        assert state.active is False

    def test_exits_on_non_monitor_decision(self):
        # Enter CRUISE
        for _ in range(CRUISE_ENTRY_THRESHOLD):
            update_cruise_state(85.0, "GREEN", "monitor")
        # Non-MONITOR exits
        state = update_cruise_state(85.0, "GREEN", "repair")
        assert state.active is False
        assert state.consecutive_stable == 0

    def test_exits_on_non_green_alert(self):
        for _ in range(CRUISE_ENTRY_THRESHOLD):
            update_cruise_state(85.0, "GREEN", "monitor")
        state = update_cruise_state(65.0, "YELLOW", "monitor")
        assert state.active is False

    def test_exits_on_score_drift(self):
        for _ in range(CRUISE_ENTRY_THRESHOLD):
            update_cruise_state(85.0, "GREEN", "monitor")
        # Score drift > 5.0
        state = update_cruise_state(78.0, "GREEN", "monitor")
        assert state.active is False

    def test_increments_cruise_cycles(self):
        for _ in range(CRUISE_ENTRY_THRESHOLD):
            update_cruise_state(85.0, "GREEN", "monitor")
        # One more cycle in CRUISE
        state = update_cruise_state(85.0, "GREEN", "monitor")
        assert state.active is True
        assert state.cruise_cycles == 1

    def test_exits_after_max_consecutive(self):
        # Enter CRUISE
        for _ in range(CRUISE_ENTRY_THRESHOLD):
            update_cruise_state(85.0, "GREEN", "monitor")
        # Run max consecutive cycles
        for _ in range(CRUISE_MAX_CONSECUTIVE):
            state = update_cruise_state(85.0, "GREEN", "monitor")
        # Should have exited
        assert state.active is False
        assert state.cruise_cycles == 0

    def test_re_enters_after_exit_and_stable(self):
        # Enter CRUISE
        for _ in range(CRUISE_ENTRY_THRESHOLD):
            update_cruise_state(85.0, "GREEN", "monitor")
        # Exit
        update_cruise_state(85.0, "GREEN", "repair")
        # Re-enter
        for _ in range(CRUISE_ENTRY_THRESHOLD):
            state = update_cruise_state(85.0, "GREEN", "monitor")
        assert state.active is True
