"""FTMO-compliant position sizer for NovaTrade live trading.

Mirrors the Pine f_qty() model: 0.75% equity risk per trade, clamped to
FTMO-safe lot-size bounds (max 10.00 lots for $100K accounts).  Provides both calculation and cross-check
validation for TradingView alert volumes.

Usage::

    sizer = PositionSizer()
    lot = sizer.calculate(equity=100000, entry=1.10150, stop=1.10000,
                          risk_pct=0.0075, pip_value=0.0001,
                          pip_value_per_lot=10.0)
    # lot = 5.00

    ok, reason = sizer.validate(requested=0.22, calculated=0.20, tolerance=0.10)
    # ok = True (within 10% tolerance)
"""

from __future__ import annotations

import logging
import random
from typing import ClassVar

log = logging.getLogger("novatrade.risk.position_sizer")


class PositionSizer:
    """Calculate and validate lot sizes for FTMO-compliant trading.

    Matches the backtest engine's position sizing formula:
        volume = (equity * risk_pct) / (stop_distance_pips * pip_value_per_lot)
        volume = clamp(volume, min_lot, max_lot), rounded to 2 decimals
    """

    def __init__(
        self,
        min_lot: float = 0.01,
        max_lot: float = 10.00,
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
        risk_pct: float = 0.0075,
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
        if volume > 10.0:
            log.warning(
                "Calculated volume %.2f exceeds 10.0 lots — capping. equity=%.0f risk_pct=%.4f stop_pips=%.1f",
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
            return False, f"requested volume is {requested} (non-positive)"

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

    4-tier drawdown scaling (as % of daily DD limit used):
        0-50%  → 100% size
        50-70% → 75% size
        70-85% → 50% size
        85%+   → 25% size (survival mode)

    Consecutive-loss scaling:
        0-1 losses → 100% size
        2 losses   → 75% size
        3 losses   → 50% size
        4+ losses  → 25% size (until a winner)

    The final scale factor is the minimum of both — the more restrictive wins.
    """

    # Drawdown tiers: (threshold_pct, scale_factor)
    DD_TIERS: ClassVar[list[tuple[float, float]]] = [
        (0.85, 0.25),  # ≥85% of DD limit used → survival mode
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

    def __init__(self) -> None:
        self._consecutive_losses: int = 0

    @property
    def consecutive_losses(self) -> int:
        return self._consecutive_losses

    def record_loss(self) -> None:
        """Record a losing trade."""
        self._consecutive_losses += 1

    def record_win(self) -> None:
        """Record a winning trade — resets the consecutive loss counter."""
        self._consecutive_losses = 0

    def reset(self) -> None:
        """Reset state (e.g., for a new trading day)."""
        self._consecutive_losses = 0

    def drawdown_scale(self, dd_used_pct: float) -> float:
        """Return the scale factor based on how much of the daily DD limit is used.

        Args:
            dd_used_pct: Fraction of daily drawdown limit consumed (0.0–1.0+).
                         E.g., 0.6 means 60% of the daily loss limit has been used.

        Returns:
            Scale factor in [0.25, 1.0].
        """
        dd_used_pct = max(0.0, dd_used_pct)
        for threshold, scale in self.DD_TIERS:
            if dd_used_pct >= threshold:
                return scale
        return 1.0  # unreachable but safe

    def loss_streak_scale(self) -> float:
        """Return the scale factor based on consecutive losses.

        Returns:
            Scale factor in [0.25, 1.0].
        """
        for threshold, scale in self.LOSS_TIERS:
            if self._consecutive_losses >= threshold:
                return scale
        return 1.0  # unreachable but safe

    def combined_scale(self, dd_used_pct: float) -> float:
        """Return the most restrictive scale factor from both drawdown and loss streak.

        Args:
            dd_used_pct: Fraction of daily drawdown limit consumed (0.0–1.0+).

        Returns:
            Minimum of drawdown_scale and loss_streak_scale.
        """
        return min(self.drawdown_scale(dd_used_pct), self.loss_streak_scale())

    def scale_volume(self, base_volume: float, dd_used_pct: float, min_lot: float = 0.01) -> float:
        """Apply combined scaling to a base volume.

        Args:
            base_volume: The unscaled lot size from PositionSizer.calculate().
            dd_used_pct: Fraction of daily drawdown limit consumed.
            min_lot: Minimum lot size floor.

        Returns:
            Scaled volume, never below min_lot, rounded to 2 decimals.
        """
        scale = self.combined_scale(dd_used_pct)
        scaled = round(base_volume * scale, 2)
        return max(min_lot, scaled)
