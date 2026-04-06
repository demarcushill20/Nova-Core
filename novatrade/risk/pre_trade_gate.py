"""Provider-neutral pre-trade risk gate for NovaTrade.

Evaluates an OrderRequest against account state, configuration, and current
positions before any order reaches the broker adapter.  Returns a structured
RiskDecision with per-check detail.

Design principles:
- Fail closed: any check error → deny
- Provider-neutral: no MetaApi imports, no vendor objects
- No side effects beyond returning a decision and logging
- Config-driven thresholds
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

# Module-level reference for testability — patch this, not time.time,
# to avoid poisoning the global time module during tests.
_now = time.time
from typing import TYPE_CHECKING, List, Optional  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

if TYPE_CHECKING:
    from novatrade.monitor.feed_health import FeedHealthSupervisor

from novatrade.config import NovaTradeCfg  # noqa: E402
from novatrade.models import (  # noqa: E402
    AccountMode,
    AccountState,
    HealthState,
    HealthStatus,
    OrderRequest,
    Position,
    RiskCheckResult,
    RiskDecision,
    RiskVerdict,
    SymbolPrice,
)
from novatrade.risk.ftmo_compliance import (  # noqa: E402
    BestDayRuleTracker,
    FtmoDailyLossTracker,
    GapTradingPreventer,
    LotSizeConsistencyChecker,
    NewsCalendarBlocker,
    ServerRequestCounter,
    SlModificationCounter,
    TradingDaysTracker,
    WeekendAutoCloser,
    WeeklyLossTracker,
)
from novatrade.risk.position_sizer import DrawdownScaler, PositionSizer  # noqa: E402
from novatrade.risk.daily_budget_tracker import DailyRiskBudgetTracker  # noqa: E402
from novatrade.risk.profit_cushion_protocol import ProfitCushionProtocol  # noqa: E402

log = logging.getLogger("novatrade.risk.pre_trade_gate")

# Modes that the MVP is allowed to trade in.
_MVP_ALLOWED_MODES = frozenset({AccountMode.DEMO})


def _pip_value_for_symbol(symbol: str) -> float:
    """Return pip value for a symbol (matches hard_risk_supervisor._pip_value).

    Standard forex: 0.0001 (4th decimal).
    JPY pairs: 0.01 (2nd decimal).
    Gold/XAU: 0.1 (1st decimal).
    """
    s = symbol.upper().replace(".", "")
    if "JPY" in s:
        return 0.01
    if "XAU" in s or "GOLD" in s:
        return 0.1
    return 0.0001


class PreTradeGate:
    """Evaluates whether an order should be allowed to reach the adapter.

    Usage::

        gate = PreTradeGate(cfg)
        decision = gate.evaluate(request, account, positions)
        if decision.denied:
            # reject — decision.reason, decision.checks have detail
        else:
            # safe to forward to adapter
    """

    def __init__(
        self,
        cfg: NovaTradeCfg,
        *,
        feed_health: FeedHealthSupervisor | None = None,
    ) -> None:
        self._cfg = cfg
        self._risk = cfg.risk
        self._feed_health = feed_health
        # Rolling trade log: list of (timestamp, symbol, side_value) for
        # cooldown and daily-count tracking.  Kept in-memory — resets on
        # process restart, which is acceptable for MVP.
        self._trade_log: list[tuple[float, str, str]] = []
        # FTMO compliance: lot-size consistency (trailing 20 trades)
        self._lot_checker = LotSizeConsistencyChecker()
        # FTMO compliance: daily server request counter (hard ceiling 1,500)
        self._request_counter = ServerRequestCounter()
        # FTMO compliance: minimum trading days tracker
        self._days_tracker = TradingDaysTracker()
        # FTMO compliance: daily loss tracker (FTMO Max Daily Loss rule)
        self._daily_loss_tracker = FtmoDailyLossTracker(
            daily_loss_pct=self._risk.max_daily_drawdown_pct,
        )
        # FTMO compliance: weekend auto-closer
        self._weekend_closer = WeekendAutoCloser()
        # FTMO compliance: news calendar blocker
        self._news_blocker = NewsCalendarBlocker()
        # FTMO compliance: gap trading preventer
        self._gap_preventer = GapTradingPreventer()
        # FTMO compliance: SL modification counter per trade
        self._sl_mod_counter = SlModificationCounter()
        # FTMO compliance: weekly loss tracker (rolling 7-day cumulative loss)
        self._weekly_loss_tracker = WeeklyLossTracker()
        # FTMO compliance: best day rule tracker (funded account enforcement)
        self._best_day_tracker = BestDayRuleTracker()
        # FTMO compliance: position sizer for volume cross-check
        self._sizer = PositionSizer(
            min_lot=self._risk.min_volume_per_trade,
            max_lot=self._risk.max_volume_per_trade,
            micro_variation_enabled=self._risk.lot_micro_variation_enabled,
            micro_variation_step=self._risk.lot_micro_variation_step,
        )
        # Drawdown-adaptive risk scaling (P0 priority from research)
        self._drawdown_scaler = DrawdownScaler(
            hysteresis=True,
            total_dd_enabled=True,
            loss_recovery_mode="instant",
        )
        # Daily risk budget tracker (P1 priority from funded account survival research)
        self._daily_budget_tracker = DailyRiskBudgetTracker(
            daily_loss_limit_pct=self._risk.max_daily_drawdown_pct,
            max_risk_per_trade_pct=20.0,  # Max 20% of daily budget per trade
            min_risk_per_trade_pct=5.0,   # Min 5% of daily budget per trade
            allocation_strategy="adaptive",
        )
        # Profit cushion protocol (P2 priority from funded account survival research)
        # Auto-loads existing state or requires initialization for new cycles
        try:
            self._profit_cushion = ProfitCushionProtocol()
            log.info("Loaded existing profit cushion state")
        except ValueError:
            # No existing state - will need to be initialized when account equity is known
            self._profit_cushion = None
            log.info("Profit cushion not initialized - will set up on first trade")

        # Scaling plan calendar (P3 priority from funded account survival research)
        # Manages FTMO 4-month cycles, scaling deadlines, and calendar automation
        try:
            from .scaling_plan_calendar import ScalingPlanCalendar
            self._scaling_calendar = ScalingPlanCalendar()
            log.info("Initialized scaling plan calendar")
        except Exception as exc:
            log.error(f"Failed to initialize scaling plan calendar: {exc}")
            self._scaling_calendar = None

        # Attempt to restore persisted state from prior session
        self._lot_checker.load_state()
        self._request_counter.load_state()
        self._days_tracker.load_state()
        self._daily_loss_tracker.load_state()
        self._sl_mod_counter.load_state()
        self._weekly_loss_tracker.load_state()
        self._daily_budget_tracker.load_state()
        # Note: profit cushion state is loaded automatically in __init__

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def evaluate(
        self,
        request: OrderRequest,
        account: AccountState,
        positions: list[Position],
        *,
        health: HealthStatus | None = None,
        price: SymbolPrice | None = None,
    ) -> RiskDecision:
        """Run all pre-trade checks.  Returns a RiskDecision.

        Checks run in priority order.  All checks execute (no short-circuit)
        so the caller sees every failing rule at once.
        """
        checks: list[RiskCheckResult] = []
        now = _now()

        checks.append(self._check_kill_switch())
        checks.append(self._check_dry_run())
        checks.append(self._check_account_mode(account))
        checks.append(self._check_trade_allowed(account))
        checks.append(self._check_health(health))
        checks.append(self._check_feed_health(request.symbol))
        checks.append(self._check_symbol(request))
        checks.append(self._check_volume(request))
        checks.append(self._lot_checker.check(request.volume))
        checks.append(self._request_counter.check())
        checks.append(self._check_stop_loss(request))
        checks.append(self._check_sl_distance(request))
        checks.append(self._check_volume_sizing(request, account))
        checks.append(self._check_max_positions(positions))
        checks.append(self._check_daily_trade_count(now))
        checks.append(self._check_cooldown(request, now))
        checks.append(self._check_duplicate_position(request, positions))
        checks.append(self._check_drawdown(account))
        checks.append(self._daily_loss_tracker.check(account.balance, account.equity))
        checks.append(self._check_daily_budget(request, account))
        checks.append(self._check_spread(price))
        checks.append(self._check_weekend(now))
        checks.append(self._check_news(request.symbol, now))
        checks.append(self._check_gap_spread(price))
        checks.append(self._check_rollover_dead_zone(now))
        checks.append(self._check_london_fix(now))
        checks.append(self._weekly_loss_tracker.check())
        checks.append(self._check_best_day_rule())
        checks.append(self._days_tracker.check())

        failed = [c for c in checks if not c.passed]

        if failed:
            first = failed[0]
            decision = RiskDecision(
                verdict=RiskVerdict.DENY,
                checks=checks,
                reason=first.detail,
                rule=first.name,
                request=request,
            )
            log.warning(
                "pre-trade DENY: %s %s %s vol=%.2f — %d check(s) failed: %s",
                request.side.value,
                request.order_type.value,
                request.symbol,
                request.volume,
                len(failed),
                ", ".join(f.name for f in failed),
            )
            # Trade journal: log rejection (v87 P2.4)
            try:
                from novatrade.monitor.trade_journal import log_trade_reject

                log_trade_reject(
                    symbol=request.symbol,
                    side=request.side.value,
                    reason=first.detail,
                    gate=first.name,
                )
            except Exception:  # noqa: S110 — journal is best-effort, must not block trading
                pass
        else:
            decision = RiskDecision(
                verdict=RiskVerdict.ALLOW,
                checks=checks,
                request=request,
            )
            log.info(
                "pre-trade ALLOW: %s %s %s vol=%.2f — %d checks passed",
                request.side.value,
                request.order_type.value,
                request.symbol,
                request.volume,
                len(checks),
            )

        return decision

    def initialize_daily_loss(self, balance: float, equity: float) -> None:
        """Initialize the FTMO daily loss tracker and daily budget tracker with account state.

        Call once at session start after fetching account balance from broker.
        """
        self._daily_loss_tracker.initialize(balance, equity)
        self._weekly_loss_tracker.initialize(balance)
        self._daily_budget_tracker.initialize(equity)

    def record_closed_trade_pnl(self, pnl: float, date_str: str | None = None) -> None:
        """Record a closed trade's P&L for weekly loss and best day tracking.

        Call after each trade closure to keep rolling trackers up to date.
        """
        self._weekly_loss_tracker.record_daily_pnl(pnl, date_str)
        if date_str is None:
            from zoneinfo import ZoneInfo

            _tz = ZoneInfo("Europe/Prague")
            date_str = datetime.now(timezone.utc).astimezone(_tz).strftime("%Y-%m-%d")
        self._best_day_tracker.record_daily_pnl(date_str, pnl)

    def record_trade(self, symbol: str, side_value: str, volume: float = 0.0) -> None:
        """Record a trade for cooldown, daily-count, and FTMO tracking.

        Call this after a successful order placement so subsequent
        evaluate() calls see it.
        """
        self._trade_log.append((_now(), symbol, side_value))
        if volume > 0:
            self._lot_checker.record(volume, symbol)
        self._request_counter.record("order_open")
        self._days_tracker.record_trade_day()

    def record_trade_with_budget(
        self,
        symbol: str,
        side_value: str,
        volume: float,
        entry_price: float,
        stop_loss: float,
        trade_id: str = "",
    ) -> None:
        """Record a trade with full details for budget tracking.

        This should be called after successful order placement to record
        the trade against the daily budget allocation.
        """
        # Record in standard tracking
        self.record_trade(symbol, side_value, volume)

        # Record budget allocation
        self._daily_budget_tracker.allocate_trade_risk(
            symbol=symbol,
            entry_price=entry_price,
            stop_loss=stop_loss,
            volume=volume,
            trade_id=trade_id,
        )

    def record_server_request(self, operation: str) -> None:
        """Record a non-trade server request (modify SL/TP, close, etc.)."""
        self._request_counter.record(operation)

    def record_sl_modification(self, position_id: str) -> None:
        """Record a stop-loss modification for FTMO EA compliance tracking."""
        self._sl_mod_counter.record(position_id)

    def check_sl_mod_limit(self, position_id: str = "") -> RiskCheckResult:
        """Check if SL modification limits allow further modifications."""
        return self._sl_mod_counter.check(position_id)

    def save_ftmo_state(self) -> None:
        """Persist FTMO compliance state and daily budget for crash recovery."""
        self._lot_checker.save_state()
        self._request_counter.save_state()
        self._days_tracker.save_state()
        self._daily_loss_tracker.save_state()
        self._sl_mod_counter.save_state()
        self._weekly_loss_tracker.save_state()
        self._daily_budget_tracker.save_state()

    def _day_start_prague_tz(self, ts: float) -> float:
        """Return midnight Prague timezone timestamp for the day containing *ts*.

        Uses FTMO's daily reset timezone (Europe/Prague) for consistent daily boundaries.
        This correctly handles CET↔CEST DST transitions, unlike UTC-based calculations.
        """
        prague_tz = ZoneInfo(self._cfg.risk.daily_reset_tz)
        dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(prague_tz)
        # Get date in Prague timezone, then convert back to midnight Prague time as UTC timestamp
        midnight_prague = datetime(dt.year, dt.month, dt.day, tzinfo=prague_tz)
        return midnight_prague.timestamp()

    # ------------------------------------------------------------------
    # Individual checks
    # ------------------------------------------------------------------

    def _check_kill_switch(self) -> RiskCheckResult:
        """Check NovaCore kill switch — deny if system is not in run mode."""
        try:
            from nova_kill_switch import check_kill_switch

            mode = check_kill_switch()
        except Exception:
            # If kill switch module is unavailable, pass (fail-open here is
            # acceptable because the kill switch is an external system guard,
            # and all other checks still apply).
            return RiskCheckResult(name="kill_switch", passed=True, detail="kill switch module unavailable — skipped")

        if mode == "run":
            return RiskCheckResult(name="kill_switch", passed=True, detail=f"mode={mode}")
        return RiskCheckResult(
            name="kill_switch",
            passed=False,
            detail=f"NovaCore kill switch active: mode={mode}",
        )

    def _check_dry_run(self) -> RiskCheckResult:
        """Deny real execution when dry_run is enabled."""
        if self._cfg.dry_run:
            return RiskCheckResult(
                name="dry_run",
                passed=False,
                detail="dry_run is enabled — no real orders allowed",
            )
        return RiskCheckResult(name="dry_run", passed=True, detail="dry_run=False")

    def _check_account_mode(self, account: AccountState) -> RiskCheckResult:
        """Only allow trading in MVP-permitted account modes."""
        if account.mode in _MVP_ALLOWED_MODES:
            return RiskCheckResult(
                name="account_mode",
                passed=True,
                detail=f"mode={account.mode.value}",
            )
        return RiskCheckResult(
            name="account_mode",
            passed=False,
            detail=f"account mode {account.mode.value} not allowed in MVP "
            f"(allowed: {', '.join(m.value for m in _MVP_ALLOWED_MODES)})",
        )

    def _check_trade_allowed(self, account: AccountState) -> RiskCheckResult:
        """Deny if the broker reports trading is disabled on this account.

        This catches cases where the prop firm or broker has suspended trading
        permissions (e.g. account under review, tradeAllowed=false).
        """
        if not account.trade_allowed:
            return RiskCheckResult(
                name="trade_allowed",
                passed=False,
                detail="broker reports tradeAllowed=false — trading disabled on this account",
            )
        return RiskCheckResult(
            name="trade_allowed",
            passed=True,
            detail="tradeAllowed=true",
        )

    def _check_health(self, health: HealthStatus | None) -> RiskCheckResult:
        """Deny if adapter health is DOWN."""
        if health is None:
            return RiskCheckResult(
                name="health",
                passed=True,
                detail="no health data — skipped",
            )
        if health.state == HealthState.DOWN:
            return RiskCheckResult(
                name="health",
                passed=False,
                detail=f"adapter health is DOWN: {health.message}",
            )
        return RiskCheckResult(
            name="health",
            passed=True,
            detail=f"state={health.state.value}",
        )

    def _check_feed_health(self, symbol: str) -> RiskCheckResult:
        """Deny if the data feed for this symbol is unhealthy.

        Uses FeedHealthSupervisor.is_tradeable() which checks staleness,
        clock drift, spread widening, disconnection, and market closure.
        Fail-open if no feed supervisor is configured (preserves backward
        compat for dry-run / test scenarios).
        """
        if self._feed_health is None:
            return RiskCheckResult(
                name="feed_health",
                passed=True,
                detail="no feed health supervisor — skipped",
            )
        try:
            if self._feed_health.is_tradeable(symbol):
                snap = self._feed_health.get_snapshot(symbol)
                return RiskCheckResult(
                    name="feed_health",
                    passed=True,
                    detail=(
                        f"state={snap.state.value} age={snap.last_tick_age:.1f}s"
                        f" spread={snap.current_spread_pips:.1f}pips"
                    ),
                )
            snap = self._feed_health.get_snapshot(symbol)
            return RiskCheckResult(
                name="feed_health",
                passed=False,
                detail=(
                    f"feed unhealthy: state={snap.state.value}"
                    f" age={snap.last_tick_age:.1f}s"
                    f" spread={snap.current_spread_pips:.1f}pips"
                ),
            )
        except Exception as exc:
            # Fail-closed on errors
            return RiskCheckResult(
                name="feed_health",
                passed=False,
                detail=f"feed health check error: {exc}",
            )

    def _check_sl_distance(self, request: OrderRequest) -> RiskCheckResult:
        """Validate stop-loss is on the correct side and meets minimum distance.

        - BUY orders: SL must be below entry price
        - SELL orders: SL must be above entry price
        - Distance must be >= RiskConfig.min_sl_distance_pips (prevents micro-stops)
        """
        if request.price is None or request.stop_loss is None:
            return RiskCheckResult(
                name="sl_distance",
                passed=True,
                detail="price or stop_loss not set — skipped",
            )

        from novatrade.models import OrderSide

        sl_distance = abs(request.price - request.stop_loss)
        # Symbol-aware pip conversion (matches HardRiskSupervisor logic)
        pip_val = _pip_value_for_symbol(request.symbol)
        sl_pips = sl_distance / pip_val

        # Check SL direction
        if request.side == OrderSide.BUY and request.stop_loss >= request.price:
            return RiskCheckResult(
                name="sl_distance",
                passed=False,
                detail=f"BUY order SL ({request.stop_loss}) must be below entry ({request.price})",
            )
        if request.side == OrderSide.SELL and request.stop_loss <= request.price:
            return RiskCheckResult(
                name="sl_distance",
                passed=False,
                detail=f"SELL order SL ({request.stop_loss}) must be above entry ({request.price})",
            )

        # Check minimum distance — configurable via RiskConfig.min_sl_distance_pips
        min_sl_pips = self._risk.min_sl_distance_pips
        if sl_pips < min_sl_pips:
            return RiskCheckResult(
                name="sl_distance",
                passed=False,
                detail=f"SL distance {sl_pips:.1f} pips < minimum {min_sl_pips} pips",
            )

        return RiskCheckResult(
            name="sl_distance",
            passed=True,
            detail=f"sl_pips={sl_pips:.1f}, side={request.side.value}",
        )

    def _check_symbol(self, request: OrderRequest) -> RiskCheckResult:
        """Check symbol is in the configured allowlist (including FTMO-resolved names)."""
        allowed = self._cfg.symbols
        if request.symbol in allowed:
            return RiskCheckResult(
                name="symbol_allowed",
                passed=True,
                detail=f"{request.symbol} in allowlist",
            )
        # Also accept FTMO-resolved broker symbols
        if self._cfg.ftmo.enabled:
            resolved = {self._cfg.ftmo.resolve_symbol(s) for s in allowed}
            if request.symbol in resolved:
                return RiskCheckResult(
                    name="symbol_allowed",
                    passed=True,
                    detail=f"{request.symbol} in allowlist (via FTMO mapping)",
                )
        return RiskCheckResult(
            name="symbol_allowed",
            passed=False,
            detail=f"{request.symbol} not in allowed symbols: {allowed}",
        )

    def _check_volume(self, request: OrderRequest) -> RiskCheckResult:
        """Check volume is within configured bounds."""
        min_vol = self._risk.min_volume_per_trade
        max_vol = self._risk.max_volume_per_trade
        if request.volume < min_vol:
            return RiskCheckResult(
                name="volume_bounds",
                passed=False,
                detail=f"volume {request.volume} below minimum {min_vol}",
            )
        if request.volume > max_vol:
            return RiskCheckResult(
                name="volume_bounds",
                passed=False,
                detail=f"volume {request.volume} exceeds maximum {max_vol}",
            )
        return RiskCheckResult(
            name="volume_bounds",
            passed=True,
            detail=f"volume={request.volume} within [{min_vol}, {max_vol}]",
        )

    def _check_volume_sizing(self, request: OrderRequest, account: AccountState) -> RiskCheckResult:
        """Cross-check requested volume against drawdown-adaptive risk model.

        Uses 1% equity base risk with dynamic scaling based on drawdown proximity.
        As daily/total drawdown approaches FTMO limits, position sizes are automatically
        reduced to preserve the account (P0 priority from funded account research).

        Requires both entry price and stop-loss to be set.  If either is
        missing, the check passes (informational only) because not all
        order flows provide both values at gate time.
        """
        if request.price is None or request.stop_loss is None:
            return RiskCheckResult(
                name="volume_sizing",
                passed=True,
                detail="price or stop_loss not set — sizing cross-check skipped",
            )

        if account.equity <= 0:
            return RiskCheckResult(
                name="volume_sizing",
                passed=True,
                detail="equity <= 0 — sizing cross-check skipped",
            )

        try:
            # Calculate base volume using standard 1% equity risk
            base_calculated = self._sizer.calculate(
                equity=account.equity,
                entry=request.price,
                stop=request.stop_loss,
            )

            # Apply drawdown-adaptive scaling (P0 critical priority)
            # Estimate daily drawdown from account balance vs configured limits
            # This is a simplified approach - full implementation would get actual DD state
            daily_dd_limit_pct = self._risk.max_daily_drawdown_pct  # e.g., 5.0
            total_dd_limit_pct = self._risk.max_total_drawdown_pct  # e.g., 10.0

            # Estimate drawdown usage as percentage of limits
            # For FTMO $100K account, assume $100K starting balance
            estimated_initial_equity = 100000.0  # FTMO standard
            if hasattr(account, "initial_equity") and account.initial_equity > 0:
                estimated_initial_equity = account.initial_equity

            # Estimate daily start equity (simplified - same as current for now)
            estimated_daily_start = estimated_initial_equity
            if hasattr(account, "daily_start_equity") and account.daily_start_equity > 0:
                estimated_daily_start = account.daily_start_equity

            # Calculate drawdown usage percentages
            daily_dd_used = max(0.0, (estimated_daily_start - account.equity) / estimated_daily_start * 100)
            total_dd_used = max(0.0, (estimated_initial_equity - account.equity) / estimated_initial_equity * 100)

            daily_dd_usage_pct = min(1.0, daily_dd_used / daily_dd_limit_pct) if daily_dd_limit_pct > 0 else 0.0
            total_dd_usage_pct = min(1.0, total_dd_used / total_dd_limit_pct) if total_dd_limit_pct > 0 else 0.0

            # Apply drawdown scaling to reduce position size as we approach limits
            drawdown_scaled = self._drawdown_scaler.scale_volume(
                base_volume=base_calculated,
                dd_used_pct=daily_dd_usage_pct,
                total_dd_used_pct=total_dd_usage_pct,
                min_lot=self._risk.min_volume_per_trade,
            )

            # Initialize profit cushion on first trade if needed
            if self._profit_cushion is None:
                from datetime import datetime
                from zoneinfo import ZoneInfo

                # Initialize new FTMO cycle starting today
                cycle_start = datetime.now(ZoneInfo("Europe/Prague")).replace(hour=0, minute=0, second=0, microsecond=0)
                try:
                    self._profit_cushion = ProfitCushionProtocol(
                        starting_equity=account.equity,
                        cycle_start_date=cycle_start,
                    )
                    log.info(f"Initialized new profit cushion cycle: equity=${account.equity:,.2f}")

                    # Initialize scaling calendar for the same cycle
                    if self._scaling_calendar and not self._scaling_calendar.get_current_cycle():
                        self._scaling_calendar.initialize_cycle(
                            starting_equity=account.equity,
                            cycle_start_date=cycle_start
                        )
                        log.info(f"Initialized scaling plan calendar for cycle starting ${account.equity:,.2f}")

                except Exception as exc:
                    log.warning(f"Failed to initialize profit cushion: {exc}")
                    # Fallback to no profit cushion scaling
                    calculated = drawdown_scaled

            # Apply profit cushion protocol scaling
            if self._profit_cushion is not None:
                try:
                    # Update current equity (may change tier)
                    self._profit_cushion.update_equity(account.equity)

                    # Get profit cushion risk multiplier
                    profit_multiplier = self._profit_cushion.get_risk_multiplier()
                    profit_tier = self._profit_cushion.get_tier()

                    # Apply profit cushion scaling
                    calculated = max(
                        self._risk.min_volume_per_trade,
                        drawdown_scaled * profit_multiplier
                    )

                    # Update scaling calendar with current equity
                    if self._scaling_calendar:
                        try:
                            self._scaling_calendar.update_equity(account.equity)
                        except Exception as scaling_exc:
                            log.warning(f"Scaling calendar update failed: {scaling_exc}")

                    log.debug(
                        "Profit cushion scaling: tier=%s, multiplier=%.2fx, pre=%.2f, post=%.2f",
                        profit_tier,
                        profit_multiplier,
                        drawdown_scaled,
                        calculated
                    )
                except Exception as exc:
                    log.warning(f"Profit cushion scaling failed: {exc}, using drawdown-only scaling")
                    calculated = drawdown_scaled
            else:
                calculated = drawdown_scaled

            log.debug(
                "Multi-tier risk scaling: base=%.2f, drawdown=%.2f, final=%.2f, daily_dd=%.1f%%, total_dd=%.1f%%",
                base_calculated,
                drawdown_scaled,
                calculated,
                daily_dd_usage_pct * 100,
                total_dd_usage_pct * 100,
            )

        except ValueError as exc:
            return RiskCheckResult(
                name="volume_sizing",
                passed=True,
                detail=f"sizing calculation failed: {exc} — skipped",
            )

        ok, reason = self._sizer.validate(
            requested=request.volume,
            calculated=calculated,
            tolerance=0.25,
        )

        return RiskCheckResult(
            name="volume_sizing",
            passed=ok,
            detail=reason,
        )

    def _check_stop_loss(self, request: OrderRequest) -> RiskCheckResult:
        """Require a stop-loss if configured."""
        if not self._risk.require_stop_loss:
            return RiskCheckResult(
                name="stop_loss",
                passed=True,
                detail="stop-loss not required by config",
            )
        if request.stop_loss is not None:
            return RiskCheckResult(
                name="stop_loss",
                passed=True,
                detail=f"stop_loss={request.stop_loss}",
            )
        return RiskCheckResult(
            name="stop_loss",
            passed=False,
            detail="stop-loss is required but not set on order",
        )

    def _check_max_positions(self, positions: list[Position]) -> RiskCheckResult:
        """Deny if already at position limit."""
        count = len(positions)
        limit = self._risk.max_positions
        if count >= limit:
            return RiskCheckResult(
                name="max_positions",
                passed=False,
                detail=f"open positions ({count}) >= limit ({limit})",
            )
        return RiskCheckResult(
            name="max_positions",
            passed=True,
            detail=f"open={count}, limit={limit}",
        )

    def _check_daily_trade_count(self, now: float) -> RiskCheckResult:
        """Deny if daily trade count has been reached."""
        limit = self._risk.max_trades_per_day
        day_start = self._day_start_prague_tz(now)
        today_count = sum(1 for ts, _, _ in self._trade_log if ts >= day_start)
        if today_count >= limit:
            return RiskCheckResult(
                name="daily_trade_count",
                passed=False,
                detail=f"trades today ({today_count}) >= limit ({limit})",
            )
        return RiskCheckResult(
            name="daily_trade_count",
            passed=True,
            detail=f"trades_today={today_count}, limit={limit}",
        )

    def _check_cooldown(self, request: OrderRequest, now: float) -> RiskCheckResult:
        """Deny if a trade on the same symbol+side was placed too recently."""
        window = self._risk.cooldown_seconds
        if window <= 0:
            return RiskCheckResult(name="cooldown", passed=True, detail="cooldown disabled")

        cutoff = now - window
        for ts, sym, side in reversed(self._trade_log):
            if ts < cutoff:
                break
            if sym == request.symbol and side == request.side.value:
                ago = int(now - ts)
                return RiskCheckResult(
                    name="cooldown",
                    passed=False,
                    detail=f"duplicate {request.side.value} {request.symbol} {ago}s ago — cooldown {window}s",
                )
        return RiskCheckResult(name="cooldown", passed=True, detail=f"window={window}s clear")

    def _check_duplicate_position(
        self,
        request: OrderRequest,
        positions: list[Position],
    ) -> RiskCheckResult:
        """Warn-deny if an open position on same symbol+side already exists."""
        for p in positions:
            if p.symbol == request.symbol and p.side == request.side:
                return RiskCheckResult(
                    name="duplicate_position",
                    passed=False,
                    detail=f"existing {p.side.value} position on {p.symbol} (id={p.position_id}, vol={p.volume})",
                )
        return RiskCheckResult(name="duplicate_position", passed=True, detail="no conflict")

    def _check_drawdown(self, account: AccountState) -> RiskCheckResult:
        """Deny if equity drawdown from balance exceeds threshold."""
        if account.balance <= 0:
            return RiskCheckResult(
                name="drawdown",
                passed=True,
                detail="balance=0 — skipped",
            )
        drawdown_pct = ((account.balance - account.equity) / account.balance) * 100
        limit = self._risk.max_drawdown_equity_pct
        if drawdown_pct >= limit:
            return RiskCheckResult(
                name="drawdown",
                passed=False,
                detail=f"equity drawdown {drawdown_pct:.2f}% >= limit {limit:.1f}%",
            )
        return RiskCheckResult(
            name="drawdown",
            passed=True,
            detail=f"drawdown={drawdown_pct:.2f}%, limit={limit:.1f}%",
        )

    def _check_daily_budget(self, request: OrderRequest, account: AccountState) -> RiskCheckResult:
        """Check if sufficient daily risk budget is available for this trade.

        Uses the Daily Risk Budget Tracker (P1 priority from funded account survival research)
        to ensure proper risk distribution throughout the trading day.
        """
        if request.price is None or request.stop_loss is None:
            return RiskCheckResult(
                name="daily_budget",
                passed=True,
                detail="price or stop_loss not set — budget check skipped",
            )

        # Calculate required risk for this trade
        stop_distance = abs(request.price - request.stop_loss)
        pip_value = _pip_value_for_symbol(request.symbol)
        stop_pips = stop_distance / pip_value
        required_risk_usd = request.volume * stop_pips * 10.0  # $10/pip for standard lot

        # Check budget availability
        return self._daily_budget_tracker.check_budget_availability(required_risk_usd)

    def _check_spread(self, price: SymbolPrice | None) -> RiskCheckResult:
        """Deny if current spread exceeds ceiling."""
        if price is None:
            return RiskCheckResult(
                name="spread",
                passed=True,
                detail="no price data — skipped",
            )
        ceiling = self._risk.spread_ceiling_points
        # Spread is in price units; convert to points (assume 5-digit pricing).
        spread_points = price.spread * 100_000
        if spread_points > ceiling:
            return RiskCheckResult(
                name="spread",
                passed=False,
                detail=f"spread {spread_points:.1f} points > ceiling {ceiling:.1f}",
            )
        return RiskCheckResult(
            name="spread",
            passed=True,
            detail=f"spread={spread_points:.1f}pts, ceiling={ceiling:.1f}",
        )

    def _check_weekend(self, now: float) -> RiskCheckResult:
        """Deny if within the weekend auto-close window."""
        if self._weekend_closer.should_close_positions(now):
            return RiskCheckResult(
                name="weekend_close",
                passed=False,
                detail="Weekend auto-close window",
            )
        return RiskCheckResult(
            name="weekend_close",
            passed=True,
            detail="not in weekend close window",
        )

    def _check_news(self, symbol: str, now: float) -> RiskCheckResult:
        """Deny if a high-impact news event is within the blackout window."""
        blocked, reason = self._news_blocker.is_blocked(now, symbol, blackout_minutes=self._risk.news_blackout_minutes)
        if blocked:
            return RiskCheckResult(
                name="news_blackout",
                passed=False,
                detail=reason,
            )
        return RiskCheckResult(
            name="news_blackout",
            passed=True,
            detail="no news blackout active",
        )

    def _check_rollover_dead_zone(self, now: float) -> RiskCheckResult:
        """Deny trading during the daily FX rollover window.

        Between 21:00-23:00 UTC, spreads widen significantly, liquidity drops,
        and fill quality degrades.  Blocking new entries during this window
        avoids adverse fills and also makes the bot's trading pattern less
        detectable as an EA (no trades during the most volatile overnight
        transition period).
        """
        if not self._risk.rollover_dead_zone_enabled:
            return RiskCheckResult(
                name="rollover_dead_zone",
                passed=True,
                detail="rollover dead zone disabled",
            )

        utc_dt = datetime.fromtimestamp(now, tz=timezone.utc)
        hour = utc_dt.hour

        start = self._risk.rollover_start_hour_utc
        end = self._risk.rollover_end_hour_utc

        if start <= end:
            in_zone = start <= hour < end
        else:
            # Wraps midnight (e.g. 23:00-01:00)
            in_zone = hour >= start or hour < end

        if in_zone:
            return RiskCheckResult(
                name="rollover_dead_zone",
                passed=False,
                detail=(
                    f"in rollover dead zone ({start:02d}:00-{end:02d}:00 UTC), "
                    f"current={hour:02d}:{utc_dt.minute:02d} UTC"
                ),
            )
        return RiskCheckResult(
            name="rollover_dead_zone",
            passed=True,
            detail=(
                f"outside rollover window ({start:02d}:00-{end:02d}:00 UTC), current={hour:02d}:{utc_dt.minute:02d} UTC"
            ),
        )

    def _check_london_fix(self, now: float) -> RiskCheckResult:
        """Deny trading during the London 4PM Fix window.

        The London Fix (15:45-16:15 UTC) is when benchmark FX rates are set.
        Spreads widen, volatility spikes, and FTMO may flag trading during
        this period as suspicious EA behavior targeting fix-related price
        movements.
        """
        if not self._risk.london_fix_avoidance_enabled:
            return RiskCheckResult(
                name="london_fix",
                passed=True,
                detail="London Fix avoidance disabled",
            )

        utc_dt = datetime.fromtimestamp(now, tz=timezone.utc)
        current_minutes = utc_dt.hour * 60 + utc_dt.minute
        start_minutes = self._risk.london_fix_start_hour_utc * 60 + self._risk.london_fix_start_minute_utc
        end_minutes = self._risk.london_fix_end_hour_utc * 60 + self._risk.london_fix_end_minute_utc

        if start_minutes <= current_minutes < end_minutes:
            return RiskCheckResult(
                name="london_fix",
                passed=False,
                detail=(
                    f"in London Fix window "
                    f"({self._risk.london_fix_start_hour_utc:02d}:{self._risk.london_fix_start_minute_utc:02d}"
                    f"-{self._risk.london_fix_end_hour_utc:02d}:{self._risk.london_fix_end_minute_utc:02d} UTC), "
                    f"current={utc_dt.hour:02d}:{utc_dt.minute:02d} UTC"
                ),
            )
        return RiskCheckResult(
            name="london_fix",
            passed=True,
            detail=(
                f"outside London Fix window "
                f"({self._risk.london_fix_start_hour_utc:02d}:{self._risk.london_fix_start_minute_utc:02d}"
                f"-{self._risk.london_fix_end_hour_utc:02d}:{self._risk.london_fix_end_minute_utc:02d} UTC), "
                f"current={utc_dt.hour:02d}:{utc_dt.minute:02d} UTC"
            ),
        )

    def _check_best_day_rule(self) -> RiskCheckResult:
        """Deny if today's profit exceeds 50% of total cumulative profit.

        FTMO Best Day Rule (funded accounts): no single day's profit may
        account for more than 50% of total lifetime profit.  This prevents
        concentration risk and ensures consistent performance.
        """
        ok, reason = self._best_day_tracker.check(max_pct=0.50)
        return RiskCheckResult(
            name="best_day_rule",
            passed=ok,
            detail=reason,
        )

    def _check_gap_spread(self, price: SymbolPrice | None) -> RiskCheckResult:
        """Deny if spread is abnormally wide relative to session average.

        Uses the FeedHealthSupervisor's average spread when available.
        Falls back to passing if no feed data is available.
        """
        if price is None or self._feed_health is None:
            return RiskCheckResult(
                name="gap_spread",
                passed=True,
                detail="no price or feed data — skipped",
            )
        try:
            snap = self._feed_health.get_snapshot(price.symbol)
            avg_spread = snap.avg_spread_pips
            current_spread = snap.current_spread_pips
            if avg_spread <= 0:
                return RiskCheckResult(
                    name="gap_spread",
                    passed=True,
                    detail="no avg spread data — skipped",
                )
            allowed, reason = self._gap_preventer.check(current_spread, avg_spread)
            return RiskCheckResult(
                name="gap_spread",
                passed=allowed,
                detail=reason,
            )
        except Exception as exc:
            return RiskCheckResult(
                name="gap_spread",
                passed=False,
                detail=f"gap spread check error — blocked for safety: {exc}",
            )

    # ---------------------------------------------------------------------------
    # Drawdown-adaptive risk management (P0 priority)
    # ---------------------------------------------------------------------------

    def record_trade_loss(self) -> None:
        """Record a losing trade for consecutive loss tracking.

        This updates the internal state of the DrawdownScaler to apply
        additional position size reduction based on loss streak length.
        """
        self._drawdown_scaler.record_loss()
        log.debug("Recorded trade loss — consecutive losses: %d", self._drawdown_scaler.consecutive_losses)

    def record_trade_win(self) -> None:
        """Record a winning trade for consecutive loss tracking.

        Resets or reduces the consecutive loss count depending on the
        configured recovery mode (instant or gradual).
        """
        self._drawdown_scaler.record_win()
        log.debug("Recorded trade win — consecutive losses: %d", self._drawdown_scaler.consecutive_losses)

    def reset_daily_risk_scaling(self) -> None:
        """Reset drawdown scaling state for a new trading day.

        Should be called at the start of each trading day to reset
        the daily drawdown tracking and loss streak state.
        """
        self._drawdown_scaler.reset()
        log.info("Reset daily drawdown scaling state")

    @property
    def consecutive_losses(self) -> int:
        """Current consecutive loss count for monitoring."""
        return self._drawdown_scaler.consecutive_losses

    @property
    def current_drawdown_scale(self) -> float:
        """Current drawdown scaling factor (1.0 = full size, 0.25 = survival mode)."""
        return self._drawdown_scaler.current_dd_scale

    @property
    def daily_budget_status(self) -> dict:
        """Current daily budget tracker status for monitoring."""
        return self._daily_budget_tracker.get_status_summary()

    @property
    def remaining_daily_budget(self) -> float:
        """Remaining risk budget for today in USD."""
        return self._daily_budget_tracker.remaining_budget_usd

    @property
    def profit_cushion_status(self) -> dict:
        """Current profit cushion protocol status for monitoring."""
        if self._profit_cushion is None:
            return {"error": "Profit cushion not initialized"}
        return self._profit_cushion.get_cycle_status()

    @property
    def profit_cushion_multiplier(self) -> float:
        """Current profit cushion risk multiplier (1.0 = full size, 0.1 = scaling lock mode)."""
        if self._profit_cushion is None:
            return 1.0
        return self._profit_cushion.get_risk_multiplier()

    @property
    def profit_cushion_tier(self) -> str:
        """Current profit cushion tier (normal/conservative/selective/turtle/scaling_lock)."""
        if self._profit_cushion is None:
            return "normal"
        return self._profit_cushion.get_tier()

    @property
    def scaling_calendar_status(self) -> dict:
        """Current scaling plan calendar status for monitoring."""
        if self._scaling_calendar is None:
            return {"error": "Scaling calendar not initialized"}
        return self._scaling_calendar.get_cycle_status()

    @property
    def is_scaling_eligible(self) -> bool:
        """Whether account is eligible for FTMO scaling (10% profit reached)."""
        if self._scaling_calendar is None:
            return False
        current_cycle = self._scaling_calendar.get_current_cycle()
        return current_cycle.is_scaling_eligible if current_cycle else False

    @property
    def scaling_deadline(self) -> Optional[str]:
        """Next scaling submission deadline (ISO format)."""
        if self._scaling_calendar is None:
            return None
        current_cycle = self._scaling_calendar.get_current_cycle()
        return current_cycle.scaling_deadline.isoformat() if current_cycle else None

    @property
    def upcoming_scaling_events(self) -> List[dict]:
        """Upcoming scaling calendar events."""
        if self._scaling_calendar is None:
            return []
        events = self._scaling_calendar.get_upcoming_events(days_ahead=30)
        return [
            {
                "event_id": e.event_id,
                "type": e.event_type,
                "title": e.title,
                "due_date": e.due_date.isoformat(),
                "status": e.status
            }
            for e in events
        ]

    def start_new_profit_cycle(self, starting_equity: float, cycle_start_date=None) -> None:
        """Start a new FTMO profit cycle.

        Args:
            starting_equity: Starting equity for new cycle
            cycle_start_date: Start date (defaults to now)
        """
        if cycle_start_date is None:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            cycle_start_date = datetime.now(ZoneInfo("Europe/Prague")).replace(hour=0, minute=0, second=0, microsecond=0)

        try:
            self._profit_cushion = ProfitCushionProtocol(
                starting_equity=starting_equity,
                cycle_start_date=cycle_start_date,
            )
            log.info(f"Started new profit cushion cycle: equity=${starting_equity:,.2f}")

            # Also initialize scaling calendar for the same cycle
            if self._scaling_calendar:
                self._scaling_calendar.initialize_cycle(
                    starting_equity=starting_equity,
                    cycle_start_date=cycle_start_date
                )
                log.info(f"Started new scaling plan calendar: equity=${starting_equity:,.2f}")

        except Exception as exc:
            log.error(f"Failed to start new profit cycle: {exc}")
            raise


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
