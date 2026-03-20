"""Strategy concept translator — converts natural language or keywords to doctrine.

Two modes:
1. Rule-based (MVP): pattern-matches known strategy families (IRB, breakout,
   mean reversion, trend following) and fills a StrategyDoctrine with sensible
   defaults for that family.
2. Skeleton fallback: for unknown concepts, returns a minimal valid doctrine
   that the operator can complete manually.

LLM-assisted translation is a future enhancement (not MVP).
"""

from __future__ import annotations

import re

from novatrade.doctrine.schema import (
    DoctrineCampaign,
    DoctrineData,
    DoctrineExecution,
    DoctrineValidation,
    ParamBound,
    StrategyDoctrine,
)

# ---------------------------------------------------------------------------
# Known strategy family patterns
# ---------------------------------------------------------------------------

_IRB_PATTERNS = re.compile(
    r"(irb|institutional.?rejection.?block|inside.?bar.?breakout"
    r"|rejection.?block|imbalance.?recovery)",
    re.IGNORECASE,
)

_BREAKOUT_PATTERNS = re.compile(
    r"(breakout|range.?break|channel.?break|consolidation.?break"
    r"|support.?resistance.?break)",
    re.IGNORECASE,
)

_MEAN_REVERSION_PATTERNS = re.compile(
    r"(mean.?reversion|revert.?to.?mean|bollinger.?band"
    r"|oversold|overbought|rsi.?reversal)",
    re.IGNORECASE,
)

