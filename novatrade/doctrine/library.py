"""Curated doctrine template library — pre-built strategy families.

Provides ready-to-use StrategyDoctrine templates for common strategy types.
Each template includes mutable params, immutable rules, success criteria,
campaign settings, and recommended pair/timeframe combinations.

Usage::

    from novatrade.doctrine.library import (
        get_doctrine_template,
        list_doctrine_templates,
        create_doctrine_from_template,
    )

    # Browse available templates
    templates = list_doctrine_templates()

    # Load one
    doctrine = get_doctrine_template("irb_conservative")

    # Load with overrides
    doctrine = create_doctrine_from_template(
        "irb_aggressive",
        pair="GBPUSD",
        timeframe="M15",
    )
"""

from __future__ import annotations

from typing import Any

from novatrade.doctrine.schema import (
    DoctrineCampaign,
    DoctrineData,
    DoctrineExecution,
    DoctrineValidation,
    ParamBound,
    StrategyDoctrine,
)

# ---------------------------------------------------------------------------
# Recommended pair/timeframe combos
# ---------------------------------------------------------------------------

_RECOMMENDED: dict[str, dict[str, Any]] = {
    "irb_conservative": {
        "pairs": ["EURUSD", "GBPUSD", "USDJPY"],
        "timeframes": ["H1", "H4"],
        "description": "Institutional Rejection Block with tight risk and high confirmation. "
        "Conservative position sizing, strict trend alignment. Best for "
        "major pairs on H1/H4 where institutional footprints are clearest.",
    },
    "irb_aggressive": {
        "pairs": ["EURUSD", "GBPUSD", "AUDUSD", "USDCAD"],
        "timeframes": ["M15", "M30", "H1"],
        "description": "IRB with wider stops and more trade frequency. "
        "Relaxed entry filters allow more setups. Suited for liquid "
        "pairs on shorter timeframes.",
    },
    "irb_scalper": {
        "pairs": ["EURUSD", "GBPUSD", "USDJPY"],
        "timeframes": ["M5", "M15"],
        "description": "Quick IRB entries with minimal hold time. Tight stops "
        "and fast exits. Only viable during London/NY sessions on "
        "the most liquid pairs.",
    },
    "mean_reversion_bollinger": {
        "pairs": ["EURUSD", "GBPUSD", "AUDUSD", "USDCHF"],
        "timeframes": ["H1", "H4"],
        "description": "Bollinger Band mean reversion with RSI confirmation. "
        "Enters on touch/pierce of outer bands with RSI extremes. "
        "Works best in ranging markets.",
    },
    "breakout_donchian": {
        "pairs": ["EURUSD", "GBPUSD", "USDJPY", "GBPJPY"],
        "timeframes": ["H1", "H4", "D1"],
        "description": "Donchian channel breakout with volume confirmation. "
        "Classic trend-capture strategy. Requires clear consolidation "
        "before breakout.",
    },
    "trend_ema_cross": {
        "pairs": ["EURUSD", "GBPUSD", "AUDUSD", "USDCAD", "USDJPY"],
        "timeframes": ["H1", "H4", "D1"],
        "description": "EMA crossover with ADX trend strength filter. "
        "Only takes trades when ADX confirms a trending market. "
        "Avoids choppy conditions.",
    },
}


# ---------------------------------------------------------------------------
# Template definitions
# ---------------------------------------------------------------------------


