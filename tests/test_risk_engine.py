"""Tests for novatrade.risk.risk_engine — comprehensive risk management."""

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
from novatrade.risk.risk_engine import (
    DrawdownState,
    RiskEngine,
    RiskLevel,
    RiskSnapshot,
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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRiskEngineInitialization:
    def test_init(self):
        engine = RiskEngine(_cfg())
        assert engine.halted is False
        assert engine.current_equity == 0

    def test_initialize_sets_equity(self):
        engine = RiskEngine(_cfg())
        engine.initialize(_account(equity=100_000))
        assert engine.current_equity == 100_000

    def test_initialize_sets_drawdown_refs(self):
        engine = RiskEngine(_cfg())
        engine.initialize(_account(equity=50_000))
        snap = engine.get_risk_snapshot()
        assert snap.equity == 50_000
        assert snap.daily_drawdown.reference_equity == 50_000
        assert snap.total_drawdown.reference_equity == 50_000


class TestPreTradeCheck:
    def test_allow_normal_trade(self):
        engine = RiskEngine(_cfg())
        engine.initialize(_account())
        decision = engine.pre_trade_check(
            _order(),
            _account(),
            [],
        )
        assert decision.verdict == RiskVerdict.ALLOW

    def test_halt_when_halted(self):
        engine = RiskEngine(_cfg())
        engine.initialize(_account())
        engine._halt("test halt")
        decision = engine.pre_trade_check(
            _order(),
            _account(),
            [],
        )
        assert decision.verdict == RiskVerdict.HALT
        assert decision.denied  # HALT is a form of denial
        assert decision.halted
        assert decision.policy_layer == 0
        assert "halted" in decision.reason.lower()

    def test_includes_gate_checks(self):
        engine = RiskEngine(_cfg())
        engine.initialize(_account())
        decision = engine.pre_trade_check(
            _order(),
            _account(),
            [],
        )
        check_names = [c.name for c in decision.checks]
        assert "volume_bounds" in check_names


class TestTradeLifecycle:
    def test_on_trade_fill(self):
        engine = RiskEngine(_cfg())
        engine.initialize(_account())
        engine.on_trade_fill("pos1", "EURUSD", OrderSide.BUY, 0.10, 1.1000, 1.0950)
        assert "pos1" in engine._position_risks

    def test_on_trade_close_updates_equity(self):
        engine = RiskEngine(_cfg())
        engine.initialize(_account(equity=100_000))
        engine.on_trade_fill("pos1", "EURUSD", OrderSide.BUY, 0.10, 1.1000, 1.0950)
        engine.on_trade_close("pos1", "EURUSD", "BUY", 0.10, 500.0, 50.0, "TRAILING_STOP")
        assert engine.current_equity == 100_500

    def test_on_trade_close_removes_position(self):
        engine = RiskEngine(_cfg())
        engine.initialize(_account())
        engine.on_trade_fill("pos1", "EURUSD", OrderSide.BUY, 0.10, 1.1000, 1.0950)
        engine.on_trade_close("pos1", "EURUSD", "BUY", 0.10, -300.0, -30.0, "STOP_LOSS")
        assert "pos1" not in engine._position_risks

    def test_trade_history_recorded(self):
        engine = RiskEngine(_cfg())
        engine.initialize(_account())
        engine.on_trade_close("pos1", "EURUSD", "BUY", 0.10, 100.0, 10.0, "TRAILING_STOP")
        assert len(engine.trade_history) == 1
        assert engine.trade_history[0].pnl_usd == 100.0


class TestPositionRiskTracking:
    def test_trailing_stop_long_tightens(self):
        engine = RiskEngine(_cfg())
        engine.initialize(_account())
        engine.on_trade_fill("pos1", "EURUSD", OrderSide.BUY, 0.10, 1.1000, 1.0950)

        # Update with higher price
        prs = engine.update_position_risk("pos1", 1.1100, 0.0020, bar_index=5)
        assert prs is not None
        # Trail = 1.1100 - 1.5 * 0.0020 = 1.1100 - 0.003 = 1.1070
        assert prs.current_stop == pytest.approx(1.1070, abs=0.0001)

    def test_trailing_stop_short_tightens(self):
        engine = RiskEngine(_cfg())
        engine.initialize(_account())
        engine.on_trade_fill("pos1", "EURUSD", OrderSide.SELL, 0.10, 1.1000, 1.1050)

        prs = engine.update_position_risk("pos1", 1.0900, 0.0020, bar_index=5)
        assert prs is not None
        # Trail = 1.0900 + 1.5 * 0.0020 = 1.0930
        assert prs.current_stop == pytest.approx(1.0930, abs=0.0001)

    def test_trailing_stop_never_widens(self):
        engine = RiskEngine(_cfg())
        engine.initialize(_account())
        engine.on_trade_fill("pos1", "EURUSD", OrderSide.BUY, 0.10, 1.1000, 1.0950)

        # Move up
        engine.update_position_risk("pos1", 1.1100, 0.0020, bar_index=5)
        stop_after_up = engine.get_trailing_stop("pos1")

        # Price pulls back — stop should NOT widen
        engine.update_position_risk("pos1", 1.1050, 0.0020, bar_index=6)
        stop_after_down = engine.get_trailing_stop("pos1")

        assert stop_after_down >= stop_after_up

    def test_time_stop_check(self):
        engine = RiskEngine(_cfg())
        engine.initialize(_account())
        engine.on_trade_fill("pos1", "EURUSD", OrderSide.BUY, 0.10, 1.1000, 1.0950)
        engine._position_risks["pos1"].entry_bar = 0

        engine.update_position_risk("pos1", 1.1050, 0.0020, bar_index=39)
        assert not engine.should_exit_time_stop("pos1", max_bars=40)

        engine.update_position_risk("pos1", 1.1050, 0.0020, bar_index=40)
        assert engine.should_exit_time_stop("pos1", max_bars=40)

    def test_unknown_position_returns_none(self):
        engine = RiskEngine(_cfg())
        engine.initialize(_account())
        assert engine.update_position_risk("nonexistent", 1.1, 0.002, 5) is None
        assert engine.get_trailing_stop("nonexistent") is None


class TestDrawdownState:
    def test_update_tracks_peak(self):
        dd = DrawdownState(
            reference_equity=100_000,
            current_equity=100_000,
            peak_equity=100_000,
        )
        dd.update(105_000)
        assert dd.peak_equity == 105_000
        assert dd.current_drawdown_pct == 0.0

    def test_update_tracks_drawdown(self):
        dd = DrawdownState(
            reference_equity=100_000,
            current_equity=100_000,
            peak_equity=100_000,
        )
        dd.update(95_000)
        assert dd.current_drawdown_pct == 5.0
        assert dd.current_drawdown_usd == 5_000

    def test_max_drawdown_recorded(self):
        dd = DrawdownState(
            reference_equity=100_000,
            current_equity=100_000,
            peak_equity=100_000,
        )
        dd.update(97_000)
        dd.update(99_000)  # recovery
        dd.update(96_000)  # new low from peak 100000
        assert dd.max_drawdown_pct == 4.0
        assert dd.max_drawdown_usd == 4_000


class TestRiskSnapshot:
    def test_snapshot_structure(self):
        engine = RiskEngine(_cfg())
        engine.initialize(_account())
        snap = engine.get_risk_snapshot(_account())
        assert snap.risk_level == RiskLevel.NORMAL
        assert snap.halted is False
        assert snap.equity == 100_000

    def test_snapshot_to_dict(self):
        snap = RiskSnapshot(equity=100_000)
        d = snap.to_dict()
        assert "equity" in d
        assert "risk_level" in d
        assert "halted" in d

    def test_elevated_risk_level(self):
        engine = RiskEngine(_cfg())
        engine.initialize(_account(equity=100_000))
        # Simulate 3% daily drawdown (> 50% of 5% limit)
        engine._daily_dd.update(100_000)
        engine._daily_dd.update(97_000)
        snap = engine.get_risk_snapshot(_account(equity=97_000))
        assert snap.risk_level in (RiskLevel.ELEVATED, RiskLevel.CRITICAL)


class TestDailyReset:
    def test_reset_clears_daily_state(self):
        engine = RiskEngine(_cfg())
        engine.initialize(_account(equity=100_000))
        engine.on_trade_close("p1", "EURUSD", "BUY", 0.1, 500, 50, "TRAIL")

        engine.reset_daily(100_500)
        snap = engine.get_risk_snapshot()
        assert snap.trades_today == 0
        assert snap.pnl_today_usd == 0


class TestHaltAndResume:
    def test_halt_blocks_trades(self):
        engine = RiskEngine(_cfg())
        engine.initialize(_account())
        engine._halt("test")
        assert engine.halted is True
        decision = engine.pre_trade_check(_order(), _account(), [])
        assert decision.denied
        assert decision.verdict == RiskVerdict.HALT

    def test_resume_allows_trades(self):
        engine = RiskEngine(_cfg())
        engine.initialize(_account())
        engine._halt("test")
        engine.resume()
        assert engine.halted is False
        decision = engine.pre_trade_check(_order(), _account(), [])
        assert decision.verdict == RiskVerdict.ALLOW

    def test_auto_halt_on_daily_drawdown(self):
        engine = RiskEngine(_cfg())
        engine.initialize(_account(equity=100_000))
        # Lose 5% (daily limit)
        engine.on_trade_close("p1", "EURUSD", "BUY", 1.0, -5_000, -500, "SL")
        assert engine.halted is True
        assert "daily" in engine.halt_reason.lower()

    def test_auto_halt_on_total_drawdown(self):
        engine = RiskEngine(_cfg())
        engine.initialize(_account(equity=100_000))
        # Spread losses across "days" to avoid hitting daily limit first
        engine.on_trade_close("p1", "EURUSD", "BUY", 1.0, -4_000, -400, "SL")
        engine.resume()  # clear daily halt if triggered
        engine.reset_daily(96_000)  # new day
        engine.on_trade_close("p2", "EURUSD", "BUY", 1.0, -4_000, -400, "SL")
        engine.resume()
        engine.reset_daily(92_000)  # new day
        engine.on_trade_close("p3", "EURUSD", "BUY", 1.0, -3_000, -300, "SL")
        assert engine.halted is True
        assert "total" in engine.halt_reason.lower()


class TestMFEMAETracking:
    def test_mfe_tracked(self):
        engine = RiskEngine(_cfg())
        engine.initialize(_account())
        engine.on_trade_fill("pos1", "EURUSD", OrderSide.BUY, 0.10, 1.1000, 1.0950)

        prs = engine.update_position_risk("pos1", 1.1050, 0.002, 5)
        assert prs.max_favorable_excursion == pytest.approx(50.0, abs=1.0)

    def test_mae_tracked(self):
        engine = RiskEngine(_cfg())
        engine.initialize(_account())
        engine.on_trade_fill("pos1", "EURUSD", OrderSide.BUY, 0.10, 1.1000, 1.0950)

        prs = engine.update_position_risk("pos1", 1.0970, 0.002, 5)
        assert prs.max_adverse_excursion == pytest.approx(30.0, abs=1.0)


# ---------------------------------------------------------------------------
# FTMO compliance wiring
# ---------------------------------------------------------------------------


class TestRiskEngineFtmoWiring:
    """Verify FTMO compliance features are properly wired through RiskEngine."""

    def test_on_trade_fill_passes_volume_to_lot_checker(self):
        """on_trade_fill must pass volume to PreTradeGate.record_trade."""
        engine = RiskEngine(_cfg())
        engine.initialize(_account())
        initial_count = len(engine._gate._lot_checker._history)
        engine.on_trade_fill("pos1", "EURUSD", OrderSide.BUY, 0.15, 1.1000, 1.0950)

        # The lot checker should have one new entry with the correct volume
        checker = engine._gate._lot_checker
        assert len(checker._history) == initial_count + 1
        assert checker._history[-1].volume == 0.15

    def test_record_server_request_increments_counter(self):
        """record_server_request should increment the FTMO request counter."""
        engine = RiskEngine(_cfg())
        engine.initialize(_account())
        initial_count = engine._gate._request_counter._count

        engine.record_server_request("modify_sl")
        engine.record_server_request("cancel_order")

        assert engine._gate._request_counter._count == initial_count + 2

    def test_save_ftmo_state_delegates_to_gate(self):
        """save_ftmo_state should persist all three FTMO compliance states."""
        from unittest.mock import patch

        from novatrade.risk.ftmo_compliance import (
            LotSizeConsistencyChecker,
            ServerRequestCounter,
            TradingDaysTracker,
        )

        engine = RiskEngine(_cfg())
        engine.initialize(_account())

        with (
            patch.object(LotSizeConsistencyChecker, "save_state") as lot_save,
            patch.object(ServerRequestCounter, "save_state") as req_save,
            patch.object(TradingDaysTracker, "save_state") as days_save,
        ):
            engine.save_ftmo_state()
            lot_save.assert_called_once()
            req_save.assert_called_once()
            days_save.assert_called_once()

    def test_on_trade_fill_records_trading_day(self):
        """on_trade_fill should record the trading day for min-days tracker."""
        engine = RiskEngine(_cfg())
        engine.initialize(_account())
        engine.on_trade_fill("pos1", "EURUSD", OrderSide.BUY, 0.10, 1.1000, 1.0950)

        assert engine._gate._days_tracker.days_traded == 1
