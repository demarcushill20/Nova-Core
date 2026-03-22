"""Tests for novatrade.monitor.ops_monitor — Phase 7 operational monitoring."""

import datetime as dt
from unittest.mock import AsyncMock

import pytest

from novatrade.config import NovaTradeCfg, RiskConfig
from novatrade.execution.trading_agent import AgentState, TradingAgent
from novatrade.models import (
    AccountMode,
    AccountState,
    HealthState,
    HealthStatus,
    OrderResult,
    OrderSide,
    OrderStatus,
    OrderType,
    PendingOrder,
    Position,
)
from novatrade.monitor.ops_monitor import (
    AlertCategory,
    AlertSeverity,
    CycleResult,
    DailySummary,
    OpsAlert,
    OpsMonitor,
    ReconciliationAction,
    ReconciliationOutcome,
)
from novatrade.risk.risk_engine import RiskEngine
from novatrade.validation.evidence import EvidenceRecorder

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
        dry_run=False,
    )
    defaults.update(overrides)
    return NovaTradeCfg(**defaults)  # type: ignore[arg-type]


def _account(equity: float = 100_000, balance: float = 100_000) -> AccountState:
    return AccountState(balance=balance, equity=equity, mode=AccountMode.DEMO)


def _position(
    position_id: str = "pos1",
    symbol: str = "EURUSD",
    side: OrderSide = OrderSide.BUY,
    volume: float = 0.10,
    open_price: float = 1.1000,
    stop_loss: float = 1.0950,
) -> Position:
    return Position(
        position_id=position_id,
        symbol=symbol,
        side=side,
        volume=volume,
        open_price=open_price,
        stop_loss=stop_loss,
    )


def _pending_order(
    order_id: str = "ord1",
    symbol: str = "EURUSD",
    side: OrderSide = OrderSide.BUY,
) -> PendingOrder:
    return PendingOrder(
        order_id=order_id,
        symbol=symbol,
        side=side,
        order_type=OrderType.STOP,
        volume=0.10,
        open_price=1.1000,
        stop_loss=1.0950,
    )


def _mock_adapter(
    *,
    health: HealthStatus | None = None,
    account: AccountState | None = None,
    positions: list[Position] | None = None,
    orders: list[PendingOrder] | None = None,
    cancel_result: OrderResult | None = None,
    close_result: OrderResult | None = None,
) -> AsyncMock:
    adapter = AsyncMock()
    adapter.health_check = AsyncMock(return_value=health or HealthStatus(state=HealthState.OK, connected=True))
    adapter.get_account = AsyncMock(return_value=account or _account())
    adapter.get_positions = AsyncMock(return_value=positions or [])
    adapter.get_orders = AsyncMock(return_value=orders or [])
    adapter.cancel_order = AsyncMock(
        return_value=cancel_result or OrderResult(ok=True, order_id="ord1", status=OrderStatus.CANCELLED)
    )
    adapter.close_position = AsyncMock(
        return_value=close_result or OrderResult(ok=True, order_id="pos1", status=OrderStatus.FILLED)
    )
    return adapter


def _make_clock(year=2026, month=3, day=17, hour=12, minute=0):
    """Create a fixed clock for testing."""
    fixed = dt.datetime(year, month, day, hour, minute, tzinfo=dt.timezone.utc)
    return lambda: fixed


def _build_monitor(
    adapter=None,
    agent=None,
    risk_engine=None,
    recorder=None,
    clock=None,
    cfg=None,
) -> OpsMonitor:
    """Build an OpsMonitor with sensible defaults."""
    c = cfg or _cfg()
    a = adapter or _mock_adapter()
    if risk_engine is None:
        risk_engine = RiskEngine(c)
        risk_engine.initialize(_account())
    if agent is None:
        agent = TradingAgent(c, a, risk_engine, recorder)
    return OpsMonitor(c, a, agent, risk_engine, recorder, clock=clock or _make_clock())


# ---------------------------------------------------------------------------
# Health checks
# ---------------------------------------------------------------------------