def _build_irb_conservative() -> StrategyDoctrine:
    """IRB with tight risk and high confirmation requirements."""
    return StrategyDoctrine(
        name="IRB Conservative",
        version="1.0.0",
        author="nova",
        description="Institutional Rejection Block — conservative variant with "
        "tight risk, high confirmation, and strict trend alignment.",
        concept="IRB reversal with strict filters",
        setup_type="reversal",
        entry_signal="irb_rejection",
        confirmations=["ema_trend", "adx_strength"],
        exit_method="trailing_atr",
        data=DoctrineData(
            pair="EURUSD",
            timeframes=["H1", "H4"],
            min_bars=5000,
        ),
        mutable_params={
            "irb_threshold": ParamBound(default=0.50, min=0.40, max=0.60, step=0.01),
            "ema_period": ParamBound(default=20, min=15, max=30, step=1),
            "atr_period": ParamBound(default=14, min=10, max=21, step=1),
            "adx_period": ParamBound(default=14, min=10, max=18, step=1),
            "trend_slope_threshold": ParamBound(default=0.5, min=0.3, max=1.0, step=0.05),
            "adx_threshold": ParamBound(default=25.0, min=20.0, max=30.0, step=0.5),
            "overextension_threshold": ParamBound(default=2.0, min=1.8, max=2.5, step=0.1),
            "trail_atr_multiplier": ParamBound(default=1.5, min=1.0, max=2.0, step=0.1),
            "trigger_window_bars": ParamBound(default=15, min=10, max=25, step=1),
            "time_stop_bars": ParamBound(default=30, min=20, max=50, step=1),
            "mtf_lookback": ParamBound(default=5, min=3, max=10, step=1),
            "warmup_bars": ParamBound(default=34, min=25, max=50, step=1),
        },
        immutable_params={
            "risk_per_trade_pct": 0.5,
            "max_concurrent_trades": 1,
            "max_daily_loss_pct": 2.0,
        },
        filters_mandatory=["trend_alignment", "adx_strength"],
        filters_optional=["overextension_guard", "session_london_ny_overlap"],
        filters_forbidden=["regime_low_volatility_skip"],
        execution=DoctrineExecution(
            spread_pips=1.5,
            slippage_pips=0.5,
            commission_per_lot=7.0,
            leverage=30,
        ),
        validation=DoctrineValidation(
            min_trades=50,
            min_months=6,
            max_drawdown_pct=12.0,
            min_profit_factor=1.5,
            min_sharpe=0.7,
            walk_forward_windows=6,
            walk_forward_efficiency_min=0.55,
            max_pbo=0.40,
        ),
        campaign=DoctrineCampaign(
            max_experiments=500,
            search_strategy="auto",
            wall_clock_limit_hours=4.0,
            stagnation_limit=60,
        ),
        invalidation_rules=[
            "profit_factor < 1.0 after Stage A",
            "max_drawdown > 15% under stress",
            "WFE < 0.4",
        ],
    )


def _build_irb_aggressive() -> StrategyDoctrine:
    """IRB with wider stops and more trade frequency."""
    return StrategyDoctrine(
        name="IRB Aggressive",
        version="1.0.0",
        author="nova",
        description="Institutional Rejection Block — aggressive variant with "
        "wider stops, relaxed filters, and more trade opportunities.",
        concept="IRB reversal with relaxed filters for more trades",
        setup_type="reversal",
        entry_signal="irb_rejection",
        confirmations=["ema_trend"],
        exit_method="trailing_atr",
        data=DoctrineData(
            pair="EURUSD",
            timeframes=["H1", "H4"],
            min_bars=5000,
        ),
        mutable_params={
            "irb_threshold": ParamBound(default=0.40, min=0.30, max=0.55, step=0.01),
            "ema_period": ParamBound(default=20, min=10, max=50, step=1),
            "atr_period": ParamBound(default=14, min=7, max=21, step=1),
            "adx_period": ParamBound(default=14, min=7, max=21, step=1),
            "trend_slope_threshold": ParamBound(default=0.3, min=0.1, max=0.8, step=0.05),
            "adx_threshold": ParamBound(default=18.0, min=12.0, max=25.0, step=0.5),
            "overextension_threshold": ParamBound(default=2.5, min=1.5, max=3.5, step=0.1),
            "trail_atr_multiplier": ParamBound(default=2.0, min=1.0, max=3.0, step=0.1),
            "trigger_window_bars": ParamBound(default=25, min=10, max=40, step=1),
            "time_stop_bars": ParamBound(default=50, min=20, max=80, step=1),
            "mtf_lookback": ParamBound(default=5, min=1, max=20, step=1),
            "warmup_bars": ParamBound(default=34, min=20, max=60, step=1),
        },
        immutable_params={
            "risk_per_trade_pct": 1.0,
            "max_concurrent_trades": 2,
            "max_daily_loss_pct": 4.0,
        },
        filters_mandatory=["trend_alignment"],
        filters_optional=["adx_strength", "overextension_guard", "session_london_ny_overlap"],
        execution=DoctrineExecution(
            spread_pips=1.5,
            slippage_pips=0.5,
            commission_per_lot=7.0,
            leverage=30,
        ),
        validation=DoctrineValidation(
            min_trades=30,
            min_months=6,
            max_drawdown_pct=18.0,
            min_profit_factor=1.2,
            min_sharpe=0.4,
            walk_forward_windows=6,
            walk_forward_efficiency_min=0.45,
            max_pbo=0.50,
        ),
        campaign=DoctrineCampaign(
            max_experiments=800,
            search_strategy="auto",
            wall_clock_limit_hours=6.0,
            stagnation_limit=80,
        ),
    )


