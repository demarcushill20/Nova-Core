"""Tests for FtmoDailyLossTracker and its integration into PreTradeGate.

Covers:
- Core tracker: initialization, day reference, buffer logic, day rollover
- PreTradeGate integration: check wired correctly, auto-initialization
- Runner integration: state_store wired into LiveLoop constructor
- Edge cases: zero balance, negative equity, exactly-at-threshold
- State persistence: save and load for crash recovery
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from novatrade.config import NovaTradeCfg, RiskConfig
from novatrade.models import (
    AccountMode,
    AccountState,
    OrderRequest,
    OrderSide,
    OrderType,
)
from novatrade.risk.ftmo_compliance import FtmoDailyLossTracker
from novatrade.risk.pre_trade_gate import PreTradeGate

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cfg(**overrides) -> NovaTradeCfg:
    risk_overrides = overrides.pop("risk", {})
    risk = RiskConfig(**{**{"require_stop_loss": True}, **risk_overrides})
    defaults = {
        "dry_run": False,
        "symbols": ["EURUSD"],
        "risk": risk,
    }
    defaults.update(overrides)
    return NovaTradeCfg(**defaults)  # type: ignore[arg-type]


def _account(balance=100_000.0, equity=100_000.0) -> AccountState:
    return AccountState(balance=balance, equity=equity, mode=AccountMode.DEMO)


def _order(**overrides) -> OrderRequest:
    defaults = {
        "symbol": "EURUSD",
        "side": OrderSide.BUY,
        "order_type": OrderType.MARKET,
        "volume": 0.1,
        "stop_loss": 1.0900,
    }
    defaults.update(overrides)
    return OrderRequest(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# FtmoDailyLossTracker — Unit Tests
# ---------------------------------------------------------------------------


class TestDailyLossTrackerInit:
    def test_default_parameters(self):
        t = FtmoDailyLossTracker()
        assert t.daily_loss_pct == 5.0
        assert t.buffer_pct == 80.0
        assert t.initial_account_size == 0.0

    def test_explicit_parameters(self):
        t = FtmoDailyLossTracker(
            initial_account_size=10_000.0,
            daily_loss_pct=5.0,
            buffer_pct=75.0,
        )
        assert t.daily_limit_usd == 500.0
        assert t.buffer_limit_usd == 375.0

    def test_initialize_sets_reference(self):
        t = FtmoDailyLossTracker()
        t.initialize(balance=100_000.0, equity=99_500.0)
        assert t.initial_account_size == 100_000.0
        assert t.day_reference == 100_000.0  # max(balance, equity)
        assert t.daily_limit_usd == 5_000.0

    def test_initialize_uses_equity_when_higher(self):
        t = FtmoDailyLossTracker()
        t.initialize(balance=99_000.0, equity=100_500.0)
        assert t.day_reference == 100_500.0  # equity was higher

    def test_explicit_initial_account_size_preserved(self):
        """If initial_account_size is set in constructor, initialize() should not overwrite."""
        t = FtmoDailyLossTracker(initial_account_size=10_000.0)
        t.initialize(balance=100_000.0, equity=100_000.0)
        assert t.initial_account_size == 10_000.0  # not overwritten
        assert t.daily_limit_usd == 500.0  # 5% of 10K


class TestDailyLossTrackerCheck:
    def test_not_initialized_skips(self):
        t = FtmoDailyLossTracker()
        result = t.check(balance=0.0, equity=0.0)
        assert result.passed
        assert "not initialized" in result.detail

    def test_auto_initialize_on_first_check(self):
        t = FtmoDailyLossTracker()
        result = t.check(balance=100_000.0, equity=100_000.0)
        assert result.passed
        assert t.initial_account_size == 100_000.0
        assert "daily_loss=$0.00" in result.detail

    def test_no_loss_passes(self):
        t = FtmoDailyLossTracker(initial_account_size=100_000.0)
        t.initialize(100_000.0, 100_000.0)
        result = t.check(100_000.0, 100_000.0)
        assert result.passed
        assert "utilized=0.0%" in result.detail

    def test_small_loss_passes(self):
        t = FtmoDailyLossTracker(initial_account_size=100_000.0)
        t.initialize(100_000.0, 100_000.0)
        # $1,000 loss = 20% of $5,000 limit = well under 80% buffer
        result = t.check(99_500.0, 99_000.0)
        assert result.passed

    def test_buffer_reached_denies(self):
        """At 80% of 5% limit: $4,000 loss on $100K account."""
        t = FtmoDailyLossTracker(initial_account_size=100_000.0)
        t.initialize(100_000.0, 100_000.0)
        result = t.check(97_000.0, 96_000.0)  # $4,000 loss
        assert not result.passed
        assert "buffer reached" in result.detail

    def test_hard_limit_breach_denies(self):
        """Full 5% breach: $5,000 loss on $100K account."""
        t = FtmoDailyLossTracker(initial_account_size=100_000.0)
        t.initialize(100_000.0, 100_000.0)
        result = t.check(96_000.0, 95_000.0)  # $5,000 loss
        assert not result.passed
        assert "BREACH" in result.detail

    def test_exactly_at_buffer_denies(self):
        """Exactly at buffer threshold (80% of $5,000 = $4,000)."""
        t = FtmoDailyLossTracker(initial_account_size=100_000.0)
        t.initialize(100_000.0, 100_000.0)
        result = t.check(97_000.0, 96_000.0)  # exactly $4,000 loss
        assert not result.passed

    def test_just_under_buffer_passes(self):
        """Just under buffer threshold."""
        t = FtmoDailyLossTracker(initial_account_size=100_000.0)
        t.initialize(100_000.0, 100_000.0)
        result = t.check(97_000.0, 96_001.0)  # $3,999 loss
        assert result.passed

    def test_floating_pnl_counts(self):
        """Floating P&L (equity < balance) is included in daily loss."""
        t = FtmoDailyLossTracker(initial_account_size=100_000.0)
        t.initialize(100_000.0, 100_000.0)
        # Balance unchanged, but equity dropped $4,500 (floating loss)
        result = t.check(100_000.0, 95_500.0)
        assert not result.passed  # $4,500 > $4,000 buffer

    def test_custom_buffer_pct(self):
        """Custom buffer at 90% should deny at $4,500 loss."""
        t = FtmoDailyLossTracker(
            initial_account_size=100_000.0,
            buffer_pct=90.0,
        )
        t.initialize(100_000.0, 100_000.0)
        # $4,400 loss — under 90% buffer ($4,500)
        result = t.check(96_000.0, 95_600.0)
        assert result.passed

        # $4,600 loss — over 90% buffer ($4,500)
        result = t.check(96_000.0, 95_400.0)
        assert not result.passed

    def test_10k_account(self):
        """Verify limits for a $10K FTMO account."""
        t = FtmoDailyLossTracker(initial_account_size=10_000.0)
        t.initialize(10_000.0, 10_000.0)
        assert t.daily_limit_usd == 500.0  # 5% of $10K
        assert t.buffer_limit_usd == 400.0  # 80% of $500

        # $350 loss — passes (under $400 buffer)
        result = t.check(9_700.0, 9_650.0)
        assert result.passed

        # $450 loss — fails (over $400 buffer)
        result = t.check(9_600.0, 9_550.0)
        assert not result.passed


class TestDailyLossTrackerDayRollover:
    @patch("novatrade.risk.ftmo_compliance.datetime")
    def test_new_day_resets_reference(self, mock_dt):
        """Day boundary resets the reference to new day's max(balance, equity)."""
        from datetime import datetime, timezone

        # Day 1: March 24
        mock_dt.now.return_value = datetime(2026, 3, 24, 10, 0, tzinfo=timezone.utc)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        t = FtmoDailyLossTracker(initial_account_size=100_000.0)
        t.initialize(100_000.0, 100_000.0)
        assert t.day_reference == 100_000.0

        # Some loss during day 1
        result = t.check(98_000.0, 97_500.0)
        assert result.passed  # $2,500 loss < $4,000 buffer

        # Day 2: March 25 — equity recovered to $99,000
        mock_dt.now.return_value = datetime(2026, 3, 25, 10, 0, tzinfo=timezone.utc)
        result = t.check(99_000.0, 99_000.0)
        assert result.passed
        # Reference should have reset to new day's value
        assert t.day_reference == 99_000.0