class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_healthy_adapter(self):
        monitor = _build_monitor()
        result = await monitor.run_cycle()
        assert result.health_ok is True
        assert result.health_error == ""

    @pytest.mark.asyncio
    async def test_down_adapter(self):
        adapter = _mock_adapter(health=HealthStatus(state=HealthState.DOWN, connected=False, message="timeout"))
        monitor = _build_monitor(adapter=adapter)
        result = await monitor.run_cycle()
        assert result.health_ok is False
        assert "timeout" in result.health_error

    @pytest.mark.asyncio
    async def test_health_failure_emits_alert(self):
        adapter = _mock_adapter(health=HealthStatus(state=HealthState.DOWN, connected=False, message="disconnected"))
        monitor = _build_monitor(adapter=adapter)
        result = await monitor.run_cycle()
        health_alerts = [a for a in result.alerts if a.category == AlertCategory.HEALTH]
        assert len(health_alerts) >= 1
        assert health_alerts[0].severity == AlertSeverity.CRITICAL

    @pytest.mark.asyncio
    async def test_degraded_adapter(self):
        adapter = _mock_adapter(health=HealthStatus(state=HealthState.DEGRADED, connected=True, message="slow"))
        monitor = _build_monitor(adapter=adapter)
        result = await monitor.run_cycle()
        # DEGRADED is not DOWN but still not fully OK
        assert result.health_ok is True  # adapter reports non-DOWN

    @pytest.mark.asyncio
    async def test_health_exception_handled(self):
        adapter = _mock_adapter()
        adapter.health_check = AsyncMock(side_effect=RuntimeError("kaboom"))
        monitor = _build_monitor(adapter=adapter)
        result = await monitor.run_cycle()
        assert result.health_ok is False
        assert "kaboom" in result.health_error


# ---------------------------------------------------------------------------
# Fill detection
# ---------------------------------------------------------------------------


class TestFillDetection:
    @pytest.mark.asyncio
    async def test_detect_fill_pending_long(self):
        cfg = _cfg()
        adapter = _mock_adapter(
            positions=[_position(side=OrderSide.BUY)],
        )
        risk = RiskEngine(cfg)
        risk.initialize(_account())
        agent = TradingAgent(cfg, adapter, risk)
        # Manually set agent to PENDING_LONG
        agent._state = AgentState.PENDING_LONG
        agent._pending_order_id = "ord1"
        agent._pending_side = OrderSide.BUY

        monitor = _build_monitor(adapter=adapter, agent=agent, risk_engine=risk)
        result = await monitor.run_cycle()

        assert agent.state == AgentState.LONG
        fills = [a for a in result.reconciliation_actions if a.outcome == ReconciliationOutcome.NOTIFY_FILL]
        assert len(fills) == 1

    @pytest.mark.asyncio
    async def test_detect_fill_pending_short(self):
        cfg = _cfg()
        adapter = _mock_adapter(
            positions=[_position(side=OrderSide.SELL, open_price=1.0900, stop_loss=1.0950)],
        )
        risk = RiskEngine(cfg)
        risk.initialize(_account())
        agent = TradingAgent(cfg, adapter, risk)
        agent._state = AgentState.PENDING_SHORT
        agent._pending_order_id = "ord1"
        agent._pending_side = OrderSide.SELL

        monitor = _build_monitor(adapter=adapter, agent=agent, risk_engine=risk)
        await monitor.run_cycle()

        assert agent.state == AgentState.SHORT

    @pytest.mark.asyncio
    async def test_no_fill_when_no_broker_position(self):
        cfg = _cfg()
        adapter = _mock_adapter(
            positions=[],
            orders=[_pending_order()],
        )
        risk = RiskEngine(cfg)
        risk.initialize(_account())
        agent = TradingAgent(cfg, adapter, risk)
        agent._state = AgentState.PENDING_LONG
        agent._pending_order_id = "ord1"

        monitor = _build_monitor(adapter=adapter, agent=agent, risk_engine=risk)
        await monitor.run_cycle()

        assert agent.state == AgentState.PENDING_LONG  # no change


# ---------------------------------------------------------------------------
# Broker close detection
# ---------------------------------------------------------------------------


