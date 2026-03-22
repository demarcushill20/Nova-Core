"""Property-based tests for all Pydantic v2 models (Phase 7.5).

Uses Hypothesis to verify that all schemas survive arbitrary valid inputs
via roundtrip (model_dump -> model_validate) and JSON roundtrip tests.
"""

from __future__ import annotations

import math

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from utils.schemas.contract import ContractConfidence, ContractField, ContractOutput
from utils.schemas.heartbeat import CycleOutcome, HeartbeatDecision, WriteStatus
from utils.schemas.task import Confidence, TaskResult, TaskVerification
from utils.schemas.trading import (
    IndicatorConfig,
    SignalAction,
    StrategySpec,
    StrategyState,
    TradeSignal,
)

# ---------------------------------------------------------------------------
# Shared strategies
# ---------------------------------------------------------------------------

# Safe text that avoids null bytes (Pydantic strips them) and surrogates
safe_text = st.text(
    alphabet=st.characters(
        blacklist_categories=("Cs",),  # no surrogates
        blacklist_characters=("\x00",),  # no null bytes
    ),
    min_size=0,
    max_size=50,
)

safe_text_nonempty = st.text(
    alphabet=st.characters(
        blacklist_categories=("Cs",),
        blacklist_characters=("\x00",),
    ),
    min_size=1,
    max_size=50,
)

positive_float = st.floats(min_value=0.01, max_value=1e6, allow_nan=False, allow_infinity=False)

# Strategies for simple metadata dicts (JSON-serializable values only)
json_primitives = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-1_000_000, max_value=1_000_000),
    st.floats(min_value=-1e6, max_value=1e6, allow_nan=False, allow_infinity=False),
    safe_text,
)

simple_dict = st.dictionaries(
    keys=safe_text_nonempty.filter(lambda s: len(s.strip()) > 0),
    values=json_primitives,
    max_size=5,
)


# ---------------------------------------------------------------------------
# Heartbeat schemas
# ---------------------------------------------------------------------------

write_status_st = st.sampled_from(list(WriteStatus))

cycle_outcome_st = st.builds(
    CycleOutcome,
    title=safe_text_nonempty.filter(lambda s: len(s) <= 200),
    summary=safe_text.filter(lambda s: len(s) <= 2000),
    vault_write=write_status_st,
    memory_write=write_status_st,
    artifacts=st.lists(safe_text, max_size=5),
    next_steps=st.lists(safe_text, max_size=5),
)

heartbeat_decision_st = st.builds(
    HeartbeatDecision,
    action=st.sampled_from(["research", "planning", "task", "idle", "maintenance"]),
    reason=safe_text.filter(lambda s: len(s) <= 500),
    priority=st.integers(min_value=1, max_value=10),
    target=safe_text,
    estimated_duration_minutes=st.integers(min_value=1, max_value=180),
)


@pytest.mark.property
class TestCycleOutcomeProperty:
    @given(outcome=cycle_outcome_st)
    @settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
    def test_roundtrip(self, outcome: CycleOutcome):
        data = outcome.model_dump()
        reconstructed = CycleOutcome.model_validate(data)
        assert reconstructed == outcome

    @given(outcome=cycle_outcome_st)
    @settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
    def test_json_roundtrip(self, outcome: CycleOutcome):
        json_str = outcome.model_dump_json()
        reconstructed = CycleOutcome.model_validate_json(json_str)
        assert reconstructed == outcome


@pytest.mark.property
class TestHeartbeatDecisionProperty:
    @given(decision=heartbeat_decision_st)
    @settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
    def test_roundtrip(self, decision: HeartbeatDecision):
        data = decision.model_dump()
        reconstructed = HeartbeatDecision.model_validate(data)
        assert reconstructed == decision

    @given(decision=heartbeat_decision_st)
    @settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
    def test_json_roundtrip(self, decision: HeartbeatDecision):
        json_str = decision.model_dump_json()
        reconstructed = HeartbeatDecision.model_validate_json(json_str)
        assert reconstructed == decision

    @given(priority=st.integers().filter(lambda x: x < 1 or x > 10))
    @settings(max_examples=50)
    def test_rejects_invalid_priority(self, priority: int):
        with pytest.raises(ValidationError):
            HeartbeatDecision(action="idle", priority=priority)

    @given(duration=st.integers().filter(lambda x: x < 1 or x > 180))
    @settings(max_examples=50)
    def test_rejects_invalid_duration(self, duration: int):
        with pytest.raises(ValidationError):
            HeartbeatDecision(action="idle", estimated_duration_minutes=duration)