_TREND_PATTERNS = re.compile(
    r"(trend.?follow|moving.?average.?cross|ema.?cross"
    r"|macd.?signal|momentum|pullback.?entry)",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Default param sets per family
# ---------------------------------------------------------------------------

_IRB_MUTABLE_PARAMS: dict[str, ParamBound] = {
    "irb_threshold": ParamBound(default=0.45, min=0.30, max=0.60, step=0.01),
    "ema_period": ParamBound(default=20, min=10, max=50, step=1),
    "atr_period": ParamBound(default=14, min=7, max=21, step=1),
    "adx_period": ParamBound(default=14, min=7, max=21, step=1),
    "trend_slope_threshold": ParamBound(default=0.4, min=0.1, max=1.0, step=0.05),
    "adx_threshold": ParamBound(default=20.0, min=15.0, max=30.0, step=0.5),
    "overextension_threshold": ParamBound(default=2.0, min=1.5, max=3.0, step=0.1),
    "trail_atr_multiplier": ParamBound(default=1.5, min=1.0, max=3.0, step=0.1),
    "trigger_window_bars": ParamBound(default=20, min=10, max=40, step=1),
    "time_stop_bars": ParamBound(default=40, min=20, max=80, step=1),
    "mtf_lookback": ParamBound(default=5, min=1, max=20, step=1),
    "warmup_bars": ParamBound(default=34, min=20, max=60, step=1),
}

_BREAKOUT_MUTABLE_PARAMS: dict[str, ParamBound] = {
    "lookback_period": ParamBound(default=20, min=10, max=50, step=1),
    "breakout_threshold": ParamBound(default=1.0, min=0.5, max=2.0, step=0.1),
    "atr_period": ParamBound(default=14, min=7, max=21, step=1),
    "trail_atr_multiplier": ParamBound(default=2.0, min=1.0, max=3.0, step=0.1),
    "time_stop_bars": ParamBound(default=40, min=20, max=80, step=1),
    "volume_confirmation_mult": ParamBound(default=1.5, min=1.0, max=3.0, step=0.1),
}

_MEAN_REVERSION_MUTABLE_PARAMS: dict[str, ParamBound] = {
    "bb_period": ParamBound(default=20, min=10, max=50, step=1),
    "bb_std_dev": ParamBound(default=2.0, min=1.5, max=3.0, step=0.1),
    "rsi_period": ParamBound(default=14, min=7, max=21, step=1),
    "rsi_oversold": ParamBound(default=30.0, min=20.0, max=40.0, step=1.0),
    "rsi_overbought": ParamBound(default=70.0, min=60.0, max=80.0, step=1.0),
    "atr_period": ParamBound(default=14, min=7, max=21, step=1),
    "trail_atr_multiplier": ParamBound(default=1.5, min=1.0, max=3.0, step=0.1),
}

_TREND_MUTABLE_PARAMS: dict[str, ParamBound] = {
    "fast_ema": ParamBound(default=12, min=5, max=25, step=1),
    "slow_ema": ParamBound(default=26, min=15, max=50, step=1),
    "atr_period": ParamBound(default=14, min=7, max=21, step=1),
    "trail_atr_multiplier": ParamBound(default=2.0, min=1.0, max=3.0, step=0.1),
    "trend_slope_threshold": ParamBound(default=0.3, min=0.1, max=1.0, step=0.05),
    "time_stop_bars": ParamBound(default=60, min=20, max=120, step=5),
}

# Default immutable params shared across all families
_DEFAULT_IMMUTABLE: dict[str, float | int | str | bool] = {
    "risk_per_trade_pct": 1.0,
    "max_concurrent_trades": 1,
    "max_daily_loss_pct": 3.0,
}


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def detect_strategy_family(
    concept: str,
) -> str:
    """Detect which strategy family a concept text belongs to.

    Returns one of: 'irb', 'breakout', 'mean_reversion', 'trend_following', 'unknown'.
    """
    if _IRB_PATTERNS.search(concept):
        return "irb"
    if _BREAKOUT_PATTERNS.search(concept):
        return "breakout"
    if _MEAN_REVERSION_PATTERNS.search(concept):
        return "mean_reversion"
    if _TREND_PATTERNS.search(concept):
        return "trend_following"
    return "unknown"


# ---------------------------------------------------------------------------
# Translation
# ---------------------------------------------------------------------------


def concept_to_doctrine(
    concept: str,
    pair: str = "EURUSD",
    timeframe: str = "H1",
    *,
    author: str = "nova",
) -> StrategyDoctrine:
    """Convert a strategy concept text to a StrategyDoctrine.

    Uses rule-based matching for known families. For unknown concepts,
    returns a minimal skeleton doctrine with placeholder parameters.

    Args:
        concept: Natural language strategy description.
        pair: Currency pair (e.g. "EURUSD").
        timeframe: Primary candle timeframe (e.g. "H1").
        author: Author name for the doctrine.

    Returns:
        A validated StrategyDoctrine instance.
    """
    family = detect_strategy_family(concept)

    if family == "irb":
        return _build_irb_doctrine(concept, pair, timeframe, author)
    elif family == "breakout":
        return _build_breakout_doctrine(concept, pair, timeframe, author)
    elif family == "mean_reversion":
        return _build_mean_reversion_doctrine(concept, pair, timeframe, author)
    elif family == "trend_following":
        return _build_trend_doctrine(concept, pair, timeframe, author)
    else:
        return _build_skeleton_doctrine(concept, pair, timeframe, author)


# ---------------------------------------------------------------------------
# Family-specific builders
# ---------------------------------------------------------------------------


def _infer_timeframes(timeframe: str) -> list[str]:
    """Infer confirmation timeframe from primary."""
    tf_map = {
        "M5": ["M5", "M15"],
        "M15": ["M15", "H1"],
        "M30": ["M30", "H1"],
        "H1": ["H1", "H4"],
        "H4": ["H4", "D1"],
        "D1": ["D1", "W1"],
    }
    return tf_map.get(timeframe, [timeframe])


def _build_irb_doctrine(concept: str, pair: str, timeframe: str, author: str) -> StrategyDoctrine:
    return StrategyDoctrine(
        name=f"IRB Reversal {timeframe}",
        author=author,
        description=concept,
        concept=concept,
        setup_type="reversal",
        entry_signal="irb_rejection",
        confirmations=["ema_trend", "adx_strength"],
        exit_method="trailing_atr",
        data=DoctrineData(
            pair=pair,
            timeframes=_infer_timeframes(timeframe),
            min_bars=5000,
        ),
        mutable_params=_IRB_MUTABLE_PARAMS,
        immutable_params=_DEFAULT_IMMUTABLE,
        filters_mandatory=["trend_alignment"],
        filters_optional=["adx_strength", "overextension_guard", "session_london_ny_overlap"],
        execution=DoctrineExecution(),
        validation=DoctrineValidation(),
        campaign=DoctrineCampaign(),
        invalidation_rules=[
            "profit_factor < 1.0 after Stage A",
            "max_drawdown > 20% under stress",
            "WFE < 0.3",
        ],
    )


def _build_breakout_doctrine(concept: str, pair: str, timeframe: str, author: str) -> StrategyDoctrine:
    return StrategyDoctrine(
        name=f"Breakout {timeframe}",
        author=author,
        description=concept,
        concept=concept,
        setup_type="breakout",
        entry_signal="range_breakout",
        confirmations=["volume_confirmation"],
        exit_method="trailing_atr",
        data=DoctrineData(
            pair=pair,
            timeframes=_infer_timeframes(timeframe),
            min_bars=5000,
        ),
        mutable_params=_BREAKOUT_MUTABLE_PARAMS,
        immutable_params=_DEFAULT_IMMUTABLE,
        filters_mandatory=[],
        filters_optional=["session_london", "regime_trending_only"],
        execution=DoctrineExecution(),
        validation=DoctrineValidation(),
        campaign=DoctrineCampaign(),
    )


def _build_mean_reversion_doctrine(concept: str, pair: str, timeframe: str, author: str) -> StrategyDoctrine:
    return StrategyDoctrine(
        name=f"Mean Reversion {timeframe}",
        author=author,
        description=concept,
        concept=concept,
        setup_type="mean_reversion",
        entry_signal="bollinger_bounce",
        confirmations=["rsi_filter"],
        exit_method="trailing_atr",
        data=DoctrineData(
            pair=pair,
            timeframes=_infer_timeframes(timeframe),
            min_bars=5000,
        ),
        mutable_params=_MEAN_REVERSION_MUTABLE_PARAMS,
        immutable_params=_DEFAULT_IMMUTABLE,
        filters_mandatory=[],
        filters_optional=["regime_low_volatility_skip"],
        execution=DoctrineExecution(),
        validation=DoctrineValidation(),
        campaign=DoctrineCampaign(),
    )


def _build_trend_doctrine(concept: str, pair: str, timeframe: str, author: str) -> StrategyDoctrine:
    return StrategyDoctrine(
        name=f"Trend Following {timeframe}",
        author=author,
        description=concept,
        concept=concept,
        setup_type="trend_following",
        entry_signal="ema_crossover",
        confirmations=["trend_slope"],
        exit_method="trailing_atr",
        data=DoctrineData(
            pair=pair,
            timeframes=_infer_timeframes(timeframe),
            min_bars=5000,
        ),
        mutable_params=_TREND_MUTABLE_PARAMS,
        immutable_params=_DEFAULT_IMMUTABLE,
        filters_mandatory=["trend_alignment"],
        filters_optional=["session_london_ny_overlap", "regime_trending_only"],
        execution=DoctrineExecution(),
        validation=DoctrineValidation(),
        campaign=DoctrineCampaign(),
    )


def _build_skeleton_doctrine(concept: str, pair: str, timeframe: str, author: str) -> StrategyDoctrine:
    """Minimal skeleton for unknown strategy concepts."""
    return StrategyDoctrine(
        name=f"Custom {timeframe}",
        author=author,
        description=f"[SKELETON] {concept}",
        concept=concept,
        setup_type="reversal",
        entry_signal="custom",
        confirmations=[],
        exit_method="trailing_atr",
        data=DoctrineData(
            pair=pair,
            timeframes=_infer_timeframes(timeframe),
            min_bars=3000,
        ),
        mutable_params={
            "atr_period": ParamBound(default=14, min=7, max=21, step=1),
            "trail_atr_multiplier": ParamBound(default=2.0, min=1.0, max=3.0, step=0.1),
            "time_stop_bars": ParamBound(default=40, min=20, max=80, step=1),
        },
        immutable_params=_DEFAULT_IMMUTABLE,
        execution=DoctrineExecution(),
        validation=DoctrineValidation(min_trades=30),
        campaign=DoctrineCampaign(max_experiments=200),
    )
