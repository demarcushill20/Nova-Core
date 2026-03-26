"""Tests for anti-EA-detection measures: rollover dead zone, entry jitter, lot micro-variation.

These features reduce the algorithmic fingerprint of NovaTrade orders to avoid
prop-firm EA detection and improve fill quality during adverse market conditions.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from novatrade.config import FtmoProfile, NovaTradeCfg, RiskConfig
from novatrade.execution.live_trading_agent import LiveTradingAgent
from novatrade.execution.trading_agent import AgentResult, AgentState
from novatrade.models import (
    AccountMode,
    AccountState,
    OrderRequest,
    OrderSide,
    OrderType,
)
from novatrade.risk.position_sizer import PositionSizer
from novatrade.risk.pre_trade_gate import PreTradeGate
from novatrade.strategy.live_engine import LiveSignal, SignalType

# ---------------------------------------------------------------------------
# Shared fixtures
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


# ===========================================================================
# 1. Rollover Dead Zone Gate
# ===========================================================================


class TestRolloverDeadZone:
    """Tests for the rollover dead zone pre-trade check."""

    def _make_ts(self, hour: int, minute: int = 0) -> float:
        """Create a UTC timestamp for today at the given hour:minute."""
        dt = datetime(2026, 3, 25, hour, minute, tzinfo=timezone.utc)
        return dt.timestamp()

    def test_blocks_during_rollover_window(self):
        """Orders during 21:00-23:00 UTC should be denied."""
        gate = PreTradeGate(_cfg(risk={"rollover_dead_zone_enabled": True}))
        for hour in (21, 22):
            ts = self._make_ts(hour, 30)
            with patch("novatrade.risk.pre_trade_gate._now", return_value=ts):
                decision = gate.evaluate(_order(), _account(), [])
            failed_names = [c.name for c in decision.checks if not c.passed]
            assert "rollover_dead_zone" in failed_names, f"Expected deny at {hour}:30 UTC"

    def test_allows_before_rollover(self):
        """Orders at 20:59 UTC should pass the rollover check."""
        gate = PreTradeGate(_cfg(risk={"rollover_dead_zone_enabled": True}))
        ts = self._make_ts(20, 59)
        with patch("novatrade.risk.pre_trade_gate._now", return_value=ts):
            decision = gate.evaluate(_order(), _account(), [])
        rollover_checks = [c for c in decision.checks if c.name == "rollover_dead_zone"]
        assert len(rollover_checks) == 1
        assert rollover_checks[0].passed

    def test_allows_after_rollover(self):
        """Orders at 23:00 UTC (end of window) should pass."""
        gate = PreTradeGate(_cfg(risk={"rollover_dead_zone_enabled": True}))
        ts = self._make_ts(23, 0)
        with patch("novatrade.risk.pre_trade_gate._now", return_value=ts):
            decision = gate.evaluate(_order(), _account(), [])
        rollover_checks = [c for c in decision.checks if c.name == "rollover_dead_zone"]
        assert len(rollover_checks) == 1
        assert rollover_checks[0].passed

    def test_disabled_passes_all_hours(self):
        """With rollover check disabled, all hours should pass."""
        gate = PreTradeGate(_cfg(risk={"rollover_dead_zone_enabled": False}))
        for hour in (21, 22):
            ts = self._make_ts(hour, 30)
            with patch("novatrade.risk.pre_trade_gate._now", return_value=ts):
                decision = gate.evaluate(_order(), _account(), [])
            rollover_checks = [c for c in decision.checks if c.name == "rollover_dead_zone"]
            assert len(rollover_checks) == 1
            assert rollover_checks[0].passed

    def test_custom_window(self):
        """Custom rollover window hours should be respected."""
        gate = PreTradeGate(
            _cfg(
                risk={
                    "rollover_dead_zone_enabled": True,
                    "rollover_start_hour_utc": 20,
                    "rollover_end_hour_utc": 22,
                }
            )
        )
        # 20:30 should be blocked
        ts = self._make_ts(20, 30)
        with patch("novatrade.risk.pre_trade_gate._now", return_value=ts):
            decision = gate.evaluate(_order(), _account(), [])
        rollover_checks = [c for c in decision.checks if c.name == "rollover_dead_zone"]
        assert not rollover_checks[0].passed

        # 22:00 should pass (end of custom window)
        ts = self._make_ts(22, 0)
        with patch("novatrade.risk.pre_trade_gate._now", return_value=ts):
            decision = gate.evaluate(_order(), _account(), [])
        rollover_checks = [c for c in decision.checks if c.name == "rollover_dead_zone"]
        assert rollover_checks[0].passed

    def test_midnight_wrap_window(self):
        """Window that wraps midnight (e.g. 23:00-01:00) should work correctly."""
        gate = PreTradeGate(
            _cfg(
                risk={
                    "rollover_dead_zone_enabled": True,
                    "rollover_start_hour_utc": 23,
                    "rollover_end_hour_utc": 1,
                }
            )
        )
        # 23:30 should be blocked
        ts = self._make_ts(23, 30)
        with patch("novatrade.risk.pre_trade_gate._now", return_value=ts):
            decision = gate.evaluate(_order(), _account(), [])
        rollover_checks = [c for c in decision.checks if c.name == "rollover_dead_zone"]
        assert not rollover_checks[0].passed

        # 00:30 should be blocked
        ts = self._make_ts(0, 30)
        with patch("novatrade.risk.pre_trade_gate._now", return_value=ts):
            decision = gate.evaluate(_order(), _account(), [])
        rollover_checks = [c for c in decision.checks if c.name == "rollover_dead_zone"]
        assert not rollover_checks[0].passed

        # 01:00 should pass
        ts = self._make_ts(1, 0)
        with patch("novatrade.risk.pre_trade_gate._now", return_value=ts):
            decision = gate.evaluate(_order(), _account(), [])
        rollover_checks = [c for c in decision.checks if c.name == "rollover_dead_zone"]
        assert rollover_checks[0].passed

    def test_check_detail_includes_time(self):
        """The detail string should include the current UTC time."""
        gate = PreTradeGate(_cfg(risk={"rollover_dead_zone_enabled": True}))
        ts = self._make_ts(22, 15)
        with patch("novatrade.risk.pre_trade_gate._now", return_value=ts):
            decision = gate.evaluate(_order(), _account(), [])
        rollover_checks = [c for c in decision.checks if c.name == "rollover_dead_zone"]
        assert "22:15 UTC" in rollover_checks[0].detail

    def test_boundary_start_is_inclusive(self):
        """The start hour should be inclusive (21:00 is inside the zone)."""
        gate = PreTradeGate(_cfg(risk={"rollover_dead_zone_enabled": True}))
        ts = self._make_ts(21, 0)
        with patch("novatrade.risk.pre_trade_gate._now", return_value=ts):
            decision = gate.evaluate(_order(), _account(), [])
        rollover_checks = [c for c in decision.checks if c.name == "rollover_dead_zone"]
        assert not rollover_checks[0].passed


# ===========================================================================
# 2. Entry Timing Jitter
# ===========================================================================


def _mock_trading_agent(state: AgentState = AgentState.FLAT) -> MagicMock:
    agent = MagicMock()
    agent.state = state
    agent.pending_order_id = "ORD-123" if "PENDING" in state.value else None
    agent.process_alert = AsyncMock(
        return_value=AgentResult(success=True, state_after=state),
    )
    return agent


def _mock_engine() -> MagicMock:
    engine = MagicMock()
    engine.notify_fill = MagicMock()
    engine.notify_close = MagicMock()
    return engine


def _entry_signal(side: str = "LONG", **kw) -> LiveSignal:
    defaults = {
        "signal_type": SignalType.ENTRY,
        "side": side,
        "symbol": "EURUSD",
        "entry_price": 1.1050,
        "stop_loss": 1.1000,
        "volume": 0.10,
        "timestamp": 1700000000.0,
    }
    defaults.update(kw)
    return LiveSignal(**defaults)  # type: ignore[arg-type]


def _exit_signal(side: str = "LONG", **kw) -> LiveSignal:
    defaults = {
        "signal_type": SignalType.EXIT,
        "side": side,
        "symbol": "EURUSD",
        "exit_reason": "TIME_STOP",
    }
    defaults.update(kw)
    return LiveSignal(**defaults)  # type: ignore[arg-type]


class TestEntryTimingJitter:
    """Tests for the entry timing jitter in LiveTradingAgent."""

    def _make_agent(
        self, jitter_enabled: bool = True, jitter_min: float = 1.0, jitter_max: float = 5.0
    ) -> LiveTradingAgent:
        cfg = NovaTradeCfg(
            symbols=["EURUSD"],
            ftmo=FtmoProfile(enabled=True, symbol_suffix=".ftmo"),
            risk=RiskConfig(
                entry_jitter_enabled=jitter_enabled,
                entry_jitter_min_seconds=jitter_min,
                entry_jitter_max_seconds=jitter_max,
            ),
        )
        return LiveTradingAgent(
            trading_agent=_mock_trading_agent(),
            strategy_engine=_mock_engine(),
            cfg=cfg,
        )

    def _run(self, coro):
        """Run a coroutine using a fresh event loop (safe after other async tests)."""
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_jitter_applied_to_entry_signals(self):
        """Entry signals should sleep for a random jitter before execution."""
        agent = self._make_agent(jitter_enabled=True, jitter_min=2.0, jitter_max=3.0)
        with patch("novatrade.execution.live_trading_agent.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            self._run(agent.execute(_entry_signal()))
            mock_sleep.assert_called_once()
            jitter_val = mock_sleep.call_args[0][0]
            assert 2.0 <= jitter_val <= 3.0

    def test_no_jitter_for_exit_signals(self):
        """Exit signals should NOT have jitter applied."""
        agent = self._make_agent(jitter_enabled=True)
        with patch("novatrade.execution.live_trading_agent.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            self._run(agent.execute(_exit_signal()))
            mock_sleep.assert_not_called()

    def test_no_jitter_when_disabled(self):
        """With jitter disabled, entry signals should execute immediately."""
        agent = self._make_agent(jitter_enabled=False)
        with patch("novatrade.execution.live_trading_agent.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            self._run(agent.execute(_entry_signal()))
            mock_sleep.assert_not_called()

    def test_jitter_respects_config_range(self):
        """Jitter should be within the configured min/max range."""
        agent = self._make_agent(jitter_min=0.5, jitter_max=1.5)
        jitter_values = []

        async def capture_sleep(val):
            jitter_values.append(val)

        with patch("novatrade.execution.live_trading_agent.asyncio.sleep", side_effect=capture_sleep):
            for _ in range(20):
                self._run(agent.execute(_entry_signal()))
        assert len(jitter_values) == 20
        for v in jitter_values:
            assert 0.5 <= v <= 1.5

    def test_jitter_varies_between_calls(self):
        """Jitter values should not be identical across multiple calls."""
        agent = self._make_agent(jitter_min=1.0, jitter_max=5.0)
        jitter_values = []

        async def capture_sleep(val):
            jitter_values.append(val)

        with patch("novatrade.execution.live_trading_agent.asyncio.sleep", side_effect=capture_sleep):
            for _ in range(10):
                self._run(agent.execute(_entry_signal()))
        # With a 4-second range and 10 samples, extremely unlikely all identical
        unique = set(round(v, 6) for v in jitter_values)
        assert len(unique) > 1, "jitter values should vary between calls"

    def test_no_jitter_for_modify_sl(self):
        """MODIFY_SL signals are time-critical — no jitter."""
        agent = self._make_agent(jitter_enabled=True)
        signal = LiveSignal(
            signal_type=SignalType.MODIFY_SL,
            side="LONG",
            symbol="EURUSD",
            new_stop=1.1025,
            metadata={"old_stop": 1.1000},
        )
        with patch("novatrade.execution.live_trading_agent.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            self._run(agent.execute(signal))
            mock_sleep.assert_not_called()


# ===========================================================================
# 3. Lot-Size Micro-Variation
# ===========================================================================


class TestLotMicroVariation:
    """Tests for the lot-size micro-variation in PositionSizer."""

    def test_micro_variation_changes_output(self):
        """With micro-variation enabled, repeated calculations should vary."""
        sizer = PositionSizer(
            min_lot=0.01,
            max_lot=1.0,
            micro_variation_enabled=True,
            micro_variation_step=0.01,
        )
        results = set()
        for _ in range(50):
            vol = sizer.calculate(equity=10000, entry=1.1050, stop=1.1000)
            results.add(vol)
        # With ±0.01 variation, we should see at least 2 distinct values
        assert len(results) >= 2, f"expected variation, got single value: {results}"

    def test_micro_variation_respects_bounds(self):
        """Micro-variation should never produce volumes outside [min_lot, max_lot]."""
        sizer = PositionSizer(
            min_lot=0.01,
            max_lot=0.50,
            micro_variation_enabled=True,
            micro_variation_step=0.01,
        )
        for _ in range(100):
            vol = sizer.calculate(equity=10000, entry=1.1050, stop=1.1000)
            assert 0.01 <= vol <= 0.50, f"volume {vol} out of bounds"

    def test_no_variation_when_disabled(self):
        """With micro-variation disabled, results should be deterministic."""
        sizer = PositionSizer(
            min_lot=0.01,
            max_lot=1.0,
            micro_variation_enabled=False,
        )
        results = set()
        for _ in range(20):
            vol = sizer.calculate(equity=10000, entry=1.1050, stop=1.1000)
            results.add(vol)
        assert len(results) == 1, f"expected single value, got {results}"

    def test_variation_at_min_bound(self):
        """Micro-variation at min_lot should clamp upward, not go below."""
        sizer = PositionSizer(
            min_lot=0.01,
            max_lot=1.0,
            micro_variation_enabled=True,
            micro_variation_step=0.01,
        )
        # Very small equity → should produce near-minimum lots
        for _ in range(50):
            vol = sizer.calculate(equity=100, entry=1.1050, stop=1.1000)
            assert vol >= 0.01, f"volume {vol} below minimum"

    def test_variation_at_max_bound(self):
        """Micro-variation at max_lot should clamp downward, not exceed."""
        sizer = PositionSizer(
            min_lot=0.01,
            max_lot=0.20,
            micro_variation_enabled=True,
            micro_variation_step=0.01,
        )
        # Large equity → should produce near-maximum lots
        for _ in range(50):
            vol = sizer.calculate(equity=100000, entry=1.1050, stop=1.1000)
            assert vol <= 0.20, f"volume {vol} above maximum"

    def test_variation_step_configurable(self):
        """Custom step size should produce expected variation range."""
        sizer = PositionSizer(
            min_lot=0.01,
            max_lot=5.0,
            micro_variation_enabled=True,
            micro_variation_step=0.05,
        )
        base = PositionSizer(min_lot=0.01, max_lot=5.0).calculate(equity=10000, entry=1.1050, stop=1.1000)
        results = set()
        for _ in range(100):
            vol = sizer.calculate(equity=10000, entry=1.1050, stop=1.1000)
            results.add(vol)
        # Should see base-0.05, base, base+0.05 (three possible values)
        expected = {round(base - 0.05, 2), base, round(base + 0.05, 2)}
        assert results.issubset(expected), f"unexpected values: {results - expected}"

    def test_variation_is_rounded_to_2_decimals(self):
        """All lot sizes should be rounded to 2 decimal places."""
        sizer = PositionSizer(
            min_lot=0.01,
            max_lot=1.0,
            micro_variation_enabled=True,
            micro_variation_step=0.01,
        )
        for _ in range(50):
            vol = sizer.calculate(equity=10000, entry=1.1050, stop=1.1000)
            assert vol == round(vol, 2), f"volume {vol} not rounded to 2 decimals"

    def test_zero_step_disables_variation(self):
        """A step of 0.0 should produce no variation even when enabled."""
        sizer = PositionSizer(
            min_lot=0.01,
            max_lot=1.0,
            micro_variation_enabled=True,
            micro_variation_step=0.0,
        )
        results = set()
        for _ in range(20):
            vol = sizer.calculate(equity=10000, entry=1.1050, stop=1.1000)
            results.add(vol)
        assert len(results) == 1


# ===========================================================================
# 4. Integration: Config wiring
# ===========================================================================


class TestConfigWiring:
    """Verify config parameters are correctly wired through the stack."""

    def test_rollover_config_defaults(self):
        cfg = RiskConfig()
        assert cfg.rollover_dead_zone_enabled is True
        assert cfg.rollover_start_hour_utc == 21
        assert cfg.rollover_end_hour_utc == 23

    def test_jitter_config_defaults(self):
        cfg = RiskConfig()
        assert cfg.entry_jitter_enabled is True
        assert cfg.entry_jitter_min_seconds == 1.0
        assert cfg.entry_jitter_max_seconds == 5.0

    def test_lot_variation_config_defaults(self):
        cfg = RiskConfig()
        assert cfg.lot_micro_variation_enabled is True
        assert cfg.lot_micro_variation_step == 0.01

    def test_pre_trade_gate_wires_micro_variation(self):
        """PreTradeGate should pass micro_variation config to PositionSizer."""
        gate = PreTradeGate(
            _cfg(
                risk={
                    "lot_micro_variation_enabled": True,
                    "lot_micro_variation_step": 0.02,
                }
            )
        )
        assert gate._sizer._micro_variation_enabled is True
        assert gate._sizer._micro_variation_step == 0.02


# ===========================================================================
# 5. Broker trade_allowed Safety Check
# ===========================================================================


class TestTradeAllowedCheck:
    """Tests for the trade_allowed pre-trade gate check.

    When the broker reports tradeAllowed=false (e.g. prop firm suspended
    trading, account under review), ALL orders must be denied.
    """

    def test_trade_allowed_true_passes(self):
        """Account with trade_allowed=True should pass the check."""
        gate = PreTradeGate(_cfg())
        account = _account(trade_allowed=True)
        decision = gate.evaluate(_order(), account, [])
        check = next(c for c in decision.checks if c.name == "trade_allowed")
        assert check.passed
        assert "true" in check.detail.lower()

    def test_trade_allowed_false_denies(self):
        """Account with trade_allowed=False should deny ALL orders."""
        gate = PreTradeGate(_cfg())
        account = _account(trade_allowed=False)
        decision = gate.evaluate(_order(), account, [])
        assert decision.denied
        check = next(c for c in decision.checks if c.name == "trade_allowed")
        assert not check.passed
        assert "false" in check.detail.lower()

    def test_trade_allowed_default_is_true(self):
        """AccountState defaults to trade_allowed=True for backward compat."""
        account = AccountState(balance=100_000.0, equity=100_000.0)
        assert account.trade_allowed is True

    def test_trade_allowed_check_present_in_evaluate(self):
        """The trade_allowed check should always appear in decision.checks."""
        gate = PreTradeGate(_cfg())
        account = _account()
        decision = gate.evaluate(_order(), account, [])
        check_names = [c.name for c in decision.checks]
        assert "trade_allowed" in check_names

    def test_trade_allowed_check_runs_early(self):
        """trade_allowed should run before health/volume checks (after account_mode)."""
        gate = PreTradeGate(_cfg())
        account = _account()
        decision = gate.evaluate(_order(), account, [])
        check_names = [c.name for c in decision.checks]
        # Should appear right after account_mode
        mode_idx = check_names.index("account_mode")
        ta_idx = check_names.index("trade_allowed")
        assert ta_idx == mode_idx + 1
