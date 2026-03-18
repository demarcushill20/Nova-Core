"""Tests for Phase 5.7 (StrategySpec) and Phase 5.8 (Validators)."""

import pytest
from pydantic import ValidationError

from utils.schemas.trading import (
    IndicatorConfig,
    SignalAction,
    StrategySpec,
    StrategyState,
    TradeSignal,
)
from utils.schemas.validators import (
    is_strategy_tradeable,
    signal_matches_strategy,
    validate_strategy_spec,
    validate_trade_signal,
)

# ── Phase 5.7: StrategySpec ─────────────────────────────────────────────────


class TestIndicatorConfig:
    def test_valid_construction(self):
        ic = IndicatorConfig(name="RSI", params={"period": 14}, weight=2.0)
        assert ic.name == "RSI"
        assert ic.params == {"period": 14}
        assert ic.weight == 2.0

    def test_defaults(self):
        ic = IndicatorConfig(name="MACD")
        assert ic.params == {}
        assert ic.weight == 1.0

    def test_weight_lower_bound(self):
        ic = IndicatorConfig(name="BB", weight=0.0)
        assert ic.weight == 0.0

    def test_weight_upper_bound(self):
        ic = IndicatorConfig(name="BB", weight=10.0)
        assert ic.weight == 10.0

    def test_weight_too_high_raises(self):
        with pytest.raises(ValidationError):
            IndicatorConfig(name="BB", weight=10.1)

    def test_weight_negative_raises(self):
        with pytest.raises(ValidationError):
            IndicatorConfig(name="BB", weight=-0.1)

    def test_empty_name_raises(self):
        with pytest.raises(ValidationError):
            IndicatorConfig(name="")

    def test_params_mixed_types(self):
        ic = IndicatorConfig(
            name="Custom",
            params={"period": 14, "multiplier": 2.5, "mode": "ema"},
        )
        assert ic.params["period"] == 14
        assert ic.params["multiplier"] == 2.5
        assert ic.params["mode"] == "ema"


class TestStrategyState:
    def test_all_values(self):
        assert StrategyState.DRAFT.value == "draft"
        assert StrategyState.BACKTEST.value == "backtest"
        assert StrategyState.PAPER.value == "paper"
        assert StrategyState.LIVE.value == "live"
        assert StrategyState.RETIRED.value == "retired"

    def test_from_string(self):
        assert StrategyState("draft") == StrategyState.DRAFT
        assert StrategyState("live") == StrategyState.LIVE

    def test_invalid_state_raises(self):
        with pytest.raises(ValueError):
            StrategyState("invalid")


