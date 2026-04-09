"""FTMO-compliant position sizer for NovaTrade live trading.

Mirrors the Pine f_qty() model: 1.5% equity risk per trade, clamped to
lot-size bounds (max 50.00 lots for $100K accounts).  Provides both calculation and cross-check
validation for TradingView alert volumes.

Usage::

    sizer = PositionSizer()
    lot = sizer.calculate(equity=100000, entry=1.10150, stop=1.10000,
                          risk_pct=0.015, pip_value=0.0001,
                          pip_value_per_lot=10.0)
    # lot = 10.00

    ok, reason = sizer.validate(requested=0.22, calculated=0.20, tolerance=0.10)
    # ok = True (within 10% tolerance)
"""

from __future__ import annotations

import json
import logging
import os
import random
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import ClassVar

log = logging.getLogger("novatrade.risk.position_sizer")


def _is_test_environment() -> bool:
    """Check if we're running in a test environment."""
    return (
        "pytest" in sys.modules or "PYTEST_CURRENT_TEST" in os.environ or any("test" in arg.lower() for arg in sys.argv)
    )


# ---------------------------------------------------------------------------
# Drawdown-proportional risk tiers (Zeno's Paradox approach)
# ---------------------------------------------------------------------------

# Tier multipliers: (max_drawdown_pct, risk_multiplier)
# As total drawdown deepens, the base risk_pct is scaled down.
# This creates exponential safety: the closer to breach, the harder to breach.
_DD_RISK_TIERS: list[tuple[float, float]] = [
    (0.02, 1.00),  # 0-2% drawdown → 100% of base risk (normal)
    (0.04, 0.70),  # 2-4% drawdown →  70% of base risk (cautious)
    (0.06, 0.50),  # 4-6% drawdown →  50% of base risk (defensive)
    (0.08, 0.30),  # 6-8% drawdown →  30% of base risk (survival)
    (0.10, 0.20),  # 8-10% drawdown → 20% of base risk (emergency)
]

_TIER_NAMES: list[tuple[float, str]] = [
    (0.10, "halt"),
    (0.08, "emergency"),
    (0.06, "survival"),
    (0.04, "defensive"),
    (0.02, "cautious"),
    (0.00, "normal"),
]


