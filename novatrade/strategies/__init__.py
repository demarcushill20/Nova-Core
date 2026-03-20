"""NovaTrade pluggable strategy framework.

Provides a BaseStrategy ABC and concrete implementations for multiple strategy
families: IRB (reversal), Mean Reversion, Breakout, and Trend Following.

Usage::

    from novatrade.strategies import get_strategy, list_strategies

    StrategyClass = get_strategy("irb")
    strategy = StrategyClass()
    indicators = strategy.compute_indicators(candles)
    signals = strategy.generate_signals(candles, indicators)
"""

from novatrade.strategies.base import BaseStrategy, EntrySignal, ExitSignal
from novatrade.strategies.registry import get_strategy, list_strategies, register_strategy

__all__ = [
    "BaseStrategy",
    "EntrySignal",
    "ExitSignal",
    "get_strategy",
    "list_strategies",
    "register_strategy",
]