class TestStrategySpec:
    def test_valid_full_construction(self):
        spec = StrategySpec(
            name="Rob Hoffman IRB",
            version="2.0",
            state=StrategyState.LIVE,
            symbols=["EURUSD", "GBPUSD"],
            timeframes=["1H", "4H"],
            indicators=[
                IndicatorConfig(name="RSI", params={"period": 14}),
                IndicatorConfig(name="MACD", params={"fast": 12, "slow": 26}),
            ],
            entry_rules=["RSI > 70 and MACD crossover"],
            exit_rules=["RSI < 30 or trailing stop hit"],
            max_risk_pct=2.0,
            max_positions=5,
            max_daily_trades=20,
            backtest_start="2024-01-01",
            backtest_end="2024-12-31",
            min_win_rate=0.55,
            min_profit_factor=1.5,
            metadata={"author": "nova"},
        )
        assert spec.name == "Rob Hoffman IRB"
        assert spec.version == "2.0"
        assert spec.state == StrategyState.LIVE
        assert len(spec.symbols) == 2
        assert len(spec.indicators) == 2
        assert spec.max_risk_pct == 2.0
        assert spec.max_positions == 5
        assert spec.metadata["author"] == "nova"

    def test_minimal_construction_defaults(self):
        spec = StrategySpec(name="Simple MA Cross")
        assert spec.version == "1.0"
        assert spec.state == StrategyState.DRAFT
        assert spec.symbols == []
        assert spec.timeframes == []
        assert spec.indicators == []
        assert spec.entry_rules == []
        assert spec.exit_rules == []
        assert spec.max_risk_pct == 1.0
        assert spec.max_positions == 3
        assert spec.max_daily_trades == 10
        assert spec.backtest_start == ""
        assert spec.backtest_end == ""
        assert spec.min_win_rate == 0.5
        assert spec.min_profit_factor == 1.2
        assert spec.metadata == {}

    def test_symbol_normalization_uppercase(self):
        spec = StrategySpec(name="Test", symbols=["eurusd", "gbpusd"])
        assert spec.symbols == ["EURUSD", "GBPUSD"]

    def test_symbol_normalization_strips_whitespace(self):
        spec = StrategySpec(name="Test", symbols=["  eurusd  ", "gbpusd"])
        assert spec.symbols == ["EURUSD", "GBPUSD"]

    def test_symbol_normalization_filters_empty(self):
        spec = StrategySpec(name="Test", symbols=["eurusd", "", "gbpusd"])
        assert spec.symbols == ["EURUSD", "GBPUSD"]

    def test_max_risk_pct_too_high_raises(self):
        with pytest.raises(ValidationError):
            StrategySpec(name="Test", max_risk_pct=10.0)

    def test_max_risk_pct_zero_raises(self):
        with pytest.raises(ValidationError):
            StrategySpec(name="Test", max_risk_pct=0.0)

    def test_max_risk_pct_negative_raises(self):
        with pytest.raises(ValidationError):
            StrategySpec(name="Test", max_risk_pct=-1.0)

    def test_max_positions_zero_raises(self):
        with pytest.raises(ValidationError):
            StrategySpec(name="Test", max_positions=0)

    def test_max_positions_too_high_raises(self):
        with pytest.raises(ValidationError):
            StrategySpec(name="Test", max_positions=21)

    def test_max_daily_trades_zero_raises(self):
        with pytest.raises(ValidationError):
            StrategySpec(name="Test", max_daily_trades=0)

    def test_max_daily_trades_too_high_raises(self):
        with pytest.raises(ValidationError):
            StrategySpec(name="Test", max_daily_trades=101)

    def test_empty_name_raises(self):
        with pytest.raises(ValidationError):
            StrategySpec(name="")

    def test_missing_name_raises(self):
        with pytest.raises(ValidationError):
            StrategySpec()

    def test_min_win_rate_bounds(self):
        spec_low = StrategySpec(name="Test", min_win_rate=0.0)
        assert spec_low.min_win_rate == 0.0
        spec_high = StrategySpec(name="Test", min_win_rate=1.0)
        assert spec_high.min_win_rate == 1.0
        with pytest.raises(ValidationError):
            StrategySpec(name="Test", min_win_rate=1.1)
        with pytest.raises(ValidationError):
            StrategySpec(name="Test", min_win_rate=-0.1)

    def test_min_profit_factor_bounds(self):
        spec = StrategySpec(name="Test", min_profit_factor=0.0)
        assert spec.min_profit_factor == 0.0
        with pytest.raises(ValidationError):
            StrategySpec(name="Test", min_profit_factor=-1.0)
        with pytest.raises(ValidationError):
            StrategySpec(name="Test", min_profit_factor=101.0)

    def test_state_from_string(self):
        spec = StrategySpec(name="Test", state="live")
        assert spec.state == StrategyState.LIVE

    def test_model_validate_from_dict(self):
        data = {
            "name": "Test Strategy",
            "version": "2.0",
            "state": "backtest",
            "symbols": ["eurusd"],
            "max_risk_pct": 2.5,
        }
        spec = StrategySpec.model_validate(data)
        assert spec.name == "Test Strategy"
        assert spec.state == StrategyState.BACKTEST
        assert spec.symbols == ["EURUSD"]
        assert spec.max_risk_pct == 2.5


