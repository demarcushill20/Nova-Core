"""Tests for FTMO compliance enforcement — lot-size consistency & server request counter.

Tests both components independently and their integration into PreTradeGate.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from novatrade.risk.ftmo_compliance import (
    LotSizeConsistencyChecker,
    ServerRequestCounter,
    TradingDaysTracker,
)

# =========================================================================
# Lot-Size Consistency Checker
# =========================================================================


class TestLotSizeConsistencyChecker:
    """Tests for the FTMO lot-size consistency rule."""

    def test_passes_with_insufficient_history(self):
        """Should pass when we have fewer trades than min_trades_for_enforcement."""
        checker = LotSizeConsistencyChecker(min_trades_for_enforcement=3)
        checker.record(0.10)
        checker.record(0.10)
        result = checker.check(5.0)  # wildly different but only 2 trades
        assert result.passed
        assert "only 2 trades" in result.detail

    def test_passes_consistent_volume(self):
        """Should pass when proposed volume is near the median."""
        checker = LotSizeConsistencyChecker()
        for _ in range(5):
            checker.record(0.10)
        result = checker.check(0.10)
        assert result.passed
        assert "ratio=" in result.detail

    def test_fails_volume_too_large(self):
        """Should deny when proposed volume exceeds 3x median."""
        checker = LotSizeConsistencyChecker(max_deviation_factor=3.0)
        for _ in range(5):
            checker.record(0.10)
        result = checker.check(0.50)  # 5x median → exceeds 3x limit
        assert not result.passed
        assert "lot-size consistency" in result.detail.lower() or "FTMO" in result.detail

    def test_fails_volume_too_small(self):
        """Should deny when proposed volume is below 1/3x median."""
        checker = LotSizeConsistencyChecker(max_deviation_factor=3.0)
        for _ in range(5):
            checker.record(1.00)
        result = checker.check(0.10)  # 0.1x median → below 0.33x floor
        assert not result.passed
        assert "below" in result.detail

    def test_boundary_exact_max(self):
        """Exactly 3x median should pass (not strictly greater)."""
        checker = LotSizeConsistencyChecker(max_deviation_factor=3.0, min_trades_for_enforcement=3)
        for _ in range(5):
            checker.record(0.10)
        result = checker.check(0.30)  # exactly 3.0x
        assert result.passed

    def test_boundary_just_over_max(self):
        """Just over 3x median should fail."""
        checker = LotSizeConsistencyChecker(max_deviation_factor=3.0, min_trades_for_enforcement=3)
        for _ in range(5):
            checker.record(0.10)
        result = checker.check(0.31)  # 3.1x > 3.0x
        assert not result.passed

    def test_window_size_respected(self):
        """Only the last N trades should be considered."""
        checker = LotSizeConsistencyChecker(window_size=5, min_trades_for_enforcement=3)
        # First fill with small volumes
        for _ in range(5):
            checker.record(0.01)
        # Then fill with larger volumes (pushes out the small ones)
        for _ in range(5):
            checker.record(1.00)
        # Median is now 1.00, so 2.0 should pass (2x < 3x)
        result = checker.check(2.00)
        assert result.passed

    def test_mixed_volumes_median(self):
        """Median calculation with mixed volumes."""
        checker = LotSizeConsistencyChecker(min_trades_for_enforcement=3)
        for vol in [0.05, 0.10, 0.10, 0.15, 0.20]:
            checker.record(vol)
        # Median of [0.05, 0.10, 0.10, 0.15, 0.20] = 0.10
        assert checker.current_median == 0.10
        result = checker.check(0.25)  # 2.5x median → passes (< 3x)
        assert result.passed

    def test_current_median_insufficient_data(self):
        """current_median returns None when insufficient data."""
        checker = LotSizeConsistencyChecker(min_trades_for_enforcement=5)
        checker.record(0.10)
        assert checker.current_median is None

    def test_trade_count(self):
        """trade_count tracks number of recorded trades."""
        checker = LotSizeConsistencyChecker()
        assert checker.trade_count == 0
        checker.record(0.10)
        checker.record(0.20)
        assert checker.trade_count == 2

    def test_save_and_load_state(self, tmp_path: Path):
        """State persistence round-trips correctly."""
        state_file = tmp_path / "lot_history.json"

        checker1 = LotSizeConsistencyChecker(min_trades_for_enforcement=3)
        for vol in [0.10, 0.15, 0.20, 0.25, 0.30]:
            checker1.record(vol, "EURUSD")
        checker1.save_state(state_file)

        checker2 = LotSizeConsistencyChecker(min_trades_for_enforcement=3)
        loaded = checker2.load_state(state_file)
        assert loaded == 5
        assert checker2.trade_count == 5
        assert checker2.current_median == 0.20

    def test_load_missing_file(self, tmp_path: Path):
        """Loading from a missing file returns 0."""
        checker = LotSizeConsistencyChecker()
        loaded = checker.load_state(tmp_path / "nonexistent.json")
        assert loaded == 0

    def test_load_corrupt_file(self, tmp_path: Path):
        """Loading from a corrupt file returns 0."""
        state_file = tmp_path / "lot_history.json"
        state_file.write_text("not json", encoding="utf-8")
        checker = LotSizeConsistencyChecker()
        loaded = checker.load_state(state_file)
        assert loaded == 0

    def test_zero_median_skipped(self):
        """If median is 0 (shouldn't happen in practice), skip the check."""
        checker = LotSizeConsistencyChecker(min_trades_for_enforcement=3)
        for _ in range(5):
            checker.record(0.0)
        result = checker.check(0.10)
        assert result.passed
        assert "median volume is 0" in result.detail


# =========================================================================
# Server Request Counter
# =========================================================================


class TestServerRequestCounter:
    """Tests for the FTMO daily server request counter."""

    def test_starts_at_zero(self):
        """Counter starts at 0."""
        counter = ServerRequestCounter()
        assert counter.count == 0
        assert counter.remaining == 1500

    def test_record_increments(self):
        """Each record call increments the count."""
        counter = ServerRequestCounter()
        counter.record("order_open")
        counter.record("modify_sl")
        counter.record("close_position")
        assert counter.count == 3

    def test_check_passes_under_ceiling(self):
        """Should pass when under the hard ceiling."""
        counter = ServerRequestCounter(hard_ceiling=1500)
        for i in range(10):
            counter.record(f"op_{i}")
        result = counter.check()
        assert result.passed
        assert "requests_today=10" in result.detail

    def test_check_fails_at_ceiling(self):
        """Should fail when at the hard ceiling."""
        counter = ServerRequestCounter(hard_ceiling=5)  # low ceiling for testing
        for i in range(5):
            counter.record(f"op_{i}")
        result = counter.check()
        assert not result.passed
        assert "hard ceiling" in result.detail

    def test_check_fails_above_ceiling(self):
        """Should fail when above the hard ceiling."""
        counter = ServerRequestCounter(hard_ceiling=3)
        for i in range(5):
            counter.record(f"op_{i}")
        result = counter.check()
        assert not result.passed

    def test_alert_threshold(self):
        """at_alert should be True once alert threshold is reached."""
        counter = ServerRequestCounter(hard_ceiling=10, alert_threshold=5)
        for i in range(4):
            counter.record(f"op_{i}")
        assert not counter.at_alert
        counter.record("op_4")
        assert counter.at_alert

    def test_at_ceiling_property(self):
        """at_ceiling should be True once hard ceiling is reached."""
        counter = ServerRequestCounter(hard_ceiling=3)
        assert not counter.at_ceiling
        for i in range(3):
            counter.record(f"op_{i}")
        assert counter.at_ceiling

    def test_remaining_decrements(self):
        """remaining should decrease with each record."""
        counter = ServerRequestCounter(hard_ceiling=100)
        assert counter.remaining == 100
        counter.record("op")
        assert counter.remaining == 99

    def test_remaining_floors_at_zero(self):
        """remaining never goes negative."""
        counter = ServerRequestCounter(hard_ceiling=2)
        for i in range(5):
            counter.record(f"op_{i}")
        assert counter.remaining == 0

    def test_day_reset(self):
        """Counter resets when the day changes."""
        counter = ServerRequestCounter()
        counter._day_key = "2026-01-01"  # force old day
        counter._count = 500
        # _ensure_day will detect mismatch and reset
        assert counter.count == 0  # triggers _ensure_day

    def test_save_and_load_state_same_day(self, tmp_path: Path):
        """State persistence works for same-day reload."""
        state_file = tmp_path / "request_counter.json"

        counter1 = ServerRequestCounter(hard_ceiling=1500)
        for i in range(42):
            counter1.record(f"op_{i}")
        counter1.save_state(state_file)

        counter2 = ServerRequestCounter(hard_ceiling=1500)
        loaded = counter2.load_state(state_file)
        assert loaded is True
        assert counter2.count == 42

    def test_load_stale_state(self, tmp_path: Path):
        """Loading state from a different day returns False (fresh start)."""
        state_file = tmp_path / "request_counter.json"
        state_file.write_text(
            json.dumps({"day": "2020-01-01", "count": 999}),
            encoding="utf-8",
        )
        counter = ServerRequestCounter()
        loaded = counter.load_state(state_file)
        assert loaded is False
        assert counter.count == 0

    def test_load_missing_file(self, tmp_path: Path):
        """Loading from missing file returns False."""
        counter = ServerRequestCounter()
        loaded = counter.load_state(tmp_path / "nonexistent.json")
        assert loaded is False

    def test_load_corrupt_file(self, tmp_path: Path):
        """Loading from corrupt file returns False."""
        state_file = tmp_path / "request_counter.json"
        state_file.write_text("{{bad json", encoding="utf-8")
        counter = ServerRequestCounter()
        loaded = counter.load_state(state_file)
        assert loaded is False


# =========================================================================
# Trading Days Tracker
# =========================================================================


class TestTradingDaysTracker:
    """Tests for the FTMO minimum trading days tracker."""

    def test_starts_empty(self):
        """Tracker starts with zero trading days."""
        tracker = TradingDaysTracker()
        assert tracker.days_traded == 0
        assert tracker.days_remaining == 4
        assert not tracker.requirement_met

    def test_record_trade_day_default_today(self):
        """Recording without explicit date uses today UTC."""
        tracker = TradingDaysTracker()
        tracker.record_trade_day()
        assert tracker.days_traded == 1

    def test_record_trade_day_explicit(self):
        """Recording with explicit date string."""
        tracker = TradingDaysTracker()
        tracker.record_trade_day("2026-03-01")
        tracker.record_trade_day("2026-03-02")
        assert tracker.days_traded == 2

    def test_duplicate_dates_not_counted(self):
        """Same date recorded twice counts as one day."""
        tracker = TradingDaysTracker()
        tracker.record_trade_day("2026-03-01")
        tracker.record_trade_day("2026-03-01")
        tracker.record_trade_day("2026-03-01")
        assert tracker.days_traded == 1

    def test_requirement_met_at_threshold(self):
        """Requirement is met when days_traded >= min_days_required."""
        tracker = TradingDaysTracker(min_days_required=3)
        tracker.record_trade_day("2026-03-01")
        tracker.record_trade_day("2026-03-02")
        assert not tracker.requirement_met
        tracker.record_trade_day("2026-03-03")
        assert tracker.requirement_met
        assert tracker.days_remaining == 0

    def test_requirement_exceeded(self):
        """Trading more than required still reports met."""
        tracker = TradingDaysTracker(min_days_required=2)
        for day in range(1, 6):
            tracker.record_trade_day(f"2026-03-{day:02d}")
        assert tracker.requirement_met
        assert tracker.days_remaining == 0
        assert tracker.days_traded == 5

    def test_days_remaining_decreases(self):
        """days_remaining decreases as trading days are added."""
        tracker = TradingDaysTracker(min_days_required=4)
        assert tracker.days_remaining == 4
        tracker.record_trade_day("2026-03-01")
        assert tracker.days_remaining == 3
        tracker.record_trade_day("2026-03-02")
        assert tracker.days_remaining == 2

    def test_trading_dates_sorted(self):
        """trading_dates returns dates in sorted order."""
        tracker = TradingDaysTracker()
        tracker.record_trade_day("2026-03-15")
        tracker.record_trade_day("2026-03-01")
        tracker.record_trade_day("2026-03-10")
        assert tracker.trading_dates == ["2026-03-01", "2026-03-10", "2026-03-15"]

    def test_check_returns_progress(self):
        """check() returns informational result showing progress."""
        tracker = TradingDaysTracker(min_days_required=4)
        tracker.record_trade_day("2026-03-01")
        tracker.record_trade_day("2026-03-02")
        result = tracker.check()
        assert result.passed  # informational — never blocks
        assert "2/4" in result.detail
        assert "need 2 more" in result.detail

    def test_check_returns_met(self):
        """check() shows requirement met when threshold reached."""
        tracker = TradingDaysTracker(min_days_required=2)
        tracker.record_trade_day("2026-03-01")
        tracker.record_trade_day("2026-03-02")
        result = tracker.check()
        assert result.passed
        assert "requirement met" in result.detail

    def test_save_and_load_state(self, tmp_path: Path):
        """State persistence round-trips correctly."""
        state_file = tmp_path / "trading_days.json"

        t1 = TradingDaysTracker(
            min_days_required=4,
            challenge_start="2026-03-01",
            challenge_end="2026-03-31",
        )
        t1.record_trade_day("2026-03-05")
        t1.record_trade_day("2026-03-08")
        t1.record_trade_day("2026-03-12")
        t1.save_state(state_file)

        t2 = TradingDaysTracker()
        loaded = t2.load_state(state_file)
        assert loaded is True
        assert t2.days_traded == 3
        assert t2.trading_dates == ["2026-03-05", "2026-03-08", "2026-03-12"]
        assert t2.challenge_start == "2026-03-01"
        assert t2.challenge_end == "2026-03-31"

    def test_load_missing_file(self, tmp_path: Path):
        """Loading from missing file returns False."""
        tracker = TradingDaysTracker()
        loaded = tracker.load_state(tmp_path / "nonexistent.json")
        assert loaded is False

    def test_load_corrupt_file(self, tmp_path: Path):
        """Loading from corrupt file returns False."""
        state_file = tmp_path / "trading_days.json"
        state_file.write_text("{bad json!", encoding="utf-8")
        tracker = TradingDaysTracker()
        loaded = tracker.load_state(state_file)
        assert loaded is False

    def test_custom_min_days(self):
        """Custom min_days_required works."""
        tracker = TradingDaysTracker(min_days_required=10)
        for day in range(1, 11):
            tracker.record_trade_day(f"2026-03-{day:02d}")
        assert tracker.requirement_met
        assert tracker.days_remaining == 0


# =========================================================================
# Integration with PreTradeGate
# =========================================================================


class TestPreTradeGateIntegration:
    """Test that FTMO compliance checks are wired into PreTradeGate."""

    @pytest.fixture
    def _patch_state_load(self):
        """Patch state loading so tests don't depend on filesystem."""
        with (
            patch.object(LotSizeConsistencyChecker, "load_state", return_value=0),
            patch.object(ServerRequestCounter, "load_state", return_value=False),
            patch.object(TradingDaysTracker, "load_state", return_value=False),
        ):
            yield

    @pytest.fixture
    def gate(self, _patch_state_load):
        from novatrade.config import NovaTradeCfg
        from novatrade.risk.pre_trade_gate import PreTradeGate

        cfg = NovaTradeCfg()
        return PreTradeGate(cfg)

    @pytest.fixture
    def account(self):
        from novatrade.models import AccountMode, AccountState

        return AccountState(
            balance=100_000.0,
            equity=100_000.0,
            margin=0.0,
            free_margin=100_000.0,
            mode=AccountMode.DEMO,
        )

    @pytest.fixture
    def order(self):
        from novatrade.models import OrderRequest, OrderSide, OrderType

        return OrderRequest(
            symbol="EURUSD",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            volume=0.10,
            stop_loss=1.0800,
        )

    def test_lot_consistency_check_present(self, gate, order, account):
        """Evaluate includes the lot_consistency check."""
        decision = gate.evaluate(order, account, [])
        check_names = [c.name for c in decision.checks]
        assert "lot_consistency" in check_names

    def test_server_request_check_present(self, gate, order, account):
        """Evaluate includes the server_request_limit check."""
        decision = gate.evaluate(order, account, [])
        check_names = [c.name for c in decision.checks]
        assert "server_request_limit" in check_names

    def test_lot_consistency_blocks_outlier(self, gate, order, account):
        """Gate should deny when lot size is an outlier."""
        # Seed history with 0.10 lots
        for _ in range(5):
            gate._lot_checker.record(0.10)
        # Try to trade 1.00 (10x median → exceeds 3x)
        order.volume = 1.00
        decision = gate.evaluate(order, account, [])
        assert decision.denied
        failed_names = [c.name for c in decision.failed_checks]
        assert "lot_consistency" in failed_names

    def test_request_counter_blocks_at_ceiling(self, gate, order, account):
        """Gate should deny when server request ceiling is reached."""
        gate._request_counter._hard_ceiling = 5
        gate._request_counter.hard_ceiling = 5
        for i in range(5):
            gate._request_counter.record(f"op_{i}")
        decision = gate.evaluate(order, account, [])
        assert decision.denied
        failed_names = [c.name for c in decision.failed_checks]
        assert "server_request_limit" in failed_names

    def test_record_trade_updates_lot_checker(self, gate):
        """record_trade should update the lot checker and request counter."""
        gate.record_trade("EURUSD", "buy", volume=0.15)
        assert gate._lot_checker.trade_count == 1
        assert gate._request_counter.count == 1

    def test_record_server_request(self, gate):
        """record_server_request tracks non-trade operations."""
        gate.record_server_request("modify_sl")
        gate.record_server_request("close_position")
        assert gate._request_counter.count == 2

    def test_min_trading_days_check_present(self, gate, order, account):
        """Evaluate includes the min_trading_days check."""
        decision = gate.evaluate(order, account, [])
        check_names = [c.name for c in decision.checks]
        assert "min_trading_days" in check_names

    def test_min_trading_days_never_blocks(self, gate, order, account):
        """min_trading_days check is informational — never causes DENY."""
        decision = gate.evaluate(order, account, [])
        days_check = next(c for c in decision.checks if c.name == "min_trading_days")
        assert days_check.passed

    def test_record_trade_updates_days_tracker(self, gate):
        """record_trade should also record the trading day."""
        gate.record_trade("EURUSD", "buy", volume=0.15)
        assert gate._days_tracker.days_traded == 1

    def test_save_ftmo_state(self, gate, tmp_path: Path):
        """save_ftmo_state persists all three FTMO checkers."""
        gate._lot_checker._history.append(
            __import__("novatrade.risk.ftmo_compliance", fromlist=["LotRecord"]).LotRecord(time.time(), 0.10, "EURUSD")
        )
        gate._request_counter.record("test_op")
        gate._days_tracker.record_trade_day("2026-03-22")
        with (
            patch.object(LotSizeConsistencyChecker, "save_state") as lot_save,
            patch.object(ServerRequestCounter, "save_state") as req_save,
            patch.object(TradingDaysTracker, "save_state") as days_save,
        ):
            gate.save_ftmo_state()
            lot_save.assert_called_once()
            req_save.assert_called_once()
            days_save.assert_called_once()