def _build_irb_scalper() -> StrategyDoctrine:
    """IRB scalper — quick entries, minimal hold time."""
    return StrategyDoctrine(
        name="IRB Scalper",
        version="1.0.0",
        author="nova",
        description="Institutional Rejection Block — scalping variant with "
        "quick entries, tight stops, and minimal hold time.",
        concept="IRB scalping with fast entry and exit",
        setup_type="reversal",
        entry_signal="irb_rejection",
        confirmations=["ema_trend"],
        exit_method="trailing_atr",
        data=DoctrineData(
            pair="EURUSD",
            timeframes=["M5", "M15"],
            min_bars=8000,
        ),
        mutable_params={
            "irb_threshold": ParamBound(default=0.35, min=0.25, max=0.50, step=0.01),
            "ema_period": ParamBound(default=12, min=5, max=25, step=1),
            "atr_period": ParamBound(default=10, min=5, max=14, step=1),
            "adx_period": ParamBound(default=10, min=7, max=14, step=1),
            "trend_slope_threshold": ParamBound(default=0.3, min=0.1, max=0.6, step=0.05),
            "adx_threshold": ParamBound(default=20.0, min=15.0, max=28.0, step=0.5),
            "trail_atr_multiplier": ParamBound(default=1.0, min=0.5, max=2.0, step=0.1),
            "trigger_window_bars": ParamBound(default=10, min=5, max=20, step=1),
            "time_stop_bars": ParamBound(default=15, min=5, max=30, step=1),
            "mtf_lookback": ParamBound(default=3, min=1, max=8, step=1),
            "warmup_bars": ParamBound(default=20, min=10, max=30, step=1),
        },
        immutable_params={
            "risk_per_trade_pct": 0.5,
            "max_concurrent_trades": 1,
            "max_daily_loss_pct": 2.0,
        },
        filters_mandatory=["trend_alignment", "session_london_ny_overlap"],
        filters_optional=["adx_strength"],
        execution=DoctrineExecution(
            spread_pips=0.8,
            slippage_pips=0.3,
            commission_per_lot=7.0,
            leverage=30,
        ),
        validation=DoctrineValidation(
            min_trades=100,
            min_months=3,
            max_drawdown_pct=10.0,
            min_profit_factor=1.3,
            min_sharpe=0.5,
            walk_forward_windows=8,
            walk_forward_efficiency_min=0.50,
            max_pbo=0.45,
        ),
        campaign=DoctrineCampaign(
            max_experiments=600,
            search_strategy="auto",
            wall_clock_limit_hours=3.0,
            stagnation_limit=50,
        ),
        invalidation_rules=[
            "profit_factor < 1.0 after Stage A",
            "max_drawdown > 12% under stress",
            "average hold time > 20 bars",
        ],
    )


def _build_mean_reversion_bollinger() -> StrategyDoctrine:
    """Bollinger Band mean reversion with RSI confirmation."""
    return StrategyDoctrine(
        name="Mean Reversion Bollinger",
        version="1.0.0",
        author="nova",
        description="Mean reversion using Bollinger Bands with RSI divergence "
        "confirmation. Enters on band touch with extreme RSI.",
        concept="Bollinger band mean reversion with RSI filter",
        setup_type="mean_reversion",
        entry_signal="bollinger_bounce",
        confirmations=["rsi_filter"],
        exit_method="trailing_atr",
        data=DoctrineData(
            pair="EURUSD",
            timeframes=["H1", "H4"],
            min_bars=5000,
        ),
        mutable_params={
            "bb_period": ParamBound(default=20, min=10, max=50, step=1),
            "bb_std_dev": ParamBound(default=2.0, min=1.5, max=3.0, step=0.1),
            "rsi_period": ParamBound(default=14, min=7, max=21, step=1),
            "rsi_oversold": ParamBound(default=30.0, min=20.0, max=40.0, step=1.0),
            "rsi_overbought": ParamBound(default=70.0, min=60.0, max=80.0, step=1.0),
            "atr_period": ParamBound(default=14, min=7, max=21, step=1),
            "trail_atr_multiplier": ParamBound(default=1.5, min=1.0, max=2.5, step=0.1),
            "time_stop_bars": ParamBound(default=30, min=15, max=60, step=1),
        },
        immutable_params={
            "risk_per_trade_pct": 1.0,
            "max_concurrent_trades": 1,
            "max_daily_loss_pct": 3.0,
        },
        filters_mandatory=[],
        filters_optional=["regime_low_volatility_skip", "session_london_ny_overlap"],
        execution=DoctrineExecution(
            spread_pips=1.5,
            slippage_pips=0.5,
            commission_per_lot=7.0,
            leverage=30,
        ),
        validation=DoctrineValidation(
            min_trades=50,
            min_months=6,
            max_drawdown_pct=15.0,
            min_profit_factor=1.3,
            min_sharpe=0.5,
            walk_forward_windows=6,
            walk_forward_efficiency_min=0.50,
            max_pbo=0.50,
        ),
        campaign=DoctrineCampaign(
            max_experiments=400,
            search_strategy="auto",
            wall_clock_limit_hours=4.0,
            stagnation_limit=50,
        ),
    )