class TestBrokerCloseDetection:
    @pytest.mark.asyncio
    async def test_detect_broker_close_long(self):
        cfg = _cfg()
        adapter = _mock_adapter(positions=[])  # no positions at broker
        risk = RiskEngine(cfg)
        risk.initialize(_account())
        agent = TradingAgent(cfg, adapter, risk)
        agent._state = AgentState.LONG
        agent._position_id = "pos1"
        agent._position_side = OrderSide.BUY

        monitor = _build_monitor(adapter=adapter, agent=agent, risk_engine=risk)
        result = await monitor.run_cycle()

        assert agent.state == AgentState.FLAT
        closes = [a for a in result.reconciliation_actions if a.outcome == ReconciliationOutcome.NOTIFY_BROKER_CLOSE]
        assert len(closes) == 1

    @pytest.mark.asyncio
    async def test_detect_broker_close_short(self):
        cfg = _cfg()
        adapter = _mock_adapter(positions=[])
        risk = RiskEngine(cfg)
        risk.initialize(_account())
        agent = TradingAgent(cfg, adapter, risk)
        agent._state = AgentState.SHORT
        agent._position_id = "pos1"
        agent._position_side = OrderSide.SELL

        monitor = _build_monitor(adapter=adapter, agent=agent, risk_engine=risk)
        await monitor.run_cycle()

        assert agent.state == AgentState.FLAT

    @pytest.mark.asyncio
    async def test_no_close_when_position_exists(self):
        cfg = _cfg()
        adapter = _mock_adapter(positions=[_position()])
        risk = RiskEngine(cfg)
        risk.initialize(_account())
        agent = TradingAgent(cfg, adapter, risk)
        agent._state = AgentState.LONG
        agent._position_id = "pos1"
        agent._position_side = OrderSide.BUY

        monitor = _build_monitor(adapter=adapter, agent=agent, risk_engine=risk)
        result = await monitor.run_cycle()

        assert agent.state == AgentState.LONG
        closes = [a for a in result.reconciliation_actions if a.outcome == ReconciliationOutcome.NOTIFY_BROKER_CLOSE]
        assert len(closes) == 0


# ---------------------------------------------------------------------------
# Stale pending order detection
# ---------------------------------------------------------------------------


class TestStalePendingDetection:
    @pytest.mark.asyncio
    async def test_stale_pending_resets_to_flat(self):
        cfg = _cfg()
        adapter = _mock_adapter(positions=[], orders=[])  # nothing at broker
        risk = RiskEngine(cfg)
        risk.initialize(_account())
        agent = TradingAgent(cfg, adapter, risk)
        agent._state = AgentState.PENDING_LONG
        agent._pending_order_id = "ord_gone"

        monitor = _build_monitor(adapter=adapter, agent=agent, risk_engine=risk)
        result = await monitor.run_cycle()

        assert agent.state == AgentState.FLAT
        stale = [a for a in result.reconciliation_actions if a.outcome == ReconciliationOutcome.CANCEL_STALE_PENDING]
        assert len(stale) == 1


# ---------------------------------------------------------------------------
# Orphan detection
# ---------------------------------------------------------------------------


class TestOrphanDetection:
    @pytest.mark.asyncio
    async def test_orphan_position_detected(self):
        cfg = _cfg()
        adapter = _mock_adapter(positions=[_position()])
        risk = RiskEngine(cfg)
        risk.initialize(_account())
        agent = TradingAgent(cfg, adapter, risk)
        # Agent is FLAT but broker has position
        assert agent.state == AgentState.FLAT

        monitor = _build_monitor(adapter=adapter, agent=agent, risk_engine=risk)
        result = await monitor.run_cycle()

        orphans = [a for a in result.reconciliation_actions if a.outcome == ReconciliationOutcome.ORPHAN_POSITION]
        assert len(orphans) == 1

    @pytest.mark.asyncio
    async def test_orphan_pending_detected(self):
        cfg = _cfg()
        adapter = _mock_adapter(orders=[_pending_order()])
        risk = RiskEngine(cfg)
        risk.initialize(_account())
        agent = TradingAgent(cfg, adapter, risk)

        monitor = _build_monitor(adapter=adapter, agent=agent, risk_engine=risk)
        result = await monitor.run_cycle()

        orphans = [a for a in result.reconciliation_actions if a.outcome == ReconciliationOutcome.ORPHAN_PENDING]
        assert len(orphans) == 1


# ---------------------------------------------------------------------------
# Consistent state (no action needed)
# ---------------------------------------------------------------------------