# ---------------------------------------------------------------------------
# Task schemas
# ---------------------------------------------------------------------------

confidence_st = st.sampled_from(list(Confidence))

task_verification_st = st.builds(
    TaskVerification,
    description=safe_text_nonempty.filter(lambda s: len(s) <= 500),
    passed=st.booleans(),
    detail=safe_text.filter(lambda s: len(s) <= 1000),
)

task_result_st = st.builds(
    TaskResult,
    summary=safe_text_nonempty.filter(lambda s: len(s) <= 2000),
    files_changed=st.lists(safe_text, max_size=5),
    verification=st.lists(task_verification_st, max_size=3),
    confidence=confidence_st,
    errors=st.lists(safe_text, max_size=3),
    warnings=st.lists(safe_text, max_size=3),
    next_actions=st.lists(safe_text, max_size=3),
)


@pytest.mark.property
class TestTaskVerificationProperty:
    @given(tv=task_verification_st)
    @settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
    def test_roundtrip(self, tv: TaskVerification):
        data = tv.model_dump()
        reconstructed = TaskVerification.model_validate(data)
        assert reconstructed == tv

    @given(tv=task_verification_st)
    @settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
    def test_json_roundtrip(self, tv: TaskVerification):
        json_str = tv.model_dump_json()
        reconstructed = TaskVerification.model_validate_json(json_str)
        assert reconstructed == tv


@pytest.mark.property
class TestTaskResultProperty:
    @given(result=task_result_st)
    @settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
    def test_roundtrip(self, result: TaskResult):
        data = result.model_dump()
        reconstructed = TaskResult.model_validate(data)
        assert reconstructed == result

    @given(result=task_result_st)
    @settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
    def test_json_roundtrip(self, result: TaskResult):
        json_str = result.model_dump_json()
        reconstructed = TaskResult.model_validate_json(json_str)
        assert reconstructed == result

    @given(csv=safe_text_nonempty)
    @settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
    def test_files_changed_string_parsing(self, csv: str):
        """The files_changed validator accepts comma-separated strings."""
        result = TaskResult(summary="test", files_changed=csv)
        assert isinstance(result.files_changed, list)
        for f in result.files_changed:
            assert isinstance(f, str)
            assert f == f.strip()


# ---------------------------------------------------------------------------
# Contract schemas
# ---------------------------------------------------------------------------

contract_confidence_st = st.sampled_from(list(ContractConfidence))

contract_field_st = st.builds(
    ContractField,
    key=safe_text_nonempty,
    value=safe_text,
)

# resource_constraints needs JSON-safe dict values
resource_constraints_st = st.dictionaries(
    keys=safe_text_nonempty.filter(lambda s: len(s.strip()) > 0),
    values=st.one_of(
        st.integers(min_value=0, max_value=100_000),
        st.floats(min_value=0.0, max_value=1e6, allow_nan=False, allow_infinity=False),
        safe_text,
    ),
    max_size=3,
)

