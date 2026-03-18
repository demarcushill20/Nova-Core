"""Pydantic v2 schemas for structured LLM outputs.

These models validate JSON responses from Claude CLI subprocess calls.
Used by utils/structured_output.py for parse-and-validate workflows.
"""

from utils.schemas.contract import ContractField, ContractOutput
from utils.schemas.heartbeat import CycleOutcome, HeartbeatDecision
from utils.schemas.task import TaskResult, TaskVerification
from utils.schemas.trading import IndicatorConfig, SignalAction, StrategySpec, StrategyState, TradeSignal
from utils.schemas.validators import (
    is_strategy_tradeable,
    signal_matches_strategy,
    validate_strategy_spec,
    validate_trade_signal,
)

__all__ = [
    "ContractField",
    "ContractOutput",
    "CycleOutcome",
    "HeartbeatDecision",
    "IndicatorConfig",
    "SignalAction",
    "StrategySpec",
    "StrategyState",
    "TaskResult",
    "TaskVerification",
    "TradeSignal",
    "is_strategy_tradeable",
    "signal_matches_strategy",
    "validate_strategy_spec",
    "validate_trade_signal",
]