class TestConsistentState:
    @pytest.mark.asyncio
    async def test_flat_no_broker_state(self):
        monitor = _build_monitor()
        result = await monitor.run_cycle()
        no_action = [a for a in result.reconciliation_actions if a.outcome == ReconciliationOutcome.NO_ACTION]
        assert len(no_action) == 1

    @pytest.mark.asyncio
    async def test_long_with_matching_position(self):
        cfg = _cfg()
        adapter = _mock_adapter(positions=[_position()])
        risk = RiskEngine(cfg)
        risk.initialize(_account())
        agent = TradingAgent(cfg, adapter, risk)
        agent._state = AgentState.LONG
        agent._position_id = "pos1"
        agent._position_side = OrderSide.BUY

        monitor = _build_monitor(adapter=adapter, agent=agent, risk_engine=risk)
        result = await monitor.run_cycle()

        assert agent.state == AgentState.LONG
        no_action = [a for a in result.reconciliation_actions if a.outcome == ReconciliationOutcome.NO_ACTION]
        assert len(no_action) == 1


# ---------------------------------------------------------------------------
# Daily reset
# ---------------------------------------------------------------------------


class TestDailyReset:
    @pytest.mark.asyncio
    async def test_daily_reset_on_new_day(self):
        cfg = _cfg()
        adapter = _mock_adapter(account=_account(equity=100_500))
        risk = RiskEngine(cfg)
        risk.initialize(_account())
        # Pretend last reset was yesterday
        clock = _make_clock(day=17)
        monitor = _build_monitor(adapter=adapter, risk_engine=risk, clock=clock)
        monitor._last_reset_date = "2026-03-16"  # yesterday

        result = await monitor.run_cycle()
        assert result.daily_reset_performed is True

    @pytest.mark.asyncio
    async def test_no_double_reset_same_day(self):
        clock = _make_clock(day=17)
        monitor = _build_monitor(clock=clock)
        monitor._last_reset_date = "2026-03-17"  # already reset today

        result = await monitor.run_cycle()
        assert result.daily_reset_performed is False

    @pytest.mark.asyncio
    async def test_reset_does_not_clear_halt(self):
        cfg = _cfg()
        adapter = _mock_adapter(account=_account(equity=95_000))
        risk = RiskEngine(cfg)
        risk.initialize(_account())
        risk._halt("test halt")

        clock = _make_clock(day=17)
        monitor = _build_monitor(adapter=adapter, risk_engine=risk, clock=clock)
        monitor._last_reset_date = "2026-03-16"

        result = await monitor.run_cycle()
        assert result.daily_reset_performed is True
        assert risk.halted is True  # halt NOT cleared


