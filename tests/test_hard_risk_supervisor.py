"""Tests for the Hard Risk Supervisor — deterministic, non-AI safety layer.

Covers all 12 veto rules, post-trade tracking, continuous monitoring,
emergency halt, state persistence, and daily reset logic.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from novatrade.risk.hard_risk_supervisor import (
    HardLimits,
    HardRiskSupervisor,
    SupervisorAction,
    SupervisorDecision,
    SupervisorVerdict,
    _pip_usd_value,
    _pip_value,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_state(tmp_path: Path) -> tuple[Path, Path]:
    """Return (state_dir, kill_switch_dir) in a temp directory."""
    state_dir = tmp_path / "supervisor"
    kill_dir = tmp_path / "state"
    return state_dir, kill_dir


@pytest.fixture
def supervisor(tmp_state: tuple[Path, Path]) -> HardRiskSupervisor:
    """A supervisor with default limits initialized at $10,000."""
    state_dir, kill_dir = tmp_state
    s = HardRiskSupervisor(
        limits=HardLimits(),
        state_dir=state_dir,
        kill_switch_dir=kill_dir,
    )
    s.initialize(initial_equity=10_000.0)
    return s


@pytest.fixture
def tight_supervisor(tmp_state: tuple[Path, Path]) -> HardRiskSupervisor:
    """A supervisor with tight limits for edge-case testing."""
    state_dir, kill_dir = tmp_state
    s = HardRiskSupervisor(
        limits=HardLimits(
            equity_floor_usd=9_000.0,
            max_daily_loss_usd=100.0,
            max_total_loss_usd=200.0,
            max_loss_per_trade_usd=50.0,
            max_lot_size=0.5,
            max_concurrent_positions=2,
            max_trades_per_day=5,
            consecutive_loss_limit=3,
            consecutive_loss_cooldown_s=60,
            rapid_loss_count=2,
            rapid_loss_window_s=60,
            max_volume_ratio=3.0,
            min_stop_distance_pips=10.0,
            max_same_symbol_positions=1,
        ),
        state_dir=state_dir,
        kill_switch_dir=kill_dir,
    )
    s.initialize(initial_equity=10_000.0)
    return s


def _make_position(
    symbol: str = "EURUSD",
    side: str = "BUY",
    volume: float = 0.1,
    position_id: str = "pos_1",
) -> dict:
    return {
        "symbol": symbol,
        "side": side,
        "volume": volume,
        "position_id": position_id,
    }


# ---------------------------------------------------------------------------
# HardLimits tests
# ---------------------------------------------------------------------------


class TestHardLimits:
    """HardLimits is frozen — cannot be mutated after creation."""

    def test_immutable(self) -> None:
        limits = HardLimits()
        with pytest.raises(AttributeError):
            limits.equity_floor_usd = 5000.0  # type: ignore[misc]

    def test_defaults_reasonable(self) -> None:
        limits = HardLimits()
        assert limits.equity_floor_usd > 0
        assert limits.max_daily_loss_usd > 0
        assert limits.max_total_loss_usd > limits.max_daily_loss_usd
        assert limits.max_lot_size > 0
        assert limits.max_concurrent_positions >= 1
        assert limits.max_trades_per_day >= 1

    def test_custom_values(self) -> None:
        limits = HardLimits(equity_floor_usd=5000.0, max_lot_size=2.0)
        assert limits.equity_floor_usd == 5000.0
        assert limits.max_lot_size == 2.0


# ---------------------------------------------------------------------------
# Initialization tests
# ---------------------------------------------------------------------------


class TestInitialization:
    def test_initialize_sets_equity(self, supervisor: HardRiskSupervisor) -> None:
        assert supervisor.daily_pnl_usd == 0.0
        assert supervisor.total_pnl_usd == 0.0
        assert supervisor.trades_today == 0
        assert supervisor.consecutive_losses == 0
        assert not supervisor.halted

    def test_initialize_restores_halt(self, tmp_state: tuple[Path, Path]) -> None:
        state_dir, kill_dir = tmp_state
        state_dir.mkdir(parents=True, exist_ok=True)
        state_file = state_dir / "hard_supervisor_state.json"
        state_file.write_text(
            json.dumps(
                {
                    "halted": True,
                    "halt_reason": "test halt",
                    "consecutive_losses": 3,
                    "trades_today": 10,
                }
            )
        )

        s = HardRiskSupervisor(
            state_dir=state_dir,
            kill_switch_dir=kill_dir,
        )
        s.initialize(initial_equity=10_000.0)
        assert s.halted
        assert s.halt_reason == "test halt"
        assert s.consecutive_losses == 3
        assert s.trades_today == 10


# ---------------------------------------------------------------------------
# Rule 0: Halted state — nothing gets through
# ---------------------------------------------------------------------------


class TestRule0Halted:
    def test_halted_veto(self, supervisor: HardRiskSupervisor) -> None:
        supervisor.emergency_halt("test")
        d = supervisor.veto("EURUSD", "BUY", 0.1, 1.1000, 1.0950, 10_000.0)
        assert d.vetoed
        assert d.verdict == SupervisorVerdict.EMERGENCY_HALT
        assert d.rule == "halted"


# ---------------------------------------------------------------------------
# Rule 1: Equity floor
# ---------------------------------------------------------------------------


class TestRule1EquityFloor:
    def test_equity_below_floor(self, supervisor: HardRiskSupervisor) -> None:
        d = supervisor.veto("EURUSD", "BUY", 0.1, 1.1000, 1.0950, 8_000.0)
        assert d.verdict == SupervisorVerdict.EMERGENCY_HALT
        assert d.rule == "equity_floor"
        assert supervisor.halted

    def test_equity_at_floor_passes_floor_check(
        self, tmp_state: tuple[Path, Path]
    ) -> None:
        """At exactly the floor, equity_floor passes but daily loss may trigger.

        Use limits where floor and daily cap are consistent.
        """
        state_dir, kill_dir = tmp_state
        s = HardRiskSupervisor(
            limits=HardLimits(
                equity_floor_usd=9_500.0,
                max_daily_loss_usd=600.0,  # 10000-9500=500 < 600
                max_total_loss_usd=600.0,
            ),
            state_dir=state_dir,
            kill_switch_dir=kill_dir,
        )
        s.initialize(initial_equity=10_000.0)
        d = s.veto("EURUSD", "BUY", 0.1, 1.1000, 1.0950, 9_500.0)
        assert d.verdict == SupervisorVerdict.ALLOW

    def test_equity_above_floor(self, supervisor: HardRiskSupervisor) -> None:
        d = supervisor.veto("EURUSD", "BUY", 0.1, 1.1000, 1.0950, 10_000.0)
        assert d.verdict == SupervisorVerdict.ALLOW


# ---------------------------------------------------------------------------
# Rule 2: Daily loss cap
# ---------------------------------------------------------------------------


class TestRule2DailyLossCap:
    def test_daily_loss_exceeds_cap(self, supervisor: HardRiskSupervisor) -> None:
        # Daily loss = 10000 - 9500 = $500 >= cap $450
        d = supervisor.veto("EURUSD", "BUY", 0.1, 1.1000, 1.0950, 9_500.0)
        assert d.verdict == SupervisorVerdict.EMERGENCY_HALT
        assert d.rule == "daily_loss_cap"
        assert supervisor.halted

    def test_daily_loss_within_cap(self, supervisor: HardRiskSupervisor) -> None:
        # Daily loss = 10000 - 9600 = $400 < cap $450
        d = supervisor.veto("EURUSD", "BUY", 0.1, 1.1000, 1.0950, 9_600.0)
        assert d.verdict == SupervisorVerdict.ALLOW


# ---------------------------------------------------------------------------
# Rule 3: Total loss cap
# ---------------------------------------------------------------------------


class TestRule3TotalLossCap:
    def test_total_loss_exceeds_cap(self, tmp_state: tuple[Path, Path]) -> None:
        state_dir, kill_dir = tmp_state
        # Set daily cap high enough that total cap triggers first
        s = HardRiskSupervisor(
            limits=HardLimits(
                equity_floor_usd=5_000.0,
                max_daily_loss_usd=500.0,
                max_total_loss_usd=200.0,  # Total cap is tighter
            ),
            state_dir=state_dir,
            kill_switch_dir=kill_dir,
        )
        s.initialize(initial_equity=10_000.0)
        # Total loss = 10000 - 9700 = $300 >= cap $200 (daily loss $300 < $500)
        d = s.veto("EURUSD", "BUY", 0.1, 1.1000, 1.0900, 9_700.0)
        assert d.verdict == SupervisorVerdict.EMERGENCY_HALT
        assert d.rule == "total_loss_cap"


# ---------------------------------------------------------------------------
# Rule 4: Max lot size
# ---------------------------------------------------------------------------


class TestRule4MaxLotSize:
    def test_volume_exceeds_max(self, supervisor: HardRiskSupervisor) -> None:
        d = supervisor.veto("EURUSD", "BUY", 1.5, 1.1000, 1.0950, 10_000.0)
        assert d.verdict == SupervisorVerdict.VETO
        assert d.rule == "max_lot_size"

    def test_volume_at_max(self, supervisor: HardRiskSupervisor) -> None:
        # 1.0 lot × 10 pip SL × $10/pip = $100 < $150 limit
        d = supervisor.veto("EURUSD", "BUY", 1.0, 1.10000, 1.09900, 10_000.0)
        assert d.verdict == SupervisorVerdict.ALLOW

    def test_volume_below_max(self, supervisor: HardRiskSupervisor) -> None:
        d = supervisor.veto("EURUSD", "BUY", 0.5, 1.10000, 1.09900, 10_000.0)
        assert d.verdict == SupervisorVerdict.ALLOW


# ---------------------------------------------------------------------------
# Rule 5: Max concurrent positions
# ---------------------------------------------------------------------------


class TestRule5MaxConcurrentPositions:
    def test_too_many_positions(self, supervisor: HardRiskSupervisor) -> None:
        positions = [
            _make_position(position_id="p1"),
            _make_position(position_id="p2"),
            _make_position(position_id="p3"),
        ]
        d = supervisor.veto(
            "EURUSD", "BUY", 0.1, 1.1000, 1.0950, 10_000.0,
            open_positions=positions,
        )
        assert d.verdict == SupervisorVerdict.VETO
        assert d.rule == "max_concurrent_positions"

    def test_within_position_limit(self, supervisor: HardRiskSupervisor) -> None:
        positions = [_make_position(position_id="p1"), _make_position(position_id="p2")]
        d = supervisor.veto(
            "EURUSD", "BUY", 0.1, 1.1000, 1.0950, 10_000.0,
            open_positions=positions,
        )
        # Will pass positions check but may be vetoed by symbol concentration
        assert d.rule != "max_concurrent_positions"


# ---------------------------------------------------------------------------
# Rule 6: Currency concentration
# ---------------------------------------------------------------------------


class TestRule6CurrencyConcentration:
    def test_same_symbol_blocked(self, tight_supervisor: HardRiskSupervisor) -> None:
        positions = [_make_position(symbol="EURUSD", position_id="p1")]
        d = tight_supervisor.veto(
            "EURUSD", "BUY", 0.1, 1.1000, 1.0900, 10_000.0,
            open_positions=positions,
        )
        assert d.verdict == SupervisorVerdict.VETO
        assert d.rule == "symbol_concentration"

    def test_different_symbol_allowed(self, tight_supervisor: HardRiskSupervisor) -> None:
        positions = [_make_position(symbol="GBPUSD", position_id="p1")]
        d = tight_supervisor.veto(
            "EURUSD", "BUY", 0.1, 1.1000, 1.0900, 10_000.0,
            open_positions=positions,
        )
        assert d.rule != "symbol_concentration"


# ---------------------------------------------------------------------------
# Rule 7: Daily trade count
# ---------------------------------------------------------------------------


class TestRule7DailyTradeCount:
    def test_exceeds_daily_cap(self, tight_supervisor: HardRiskSupervisor) -> None:
        # Simulate 5 trades opened today
        for _ in range(5):
            tight_supervisor.on_trade_opened(0.1)

        d = tight_supervisor.veto(
            "EURUSD", "BUY", 0.1, 1.1000, 1.0900, 10_000.0,
        )
        assert d.verdict == SupervisorVerdict.VETO
        assert d.rule == "daily_trade_cap"

    def test_within_daily_cap(self, tight_supervisor: HardRiskSupervisor) -> None:
        for _ in range(3):
            tight_supervisor.on_trade_opened(0.1)

        d = tight_supervisor.veto(
            "EURUSD", "BUY", 0.1, 1.1000, 1.0900, 10_000.0,
        )
        assert d.rule != "daily_trade_cap"


# ---------------------------------------------------------------------------
# Rule 8: Consecutive loss cooldown
# ---------------------------------------------------------------------------


class TestRule8ConsecutiveLossCooldown:
    def test_cooldown_activates(self, tmp_state: tuple[Path, Path]) -> None:
        state_dir, kill_dir = tmp_state
        # Use limits where rapid_loss_count > consecutive_loss_limit
        # so cooldown triggers before rapid halt
        s = HardRiskSupervisor(
            limits=HardLimits(
                equity_floor_usd=5_000.0,
                max_daily_loss_usd=1000.0,
                max_total_loss_usd=2000.0,
                consecutive_loss_limit=3,
                consecutive_loss_cooldown_s=60,
                rapid_loss_count=10,  # High enough to not interfere
                rapid_loss_window_s=60,
            ),
            state_dir=state_dir,
            kill_switch_dir=kill_dir,
        )
        s.initialize(initial_equity=10_000.0)

        for _ in range(3):
            s.on_trade_closed(-10.0)

        d = s.veto("EURUSD", "BUY", 0.1, 1.1000, 1.0900, 10_000.0)
        assert d.verdict == SupervisorVerdict.VETO
        assert d.rule == "consecutive_loss_cooldown"

    def test_win_resets_streak(self, tight_supervisor: HardRiskSupervisor) -> None:
        # Avoid rapid loss by spacing timestamps
        tight_supervisor.on_trade_closed(-10.0)
        tight_supervisor._recent_losses = [time.time() - 120]  # Push outside window
        tight_supervisor.on_trade_closed(20.0)  # Win resets counter
        assert tight_supervisor.consecutive_losses == 0

    def test_cooldown_expires(self, tmp_state: tuple[Path, Path]) -> None:
        state_dir, kill_dir = tmp_state
        s = HardRiskSupervisor(
            limits=HardLimits(
                equity_floor_usd=5_000.0,
                max_daily_loss_usd=1000.0,
                max_total_loss_usd=2000.0,
                consecutive_loss_limit=3,
                consecutive_loss_cooldown_s=60,
                rapid_loss_count=10,
                rapid_loss_window_s=60,
            ),
            state_dir=state_dir,
            kill_switch_dir=kill_dir,
        )
        s.initialize(initial_equity=10_000.0)

        for _ in range(3):
            s.on_trade_closed(-10.0)

        # Fast-forward past cooldown
        s._last_loss_cooldown_until = time.time() - 1

        d = s.veto("EURUSD", "BUY", 0.1, 1.1000, 1.0900, 10_000.0)
        assert d.rule != "consecutive_loss_cooldown"


# ---------------------------------------------------------------------------
# Rule 9: Rapid loss detection
# ---------------------------------------------------------------------------


class TestRule9RapidLossDetection:
    def test_rapid_losses_trigger_halt(self, tight_supervisor: HardRiskSupervisor) -> None:
        # 2 losses in 60s window triggers halt (tight limits)
        tight_supervisor.on_trade_closed(-10.0)
        tight_supervisor.on_trade_closed(-10.0)
        assert tight_supervisor.halted

    def test_spaced_losses_ok(self, tight_supervisor: HardRiskSupervisor) -> None:
        tight_supervisor.on_trade_closed(-10.0)
        # Push first loss outside the window
        tight_supervisor._recent_losses = [time.time() - 120]
        tight_supervisor.on_trade_closed(-10.0)
        # After pruning, only 1 recent loss in window — no halt
        # (depends on rapid_loss_count=2)
        # The second on_trade_closed adds a new timestamp, so we have 2 in window
        # but the first was moved outside. Let's verify:
        tight_supervisor._prune_recent_losses()
        # The manually set timestamp is outside window; the on_trade_closed adds one
        assert len(tight_supervisor._recent_losses) <= 1


# ---------------------------------------------------------------------------
# Rule 10: Min stop distance
# ---------------------------------------------------------------------------


class TestRule10MinStopDistance:
    def test_stop_too_close(self, tight_supervisor: HardRiskSupervisor) -> None:
        # 5 pips distance but limit is 10
        d = tight_supervisor.veto(
            "EURUSD", "BUY", 0.1, 1.10000, 1.09950, 10_000.0,
        )
        assert d.verdict == SupervisorVerdict.VETO
        assert d.rule == "min_stop_distance"

    def test_stop_far_enough(self, tight_supervisor: HardRiskSupervisor) -> None:
        # 50 pips distance
        d = tight_supervisor.veto(
            "EURUSD", "BUY", 0.1, 1.10000, 1.09500, 10_000.0,
        )
        assert d.rule != "min_stop_distance"

    def test_no_stop_loss_passes(self, tight_supervisor: HardRiskSupervisor) -> None:
        d = tight_supervisor.veto(
            "EURUSD", "BUY", 0.1, 1.10000, None, 10_000.0,
        )
        assert d.rule != "min_stop_distance"


# ---------------------------------------------------------------------------
# Rule 11: Per-trade risk
# ---------------------------------------------------------------------------


class TestRule11PerTradeRisk:
    def test_risk_exceeds_limit(self, tight_supervisor: HardRiskSupervisor) -> None:
        # 0.5 lot × 100 pip SL × $10/pip = $500 > $50 limit
        d = tight_supervisor.veto(
            "EURUSD", "BUY", 0.5, 1.10000, 1.09000, 10_000.0,
        )
        assert d.verdict == SupervisorVerdict.VETO
        assert d.rule == "max_loss_per_trade"

    def test_risk_within_limit(self, tight_supervisor: HardRiskSupervisor) -> None:
        # 0.01 lot × 20 pip SL × $10/pip = $2 < $50 limit
        d = tight_supervisor.veto(
            "EURUSD", "BUY", 0.01, 1.10000, 1.09800, 10_000.0,
        )
        assert d.rule != "max_loss_per_trade"


# ---------------------------------------------------------------------------
# Rule 12: Fat finger volume check
# ---------------------------------------------------------------------------


class TestRule12FatFingerVolume:
    def test_fat_finger_detected(self, supervisor: HardRiskSupervisor) -> None:
        # Build history of small trades (use default supervisor with 30 trade cap)
        for _ in range(5):
            supervisor.on_trade_opened(0.01)

        # Now try a much larger volume (0.1 > 5.0 × 0.01)
        d = supervisor.veto(
            "EURUSD", "BUY", 0.1, 1.10000, 1.09900, 10_000.0,
        )
        assert d.verdict == SupervisorVerdict.VETO
        assert d.rule == "fat_finger_volume"

    def test_normal_volume_ok(self, supervisor: HardRiskSupervisor) -> None:
        for _ in range(5):
            supervisor.on_trade_opened(0.1)

        d = supervisor.veto(
            "EURUSD", "BUY", 0.1, 1.10000, 1.09900, 10_000.0,
        )
        assert d.rule != "fat_finger_volume"

    def test_insufficient_history_skips(self, supervisor: HardRiskSupervisor) -> None:
        # Less than 3 trades — skip fat finger check
        supervisor.on_trade_opened(0.01)
        d = supervisor.veto(
            "EURUSD", "BUY", 0.5, 1.10000, 1.09950, 10_000.0,
        )
        # May be vetoed by other rules, but not fat_finger_volume
        assert d.rule != "fat_finger_volume"


# ---------------------------------------------------------------------------
# Fail-closed behavior
# ---------------------------------------------------------------------------


class TestFailClosed:
    def test_error_during_veto_denies(self, supervisor: HardRiskSupervisor) -> None:
        """Any exception in veto evaluation → VETO (fail-closed)."""
        with patch.object(
            supervisor, "_evaluate_veto", side_effect=RuntimeError("boom")
        ):
            d = supervisor.veto("EURUSD", "BUY", 0.1, 1.1, 1.09, 10_000.0)
            assert d.vetoed
            assert d.rule == "supervisor_error"


# ---------------------------------------------------------------------------
# Post-trade tracking
# ---------------------------------------------------------------------------


class TestPostTradeTracking:
    def test_loss_increments_streak(self, supervisor: HardRiskSupervisor) -> None:
        supervisor.on_trade_closed(-50.0)
        assert supervisor.consecutive_losses == 1
        supervisor.on_trade_closed(-30.0)
        assert supervisor.consecutive_losses == 2

    def test_win_resets_streak(self, supervisor: HardRiskSupervisor) -> None:
        supervisor.on_trade_closed(-50.0)
        supervisor.on_trade_closed(-30.0)
        supervisor.on_trade_closed(100.0)
        assert supervisor.consecutive_losses == 0

    def test_pnl_tracking(self, supervisor: HardRiskSupervisor) -> None:
        supervisor.on_trade_closed(-50.0)
        supervisor.on_trade_closed(100.0)
        assert supervisor.daily_pnl_usd == 50.0
        assert supervisor.total_pnl_usd == 50.0

    def test_trade_count(self, supervisor: HardRiskSupervisor) -> None:
        supervisor.on_trade_opened(0.1)
        supervisor.on_trade_opened(0.2)
        assert supervisor.trades_today == 2


# ---------------------------------------------------------------------------
# Continuous monitoring (check_account)
# ---------------------------------------------------------------------------


class TestCheckAccount:
    def test_equity_floor_triggers_close_all(
        self, supervisor: HardRiskSupervisor
    ) -> None:
        positions = [_make_position(position_id="p1")]
        actions = supervisor.check_account(8_000.0, positions)
        assert len(actions) == 2
        assert actions[0].action == "CLOSE_ALL"
        assert actions[0].position_ids == ["p1"]
        assert actions[1].action == "HALT"

    def test_daily_loss_triggers_halt(self, supervisor: HardRiskSupervisor) -> None:
        actions = supervisor.check_account(9_500.0, [])
        assert any(a.action == "HALT" for a in actions)

    def test_healthy_account_no_actions(self, supervisor: HardRiskSupervisor) -> None:
        actions = supervisor.check_account(10_100.0, [])
        assert actions == []


# ---------------------------------------------------------------------------
# Emergency halt
# ---------------------------------------------------------------------------


class TestEmergencyHalt:
    def test_halt_sets_state(self, supervisor: HardRiskSupervisor) -> None:
        supervisor.emergency_halt("test reason")
        assert supervisor.halted
        assert supervisor.halt_reason == "test reason"

    def test_halt_writes_kill_switch(
        self, supervisor: HardRiskSupervisor, tmp_state: tuple[Path, Path]
    ) -> None:
        _, kill_dir = tmp_state
        supervisor.emergency_halt("test")
        kill_file = kill_dir / "nova-kill"
        assert kill_file.exists()
        assert kill_file.read_text() == "CONFIRM"

    def test_halt_persists_state(
        self, supervisor: HardRiskSupervisor, tmp_state: tuple[Path, Path]
    ) -> None:
        state_dir, _ = tmp_state
        supervisor.emergency_halt("persistence test")
        state_file = state_dir / "hard_supervisor_state.json"
        assert state_file.exists()
        data = json.loads(state_file.read_text())
        assert data["halted"] is True
        assert data["halt_reason"] == "persistence test"

    def test_halt_is_idempotent(self, supervisor: HardRiskSupervisor) -> None:
        supervisor.emergency_halt("first")
        supervisor.emergency_halt("second")
        assert supervisor.halt_reason == "first"  # Second call is no-op


# ---------------------------------------------------------------------------
# Resume
# ---------------------------------------------------------------------------


class TestResume:
    def test_resume_clears_halt(self, supervisor: HardRiskSupervisor) -> None:
        supervisor.emergency_halt("test")
        assert supervisor.halted
        supervisor.resume()
        assert not supervisor.halted
        assert supervisor.halt_reason == ""

    def test_resume_resets_counters(self, supervisor: HardRiskSupervisor) -> None:
        supervisor.on_trade_closed(-10.0)
        supervisor.on_trade_closed(-10.0)
        supervisor.emergency_halt("test")
        supervisor.resume()
        assert supervisor.consecutive_losses == 0

    def test_resume_when_not_halted(self, supervisor: HardRiskSupervisor) -> None:
        supervisor.resume()  # No-op, should not error
        assert not supervisor.halted


# ---------------------------------------------------------------------------
# Daily reset
# ---------------------------------------------------------------------------


class TestDailyReset:
    def test_reset_clears_daily_counters(self, supervisor: HardRiskSupervisor) -> None:
        supervisor.on_trade_opened(0.1)
        supervisor.on_trade_opened(0.1)
        supervisor.on_trade_closed(-10.0)
        supervisor.reset_daily(9_990.0)
        assert supervisor.trades_today == 0
        assert supervisor.daily_pnl_usd == 0.0

    def test_reset_does_not_clear_halt(self, supervisor: HardRiskSupervisor) -> None:
        supervisor.emergency_halt("test")
        supervisor.reset_daily(10_000.0)
        assert supervisor.halted

    def test_auto_reset_on_date_change(self, supervisor: HardRiskSupervisor) -> None:
        supervisor.on_trade_opened(0.1)
        assert supervisor.trades_today == 1
        # Force the last reset date to yesterday
        supervisor._last_daily_reset = "2020-01-01"
        supervisor.check_account(10_000.0)
        assert supervisor.trades_today == 0


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------


class TestSnapshot:
    def test_snapshot_fields(self, supervisor: HardRiskSupervisor) -> None:
        snap = supervisor.snapshot()
        assert "halted" in snap
        assert "initial_equity" in snap
        assert "daily_pnl_usd" in snap
        assert "total_pnl_usd" in snap
        assert "trades_today" in snap
        assert "consecutive_losses" in snap
        assert "limits" in snap
        assert "timestamp" in snap

    def test_snapshot_reflects_state(self, supervisor: HardRiskSupervisor) -> None:
        supervisor.on_trade_opened(0.1)
        supervisor.on_trade_closed(-25.0)
        snap = supervisor.snapshot()
        assert snap["trades_today"] == 1
        assert snap["daily_pnl_usd"] == -25.0
        assert snap["consecutive_losses"] == 1


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------


class TestStatePersistence:
    def test_state_roundtrip(self, tmp_state: tuple[Path, Path]) -> None:
        state_dir, kill_dir = tmp_state

        # First instance: create state
        s1 = HardRiskSupervisor(state_dir=state_dir, kill_switch_dir=kill_dir)
        s1.initialize(initial_equity=10_000.0)
        s1.on_trade_opened(0.1)
        s1.on_trade_closed(-50.0)
        s1.on_trade_closed(-30.0)

        # Second instance: restore state
        s2 = HardRiskSupervisor(state_dir=state_dir, kill_switch_dir=kill_dir)
        s2.initialize(initial_equity=10_000.0)
        assert s2.consecutive_losses == 2
        assert s2.trades_today == 1

    def test_halt_survives_restart(self, tmp_state: tuple[Path, Path]) -> None:
        state_dir, kill_dir = tmp_state

        s1 = HardRiskSupervisor(state_dir=state_dir, kill_switch_dir=kill_dir)
        s1.initialize(initial_equity=10_000.0)
        s1.emergency_halt("crash test")

        s2 = HardRiskSupervisor(state_dir=state_dir, kill_switch_dir=kill_dir)
        s2.initialize(initial_equity=10_000.0)
        assert s2.halted
        assert s2.halt_reason == "crash test"

    def test_missing_state_file_ok(self, tmp_state: tuple[Path, Path]) -> None:
        state_dir, kill_dir = tmp_state
        s = HardRiskSupervisor(state_dir=state_dir, kill_switch_dir=kill_dir)
        s.initialize(initial_equity=10_000.0)  # Should not error
        assert not s.halted

    def test_corrupt_state_file_ok(self, tmp_state: tuple[Path, Path]) -> None:
        state_dir, kill_dir = tmp_state
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "hard_supervisor_state.json").write_text("not json")
        s = HardRiskSupervisor(state_dir=state_dir, kill_switch_dir=kill_dir)
        s.initialize(initial_equity=10_000.0)  # Should not error
        assert not s.halted


# ---------------------------------------------------------------------------
# Pip value helpers
# ---------------------------------------------------------------------------


class TestPipHelpers:
    def test_standard_forex(self) -> None:
        assert _pip_value("EURUSD") == 0.0001
        assert _pip_value("GBPUSD") == 0.0001

    def test_jpy_pair(self) -> None:
        assert _pip_value("USDJPY") == 0.01
        assert _pip_value("EURJPY") == 0.01

    def test_gold(self) -> None:
        assert _pip_value("XAUUSD") == 0.1

    def test_pip_usd_standard(self) -> None:
        assert _pip_usd_value("EURUSD") == 10.0

    def test_pip_usd_jpy(self) -> None:
        assert _pip_usd_value("USDJPY") == 7.0

    def test_pip_usd_gold(self) -> None:
        assert _pip_usd_value("XAUUSD") == 10.0


# ---------------------------------------------------------------------------
# Integration: full lifecycle
# ---------------------------------------------------------------------------


class TestFullLifecycle:
    def test_normal_trading_session(self, supervisor: HardRiskSupervisor) -> None:
        """Simulate a normal session: trade, win, trade, loss, continue."""
        # First trade
        d = supervisor.veto("EURUSD", "BUY", 0.1, 1.1000, 1.0950, 10_000.0)
        assert d.verdict == SupervisorVerdict.ALLOW
        supervisor.on_trade_opened(0.1)
        supervisor.on_trade_closed(50.0)

        # Second trade
        d = supervisor.veto("EURUSD", "SELL", 0.1, 1.1000, 1.1050, 10_050.0)
        assert d.verdict == SupervisorVerdict.ALLOW
        supervisor.on_trade_opened(0.1)
        supervisor.on_trade_closed(-30.0)

        assert supervisor.trades_today == 2
        assert supervisor.total_pnl_usd == 20.0
        assert supervisor.consecutive_losses == 1
        assert not supervisor.halted

    def test_losing_session_triggers_halt(
        self, tight_supervisor: HardRiskSupervisor
    ) -> None:
        """Rapid losses should trigger emergency halt."""
        tight_supervisor.on_trade_closed(-10.0)
        tight_supervisor.on_trade_closed(-10.0)
        # With rapid_loss_count=2 and rapid_loss_window_s=60, this halts
        assert tight_supervisor.halted

    def test_resume_and_continue(self, tight_supervisor: HardRiskSupervisor) -> None:
        """After halt + resume, trading can continue."""
        tight_supervisor.emergency_halt("test")
        assert tight_supervisor.halted

        tight_supervisor.resume()
        # Use tiny risk: 0.01 lot × 20 pip SL × $10/pip = $2 < $50 limit
        d = tight_supervisor.veto(
            "EURUSD", "BUY", 0.01, 1.10000, 1.09800, 10_000.0,
        )
        assert not d.vetoed


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_zero_equity(self, supervisor: HardRiskSupervisor) -> None:
        d = supervisor.veto("EURUSD", "BUY", 0.1, 1.1, 1.09, 0.0)
        assert d.verdict == SupervisorVerdict.EMERGENCY_HALT

    def test_negative_equity(self, supervisor: HardRiskSupervisor) -> None:
        d = supervisor.veto("EURUSD", "BUY", 0.1, 1.1, 1.09, -500.0)
        assert d.verdict == SupervisorVerdict.EMERGENCY_HALT

    def test_no_positions_passed(self, supervisor: HardRiskSupervisor) -> None:
        d = supervisor.veto("EURUSD", "BUY", 0.1, 1.1, 1.09, 10_000.0)
        assert d.verdict == SupervisorVerdict.ALLOW

    def test_none_positions(self, supervisor: HardRiskSupervisor) -> None:
        d = supervisor.veto(
            "EURUSD", "BUY", 0.1, 1.1, 1.09, 10_000.0, open_positions=None
        )
        assert d.verdict == SupervisorVerdict.ALLOW

    def test_ftmo_symbol_suffix(self, supervisor: HardRiskSupervisor) -> None:
        """Symbols with broker suffixes should work."""
        d = supervisor.veto("EURUSD.ftmo", "BUY", 0.1, 1.1, 1.09, 10_000.0)
        assert d.verdict == SupervisorVerdict.ALLOW

    def test_sell_side(self, supervisor: HardRiskSupervisor) -> None:
        d = supervisor.veto("EURUSD", "SELL", 0.1, 1.1, 1.11, 10_000.0)
        assert d.verdict == SupervisorVerdict.ALLOW

    def test_jpy_pair_stop_distance(self, tight_supervisor: HardRiskSupervisor) -> None:
        """JPY pairs use 0.01 pip value — stop distance should be correct."""
        # 0.500 yen distance = 50 pips (ok, > 10 pip min)
        d = tight_supervisor.veto(
            "USDJPY", "BUY", 0.01, 150.000, 149.500, 10_000.0,
        )
        assert d.rule != "min_stop_distance"