class PositionSizer:
    """Calculate and validate lot sizes for FTMO-compliant trading.

    Matches the backtest engine's position sizing formula:
        volume = (equity * risk_pct) / (stop_distance_pips * pip_value_per_lot)
        volume = clamp(volume, min_lot, max_lot), rounded to 2 decimals
    """

    def __init__(
        self,
        min_lot: float = 0.01,
        max_lot: float = 50.00,
        *,
        micro_variation_enabled: bool = False,
        micro_variation_step: float = 0.01,
    ) -> None:
        self._min_lot = min_lot
        self._max_lot = max_lot
        self._micro_variation_enabled = micro_variation_enabled
        self._micro_variation_step = micro_variation_step

    def calculate(
        self,
        equity: float,
        entry: float,
        stop: float,
        risk_pct: float = 0.015,
        pip_value: float = 0.0001,
        pip_value_per_lot: float = 10.0,
    ) -> float:
        """Calculate lot size using 1% equity risk model.

        Args:
            equity: Current account equity in USD.
            entry: Entry price.
            stop: Stop-loss price.
            risk_pct: Fraction of equity to risk (0.01 = 1%).
            pip_value: Price increment per pip (0.0001 for 5-digit forex).
            pip_value_per_lot: Dollar value per pip per standard lot ($10 for EURUSD).

        Returns:
            Lot size clamped to [min_lot, max_lot], rounded to 2 decimals.

        Raises:
            ValueError: If equity <= 0, risk_pct <= 0, or entry == stop.
        """
        if equity <= 0:
            raise ValueError(f"equity must be positive, got {equity}")
        if risk_pct <= 0:
            raise ValueError(f"risk_pct must be positive, got {risk_pct}")
        if pip_value <= 0:
            raise ValueError(f"pip_value must be positive, got {pip_value}")
        if pip_value_per_lot <= 0:
            raise ValueError(f"pip_value_per_lot must be positive, got {pip_value_per_lot}")

        stop_distance = abs(entry - stop)
        if stop_distance == 0:
            raise ValueError("entry and stop must differ")

        stop_distance_pips = stop_distance / pip_value
        risk_dollars = equity * risk_pct
        volume = risk_dollars / (stop_distance_pips * pip_value_per_lot)

        # Guard: warn if calculated volume exceeds safety ceiling
        if volume > 50.0:
            log.warning(
                "Calculated volume %.2f exceeds 50.0 lots — capping. equity=%.0f risk_pct=%.4f stop_pips=%.1f",
                volume,
                equity,
                risk_pct,
                stop_distance_pips,
            )

        # Clamp and round
        volume = max(self._min_lot, min(self._max_lot, round(volume, 2)))

        # Anti-EA-detection: add micro-variation so consecutive trades don't
        # use identical lot sizes (a strong EA fingerprint for FTMO detection).
        if self._micro_variation_enabled and self._micro_variation_step > 0:
            offset = random.choice([-1, 0, 1]) * self._micro_variation_step  # noqa: S311
            volume = round(volume + offset, 2)
            volume = max(self._min_lot, min(self._max_lot, volume))

        return volume

    def validate(
        self,
        requested: float,
        calculated: float,
        tolerance: float = 0.10,
    ) -> tuple[bool, str]:
        """Cross-check a requested volume against the calculated volume.

        Only rejects when the requested volume EXCEEDS the calculated volume
        by more than ``tolerance``.  Under-sizing (using less risk) is always
        considered safe and passes the check.

        Args:
            requested: Volume from TradingView alert or external source.
            calculated: Volume from calculate().
            tolerance: Maximum allowed over-size relative deviation (0.10 = 10%).

        Returns:
            (ok, reason) tuple. ok=True if within tolerance or under-sized.
        """
        if calculated <= 0:
            return False, f"calculated volume is {calculated} (non-positive)"
        if requested <= 0:
            return True, f"volume auto-sized: using calculated={calculated:.2f}"

        # Under-sizing is always safe (conservative risk)
        if requested <= calculated:
            return True, (f"volume OK: requested={requested:.2f} <= calculated={calculated:.2f} (conservative)")

        over_ratio = (requested - calculated) / calculated
        if over_ratio > tolerance:
            return False, (
                f"volume over-sized: requested={requested:.2f}, "
                f"calculated={calculated:.2f}, over by {over_ratio:.1%} > tolerance={tolerance:.0%}"
            )

        return True, (f"volume OK: requested={requested:.2f}, calculated={calculated:.2f}, over by {over_ratio:.1%}")

    @property
    def min_lot(self) -> float:
        return self._min_lot

    @property
    def max_lot(self) -> float:
        return self._max_lot