# ── Phase 5.8: Validators ──────────────────────────────────────────────────


class TestValidateTradeSignal:
    def test_valid_payload(self):
        payload = {
            "action": "signal",
            "symbol": "EURUSD",
            "side": "buy",
            "entry_price": 1.0850,
            "stop_loss": 1.0800,
            "confidence": 0.85,
        }
        signal, errors = validate_trade_signal(payload)
        assert signal is not None
        assert errors == []
        assert signal.symbol == "EURUSD"
        assert signal.action == SignalAction.SIGNAL
        assert signal.confidence == 0.85

    def test_missing_action_returns_errors(self):
        payload = {"symbol": "EURUSD", "side": "buy"}
        signal, errors = validate_trade_signal(payload)
        assert signal is None
        assert len(errors) > 0

    def test_invalid_confidence_returns_errors(self):
        payload = {
            "action": "signal",
            "symbol": "EURUSD",
            "confidence": 2.0,
        }
        signal, errors = validate_trade_signal(payload)
        assert signal is None
        assert len(errors) > 0
        assert any("confidence" in str(e) for e in errors)

    def test_empty_symbol_returns_errors(self):
        payload = {"action": "signal", "symbol": ""}
        signal, errors = validate_trade_signal(payload)
        assert signal is None
        assert len(errors) > 0

    def test_valid_minimal_payload(self):
        payload = {"action": "close", "symbol": "USDJPY"}
        signal, errors = validate_trade_signal(payload)
        assert signal is not None
        assert errors == []
        assert signal.action == SignalAction.CLOSE
        assert signal.symbol == "USDJPY"


class TestValidateStrategySpec:
    def test_valid_payload(self):
        payload = {
            "name": "IRB Breakout v2",
            "version": "2.0",
            "state": "paper",
            "symbols": ["EURUSD", "GBPUSD"],
            "max_risk_pct": 1.5,
        }
        spec, errors = validate_strategy_spec(payload)
        assert spec is not None
        assert errors == []
        assert spec.name == "IRB Breakout v2"
        assert spec.state == StrategyState.PAPER

    def test_missing_name_returns_errors(self):
        payload = {"version": "1.0", "state": "draft"}
        spec, errors = validate_strategy_spec(payload)
        assert spec is None
        assert len(errors) > 0

    def test_invalid_risk_returns_errors(self):
        payload = {"name": "Test", "max_risk_pct": 10.0}
        spec, errors = validate_strategy_spec(payload)
        assert spec is None
        assert len(errors) > 0

    def test_minimal_valid_payload(self):
        payload = {"name": "Simple Strategy"}
        spec, errors = validate_strategy_spec(payload)
        assert spec is not None
        assert errors == []
        assert spec.state == StrategyState.DRAFT


class TestIsStrategyTradeable:
    def test_live_is_tradeable(self):
        spec = StrategySpec(name="Test", state=StrategyState.LIVE)
        tradeable, reason = is_strategy_tradeable(spec)
        assert tradeable is True
        assert "live" in reason.lower()

    def test_paper_not_tradeable(self):
        spec = StrategySpec(name="Test", state=StrategyState.PAPER)
        tradeable, reason = is_strategy_tradeable(spec)
        assert tradeable is False
        assert "paper" in reason.lower()

    def test_draft_not_tradeable(self):
        spec = StrategySpec(name="Test", state=StrategyState.DRAFT)
        tradeable, reason = is_strategy_tradeable(spec)
        assert tradeable is False
        assert "draft" in reason.lower()

    def test_backtest_not_tradeable(self):
        spec = StrategySpec(name="Test", state=StrategyState.BACKTEST)
        tradeable, reason = is_strategy_tradeable(spec)
        assert tradeable is False
        assert "backtest" in reason.lower()

    def test_retired_not_tradeable(self):
        spec = StrategySpec(name="Test", state=StrategyState.RETIRED)
        tradeable, reason = is_strategy_tradeable(spec)
        assert tradeable is False
        assert "retired" in reason.lower()


