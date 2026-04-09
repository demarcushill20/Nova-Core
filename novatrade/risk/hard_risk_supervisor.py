"""Hard Risk Supervisor — deterministic, non-AI safety layer for NovaTrade.

This module sits ABOVE every other risk component.  It is the supreme
authority on whether a trade can proceed.  No AI, no ML, no LLM — just
deterministic rules with immutable limits.

Design principles:
  1. **Deterministic** — pure arithmetic, no probabilistic models.
  2. **Immutable at runtime** — limits are frozen after construction.
  3. **Fail-closed** — any error in evaluation → DENY.
  4. **Independent state** — tracks equity/P&L independently of RiskEngine.
  5. **Override authority** — can veto any order the RiskEngine approves,
     force-close positions, and activate the system kill switch.
  6. **Non-bypassable** — no config flag, env var, or API call can disable it.

Daily reset uses Europe/Prague (FTMO's timezone) via ZoneInfo for DST safety.

Integration:
  - Pre-trade:  ``supervisor.veto(request, account, positions)``
  - Post-trade: ``supervisor.on_trade_closed(pnl_usd)``
  - Continuous:  ``supervisor.check_account(account, positions)``
  - Emergency:  ``supervisor.emergency_halt(reason)``
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
from zoneinfo import ZoneInfo

_FTMO_TZ = ZoneInfo("Europe/Prague")

log = logging.getLogger("novatrade.risk.hard_supervisor")


# ---------------------------------------------------------------------------
# Hard limits — frozen after construction
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HardLimits:
    """Immutable hard limits.  Cannot be changed at runtime.

    These are absolute ceilings — the supervisor will never allow a trade
    or account state that violates any of them.  All dollar values are in
    the account currency (USD by default).

    The defaults are suitable for a $100,000 FTMO challenge account.
    Override via constructor for different account sizes.
    """

    # --- Equity protection ---
    equity_floor_usd: float = 8_500.0  # Below this: close all + halt
    max_daily_loss_usd: float = 4_500.0  # Absolute daily loss cap ($)
    max_total_loss_usd: float = 9_000.0  # Absolute total loss cap ($)
    max_loss_per_trade_usd: float = 1_500.0  # Max risk on a single trade ($)

    # --- Position limits ---
    max_lot_size: float = 50.0  # Max lots per order (absolute, supports $100K account)
    max_concurrent_positions: int = 1  # FTMO-safe: single position for IRB
    max_trades_per_day: int = 10  # FTMO-safe: aligned with RiskConfig

    # --- Rapid-loss circuit breaker ---
    consecutive_loss_limit: int = 5  # After N consecutive losses → cooldown
    consecutive_loss_cooldown_s: int = 3600  # 1 hour cooldown after streak
    rapid_loss_count: int = 3  # N losses in rapid_loss_window_s → halt
    rapid_loss_window_s: int = 300  # 5 minute window for rapid loss detection

    # --- Fat finger protection ---
    max_volume_ratio: float = 5.0  # Reject if volume > ratio × median recent
    min_stop_distance_pips: float = 1.0  # SL must be at least this far from entry (absolute safety net)

    # --- Currency concentration ---
    max_same_symbol_positions: int = 1  # Max positions on same symbol


class SupervisorVerdict(Enum):
    """Outcome of a supervisor evaluation."""

    ALLOW = "ALLOW"
    VETO = "VETO"  # Reject this order
    EMERGENCY_HALT = "EMERGENCY_HALT"  # Reject + halt everything


@dataclass
class SupervisorDecision:
    """Result of a supervisor pre-trade veto check."""

    verdict: SupervisorVerdict
    rule: str = ""  # Which rule triggered the veto
    detail: str = ""  # Human-readable explanation
    adjusted_volume: float | None = None  # If set, caller must use this volume instead
    timestamp: float = field(default_factory=time.time)

    @property
    def vetoed(self) -> bool:
        return self.verdict != SupervisorVerdict.ALLOW


@dataclass
class SupervisorAction:
    """An action the supervisor demands the runtime execute."""

    action: str  # "CLOSE_ALL", "HALT", "CLOSE_POSITION"
    reason: str = ""
    position_ids: list[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Hard Risk Supervisor
# ---------------------------------------------------------------------------


class HardRiskSupervisor:
    """Deterministic, non-AI risk supervisor with override authority.

    This is the final safety net.  It tracks account state independently,
    enforces absolute limits, and can shut down the entire trading system.

    Usage::

        supervisor = HardRiskSupervisor(
            limits=HardLimits(equity_floor_usd=8500),
            state_dir=Path("STATE/supervisor"),
        )
        supervisor.initialize(initial_equity=10000.0)

        # Pre-trade veto (called BEFORE RiskEngine)
        decision = supervisor.veto(request, account, positions)
        if decision.vetoed:
            reject_order(decision)

        # Post-trade tracking
        supervisor.on_trade_closed(pnl_usd=-50.0)

        # Continuous monitoring (call periodically)
        actions = supervisor.check_account(account, positions)
        for action in actions:
            execute_supervisor_action(action)
    """

    def __init__(
        self,
        limits: HardLimits | None = None,
        state_dir: str | Path | None = None,
        kill_switch_dir: str | Path | None = None,
    ) -> None:
        self._limits = limits or HardLimits()
        self._state_dir = Path(state_dir) if state_dir else Path("STATE/supervisor")
        self._kill_switch_dir = Path(kill_switch_dir) if kill_switch_dir else Path("STATE")
        self._lock = threading.RLock()

        # --- Independent state ---
        self._initial_equity: float = 0.0
        self._daily_start_equity: float = 0.0
        self._current_equity: float = 0.0
        self._halted: bool = False
        self._halt_reason: str = ""

        # Trade tracking
        self._trades_today: int = 0
        self._consecutive_losses: int = 0
        self._last_loss_cooldown_until: float = 0.0
        self._recent_losses: list[float] = []  # timestamps of recent losses
        self._recent_volumes: list[float] = []  # last 20 trade volumes

        # Session metadata
        self._session_start: float = 0.0
        self._daily_pnl_usd: float = 0.0
        self._total_pnl_usd: float = 0.0
        self._last_daily_reset: str = ""

    # ------------------------------------------------------------------
    # Timezone-safe date helper
    # ------------------------------------------------------------------

    @staticmethod
    def _today_prague() -> str:
        """Return today's date in Europe/Prague (DST-safe)."""
        return dt.datetime.now(dt.timezone.utc).astimezone(_FTMO_TZ).strftime("%Y-%m-%d")

    # ------------------------------------------------------------------
    # Properties (read-only)
    # ------------------------------------------------------------------

    @property
    def limits(self) -> HardLimits:
        return self._limits

    @property
    def halted(self) -> bool:
        return self._halted

    @property
    def halt_reason(self) -> str:
        return self._halt_reason

    @property
    def consecutive_losses(self) -> int:
        return self._consecutive_losses

    @property
    def trades_today(self) -> int:
        return self._trades_today

    @property
    def daily_pnl_usd(self) -> float:
        return self._daily_pnl_usd

    @property
    def total_pnl_usd(self) -> float:
        return self._total_pnl_usd

    # ------------------------------------------------------------------
    # Initialization
    # ------------------------------------------------------------------

    def initialize(self, initial_equity: float) -> None:
        """Set initial equity.  Call once at session start.

        Restores halted state from disk if present.
        """
        self._initial_equity = initial_equity
        self._daily_start_equity = initial_equity
        self._current_equity = initial_equity
        self._session_start = time.time()
        self._last_daily_reset = self._today_prague()

        # Restore persisted halt state
        self._restore_state()

        log.info(
            "HardRiskSupervisor initialized — equity=$%.2f limits=%s halted=%s",
            initial_equity,
            f"floor=${self._limits.equity_floor_usd} "
            f"daily_cap=${self._limits.max_daily_loss_usd} "
            f"total_cap=${self._limits.max_total_loss_usd}",
            self._halted,
        )

    # ------------------------------------------------------------------
    # Pre-trade veto — THE supreme authority
    # ------------------------------------------------------------------

    def veto(
        self,
        symbol: str,
        side: str,
        volume: float,
        entry_price: float,
        stop_loss: float | None,
        account_equity: float,
        open_positions: list[dict] | None = None,
    ) -> SupervisorDecision:
        """Evaluate whether an order should be vetoed.

        This is called BEFORE the RiskEngine.  If the supervisor says VETO,
        the order never reaches the RiskEngine.

        Args:
            symbol: Trading symbol (e.g. "EURUSD").
            side: "BUY" or "SELL".
            volume: Lot size of the proposed order.
            entry_price: Intended entry price.
            stop_loss: Stop loss price (None if no SL).
            account_equity: Current account equity from broker.
            open_positions: List of open position dicts with keys:
                symbol, side, volume, position_id.

        Returns:
            SupervisorDecision with ALLOW, VETO, or EMERGENCY_HALT.
        """
        try:
            return self._evaluate_veto(
                symbol,
                side,
                volume,
                entry_price,
                stop_loss,
                account_equity,
                open_positions or [],
            )
        except Exception as exc:
            # Fail-closed: any error → VETO
            log.error("Supervisor veto evaluation error (fail-closed): %s", exc)
            return SupervisorDecision(
                verdict=SupervisorVerdict.VETO,
                rule="supervisor_error",
                detail=f"Evaluation error (fail-closed): {exc}",
            )

    def _evaluate_veto(
        self,
        symbol: str,
        side: str,
        volume: float,
        entry_price: float,
        stop_loss: float | None,
        account_equity: float,
        open_positions: list[dict],
    ) -> SupervisorDecision:
        """Internal veto evaluation — all rules checked sequentially."""

        # Rule 0: Halted — nothing gets through
        if self._halted:
            return SupervisorDecision(
                verdict=SupervisorVerdict.EMERGENCY_HALT,
                rule="halted",
                detail=f"Supervisor halted: {self._halt_reason}",
            )

        # Rule 1: Equity floor
        if account_equity < self._limits.equity_floor_usd:
            self._emergency_halt(f"Equity ${account_equity:.2f} below floor ${self._limits.equity_floor_usd:.2f}")
            return SupervisorDecision(
                verdict=SupervisorVerdict.EMERGENCY_HALT,
                rule="equity_floor",
                detail=f"Equity ${account_equity:.2f} < floor ${self._limits.equity_floor_usd:.2f}",
            )

        # Rule 2: Daily loss cap
        self._update_equity(account_equity)
        daily_loss = self._daily_start_equity - account_equity
        if daily_loss >= self._limits.max_daily_loss_usd:
            self._emergency_halt(f"Daily loss ${daily_loss:.2f} >= cap ${self._limits.max_daily_loss_usd:.2f}")
            return SupervisorDecision(
                verdict=SupervisorVerdict.EMERGENCY_HALT,
                rule="daily_loss_cap",
                detail=f"Daily loss ${daily_loss:.2f} >= cap ${self._limits.max_daily_loss_usd:.2f}",
            )

        # Rule 3: Total loss cap
        total_loss = self._initial_equity - account_equity
        if total_loss >= self._limits.max_total_loss_usd:
            self._emergency_halt(f"Total loss ${total_loss:.2f} >= cap ${self._limits.max_total_loss_usd:.2f}")
            return SupervisorDecision(
                verdict=SupervisorVerdict.EMERGENCY_HALT,
                rule="total_loss_cap",
                detail=f"Total loss ${total_loss:.2f} >= cap ${self._limits.max_total_loss_usd:.2f}",
            )

        # Rule 4: Max lot size (fat finger)
        if volume > self._limits.max_lot_size:
            return SupervisorDecision(
                verdict=SupervisorVerdict.VETO,
                rule="max_lot_size",
                detail=f"Volume {volume:.2f} > hard max {self._limits.max_lot_size:.2f}",
            )

        # Rule 5: Max concurrent positions
        if len(open_positions) >= self._limits.max_concurrent_positions:
            return SupervisorDecision(
                verdict=SupervisorVerdict.VETO,
                rule="max_concurrent_positions",
                detail=(f"{len(open_positions)} open positions >= hard max {self._limits.max_concurrent_positions}"),
            )

        # Rule 6: Currency concentration
        same_symbol = sum(1 for p in open_positions if p.get("symbol") == symbol)
        if same_symbol >= self._limits.max_same_symbol_positions:
            return SupervisorDecision(
                verdict=SupervisorVerdict.VETO,
                rule="symbol_concentration",
                detail=(
                    f"{same_symbol} existing position(s) on {symbol} >= limit {self._limits.max_same_symbol_positions}"
                ),
            )

        # Rule 7: Daily trade count
        if self._trades_today >= self._limits.max_trades_per_day:
            return SupervisorDecision(
                verdict=SupervisorVerdict.VETO,
                rule="daily_trade_cap",
                detail=(f"{self._trades_today} trades today >= hard max {self._limits.max_trades_per_day}"),
            )

        # Rule 8: Consecutive loss cooldown
        now = time.time()
        if now < self._last_loss_cooldown_until:
            remaining = int(self._last_loss_cooldown_until - now)
            return SupervisorDecision(
                verdict=SupervisorVerdict.VETO,
                rule="consecutive_loss_cooldown",
                detail=(f"{self._consecutive_losses} consecutive losses — cooldown active ({remaining}s remaining)"),
            )

        # Rule 9: Rapid loss detection
        self._prune_recent_losses()
        if len(self._recent_losses) >= self._limits.rapid_loss_count:
            self._emergency_halt(f"{len(self._recent_losses)} losses in {self._limits.rapid_loss_window_s}s window")
            return SupervisorDecision(
                verdict=SupervisorVerdict.EMERGENCY_HALT,
                rule="rapid_loss_detection",
                detail=(f"{len(self._recent_losses)} losses in {self._limits.rapid_loss_window_s}s — emergency halt"),
            )

        # Rule 10: Stop loss distance
        if stop_loss is not None and entry_price > 0:
            distance_pips = abs(entry_price - stop_loss) / _pip_value(symbol)
            if distance_pips < self._limits.min_stop_distance_pips:
                return SupervisorDecision(
                    verdict=SupervisorVerdict.VETO,
                    rule="min_stop_distance",
                    detail=(
                        f"SL distance {distance_pips:.1f} pips < min {self._limits.min_stop_distance_pips:.1f} pips"
                    ),
                )

        # Rule 11: Per-trade risk (SL-based) — scale down to fit, don't veto
        adjusted_volume: float | None = None
        if stop_loss is not None and entry_price > 0:
            sl_distance = abs(entry_price - stop_loss)
            pip_val = _pip_value(symbol)
            pip_usd = _pip_usd_value(symbol)
            # Approximate risk: volume × (sl_distance / pip_value) × $10/pip for standard lot
            risk_usd = volume * (sl_distance / pip_val) * pip_usd
            if risk_usd > self._limits.max_loss_per_trade_usd:
                # Scale lot size down to fit the per-trade risk cap
                risk_per_lot = (sl_distance / pip_val) * pip_usd
                if risk_per_lot > 0:
                    safe_volume = round(self._limits.max_loss_per_trade_usd / risk_per_lot, 2)
                    safe_volume = max(0.01, safe_volume)  # minimum lot
                    adjusted_volume = safe_volume
                    log.info(
                        "Risk gate scaled volume %.2f → %.2f to fit $%.0f/trade cap (risk was $%.2f)",
                        volume,
                        safe_volume,
                        self._limits.max_loss_per_trade_usd,
                        risk_usd,
                    )
                else:
                    return SupervisorDecision(
                        verdict=SupervisorVerdict.VETO,
                        rule="max_loss_per_trade",
                        detail="Cannot compute safe volume — zero risk-per-lot",
                    )

        # Rule 12: Fat finger volume check (vs median of recent trades)
        if self._recent_volumes and len(self._recent_volumes) >= 3:
            sorted_vols = sorted(self._recent_volumes)
            median = sorted_vols[len(sorted_vols) // 2]
            if median > 0 and volume > median * self._limits.max_volume_ratio:
                return SupervisorDecision(
                    verdict=SupervisorVerdict.VETO,
                    rule="fat_finger_volume",
                    detail=(f"Volume {volume:.2f} > {self._limits.max_volume_ratio:.1f}× median {median:.2f}"),
                )

        # All rules passed
        return SupervisorDecision(verdict=SupervisorVerdict.ALLOW, adjusted_volume=adjusted_volume)

    # ------------------------------------------------------------------
    # Post-trade tracking
    # ------------------------------------------------------------------

    def on_trade_opened(self, volume: float) -> None:
        """Record that a trade was opened (for daily count and volume tracking)."""
        with self._lock:
            self._trades_today += 1
            self._recent_volumes.append(volume)
            if len(self._recent_volumes) > 20:
                self._recent_volumes = self._recent_volumes[-20:]

    def on_trade_closed(self, pnl_usd: float) -> None:
        """Record a trade closure and update P&L tracking.

        Args:
            pnl_usd: Realized P&L in USD.  Negative = loss.
        """
        with self._lock:
            self._daily_pnl_usd += pnl_usd
            self._total_pnl_usd += pnl_usd
            self._current_equity += pnl_usd

            if pnl_usd < 0:
                self._consecutive_losses += 1
                self._recent_losses.append(time.time())

                # Trigger cooldown if streak limit reached
                if self._consecutive_losses >= self._limits.consecutive_loss_limit:
                    self._last_loss_cooldown_until = time.time() + self._limits.consecutive_loss_cooldown_s
                    log.warning(
                        "SUPERVISOR: %d consecutive losses — cooldown activated for %ds",
                        self._consecutive_losses,
                        self._limits.consecutive_loss_cooldown_s,
                    )
            else:
                # Winning trade resets consecutive loss counter
                self._consecutive_losses = 0

            # Check rapid loss
            self._prune_recent_losses()
            if len(self._recent_losses) >= self._limits.rapid_loss_count:
                self._emergency_halt(
                    f"Rapid loss: {len(self._recent_losses)} losses in {self._limits.rapid_loss_window_s}s"
                )

        self._persist_state()

    # ------------------------------------------------------------------
    # Continuous account monitoring
    # ------------------------------------------------------------------

    def check_account(
        self,
        account_equity: float,
        positions: list[dict] | None = None,
    ) -> list[SupervisorAction]:
        """Periodic account health check.  Call from monitoring loop.

        Returns a list of actions the runtime MUST execute.  An empty list
        means no intervention required.

        Args:
            account_equity: Current broker-reported equity.
            positions: Current open positions (dicts with position_id, symbol, etc).

        Returns:
            List of SupervisorAction objects.  Possible actions:
              - CLOSE_ALL: close all positions immediately
              - HALT: activate system kill switch
        """
        actions: list[SupervisorAction] = []
        positions = positions or []

        self._update_equity(account_equity)
        self._maybe_daily_reset(account_equity)

        # Check equity floor
        if account_equity < self._limits.equity_floor_usd:
            self._emergency_halt(f"Equity ${account_equity:.2f} below floor ${self._limits.equity_floor_usd:.2f}")
            pos_ids = [p.get("position_id", "") for p in positions if p.get("position_id")]
            if pos_ids:
                actions.append(
                    SupervisorAction(
                        action="CLOSE_ALL",
                        reason=f"Equity floor breach: ${account_equity:.2f}",
                        position_ids=pos_ids,
                    )
                )
            actions.append(
                SupervisorAction(
                    action="HALT",
                    reason=f"Equity floor breach: ${account_equity:.2f}",
                )
            )
            return actions

        # Check daily loss
        daily_loss = self._daily_start_equity - account_equity
        if daily_loss >= self._limits.max_daily_loss_usd:
            self._emergency_halt(f"Daily loss ${daily_loss:.2f} >= cap ${self._limits.max_daily_loss_usd:.2f}")
            pos_ids = [p.get("position_id", "") for p in positions if p.get("position_id")]
            if pos_ids:
                actions.append(
                    SupervisorAction(
                        action="CLOSE_ALL",
                        reason=f"Daily loss cap breach: ${daily_loss:.2f}",
                        position_ids=pos_ids,
                    )
                )
            actions.append(
                SupervisorAction(
                    action="HALT",
                    reason=f"Daily loss cap: ${daily_loss:.2f}",
                )
            )
            return actions

        # Check total loss
        total_loss = self._initial_equity - account_equity
        if total_loss >= self._limits.max_total_loss_usd:
            self._emergency_halt(f"Total loss ${total_loss:.2f} >= cap ${self._limits.max_total_loss_usd:.2f}")
            pos_ids = [p.get("position_id", "") for p in positions if p.get("position_id")]
            if pos_ids:
                actions.append(
                    SupervisorAction(
                        action="CLOSE_ALL",
                        reason=f"Total loss cap breach: ${total_loss:.2f}",
                        position_ids=pos_ids,
                    )
                )
            actions.append(
                SupervisorAction(
                    action="HALT",
                    reason=f"Total loss cap: ${total_loss:.2f}",
                )
            )
            return actions

        return actions

    # ------------------------------------------------------------------
    # Emergency halt — the nuclear option
    # ------------------------------------------------------------------

    def emergency_halt(self, reason: str) -> None:
        """Public entry point for external emergency halt requests."""
        self._emergency_halt(reason)

    def _emergency_halt(self, reason: str) -> None:
        """Activate emergency halt.

        1. Sets internal halt flag
        2. Persists halt to disk
        3. Writes system kill switch file (overrides everything)
        """
        if self._halted:
            return  # Already halted

        with self._lock:
            self._halted = True
            self._halt_reason = reason

        log.critical("🚨 HARD RISK SUPERVISOR EMERGENCY HALT: %s", reason)

        # Persist supervisor state
        self._persist_state()

        # Write system kill switch file — this overrides EVERYTHING
        self._write_kill_switch(reason)

    def resume(self) -> None:
        """Resume after emergency halt.  Requires explicit operator action.

        Does NOT remove the system kill switch file — operator must do that
        separately to ensure full intentionality.
        """
        if not self._halted:
            return

        with self._lock:
            self._halted = False
            self._halt_reason = ""
            self._consecutive_losses = 0
            self._last_loss_cooldown_until = 0.0
            self._recent_losses.clear()

        self._persist_state()

        log.warning("SUPERVISOR: Resumed from halt by operator action")

    # ------------------------------------------------------------------
    # Daily reset
    # ------------------------------------------------------------------

    def reset_daily(self, equity: float) -> None:
        """Reset daily counters.  Call at start of each trading day.

        Does NOT clear a halt state.
        """
        with self._lock:
            self._daily_start_equity = equity
            self._trades_today = 0
            self._daily_pnl_usd = 0.0
            self._last_daily_reset = self._today_prague()
            self._recent_losses.clear()

        self._persist_state()
        log.info("SUPERVISOR: Daily reset — equity=$%.2f", equity)

    def _maybe_daily_reset(self, equity: float) -> None:
        """Auto-reset daily counters if the date has changed."""
        today = self._today_prague()
        if today != self._last_daily_reset:
            self.reset_daily(equity)

    # ------------------------------------------------------------------
    # State snapshot
    # ------------------------------------------------------------------

    def snapshot(self) -> dict:
        """Return a point-in-time snapshot of the supervisor state."""
        return {
            "halted": self._halted,
            "halt_reason": self._halt_reason,
            "initial_equity": round(self._initial_equity, 2),
            "daily_start_equity": round(self._daily_start_equity, 2),
            "current_equity": round(self._current_equity, 2),
            "daily_pnl_usd": round(self._daily_pnl_usd, 2),
            "total_pnl_usd": round(self._total_pnl_usd, 2),
            "trades_today": self._trades_today,
            "consecutive_losses": self._consecutive_losses,
            "cooldown_active": time.time() < self._last_loss_cooldown_until,
            "cooldown_remaining_s": max(0, int(self._last_loss_cooldown_until - time.time())),
            "limits": {
                "equity_floor_usd": self._limits.equity_floor_usd,
                "max_daily_loss_usd": self._limits.max_daily_loss_usd,
                "max_total_loss_usd": self._limits.max_total_loss_usd,
                "max_loss_per_trade_usd": self._limits.max_loss_per_trade_usd,
                "max_lot_size": self._limits.max_lot_size,
                "max_concurrent_positions": self._limits.max_concurrent_positions,
                "max_trades_per_day": self._limits.max_trades_per_day,
            },
            "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _update_equity(self, equity: float) -> None:
        """Update tracked equity from broker reading."""
        self._current_equity = equity

    def _prune_recent_losses(self) -> None:
        """Remove loss timestamps outside the rapid-loss window."""
        cutoff = time.time() - self._limits.rapid_loss_window_s
        self._recent_losses = [t for t in self._recent_losses if t > cutoff]

    def _persist_state(self) -> None:
        """Write supervisor state to disk for crash recovery."""
        try:
            self._state_dir.mkdir(parents=True, exist_ok=True)
            state_file = self._state_dir / "hard_supervisor_state.json"

            payload = {
                "halted": self._halted,
                "halt_reason": self._halt_reason,
                "initial_equity": self._initial_equity,
                "daily_start_equity": self._daily_start_equity,
                "current_equity": self._current_equity,
                "daily_pnl_usd": self._daily_pnl_usd,
                "total_pnl_usd": self._total_pnl_usd,
                "trades_today": self._trades_today,
                "consecutive_losses": self._consecutive_losses,
                "cooldown_until": self._last_loss_cooldown_until,
                "last_daily_reset": self._last_daily_reset,
                "last_updated": dt.datetime.now(dt.timezone.utc).isoformat(),
            }

            with self._lock:
                tmp = state_file.with_suffix(".tmp")
                tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
                tmp.replace(state_file)

        except OSError as exc:
            log.error("SUPERVISOR: Failed to persist state: %s", exc)

    def _restore_state(self) -> None:
        """Restore persisted halt state from disk."""
        state_file = self._state_dir / "hard_supervisor_state.json"
        if not state_file.exists():
            return

        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("SUPERVISOR: Could not read persisted state: %s", exc)
            return

        if not isinstance(data, dict):
            return

        if data.get("halted"):
            self._halted = True
            self._halt_reason = data.get("halt_reason", "halt restored from prior session")
            log.warning(
                "SUPERVISOR: Restored HALT from disk: %s",
                self._halt_reason,
            )

        # Restore counters
        self._consecutive_losses = data.get("consecutive_losses", 0)
        self._trades_today = data.get("trades_today", 0)
        self._daily_pnl_usd = data.get("daily_pnl_usd", 0.0)
        self._total_pnl_usd = data.get("total_pnl_usd", 0.0)
        self._last_loss_cooldown_until = data.get("cooldown_until", 0.0)
        self._last_daily_reset = data.get("last_daily_reset", "")

    def _write_kill_switch(self, reason: str) -> None:
        """Write the system kill switch file.

        This is the ultimate override — it activates the NovaCore kill switch
        which halts ALL trading regardless of what any other component thinks.
        """
        try:
            self._kill_switch_dir.mkdir(parents=True, exist_ok=True)
            kill_file = self._kill_switch_dir / "nova-kill"
            kill_file.write_text("CONFIRM", encoding="utf-8")
            log.critical(
                "SUPERVISOR: Wrote system kill switch to %s — reason: %s",
                kill_file,
                reason,
            )
        except OSError as exc:
            log.error("SUPERVISOR: CRITICAL — failed to write kill switch: %s", exc)


# ---------------------------------------------------------------------------
# Pip value helpers — delegated to shared module
# ---------------------------------------------------------------------------

from novatrade.risk.symbol_helpers import pip_usd_value as _pip_usd_value  # noqa: E402
from novatrade.risk.symbol_helpers import pip_value as _pip_value  # noqa: E402