class TestDailyResetCEST:
    """CEST/CET-aware daily reset — FTMO resets at midnight Prague time.

    CET  = UTC+1 (winter, Nov–Mar)
    CEST = UTC+2 (summer, Mar–Oct)
    DST transition 2026: March 29 at 02:00 Prague → 03:00 Prague (spring forward)
    """

    @pytest.mark.asyncio
    async def test_reset_at_midnight_cet(self):
        """Before DST: midnight Prague = 23:00 UTC previous day.
        At 22:59 UTC on Mar 16, Prague date is still Mar 16.
        At 23:00 UTC on Mar 16, Prague date becomes Mar 17.
        """
        cfg = _cfg(risk=RiskConfig(daily_reset_tz="Europe/Prague"))
        adapter = _mock_adapter(account=_account(equity=100_000))
        risk = RiskEngine(cfg)
        risk.initialize(_account())

        # 22:59 UTC on Mar 16 → Prague 23:59 Mar 16 (still same day)
        clock_before = _make_clock(year=2026, month=3, day=16, hour=22, minute=59)
        monitor = _build_monitor(adapter=adapter, risk_engine=risk, clock=clock_before, cfg=cfg)
        monitor._last_reset_date = "2026-03-16"
        result = await monitor.run_cycle()
        assert result.daily_reset_performed is False

        # 23:00 UTC on Mar 16 → Prague 00:00 Mar 17 (new day → reset)
        clock_after = _make_clock(year=2026, month=3, day=16, hour=23, minute=0)
        risk2 = RiskEngine(cfg)
        risk2.initialize(_account())
        monitor2 = _build_monitor(adapter=adapter, risk_engine=risk2, clock=clock_after, cfg=cfg)
        monitor2._last_reset_date = "2026-03-16"
        result2 = await monitor2.run_cycle()
        assert result2.daily_reset_performed is True

    @pytest.mark.asyncio
    async def test_reset_at_midnight_cest_after_dst(self):
        """After DST (Mar 29+): midnight Prague = 22:00 UTC.
        At 21:59 UTC on Mar 30, Prague date is still Mar 30.
        At 22:00 UTC on Mar 30, Prague date becomes Mar 31.
        """
        cfg = _cfg(risk=RiskConfig(daily_reset_tz="Europe/Prague"))
        adapter = _mock_adapter(account=_account(equity=100_000))

        # 21:59 UTC on Mar 30 → Prague 23:59 Mar 30 (same day)
        risk1 = RiskEngine(cfg)
        risk1.initialize(_account())
        clock_before = _make_clock(year=2026, month=3, day=30, hour=21, minute=59)
        monitor = _build_monitor(adapter=adapter, risk_engine=risk1, clock=clock_before, cfg=cfg)
        monitor._last_reset_date = "2026-03-30"
        result = await monitor.run_cycle()
        assert result.daily_reset_performed is False

        # 22:00 UTC on Mar 30 → Prague 00:00 Mar 31 (new day → reset)
        risk2 = RiskEngine(cfg)
        risk2.initialize(_account())
        clock_after = _make_clock(year=2026, month=3, day=30, hour=22, minute=0)
        monitor2 = _build_monitor(adapter=adapter, risk_engine=risk2, clock=clock_after, cfg=cfg)
        monitor2._last_reset_date = "2026-03-30"
        result2 = await monitor2.run_cycle()
        assert result2.daily_reset_performed is True

    @pytest.mark.asyncio
    async def test_dst_transition_day_march_29(self):
        """March 29 2026: clocks spring forward at 02:00 → 03:00 Prague.
        Midnight Prague on Mar 29 = 23:00 UTC Mar 28 (still CET).
        Midnight Prague on Mar 30 = 22:00 UTC Mar 29 (now CEST).
        """
        cfg = _cfg(risk=RiskConfig(daily_reset_tz="Europe/Prague"))
        adapter = _mock_adapter(account=_account(equity=100_000))

        # 23:00 UTC Mar 28 → Prague 00:00 Mar 29 (CET, UTC+1)
        risk1 = RiskEngine(cfg)
        risk1.initialize(_account())
        clock1 = _make_clock(year=2026, month=3, day=28, hour=23, minute=0)
        m1 = _build_monitor(adapter=adapter, risk_engine=risk1, clock=clock1, cfg=cfg)
        m1._last_reset_date = "2026-03-28"
        r1 = await m1.run_cycle()
        assert r1.daily_reset_performed is True  # new Prague day

        # 22:00 UTC Mar 29 → Prague 00:00 Mar 30 (CEST, UTC+2)
        risk2 = RiskEngine(cfg)
        risk2.initialize(_account())
        clock2 = _make_clock(year=2026, month=3, day=29, hour=22, minute=0)
        m2 = _build_monitor(adapter=adapter, risk_engine=risk2, clock=clock2, cfg=cfg)
        m2._last_reset_date = "2026-03-29"
        r2 = await m2.run_cycle()
        assert r2.daily_reset_performed is True  # new Prague day (CEST now)

    @pytest.mark.asyncio
    async def test_utc_would_reset_wrong_time_without_tz(self):
        """Demonstrate that UTC-based reset fires at the wrong hour.
        At 00:01 UTC on Mar 30 (after CEST), Prague time is 02:01 Mar 30.
        A UTC-based reset would fire here, but FTMO's day started at 22:00 UTC.
        With TZ-aware logic, this is NOT a new Prague day if already reset for Mar 30.
        """
        cfg = _cfg(risk=RiskConfig(daily_reset_tz="Europe/Prague"))
        adapter = _mock_adapter(account=_account(equity=100_000))
        risk = RiskEngine(cfg)
        risk.initialize(_account())

        # 00:01 UTC Mar 30 → Prague 02:01 Mar 30 (same Prague day)
        clock = _make_clock(year=2026, month=3, day=30, hour=0, minute=1)
        monitor = _build_monitor(adapter=adapter, risk_engine=risk, clock=clock, cfg=cfg)
        monitor._last_reset_date = "2026-03-30"  # already reset for this Prague day
        result = await monitor.run_cycle()
        assert result.daily_reset_performed is False

    @pytest.mark.asyncio
    async def test_default_tz_is_prague(self):
        """Default daily_reset_tz should be Europe/Prague."""
        cfg = _cfg()
        assert cfg.risk.daily_reset_tz == "Europe/Prague"


