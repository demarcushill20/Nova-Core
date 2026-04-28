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