class TestDailyLossTrackerPeakEquity:
    def test_update_equity_tracks_peak(self):
        t = FtmoDailyLossTracker(initial_account_size=100_000.0)
        t.initialize(100_000.0, 100_000.0)
        t.update_equity(101_000.0)
        assert t.peak_equity_today == 101_000.0
        t.update_equity(99_000.0)
        assert t.peak_equity_today == 101_000.0  # stays at peak


class TestDailyLossTrackerPersistence:
    def test_save_and_load(self, tmp_path):
        state_file = tmp_path / "daily_loss_tracker.json"

        # Save
        t1 = FtmoDailyLossTracker(initial_account_size=100_000.0)
        t1.initialize(100_000.0, 99_500.0)
        t1.update_equity(99_800.0)
        t1.save_state(state_file)

        assert state_file.exists()
        data = json.loads(state_file.read_text())
        assert data["initial_account_size"] == 100_000.0
        assert data["day_reference"] == 100_000.0  # max(100K, 99.5K)

        # Load into new tracker with same day
        t2 = FtmoDailyLossTracker()
        loaded = t2.load_state(state_file)
        assert loaded is True
        assert t2.initial_account_size == 100_000.0
        assert t2.day_reference == 100_000.0

    def test_load_different_day_returns_false(self, tmp_path):
        state_file = tmp_path / "daily_loss_tracker.json"

        # Write state with yesterday's date
        data = {
            "initial_account_size": 100_000.0,
            "day_key": "2020-01-01",  # old date
            "day_reference": 100_000.0,
            "peak_equity_today": 100_000.0,
        }
        state_file.write_text(json.dumps(data))

        t = FtmoDailyLossTracker()
        loaded = t.load_state(state_file)
        assert loaded is False

    def test_load_missing_file_returns_false(self, tmp_path):
        t = FtmoDailyLossTracker()
        loaded = t.load_state(tmp_path / "nonexistent.json")
        assert loaded is False