# ---------------------------------------------------------------------------
# Risk action execution
# ---------------------------------------------------------------------------


class TestRiskActionExecution:
    @pytest.mark.asyncio
    async def test_cancel_pending_when_halted(self):
        cfg = _cfg()
        adapter = _mock_adapter(orders=[_pending_order()])
        risk = RiskEngine(cfg)
        risk.initialize(_account())
        risk._halt("drawdown breach")
        # Simulate critical total drawdown to trigger FLATTEN + CANCEL
        risk._total_dd.current_drawdown_pct = 9.0  # >= 80% of 10%

        agent = TradingAgent(cfg, adapter, risk)
        monitor = _build_monitor(adapter=adapter, agent=agent, risk_engine=risk)

        results = await monitor.execute_pending_actions()
        cancel_results = [r for r in results if "cancel" in r.action_taken.lower() or "Cancelled" in r.detail]
        assert len(cancel_results) >= 1
        adapter.cancel_order.assert_called()

    @pytest.mark.asyncio
    async def test_flatten_positions_when_halted(self):
        cfg = _cfg()
        adapter = _mock_adapter(positions=[_position()])
        risk = RiskEngine(cfg)
        risk.initialize(_account())
        risk._halt("total drawdown")
        risk._total_dd.current_drawdown_pct = 9.0

        agent = TradingAgent(cfg, adapter, risk)
        monitor = _build_monitor(adapter=adapter, agent=agent, risk_engine=risk)

        results = await monitor.execute_pending_actions()
        flatten_results = [r for r in results if "flatten" in r.action_taken.lower() or "Closed" in r.detail]
        assert len(flatten_results) >= 1
        adapter.close_position.assert_called()

    @pytest.mark.asyncio
    async def test_no_actions_when_not_halted(self):
        monitor = _build_monitor()
        results = await monitor.execute_pending_actions()
        assert (
            all("none" in r.action_taken.lower() or r.outcome == ReconciliationOutcome.NO_ACTION for r in results)
            or len(results) == 0
        )

    @pytest.mark.asyncio
    async def test_cancel_failure_reported(self):
        cfg = _cfg()
        adapter = _mock_adapter(
            orders=[_pending_order()],
            cancel_result=OrderResult(ok=False, error="timeout"),
        )
        risk = RiskEngine(cfg)
        risk.initialize(_account())
        risk._halt("test")
        risk._total_dd.current_drawdown_pct = 9.0

        agent = TradingAgent(cfg, adapter, risk)
        monitor = _build_monitor(adapter=adapter, agent=agent, risk_engine=risk)

        results = await monitor.execute_pending_actions()
        failed = [r for r in results if not r.success]
        assert len(failed) >= 1

    @pytest.mark.asyncio
    async def test_flatten_failure_reported(self):
        cfg = _cfg()
        adapter = _mock_adapter(
            positions=[_position()],
            close_result=OrderResult(ok=False, error="timeout"),
        )
        risk = RiskEngine(cfg)
        risk.initialize(_account())
        risk._halt("test")
        risk._total_dd.current_drawdown_pct = 9.0

        agent = TradingAgent(cfg, adapter, risk)
        monitor = _build_monitor(adapter=adapter, agent=agent, risk_engine=risk)

        results = await monitor.execute_pending_actions()
        failed = [r for r in results if not r.success]
        assert len(failed) >= 1


# ---------------------------------------------------------------------------
# Risk alerts
# ---------------------------------------------------------------------------


