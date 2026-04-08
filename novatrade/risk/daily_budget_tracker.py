"""Daily Risk Budget Tracker for NovaTrade.

P1 Priority from funded account survival research: track and allocate daily
risk budget across multiple trades rather than just checking compliance.

Key insight: "Size based on daily limit, not account — $1K risk on $100K =
20% of daily limit in one trade"

Instead of risking 1% of account balance per trade ($1K on $100K), we should
risk a percentage of the DAILY LOSS LIMIT ($5K on $100K FTMO = 5% daily limit).

This prevents:
- Multiple trades consuming the entire daily budget in early hours
- Risk concentration that violates FTMO daily limits
- Poor risk distribution across trading sessions

Design principles:
- Track remaining daily budget in real-time
- Allocate risk per trade based on remaining budget
- Reset at midnight Europe/Prague (FTMO timezone)
- Integrate with existing PreTradeGate and FtmoDailyLossTracker
- Support multiple allocation strategies (equal, weighted, time-based)
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from novatrade.models import RiskCheckResult

log = logging.getLogger("novatrade.risk.daily_budget_tracker")

_FTMO_TZ = ZoneInfo("Europe/Prague")
_STATE_DIR = Path(__file__).resolve().parent.parent.parent / "STATE" / "novatrade"


@dataclass
class TradeRiskAllocation:
    """Records a single trade's risk allocation from the daily budget."""

    timestamp: float
    symbol: str
    allocated_risk_usd: float
    entry_price: float
    stop_loss: float
    volume: float
    trade_id: str = ""  # For tracking actual vs allocated