contract_output_st = st.builds(
    ContractOutput,
    summary=safe_text_nonempty.filter(lambda s: len(s) <= 2000),
    files_changed=st.lists(safe_text, max_size=5),
    verification=safe_text.filter(lambda s: len(s) <= 2000),
    confidence=contract_confidence_st,
    tests_passed=st.one_of(st.none(), st.booleans()),
    test_count=st.one_of(st.none(), st.integers(min_value=0, max_value=10000)),
    lint_clean=st.one_of(st.none(), st.booleans()),
    extra_fields=st.lists(contract_field_st, max_size=3),
    max_retries=st.one_of(st.none(), st.integers(min_value=0, max_value=10)),
    timeout_seconds=st.one_of(st.none(), st.integers(min_value=1, max_value=3600)),
    success_criteria=st.lists(safe_text, max_size=3),
    resource_constraints=resource_constraints_st,
    preconditions=st.lists(safe_text, max_size=3),
    postconditions=st.lists(safe_text, max_size=3),
    rollback_steps=st.lists(safe_text, max_size=3),
)


@pytest.mark.property
class TestContractFieldProperty:
    @given(cf=contract_field_st)
    @settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
    def test_roundtrip(self, cf: ContractField):
        data = cf.model_dump()
        reconstructed = ContractField.model_validate(data)
        assert reconstructed == cf


@pytest.mark.property
class TestContractOutputProperty:
    @given(contract=contract_output_st)
    @settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
    def test_roundtrip(self, contract: ContractOutput):
        data = contract.model_dump()
        reconstructed = ContractOutput.model_validate(data)
        assert reconstructed == contract

    @given(contract=contract_output_st)
    @settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
    def test_json_roundtrip(self, contract: ContractOutput):
        json_str = contract.model_dump_json()
        reconstructed = ContractOutput.model_validate_json(json_str)
        assert reconstructed == contract

    @given(contract=contract_output_st)
    @settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
    def test_to_markdown_produces_string(self, contract: ContractOutput):
        """to_markdown() should always return a non-empty string."""
        md = contract.to_markdown()
        assert isinstance(md, str)
        assert md.startswith("## CONTRACT")

    @given(csv=safe_text_nonempty)
    @settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
    def test_files_changed_string_parsing(self, csv: str):
        """The files_changed validator accepts comma-separated strings."""
        contract = ContractOutput(summary="test", files_changed=csv)
        assert isinstance(contract.files_changed, list)

    @given(
        retries=st.integers().filter(lambda x: x < 0 or x > 10),
    )
    @settings(max_examples=50)
    def test_rejects_invalid_max_retries(self, retries: int):
        with pytest.raises(ValidationError):
            ContractOutput(summary="test", max_retries=retries)

    @given(
        timeout=st.integers().filter(lambda x: x < 1 or x > 3600),
    )
    @settings(max_examples=50)
    def test_rejects_invalid_timeout(self, timeout: int):
        with pytest.raises(ValidationError):
            ContractOutput(summary="test", timeout_seconds=timeout)


# ---------------------------------------------------------------------------
# Trading schemas
# ---------------------------------------------------------------------------

signal_action_st = st.sampled_from(list(SignalAction))

# Symbols: 1-15 chars, safe ASCII letters and digits to avoid Unicode issues
symbol_st = st.text(
    alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789",
    min_size=1,
    max_size=15,
).filter(lambda s: len(s.strip()) >= 1)

side_st = st.sampled_from(["buy", "sell", ""])

trade_signal_st = st.builds(
    TradeSignal,
    action=signal_action_st,
    symbol=symbol_st,
    side=side_st,
    entry_price=st.one_of(st.none(), positive_float),
    stop_loss=st.one_of(st.none(), positive_float),
    take_profit=st.one_of(st.none(), positive_float),
    lot_size=st.one_of(
        st.none(),
        st.floats(min_value=0.01, max_value=100.0, allow_nan=False, allow_infinity=False),
    ),
    timeframe=st.sampled_from(["", "1M", "5M", "15M", "1H", "4H", "D1", "W1"]),
    strategy=safe_text,
    confidence=st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False),
    metadata=simple_dict,
)


