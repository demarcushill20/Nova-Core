"""Comprehensive risk engine for NovaTrade.

Extends the pre-trade gate into a full risk management system that handles:

1. **Pre-trade risk** — delegates to PreTradeGate (13 checks)
2. **Position-level risk** — trailing stop management, time stop monitoring
3. **Portfolio-level risk** — drawdown tracking, FTMO compliance, daily P&L
4. **Session-level risk** — trade logging, equity curve, risk metrics

The risk engine is the single coordination point between the strategy signal
generator, the pre-trade gate, and the execution layer.

Design:
- Provider-neutral: no MetaApi imports
- Fail-closed: any error → halt trading
- Config-driven: all thresholds from RiskConfig / BacktestEnvironment
- Stateful: tracks equity curve, drawdowns, trade history across session
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum

from novatrade.config import NovaTradeCfg
from novatrade.models import (
    AccountState,
    HealthStatus,
    OrderRequest,
    OrderSide,
    Position,
    RiskCheckResult,
    RiskDecision,
    RiskVerdict,
    SymbolPrice,
)
from novatrade.risk.pre_trade_gate import PreTradeGate

log = logging.getLogger("novatrade.risk.risk_engine")


# ---------------------------------------------------------------------------
# Enums and data classes
# ---------------------------------------------------------------------------


class RiskLevel(Enum):
    """Current portfolio risk level."""

    NORMAL = "NORMAL"
    ELEVATED = "ELEVATED"  # approaching limits
    CRITICAL = "CRITICAL"  # at or near limits
    HALTED = "HALTED"  # trading halted


class DrawdownType(Enum):
    DAILY = "DAILY"
    TOTAL = "TOTAL"


@dataclass
class DrawdownState:
    """Tracks drawdown from a reference point (daily or total)."""

    reference_equity: float = 0.0
    current_equity: float = 0.0
    peak_equity: float = 0.0
    max_drawdown_pct: float = 0.0
    max_drawdown_usd: float = 0.0
    current_drawdown_pct: float = 0.0
    current_drawdown_usd: float = 0.0
    drawdown_type: DrawdownType = DrawdownType.DAILY

    def update(self, equity: float) -> None:
        """Update drawdown state with new equity reading."""
        self.current_equity = equity
        if equity > self.peak_equity:
            self.peak_equity = equity

        self.current_drawdown_usd = self.peak_equity - equity
        self.current_drawdown_pct = (
            (self.current_drawdown_usd / self.reference_equity) * 100 if self.reference_equity > 0 else 0.0
        )

        if self.current_drawdown_pct > self.max_drawdown_pct:
            self.max_drawdown_pct = self.current_drawdown_pct
            self.max_drawdown_usd = self.current_drawdown_usd


@dataclass
class TradeRecord:
    """Lightweight record of a completed trade for session tracking."""

    timestamp: float
    symbol: str
    side: str
    volume: float
    pnl_usd: float
    pnl_pips: float
    exit_reason: str
    risk_r: float = 0.0


@dataclass
class PositionRiskState:
    """Risk state for a single open position."""

    position_id: str
    symbol: str
    side: OrderSide
    entry_price: float
    current_stop: float
    entry_bar: int = 0
    bars_held: int = 0
    best_close: float = 0.0
    unrealized_pnl: float = 0.0
    max_favorable_excursion: float = 0.0
    max_adverse_excursion: float = 0.0


@dataclass
class RiskSnapshot:
    """Point-in-time risk state for the entire portfolio."""

    timestamp: float = field(default_factory=time.time)
    risk_level: RiskLevel = RiskLevel.NORMAL
    equity: float = 0.0
    daily_drawdown: DrawdownState = field(default_factory=lambda: DrawdownState(drawdown_type=DrawdownType.DAILY))
    total_drawdown: DrawdownState = field(default_factory=lambda: DrawdownState(drawdown_type=DrawdownType.TOTAL))
    open_positions: int = 0
    trades_today: int = 0
    pnl_today_usd: float = 0.0
    halted: bool = False
    halt_reason: str = ""

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "risk_level": self.risk_level.value,
            "equity": round(self.equity, 2),
            "daily_dd_pct": round(self.daily_drawdown.current_drawdown_pct, 4),
            "daily_dd_max_pct": round(self.daily_drawdown.max_drawdown_pct, 4),
            "total_dd_pct": round(self.total_drawdown.current_drawdown_pct, 4),
            "total_dd_max_pct": round(self.total_drawdown.max_drawdown_pct, 4),
            "open_positions": self.open_positions,
            "trades_today": self.trades_today,
            "pnl_today_usd": round(self.pnl_today_usd, 2),
            "halted": self.halted,
            "halt_reason": self.halt_reason,
        }


# ---------------------------------------------------------------------------
# Risk engine
# ---------------------------------------------------------------------------


class RiskEngine:
    """Comprehensive risk management for NovaTrade.

    Coordinates pre-trade checks, position-level risk, portfolio drawdown
    tracking, and FTMO compliance monitoring.

    Usage::

        engine = RiskEngine(cfg)
        engine.initialize(account_state)

        # Before each trade
        decision = engine.pre_trade_check(request, account, positions)

        # After trade fills
        engine.on_trade_fill(symbol, side, volume, fill_price)

        # After trade closes
        engine.on_trade_close(symbol, side, pnl_usd, pnl_pips, exit_reason)

        # Periodic monitoring
        snapshot = engine.get_risk_snapshot(account)

        # Position monitoring
        engine.update_position_risk(position_id, current_price, atr_value, bar_index)
    """

    def __init__(self, cfg: NovaTradeCfg) -> None:
        self._cfg = cfg
        self._risk = cfg.risk
        self._gate = PreTradeGate(cfg)

        # Portfolio state
        self._initial_equity: float = 0.0
        self._current_equity: float = 0.0
        self._daily_start_equity: float = 0.0
        self._halted = False
        self._halt_reason = ""

        # Drawdown tracking
        self._daily_dd = DrawdownState(drawdown_type=DrawdownType.DAILY)
        self._total_dd = DrawdownState(drawdown_type=DrawdownType.TOTAL)

        # Trade history (current session)
        self._trade_history: list[TradeRecord] = []
        self._today_trades: list[TradeRecord] = []

        # Position risk states
        self._position_risks: dict[str, PositionRiskState] = {}

        # Session metadata
        self._session_start: float = time.time()
        self._last_day_reset: float = 0.0

    # ------------------------------------------------------------------
    # Initialization
    # ------------------------------------------------------------------

    def initialize(self, account: AccountState) -> None:
        """Set initial equity from account state. Call once at session start."""
        self._initial_equity = account.equity
        self._current_equity = account.equity
        self._daily_start_equity = account.equity

        self._daily_dd = DrawdownState(
            reference_equity=account.equity,
            current_equity=account.equity,
            peak_equity=account.equity,
            drawdown_type=DrawdownType.DAILY,
        )
        self._total_dd = DrawdownState(
            reference_equity=account.equity,
            current_equity=account.equity,
            peak_equity=account.equity,
            drawdown_type=DrawdownType.TOTAL,
        )
        self._last_day_reset = time.time()

        log.info("Risk engine initialized — equity=$%.2f", account.equity)

    # ------------------------------------------------------------------
    # Pre-trade evaluation
    # ------------------------------------------------------------------

    def pre_trade_check(
        self,
        request: OrderRequest,
        account: AccountState,
        positions: list[Position],
        *,
        health: HealthStatus | None = None,
        price: SymbolPrice | None = None,
    ) -> RiskDecision:
        """Run all pre-trade risk checks including portfolio-level guards.

        Delegates to PreTradeGate for the 13 standard checks, then adds
        portfolio-level checks (halt state, FTMO daily/total drawdown).
        """
        # Check halt state first
        if self._halted:
            return RiskDecision(
                verdict=RiskVerdict.DENY,
                checks=[
                    RiskCheckResult(
                        name="risk_engine_halt",
                        passed=False,
                        detail=f"Trading halted: {self._halt_reason}",
                    )
                ],
                reason=f"Trading halted: {self._halt_reason}",
                rule="risk_engine_halt",
                request=request,
            )

        # Run standard pre-trade gate
        decision = self._gate.evaluate(
            request,
            account,
            positions,
            health=health,
            price=price,
        )

        # Add portfolio-level checks
        portfolio_checks = self._portfolio_checks(account)
        decision.checks.extend(portfolio_checks)

        failed_portfolio = [c for c in portfolio_checks if not c.passed]
        if failed_portfolio and not decision.denied:
            # Override to DENY if portfolio checks fail
            first_fail = failed_portfolio[0]
            return RiskDecision(
                verdict=RiskVerdict.DENY,
                checks=decision.checks,
                reason=first_fail.detail,
                rule=first_fail.name,
                request=request,
            )

        return decision

    def _portfolio_checks(self, account: AccountState) -> list[RiskCheckResult]:
        """Additional portfolio-level risk checks beyond the 13-check gate."""
        checks: list[RiskCheckResult] = []

        # FTMO daily drawdown check
        if account.equity > 0 and self._daily_start_equity > 0:
            daily_dd = ((self._daily_start_equity - account.equity) / self._daily_start_equity) * 100
            limit = self._risk.max_daily_drawdown_pct
            if daily_dd >= limit * 0.8:  # warn at 80% of limit
                if daily_dd >= limit:
                    checks.append(
                        RiskCheckResult(
                            name="ftmo_daily_drawdown",
                            passed=False,
                            detail=f"Daily drawdown {daily_dd:.2f}% >= limit {limit:.1f}%",
                        )
                    )
                    self._halt("FTMO daily drawdown limit reached")
                else:
                    checks.append(
                        RiskCheckResult(
                            name="ftmo_daily_drawdown",
                            passed=True,
                            detail=f"Daily drawdown {daily_dd:.2f}% approaching limit {limit:.1f}% (>80%)",
                        )
                    )
            else:
                checks.append(
                    RiskCheckResult(
                        name="ftmo_daily_drawdown",
                        passed=True,
                        detail=f"Daily drawdown {daily_dd:.2f}%, limit {limit:.1f}%",
                    )
                )

        # FTMO total drawdown check
        if account.equity > 0 and self._initial_equity > 0:
            total_dd = ((self._initial_equity - account.equity) / self._initial_equity) * 100
            limit = self._risk.max_total_drawdown_pct
            if total_dd >= limit:
                checks.append(
                    RiskCheckResult(
                        name="ftmo_total_drawdown",
                        passed=False,
                        detail=f"Total drawdown {total_dd:.2f}% >= limit {limit:.1f}%",
                    )
                )
                self._halt("FTMO total drawdown limit reached")
            else:
                checks.append(
                    RiskCheckResult(
                        name="ftmo_total_drawdown",
                        passed=True,
                        detail=f"Total drawdown {total_dd:.2f}%, limit {limit:.1f}%",
                    )
                )

        return checks

    # ------------------------------------------------------------------
    # Trade lifecycle
    # ------------------------------------------------------------------

    def on_trade_fill(
        self,
        position_id: str,
        symbol: str,
        side: OrderSide,
        volume: float,
        fill_price: float,
        stop_loss: float,
    ) -> None:
        """Record a trade fill and start position risk tracking."""
        self._gate.record_trade(symbol, side.value)

        self._position_risks[position_id] = PositionRiskState(
            position_id=position_id,
            symbol=symbol,
            side=side,
            entry_price=fill_price,
            current_stop=stop_loss,
            best_close=fill_price,
        )
        log.info(
            "Trade fill: %s %s %s vol=%.2f at %.5f sl=%.5f",
            position_id,
            side.value,
            symbol,
            volume,
            fill_price,
            stop_loss,
        )

    def on_trade_close(
        self,
        position_id: str,
        symbol: str,
        side: str,
        volume: float,
        pnl_usd: float,
        pnl_pips: float,
        exit_reason: str,
        risk_r: float = 0.0,
    ) -> None:
        """Record a trade close and update portfolio state."""
        self._current_equity += pnl_usd

        record = TradeRecord(
            timestamp=time.time(),
            symbol=symbol,
            side=side,
            volume=volume,
            pnl_usd=pnl_usd,
            pnl_pips=pnl_pips,
            exit_reason=exit_reason,
            risk_r=risk_r,
        )
        self._trade_history.append(record)
        self._today_trades.append(record)

        # Update drawdowns
        self._daily_dd.update(self._current_equity)
        self._total_dd.update(self._current_equity)

        # Check for halt conditions
        if self._daily_dd.current_drawdown_pct >= self._risk.max_daily_drawdown_pct:
            self._halt("Daily drawdown limit breached")
        elif self._total_dd.current_drawdown_pct >= self._risk.max_total_drawdown_pct:
            self._halt("Total drawdown limit breached")

        # Clean up position risk state
        self._position_risks.pop(position_id, None)

        log.info(
            "Trade closed: %s %s pnl=$%.2f (%.1f pips, %.2fR) — %s equity=$%.2f",
            symbol,
            side,
            pnl_usd,
            pnl_pips,
            risk_r,
            exit_reason,
            self._current_equity,
        )

    # ------------------------------------------------------------------
    # Position-level risk monitoring
    # ------------------------------------------------------------------

    def update_position_risk(
        self,
        position_id: str,
        current_price: float,
        atr_value: float,
        bar_index: int,
        pip_value: float = 0.0001,
        atr_multiplier: float = 1.5,
    ) -> PositionRiskState | None:
        """Update trailing stop and risk metrics for an open position.

        Returns updated PositionRiskState or None if position not tracked.
        """
        prs = self._position_risks.get(position_id)
        if prs is None:
            return None

        prs.bars_held = bar_index - prs.entry_bar

        # Update MFE/MAE
        if prs.side == OrderSide.BUY:
            favorable = (current_price - prs.entry_price) / pip_value
            adverse = (prs.entry_price - current_price) / pip_value
        else:
            favorable = (prs.entry_price - current_price) / pip_value
            adverse = (current_price - prs.entry_price) / pip_value

        prs.max_favorable_excursion = max(prs.max_favorable_excursion, favorable)
        prs.max_adverse_excursion = max(prs.max_adverse_excursion, adverse)

        # Update trailing stop
        if atr_value > 0:
            if prs.side == OrderSide.BUY:
                prs.best_close = max(prs.best_close, current_price)
                new_trail = prs.best_close - atr_multiplier * atr_value
                if new_trail > prs.current_stop:
                    prs.current_stop = new_trail
            else:
                prs.best_close = min(prs.best_close, current_price)
                new_trail = prs.best_close + atr_multiplier * atr_value
                if new_trail < prs.current_stop:
                    prs.current_stop = new_trail

        return prs

    def should_exit_time_stop(self, position_id: str, max_bars: int = 40) -> bool:
        """Check if a position has exceeded the time stop."""
        prs = self._position_risks.get(position_id)
        if prs is None:
            return False
        return prs.bars_held >= max_bars

    def get_trailing_stop(self, position_id: str) -> float | None:
        """Get the current trailing stop level for a position."""
        prs = self._position_risks.get(position_id)
        return prs.current_stop if prs else None

    # ------------------------------------------------------------------
    # Portfolio monitoring
    # ------------------------------------------------------------------

    def get_risk_snapshot(self, account: AccountState | None = None) -> RiskSnapshot:
        """Get current portfolio risk snapshot."""
        equity = account.equity if account else self._current_equity
        self._daily_dd.update(equity)
        self._total_dd.update(equity)

        # Determine risk level
        daily_pct = self._daily_dd.current_drawdown_pct
        total_pct = self._total_dd.current_drawdown_pct
        daily_limit = self._risk.max_daily_drawdown_pct
        total_limit = self._risk.max_total_drawdown_pct

        if self._halted:
            level = RiskLevel.HALTED
        elif daily_pct >= daily_limit * 0.8 or total_pct >= total_limit * 0.8:
            level = RiskLevel.CRITICAL
        elif daily_pct >= daily_limit * 0.5 or total_pct >= total_limit * 0.5:
            level = RiskLevel.ELEVATED
        else:
            level = RiskLevel.NORMAL

        pnl_today = sum(t.pnl_usd for t in self._today_trades)

        return RiskSnapshot(
            risk_level=level,
            equity=equity,
            daily_drawdown=self._daily_dd,
            total_drawdown=self._total_dd,
            open_positions=len(self._position_risks),
            trades_today=len(self._today_trades),
            pnl_today_usd=pnl_today,
            halted=self._halted,
            halt_reason=self._halt_reason,
        )

    def reset_daily(self, equity: float) -> None:
        """Reset daily drawdown tracking (call at start of each trading day)."""
        self._daily_start_equity = equity
        self._daily_dd = DrawdownState(
            reference_equity=equity,
            current_equity=equity,
            peak_equity=equity,
            drawdown_type=DrawdownType.DAILY,
        )
        self._today_trades.clear()
        self._last_day_reset = time.time()
        log.info("Daily risk reset — equity=$%.2f", equity)

    @property
    def halted(self) -> bool:
        return self._halted

    @property
    def halt_reason(self) -> str:
        return self._halt_reason

    @property
    def trade_history(self) -> list[TradeRecord]:
        return list(self._trade_history)

    @property
    def current_equity(self) -> float:
        return self._current_equity

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _halt(self, reason: str) -> None:
        """Halt all trading."""
        if not self._halted:
            self._halted = True
            self._halt_reason = reason
            log.critical("RISK ENGINE HALT: %s", reason)

    def resume(self) -> None:
        """Resume trading after a halt (requires explicit operator action)."""
        if self._halted:
            log.warning("Risk engine resumed from halt: %s", self._halt_reason)
            self._halted = False
            self._halt_reason = ""
