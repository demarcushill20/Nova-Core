"""Strategy registry -- auto-discovers and registers all built-in strategies.

Usage::

    from novatrade.strategies.registry import get_strategy, list_strategies

    cls = get_strategy("irb")
    strategy = cls()

    all_names = list_strategies()  # ["irb", "mean_reversion", "breakout", "trend_following"]
"""

from __future__ import annotations

from novatrade.strategies.base import BaseStrategy

# ---------------------------------------------------------------------------
# Registry state
# ---------------------------------------------------------------------------

STRATEGY_REGISTRY: dict[str, type[BaseStrategy]] = {}


def register_strategy(name: str, cls: type[BaseStrategy]) -> None:
    """Register a strategy class under the given name.

    Args:
        name: Strategy identifier (e.g. "irb", "mean_reversion").
        cls: A concrete subclass of BaseStrategy.

    Raises:
        TypeError: If cls is not a subclass of BaseStrategy.
        ValueError: If name is empty.
    """
    if not name:
        raise ValueError("Strategy name must not be empty")
    if not (isinstance(cls, type) and issubclass(cls, BaseStrategy)):
        raise TypeError(f"{cls} is not a subclass of BaseStrategy")
    STRATEGY_REGISTRY[name] = cls


def get_strategy(name: str) -> type[BaseStrategy]:
    """Look up a registered strategy class by name.

    Args:
        name: Strategy identifier.

    Returns:
        The strategy class (not an instance).

    Raises:
        KeyError: If no strategy is registered under that name.
    """
    if name not in STRATEGY_REGISTRY:
        available = ", ".join(sorted(STRATEGY_REGISTRY.keys())) or "(none)"
        raise KeyError(f"Unknown strategy '{name}'. Available: {available}")
    return STRATEGY_REGISTRY[name]


def list_strategies() -> list[str]:
    """Return sorted list of all registered strategy names."""
    return sorted(STRATEGY_REGISTRY.keys())


# ---------------------------------------------------------------------------
# Auto-register built-in strategies on module import
# ---------------------------------------------------------------------------


def _auto_register() -> None:
    """Import and register all built-in strategy implementations."""
    from novatrade.strategies.breakout import BreakoutStrategy
    from novatrade.strategies.irb import IRBStrategy
    from novatrade.strategies.mean_reversion import MeanReversionStrategy
    from novatrade.strategies.seykota_trend import SeykotaTrendStrategy
    from novatrade.strategies.three_ema_stoch_rsi import ThreeEmaStochRsiStrategy
    from novatrade.strategies.trend_following import TrendFollowingStrategy

    register_strategy("irb", IRBStrategy)
    register_strategy("mean_reversion", MeanReversionStrategy)
    register_strategy("breakout", BreakoutStrategy)
    register_strategy("trend_following", TrendFollowingStrategy)
    register_strategy("three_ema_stoch_rsi", ThreeEmaStochRsiStrategy)
    register_strategy("seykota_trend", SeykotaTrendStrategy)


_auto_register()
