"""Comprehensive risk engine for NovaTrade.

Extends the pre-trade gate into a full risk management system that handles:

1. **Pre-trade risk** — 5-layer policy evaluation (Phase 6)
2. **Position-level risk** — trailing stop management, time stop monitoring
3. **Portfolio-level risk** — drawdown tracking, FTMO compliance, daily P&L
4. **Session-level risk** — forex market hours, trade logging, risk metrics

The risk engine is the single coordination point between the strategy signal
generator, the pre-trade gate, and the execution layer.

Policy layers (evaluated in order, short-circuit on DENY/HALT):
  Layer 0: System kill switch (halt state)
  Layer 1: Session policy (24/5 forex market hours)
  Layer 2: Drawdown governance (FTMO daily/total limits with tiered warnings)
  Layer 3: Exposure control (IRB max 1 position)
  Layer 4: Pre-trade gate (13 standard checks)

Design:
- Provider-neutral: no MetaApi imports
- Fail-closed: any error → halt trading
- Config-driven: all thresholds from RiskConfig / BacktestEnvironment
- Stateful: tracks equity curve, drawdowns, trade history across session
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from novatrade.config import NovaTradeCfg
from novatrade.models import (
    AccountState,
    HealthStatus,
    OrderRequest,
    OrderSide,
    Position,
    RiskAction,
    RiskCheckResult,
    RiskDecision,
    RiskVerdict,
    SymbolPrice,
)
from novatrade.risk.pre_trade_gate import PreTradeGate
from novatrade.risk.session_aware_filter import SessionAwareFilter

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


class DrawdownTier(Enum):
    """Drawdown warning tier for tiered escalation."""

    NORMAL = "NORMAL"
    ELEVATED = "ELEVATED"  # >= 60% of limit
    CRITICAL = "CRITICAL"  # >= 80% of limit
    BREACH = "BREACH"  # >= 100% of limit


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
        """Update drawdown state with new equity reading.

        M8 fix: drawdown is calculated from *reference_equity* (start-of-day
        or initial balance), matching FTMO's definition and consistent with
        ``_check_drawdown_governance()``.  Peak equity is still tracked for
        informational purposes (high-water mark), but drawdown metrics use
        reference as both numerator base and denominator.
        """
        self.current_equity = equity
        if equity > self.peak_equity:
            self.peak_equity = equity

        # Drawdown from reference equity (FTMO definition), not from peak
        self.current_drawdown_usd = max(self.reference_equity - equity, 0.0)
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
# Helpers
# ---------------------------------------------------------------------------


def _drawdown_tier(pct: float, limit: float) -> DrawdownTier:
    """Classify drawdown percentage into a warning tier."""
    if pct >= limit:
        return DrawdownTier.BREACH
    if pct >= limit * 0.8:
        return DrawdownTier.CRITICAL
    if pct >= limit * 0.6:
        return DrawdownTier.ELEVATED
    return DrawdownTier.NORMAL


def _is_forex_weekend(now: dt.datetime) -> bool:
    """Check if the given UTC datetime falls in the forex weekend close.

    Forex market: Sunday 22:00 UTC to Friday 22:00 UTC.
    Weekend close: Friday 22:00 UTC to Sunday 22:00 UTC.
    """
    weekday = now.weekday()  # 0=Mon, 4=Fri, 5=Sat, 6=Sun
    hour = now.hour
    if weekday == 4 and hour >= 22:  # Friday 22:00+
        return True
    if weekday == 5:  # Saturday (all day)
        return True
    return weekday == 6 and hour < 22  # Sunday before 22:00


# ---------------------------------------------------------------------------
# Risk engine
# ---------------------------------------------------------------------------


class RiskEngine:
    """Comprehensive risk management for NovaTrade.

    Coordinates pre-trade checks, position-level risk, portfolio drawdown
    tracking, and FTMO compliance monitoring.

    Phase 6 hardening: pre-trade evaluation is organized into 5 policy layers
    evaluated in sequence.  A DENY or HALT at any layer short-circuits the
    remaining layers.

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

        # Portfolio-level actions (Phase 6)
        actions = engine.get_required_actions()
    """

    def __init__(self, cfg: NovaTradeCfg) -> None:
        self._cfg = cfg
        self._risk = cfg.risk
        self._gate = PreTradeGate(cfg)

        # P4: Enhanced session-aware filtering with London-NY overlap focus
        self._session_filter = self._create_session_filter()

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

        # Injectable clock for testability (Phase 6)
        self._clock = _default_utc_now

    def _create_session_filter(self) -> SessionAwareFilter:
        """Create session filter based on risk configuration.

        P4: Enhanced Session-Aware Risk Management
        Reads configuration to create appropriate session filter with
        London-NY overlap focus and configurable quality thresholds.

        Returns:
            Configured SessionAwareFilter instance.
        """
        from novatrade.risk.session_aware_filter import SessionAwareConfig, SessionQuality

        # Map string quality to enum
        quality_map = {
            "minimum": SessionQuality.POOR,
            "acceptable": SessionQuality.ACCEPTABLE,
            "good": SessionQuality.GOOD,
            "premium": SessionQuality.PREMIUM,
        }
        min_quality = quality_map.get(self._risk.session_minimum_quality, SessionQuality.ACCEPTABLE)

        config = SessionAwareConfig(
            allow_overlap_only=self._risk.session_overlap_only,
            allow_premium_sessions=True,  # Always allow premium sessions
            allow_asian_session=self._risk.session_allow_asian,
            minimum_quality=min_quality,
            block_transition_periods=False,  # TODO: Future enhancement
        )

        return SessionAwareFilter(config)

    # ------------------------------------------------------------------
    # Initialization
    # ------------------------------------------------------------------

    def initialize(
        self,
        account: AccountState,
        *,
        state_file: str | Path | None = None,
    ) -> None:
        """Set initial equity from account state. Call once at session start.

        RC1 fix: reads persisted halt state from disk *before* resetting
        drawdown tracking.  If the previous session halted, the engine
        restores ``_halted`` and ``_halt_reason`` so trading cannot resume
        without an explicit ``resume()`` call by the operator.

        Args:
            account: Current account state with equity.
            state_file: Optional path to risk state JSON (for testing).
                        Defaults to ``STATE/novatrade_risk_state.json``.
        """
        # --- RC1: restore persisted halt state before anything else ---
        restored = self._restore_halt_state(state_file=state_file)

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

        if restored:
            log.info(
                "Risk engine initialized — equity=$%.2f (HALTED from prior session: %s)",
                account.equity,
                self._halt_reason,
            )
        else:
            log.info("Risk engine initialized — equity=$%.2f", account.equity)

    # ------------------------------------------------------------------
    # Pre-trade evaluation (Phase 6: 5-layer policy model)
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
        """Run all pre-trade risk checks in policy layer order.

        Layers are evaluated in sequence.  A DENY or HALT at any layer
        short-circuits remaining layers.  All checks within a layer run
        (no short-circuit within a layer) so the caller sees every failing
        rule at once.

        Layer 0: System kill switch (halt state)
        Layer 1: Session policy (24/5 forex market hours)
        Layer 2: Drawdown governance (FTMO daily/total with tiered warnings)
        Layer 3: Exposure control (IRB max 1 position)
        Layer 4: Pre-trade gate (13 standard checks)
        """
        all_checks: list[RiskCheckResult] = []

        # --- Layer 0: Halt state ---
        if self._halted:
            halt_check = RiskCheckResult(
                name="risk_engine_halt",
                passed=False,
                detail=f"Trading halted: {self._halt_reason}",
            )
            return RiskDecision(
                verdict=RiskVerdict.HALT,
                checks=[halt_check],
                reason=f"Trading halted: {self._halt_reason}",
                rule="risk_engine_halt",
                policy_layer=0,
                actions=[RiskAction.HALT_TRADING],
                request=request,
            )

        # --- Layer 1: Session policy (24/5 forex hours) ---
        if self._risk.check_forex_session:
            session_check = self._check_session()
            all_checks.append(session_check)
            if not session_check.passed:
                return RiskDecision(
                    verdict=RiskVerdict.DENY,
                    checks=all_checks,
                    reason=session_check.detail,
                    rule="forex_session",
                    policy_layer=1,
                    request=request,
                )

        # --- Layer 2: Drawdown governance (FTMO limits + tiered warnings) ---
        dd_checks, should_halt = self._check_drawdown_governance(account)
        all_checks.extend(dd_checks)
        if should_halt:
            first_fail = next((c for c in dd_checks if not c.passed), dd_checks[0])
            return RiskDecision(
                verdict=RiskVerdict.HALT,
                checks=all_checks,
                reason=first_fail.detail,
                rule=first_fail.name,
                policy_layer=2,
                actions=[RiskAction.HALT_TRADING],
                request=request,
            )

        # --- Layer 3: Exposure control (IRB-specific) ---
        if self._risk.irb_max_open_positions > 0:
            exposure_checks = self._check_irb_exposure(positions)
            all_checks.extend(exposure_checks)
            failed_exposure = [c for c in exposure_checks if not c.passed]
            if failed_exposure:
                first_fail = failed_exposure[0]
                return RiskDecision(
                    verdict=RiskVerdict.DENY,
                    checks=all_checks,
                    reason=first_fail.detail,
                    rule=first_fail.name,
                    policy_layer=3,
                    request=request,
                )

        # --- Layer 4: Pre-trade gate (13 standard checks) ---
        gate_decision = self._gate.evaluate(
            request,
            account,
            positions,
            health=health,
            price=price,
        )
        all_checks.extend(gate_decision.checks)

        if gate_decision.denied:
            return RiskDecision(
                verdict=RiskVerdict.DENY,
                checks=all_checks,
                reason=gate_decision.reason,
                rule=gate_decision.rule,
                policy_layer=4,
                request=request,
            )

        # All layers passed
        return RiskDecision(
            verdict=RiskVerdict.ALLOW,
            checks=all_checks,
            policy_layer=-1,
            request=request,
        )

    # ------------------------------------------------------------------
    # Layer 1: Session policy
    # ------------------------------------------------------------------

    def _check_session(self) -> RiskCheckResult:
        """Enhanced session-aware risk check with London-NY overlap focus.

        P4: Session-Aware Risk Management
        - Blocks weekend closure (market closed)
        - Evaluates session quality (Premium > Good > Acceptable > Poor)
        - Focuses on London-NY overlap for optimal execution
        - Configurable session preferences

        This replaces the basic weekend-only check with sophisticated session
        awareness to improve execution quality and FTMO compliance.
        """
        now = self._clock()
        session_info = self._session_filter.check_session(now)

        # Check if trading is allowed based on session filtering
        is_allowed = self._session_filter.is_trading_allowed(now)

        if not is_allowed:
            return RiskCheckResult(
                name="forex_session",
                passed=False,
                detail=f"Session blocked: {session_info.reason}",
            )

        # Include session quality information in success case
        quality_note = f" ({session_info.quality.value})" if session_info.quality else ""
        return RiskCheckResult(
            name="forex_session",
            passed=True,
            detail=f"Session allowed: {session_info.reason}{quality_note}",
        )

    # ------------------------------------------------------------------
    # Layer 2: Drawdown governance
    # ------------------------------------------------------------------

    def _check_drawdown_governance(self, account: AccountState) -> tuple[list[RiskCheckResult], bool]:
        """FTMO drawdown limits with tiered warnings.

        Returns ``(checks, should_halt)``.  Warning-tier transitions are
        logged but do not deny trades — only BREACH triggers a halt.

        RC2 fix: zero or negative equity is now an immediate emergency halt
        instead of silently skipping all drawdown checks.
        """
        checks: list[RiskCheckResult] = []
        should_halt = False

        # RC2: Zero or negative equity — emergency halt
        if account.equity <= 0:
            checks.append(
                RiskCheckResult(
                    name="equity_zero_or_negative",
                    passed=False,
                    detail=f"Equity at or below zero (${account.equity:.2f}) — emergency halt",
                )
            )
            self._halt(f"Equity at or below zero: ${account.equity:.2f}")
            return checks, True

        # Daily drawdown
        if self._daily_start_equity > 0:
            daily_dd = max(
                ((self._daily_start_equity - account.equity) / self._daily_start_equity) * 100,
                0.0,
            )
            limit = self._risk.max_daily_drawdown_pct
            tier = _drawdown_tier(daily_dd, limit)

            if tier == DrawdownTier.BREACH:
                checks.append(
                    RiskCheckResult(
                        name="ftmo_daily_drawdown",
                        passed=False,
                        detail=f"Daily drawdown {daily_dd:.2f}% >= limit {limit:.1f}% [BREACH]",
                    )
                )
                should_halt = True
                self._halt("FTMO daily drawdown limit reached")
            else:
                checks.append(
                    RiskCheckResult(
                        name="ftmo_daily_drawdown",
                        passed=True,
                        detail=f"Daily drawdown {daily_dd:.2f}%, limit {limit:.1f}% [{tier.value}]",
                    )
                )

            if tier in (DrawdownTier.ELEVATED, DrawdownTier.CRITICAL):
                log.warning(
                    "Daily drawdown %s: %.2f%% of %.1f%% limit",
                    tier.value,
                    daily_dd,
                    limit,
                )

        # Total drawdown
        if self._initial_equity > 0:
            total_dd = max(
                ((self._initial_equity - account.equity) / self._initial_equity) * 100,
                0.0,
            )
            limit = self._risk.max_total_drawdown_pct
            tier = _drawdown_tier(total_dd, limit)

            if tier == DrawdownTier.BREACH:
                checks.append(
                    RiskCheckResult(
                        name="ftmo_total_drawdown",
                        passed=False,
                        detail=f"Total drawdown {total_dd:.2f}% >= limit {limit:.1f}% [BREACH]",
                    )
                )
                should_halt = True
                self._halt("FTMO total drawdown limit reached")
            else:
                checks.append(
                    RiskCheckResult(
                        name="ftmo_total_drawdown",
                        passed=True,
                        detail=f"Total drawdown {total_dd:.2f}%, limit {limit:.1f}% [{tier.value}]",
                    )
                )

            if tier in (DrawdownTier.ELEVATED, DrawdownTier.CRITICAL):
                log.warning(
                    "Total drawdown %s: %.2f%% of %.1f%% limit",
                    tier.value,
                    total_dd,
                    limit,
                )

        return checks, should_halt

    # ------------------------------------------------------------------
    # Layer 3: Exposure control
    # ------------------------------------------------------------------

    def _check_irb_exposure(self, positions: list[Position]) -> list[RiskCheckResult]:
        """IRB-specific exposure limits.

        Strategy spec §4.4: only one position at a time. This is stricter than
        the general risk config ``max_positions`` and provides defense-in-depth
        alongside the Trading Agent FSM.
        """
        checks: list[RiskCheckResult] = []
        limit = self._risk.irb_max_open_positions

        open_count = len(positions)
        if open_count >= limit:
            checks.append(
                RiskCheckResult(
                    name="irb_max_positions",
                    passed=False,
                    detail=f"IRB: {open_count} open position(s) >= limit {limit}",
                )
            )
        else:
            checks.append(
                RiskCheckResult(
                    name="irb_max_positions",
                    passed=True,
                    detail=f"IRB: {open_count} open position(s), limit {limit}",
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
        self._gate.record_trade(symbol, side.value, volume)

        # P4: First trade validation — validate execution quality of the first
        # live trade to catch integration bugs early (R4 priority from funded
        # account survival research).  Expected values were stored during
        # pre_trade_check → gate.evaluate → prepare_first_trade_checks.
        try:
            result = self._gate.validate_first_trade_execution(
                actual_entry_price=fill_price,
                actual_sl_price=stop_loss,
                actual_volume=volume,
                execution_time_ms=0.0,  # Stop orders — no meaningful latency metric
            )
            if result.checks_performed:  # Only log if this was actually the first trade
                if result.passed:
                    log.info("First trade validation PASSED — platform integration healthy")
                else:
                    log.error(
                        "First trade validation FAILED — errors: %s",
                        "; ".join(result.errors),
                    )
        except Exception as exc:
            log.warning("First trade validation error (non-blocking): %s", exc)

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
        # Trade journal logging (v87 P2.4)
        try:
            from novatrade.monitor.trade_journal import log_trade_open

            log_trade_open(
                position_id=position_id,
                symbol=symbol,
                side=side.value,
                volume=volume,
                entry_price=fill_price,
                stop_loss=stop_loss,
            )
        except Exception:  # noqa: S110 — journal is best-effort, must not block trading
            pass

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

        # Trade journal logging (v87 P2.4)
        try:
            from novatrade.monitor.trade_journal import log_trade_close

            log_trade_close(
                position_id=position_id,
                symbol=symbol,
                side=side,
                volume=volume,
                pnl_usd=pnl_usd,
                pnl_pips=pnl_pips,
                exit_reason=exit_reason,
            )
        except Exception:  # noqa: S110 — journal is best-effort, must not block trading
            pass

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

    def record_server_request(self, operation: str) -> None:
        """Record a non-trade server request for FTMO daily counter."""
        self._gate.record_server_request(operation)

    def save_ftmo_state(self) -> None:
        """Persist FTMO compliance state for crash recovery."""
        self._gate.save_ftmo_state()

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
        """Reset daily drawdown tracking (call at start of each trading day).

        Note: does NOT clear a halt.  See kill_switch_policy.md §7.
        """
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

    def get_required_actions(self) -> list[RiskAction]:
        """Return portfolio-level actions the caller should execute.

        Check this after any risk-affecting event (trade close, drawdown
        change, halt trigger).  Actions are based on current engine state.

        Returns [RiskAction.NONE] if no action is needed.
        """
        actions: list[RiskAction] = []

        if self._halted:
            actions.append(RiskAction.HALT_TRADING)
            # At critical total drawdown, recommend flattening
            if (
                self._initial_equity > 0
                and self._total_dd.current_drawdown_pct >= self._risk.max_total_drawdown_pct * 0.8
            ):
                actions.append(RiskAction.FLATTEN_POSITIONS)
                actions.append(RiskAction.CANCEL_PENDING)

        return actions if actions else [RiskAction.NONE]

    # ------------------------------------------------------------------
    # Equity kill switch (Phase 6B Step 6.16)
    # ------------------------------------------------------------------

    _state_lock = threading.Lock()

    def should_kill_switch(self) -> bool:
        """Return True if equity drawdown exceeds FTMO limits.

        Checks both daily and total drawdown against configured limits.
        Uses the internal drawdown tracking state (no external I/O).
        """
        daily_pct = self._daily_dd.current_drawdown_pct
        total_pct = self._total_dd.current_drawdown_pct
        daily_limit = self._risk.max_daily_drawdown_pct
        total_limit = self._risk.max_total_drawdown_pct

        if daily_pct >= daily_limit:
            return True
        return total_pct >= total_limit

    def write_risk_state(
        self,
        state_file: str | Path | None = None,
    ) -> None:
        """Write current drawdown state to JSON for cross-process consumption.

        Defaults to ``STATE/novatrade_risk_state.json`` relative to the
        project root (``~/nova-core``).  Thread-safe via ``_state_lock``.
        """
        if state_file is None:
            state_file = Path(__file__).resolve().parents[2] / "STATE" / "novatrade_risk_state.json"
        else:
            state_file = Path(state_file)

        total_pct = self._total_dd.current_drawdown_pct
        total_limit = self._risk.max_total_drawdown_pct
        breached = total_pct >= total_limit

        payload = {
            "equity_drawdown_pct": round(total_pct, 4),
            "daily_drawdown_pct": round(self._daily_dd.current_drawdown_pct, 4),
            "max_allowed_drawdown_pct": total_limit,
            "max_daily_drawdown_pct": self._risk.max_daily_drawdown_pct,
            "breached": breached,
            "halted": self._halted,
            "halt_reason": self._halt_reason,
            "last_updated": dt.datetime.now(dt.timezone.utc).isoformat(),
        }

        try:
            with self._state_lock:
                state_file.parent.mkdir(parents=True, exist_ok=True)
                tmp = state_file.with_suffix(".tmp")
                tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
                tmp.replace(state_file)
            log.debug("Wrote risk state to %s", state_file)
        except OSError as exc:
            # H8 fix: surface write failures instead of silently losing state
            log.error(
                "CRITICAL: Failed to write risk state to %s: %s — risk state will not be visible to kill switch",
                state_file,
                exc,
            )
            raise

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

    def _restore_halt_state(
        self,
        state_file: str | Path | None = None,
    ) -> bool:
        """Read persisted risk state and restore halt flag if set.

        Returns True if a halted state was restored from disk, False otherwise.
        Tolerates missing or malformed files (logs warning, returns False).
        """
        if state_file is None:
            state_file = Path(__file__).resolve().parents[2] / "STATE" / "novatrade_risk_state.json"
        else:
            state_file = Path(state_file)

        if not state_file.exists():
            return False

        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning(
                "Could not read persisted risk state from %s: %s — starting clean",
                state_file,
                exc,
            )
            return False

        if not isinstance(data, dict):
            log.warning("Persisted risk state is not a dict — starting clean")
            return False

        if data.get("halted"):
            self._halted = True
            self._halt_reason = data.get("halt_reason", "halt restored from prior session")
            log.warning(
                "Restored HALT state from disk: %s (file: %s)",
                self._halt_reason,
                state_file,
            )
            return True

        return False

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


def _default_utc_now() -> dt.datetime:
    """Default clock — returns current UTC datetime."""
    return dt.datetime.now(dt.timezone.utc)