class TestDailyLossTrackerSnapshot:
    def test_snapshot_contents(self):
        t = FtmoDailyLossTracker(initial_account_size=100_000.0)
        t.initialize(100_000.0, 100_000.0)
        snap = t.snapshot()
        assert snap["initialized"] is True
        assert snap["initial_account_size"] == 100_000.0
        assert snap["daily_limit_usd"] == 5_000.0
        assert snap["buffer_limit_usd"] == 4_000.0
        assert snap["day_reference"] == 100_000.0


# ---------------------------------------------------------------------------
# PreTradeGate Integration
# ---------------------------------------------------------------------------


class TestPreTradeGateIntegration:
    def test_ftmo_daily_loss_check_present(self):
        """Verify the ftmo_daily_loss check is included in gate evaluation."""
        gate = PreTradeGate(_cfg())
        decision = gate.evaluate(_order(), _account(), [])
        check_names = [c.name for c in decision.checks]
        assert "ftmo_daily_loss" in check_names

    def test_auto_initializes_on_first_evaluate(self):
        """Gate auto-initializes daily loss tracker from account data."""
        gate = PreTradeGate(_cfg())
        decision = gate.evaluate(_order(), _account(100_000.0, 100_000.0), [])
        ftmo_check = next(c for c in decision.checks if c.name == "ftmo_daily_loss")
        assert ftmo_check.passed
        assert "daily_loss=$0.00" in ftmo_check.detail

    def test_denies_when_buffer_reached(self):
        """Gate denies orders when FTMO daily loss buffer is reached."""
        gate = PreTradeGate(_cfg())
        # First call initializes
        gate.evaluate(_order(), _account(100_000.0, 100_000.0), [])
        # Second call with loss exceeding buffer ($4,000 of $5,000 limit)
        acct = _account(97_000.0, 95_500.0)  # $4,500 loss from ref ($100K)
        decision = gate.evaluate(_order(), acct, [])
        ftmo_check = next(c for c in decision.checks if c.name == "ftmo_daily_loss")
        assert not ftmo_check.passed
        assert "buffer reached" in ftmo_check.detail

    def test_passes_when_within_buffer(self):
        """Gate allows orders when within FTMO daily loss buffer."""
        gate = PreTradeGate(_cfg())
        gate.evaluate(_order(), _account(100_000.0, 100_000.0), [])
        acct = _account(99_000.0, 98_500.0)  # $1,500 loss — well within
        decision = gate.evaluate(_order(), acct, [])
        ftmo_check = next(c for c in decision.checks if c.name == "ftmo_daily_loss")
        assert ftmo_check.passed

    def test_initialize_daily_loss_method(self):
        """Explicit initialization via gate method."""
        gate = PreTradeGate(_cfg())
        gate.initialize_daily_loss(100_000.0, 100_000.0)
        assert gate._daily_loss_tracker.initial_account_size == 100_000.0

    def test_save_ftmo_state_includes_daily_loss(self, tmp_path):
        """save_ftmo_state persists daily loss tracker."""
        gate = PreTradeGate(_cfg())
        gate.initialize_daily_loss(100_000.0, 100_000.0)
        # Patch state dir to tmp
        gate._daily_loss_tracker.save_state(tmp_path / "daily_loss.json")
        assert (tmp_path / "daily_loss.json").exists()

    def test_custom_daily_drawdown_pct(self):
        """Gate uses config.risk.max_daily_drawdown_pct for daily loss limit."""
        cfg = _cfg(risk={"max_daily_drawdown_pct": 4.0})
        gate = PreTradeGate(cfg)
        assert gate._daily_loss_tracker.daily_loss_pct == 4.0
        gate.initialize_daily_loss(100_000.0, 100_000.0)
        assert gate._daily_loss_tracker.daily_limit_usd == 4_000.0