@dataclass
class DailyRiskBudgetTracker:
    """Tracks and allocates daily risk budget across multiple trades.

    Core concept: Instead of risking 1% of account per trade, risk a percentage
    of the daily loss limit to ensure proper risk distribution throughout the day.

    For a $100K FTMO account:
    - Daily loss limit: $5K (5%)
    - Traditional: $1K/trade (20% of daily budget in one trade)
    - Budget-aware: $500-1K/trade (10-20% of daily budget, adaptive)

    Features:
    - Real-time budget tracking
    - Multiple allocation strategies
    - Time-based budget scaling
    - Integration with FTMO compliance
    """

    # Configuration
    daily_loss_limit_pct: float = 5.0  # FTMO standard
    max_risk_per_trade_pct: float = 20.0  # Max % of daily budget per trade
    min_risk_per_trade_pct: float = 5.0  # Min % of daily budget per trade
    max_trades_per_day: int = 10  # Expected maximum number of trades
    allocation_strategy: str = "adaptive"  # "equal", "adaptive", "time_weighted"

    # State
    _initial_budget_usd: float = field(default=0.0, repr=False)
    _allocated_today: list[TradeRiskAllocation] = field(default_factory=list, repr=False)
    _day_key: str = field(default="", repr=False)
    _initialized: bool = field(default=False, repr=False)

    def __post_init__(self) -> None:
        """Ensure state directory exists."""
        _STATE_DIR.mkdir(parents=True, exist_ok=True)

    # -- Properties ----------------------------------------------------------

    @property
    def daily_budget_usd(self) -> float:
        """Total daily risk budget in USD."""
        return self._initial_budget_usd

    @property
    def allocated_today_usd(self) -> float:
        """Total risk already allocated today."""
        return sum(alloc.allocated_risk_usd for alloc in self._allocated_today)

    @property
    def remaining_budget_usd(self) -> float:
        """Remaining risk budget for today."""
        return max(0.0, self.daily_budget_usd - self.allocated_today_usd)

    @property
    def budget_utilization_pct(self) -> float:
        """Percentage of daily budget already allocated."""
        if self.daily_budget_usd <= 0:
            return 0.0
        return (self.allocated_today_usd / self.daily_budget_usd) * 100

    @property
    def trades_today(self) -> int:
        """Number of trades allocated today."""
        return len(self._allocated_today)

    # -- Initialization ------------------------------------------------------

    def initialize(self, account_equity: float) -> None:
        """Initialize daily budget based on account equity.

        Args:
            account_equity: Current account equity for budget calculation
        """
        self._initial_budget_usd = account_equity * (self.daily_loss_limit_pct / 100.0)
        self._maybe_new_day()
        self._initialized = True

        log.info(
            "DailyRiskBudgetTracker initialized: equity=$%.2f budget=$%.2f (%.1f%% daily limit)",
            account_equity,
            self._initial_budget_usd,
            self.daily_loss_limit_pct,
        )

    # -- Day boundary management ---------------------------------------------

    def _maybe_new_day(self) -> None:
        """Check for day rollover and reset budget if needed."""
        today = datetime.now(timezone.utc).astimezone(_FTMO_TZ).strftime("%Y-%m-%d")
        if today != self._day_key:
            prev_allocated = len(self._allocated_today)
            prev_amount = self.allocated_today_usd

            self._allocated_today.clear()
            self._day_key = today

            if prev_allocated > 0:
                log.info(
                    "Daily budget reset: %s → %s (%d trades, $%.2f allocated yesterday)",
                    self._day_key,
                    today,
                    prev_allocated,
                    prev_amount,
                )

    # -- Risk allocation -----------------------------------------------------

    def calculate_trade_risk(
        self,
        symbol: str,
        entry_price: float,
        stop_loss: float,
        current_time: float | None = None,
    ) -> tuple[float, str]:
        """Calculate appropriate risk allocation for a trade.

        Returns:
            Tuple of (risk_usd, reasoning)
        """
        if not self._initialized:
            return 0.0, "budget tracker not initialized"

        self._maybe_new_day()

        if current_time is None:
            current_time = time.time()

        # Check if we have budget remaining
        if self.remaining_budget_usd <= 0:
            return 0.0, f"daily budget exhausted ({self.allocated_today_usd:.0f}/{self.daily_budget_usd:.0f})"

        # Calculate base risk allocation
        if self.allocation_strategy == "equal":
            risk_usd = self._calculate_equal_allocation()
        elif self.allocation_strategy == "adaptive":
            risk_usd = self._calculate_adaptive_allocation(current_time)
        elif self.allocation_strategy == "time_weighted":
            risk_usd = self._calculate_time_weighted_allocation(current_time)
        else:
            log.warning(f"Unknown allocation strategy: {self.allocation_strategy}")
            risk_usd = self._calculate_adaptive_allocation(current_time)

        # Apply bounds
        min_risk = self.daily_budget_usd * (self.min_risk_per_trade_pct / 100.0)
        max_risk = self.daily_budget_usd * (self.max_risk_per_trade_pct / 100.0)

        # Don't exceed remaining budget
        max_risk = min(max_risk, self.remaining_budget_usd)

        risk_usd = max(min_risk, min(risk_usd, max_risk))

        reasoning = (
            f"{self.allocation_strategy} allocation: ${risk_usd:.0f} "
            f"({risk_usd / self.daily_budget_usd * 100:.1f}% of daily budget, "
            f"{self.remaining_budget_usd:.0f} remaining)"
        )

        return risk_usd, reasoning

    def _calculate_equal_allocation(self) -> float:
        """Allocate budget equally across expected number of trades."""
        if self.trades_today >= self.max_trades_per_day:
            return 0.0

        remaining_trades = self.max_trades_per_day - self.trades_today
        return self.remaining_budget_usd / remaining_trades

    def _calculate_adaptive_allocation(self, current_time: float) -> float:
        """Adaptive allocation based on budget utilization and time of day."""
        # Base allocation: scale down as budget gets consumed
        utilization = self.budget_utilization_pct / 100.0
        base_pct = self.max_risk_per_trade_pct * (1.0 - utilization * 0.5)  # Reduce by 50% as budget consumed

        # Time factor: be more conservative later in the day
        utc_dt = datetime.fromtimestamp(current_time, tz=timezone.utc)
        prague_dt = utc_dt.astimezone(_FTMO_TZ)
        hour = prague_dt.hour

        # More conservative from 16:00-22:00 Prague time (US session overlap + close)
        if 16 <= hour <= 22:
            base_pct *= 0.8

        return self.daily_budget_usd * (base_pct / 100.0)

    def _calculate_time_weighted_allocation(self, current_time: float) -> float:
        """Time-weighted allocation: larger allocations during high-probability windows."""
        utc_dt = datetime.fromtimestamp(current_time, tz=timezone.utc)
        prague_dt = utc_dt.astimezone(_FTMO_TZ)
        hour = prague_dt.hour

        # High probability windows (London + NY open/overlap)
        if 8 <= hour <= 12:  # London morning
            weight = 1.2
        elif 14 <= hour <= 17:  # NY-London overlap
            weight = 1.4
        elif 20 <= hour <= 24:  # NY afternoon
            weight = 1.1
        else:  # Asian/quiet hours
            weight = 0.7

        base_allocation = self.remaining_budget_usd / max(1, self.max_trades_per_day - self.trades_today)
        return base_allocation * weight

    # -- Trade recording -----------------------------------------------------

    def allocate_trade_risk(
        self,
        symbol: str,
        entry_price: float,
        stop_loss: float,
        volume: float,
        trade_id: str = "",
    ) -> RiskCheckResult:
        """Record a trade allocation against the daily budget.

        This should be called AFTER a trade is successfully placed.
        """
        risk_usd, reasoning = self.calculate_trade_risk(symbol, entry_price, stop_loss)

        if risk_usd <= 0:
            return RiskCheckResult(
                name="daily_budget",
                passed=False,
                detail=f"Cannot allocate risk: {reasoning}",
            )

        # Calculate actual risk based on volume and price
        stop_distance = abs(entry_price - stop_loss)
        pip_value = self._get_pip_value(symbol)
        stop_pips = stop_distance / pip_value
        actual_risk = volume * stop_pips * 10.0  # $10/pip for standard lot

        # Record the allocation
        allocation = TradeRiskAllocation(
            timestamp=time.time(),
            symbol=symbol,
            allocated_risk_usd=risk_usd,
            entry_price=entry_price,
            stop_loss=stop_loss,
            volume=volume,
            trade_id=trade_id,
        )

        self._allocated_today.append(allocation)

        log.info(
            "Risk allocated: %s vol=%.2f risk=$%.0f (budget: %.1f%% used, $%.0f remaining)",
            symbol,
            volume,
            actual_risk,
            self.budget_utilization_pct,
            self.remaining_budget_usd,
        )

        return RiskCheckResult(
            name="daily_budget",
            passed=True,
            detail=f"risk allocated: ${actual_risk:.0f}, {reasoning}",
        )

    @staticmethod
    def _get_pip_value(symbol: str) -> float:
        """Get pip value for symbol — delegates to shared helper."""
        from novatrade.risk.symbol_helpers import pip_value

        return pip_value(symbol)

    # -- Budget checking for pre-trade gate ----------------------------------

    def check_budget_availability(self, required_risk_usd: float) -> RiskCheckResult:
        """Check if sufficient budget is available for a trade.

        This is called by PreTradeGate before placing orders.
        """
        if not self._initialized:
            return RiskCheckResult(
                name="daily_budget_check",
                passed=True,
                detail="budget tracker not initialized — skipped",
            )

        self._maybe_new_day()

        if self.remaining_budget_usd <= 0:
            return RiskCheckResult(
                name="daily_budget_check",
                passed=False,
                detail=f"daily risk budget exhausted: {self.allocated_today_usd:.0f}/{self.daily_budget_usd:.0f} used",
            )

        if required_risk_usd > self.remaining_budget_usd:
            return RiskCheckResult(
                name="daily_budget_check",
                passed=False,
                detail=f"insufficient budget: need ${required_risk_usd:.0f}, have ${self.remaining_budget_usd:.0f}",
            )

        max_single_trade = self.daily_budget_usd * (self.max_risk_per_trade_pct / 100.0)
        if required_risk_usd > max_single_trade:
            return RiskCheckResult(
                name="daily_budget_check",
                passed=False,
                detail=(
                    f"trade risk ${required_risk_usd:.0f} exceeds max per trade "
                    f"${max_single_trade:.0f} ({self.max_risk_per_trade_pct}%)"
                ),
            )

        return RiskCheckResult(
            name="daily_budget_check",
            passed=True,
            detail=(
                f"budget available: ${self.remaining_budget_usd:.0f} "
                f"(${required_risk_usd:.0f} needed, {self.budget_utilization_pct:.1f}% used)"
            ),
        )

    # -- State persistence ---------------------------------------------------

    def save_state(self) -> None:
        """Persist daily allocations to disk."""
        if not self._day_key:
            return

        state_file = _STATE_DIR / f"daily_budget_{self._day_key}.json"

        state_data = {
            "day_key": self._day_key,
            "daily_budget_usd": self._initial_budget_usd,
            "allocations": [
                {
                    "timestamp": alloc.timestamp,
                    "symbol": alloc.symbol,
                    "allocated_risk_usd": alloc.allocated_risk_usd,
                    "entry_price": alloc.entry_price,
                    "stop_loss": alloc.stop_loss,
                    "volume": alloc.volume,
                    "trade_id": alloc.trade_id,
                }
                for alloc in self._allocated_today
            ],
        }

        try:
            with state_file.open("w") as f:
                json.dump(state_data, f, indent=2)
            log.debug(f"Daily budget state saved to {state_file}")
        except Exception as exc:
            log.error(f"Failed to save daily budget state: {exc}")

    def load_state(self) -> None:
        """Load daily allocations from disk if available."""
        if not self._day_key:
            self._maybe_new_day()

        state_file = _STATE_DIR / f"daily_budget_{self._day_key}.json"

        if not state_file.exists():
            return

        try:
            with state_file.open() as f:
                state_data = json.load(f)

            if state_data.get("day_key") != self._day_key:
                return  # Stale data

            self._initial_budget_usd = state_data.get("daily_budget_usd", 0.0)

            for alloc_data in state_data.get("allocations", []):
                allocation = TradeRiskAllocation(
                    timestamp=alloc_data["timestamp"],
                    symbol=alloc_data["symbol"],
                    allocated_risk_usd=alloc_data["allocated_risk_usd"],
                    entry_price=alloc_data["entry_price"],
                    stop_loss=alloc_data["stop_loss"],
                    volume=alloc_data["volume"],
                    trade_id=alloc_data.get("trade_id", ""),
                )
                self._allocated_today.append(allocation)

            log.info(
                f"Daily budget state loaded: {len(self._allocated_today)} allocations, "
                f"${self.allocated_today_usd:.0f} used of ${self.daily_budget_usd:.0f}"
            )

        except Exception as exc:
            log.error(f"Failed to load daily budget state: {exc}")

    # -- Status and diagnostics ----------------------------------------------

    def get_status_summary(self) -> dict:
        """Get current budget status for monitoring/logging."""
        return {
            "day_key": self._day_key,
            "budget_usd": self.daily_budget_usd,
            "allocated_usd": self.allocated_today_usd,
            "remaining_usd": self.remaining_budget_usd,
            "utilization_pct": self.budget_utilization_pct,
            "trades_today": self.trades_today,
            "max_per_trade_usd": self.daily_budget_usd * (self.max_risk_per_trade_pct / 100.0),
            "allocation_strategy": self.allocation_strategy,
        }
