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

import logging

log = logging.getLogger("novatrade.risk.position_sizer")


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
    ) -> None:
        self._min_lot = min_lot
        self._max_lot = max_lot

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

    The HardRiskSupervisor provides the final safety cap on position sizes.
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
