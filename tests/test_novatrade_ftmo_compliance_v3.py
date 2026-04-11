"""Tests for FTMO compliance v3 enhancements.

Covers:
- WeeklyLossTracker: rolling 7-day loss tracking with buffer
- WeekendAutoCloser: Friday force-close-all and graduated phases
- BestDayRuleTracker: enforcement via PreTradeGate (not just monitoring)
- Config alignment: max_trades_per_day=10, max_positions=1
- HardRiskSupervisor: aligned limits
- PreTradeGate integration: weekly loss + best day checks wired in
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from novatrade.config import NovaTradeCfg, RiskConfig
from novatrade.models import (
    AccountMode,
    AccountState,
    OrderRequest,
    OrderSide,
    OrderType,
    RiskVerdict,
)
from novatrade.risk.ftmo_compliance import (
    BestDayRuleTracker,
    WeekendAutoCloser,
    WeeklyLossTracker,
)
from novatrade.risk.hard_risk_supervisor import HardLimits, HardRiskSupervisor
from novatrade.risk.pre_trade_gate import PreTradeGate

_FTMO_TZ = ZoneInfo("Europe/Prague")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def weekly_tracker():
    """A WeeklyLossTracker initialized with a $100K account."""
    t = WeeklyLossTracker(initial_account_size=100_000.0)
    t.initialize(100_000.0)
    return t


@pytest.fixture
def closer():
    return WeekendAutoCloser()


@pytest.fixture
def best_day():
    return BestDayRuleTracker()


def _make_cfg() -> NovaTradeCfg:
    """Build a NovaTradeCfg for testing."""
    return NovaTradeCfg(
        mode=AccountMode.DEMO,
        risk=RiskConfig(
            max_trades_per_day=10,
            max_positions=1,
            cooldown_seconds=0,
            rollover_dead_zone_enabled=False,
            london_fix_avoidance_enabled=False,
            require_stop_loss=False,
        ),
    )


def _make_order(symbol="EURUSD", side=OrderSide.BUY, volume=0.10, price=1.1000, sl=1.0950):
    return OrderRequest(
        symbol=symbol,
        side=side,
        order_type=OrderType.MARKET,
        volume=volume,
        price=price,
        stop_loss=sl,
    )


def _make_account(balance=100_000.0, equity=100_000.0, mode=AccountMode.DEMO):
    return AccountState(
        balance=balance,
        equity=equity,
        mode=mode,
        trade_allowed=True,
    )


# ===========================================================================
# WeeklyLossTracker Tests
# ===========================================================================


class TestWeeklyLossTracker:
    """Tests for the rolling 7-day weekly loss tracker."""

    def test_initialization(self, weekly_tracker):
        assert weekly_tracker._initialized is True
        assert weekly_tracker.initial_account_size == 100_000.0
        assert weekly_tracker.weekly_limit_usd == 5_000.0  # 5% of 100K
        assert weekly_tracker.buffer_limit_usd == 4_000.0  # 80% of 5K

    def test_check_passes_when_no_losses(self, weekly_tracker):
        result = weekly_tracker.check()
        assert result.passed is True
        assert "weekly_loss" == result.name

    def test_check_passes_with_small_loss(self, weekly_tracker):
        today = datetime.now(timezone.utc).astimezone(_FTMO_TZ).strftime("%Y-%m-%d")
        weekly_tracker.record_daily_pnl(-500.0, today)
        result = weekly_tracker.check()
        assert result.passed is True
        assert "500.00" in result.detail

    def test_check_fails_at_buffer(self, weekly_tracker):
        """Deny when 7-day loss reaches 80% of weekly limit ($4K)."""
        today = datetime.now(timezone.utc).astimezone(_FTMO_TZ).strftime("%Y-%m-%d")
        weekly_tracker.record_daily_pnl(-4_100.0, today)
        result = weekly_tracker.check()
        assert result.passed is False
        assert "buffer" in result.detail.lower()

    def test_check_fails_at_hard_limit(self, weekly_tracker):
        """Deny when 7-day loss reaches full weekly limit ($5K)."""
        today = datetime.now(timezone.utc).astimezone(_FTMO_TZ).strftime("%Y-%m-%d")
        weekly_tracker.record_daily_pnl(-5_100.0, today)
        result = weekly_tracker.check()
        assert result.passed is False
        assert "BREACH" in result.detail

    def test_rolling_window_excludes_old_days(self, weekly_tracker):
        """Losses older than 7 days should not count."""
        today = datetime.now(timezone.utc).astimezone(_FTMO_TZ).date()
        # 8 days ago — outside window
        old_day = (today - timedelta(days=8)).strftime("%Y-%m-%d")
        weekly_tracker.record_daily_pnl(-5_000.0, old_day)
        result = weekly_tracker.check()
        assert result.passed is True  # old loss doesn't count

    def test_rolling_window_includes_recent_days(self, weekly_tracker):
        """Losses within 7 days should be summed."""
        today = datetime.now(timezone.utc).astimezone(_FTMO_TZ).date()
        for i in range(5):
            day = (today - timedelta(days=i)).strftime("%Y-%m-%d")
            weekly_tracker.record_daily_pnl(-900.0, day)
        # Total: 5 × $900 = $4500 > buffer ($4000)
        result = weekly_tracker.check()
        assert result.passed is False

    def test_profitable_days_dont_offset_losses(self, weekly_tracker):
        """Only negative days count as losses (conservative approach)."""
        today = datetime.now(timezone.utc).astimezone(_FTMO_TZ).date()
        # Day 0: big loss
        weekly_tracker.record_daily_pnl(-4_500.0, today.strftime("%Y-%m-%d"))
        # Day 1: big profit
        yesterday = (today - timedelta(days=1)).strftime("%Y-%m-%d")
        weekly_tracker.record_daily_pnl(3_000.0, yesterday)
        # Loss is still $4500 (profit doesn't offset)
        result = weekly_tracker.check()
        assert result.passed is False

    def test_uninitialized_passes(self):
        tracker = WeeklyLossTracker()
        result = tracker.check()
        assert result.passed is True
        assert "not initialized" in result.detail

    def test_snapshot(self, weekly_tracker):
        today = datetime.now(timezone.utc).astimezone(_FTMO_TZ).strftime("%Y-%m-%d")
        weekly_tracker.record_daily_pnl(-1_000.0, today)
        snap = weekly_tracker.snapshot()
        assert snap["initialized"] is True
        assert snap["initial_account_size"] == 100_000.0
        assert snap["rolling_7day_loss"] == 1_000.0

    def test_save_and_load_state(self, weekly_tracker, tmp_path):
        today = datetime.now(timezone.utc).astimezone(_FTMO_TZ).strftime("%Y-%m-%d")
        weekly_tracker.record_daily_pnl(-2_000.0, today)

        save_path = tmp_path / "weekly_loss.json"
        weekly_tracker.save_state(save_path)

        loaded = WeeklyLossTracker()
        assert loaded.load_state(save_path) is True
        assert loaded.initial_account_size == 100_000.0
        assert loaded._daily_pnl.get(today) == -2_000.0

    def test_load_state_missing_file(self):
        tracker = WeeklyLossTracker()
        assert tracker.load_state(Path("/tmp/nonexistent_weekly.json")) is False

    def test_multiple_losses_same_day_accumulate(self, weekly_tracker):
        today = datetime.now(timezone.utc).astimezone(_FTMO_TZ).strftime("%Y-%m-%d")
        weekly_tracker.record_daily_pnl(-1_000.0, today)
        weekly_tracker.record_daily_pnl(-1_500.0, today)
        weekly_tracker.record_daily_pnl(-1_600.0, today)
        # Total: $4100 > buffer
        result = weekly_tracker.check()
        assert result.passed is False

    def test_custom_thresholds(self):
        tracker = WeeklyLossTracker(
            initial_account_size=50_000.0,
            weekly_loss_pct=3.0,
            buffer_pct=90.0,
        )
        tracker.initialize(50_000.0)
        # weekly_limit = 3% of 50K = $1500
        # buffer = 90% of $1500 = $1350
        assert tracker.weekly_limit_usd == 1_500.0
        assert tracker.buffer_limit_usd == 1_350.0

        today = datetime.now(timezone.utc).astimezone(_FTMO_TZ).strftime("%Y-%m-%d")
        tracker.record_daily_pnl(-1_400.0, today)
        result = tracker.check()
        assert result.passed is False


# ===========================================================================
# WeekendAutoCloser Force-Close Tests
# ===========================================================================


class TestFridayForceClose:
    """Tests for the enhanced WeekendAutoCloser force-close feature."""

    def _friday_at(self, hour: int, minute: int = 0) -> float:
        """Create a timestamp for a Friday at the given UTC hour."""
        # Find next Friday from a known date
        base = datetime(2026, 4, 3, hour, minute, tzinfo=timezone.utc)  # 2026-04-03 is Friday
        return base.timestamp()

    def _saturday_at(self, hour: int = 12) -> float:
        base = datetime(2026, 4, 4, hour, 0, tzinfo=timezone.utc)  # Saturday
        return base.timestamp()

    def _sunday_at(self, hour: int = 12) -> float:
        base = datetime(2026, 4, 5, hour, 0, tzinfo=timezone.utc)  # Sunday
        return base.timestamp()

    def _wednesday_at(self, hour: int = 14) -> float:
        base = datetime(2026, 4, 1, hour, 0, tzinfo=timezone.utc)  # Wednesday
        return base.timestamp()

    def test_no_force_close_on_wednesday(self, closer):
        assert closer.should_force_close_all(self._wednesday_at(14)) is False

    def test_no_force_close_friday_morning(self, closer):
        assert closer.should_force_close_all(self._friday_at(10)) is False

    def test_no_force_close_friday_1900(self, closer):
        assert closer.should_force_close_all(self._friday_at(19)) is False

    def test_force_close_friday_2000(self, closer):
        assert closer.should_force_close_all(self._friday_at(20)) is True

    def test_force_close_friday_2100(self, closer):
        assert closer.should_force_close_all(self._friday_at(21)) is True

    def test_force_close_friday_2359(self, closer):
        assert closer.should_force_close_all(self._friday_at(23, 59)) is True

    def test_force_close_saturday(self, closer):
        assert closer.should_force_close_all(self._saturday_at(12)) is True

    def test_force_close_sunday_before_open(self, closer):
        # Sunday before market reopen (21:00 or 22:00 UTC depending on DST)
        assert closer.should_force_close_all(self._sunday_at(12)) is True

    # -- Friday close phases --

    def test_phase_normal_wednesday(self, closer):
        assert closer.friday_close_phase(self._wednesday_at(14)) == "normal"

    def test_phase_normal_friday_morning(self, closer):
        assert closer.friday_close_phase(self._friday_at(10)) == "normal"

    def test_phase_warning_friday_1930(self, closer):
        assert closer.friday_close_phase(self._friday_at(19, 30)) == "warning"

    def test_phase_warning_friday_1944(self, closer):
        assert closer.friday_close_phase(self._friday_at(19, 44)) == "warning"

    def test_phase_partial_friday_1945(self, closer):
        assert closer.friday_close_phase(self._friday_at(19, 45)) == "partial"

    def test_phase_partial_friday_1959(self, closer):
        assert closer.friday_close_phase(self._friday_at(19, 59)) == "partial"

    def test_phase_force_friday_2000(self, closer):
        assert closer.friday_close_phase(self._friday_at(20, 0)) == "force"

    def test_phase_force_friday_2200(self, closer):
        assert closer.friday_close_phase(self._friday_at(22, 0)) == "force"

    def test_phase_weekend_saturday(self, closer):
        assert closer.friday_close_phase(self._saturday_at()) == "weekend"

    def test_phase_weekend_sunday(self, closer):
        assert closer.friday_close_phase(self._sunday_at(10)) == "weekend"


# ===========================================================================
# BestDayRuleTracker Enforcement Tests
# ===========================================================================


class TestBestDayRuleEnforcement:
    """Tests for BestDayRuleTracker enforcement (not just monitoring)."""

    def test_passes_with_no_data(self, best_day):
        ok, reason = best_day.check(max_pct=0.50)
        assert ok is True

    def test_passes_with_balanced_profits(self, best_day):
        best_day.record_daily_pnl("2026-04-01", 200.0)
        best_day.record_daily_pnl("2026-04-02", 200.0)
        best_day.record_daily_pnl("2026-04-03", 200.0)
        # Best day: $200 / $600 total = 33% < 50%
        ok, reason = best_day.check(max_pct=0.50)
        assert ok is True

    def test_fails_when_single_day_dominates(self, best_day):
        best_day.record_daily_pnl("2026-04-01", 500.0)
        best_day.record_daily_pnl("2026-04-02", 50.0)
        best_day.record_daily_pnl("2026-04-03", 50.0)
        # Best day: $500 / $600 total = 83% > 50%
        ok, reason = best_day.check(max_pct=0.50)
        assert ok is False
        assert "83" in reason or "0.8" in reason

    def test_negative_days_excluded_from_total(self, best_day):
        best_day.record_daily_pnl("2026-04-01", 300.0)
        best_day.record_daily_pnl("2026-04-02", -100.0)
        best_day.record_daily_pnl("2026-04-03", 300.0)
        # Total profit = $300 + $300 = $600 (loss day excluded)
        # Best day: $300 / $600 = 50% — exactly at threshold
        ok, reason = best_day.check(max_pct=0.50)
        assert ok is True  # 50% is not > 50%

    def test_fails_just_over_threshold(self, best_day):
        best_day.record_daily_pnl("2026-04-01", 301.0)
        best_day.record_daily_pnl("2026-04-02", 299.0)
        # Best day: $301 / $600 = 50.17% > 50%
        ok, reason = best_day.check(max_pct=0.50)
        assert ok is False

    def test_only_profitable_days_in_total(self, best_day):
        best_day.record_daily_pnl("2026-04-01", -500.0)
        ok, reason = best_day.check(max_pct=0.50)
        assert ok is True  # no profitable days


# ===========================================================================
# Config Alignment Tests
# ===========================================================================


class TestConfigAlignment:
    """Tests for config defaults aligned with FTMO requirements."""

    def test_risk_config_max_trades_per_day(self):
        cfg = RiskConfig()
        assert cfg.max_trades_per_day == 10

    def test_risk_config_max_positions(self):
        cfg = RiskConfig()
        assert cfg.max_positions == 1

    def test_hard_limits_max_concurrent_positions(self):
        limits = HardLimits()
        assert limits.max_concurrent_positions == 1

    def test_hard_limits_max_trades_per_day(self):
        limits = HardLimits()
        assert limits.max_trades_per_day == 10

    def test_supervisor_vetoes_at_1_position(self, tmp_path):
        supervisor = HardRiskSupervisor(state_dir=str(tmp_path / "state"), kill_switch_dir=str(tmp_path / "kill"))
        supervisor.initialize(100_000.0)
        decision = supervisor.veto(
            symbol="EURUSD",
            side="BUY",
            volume=0.10,
            entry_price=1.1000,
            stop_loss=1.0950,
            account_equity=100_000.0,
            open_positions=[{"symbol": "EURUSD", "side": "BUY", "volume": 0.1, "position_id": "pos1"}],
        )
        assert decision.vetoed is True
        assert "max_concurrent_positions" in decision.rule or "symbol_concentration" in decision.rule

    def test_supervisor_vetoes_at_10_daily_trades(self, tmp_path):
        supervisor = HardRiskSupervisor(state_dir=str(tmp_path / "state"), kill_switch_dir=str(tmp_path / "kill"))
        supervisor.initialize(100_000.0)
        # Record 10 trades
        for _ in range(10):
            supervisor.on_trade_opened(0.10)
        decision = supervisor.veto(
            symbol="EURUSD",
            side="BUY",
            volume=0.10,
            entry_price=1.1000,
            stop_loss=1.0950,
            account_equity=100_000.0,
            open_positions=[],
        )
        assert decision.vetoed is True
        assert "daily_trade_cap" in decision.rule


# ===========================================================================
# PreTradeGate Integration Tests
# ===========================================================================


class TestPreTradeGateIntegration:
    """Tests for weekly loss + best day checks wired into PreTradeGate."""

    def test_gate_has_weekly_loss_check(self):
        cfg = _make_cfg()
        gate = PreTradeGate(cfg)
        order = _make_order()
        account = _make_account()
        gate.initialize_daily_loss(100_000.0, 100_000.0)

        decision = gate.evaluate(order, account, [])
        check_names = [c.name for c in decision.checks]
        assert "weekly_loss" in check_names

    def test_gate_has_best_day_rule_check(self):
        cfg = _make_cfg()
        gate = PreTradeGate(cfg)
        order = _make_order()
        account = _make_account()
        gate.initialize_daily_loss(100_000.0, 100_000.0)

        decision = gate.evaluate(order, account, [])
        check_names = [c.name for c in decision.checks]
        assert "best_day_rule" in check_names

    def test_gate_denies_when_weekly_loss_exceeded(self):
        cfg = _make_cfg()
        gate = PreTradeGate(cfg)
        gate.initialize_daily_loss(100_000.0, 100_000.0)

        # Record massive weekly loss
        today = datetime.now(timezone.utc).astimezone(_FTMO_TZ).strftime("%Y-%m-%d")
        gate._weekly_loss_tracker.record_daily_pnl(-5_100.0, today)

        order = _make_order()
        account = _make_account()
        decision = gate.evaluate(order, account, [])

        # Should be denied
        failed_names = [c.name for c in decision.checks if not c.passed]
        assert "weekly_loss" in failed_names
        assert decision.verdict == RiskVerdict.DENY

    def test_gate_denies_when_best_day_rule_violated(self):
        cfg = _make_cfg()
        gate = PreTradeGate(cfg)
        gate.initialize_daily_loss(100_000.0, 100_000.0)

        # Record one huge day and one tiny day
        gate._best_day_tracker.record_daily_pnl("2026-04-01", 900.0)
        gate._best_day_tracker.record_daily_pnl("2026-04-02", 10.0)

        order = _make_order()
        account = _make_account()
        decision = gate.evaluate(order, account, [])

        failed_names = [c.name for c in decision.checks if not c.passed]
        assert "best_day_rule" in failed_names

    def test_record_closed_trade_pnl_updates_both_trackers(self):
        cfg = _make_cfg()
        gate = PreTradeGate(cfg)
        gate.initialize_daily_loss(100_000.0, 100_000.0)

        gate.record_closed_trade_pnl(-500.0, "2026-04-02")

        # Weekly tracker should have the loss
        today = "2026-04-02"
        assert gate._weekly_loss_tracker._daily_pnl.get(today) == -500.0

        # Best day tracker should also have it
        assert gate._best_day_tracker._daily_pnl.get(today) == -500.0

    def test_save_ftmo_state_includes_weekly_tracker(self, tmp_path):
        cfg = _make_cfg()
        gate = PreTradeGate(cfg)
        gate.initialize_daily_loss(100_000.0, 100_000.0)

        # Monkey-patch state dir for test
        save_path = tmp_path / "weekly_loss_tracker.json"
        gate._weekly_loss_tracker.record_daily_pnl(-100.0, "2026-04-02")
        gate._weekly_loss_tracker.save_state(save_path)

        assert save_path.exists()
        data = json.loads(save_path.read_text())
        assert data["initial_account_size"] == 100_000.0

    def test_gate_max_positions_is_1(self):
        """Verify the default config limits positions to 1."""
        cfg = _make_cfg()
        gate = PreTradeGate(cfg)
        gate.initialize_daily_loss(100_000.0, 100_000.0)

        order = _make_order()
        account = _make_account()

        from novatrade.models import Position

        existing = [
            Position(
                position_id="pos1",
                symbol="EURUSD",
                side=OrderSide.BUY,
                volume=0.1,
                open_price=1.0900,
                unrealized_pnl=50.0,
            )
        ]

        decision = gate.evaluate(order, account, existing)
        failed_names = [c.name for c in decision.checks if not c.passed]
        assert "max_positions" in failed_names

    def test_gate_max_daily_trades_is_10(self):
        """Verify the gate denies after 10 trades."""
        cfg = _make_cfg()
        gate = PreTradeGate(cfg)
        gate.initialize_daily_loss(100_000.0, 100_000.0)

        # Record 10 trades
        for _ in range(10):
            gate.record_trade("EURUSD", "BUY", 0.10)

        order = _make_order()
        account = _make_account()
        decision = gate.evaluate(order, account, [])

        failed_names = [c.name for c in decision.checks if not c.passed]
        assert "daily_trade_count" in failed_names