class TestRiskAlerts:
    @pytest.mark.asyncio
    async def test_halt_emits_critical_alert(self):
        cfg = _cfg()
        adapter = _mock_adapter()
        risk = RiskEngine(cfg)
        risk.initialize(_account())
        risk._halt("daily limit")

        monitor = _build_monitor(adapter=adapter, risk_engine=risk)
        result = await monitor.run_cycle()

        risk_alerts = [a for a in result.alerts if a.category == AlertCategory.RISK]
        assert len(risk_alerts) >= 1
        assert risk_alerts[0].severity == AlertSeverity.CRITICAL

    @pytest.mark.asyncio
    async def test_no_risk_alert_when_normal(self):
        monitor = _build_monitor()
        result = await monitor.run_cycle()

        risk_alerts = [a for a in result.alerts if a.category == AlertCategory.RISK]
        assert len(risk_alerts) == 0


# ---------------------------------------------------------------------------
# Daily summary
# ---------------------------------------------------------------------------


class TestDailySummary:
    @pytest.mark.asyncio
    async def test_summary_structure(self):
        monitor = _build_monitor()
        await monitor.run_cycle()
        summary = monitor.generate_daily_summary()

        assert summary.date == "2026-03-17"
        assert summary.cycles_run >= 1
        assert summary.current_state == "FLAT"

    @pytest.mark.asyncio
    async def test_summary_to_dict(self):
        monitor = _build_monitor()
        await monitor.run_cycle()
        summary = monitor.generate_daily_summary()
        d = summary.to_dict()

        assert "date" in d
        assert "health" in d
        assert "risk" in d
        assert "actions" in d
        assert "reconciliation" in d
        assert "alerts" in d
        assert "state" in d

    @pytest.mark.asyncio
    async def test_write_daily_summary(self, tmp_path):
        monitor = _build_monitor()
        await monitor.run_cycle()
        summary = monitor.generate_daily_summary()
        path = monitor.write_daily_summary(summary, tmp_path)

        assert path.exists()
        import json

        data = json.loads(path.read_text())
        assert data["date"] == "2026-03-17"

    @pytest.mark.asyncio
    async def test_summary_counts_fills(self):
        cfg = _cfg()
        adapter = _mock_adapter(positions=[_position()])
        risk = RiskEngine(cfg)
        risk.initialize(_account())
        agent = TradingAgent(cfg, adapter, risk)
        agent._state = AgentState.PENDING_LONG
        agent._pending_order_id = "ord1"
        agent._pending_side = OrderSide.BUY

        monitor = _build_monitor(adapter=adapter, agent=agent, risk_engine=risk)
        await monitor.run_cycle()
        summary = monitor.generate_daily_summary()
        assert summary.fills_detected == 1

    @pytest.mark.asyncio
    async def test_reset_daily_tracking(self):
        monitor = _build_monitor()
        await monitor.run_cycle()
        monitor.reset_daily_tracking()
        summary = monitor.generate_daily_summary()
        assert summary.cycles_run == 0


# ---------------------------------------------------------------------------
# Evidence recording
# ---------------------------------------------------------------------------


class TestEvidenceRecording:
    @pytest.mark.asyncio
    async def test_cycle_records_health(self, tmp_path):
        recorder = EvidenceRecorder(tmp_path / "evidence.jsonl")
        monitor = _build_monitor(recorder=recorder)
        await monitor.run_cycle()

        records = recorder.load()
        health_records = [r for r in records if r.event_type.value == "HEALTH_SNAPSHOT"]
        assert len(health_records) >= 1

    @pytest.mark.asyncio
    async def test_cycle_records_monitoring_event(self, tmp_path):
        recorder = EvidenceRecorder(tmp_path / "evidence.jsonl")
        monitor = _build_monitor(recorder=recorder)
        await monitor.run_cycle()

        records = recorder.load()
        monitoring_records = [r for r in records if r.event_type.value == "MONITORING"]
        assert len(monitoring_records) >= 1

    @pytest.mark.asyncio
    async def test_fill_recorded_in_evidence(self, tmp_path):
        recorder = EvidenceRecorder(tmp_path / "evidence.jsonl")
        cfg = _cfg()
        adapter = _mock_adapter(positions=[_position()])
        risk = RiskEngine(cfg)
        risk.initialize(_account())
        agent = TradingAgent(cfg, adapter, risk, recorder)
        agent._state = AgentState.PENDING_LONG
        agent._pending_order_id = "ord1"
        agent._pending_side = OrderSide.BUY

        monitor = _build_monitor(adapter=adapter, agent=agent, risk_engine=risk, recorder=recorder)
        await monitor.run_cycle()

        records = recorder.load()
        fill_events = [
            r
            for r in records
            if r.event_type.value == "MONITORING"
            and r.data.get("monitoring_event") == "ALERT"
            and "Fill" in r.data.get("message", "")
        ]
        assert len(fill_events) >= 1


