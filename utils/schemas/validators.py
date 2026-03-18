"""Signal and strategy validation helpers for the webhook pipeline."""

from __future__ import annotations

import logging
from typing import Any

from pydantic import ValidationError

from utils.schemas.trading import StrategySpec, StrategyState, TradeSignal

logger = logging.getLogger(__name__)


def validate_trade_signal(payload: dict[str, Any]) -> tuple[TradeSignal | None, list[str]]:
    """Validate a webhook payload as a TradeSignal.

    Returns:
        (signal, errors) — signal is None if validation fails, errors is empty on success
    """
    try:
        signal = TradeSignal.model_validate(payload)
        return signal, []
    except ValidationError as e:
        errors = [f"{err['loc']}: {err['msg']}" for err in e.errors()]
        logger.warning("Trade signal validation failed: %s", errors)
        return None, errors


def validate_strategy_spec(payload: dict[str, Any]) -> tuple[StrategySpec | None, list[str]]:
    """Validate a payload as a StrategySpec.

    Returns:
        (spec, errors) — spec is None if validation fails
    """
    try:
        spec = StrategySpec.model_validate(payload)
        return spec, []
    except ValidationError as e:
        errors = [f"{err['loc']}: {err['msg']}" for err in e.errors()]
        logger.warning("Strategy spec validation failed: %s", errors)
        return None, errors


def is_strategy_tradeable(spec: StrategySpec) -> tuple[bool, str]:
    """Check if a strategy is in a state that allows live trading.

    Returns:
        (tradeable, reason)
    """
    if spec.state == StrategyState.LIVE:
        return True, "Strategy is live"
    if spec.state == StrategyState.PAPER:
        return False, "Strategy is in paper trading mode"
    if spec.state == StrategyState.RETIRED:
        return False, "Strategy is retired"
    return False, f"Strategy is in {spec.state.value} state — not ready for trading"


def signal_matches_strategy(signal: TradeSignal, spec: StrategySpec) -> tuple[bool, list[str]]:
    """Check if a trade signal is compatible with its strategy spec.

    Validates: symbol is in strategy's symbol list, timeframe matches, etc.

    Returns:
        (compatible, warnings)
    """
    warnings = []
    compatible = True

    if spec.symbols and signal.symbol not in spec.symbols:
        warnings.append(f"Symbol {signal.symbol} not in strategy symbols: {spec.symbols}")
        compatible = False

    if spec.timeframes and signal.timeframe and signal.timeframe not in spec.timeframes:
        warnings.append(f"Timeframe {signal.timeframe} not in strategy timeframes: {spec.timeframes}")
        compatible = False

    return compatible, warnings