class DrawdownScaler:
    """Dynamically reduce position size based on drawdown proximity and loss streaks.

    4-tier drawdown scaling (as % of daily DD limit used) with hysteresis:
        Entry (tighten):  50% → 0.75, 70% → 0.50, 85% → 0.25
        Exit  (recover):  45% → 1.00, 60% → 0.75, 75% → 0.50

    Total-account DD scaling (as % of max overall loss limit):
        0-50%  → 100% size
        50-70% → 75% size
        70-85% → 50% size
        85%+   → 10% size (hard survival)

    Consecutive-loss scaling:
        0-1 losses → 100% size
        2 losses   → 75% size
        3 losses   → 50% size
        4+ losses  → 25% size

    Loss-streak recovery modes:
        instant  — first win resets to full size (default)
        gradual  — each win steps up one tier

    The final scale factor is the minimum of all active layers.
    """

    # Drawdown tiers: (entry_threshold, exit_threshold, scale_factor)
    # Enter the tier when DD rises above entry_threshold.
    # Exit the tier (recover) when DD drops below exit_threshold.
    DD_TIERS_HYSTERESIS: ClassVar[list[tuple[float, float, float]]] = [
        (0.85, 0.75, 0.25),  # ≥85% enter survival, recover below 75%
        (0.70, 0.60, 0.50),  # ≥70% enter half, recover below 60%
        (0.50, 0.45, 0.75),  # ≥50% enter 3/4, recover below 45%
    ]

    # Legacy non-hysteresis tiers (used when hysteresis is disabled)
    DD_TIERS: ClassVar[list[tuple[float, float]]] = [
        (0.85, 0.25),
        (0.70, 0.50),
        (0.50, 0.75),
        (0.00, 1.00),
    ]

    # Total-account DD tiers (10% max overall loss limit)
    TOTAL_DD_TIERS: ClassVar[list[tuple[float, float]]] = [
        (0.85, 0.10),  # ≥85% of total limit → hard survival
        (0.70, 0.50),  # ≥70% → half size
        (0.50, 0.75),  # ≥50% → three-quarter size
        (0.00, 1.00),  # <50% → full size
    ]

    # Consecutive-loss tiers: (consecutive_losses, scale_factor)
    LOSS_TIERS: ClassVar[list[tuple[int, float]]] = [
        (4, 0.25),  # 4+ consecutive losses → survival mode
        (3, 0.50),  # 3 losses → half size
        (2, 0.75),  # 2 losses → three-quarter size
        (0, 1.00),  # 0-1 losses → full size
    ]

    # Scale factors ordered for gradual recovery (worst → best)
    _LOSS_SCALE_LADDER: ClassVar[list[float]] = [0.25, 0.50, 0.75, 1.00]

    def __init__(
        self,
        *,
        hysteresis: bool = True,
        total_dd_enabled: bool = False,
        loss_recovery_mode: str = "instant",
        state_file: str | None = None,
        enable_persistence: bool | None = None,
    ) -> None:
        self._consecutive_losses: int = 0
        self._hysteresis = hysteresis
        self._total_dd_enabled = total_dd_enabled
        self._loss_recovery_mode = loss_recovery_mode  # "instant" or "gradual"
        # Track current DD tier scale for hysteresis (start at full size)
        self._current_dd_scale: float = 1.0
        # Track current loss-streak scale for gradual recovery
        self._current_loss_scale: float = 1.0
        # State persistence with 24-hour TTL (auto-disabled for testing)
        if enable_persistence is None:
            enable_persistence = not _is_test_environment()
        self._enable_persistence = enable_persistence
        self._state_file = (
            Path(state_file) if state_file else Path("/home/nova/nova-core/STATE/novatrade/drawdown_scaler.json")
        )
        if self._enable_persistence:
            self._load_state()

    @property
    def consecutive_losses(self) -> int:
        return self._consecutive_losses

    @property
    def current_dd_scale(self) -> float:
        """Current drawdown tier (reflects hysteresis state)."""
        return self._current_dd_scale

    def record_loss(self) -> None:
        """Record a losing trade."""
        self._consecutive_losses += 1
        # Update loss scale immediately on loss
        self._current_loss_scale = self._compute_loss_scale()
        self._save_state()

    def record_win(self) -> None:
        """Record a winning trade."""
        if self._loss_recovery_mode == "gradual":
            # Step up one tier in the scale ladder
            try:
                idx = self._LOSS_SCALE_LADDER.index(self._current_loss_scale)
            except ValueError:
                idx = 0
            if idx < len(self._LOSS_SCALE_LADDER) - 1:
                self._current_loss_scale = self._LOSS_SCALE_LADDER[idx + 1]
            # Reduce consecutive_losses proportionally
            if self._consecutive_losses > 0:
                self._consecutive_losses = max(0, self._consecutive_losses - 1)
        else:
            # Instant reset
            self._consecutive_losses = 0
            self._current_loss_scale = 1.0
        self._save_state()

    def _load_state(self) -> None:
        """Load state from persistent storage with 24-hour TTL."""
        try:
            if not self._state_file.exists():
                return

            with open(self._state_file) as f:
                data = json.load(f)

            # Check if state is stale (older than 24 hours)
            saved_at = datetime.fromisoformat(data.get("saved_at", "1970-01-01T00:00:00+00:00"))
            now = datetime.now(timezone.utc)
            age_hours = (now - saved_at).total_seconds() / 3600

            if age_hours > 24:
                log.info("DrawdownScaler state file aged %s hours, resetting to defaults", age_hours)
                return

            # Restore state
            self._consecutive_losses = data.get("consecutive_losses", 0)
            self._current_dd_scale = data.get("current_dd_scale", 1.0)
            self._current_loss_scale = data.get("current_loss_scale", 1.0)

            log.info(
                "DrawdownScaler state restored: consecutive_losses=%d, dd_scale=%.2f, loss_scale=%.2f (age=%.1fh)",
                self._consecutive_losses,
                self._current_dd_scale,
                self._current_loss_scale,
                age_hours,
            )

        except Exception as e:
            log.warning("Failed to load DrawdownScaler state: %s", e)

    def _save_state(self) -> None:
        """Save current state to persistent storage."""
        if not self._enable_persistence:
            return

        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)

            data = {
                "consecutive_losses": self._consecutive_losses,
                "current_dd_scale": self._current_dd_scale,
                "current_loss_scale": self._current_loss_scale,
                "saved_at": datetime.now(timezone.utc).isoformat(),
                "recovery_mode": self._loss_recovery_mode,
                "hysteresis": self._hysteresis,
            }

            with open(self._state_file, "w") as f:
                json.dump(data, f, indent=2)

        except Exception as e:
            log.warning("Failed to save DrawdownScaler state: %s", e)

    def reset(self) -> None:
        """Reset state (e.g., for a new trading day)."""
        self._consecutive_losses = 0
        self._current_dd_scale = 1.0
        self._current_loss_scale = 1.0
        self._save_state()

    def drawdown_scale(self, dd_used_pct: float) -> float:
        """Return the scale factor based on how much of the daily DD limit is used.

        With hysteresis enabled, uses asymmetric entry/exit thresholds to prevent
        rapid tier oscillation when DD hovers near a boundary.

        Args:
            dd_used_pct: Fraction of daily drawdown limit consumed (0.0–1.0+).

        Returns:
            Scale factor in [0.25, 1.0].
        """
        dd_used_pct = max(0.0, dd_used_pct)

        if not self._hysteresis:
            # Legacy mode: simple threshold matching
            for threshold, scale in self.DD_TIERS:
                if dd_used_pct >= threshold:
                    return scale
            return 1.0

        # Hysteresis mode: direction-aware tier transitions
        new_scale = 1.0  # default: full size

        for entry_thresh, exit_thresh, tier_scale in self.DD_TIERS_HYSTERESIS:
            if dd_used_pct >= entry_thresh:
                # DD is above entry threshold → enter this tier
                new_scale = tier_scale
                break
            if self._current_dd_scale <= tier_scale and dd_used_pct >= exit_thresh:
                # Already in this tier and DD hasn't dropped below exit threshold
                new_scale = tier_scale
                break

        self._current_dd_scale = new_scale
        return new_scale

    def total_dd_scale(self, total_dd_used_pct: float) -> float:
        """Return scale factor based on total-account drawdown (max overall loss).

        Args:
            total_dd_used_pct: Fraction of total DD limit consumed (0.0–1.0+).

        Returns:
            Scale factor in [0.10, 1.0].
        """
        total_dd_used_pct = max(0.0, total_dd_used_pct)
        for threshold, scale in self.TOTAL_DD_TIERS:
            if total_dd_used_pct >= threshold:
                return scale
        return 1.0

    def _compute_loss_scale(self) -> float:
        """Compute raw loss-streak scale from consecutive_losses."""
        for threshold, scale in self.LOSS_TIERS:
            if self._consecutive_losses >= threshold:
                return scale
        return 1.0

    def loss_streak_scale(self) -> float:
        """Return the scale factor based on consecutive losses.

        In gradual recovery mode, returns the tracked scale (stepped up per win).
        In instant mode, computes directly from consecutive_losses.

        Returns:
            Scale factor in [0.25, 1.0].
        """
        if self._loss_recovery_mode == "gradual":
            return self._current_loss_scale
        return self._compute_loss_scale()

    def combined_scale(
        self,
        dd_used_pct: float,
        total_dd_used_pct: float | None = None,
    ) -> float:
        """Return the most restrictive scale factor from all active layers.

        Args:
            dd_used_pct: Fraction of daily drawdown limit consumed (0.0–1.0+).
            total_dd_used_pct: Fraction of total DD limit consumed (optional).

        Returns:
            Minimum of all active scale factors.
        """
        scales = [self.drawdown_scale(dd_used_pct), self.loss_streak_scale()]
        if self._total_dd_enabled and total_dd_used_pct is not None:
            scales.append(self.total_dd_scale(total_dd_used_pct))
        return min(scales)

    def scale_volume(
        self,
        base_volume: float,
        dd_used_pct: float,
        min_lot: float = 0.01,
        total_dd_used_pct: float | None = None,
    ) -> float:
        """Apply combined scaling to a base volume.

        Args:
            base_volume: The unscaled lot size from PositionSizer.calculate().
            dd_used_pct: Fraction of daily drawdown limit consumed.
            min_lot: Minimum lot size floor.
            total_dd_used_pct: Fraction of total DD limit consumed (optional).

        Returns:
            Scaled volume, never below min_lot, rounded to 2 decimals.
        """
        scale = self.combined_scale(dd_used_pct, total_dd_used_pct)
        scaled = round(base_volume * scale, 2)
        return max(min_lot, scaled)