# ---------------------------------------------------------------------------
# CycleResult and OpsAlert dataclasses
# ---------------------------------------------------------------------------


class TestDataClasses:
    def test_cycle_result_ok_default(self):
        r = CycleResult()
        assert r.ok is True

    def test_cycle_result_ok_with_failed_recon(self):
        r = CycleResult()
        r.reconciliation_actions.append(
            ReconciliationAction(
                outcome=ReconciliationOutcome.ORPHAN_POSITION,
                detail="orphan",
                success=False,
            )
        )
        assert r.ok is False

    def test_cycle_result_to_dict(self):
        r = CycleResult()
        d = r.to_dict()
        assert "health_ok" in d
        assert "reconciliation_actions" in d
        assert "alerts" in d

    def test_alert_to_dict(self):
        a = OpsAlert(
            severity=AlertSeverity.WARNING,
            category=AlertCategory.HEALTH,
            message="test",
        )
        d = a.to_dict()
        assert d["severity"] == "WARNING"
        assert d["category"] == "HEALTH"

    def test_daily_summary_to_dict(self):
        s = DailySummary(date="2026-03-17")
        d = s.to_dict()
        assert d["date"] == "2026-03-17"
        assert "health" in d


# ---------------------------------------------------------------------------
# Degraded capability
# ---------------------------------------------------------------------------


class TestDegradedCapability:
    @pytest.mark.asyncio
    async def test_reconcile_skipped_on_health_failure(self):
        adapter = _mock_adapter(health=HealthStatus(state=HealthState.DOWN, connected=False, message="offline"))
        monitor = _build_monitor(adapter=adapter)
        result = await monitor.run_cycle()

        # Should not attempt reconciliation when health is down
        recon = [a for a in result.reconciliation_actions if a.outcome != ReconciliationOutcome.NO_ACTION]
        assert len(recon) == 0

    @pytest.mark.asyncio
    async def test_get_positions_failure_handled(self):
        """get_positions failure during health snapshot degrades health."""
        adapter = _mock_adapter()
        adapter.get_positions = AsyncMock(side_effect=ConnectionError("lost"))
        monitor = _build_monitor(adapter=adapter)
        result = await monitor.run_cycle()

        # Health snapshot catches the failure — health is degraded
        assert result.health_ok is False
        # Reconciliation is skipped when health is not OK
        assert len(result.reconciliation_actions) == 0

    @pytest.mark.asyncio
    async def test_get_orders_failure_graceful(self):
        """get_orders failure doesn't crash reconciliation."""
        adapter = _mock_adapter()
        adapter.get_orders = AsyncMock(side_effect=NotImplementedError("not supported"))
        monitor = _build_monitor(adapter=adapter)
        result = await monitor.run_cycle()
        # Should still complete — get_orders failure is non-fatal
        assert result.health_ok is True


# ---------------------------------------------------------------------------
# MetaApiAdapter cancel_order + get_orders (model-level tests)
# ---------------------------------------------------------------------------


class TestPendingOrderModel:
    def test_pending_order_creation(self):
        order = PendingOrder(
            order_id="123",
            symbol="EURUSD",
            side=OrderSide.BUY,
            order_type=OrderType.STOP,
            volume=0.10,
            open_price=1.1000,
        )
        assert order.order_id == "123"
        assert order.side == OrderSide.BUY

    def test_pending_order_with_sl_tp(self):
        order = PendingOrder(
            order_id="456",
            symbol="GBPUSD",
            side=OrderSide.SELL,
            order_type=OrderType.STOP,
            volume=0.05,
            open_price=1.2500,
            stop_loss=1.2550,
            take_profit=1.2400,
        )
        assert order.stop_loss == 1.2550
        assert order.take_profit == 1.2400