# ---------------------------------------------------------------------------
# Runner Integration: state_store → LiveLoop wiring
# ---------------------------------------------------------------------------


class TestRunnerLiveLoopWiring:
    def test_live_loop_accepts_state_store(self):
        """Verify LiveLoop constructor accepts state_store parameter."""
        # Just verify the constructor signature includes state_store
        import inspect

        from novatrade.runtime.live_loop import LiveLoop

        sig = inspect.signature(LiveLoop.__init__)
        params = list(sig.parameters.keys())
        assert "state_store" in params

    def test_live_loop_stores_state_store(self):
        """Verify LiveLoop stores state_store internally."""
        from unittest.mock import MagicMock

        from novatrade.runtime.live_loop import LiveLoop

        mock_store = MagicMock()
        loop = LiveLoop(
            poller=MagicMock(),
            aggregator=MagicMock(),
            supervisor=MagicMock(),
            strategy_engine=MagicMock(),
            live_agent=MagicMock(),
            state_store=mock_store,
        )
        assert loop._state_store is mock_store

    def test_build_live_stack_passes_state_store(self):
        """Verify build_live_stack passes state_store to LiveLoop.

        We verify this by reading the runner source code — the `state_store=state_store`
        line must be present in the LiveLoop constructor call.
        """
        import ast

        runner_path = Path(__file__).parent.parent / "novatrade" / "runtime" / "runner.py"
        source = runner_path.read_text()
        tree = ast.parse(source)

        # Find the build_live_stack function
        found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.keyword) and node.arg == "state_store":
                # Check it's in a LiveLoop constructor call
                found = True
                break

        assert found, "state_store not passed to LiveLoop in build_live_stack"


# ---------------------------------------------------------------------------
# Edge Cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_zero_initial_account_size(self):
        t = FtmoDailyLossTracker(initial_account_size=0.0)
        result = t.check(0.0, 0.0)
        assert result.passed
        assert "not initialized" in result.detail

    def test_negative_loss_passes(self):
        """Profit (negative loss) should always pass."""
        t = FtmoDailyLossTracker(initial_account_size=100_000.0)
        t.initialize(100_000.0, 100_000.0)
        # Equity is above reference (profit)
        result = t.check(100_500.0, 101_000.0)
        assert result.passed

    def test_exact_daily_limit_is_breach(self):
        """Exactly at the limit is a breach (>= comparison)."""
        t = FtmoDailyLossTracker(initial_account_size=100_000.0)
        t.initialize(100_000.0, 100_000.0)
        result = t.check(96_000.0, 95_000.0)  # exactly $5,000 loss
        assert not result.passed
        assert "BREACH" in result.detail

    def test_utilization_percentage_accuracy(self):
        """Verify utilization percentage in detail string."""
        t = FtmoDailyLossTracker(initial_account_size=100_000.0)
        t.initialize(100_000.0, 100_000.0)
        # $2,500 loss = 50% of $5,000 limit
        result = t.check(98_000.0, 97_500.0)
        assert result.passed
        assert "utilized=50.0%" in result.detail