@pytest.mark.property
class TestTradeSignalProperty:
    @given(signal=trade_signal_st)
    @settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
    def test_roundtrip(self, signal: TradeSignal):
        data = signal.model_dump()
        reconstructed = TradeSignal.model_validate(data)
        assert reconstructed == signal

    @given(signal=trade_signal_st)
    @settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
    def test_json_roundtrip(self, signal: TradeSignal):
        json_str = signal.model_dump_json()
        reconstructed = TradeSignal.model_validate_json(json_str)
        assert reconstructed == signal

    @given(symbol=symbol_st)
    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    def test_symbol_normalized_to_uppercase(self, symbol: str):
        sig = TradeSignal(action=SignalAction.SIGNAL, symbol=symbol)
        assert sig.symbol == symbol.upper().strip()

    @given(side=st.sampled_from(["Buy", "SELL", " buy ", " Sell"]))
    @settings(max_examples=20)
    def test_side_normalized_to_lowercase(self, side: str):
        sig = TradeSignal(action=SignalAction.SIGNAL, symbol="EURUSD", side=side)
        assert sig.side == side.lower().strip()

    def test_rejects_empty_symbol(self):
        with pytest.raises(ValidationError):
            TradeSignal(action=SignalAction.SIGNAL, symbol="")

    @given(confidence=st.floats().filter(lambda x: not math.isnan(x) and (x < 0.0 or x > 1.0)))
    @settings(max_examples=50)
    def test_rejects_invalid_confidence(self, confidence: float):
        with pytest.raises(ValidationError):
            TradeSignal(action=SignalAction.SIGNAL, symbol="EURUSD", confidence=confidence)

    @given(
        lot=st.floats(min_value=100.01, max_value=1e6, allow_nan=False, allow_infinity=False),
    )
    @settings(max_examples=50)
    def test_rejects_lot_size_over_100(self, lot: float):
        with pytest.raises(ValidationError):
            TradeSignal(action=SignalAction.SIGNAL, symbol="EURUSD", lot_size=lot)


# ---------------------------------------------------------------------------
# IndicatorConfig
# ---------------------------------------------------------------------------

indicator_params_st = st.dictionaries(
    keys=safe_text_nonempty.filter(lambda s: len(s.strip()) > 0),
    values=st.one_of(
        st.floats(min_value=-1e6, max_value=1e6, allow_nan=False, allow_infinity=False),
        st.integers(min_value=-1_000_000, max_value=1_000_000),
        safe_text,
    ),
    max_size=5,
)

indicator_config_st = st.builds(
    IndicatorConfig,
    name=st.text(
        alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd")),
        min_size=1,
        max_size=50,
    ),
    params=indicator_params_st,
    weight=st.floats(min_value=0.0, max_value=10.0, allow_nan=False, allow_infinity=False),
)


@pytest.mark.property
class TestIndicatorConfigProperty:
    @given(ic=indicator_config_st)
    @settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
    def test_roundtrip(self, ic: IndicatorConfig):
        data = ic.model_dump()
        reconstructed = IndicatorConfig.model_validate(data)
        assert reconstructed == ic

    @given(ic=indicator_config_st)
    @settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
    def test_json_roundtrip(self, ic: IndicatorConfig):
        json_str = ic.model_dump_json()
        reconstructed = IndicatorConfig.model_validate_json(json_str)
        assert reconstructed == ic


# ---------------------------------------------------------------------------
# StrategySpec
# ---------------------------------------------------------------------------

strategy_state_st = st.sampled_from(list(StrategyState))

strategy_spec_st = st.builds(
    StrategySpec,
    name=st.text(
        alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd", "Zs")),
        min_size=1,
        max_size=100,
    ),
    version=safe_text_nonempty,
    state=strategy_state_st,
    symbols=st.lists(
        st.text(
            alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd")),
            min_size=1,
            max_size=10,
        ),
        max_size=5,
    ),
    timeframes=st.lists(
        st.sampled_from(["1M", "5M", "15M", "30M", "1H", "4H", "D1", "W1", "MN"]),
        max_size=4,
    ),
    indicators=st.lists(indicator_config_st, max_size=3),
    entry_rules=st.lists(safe_text, max_size=3),
    exit_rules=st.lists(safe_text, max_size=3),
    max_risk_pct=st.floats(min_value=0.01, max_value=5.0, allow_nan=False, allow_infinity=False),
    max_positions=st.integers(min_value=1, max_value=20),
    max_daily_trades=st.integers(min_value=1, max_value=100),
    backtest_start=safe_text,
    backtest_end=safe_text,
    min_win_rate=st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False),
    min_profit_factor=st.floats(min_value=0.0, max_value=100.0, allow_nan=False, allow_infinity=False),
    metadata=simple_dict,
)


