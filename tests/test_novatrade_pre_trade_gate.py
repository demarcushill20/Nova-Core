"""Tests for novatrade.risk.pre_trade_gate — pre-trade risk checks."""

from __future__ import annotations

import time
from unittest.mock import patch

from novatrade.config import NovaTradeCfg, RiskConfig
from novatrade.models import (
    AccountMode,
    AccountState,
    HealthState,
    HealthStatus,
    OrderRequest,
    OrderSide,
    OrderType,
    Position,
    RiskVerdict,
    SymbolPrice,
)
from novatrade.risk.pre_trade_gate import PreTradeGate

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _cfg(**overrides) -> NovaTradeCfg:
    """Build a test config with dry_run=False so orders can pass by default."""
    risk_overrides = overrides.pop("risk", {})
    risk = RiskConfig(**{**{"require_stop_loss": True}, **risk_overrides})
    defaults = {
        "dry_run": False,
        "symbols": ["EURUSD", "GBPUSD"],
        "risk": risk,
    }
    defaults.update(overrides)
    return NovaTradeCfg(**defaults)  # type: ignore[arg-type]


def _account(**overrides) -> AccountState:
    defaults = {"balance": 10000.0, "equity": 10000.0, "mode": AccountMode.DEMO}
    defaults.update(overrides)
    return AccountState(**defaults)  # type: ignore[arg-type]


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