class TestSignalMatchesStrategy:
    def test_matching_symbol_and_timeframe(self):
        signal = TradeSignal(
            action=SignalAction.SIGNAL,
            symbol="EURUSD",
            timeframe="4H",
        )
        spec = StrategySpec(
            name="Test",
            symbols=["EURUSD", "GBPUSD"],
            timeframes=["1H", "4H"],
        )
        compatible, warnings = signal_matches_strategy(signal, spec)
        assert compatible is True
        assert warnings == []

    def test_mismatched_symbol(self):
        signal = TradeSignal(
            action=SignalAction.SIGNAL,
            symbol="USDJPY",
            timeframe="4H",
        )
        spec = StrategySpec(
            name="Test",
            symbols=["EURUSD", "GBPUSD"],
            timeframes=["4H"],
        )
        compatible, warnings = signal_matches_strategy(signal, spec)
        assert compatible is False
        assert len(warnings) >= 1
        assert "USDJPY" in warnings[0]

    def test_mismatched_timeframe(self):
        signal = TradeSignal(
            action=SignalAction.SIGNAL,
            symbol="EURUSD",
            timeframe="D1",
        )
        spec = StrategySpec(
            name="Test",
            symbols=["EURUSD"],
            timeframes=["1H", "4H"],
        )
        compatible, warnings = signal_matches_strategy(signal, spec)
        assert compatible is False
        assert len(warnings) >= 1
        assert "D1" in warnings[0]

    def test_empty_strategy_symbols_passes_anything(self):
        signal = TradeSignal(
            action=SignalAction.SIGNAL,
            symbol="XAUUSD",
            timeframe="1H",
        )
        spec = StrategySpec(name="Test", symbols=[], timeframes=["1H"])
        compatible, warnings = signal_matches_strategy(signal, spec)
        assert compatible is True
        assert warnings == []

    def test_empty_strategy_timeframes_passes_anything(self):
        signal = TradeSignal(
            action=SignalAction.SIGNAL,
            symbol="EURUSD",
            timeframe="D1",
        )
        spec = StrategySpec(name="Test", symbols=["EURUSD"], timeframes=[])
        compatible, warnings = signal_matches_strategy(signal, spec)
        assert compatible is True
        assert warnings == []

    def test_signal_without_timeframe_passes(self):
        signal = TradeSignal(
            action=SignalAction.SIGNAL,
            symbol="EURUSD",
            timeframe="",
        )
        spec = StrategySpec(
            name="Test",
            symbols=["EURUSD"],
            timeframes=["1H", "4H"],
        )
        compatible, warnings = signal_matches_strategy(signal, spec)
        assert compatible is True
        assert warnings == []

    def test_both_symbol_and_timeframe_mismatch(self):
        signal = TradeSignal(
            action=SignalAction.SIGNAL,
            symbol="USDJPY",
            timeframe="D1",
        )
        spec = StrategySpec(
            name="Test",
            symbols=["EURUSD"],
            timeframes=["1H"],
        )
        compatible, warnings = signal_matches_strategy(signal, spec)
        assert compatible is False
        assert len(warnings) == 2


# ── Import integration ──────────────────────────────────────────────────────


class TestPhase57Imports:
    def test_all_new_exports_importable(self):
        from utils.schemas import (
            IndicatorConfig,
            StrategySpec,
            StrategyState,
            is_strategy_tradeable,
            signal_matches_strategy,
            validate_strategy_spec,
            validate_trade_signal,
        )

        assert IndicatorConfig.__name__ == "IndicatorConfig"
        assert StrategyState.__name__ == "StrategyState"
        assert StrategySpec.__name__ == "StrategySpec"
        assert callable(validate_trade_signal)
        assert callable(validate_strategy_spec)
        assert callable(is_strategy_tradeable)
        assert callable(signal_matches_strategy)
