"""Profit Cushion Protocol for NovaTrade.

P2 Priority from funded account survival research: progressively reduce risk as
cycle profit approaches the 10% FTMO scaling target to protect gains and ensure
scaling eligibility.

Research insight: "Most give-backs happen after strong months, not weak ones."
Industry data shows 70% of funded traders fail within 3 months, often due to
risk escalation after early profits.

Profit Cushion Tiers:
- Normal (0-3% profit): Full operation, 1.0x risk multiplier
- Conservative (+3-5% profit): Reduce risk to 0.75x normal
- Selective (+5-8% profit): Only A++ setups, 0.5x risk
- Turtle Mode (+8-10% profit): Minimum activity to hit trading days, 0.25x risk
- Scaling Lock (+10%+ profit): Consider halting to protect scaling eligibility

Design principles:
- Track cycle profit from FTMO cycle start date (4-month cycles)
- Progressive risk reduction as profit targets are approached
- Integrate with DailyRiskBudgetTracker for compound risk management
- FTMO timezone-aware cycle boundaries (Europe/Prague)
- State persistence for crash recovery
- Configurable profit thresholds and risk multipliers
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

log = logging.getLogger("novatrade.risk.profit_cushion_protocol")


@dataclass
class ProfitCushionState:
    """Current profit cushion state for a trading cycle.

    Tracks progress toward FTMO 10% scaling target and provides
    appropriate risk multipliers for each tier.
    """

    cycle_start_date: datetime
    cycle_end_date: datetime
    starting_equity: float
    current_equity: float
    cycle_profit_pct: float = 0.0
    tier: str = "normal"
    risk_multiplier: float = 1.0
    tier_changed_at: datetime | None = None
    last_updated: datetime | None = None

    @property
    def cycle_profit_amount(self) -> float:
        """Absolute profit in dollars for this cycle."""
        return self.current_equity - self.starting_equity

    @property
    def days_in_cycle(self) -> int:
        """Days elapsed since cycle start."""
        return (datetime.now(ZoneInfo("Europe/Prague")) - self.cycle_start_date).days

    @property
    def days_remaining(self) -> int:
        """Days remaining until cycle end."""
        return (self.cycle_end_date - datetime.now(ZoneInfo("Europe/Prague"))).days

    def is_scaling_eligible(self, scaling_threshold: float = 10.0) -> bool:
        """True if cycle profit >= scaling threshold.

        Args:
            scaling_threshold: Minimum profit percentage for scaling eligibility
        """
        return self.cycle_profit_pct >= scaling_threshold


@dataclass
class ProfitCushionConfig:
    """Configuration for profit cushion protocol.

    Defines profit thresholds and corresponding risk multipliers.
    """

    # Profit thresholds (as percentages of cycle starting equity)
    conservative_threshold: float = 3.0  # Start reducing risk at +3%
    selective_threshold: float = 5.0  # Only A++ setups at +5%
    turtle_threshold: float = 8.0  # Turtle mode at +8%
    scaling_threshold: float = 10.0  # FTMO scaling eligibility at +10%

    # Risk multipliers for each tier
    normal_multiplier: float = 1.0  # Full risk (0-3% profit)
    conservative_multiplier: float = 0.75  # 25% risk reduction (3-5% profit)
    selective_multiplier: float = 0.5  # 50% risk reduction (5-8% profit)
    turtle_multiplier: float = 0.25  # 75% risk reduction (8-10% profit)
    scaling_lock_multiplier: float = 0.10  # 90% risk reduction (10%+ profit)

    # Cycle configuration
    cycle_length_months: int = 4  # FTMO scaling cycle length
    timezone: str = "Europe/Prague"  # FTMO timezone

    # State persistence
    state_file_path: str = "/tmp/novatrade_profit_cushion_state.json"  # noqa: S108
    auto_save: bool = True

    def get_risk_multiplier(self, profit_pct: float) -> tuple[str, float]:
        """Get tier name and risk multiplier for given profit percentage.

        Args:
            profit_pct: Cycle profit as percentage of starting equity

        Returns:
            Tuple of (tier_name, risk_multiplier)
        """
        if profit_pct >= self.scaling_threshold:
            return "scaling_lock", self.scaling_lock_multiplier
        elif profit_pct >= self.turtle_threshold:
            return "turtle", self.turtle_multiplier
        elif profit_pct >= self.selective_threshold:
            return "selective", self.selective_multiplier
        elif profit_pct >= self.conservative_threshold:
            return "conservative", self.conservative_multiplier
        else:
            return "normal", self.normal_multiplier


class ProfitCushionProtocol:
    """Profit cushion protocol implementation.

    Tracks cycle profit and provides risk multipliers to progressively
    reduce position sizing as profit targets are approached.

    Usage:
        protocol = ProfitCushionProtocol()
        protocol.update_equity(current_equity=102500)  # +2.5% profit
        multiplier = protocol.get_risk_multiplier()    # Returns 1.0 (normal)

        protocol.update_equity(current_equity=104000)  # +4% profit
        multiplier = protocol.get_risk_multiplier()    # Returns 0.75 (conservative)
    """

    def __init__(
        self,
        config: ProfitCushionConfig | None = None,
        starting_equity: float | None = None,
        cycle_start_date: datetime | None = None,
    ):
        self.config = config or ProfitCushionConfig()

        # Try to load existing state, or initialize new cycle
        self._state = self._load_state()

        if self._state is None:
            # Initialize new cycle
            if starting_equity is None or cycle_start_date is None:
                raise ValueError("Must provide starting_equity and cycle_start_date for new cycle")
            self._initialize_new_cycle(starting_equity, cycle_start_date)

        # Update state if parameters provided (for manual override)
        if self._state is None:
            return  # should not happen — _initialize_new_cycle sets _state
        if starting_equity is not None:
            self._state.starting_equity = starting_equity
        if cycle_start_date is not None:
            self._state.cycle_start_date = cycle_start_date
            self._update_cycle_end_date()

    def _initialize_new_cycle(self, starting_equity: float, cycle_start_date: datetime) -> None:
        """Initialize a new FTMO cycle."""
        tz = ZoneInfo(self.config.timezone)

        # Ensure cycle start is timezone-aware
        if cycle_start_date.tzinfo is None:
            cycle_start_date = cycle_start_date.replace(tzinfo=tz)

        # Calculate cycle end date (4 months later)
        from dateutil.relativedelta import relativedelta

        cycle_end_date = cycle_start_date + relativedelta(months=self.config.cycle_length_months)

        self._state = ProfitCushionState(
            cycle_start_date=cycle_start_date,
            cycle_end_date=cycle_end_date,
            starting_equity=starting_equity,
            current_equity=starting_equity,
            cycle_profit_pct=0.0,
            tier="normal",
            risk_multiplier=1.0,
            last_updated=datetime.now(tz),
        )

        log.info(
            f"Initialized new profit cushion cycle: "
            f"start={cycle_start_date.isoformat()}, "
            f"end={cycle_end_date.isoformat()}, "
            f"equity=${starting_equity:,.2f}"
        )

        if self.config.auto_save:
            self._save_state()

    def _update_cycle_end_date(self) -> None:
        """Update cycle end date based on current cycle start."""
        from dateutil.relativedelta import relativedelta

        if self._state is None:
            return
        self._state.cycle_end_date = self._state.cycle_start_date + relativedelta(
            months=self.config.cycle_length_months
        )

    def update_equity(self, current_equity: float) -> bool:
        """Update current equity and recalculate profit cushion tier.

        Args:
            current_equity: Current account equity

        Returns:
            True if tier changed, False otherwise
        """
        if self._state is None:
            raise RuntimeError("Profit cushion not initialized. Call with starting parameters.")

        old_tier = self._state.tier

        # Update equity and calculate profit percentage
        self._state.current_equity = current_equity
        self._state.cycle_profit_pct = (
            (current_equity - self._state.starting_equity) / self._state.starting_equity * 100.0
        )

        # Update tier and risk multiplier
        tier, multiplier = self.config.get_risk_multiplier(self._state.cycle_profit_pct)
        self._state.tier = tier
        self._state.risk_multiplier = multiplier
        self._state.last_updated = datetime.now(ZoneInfo(self.config.timezone))

        tier_changed = old_tier != tier
        if tier_changed:
            self._state.tier_changed_at = self._state.last_updated
            log.info(
                f"Profit cushion tier changed: {old_tier} → {tier} "
                f"(profit: {self._state.cycle_profit_pct:.1f}%, "
                f"multiplier: {multiplier:.2f}x)"
            )

        if self.config.auto_save:
            self._save_state()

        return tier_changed

    def get_risk_multiplier(self) -> float:
        """Get current risk multiplier for position sizing.

        Returns:
            Risk multiplier (0.1-1.0) to apply to base position size
        """
        if self._state is None:
            log.warning("Profit cushion not initialized, returning 1.0x multiplier")
            return 1.0

        return self._state.risk_multiplier

    def get_tier(self) -> str:
        """Get current profit cushion tier.

        Returns:
            One of: "normal", "conservative", "selective", "turtle", "scaling_lock"
        """
        if self._state is None:
            return "normal"

        return self._state.tier

    def get_cycle_status(self) -> dict[str, Any]:
        """Get comprehensive cycle status information.

        Returns:
            Dictionary with cycle progress, profit, tier, etc.
        """
        if self._state is None:
            return {"error": "Profit cushion not initialized"}

        return {
            "cycle_start": self._state.cycle_start_date.isoformat(),
            "cycle_end": self._state.cycle_end_date.isoformat(),
            "days_elapsed": self._state.days_in_cycle,
            "days_remaining": self._state.days_remaining,
            "starting_equity": self._state.starting_equity,
            "current_equity": self._state.current_equity,
            "cycle_profit_amount": self._state.cycle_profit_amount,
            "cycle_profit_pct": self._state.cycle_profit_pct,
            "tier": self._state.tier,
            "risk_multiplier": self._state.risk_multiplier,
            "is_scaling_eligible": self._state.is_scaling_eligible(self.config.scaling_threshold),
            "tier_changed_at": self._state.tier_changed_at.isoformat() if self._state.tier_changed_at else None,
            "last_updated": self._state.last_updated.isoformat() if self._state.last_updated else None,
        }

    def is_cycle_expired(self) -> bool:
        """Check if current cycle has expired and needs renewal.

        Returns:
            True if past cycle end date
        """
        if self._state is None:
            return False

        now = datetime.now(ZoneInfo(self.config.timezone))
        return now > self._state.cycle_end_date

    def start_new_cycle(self, starting_equity: float, cycle_start_date: datetime | None = None) -> None:
        """Start a new FTMO cycle.

        Args:
            starting_equity: Starting equity for new cycle
            cycle_start_date: Start date (defaults to now)
        """
        if cycle_start_date is None:
            cycle_start_date = datetime.now(ZoneInfo(self.config.timezone))

        self._initialize_new_cycle(starting_equity, cycle_start_date)

    def _save_state(self) -> None:
        """Save current state to disk."""
        if self._state is None:
            return

        try:
            state_dict = {
                "cycle_start_date": self._state.cycle_start_date.isoformat(),
                "cycle_end_date": self._state.cycle_end_date.isoformat(),
                "starting_equity": self._state.starting_equity,
                "current_equity": self._state.current_equity,
                "cycle_profit_pct": self._state.cycle_profit_pct,
                "tier": self._state.tier,
                "risk_multiplier": self._state.risk_multiplier,
                "tier_changed_at": self._state.tier_changed_at.isoformat() if self._state.tier_changed_at else None,
                "last_updated": self._state.last_updated.isoformat() if self._state.last_updated else None,
            }

            with open(self.config.state_file_path, "w") as f:
                json.dump(state_dict, f, indent=2)

        except Exception as e:
            log.warning(f"Failed to save profit cushion state: {e}")

    def _load_state(self) -> ProfitCushionState | None:
        """Load state from disk, if it exists and is valid.

        Returns:
            Loaded state or None if no valid state found
        """
        try:
            with open(self.config.state_file_path) as f:
                state_dict = json.load(f)

            tz = ZoneInfo(self.config.timezone)

            state = ProfitCushionState(
                cycle_start_date=datetime.fromisoformat(state_dict["cycle_start_date"]).replace(tzinfo=tz),
                cycle_end_date=datetime.fromisoformat(state_dict["cycle_end_date"]).replace(tzinfo=tz),
                starting_equity=state_dict["starting_equity"],
                current_equity=state_dict["current_equity"],
                cycle_profit_pct=state_dict["cycle_profit_pct"],
                tier=state_dict["tier"],
                risk_multiplier=state_dict["risk_multiplier"],
                tier_changed_at=(
                    datetime.fromisoformat(state_dict["tier_changed_at"]).replace(tzinfo=tz)
                    if state_dict.get("tier_changed_at")
                    else None
                ),
                last_updated=(
                    datetime.fromisoformat(state_dict["last_updated"]).replace(tzinfo=tz)
                    if state_dict.get("last_updated")
                    else None
                ),
            )

            # Validate that state isn't corrupted/expired
            now = datetime.now(tz)
            if state.cycle_end_date < now:
                log.info(f"Loaded state is expired (ended {state.cycle_end_date}), will need new cycle")
                return None

            log.info(f"Loaded profit cushion state: tier={state.tier}, profit={state.cycle_profit_pct:.1f}%")
            return state

        except (FileNotFoundError, KeyError, ValueError, json.JSONDecodeError) as e:
            log.info(f"No valid profit cushion state found ({e}), will initialize new cycle")
            return None