@pytest.mark.property
class TestStrategySpecProperty:
    @given(spec=strategy_spec_st)
    @settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
    def test_roundtrip(self, spec: StrategySpec):
        data = spec.model_dump()
        reconstructed = StrategySpec.model_validate(data)
        assert reconstructed == spec

    @given(spec=strategy_spec_st)
    @settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
    def test_json_roundtrip(self, spec: StrategySpec):
        json_str = spec.model_dump_json()
        reconstructed = StrategySpec.model_validate_json(json_str)
        assert reconstructed == spec

    @given(
        symbols=st.lists(
            st.text(
                alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd")),
                min_size=1,
                max_size=10,
            ),
            min_size=1,
            max_size=5,
        )
    )
    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    def test_symbols_normalized_to_uppercase(self, symbols: list[str]):
        spec = StrategySpec(name="TestStrat", symbols=symbols)
        for s in spec.symbols:
            assert s == s.upper()

    @given(risk=st.floats(allow_nan=False, allow_infinity=False).filter(lambda x: x <= 0.0 or x > 5.0))
    @settings(max_examples=50)
    def test_rejects_invalid_risk_pct(self, risk: float):
        with pytest.raises(ValidationError):
            StrategySpec(name="TestStrat", max_risk_pct=risk)

    @given(
        positions=st.integers().filter(lambda x: x < 1 or x > 20),
    )
    @settings(max_examples=50)
    def test_rejects_invalid_max_positions(self, positions: int):
        with pytest.raises(ValidationError):
            StrategySpec(name="TestStrat", max_positions=positions)

    @given(
        daily=st.integers().filter(lambda x: x < 1 or x > 100),
    )
    @settings(max_examples=50)
    def test_rejects_invalid_max_daily_trades(self, daily: int):
        with pytest.raises(ValidationError):
            StrategySpec(name="TestStrat", max_daily_trades=daily)

    def test_rejects_empty_name(self):
        with pytest.raises(ValidationError):
            StrategySpec(name="")


# ---------------------------------------------------------------------------
# Cross-model integration property tests
# ---------------------------------------------------------------------------


@pytest.mark.property
class TestCrossModelProperty:
    """Verify that models composed together still survive roundtrips."""

    @given(
        summary=safe_text_nonempty.filter(lambda s: len(s) <= 2000),
        verifications=st.lists(task_verification_st, max_size=3),
        confidence=confidence_st,
    )
    @settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
    def test_task_result_with_nested_verification_roundtrip(self, summary, verifications, confidence):
        result = TaskResult(
            summary=summary,
            verification=verifications,
            confidence=confidence,
        )
        data = result.model_dump()
        reconstructed = TaskResult.model_validate(data)
        assert reconstructed == result
        assert len(reconstructed.verification) == len(verifications)

    @given(
        spec=strategy_spec_st,
        signal=trade_signal_st,
    )
    @settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
    def test_strategy_and_signal_independent_roundtrip(self, spec, signal):
        """Both trading models roundtrip independently even when generated together."""
        spec_data = spec.model_dump()
        signal_data = signal.model_dump()
        assert StrategySpec.model_validate(spec_data) == spec
        assert TradeSignal.model_validate(signal_data) == signal

    @given(contract=contract_output_st)
    @settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
    def test_contract_markdown_and_dict_roundtrip(self, contract):
        """Contract can produce markdown and also roundtrip via dict."""
        md = contract.to_markdown()
        assert "## CONTRACT" in md
        data = contract.model_dump()
        reconstructed = ContractOutput.model_validate(data)
        assert reconstructed == contract