def _build_breakout_donchian() -> StrategyDoctrine:
    """Donchian channel breakout with volume confirmation."""
    return StrategyDoctrine(
        name="Breakout Donchian",
        version="1.0.0",
        author="nova",
        description="Donchian channel breakout with volume spike confirmation. "
        "Captures trend starts after consolidation breaks.",
        concept="Donchian channel breakout with volume confirmation",
        setup_type="breakout",
        entry_signal="range_breakout",
        confirmations=["volume_confirmation"],
        exit_method="trailing_atr",
        data=DoctrineData(
            pair="EURUSD",
            timeframes=["H1", "H4"],
            min_bars=5000,
        ),
        mutable_params={
            "lookback_period": ParamBound(default=20, min=10, max=50, step=1),
            "breakout_threshold": ParamBound(default=1.0, min=0.5, max=2.0, step=0.1),
            "atr_period": ParamBound(default=14, min=7, max=21, step=1),
            "trail_atr_multiplier": ParamBound(default=2.0, min=1.0, max=3.0, step=0.1),
            "time_stop_bars": ParamBound(default=40, min=20, max=80, step=1),
            "volume_confirmation_mult": ParamBound(default=1.5, min=1.0, max=3.0, step=0.1),
        },
        immutable_params={
            "risk_per_trade_pct": 1.0,
            "max_concurrent_trades": 1,
            "max_daily_loss_pct": 3.0,
        },
        filters_mandatory=[],
        filters_optional=["session_london", "regime_trending_only"],
        execution=DoctrineExecution(
            spread_pips=1.5,
            slippage_pips=0.5,
            commission_per_lot=7.0,
            leverage=30,
        ),
        validation=DoctrineValidation(
            min_trades=40,
            min_months=6,
            max_drawdown_pct=15.0,
            min_profit_factor=1.3,
            min_sharpe=0.5,
            walk_forward_windows=6,
            walk_forward_efficiency_min=0.50,
            max_pbo=0.50,
        ),
        campaign=DoctrineCampaign(
            max_experiments=400,
            search_strategy="auto",
            wall_clock_limit_hours=4.0,
            stagnation_limit=50,
        ),
    )