def _position(symbol="EURUSD", side=OrderSide.BUY, volume=0.1, **kw) -> Position:
    return Position(
        position_id=kw.get("position_id", "P1"),
        symbol=symbol,
        side=side,
        volume=volume,
        open_price=kw.get("open_price", 1.1000),
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_valid_order_passes(self):
        gate = PreTradeGate(_cfg())
        decision = gate.evaluate(_order(), _account(), [])
        assert decision.verdict == RiskVerdict.ALLOW
        assert not decision.denied
        assert len(decision.failed_checks) == 0

    def test_all_checks_run(self):
        gate = PreTradeGate(_cfg())
        decision = gate.evaluate(_order(), _account(), [])
        check_names = [c.name for c in decision.checks]
        assert "kill_switch" in check_names
        assert "dry_run" in check_names
        assert "account_mode" in check_names
        assert "feed_health" in check_names
        assert "symbol_allowed" in check_names
        assert "volume_bounds" in check_names
        assert "stop_loss" in check_names
        assert "sl_distance" in check_names
        assert "max_positions" in check_names
        assert "daily_trade_count" in check_names
        assert "cooldown" in check_names
        assert "duplicate_position" in check_names
        assert "drawdown" in check_names
        assert "spread" in check_names


# ---------------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------------


class TestKillSwitch:
    @patch("nova_kill_switch.check_kill_switch", return_value="stopped")
    def test_kill_switch_stopped(self, _mock):
        gate = PreTradeGate(_cfg())
        decision = gate.evaluate(_order(), _account(), [])
        assert decision.denied
        failed = {c.name for c in decision.failed_checks}
        assert "kill_switch" in failed

    @patch("nova_kill_switch.check_kill_switch", return_value="run")
    def test_kill_switch_run(self, _mock):
        gate = PreTradeGate(_cfg())
        decision = gate.evaluate(_order(), _account(), [])
        ks = next(c for c in decision.checks if c.name == "kill_switch")
        assert ks.passed

    @patch("nova_kill_switch.check_kill_switch", return_value="readonly")
    def test_kill_switch_readonly_denied(self, _mock):
        gate = PreTradeGate(_cfg())
        decision = gate.evaluate(_order(), _account(), [])
        assert decision.denied

    @patch("nova_kill_switch.check_kill_switch", return_value="pause")
    def test_kill_switch_pause_denied(self, _mock):
        gate = PreTradeGate(_cfg())
        decision = gate.evaluate(_order(), _account(), [])
        assert decision.denied


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------


class TestDryRun:
    def test_dry_run_enabled_denies(self):
        gate = PreTradeGate(_cfg(dry_run=True))
        decision = gate.evaluate(_order(), _account(), [])
        assert decision.denied
        failed = {c.name for c in decision.failed_checks}
        assert "dry_run" in failed

    def test_dry_run_disabled_allows(self):
        gate = PreTradeGate(_cfg(dry_run=False))
        decision = gate.evaluate(_order(), _account(), [])
        dr = next(c for c in decision.checks if c.name == "dry_run")
        assert dr.passed


# ---------------------------------------------------------------------------
# Account mode
# ---------------------------------------------------------------------------


class TestAccountMode:
    def test_demo_allowed(self):
        gate = PreTradeGate(_cfg())
        decision = gate.evaluate(_order(), _account(mode=AccountMode.DEMO), [])
        am = next(c for c in decision.checks if c.name == "account_mode")
        assert am.passed

    def test_challenge_denied_in_mvp(self):
        gate = PreTradeGate(_cfg())
        decision = gate.evaluate(_order(), _account(mode=AccountMode.CHALLENGE), [])
        assert decision.denied
        failed = {c.name for c in decision.failed_checks}
        assert "account_mode" in failed

    def test_funded_denied_in_mvp(self):
        gate = PreTradeGate(_cfg())
        decision = gate.evaluate(
            _order(),
            _account(mode=AccountMode.FUNDED_PRESERVATION),
            [],
        )
        assert decision.denied


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


class TestHealth:
    def test_health_ok_passes(self):
        gate = PreTradeGate(_cfg())
        h = HealthStatus(state=HealthState.OK, connected=True)
        decision = gate.evaluate(_order(), _account(), [], health=h)
        hc = next(c for c in decision.checks if c.name == "health")
        assert hc.passed

    def test_health_down_denies(self):
        gate = PreTradeGate(_cfg())
        h = HealthStatus(state=HealthState.DOWN, message="timeout")
        decision = gate.evaluate(_order(), _account(), [], health=h)
        assert decision.denied
        failed = {c.name for c in decision.failed_checks}
        assert "health" in failed

    def test_health_degraded_passes(self):
        gate = PreTradeGate(_cfg())
        h = HealthStatus(state=HealthState.DEGRADED, connected=True)
        decision = gate.evaluate(_order(), _account(), [], health=h)
        hc = next(c for c in decision.checks if c.name == "health")
        assert hc.passed

    def test_no_health_data_passes(self):
        gate = PreTradeGate(_cfg())
        decision = gate.evaluate(_order(), _account(), [], health=None)
        hc = next(c for c in decision.checks if c.name == "health")
        assert hc.passed


# ---------------------------------------------------------------------------
# Symbol
# ---------------------------------------------------------------------------


class TestSymbol:
    def test_allowed_symbol(self):
        gate = PreTradeGate(_cfg(symbols=["EURUSD"]))
        decision = gate.evaluate(_order(symbol="EURUSD"), _account(), [])
        sc = next(c for c in decision.checks if c.name == "symbol_allowed")
        assert sc.passed

    def test_disallowed_symbol(self):
        gate = PreTradeGate(_cfg(symbols=["GBPUSD"]))
        decision = gate.evaluate(_order(symbol="EURUSD"), _account(), [])
        assert decision.denied
        failed = {c.name for c in decision.failed_checks}
        assert "symbol_allowed" in failed


# ---------------------------------------------------------------------------
# Volume
# ---------------------------------------------------------------------------


class TestVolume:
    def test_valid_volume(self):
        gate = PreTradeGate(_cfg(risk={"max_volume_per_trade": 1.0, "min_volume_per_trade": 0.01}))
        decision = gate.evaluate(_order(volume=0.5), _account(), [])
        vc = next(c for c in decision.checks if c.name == "volume_bounds")
        assert vc.passed

    def test_volume_too_large(self):
        gate = PreTradeGate(_cfg(risk={"max_volume_per_trade": 0.5}))
        decision = gate.evaluate(_order(volume=1.0), _account(), [])
        assert decision.denied
        failed = {c.name for c in decision.failed_checks}
        assert "volume_bounds" in failed

    def test_volume_too_small(self):
        gate = PreTradeGate(_cfg(risk={"min_volume_per_trade": 0.1}))
        decision = gate.evaluate(_order(volume=0.01), _account(), [])
        assert decision.denied
        failed = {c.name for c in decision.failed_checks}
        assert "volume_bounds" in failed


# ---------------------------------------------------------------------------
# Stop loss
# ---------------------------------------------------------------------------


class TestStopLoss:
    def test_stop_loss_present(self):
        gate = PreTradeGate(_cfg(risk={"require_stop_loss": True}))
        decision = gate.evaluate(_order(stop_loss=1.09), _account(), [])
        sl = next(c for c in decision.checks if c.name == "stop_loss")
        assert sl.passed

    def test_stop_loss_missing_denied(self):
        gate = PreTradeGate(_cfg(risk={"require_stop_loss": True}))
        decision = gate.evaluate(_order(stop_loss=None), _account(), [])
        assert decision.denied
        failed = {c.name for c in decision.failed_checks}
        assert "stop_loss" in failed

    def test_stop_loss_not_required(self):
        gate = PreTradeGate(_cfg(risk={"require_stop_loss": False}))
        decision = gate.evaluate(_order(stop_loss=None), _account(), [])
        sl = next(c for c in decision.checks if c.name == "stop_loss")
        assert sl.passed


# ---------------------------------------------------------------------------
# Max positions
# ---------------------------------------------------------------------------


class TestMaxPositions:
    def test_under_limit(self):
        gate = PreTradeGate(_cfg(risk={"max_positions": 3}))
        positions = [_position()]
        decision = gate.evaluate(_order(), _account(), positions)
        mp = next(c for c in decision.checks if c.name == "max_positions")
        assert mp.passed

    def test_at_limit_denied(self):
        gate = PreTradeGate(_cfg(risk={"max_positions": 2}))
        positions = [_position(position_id="P1"), _position(position_id="P2")]
        decision = gate.evaluate(_order(), _account(), positions)
        assert decision.denied
        failed = {c.name for c in decision.failed_checks}
        assert "max_positions" in failed


# ---------------------------------------------------------------------------
# Daily trade count
# ---------------------------------------------------------------------------


class TestDailyTradeCount:
    def test_under_limit(self):
        gate = PreTradeGate(_cfg(risk={"max_trades_per_day": 5}))
        decision = gate.evaluate(_order(), _account(), [])
        tc = next(c for c in decision.checks if c.name == "daily_trade_count")
        assert tc.passed

    def test_at_limit_denied(self):
        gate = PreTradeGate(_cfg(risk={"max_trades_per_day": 2}))
        gate.record_trade("EURUSD", "BUY")
        gate.record_trade("GBPUSD", "SELL")
        decision = gate.evaluate(_order(), _account(), [])
        assert decision.denied
        failed = {c.name for c in decision.failed_checks}
        assert "daily_trade_count" in failed


# ---------------------------------------------------------------------------
# Cooldown
# ---------------------------------------------------------------------------


class TestCooldown:
    def test_no_recent_trade_passes(self):
        gate = PreTradeGate(_cfg(risk={"cooldown_seconds": 60}))
        decision = gate.evaluate(_order(), _account(), [])
        cd = next(c for c in decision.checks if c.name == "cooldown")
        assert cd.passed

    def test_same_symbol_side_denied(self):
        gate = PreTradeGate(_cfg(risk={"cooldown_seconds": 60}))
        gate.record_trade("EURUSD", "BUY")
        decision = gate.evaluate(
            _order(symbol="EURUSD", side=OrderSide.BUY),
            _account(),
            [],
        )
        assert decision.denied
        failed = {c.name for c in decision.failed_checks}
        assert "cooldown" in failed

    def test_different_side_passes(self):
        gate = PreTradeGate(_cfg(risk={"cooldown_seconds": 60}))
        gate.record_trade("EURUSD", "BUY")
        decision = gate.evaluate(
            _order(symbol="EURUSD", side=OrderSide.SELL),
            _account(),
            [],
        )
        cd = next(c for c in decision.checks if c.name == "cooldown")
        assert cd.passed

    def test_different_symbol_passes(self):
        gate = PreTradeGate(_cfg(risk={"cooldown_seconds": 60}))
        gate.record_trade("GBPUSD", "BUY")
        decision = gate.evaluate(
            _order(symbol="EURUSD", side=OrderSide.BUY),
            _account(),
            [],
        )
        cd = next(c for c in decision.checks if c.name == "cooldown")
        assert cd.passed

    def test_cooldown_expired_passes(self):
        from novatrade.risk.pre_trade_gate import _now

        gate = PreTradeGate(_cfg(risk={"cooldown_seconds": 1}))
        gate._trade_log.append((_now() - 5, "EURUSD", "BUY"))
        decision = gate.evaluate(
            _order(symbol="EURUSD", side=OrderSide.BUY),
            _account(),
            [],
        )
        cd = next(c for c in decision.checks if c.name == "cooldown")
        assert cd.passed

    def test_cooldown_disabled(self):
        gate = PreTradeGate(_cfg(risk={"cooldown_seconds": 0}))
        gate.record_trade("EURUSD", "BUY")
        decision = gate.evaluate(
            _order(symbol="EURUSD", side=OrderSide.BUY),
            _account(),
            [],
        )
        cd = next(c for c in decision.checks if c.name == "cooldown")
        assert cd.passed


# ---------------------------------------------------------------------------
# Duplicate position
# ---------------------------------------------------------------------------


class TestDuplicatePosition:
    def test_no_conflict(self):
        gate = PreTradeGate(_cfg())
        positions = [_position(symbol="GBPUSD", side=OrderSide.SELL)]
        decision = gate.evaluate(_order(symbol="EURUSD", side=OrderSide.BUY), _account(), positions)
        dp = next(c for c in decision.checks if c.name == "duplicate_position")
        assert dp.passed

    def test_same_symbol_same_side_denied(self):
        gate = PreTradeGate(_cfg())
        positions = [_position(symbol="EURUSD", side=OrderSide.BUY)]
        decision = gate.evaluate(
            _order(symbol="EURUSD", side=OrderSide.BUY),
            _account(),
            positions,
        )
        assert decision.denied
        failed = {c.name for c in decision.failed_checks}
        assert "duplicate_position" in failed

    def test_same_symbol_opposite_side_passes(self):
        gate = PreTradeGate(_cfg())
        positions = [_position(symbol="EURUSD", side=OrderSide.SELL)]
        decision = gate.evaluate(
            _order(symbol="EURUSD", side=OrderSide.BUY),
            _account(),
            positions,
        )
        dp = next(c for c in decision.checks if c.name == "duplicate_position")
        assert dp.passed


# ---------------------------------------------------------------------------
# Drawdown
# ---------------------------------------------------------------------------


class TestDrawdown:
    def test_no_drawdown(self):
        gate = PreTradeGate(_cfg())
        decision = gate.evaluate(
            _order(),
            _account(balance=10000, equity=10000),
            [],
        )
        dd = next(c for c in decision.checks if c.name == "drawdown")
        assert dd.passed

    def test_drawdown_exceeded(self):
        gate = PreTradeGate(_cfg(risk={"max_drawdown_equity_pct": 5.0}))
        # 6% drawdown: balance=10000, equity=9400
        decision = gate.evaluate(
            _order(),
            _account(balance=10000, equity=9400),
            [],
        )
        assert decision.denied
        failed = {c.name for c in decision.failed_checks}
        assert "drawdown" in failed

    def test_drawdown_within_limit(self):
        gate = PreTradeGate(_cfg(risk={"max_drawdown_equity_pct": 5.0}))
        decision = gate.evaluate(
            _order(),
            _account(balance=10000, equity=9600),
            [],
        )
        dd = next(c for c in decision.checks if c.name == "drawdown")
        assert dd.passed

    def test_zero_balance_skipped(self):
        gate = PreTradeGate(_cfg())
        decision = gate.evaluate(
            _order(),
            _account(balance=0, equity=0),
            [],
        )
        dd = next(c for c in decision.checks if c.name == "drawdown")
        assert dd.passed


# ---------------------------------------------------------------------------
# Spread
# ---------------------------------------------------------------------------


class TestSpread:
    def test_no_price_data_passes(self):
        gate = PreTradeGate(_cfg())
        decision = gate.evaluate(_order(), _account(), [], price=None)
        sp = next(c for c in decision.checks if c.name == "spread")
        assert sp.passed

    def test_spread_within_limit(self):
        gate = PreTradeGate(_cfg(risk={"spread_ceiling_points": 30.0}))
        price = SymbolPrice(symbol="EURUSD", bid=1.10000, ask=1.10020)
        decision = gate.evaluate(_order(), _account(), [], price=price)
        sp = next(c for c in decision.checks if c.name == "spread")
        assert sp.passed  # 2 points < 30

    def test_spread_exceeded(self):
        gate = PreTradeGate(_cfg(risk={"spread_ceiling_points": 10.0}))
        # 50 points spread
        price = SymbolPrice(symbol="EURUSD", bid=1.10000, ask=1.10050)
        decision = gate.evaluate(_order(), _account(), [], price=price)
        assert decision.denied
        failed = {c.name for c in decision.failed_checks}
        assert "spread" in failed


# ---------------------------------------------------------------------------
# Multiple failures
# ---------------------------------------------------------------------------


class TestMultipleFailures:
    def test_multiple_failures_all_reported(self):
        gate = PreTradeGate(
            _cfg(
                dry_run=True,  # fail
                symbols=["GBPUSD"],  # EURUSD not allowed — fail
                risk={"require_stop_loss": True},
            )
        )
        decision = gate.evaluate(
            _order(symbol="EURUSD", stop_loss=None),  # no SL — fail
            _account(),
            [],
        )
        assert decision.denied
        failed_names = {c.name for c in decision.failed_checks}
        assert "dry_run" in failed_names
        assert "symbol_allowed" in failed_names
        assert "stop_loss" in failed_names
        # First failure is the reason
        assert decision.rule == decision.failed_checks[0].name

    def test_decision_properties(self):
        gate = PreTradeGate(_cfg())
        decision = gate.evaluate(_order(), _account(), [])
        assert not decision.denied
        assert decision.failed_checks == []
        assert decision.request is not None


# ---------------------------------------------------------------------------
# Record trade
# ---------------------------------------------------------------------------


class TestRecordTrade:
    def test_record_affects_daily_count(self):
        gate = PreTradeGate(_cfg(risk={"max_trades_per_day": 1}))
        gate.record_trade("EURUSD", "BUY")
        decision = gate.evaluate(_order(), _account(), [])
        failed = {c.name for c in decision.failed_checks}
        assert "daily_trade_count" in failed

    def test_record_affects_cooldown(self):
        gate = PreTradeGate(_cfg(risk={"cooldown_seconds": 300}))
        gate.record_trade("EURUSD", "BUY")
        decision = gate.evaluate(
            _order(symbol="EURUSD", side=OrderSide.BUY),
            _account(),
            [],
        )
        failed = {c.name for c in decision.failed_checks}
        assert "cooldown" in failed


# ---------------------------------------------------------------------------
# Feed health
# ---------------------------------------------------------------------------


class TestFeedHealth:
    def test_no_supervisor_passes(self):
        """No feed supervisor → check skipped (backward compat)."""
        gate = PreTradeGate(_cfg())
        decision = gate.evaluate(_order(), _account(), [])
        fh = next(c for c in decision.checks if c.name == "feed_health")
        assert fh.passed
        assert "skipped" in fh.detail

    def test_healthy_feed_passes(self):
        """Healthy feed → check passes."""
        from novatrade.data.tick import Tick
        from novatrade.monitor.feed_health import FeedHealthConfig, FeedHealthSupervisor

        supervisor = FeedHealthSupervisor(FeedHealthConfig(max_stale_seconds=60.0))
        # Feed a tick to make EURUSD healthy
        supervisor.on_tick(Tick(symbol="EURUSD", bid=1.1000, ask=1.1002, timestamp=time.time()))

        gate = PreTradeGate(_cfg(), feed_health=supervisor)
        decision = gate.evaluate(_order(symbol="EURUSD"), _account(), [])
        fh = next(c for c in decision.checks if c.name == "feed_health")
        assert fh.passed
        assert "HEALTHY" in fh.detail

    def test_stale_feed_denies(self):
        """Stale feed → check denies order."""
        from novatrade.data.tick import Tick
        from novatrade.monitor.feed_health import FeedHealthConfig, FeedHealthSupervisor

        clock_time = [time.time()]
        supervisor = FeedHealthSupervisor(
            FeedHealthConfig(max_stale_seconds=5.0),
            clock=lambda: clock_time[0],
        )
        # Feed a tick, then advance time past staleness threshold
        supervisor.on_tick(Tick(symbol="EURUSD", bid=1.1000, ask=1.1002, timestamp=clock_time[0]))
        clock_time[0] += 60  # 60 seconds later — well past 5s threshold

        gate = PreTradeGate(_cfg(), feed_health=supervisor)
        decision = gate.evaluate(_order(symbol="EURUSD"), _account(), [])
        fh = next(c for c in decision.checks if c.name == "feed_health")
        assert not fh.passed
        assert "STALE" in fh.detail

    def test_not_subscribed_denies(self):
        """Symbol never seen → NOT_SUBSCRIBED → denies."""
        from novatrade.monitor.feed_health import FeedHealthSupervisor

        supervisor = FeedHealthSupervisor()
        gate = PreTradeGate(_cfg(), feed_health=supervisor)
        decision = gate.evaluate(_order(symbol="EURUSD"), _account(), [])
        fh = next(c for c in decision.checks if c.name == "feed_health")
        assert not fh.passed
        assert "NOT_SUBSCRIBED" in fh.detail

    def test_wide_spread_denies(self):
        """Wide spread → SPREAD_WIDE → denies."""
        from novatrade.data.tick import Tick
        from novatrade.monitor.feed_health import FeedHealthConfig, FeedHealthSupervisor

        supervisor = FeedHealthSupervisor(FeedHealthConfig(max_spread_pips=3.0))
        # Tick with 8 pip spread (> 3.0 max)
        supervisor.on_tick(Tick(symbol="EURUSD", bid=1.1000, ask=1.1008, timestamp=time.time()))

        gate = PreTradeGate(_cfg(), feed_health=supervisor)
        decision = gate.evaluate(_order(symbol="EURUSD"), _account(), [])
        fh = next(c for c in decision.checks if c.name == "feed_health")
        assert not fh.passed
        assert "SPREAD_WIDE" in fh.detail

    def test_disconnected_denies(self):
        """Disconnected symbol → denies."""
        from novatrade.monitor.feed_health import FeedHealthSupervisor

        supervisor = FeedHealthSupervisor()
        supervisor.mark_disconnected("EURUSD")

        gate = PreTradeGate(_cfg(), feed_health=supervisor)
        decision = gate.evaluate(_order(symbol="EURUSD"), _account(), [])
        fh = next(c for c in decision.checks if c.name == "feed_health")
        assert not fh.passed
        assert "DISCONNECTED" in fh.detail


# ---------------------------------------------------------------------------
# SL distance validation
# ---------------------------------------------------------------------------


class TestSLDistance:
    def test_no_price_or_sl_skips(self):
        """Missing price or SL → check skipped."""
        gate = PreTradeGate(_cfg())
        decision = gate.evaluate(_order(stop_loss=None), _account(), [])
        sl = next(c for c in decision.checks if c.name == "sl_distance")
        assert sl.passed
        assert "skipped" in sl.detail

    def test_buy_sl_below_entry_valid(self):
        """BUY with SL below entry at sufficient distance → passes."""
        gate = PreTradeGate(_cfg())
        # Entry 1.1000, SL 1.0980 → 20 pips (>= 10 min)
        decision = gate.evaluate(
            _order(side=OrderSide.BUY, price=1.1000, stop_loss=1.0980),
            _account(),
            [],
        )
        sl = next(c for c in decision.checks if c.name == "sl_distance")
        assert sl.passed
        assert "20.0" in sl.detail

    def test_buy_sl_above_entry_denied(self):
        """BUY with SL above entry → denied (inverted SL)."""
        gate = PreTradeGate(_cfg())
        decision = gate.evaluate(
            _order(side=OrderSide.BUY, price=1.1000, stop_loss=1.1050),
            _account(),
            [],
        )
        sl = next(c for c in decision.checks if c.name == "sl_distance")
        assert not sl.passed
        assert "must be below" in sl.detail

    def test_sell_sl_above_entry_valid(self):
        """SELL with SL above entry at sufficient distance → passes."""
        gate = PreTradeGate(_cfg())
        # Entry 1.1000, SL 1.1020 → 20 pips
        decision = gate.evaluate(
            _order(side=OrderSide.SELL, price=1.1000, stop_loss=1.1020),
            _account(),
            [],
        )
        sl = next(c for c in decision.checks if c.name == "sl_distance")
        assert sl.passed

    def test_sell_sl_below_entry_denied(self):
        """SELL with SL below entry → denied (inverted SL)."""
        gate = PreTradeGate(_cfg())
        decision = gate.evaluate(
            _order(side=OrderSide.SELL, price=1.1000, stop_loss=1.0950),
            _account(),
            [],
        )
        sl = next(c for c in decision.checks if c.name == "sl_distance")
        assert not sl.passed
        assert "must be above" in sl.detail

    def test_micro_stop_denied(self):
        """SL distance < 2 pips (default min) → denied."""
        gate = PreTradeGate(_cfg())
        # Entry 1.1000, SL 1.09990 → 1 pip (< 2 min)
        decision = gate.evaluate(
            _order(side=OrderSide.BUY, price=1.1000, stop_loss=1.09990),
            _account(),
            [],
        )
        sl = next(c for c in decision.checks if c.name == "sl_distance")
        assert not sl.passed
        assert "1.0 pips" in sl.detail
        assert "minimum 2" in sl.detail

    def test_at_minimum_passes(self):
        """SL distance >= 2 pips (default min) → passes."""
        gate = PreTradeGate(_cfg())
        # Entry 1.1000, SL 1.09970 → 3 pips (>= 2 min)
        decision = gate.evaluate(
            _order(side=OrderSide.BUY, price=1.1000, stop_loss=1.09970),
            _account(),
            [],
        )
        sl = next(c for c in decision.checks if c.name == "sl_distance")
        assert sl.passed

    def test_market_order_no_price_skips(self):
        """MARKET order with no price → skipped."""
        gate = PreTradeGate(_cfg())
        decision = gate.evaluate(
            _order(order_type=OrderType.MARKET, price=None, stop_loss=1.0950),
            _account(),
            [],
        )
        sl = next(c for c in decision.checks if c.name == "sl_distance")
        assert sl.passed
        assert "skipped" in sl.detail
