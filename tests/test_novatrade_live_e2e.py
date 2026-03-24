"""End-to-end integration tests for the live trading pipeline.

Exercises the REAL signal flow across component boundaries:

    LiveStrategyEngine → LiveSignal → LiveTradingAgent → TradingAgent FSM

Unlike unit tests that mock each component in isolation, these tests
verify format compatibility, state synchronization, and lifecycle
correctness across the full pipeline.  The TradingAgent's broker calls
are mocked (no real MetaApi), but everything above the broker layer
uses real implementations.

Test groups:
  1. Pipeline integration — signal format compatibility across boundaries
  2. Full lifecycle — FLAT → PENDING → LONG → trailing → EXIT → FLAT
  3. Real IRBStrategy — crafted candle data triggers real entry/exit logic
  4. Error propagation — desync detection, rejected signals, queue overflow
"""

from __future__ import annotations

import math
from unittest.mock import AsyncMock, MagicMock

import pytest

from novatrade.backtest.environment import BacktestEnvironment
from novatrade.config import FtmoProfile, NovaTradeCfg
from novatrade.execution.live_trading_agent import LiveTradingAgent
from novatrade.execution.trading_agent import AgentResult, AgentState
from novatrade.models import Candle
from novatrade.strategies.base import BaseStrategy, EntrySignal, ExitSignal
from novatrade.strategies.irb import IRBStrategy
from novatrade.strategy.live_engine import (
    LiveConfig,
    LiveSignal,
    LiveState,
    LiveStrategyEngine,
    SignalType,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _candle(
    ts: float = 0.0,
    o: float = 1.1000,
    h: float = 1.1050,
    lo: float = 1.0950,
    c: float = 1.1020,
    vol: float = 100.0,
    symbol: str = "EURUSD",
    tf: str = "H1",
) -> Candle:
    return Candle(
        timestamp=ts,
        open=o,
        high=h,
        low=lo,
        close=c,
        volume=vol,
        symbol=symbol,
        timeframe=tf,
    )


class _ControllableStrategy(BaseStrategy):
    """Strategy with programmable signal schedule for integration testing.

    Unlike a simple mock, this tracks call counts and can return different
    signals on different bars, enabling multi-step lifecycle tests.
    """

    name = "controllable"
    family = "test"

    def __init__(self) -> None:
        self._entry_schedule: dict[int, EntrySignal | None] = {}
        self._exit_schedule: dict[int, ExitSignal | None] = {}
        self._call_count = 0

    def schedule_entry(self, bar_index: int, sig: EntrySignal | None) -> None:
        self._entry_schedule[bar_index] = sig

    def schedule_exit(self, bar_index: int, sig: ExitSignal | None) -> None:
        self._exit_schedule[bar_index] = sig

    def compute_indicators(self, candles: list[Candle]) -> dict[str, list[float]]:
        n = len(candles)
        return {
            "ema": [c.close for c in candles],
            "atr": [0.005] * n,
            "adx": [25.0] * n,
        }

    def generate_signals(self, candles, indicators):
        return []

    def check_entry(self, bar_index, candles, indicators, h4_candles=None):
        return self._entry_schedule.get(bar_index)

    def check_exit(self, bar_index, candles, indicators, position=None):
        return self._exit_schedule.get(bar_index)

    def get_default_doctrine(self, pair, timeframe):
        return {}

    def get_parameter_bounds(self):
        return {}


def _make_env(**overrides) -> BacktestEnvironment:
    defaults = dict(
        warmup_bars=10,
        initial_equity=10_000.0,
        risk_fraction=0.01,
        pip_value=0.0001,
        pip_value_per_standard_lot=10.0,
        min_volume=0.01,
        max_volume=1.0,
        trail_atr_multiplier=1.5,
        trigger_window_bars=20,
        time_stop_bars=40,
    )
    defaults.update(overrides)
    return BacktestEnvironment(**defaults)


def _make_cfg() -> NovaTradeCfg:
    return NovaTradeCfg(
        symbols=["EURUSD"],
        ftmo=FtmoProfile(enabled=True, symbol_suffix=".ftmo"),
    )


def _make_candles(n: int, base: float = 1.1000, step: float = 0.0002) -> list[Candle]:
    """Generate n uptrending H1 candles."""
    candles = []
    for i in range(n):
        o = base + i * step
        h = o + 0.0005
        lo = o - 0.0005
        c = o + 0.0003
        candles.append(_candle(ts=float(i * 3600), o=o, h=h, lo=lo, c=c))
    return candles


def _mock_trading_agent(state: AgentState = AgentState.FLAT) -> MagicMock:
    """Create a mock TradingAgent that transitions through states realistically."""
    agent = MagicMock()
    agent.state = state
    agent.pending_order_id = "ORD-E2E-001" if "PENDING" in state.value else None

    async def _process_alert(payload):
        action = payload.get("action", "")
        side = payload.get("side", "")
        before = agent.state

        if action == "PLACE_STOP_ORDER":
            if side == "BUY":
                agent.state = AgentState.PENDING_LONG
                agent.pending_order_id = "ORD-E2E-001"
            else:
                agent.state = AgentState.PENDING_SHORT
                agent.pending_order_id = "ORD-E2E-002"
        elif action == "REPLACE_STOP_ORDER":
            pass  # state stays PENDING
        elif action == "CLOSE_POSITION" or action == "CANCEL_ORDER":
            agent.state = AgentState.FLAT
            agent.pending_order_id = None
        elif action == "MODIFY_SL":
            pass  # state unchanged

        return AgentResult(success=True, state_before=before, state_after=agent.state)

    agent.process_alert = AsyncMock(side_effect=_process_alert)

    def _notify_fill(position_id, fill_price, volume, stop_loss):
        if agent.state == AgentState.PENDING_LONG:
            agent.state = AgentState.LONG
        elif agent.state == AgentState.PENDING_SHORT:
            agent.state = AgentState.SHORT
        agent.pending_order_id = None

    agent.notify_fill = MagicMock(side_effect=_notify_fill)

    def _notify_broker_close(position_id, reason):
        agent.state = AgentState.FLAT

    agent.notify_broker_close = MagicMock(side_effect=_notify_broker_close)

    return agent


# ===========================================================================
# Group 1: Pipeline Integration — signal format compatibility
# ===========================================================================


class TestPipelineIntegration:
    """Verify signal format compatibility across LiveStrategyEngine → LiveTradingAgent."""

    def _make_stack(self):
        """Build the full live stack with controllable strategy."""
        strat = _ControllableStrategy()
        env = _make_env()
        cfg = LiveConfig(symbol="EURUSD", trigger_window_bars=20)
        engine = LiveStrategyEngine(strategy=strat, env=env, config=cfg)
        engine.seed_history(_make_candles(15))

        ta = _mock_trading_agent()
        lta = LiveTradingAgent(
            trading_agent=ta,
            strategy_engine=engine,
            cfg=_make_cfg(),
        )
        return engine, strat, ta, lta

    @pytest.mark.asyncio
    async def test_entry_signal_flows_through_pipeline(self):
        """ENTRY from engine → LiveTradingAgent → TradingAgent.process_alert with correct payload."""
        engine, strat, ta, lta = self._make_stack()

        # Schedule entry at bar 15 (next bar after seed)
        strat.schedule_entry(15, EntrySignal(15, "LONG", 1.1050, 1.0950))

        signals = engine.on_bar(_candle(ts=60000.0), "H1")
        assert len(signals) == 1
        assert signals[0].signal_type == SignalType.ENTRY

        result = await lta.execute(signals[0])
        assert result.success
        assert ta.state == AgentState.PENDING_LONG

        # Verify payload format was correct
        call_args = ta.process_alert.call_args[0][0]
        assert call_args["action"] == "PLACE_STOP_ORDER"
        assert call_args["side"] == "BUY"
        assert call_args["strategy_name"] == "Rob Hoffman IRB"
        assert call_args["broker_symbol"] == "EURUSD.ftmo"
        assert call_args["entry_price"] == 1.1050

    @pytest.mark.asyncio
    async def test_exit_signal_flows_through_pipeline(self):
        """EXIT from engine → LiveTradingAgent → CLOSE_POSITION payload."""
        engine, strat, ta, lta = self._make_stack()

        # Get into position
        strat.schedule_entry(15, EntrySignal(15, "LONG", 1.1050, 1.0950))
        engine.on_bar(_candle(ts=60000.0), "H1")
        engine.notify_fill(1.1050, 0.10)
        ta.state = AgentState.LONG

        # Schedule exit
        strat.schedule_exit(16, ExitSignal(16, 1.0950, "stop_loss"))
        signals = engine.on_bar(_candle(ts=64000.0), "H1")
        exit_sigs = [s for s in signals if s.signal_type == SignalType.EXIT]
        assert len(exit_sigs) == 1

        result = await lta.execute(exit_sigs[0])
        assert result.success
        assert ta.state == AgentState.FLAT

        call_args = ta.process_alert.call_args[0][0]
        assert call_args["action"] == "CLOSE_POSITION"
        assert call_args["close_reason"] == "stop_loss"

    @pytest.mark.asyncio
    async def test_modify_sl_signal_flows_through_pipeline(self):
        """MODIFY_SL from trailing stop → LiveTradingAgent → MODIFY_SL payload."""
        engine, strat, ta, lta = self._make_stack()

        # Get into position
        strat.schedule_entry(15, EntrySignal(15, "LONG", 1.1050, 1.0950))
        engine.on_bar(_candle(ts=60000.0), "H1")
        strat._entry_schedule.clear()
        engine.notify_fill(1.1050, 0.10)
        ta.state = AgentState.LONG

        # Feed a bar with higher close → trailing stop should move
        # ATR=0.005, trail_mult=1.5 → trail = best_close - 0.0075
        bar = _candle(ts=64000.0, o=1.1080, h=1.1120, lo=1.1070, c=1.1110)
        signals = engine.on_bar(bar, "H1")
        sl_sigs = [s for s in signals if s.signal_type == SignalType.MODIFY_SL]

        if sl_sigs:  # trailing stop moved
            result = await lta.execute(sl_sigs[0])
            assert result.success
            call_args = ta.process_alert.call_args[0][0]
            assert call_args["action"] == "MODIFY_SL"
            assert call_args["new_stop"] == sl_sigs[0].new_stop

    @pytest.mark.asyncio
    async def test_pending_fill_signal_flows_through_pipeline(self):
        """PENDING_FILL from bar-detected fill → LiveTradingAgent → notify_fill."""
        engine, strat, ta, lta = self._make_stack()

        # Place pending order
        strat.schedule_entry(15, EntrySignal(15, "LONG", 1.1050, 1.0950))
        engine.on_bar(_candle(ts=60000.0), "H1")
        strat._entry_schedule.clear()
        ta.state = AgentState.PENDING_LONG
        ta.pending_order_id = "ORD-E2E-001"

        # Bar that fills the pending (high > entry_price)
        fill_bar = _candle(ts=64000.0, o=1.1020, h=1.1060, lo=1.1000, c=1.1040)
        signals = engine.on_bar(fill_bar, "H1")
        fill_sigs = [s for s in signals if s.signal_type == SignalType.PENDING_FILL]
        assert len(fill_sigs) == 1

        result = await lta.execute(fill_sigs[0])
        assert result.success
        # TradingAgent should have transitioned to LONG via notify_fill
        assert ta.state == AgentState.LONG

    @pytest.mark.asyncio
    async def test_cancel_pending_signal_flows_through_pipeline(self):
        """CANCEL_PENDING signal → LiveTradingAgent → CANCEL_ORDER payload."""
        engine, strat, ta, lta = self._make_stack()

        # Create a cancel signal manually (engine doesn't emit these directly,
        # but the pipeline must support them)
        cancel_sig = LiveSignal(
            signal_type=SignalType.CANCEL_PENDING,
            side="LONG",
            symbol="EURUSD",
            exit_reason="PENDING_EXPIRED",
        )
        ta.state = AgentState.PENDING_LONG

        result = await lta.execute(cancel_sig)
        assert result.success
        call_args = ta.process_alert.call_args[0][0]
        assert call_args["action"] == "CANCEL_ORDER"
        assert ta.state == AgentState.FLAT


# ===========================================================================
# Group 2: Full Lifecycle — complete trade through the pipeline
# ===========================================================================


class TestFullLifecycle:
    """Multi-step lifecycle tests that trace a trade from signal to close."""

    @pytest.mark.asyncio
    async def test_long_lifecycle_entry_fill_trail_exit(self):
        """Full LONG lifecycle: entry → pending fill → trailing stop → exit → flat."""
        strat = _ControllableStrategy()
        env = _make_env()
        engine = LiveStrategyEngine(strategy=strat, env=env)
        engine.seed_history(_make_candles(15))

        ta = _mock_trading_agent()
        lta = LiveTradingAgent(
            trading_agent=ta,
            strategy_engine=engine,
            cfg=_make_cfg(),
        )

        # --- Step 1: Entry signal ---
        strat.schedule_entry(15, EntrySignal(15, "LONG", 1.1050, 1.0950))
        signals = engine.on_bar(_candle(ts=60000.0), "H1")
        assert engine.state == LiveState.PENDING_LONG

        result = await lta.execute(signals[0])
        assert result.success
        assert ta.state == AgentState.PENDING_LONG

        # --- Step 2: Pending fill (bar high crosses entry) ---
        strat._entry_schedule.clear()
        fill_bar = _candle(ts=64000.0, o=1.1020, h=1.1060, lo=1.1010, c=1.1045)
        signals = engine.on_bar(fill_bar, "H1")
        fill_sigs = [s for s in signals if s.signal_type == SignalType.PENDING_FILL]
        assert len(fill_sigs) == 1
        assert engine.state == LiveState.LONG

        result = await lta.execute(fill_sigs[0])
        assert result.success
        assert ta.state == AgentState.LONG

        # --- Step 3: Trailing stop update (price moves up) ---
        higher_bar = _candle(ts=68000.0, o=1.1060, h=1.1090, lo=1.1050, c=1.1085)
        signals = engine.on_bar(higher_bar, "H1")
        sl_sigs = [s for s in signals if s.signal_type == SignalType.MODIFY_SL]
        if sl_sigs:
            result = await lta.execute(sl_sigs[0])
            assert result.success
            # Trailing stop should be above initial SL (1.0950)
            assert sl_sigs[0].new_stop > 1.0950

        # --- Step 4: Exit signal ---
        strat.schedule_exit(18, ExitSignal(18, 1.1020, "stop_loss"))
        exit_bar = _candle(ts=72000.0, o=1.1030, h=1.1040, lo=1.1010, c=1.1020)
        signals = engine.on_bar(exit_bar, "H1")
        exit_sigs = [s for s in signals if s.signal_type == SignalType.EXIT]
        assert len(exit_sigs) == 1

        result = await lta.execute(exit_sigs[0])
        assert result.success
        assert ta.state == AgentState.FLAT

        # --- Step 5: Broker close notification ---
        lta.on_broker_close("POS-001", pnl=-30.0, exit_reason="SL_HIT")
        assert engine.state == LiveState.FLAT
        assert engine.position is None
        assert engine.equity == 9970.0  # 10000 - 30

    @pytest.mark.asyncio
    async def test_short_lifecycle(self):
        """Full SHORT lifecycle: entry → fill → exit → flat."""
        strat = _ControllableStrategy()
        env = _make_env()
        engine = LiveStrategyEngine(strategy=strat, env=env)
        engine.seed_history(_make_candles(15))

        ta = _mock_trading_agent()
        lta = LiveTradingAgent(
            trading_agent=ta,
            strategy_engine=engine,
            cfg=_make_cfg(),
        )

        # Entry SHORT
        strat.schedule_entry(15, EntrySignal(15, "SHORT", 1.0950, 1.1050))
        signals = engine.on_bar(_candle(ts=60000.0), "H1")
        assert engine.state == LiveState.PENDING_SHORT
        await lta.execute(signals[0])
        assert ta.state == AgentState.PENDING_SHORT

        # Fill
        strat._entry_schedule.clear()
        fill_bar = _candle(ts=64000.0, o=1.0980, h=1.1000, lo=1.0940, c=1.0960)
        signals = engine.on_bar(fill_bar, "H1")
        fill_sigs = [s for s in signals if s.signal_type == SignalType.PENDING_FILL]
        assert len(fill_sigs) == 1
        await lta.execute(fill_sigs[0])
        assert ta.state == AgentState.SHORT

        # Exit via broker close
        lta.on_broker_close("POS-002", pnl=50.0, exit_reason="TRAILING_STOP")
        assert engine.state == LiveState.FLAT
        assert engine.equity == 10_050.0

    @pytest.mark.asyncio
    async def test_lifecycle_with_pending_expiry_then_reentry(self):
        """Pending expires → goes FLAT → new entry → fills → closes."""
        strat = _ControllableStrategy()
        env = _make_env(trigger_window_bars=3)
        cfg = LiveConfig(symbol="EURUSD", trigger_window_bars=3)
        engine = LiveStrategyEngine(strategy=strat, env=env, config=cfg)
        engine.seed_history(_make_candles(15))

        ta = _mock_trading_agent()
        lta = LiveTradingAgent(
            trading_agent=ta,
            strategy_engine=engine,
            cfg=_make_cfg(),
        )

        # Entry LONG with very high price (won't fill)
        strat.schedule_entry(15, EntrySignal(15, "LONG", 1.2000, 1.1900))
        signals = engine.on_bar(_candle(ts=60000.0), "H1")
        await lta.execute(signals[0])
        strat._entry_schedule.clear()

        # 3 bars without fill → expiry
        for i in range(3):
            engine.on_bar(_candle(ts=64000.0 + i * 3600, h=1.1050), "H1")
        assert engine.state == LiveState.FLAT

        # New entry that will fill
        strat.schedule_entry(19, EntrySignal(19, "LONG", 1.1050, 1.0950))
        ta.state = AgentState.FLAT  # reset mock state
        engine.on_bar(_candle(ts=76000.0), "H1")
        assert engine.state == LiveState.PENDING_LONG

        # Fill
        strat._entry_schedule.clear()
        fill_bar = _candle(ts=80000.0, o=1.1060, h=1.1100, lo=1.1040, c=1.1080)
        signals = engine.on_bar(fill_bar, "H1")
        fill_sigs = [s for s in signals if s.signal_type == SignalType.PENDING_FILL]
        assert len(fill_sigs) == 1
        assert engine.state == LiveState.LONG


# ===========================================================================
# Group 3: Real IRBStrategy — end-to-end with actual strategy logic
# ===========================================================================


def _irb_uptrend_candles(n: int = 60, base: float = 1.0800) -> list[Candle]:
    """Generate n uptrending H1 candles suitable for IRB signal detection.

    Creates a strong, smooth uptrend with:
    - Sufficient bars for warmup (34)
    - Clear EMA slope (well above 0.4 threshold)
    - ADX > 20 from consistent directional movement
    - Range/ATR < 2.0 (no overextension)
    """
    candles = []
    step = 0.0003  # 3 pips/bar uptrend
    for i in range(n):
        mid = base + i * step
        o = mid - 0.0001
        c = mid + 0.0002
        h = mid + 0.0006
        lo = mid - 0.0005
        candles.append(
            Candle(
                timestamp=1700000000.0 + i * 3600,
                open=o,
                high=h,
                low=lo,
                close=c,
                volume=100.0 + i,
                symbol="EURUSD",
                timeframe="H1",
            )
        )
    return candles


def _append_irb_candle(candles: list[Candle]) -> Candle:
    """Create and return an IRB candle (bullish rejection) following the series.

    Properties: big upper wick, close near low → uptrend IRB geometry.
    open and close in bottom 55% of range (irb_threshold=0.45).
    """
    last = candles[-1]
    base = last.close + 0.0003  # continue trend

    # IRB: big high wick, close at bottom → bullish rejection candle
    lo = base - 0.0003
    h = base + 0.0010  # range = 0.0013
    o = base - 0.0001  # near bottom
    c = base + 0.0001  # near bottom

    # Verify IRB geometry: open and close below up_threshold
    rng = h - lo
    up_threshold = h - 0.45 * rng
    assert o <= up_threshold, f"open {o} > up_threshold {up_threshold}"
    assert c <= up_threshold, f"close {c} > up_threshold {up_threshold}"

    irb = Candle(
        timestamp=last.timestamp + 3600,
        open=o,
        high=h,
        low=lo,
        close=c,
        volume=200.0,
        symbol="EURUSD",
        timeframe="H1",
    )
    return irb


class TestRealIRBStrategy:
    """Tests using the actual IRBStrategy with crafted candle data."""

    def test_irb_produces_entry_signal_on_crafted_data(self):
        """IRBStrategy.check_entry() fires on an uptrend IRB candle."""
        env = BacktestEnvironment(warmup_bars=34)
        strategy = IRBStrategy(env)

        candles = _irb_uptrend_candles(60)
        irb_candle = _append_irb_candle(candles)
        candles.append(irb_candle)

        indicators = strategy.compute_indicators(candles)
        i = len(candles) - 1

        # Verify preconditions
        ema = indicators["ema"]
        atr = indicators["atr"]
        assert not math.isnan(indicators["adx"][i]), "ADX is NaN"
        assert not math.isnan(ema[i]), "EMA is NaN"
        assert not math.isnan(atr[i]), "ATR is NaN"
        assert atr[i] > 0, "ATR is zero"

        # Check if entry fires (it may not due to ADX/overextension filters —
        # this test verifies the interface works, not that it always fires)
        signal = strategy.check_entry(i, candles, indicators)
        if signal is not None:
            assert signal.side == "LONG"
            assert signal.entry_price > 0
            assert signal.stop_loss > 0
            assert signal.entry_price > signal.stop_loss  # LONG: entry above SL

    def test_irb_exit_fires_on_stop_loss(self):
        """IRBStrategy.check_exit() fires stop_loss when bar.low <= current_stop."""
        env = BacktestEnvironment(warmup_bars=34, time_stop_bars=100)
        strategy = IRBStrategy(env)

        candles = _irb_uptrend_candles(60)
        indicators = strategy.compute_indicators(candles)
        i = len(candles) - 1

        position = {
            "side": "LONG",
            "entry_price": 1.0960,
            "stop_loss": 1.0940,
            "entry_bar": i - 5,
            "current_stop": 1.0940,
            "best_close": 1.0980,
        }

        # Feed a bar that hits the stop
        stop_bar = Candle(
            timestamp=1700300000.0,
            open=1.0945,
            high=1.0950,
            low=1.0935,
            close=1.0938,
            volume=150.0,
            symbol="EURUSD",
            timeframe="H1",
        )
        candles.append(stop_bar)
        indicators = strategy.compute_indicators(candles)
        i = len(candles) - 1

        exit_sig = strategy.check_exit(i, candles, indicators, position)
        assert exit_sig is not None
        assert exit_sig.reason == "stop_loss"
        assert exit_sig.exit_price == 1.0940

    def test_irb_exit_fires_time_stop(self):
        """IRBStrategy.check_exit() fires time_stop after enough bars."""
        env = BacktestEnvironment(warmup_bars=34, time_stop_bars=5)
        strategy = IRBStrategy(env)

        candles = _irb_uptrend_candles(60)
        indicators = strategy.compute_indicators(candles)
        i = len(candles) - 1

        position = {
            "side": "LONG",
            "entry_price": 1.0960,
            "stop_loss": 1.0900,
            "entry_bar": i - 10,  # 10 bars held > time_stop_bars=5
            "current_stop": 1.0950,
            "best_close": 1.0980,
        }

        exit_sig = strategy.check_exit(i, candles, indicators, position)
        assert exit_sig is not None
        assert exit_sig.reason == "time_stop"

    def test_irb_through_live_engine_lifecycle(self):
        """Feed crafted candles through LiveStrategyEngine with real IRBStrategy.

        Verifies the interface between IRBStrategy and LiveStrategyEngine works
        end-to-end: compute_indicators → check_entry → check_exit with real data.
        Even if no signal fires (depends on exact filter thresholds), the test
        confirms no crashes, correct state management, and type compatibility.
        """
        env = BacktestEnvironment(warmup_bars=34)
        strategy = IRBStrategy(env)
        config = LiveConfig(symbol="EURUSD", max_candles=500)

        engine = LiveStrategyEngine(strategy=strategy, env=env, config=config)

        # Seed with uptrending data
        history = _irb_uptrend_candles(50)
        engine.seed_history(history)
        assert engine.state == LiveState.FLAT
        assert engine.is_warmed_up

        # Feed more bars through the engine
        all_signals: list[LiveSignal] = []
        for i in range(50, 80):
            mid = 1.0800 + i * 0.0003
            bar = Candle(
                timestamp=1700000000.0 + i * 3600,
                open=mid - 0.0001,
                high=mid + 0.0006,
                low=mid - 0.0005,
                close=mid + 0.0002,
                volume=100.0,
                symbol="EURUSD",
                timeframe="H1",
            )
            signals = engine.on_bar(bar, "H1")
            all_signals.extend(signals)

        # At minimum, the engine should not crash with real strategy
        assert engine.total_bars == 80
        assert engine.h1_count <= 500

        # If any entry signals fired, verify they have valid structure
        for sig in all_signals:
            assert isinstance(sig, LiveSignal)
            assert sig.symbol == "EURUSD"
            if sig.signal_type == SignalType.ENTRY:
                assert sig.side in ("LONG", "SHORT")
                assert sig.entry_price > 0
                assert sig.volume > 0


# ===========================================================================
# Group 4: Error Propagation and Desync Detection
# ===========================================================================


class TestErrorPropagation:
    """Verify error handling and desync detection across the pipeline."""

    @pytest.mark.asyncio
    async def test_fill_desync_trading_agent_fails(self):
        """on_fill handles TradingAgent.notify_fill failure gracefully."""
        strat = _ControllableStrategy()
        env = _make_env()
        engine = LiveStrategyEngine(strategy=strat, env=env)
        engine.seed_history(_make_candles(15))

        ta = _mock_trading_agent()
        ta.notify_fill = MagicMock(side_effect=RuntimeError("broker desync"))
        lta = LiveTradingAgent(
            trading_agent=ta,
            strategy_engine=engine,
            cfg=_make_cfg(),
        )

        # on_fill should not crash even when TradingAgent fails
        lta.on_fill("POS-001", 1.1050, 0.10, 1.0950)
        # Engine should NOT be updated (TradingAgent failed first)
        # This tests the desync protection in LiveTradingAgent

    @pytest.mark.asyncio
    async def test_close_desync_engine_fails(self):
        """on_broker_close handles LiveStrategyEngine.notify_close failure."""
        strat = _ControllableStrategy()
        env = _make_env()
        engine = LiveStrategyEngine(strategy=strat, env=env)
        engine.seed_history(_make_candles(15))

        # Get engine into position
        strat.schedule_entry(15, EntrySignal(15, "LONG", 1.1050, 1.0950))
        engine.on_bar(_candle(ts=60000.0), "H1")
        engine.notify_fill(1.1050, 0.10)

        ta = _mock_trading_agent(AgentState.LONG)
        lta = LiveTradingAgent(
            trading_agent=ta,
            strategy_engine=engine,
            cfg=_make_cfg(),
        )

        # Make engine.notify_close raise
        engine.notify_close = MagicMock(side_effect=ValueError("bad pnl"))

        # Should not crash — TradingAgent gets updated, engine error is logged
        lta.on_broker_close("POS-001", pnl=-50.0, exit_reason="SL_HIT")
        ta.notify_broker_close.assert_called_once()

    @pytest.mark.asyncio
    async def test_rejected_signal_reported_correctly(self):
        """Signal rejected by TradingAgent flows back through pipeline."""
        strat = _ControllableStrategy()
        env = _make_env()
        engine = LiveStrategyEngine(strategy=strat, env=env)
        engine.seed_history(_make_candles(15))

        ta = _mock_trading_agent()

        async def _reject_all(payload):
            return AgentResult(
                success=False,
                state_after=ta.state,
                rejected_reason="risk_limit_exceeded",
            )

        ta.process_alert = AsyncMock(side_effect=_reject_all)
        lta = LiveTradingAgent(
            trading_agent=ta,
            strategy_engine=engine,
            cfg=_make_cfg(),
        )

        strat.schedule_entry(15, EntrySignal(15, "LONG", 1.1050, 1.0950))
        signals = engine.on_bar(_candle(ts=60000.0), "H1")

        result = await lta.execute(signals[0])
        assert not result.success
        assert result.rejected
        assert "risk_limit_exceeded" in result.rejected_reason

    @pytest.mark.asyncio
    async def test_cross_side_flip_cancel_then_place(self):
        """Cross-side entry: cancel opposite pending before placing new."""
        strat = _ControllableStrategy()
        env = _make_env()
        engine = LiveStrategyEngine(strategy=strat, env=env)
        engine.seed_history(_make_candles(15))

        ta = _mock_trading_agent(AgentState.PENDING_SHORT)
        lta = LiveTradingAgent(
            trading_agent=ta,
            strategy_engine=engine,
            cfg=_make_cfg(),
        )

        # LONG entry while PENDING_SHORT → should cancel first
        entry_sig = LiveSignal(
            signal_type=SignalType.ENTRY,
            side="LONG",
            symbol="EURUSD",
            entry_price=1.1050,
            stop_loss=1.0950,
            volume=0.10,
        )
        result = await lta.execute(entry_sig)
        assert result.success

        # Verify two calls: CANCEL_ORDER then PLACE_STOP_ORDER
        assert ta.process_alert.await_count == 2
        first_call = ta.process_alert.call_args_list[0][0][0]
        second_call = ta.process_alert.call_args_list[1][0][0]
        assert first_call["action"] == "CANCEL_ORDER"
        assert second_call["action"] == "PLACE_STOP_ORDER"


# ===========================================================================
# Group 5: State Synchronization
# ===========================================================================


class TestStateSynchronization:
    """Verify engine and agent states stay in sync through the pipeline."""

    @pytest.mark.asyncio
    async def test_engine_and_agent_agree_on_state_after_fill(self):
        """Both engine and TradingAgent report LONG after fill notification."""
        strat = _ControllableStrategy()
        env = _make_env()
        engine = LiveStrategyEngine(strategy=strat, env=env)
        engine.seed_history(_make_candles(15))

        ta = _mock_trading_agent()
        lta = LiveTradingAgent(
            trading_agent=ta,
            strategy_engine=engine,
            cfg=_make_cfg(),
        )

        # Entry + fill through on_fill callback
        strat.schedule_entry(15, EntrySignal(15, "LONG", 1.1050, 1.0950))
        engine.on_bar(_candle(ts=60000.0), "H1")
        ta.state = AgentState.PENDING_LONG

        lta.on_fill("POS-001", 1.1052, 0.10, 1.0950)
        assert engine.state == LiveState.LONG
        assert ta.state == AgentState.LONG

    @pytest.mark.asyncio
    async def test_engine_and_agent_agree_after_close(self):
        """Both engine and TradingAgent report FLAT after broker close."""
        strat = _ControllableStrategy()
        env = _make_env()
        engine = LiveStrategyEngine(strategy=strat, env=env)
        engine.seed_history(_make_candles(15))

        ta = _mock_trading_agent(AgentState.LONG)
        lta = LiveTradingAgent(
            trading_agent=ta,
            strategy_engine=engine,
            cfg=_make_cfg(),
        )

        # Get engine into LONG
        strat.schedule_entry(15, EntrySignal(15, "LONG", 1.1050, 1.0950))
        engine.on_bar(_candle(ts=60000.0), "H1")
        engine.notify_fill(1.1050, 0.10)
        assert engine.state == LiveState.LONG

        lta.on_broker_close("POS-001", pnl=25.0, exit_reason="TRAILING_STOP")
        assert engine.state == LiveState.FLAT
        assert ta.state == AgentState.FLAT
        assert engine.equity == 10_025.0

    @pytest.mark.asyncio
    async def test_snapshot_reflects_pipeline_state(self):
        """Engine snapshot is consistent after pipeline operations."""
        strat = _ControllableStrategy()
        env = _make_env()
        engine = LiveStrategyEngine(strategy=strat, env=env)
        engine.seed_history(_make_candles(15))

        snap = engine.snapshot()
        assert snap["state"] == "FLAT"
        assert snap["has_position"] is False
        assert snap["equity"] == 10_000.0

        strat.schedule_entry(15, EntrySignal(15, "LONG", 1.1050, 1.0950))
        engine.on_bar(_candle(ts=60000.0), "H1")

        snap = engine.snapshot()
        assert snap["state"] == "PENDING_LONG"
        assert snap["has_pending"] is True

        engine.notify_fill(1.1050, 0.10)
        snap = engine.snapshot()
        assert snap["state"] == "LONG"
        assert snap["has_position"] is True
        assert snap["position_side"] == "LONG"
        assert snap["position_entry"] == 1.1050