def _build_trend_ema_cross() -> StrategyDoctrine:
    """EMA crossover with ADX trend strength filter."""
    return StrategyDoctrine(
        name="Trend EMA Cross",
        version="1.0.0",
        author="nova",
        description="EMA crossover trend following with ADX filter. Only enters when ADX confirms a trending market.",
        concept="EMA crossover with ADX trend strength filter",
        setup_type="trend_following",
        entry_signal="ema_crossover",
        confirmations=["trend_slope", "adx_strength"],
        exit_method="trailing_atr",
        data=DoctrineData(
            pair="EURUSD",
            timeframes=["H1", "H4"],
            min_bars=5000,
        ),
        mutable_params={
            "fast_ema": ParamBound(default=12, min=5, max=25, step=1),
            "slow_ema": ParamBound(default=26, min=15, max=50, step=1),
            "atr_period": ParamBound(default=14, min=7, max=21, step=1),
            "adx_period": ParamBound(default=14, min=7, max=21, step=1),
            "adx_threshold": ParamBound(default=20.0, min=15.0, max=30.0, step=0.5),
            "trail_atr_multiplier": ParamBound(default=2.0, min=1.0, max=3.0, step=0.1),
            "trend_slope_threshold": ParamBound(default=0.3, min=0.1, max=1.0, step=0.05),
            "time_stop_bars": ParamBound(default=60, min=20, max=120, step=5),
        },
        immutable_params={
            "risk_per_trade_pct": 1.0,
            "max_concurrent_trades": 1,
            "max_daily_loss_pct": 3.0,
        },
        filters_mandatory=["trend_alignment"],
        filters_optional=["session_london_ny_overlap", "regime_trending_only"],
        execution=DoctrineExecution(
            spread_pips=1.5,
            slippage_pips=0.5,
            commission_per_lot=7.0,
            leverage=30,
        ),
        validation=DoctrineValidation(
            min_trades=40,
            min_months=6,
            max_drawdown_pct=15.0,
            min_profit_factor=1.3,
            min_sharpe=0.5,
            walk_forward_windows=6,
            walk_forward_efficiency_min=0.50,
            max_pbo=0.50,
        ),
        campaign=DoctrineCampaign(
            max_experiments=500,
            search_strategy="auto",
            wall_clock_limit_hours=4.0,
            stagnation_limit=50,
        ),
    )


# ---------------------------------------------------------------------------
# Template registry
# ---------------------------------------------------------------------------

_TEMPLATE_BUILDERS: dict[str, Any] = {
    "irb_conservative": _build_irb_conservative,
    "irb_aggressive": _build_irb_aggressive,
    "irb_scalper": _build_irb_scalper,
    "mean_reversion_bollinger": _build_mean_reversion_bollinger,
    "breakout_donchian": _build_breakout_donchian,
    "trend_ema_cross": _build_trend_ema_cross,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def list_doctrine_templates() -> list[dict[str, Any]]:
    """Return metadata for all available doctrine templates.

    Returns:
        List of dicts with ``name``, ``description``, ``pairs``, ``timeframes``.
    """
    result = []
    for name in _TEMPLATE_BUILDERS:
        info = _RECOMMENDED.get(name, {})
        result.append(
            {
                "name": name,
                "description": info.get("description", ""),
                "pairs": info.get("pairs", []),
                "timeframes": info.get("timeframes", []),
            }
        )
    return result


def get_doctrine_template(name: str) -> StrategyDoctrine:
    """Load a doctrine template by name.

    Args:
        name: Template name (e.g. ``"irb_conservative"``).

    Returns:
        A validated StrategyDoctrine with default settings.

    Raises:
        KeyError: If *name* is not a known template.
    """
    builder = _TEMPLATE_BUILDERS.get(name)
    if builder is None:
        available = ", ".join(sorted(_TEMPLATE_BUILDERS.keys()))
        raise KeyError(f"Unknown doctrine template '{name}'. Available: {available}")
    return builder()


def create_doctrine_from_template(
    template_name: str,
    *,
    pair: str | None = None,
    timeframe: str | None = None,
    **overrides: Any,
) -> StrategyDoctrine:
    """Create a doctrine from a template with optional overrides.

    Override any top-level field or nested data field.  The most common
    overrides are ``pair`` and ``timeframe``.

    Args:
        template_name: Template name (e.g. ``"irb_conservative"``).
        pair: Override currency pair.
        timeframe: Override primary timeframe (confirmation TF auto-derived).
        **overrides: Any other StrategyDoctrine field to override
            (e.g. ``name="My Strategy"``, ``version="2.0.0"``).

    Returns:
        A new StrategyDoctrine with overrides applied.

    Raises:
        KeyError: If *template_name* is not a known template.
        pydantic.ValidationError: If overrides produce invalid doctrine.
    """
    base = get_doctrine_template(template_name)
    data = base.model_dump(mode="json")

    # Apply pair/timeframe overrides to the nested data section
    if pair is not None:
        data["data"]["pair"] = pair
    if timeframe is not None:
        tf_map = {
            "M5": ["M5", "M15"],
            "M15": ["M15", "H1"],
            "M30": ["M30", "H1"],
            "H1": ["H1", "H4"],
            "H4": ["H4", "D1"],
            "D1": ["D1", "W1"],
        }
        data["data"]["timeframes"] = tf_map.get(timeframe, [timeframe])

    # Apply remaining overrides to top-level fields
    for key, value in overrides.items():
        if key in data:
            data[key] = value

    return StrategyDoctrine.model_validate(data)
