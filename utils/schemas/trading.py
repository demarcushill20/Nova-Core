"""Trading signal schemas for structured webhook/alert validation."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, field_validator


class SignalAction(str, Enum):
    SIGNAL = "signal"
    TRAIL = "trail"
    CANCEL = "cancel"
    CLOSE = "close"


class TradeSignal(BaseModel):
    """Structured trade signal from TradingView or LLM analysis."""

    action: SignalAction = Field(..., description="Signal action type")
    symbol: str = Field(..., min_length=1, max_length=20, description="Trading symbol e.g. EURUSD")
    side: str = Field(default="", description="buy or sell")
    entry_price: float | None = Field(default=None, gt=0, description="Entry price")
    stop_loss: float | None = Field(default=None, gt=0, description="Stop loss price")
    take_profit: float | None = Field(default=None, gt=0, description="Take profit price")
    lot_size: float | None = Field(default=None, gt=0, le=100, description="Position size in lots")
    timeframe: str = Field(default="", description="Chart timeframe e.g. 1H, 4H, D1")
    strategy: str = Field(default="", description="Strategy name that generated this signal")
    confidence: float = Field(default=0.5, ge=0.0, le=1.0, description="Signal confidence 0-1")
    metadata: dict = Field(default_factory=dict, description="Additional signal metadata")

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, v):
        if isinstance(v, str):
            return v.upper().strip()
        return v

    @field_validator("side", mode="before")
    @classmethod
    def normalize_side(cls, v):
        if isinstance(v, str):
            return v.lower().strip()
        return v


# ---------------------------------------------------------------------------
# Phase 5.7: IRB StrategySpec Schema
# ---------------------------------------------------------------------------


class IndicatorConfig(BaseModel):
    """Configuration for a single technical indicator."""

    name: str = Field(
        ...,
        min_length=1,
        max_length=50,
        description="Indicator name e.g. RSI, MACD, BB",
    )
    params: dict[str, float | int | str] = Field(
        default_factory=dict, description="Indicator parameters e.g. {'period': 14}"
    )
    weight: float = Field(default=1.0, ge=0.0, le=10.0, description="Weight in signal calculation")


class StrategyState(str, Enum):
    """5-state IRB strategy lifecycle."""

    DRAFT = "draft"
    BACKTEST = "backtest"
    PAPER = "paper"
    LIVE = "live"
    RETIRED = "retired"


class StrategySpec(BaseModel):
    """Full specification for an IRB (Indicator-Rule-Based) trading strategy."""

    name: str = Field(..., min_length=1, max_length=100, description="Strategy name")
    version: str = Field(default="1.0", description="Strategy version")
    state: StrategyState = Field(default=StrategyState.DRAFT, description="Current lifecycle state")
    symbols: list[str] = Field(default_factory=list, description="Symbols this strategy trades")
    timeframes: list[str] = Field(default_factory=list, description="Chart timeframes e.g. ['1H', '4H']")
    indicators: list[IndicatorConfig] = Field(default_factory=list, description="Technical indicators used")

    # Entry/exit rules
    entry_rules: list[str] = Field(default_factory=list, description="Human-readable entry conditions")
    exit_rules: list[str] = Field(default_factory=list, description="Human-readable exit conditions")

    # Risk parameters
    max_risk_pct: float = Field(default=1.0, gt=0.0, le=5.0, description="Max risk per trade as % of account")
    max_positions: int = Field(default=3, ge=1, le=20, description="Max concurrent positions")
    max_daily_trades: int = Field(default=10, ge=1, le=100, description="Max trades per day")

    # Backtest parameters
    backtest_start: str = Field(default="", description="Backtest start date ISO format")
    backtest_end: str = Field(default="", description="Backtest end date ISO format")
    min_win_rate: float = Field(default=0.5, ge=0.0, le=1.0, description="Minimum acceptable win rate")
    min_profit_factor: float = Field(default=1.2, ge=0.0, le=100.0, description="Minimum acceptable profit factor")

    metadata: dict = Field(default_factory=dict, description="Additional strategy metadata")

    @field_validator("symbols", mode="before")
    @classmethod
    def normalize_symbols(cls, v):
        if isinstance(v, list):
            return [s.upper().strip() for s in v if s]
        return v
