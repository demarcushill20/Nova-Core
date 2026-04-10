"""Tests for Risk Management Hardening.

Covers:
- Policy layer evaluation order
- Session policy (forex market hours)
- HALT verdict semantics
- Kill-switch governance
- RiskAction portfolio actions
- Forex weekend detection
- Trading Agent HALT integration
"""

import datetime as dt

from novatrade.config import NovaTradeCfg, RiskConfig
from novatrade.models import (
    AccountMode,
    AccountState,
    OrderRequest,
    OrderSide,
    OrderType,
    RiskAction,
    RiskVerdict,
)
from novatrade.risk.risk_engine import (
    RiskEngine,
    _is_forex_weekend,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _cfg(**overrides) -> NovaTradeCfg:
    risk = RiskConfig(
        max_daily_drawdown_pct=5.0,
        max_total_drawdown_pct=10.0,
        max_positions=5,
        max_volume_per_trade=1.0,
        min_volume_per_trade=0.01,
    )
    defaults = dict(
        mode=AccountMode.DEMO,
        symbols=["EURUSD"],
        risk=risk,
    )
    defaults.update(overrides)
    return NovaTradeCfg(**defaults)  # type: ignore[arg-type]


def _irb_cfg(**risk_overrides) -> NovaTradeCfg:
    """Config with session checking enabled."""
    risk_kw = dict(
        max_daily_drawdown_pct=5.0,
        max_total_drawdown_pct=10.0,
        max_positions=5,
        max_volume_per_trade=1.0,
        min_volume_per_trade=0.01,
        check_forex_session=True,
    )
    risk_kw.update(risk_overrides)
    risk = RiskConfig(**risk_kw)  # type: ignore[arg-type]
    return NovaTradeCfg(
        mode=AccountMode.DEMO,
        symbols=["EURUSD"],
        risk=risk,
    )


def _account(equity: float = 100_000, balance: float = 100_000) -> AccountState:
    return AccountState(
        balance=balance,
        equity=equity,
        mode=AccountMode.DEMO,
    )


def _order(
    symbol: str = "EURUSD",
    side: OrderSide = OrderSide.BUY,
    volume: float = 0.10,
    price: float = 1.1000,
    stop_loss: float = 1.0950,
) -> OrderRequest:
    return OrderRequest(
        symbol=symbol,
        side=side,
        order_type=OrderType.STOP,
        volume=volume,
        price=price,
        stop_loss=stop_loss,
    )


def _make_engine(cfg=None, equity=100_000):
    """Create and initialize a risk engine."""
    engine = RiskEngine(cfg or _cfg())
    engine.initialize(_account(equity=equity))
    return engine


def _make_weekday_clock(weekday: int, hour: int = 12):
    """Return a clock function that returns a specific weekday/hour.

    weekday: 0=Mon, 4=Fri, 5=Sat, 6=Sun
    """
    # 2026-03-16 is a Monday (weekday=0)
    # 2026-03-21 is Saturday (weekday=5)
    # 2026-03-22 is Sunday (weekday=6)
    base_monday = dt.datetime(2026, 3, 16, hour, 0, 0, tzinfo=dt.timezone.utc)
    target = base_monday + dt.timedelta(days=weekday)
    return lambda: target


# ---------------------------------------------------------------------------
# Forex weekend detection tests
# ---------------------------------------------------------------------------


class TestForexWeekend:
    def test_monday_is_open(self):
        t = dt.datetime(2026, 3, 16, 12, 0, tzinfo=dt.timezone.utc)  # Monday
        assert not _is_forex_weekend(t)

    def test_friday_before_close(self):
        t = dt.datetime(2026, 3, 20, 21, 0, tzinfo=dt.timezone.utc)  # Friday 21:00
        assert not _is_forex_weekend(t)

    def test_friday_at_close(self):
        t = dt.datetime(2026, 3, 20, 22, 0, tzinfo=dt.timezone.utc)  # Friday 22:00
        assert _is_forex_weekend(t)

    def test_friday_after_close(self):
        t = dt.datetime(2026, 3, 20, 23, 0, tzinfo=dt.timezone.utc)  # Friday 23:00
        assert _is_forex_weekend(t)

    def test_saturday_all_day(self):
        t = dt.datetime(2026, 3, 21, 12, 0, tzinfo=dt.timezone.utc)  # Saturday
        assert _is_forex_weekend(t)

    def test_sunday_before_open(self):
        t = dt.datetime(2026, 3, 22, 21, 0, tzinfo=dt.timezone.utc)  # Sunday 21:00
        assert _is_forex_weekend(t)

    def test_sunday_at_open(self):
        t = dt.datetime(2026, 3, 22, 22, 0, tzinfo=dt.timezone.utc)  # Sunday 22:00
        assert not _is_forex_weekend(t)

    def test_sunday_after_open(self):
        t = dt.datetime(2026, 3, 22, 23, 0, tzinfo=dt.timezone.utc)  # Sunday 23:00
        assert not _is_forex_weekend(t)

    def test_wednesday_midday(self):
        t = dt.datetime(2026, 3, 18, 14, 0, tzinfo=dt.timezone.utc)  # Wednesday
        assert not _is_forex_weekend(t)


# ---------------------------------------------------------------------------
# Session policy tests (Layer 1)
# ---------------------------------------------------------------------------


class TestSessionPolicy:
    def test_session_check_disabled_by_default(self):
        """Session check is disabled by default (not in backtest)."""
        engine = _make_engine(_cfg())
        # Weekend should ALLOW because session check is off by default
        engine._clock = _make_weekday_clock(5, 12)  # Saturday
        decision = engine.pre_trade_check(_order(), _account(), [])
        assert decision.verdict == RiskVerdict.ALLOW

    def test_session_check_explicitly_disabled(self):
        """Session check can be explicitly disabled via config."""
        cfg = _cfg(
            risk=RiskConfig(
                max_daily_drawdown_pct=5.0,
                max_total_drawdown_pct=10.0,
                max_positions=5,
                max_volume_per_trade=1.0,
                min_volume_per_trade=0.01,
                check_forex_session=False,
            )
        )
        engine = _make_engine(cfg)
        engine._clock = _make_weekday_clock(5, 12)  # Saturday
        decision = engine.pre_trade_check(_order(), _account(), [])
        assert decision.verdict == RiskVerdict.ALLOW

    def test_session_deny_during_weekend(self):
        engine = _make_engine(_irb_cfg())
        engine._clock = _make_weekday_clock(5, 12)  # Saturday noon
        decision = engine.pre_trade_check(_order(), _account(), [])
        assert decision.verdict == RiskVerdict.DENY
        assert decision.policy_layer == 1
        assert decision.rule == "forex_session"
        assert "weekend" in decision.reason.lower()

    def test_session_deny_friday_after_close(self):
        engine = _make_engine(_irb_cfg())
        engine._clock = _make_weekday_clock(4, 23)  # Friday 23:00
        decision = engine.pre_trade_check(_order(), _account(), [])
        assert decision.verdict == RiskVerdict.DENY
        assert decision.policy_layer == 1

    def test_session_deny_sunday_before_open(self):
        engine = _make_engine(_irb_cfg())
        engine._clock = _make_weekday_clock(6, 21)  # Sunday 21:00
        decision = engine.pre_trade_check(_order(), _account(), [])
        assert decision.verdict == RiskVerdict.DENY

    def test_session_allow_sunday_at_open(self):
        engine = _make_engine(_irb_cfg())
        engine._clock = _make_weekday_clock(6, 22)  # Sunday 22:00
        decision = engine.pre_trade_check(_order(), _account(), [])
        assert decision.verdict == RiskVerdict.ALLOW

    def test_session_allow_weekday(self):
        engine = _make_engine(_irb_cfg())
        engine._clock = _make_weekday_clock(2, 14)  # Wednesday 14:00
        decision = engine.pre_trade_check(_order(), _account(), [])
        assert decision.verdict == RiskVerdict.ALLOW

    def test_session_allow_friday_before_close(self):
        engine = _make_engine(_irb_cfg())
        engine._clock = _make_weekday_clock(4, 20)  # Friday 20:00 (before 21:00 close)
        decision = engine.pre_trade_check(_order(), _account(), [])
        assert decision.verdict == RiskVerdict.ALLOW

    def test_session_deny_friday_at_close(self):
        """Friday 21:00 UTC = 5 PM EST is market close."""
        engine = _make_engine(_irb_cfg())
        engine._clock = _make_weekday_clock(4, 21)  # Friday 21:00 (market close)
        decision = engine.pre_trade_check(_order(), _account(), [])
        assert decision.verdict == RiskVerdict.DENY
        assert decision.policy_layer == 1


# ---------------------------------------------------------------------------
# Policy layer order tests
# ---------------------------------------------------------------------------


class TestPolicyLayerOrder:
    def test_layer0_before_layer1(self):
        """Halt (layer 0) takes priority over session (layer 1)."""
        engine = _make_engine(_irb_cfg())
        engine._clock = _make_weekday_clock(5, 12)  # Saturday
        engine._halt("test halt")
        decision = engine.pre_trade_check(_order(), _account(), [])
        assert decision.policy_layer == 0  # halt, not session

    def test_layer1_before_layer2(self):
        """Session (layer 1) takes priority over gate (layer 2)."""
        engine = _make_engine(_irb_cfg())
        engine._clock = _make_weekday_clock(5, 12)  # Saturday
        bad_order = _order(volume=999.0)  # would fail volume check in gate
        decision = engine.pre_trade_check(
            bad_order,
            _account(),
            [],
        )
        assert decision.policy_layer == 1  # session, not gate

    def test_all_layers_pass(self):
        engine = _make_engine(_irb_cfg())
        engine._clock = _make_weekday_clock(1, 12)
        decision = engine.pre_trade_check(
            _order(),
            _account(),
            [],
        )
        assert decision.verdict == RiskVerdict.ALLOW
        assert decision.policy_layer == -1  # all passed


# ---------------------------------------------------------------------------
# HALT verdict semantics
# ---------------------------------------------------------------------------


class TestHaltVerdict:
    def test_halt_is_denied(self):
        """HALT verdict is a form of denial."""
        engine = _make_engine()
        engine._halt("test")
        decision = engine.pre_trade_check(_order(), _account(), [])
        assert decision.denied is True
        assert decision.halted is True
        assert decision.verdict == RiskVerdict.HALT

    def test_deny_is_not_halted(self):
        """DENY verdict is not a halt."""
        engine = _make_engine(_irb_cfg())
        engine._clock = _make_weekday_clock(5, 12)  # weekend
        decision = engine.pre_trade_check(_order(), _account(), [])
        assert decision.denied is True
        assert decision.halted is False
        assert decision.verdict == RiskVerdict.DENY

    def test_allow_is_neither(self):
        engine = _make_engine()
        decision = engine.pre_trade_check(_order(), _account(), [])
        assert decision.denied is False
        assert decision.halted is False
        assert decision.verdict == RiskVerdict.ALLOW


# ---------------------------------------------------------------------------
# Kill-switch governance
# ---------------------------------------------------------------------------


class TestKillSwitch:
    def test_daily_drawdown_triggers_halt(self):
        engine = _make_engine()
        engine.on_trade_close("p1", "EURUSD", "BUY", 1.0, -5_000, -500, "SL")
        assert engine.halted
        assert "daily" in engine.halt_reason.lower()

    def test_total_drawdown_triggers_halt(self):
        engine = _make_engine()
        # Multiple days of losses to hit total without daily
        engine.on_trade_close("p1", "EURUSD", "BUY", 1.0, -4_000, -400, "SL")
        engine.resume()
        engine.reset_daily(96_000)
        engine.on_trade_close("p2", "EURUSD", "BUY", 1.0, -4_000, -400, "SL")
        engine.resume()
        engine.reset_daily(92_000)
        engine.on_trade_close("p3", "EURUSD", "BUY", 1.0, -3_000, -300, "SL")
        assert engine.halted
        assert "total" in engine.halt_reason.lower()

    def test_daily_reset_does_not_clear_halt(self):
        """kill_switch_policy.md §7: daily reset does NOT auto-resume."""
        engine = _make_engine()
        engine._halt("daily drawdown breach")
        engine.reset_daily(95_000)
        assert engine.halted  # still halted

    def test_resume_clears_halt(self):
        engine = _make_engine()
        engine._halt("test")
        engine.resume()
        assert not engine.halted
        assert engine.halt_reason == ""

    def test_subsequent_trades_blocked_after_halt(self):
        engine = _make_engine()
        engine._halt("drawdown breach")
        decision = engine.pre_trade_check(_order(), _account(), [])
        assert decision.verdict == RiskVerdict.HALT
        # Second call also blocked
        decision2 = engine.pre_trade_check(_order(), _account(), [])
        assert decision2.verdict == RiskVerdict.HALT


# ---------------------------------------------------------------------------
# get_required_actions tests
# ---------------------------------------------------------------------------


class TestRequiredActions:
    def test_no_actions_when_normal(self):
        engine = _make_engine()
        actions = engine.get_required_actions()
        assert actions == [RiskAction.NONE]

    def test_halt_action_when_halted(self):
        engine = _make_engine()
        engine._halt("test")
        actions = engine.get_required_actions()
        assert RiskAction.HALT_TRADING in actions

    def test_flatten_at_critical_total_drawdown(self):
        engine = _make_engine()
        # Day 1: lose $4000 (daily DD 4% < 5%, no halt)
        engine.on_trade_close("p1", "EURUSD", "BUY", 1.0, -4_000, -400, "SL")
        engine.reset_daily(96_000)
        # Day 2: lose $4800 (daily DD = 4800/96000 = 5% → HALT)
        # Total DD = 8800/100000 = 8.8% >= 80% of 10% limit
        engine.on_trade_close("p2", "EURUSD", "BUY", 1.0, -4_800, -480, "SL")
        assert engine.halted
        actions = engine.get_required_actions()
        assert RiskAction.HALT_TRADING in actions
        assert RiskAction.FLATTEN_POSITIONS in actions
        assert RiskAction.CANCEL_PENDING in actions

    def test_no_flatten_below_critical(self):
        engine = _make_engine()
        engine._halt("daily breach")
        # total drawdown is 0 (not critical)
        actions = engine.get_required_actions()
        assert RiskAction.HALT_TRADING in actions
        assert RiskAction.FLATTEN_POSITIONS not in actions


# ---------------------------------------------------------------------------
# Integration: all checks present in ALLOW decision
# ---------------------------------------------------------------------------


class TestCheckCompleteness:
    def test_allow_includes_gate_checks(self):
        engine = _make_engine()
        decision = engine.pre_trade_check(_order(), _account(), [])
        check_names = [c.name for c in decision.checks]
        assert "volume_bounds" in check_names
        assert "stop_loss" in check_names

    def test_session_config_includes_all_layer_checks(self):
        engine = _make_engine(_irb_cfg())
        engine._clock = _make_weekday_clock(1, 12)
        decision = engine.pre_trade_check(_order(), _account(), [])
        check_names = [c.name for c in decision.checks]
        assert "forex_session" in check_names
        assert "volume_bounds" in check_names  # from gate


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_uninitialized_engine_allows(self):
        """If initialize() was never called, pre-trade gate still works."""
        engine = RiskEngine(_cfg())
        decision = engine.pre_trade_check(
            _order(),
            _account(),
            [],
        )
        assert decision.verdict == RiskVerdict.ALLOW

    def test_zero_equity_account(self):
        engine = _make_engine()
        decision = engine.pre_trade_check(
            _order(),
            _account(equity=0, balance=0),
            [],
        )
        assert decision is not None  # doesn't crash