class DrawdownProportionalRisk:
    """Adjust base risk percentage based on total account drawdown depth.

    Implements the "Zeno's Paradox" approach from FTMO risk management research:
    as drawdown deepens from initial balance, the risk per trade is progressively
    reduced, making it exponentially harder to reach the breach level.

    Tiers (total drawdown from initial balance, as % of base risk):

        ======== ============ ========================================
        Drawdown Multiplier   Effect (with 1.5% base)
        ======== ============ ========================================
        0–2%     100%         1.50% → full risk
        2–4%      70%         1.05% → cautious
        4–6%      50%         0.75% → defensive
        6–8%      30%         0.45% → survival
        8–10%     20%         0.30% → emergency
        >10%       0%         HALT — account breached
        ======== ============ ========================================

    Combined with DrawdownScaler (which does multiplicative volume scaling),
    this creates a two-layer defence: the risk budget shrinks AND the volume
    is further scaled down by daily-DD / loss-streak factors.
    """

    def __init__(
        self,
        *,
        base_risk_pct: float = 0.015,
        enabled: bool = True,
    ) -> None:
        """
        Args:
            base_risk_pct: The baseline risk percentage (0.015 = 1.5%).
            enabled: When False, always returns base_risk_pct unchanged.
        """
        if base_risk_pct < 0:
            raise ValueError(f"base_risk_pct must be non-negative, got {base_risk_pct}")
        self._base_risk_pct = base_risk_pct
        self._enabled = enabled

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def base_risk_pct(self) -> float:
        return self._base_risk_pct

    def get_risk_pct(
        self,
        equity: float,
        initial_balance: float,
    ) -> float:
        """Calculate risk percentage based on total account drawdown depth.

        Args:
            equity: Current account equity in USD.
            initial_balance: Starting account balance (e.g., 100,000 for FTMO).

        Returns:
            Risk percentage as a decimal (e.g., 0.00525 for 0.525%).
            Returns 0.0 if drawdown exceeds 10% (halt signal).
        """
        if not self._enabled:
            return self._base_risk_pct

        if initial_balance <= 0:
            return self._base_risk_pct

        if equity >= initial_balance:
            return self._base_risk_pct

        dd_pct = (initial_balance - equity) / initial_balance

        for max_dd, multiplier in _DD_RISK_TIERS:
            if dd_pct < max_dd:
                return self._base_risk_pct * multiplier

        # Beyond 10% — halt signal
        return 0.0

    def get_tier_name(
        self,
        equity: float,
        initial_balance: float,
    ) -> str:
        """Return human-readable tier name for monitoring and logging."""
        if not self._enabled or initial_balance <= 0 or equity >= initial_balance:
            return "normal"

        dd_pct = (initial_balance - equity) / initial_balance

        for threshold, name in _TIER_NAMES:
            if dd_pct >= threshold:
                return name
        return "normal"

    def get_consecutive_losses_to_breach(
        self,
        equity: float,
        initial_balance: float,
        breach_pct: float = 0.10,
    ) -> int:
        """Estimate consecutive losses needed to breach from current equity.

        Assumes each loss equals exactly the current risk_pct of equity.
        This is a simplified model — actual losses vary by stop distance.

        Returns:
            Estimated number of consecutive max-risk losses to reach breach.
            Returns 0 if already at or beyond breach.
            Returns 999 if disabled or no drawdown.
        """
        if not self._enabled or initial_balance <= 0:
            return 999

        breach_equity = initial_balance * (1.0 - breach_pct)
        if equity <= breach_equity:
            return 0

        count = 0
        sim_equity = equity
        while sim_equity > breach_equity and count < 999:
            risk_pct = self.get_risk_pct(sim_equity, initial_balance)
            if risk_pct <= 0:
                break
            loss = sim_equity * risk_pct
            sim_equity -= loss
            count += 1

        return count
