"""Multi-timeframe candle derivation for NovaTrade.

Aggregates lower-timeframe candles into higher timeframes by grouping
candles into target periods and computing OHLCV aggregates.
"""

from __future__ import annotations

from novatrade.models import Candle

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TIMEFRAME_MINUTES: dict[str, int] = {
    "M1": 1,
    "M5": 5,
    "M15": 15,
    "M30": 30,
    "H1": 60,
    "H4": 240,
    "D1": 1440,
    "W1": 10080,
}

DERIVATION_MAP: dict[str, list[str]] = {
    "M1": ["M5", "M15", "M30", "H1"],
    "M5": ["M15", "M30", "H1"],
    "M15": ["H1", "H4"],
    "M30": ["H1", "H4"],
    "H1": ["H4", "D1"],
    "H4": ["D1", "W1"],
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _timeframe_seconds(tf: str) -> int:
    """Convert timeframe label to duration in seconds."""
    minutes = TIMEFRAME_MINUTES.get(tf)
    if minutes is None:
        raise ValueError(f"Unknown timeframe: {tf!r}. Supported: {sorted(TIMEFRAME_MINUTES.keys())}")
    return minutes * 60


def _align_timestamp(ts: float, period_seconds: int) -> float:
    """Floor-align a timestamp to the start of its period.

    For W1 (weekly), aligns to Monday 00:00 UTC (epoch 0 was a Thursday,
    so we offset by 3 days = 259200s to get Monday alignment).
    """
    if period_seconds == 10080 * 60:  # W1
        # Align to Monday 00:00 UTC
        monday_offset = 259200  # Thu->Mon = 3 days
        return float(((int(ts) - monday_offset) // period_seconds) * period_seconds + monday_offset)
    return float((int(ts) // period_seconds) * period_seconds)


def _aggregate_group(group: list[Candle], target_tf: str, symbol: str) -> Candle:
    """Aggregate a group of candles into a single higher-timeframe candle.

    OHLCV rules:
    - Open: first candle's open
    - High: max of all highs
    - Low: min of all lows
    - Close: last candle's close
    - Volume: sum of all volumes
    - Timestamp: first candle's timestamp (period start)
    """
    return Candle(
        timestamp=group[0].timestamp,
        open=group[0].open,
        high=max(c.high for c in group),
        low=min(c.low for c in group),
        close=group[-1].close,
        volume=round(sum(c.volume for c in group), 2),
        symbol=symbol,
        timeframe=target_tf,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def derive_timeframe(candles: list[Candle], target_tf: str) -> list[Candle]:
    """Derive a higher timeframe from lower-timeframe candles.

    Groups source candles by the target timeframe period, then aggregates
    each group into a single candle using standard OHLCV aggregation.

    Args:
        candles: Source candles (must be sorted by timestamp).
        target_tf: Target timeframe label (e.g. "H4", "D1").

    Returns:
        List of aggregated candles at the target timeframe, sorted by
        timestamp.

    Raises:
        ValueError: If target_tf is unknown or not derivable from source.
        ValueError: If candles list is empty.
    """
    if not candles:
        raise ValueError("Cannot derive timeframe from empty candle list")

    if target_tf not in TIMEFRAME_MINUTES:
        raise ValueError(f"Unknown target timeframe: {target_tf!r}. Supported: {sorted(TIMEFRAME_MINUTES.keys())}")

    # Validate derivation is valid (target must be higher than source)
    source_tf = candles[0].timeframe
    if source_tf and source_tf in TIMEFRAME_MINUTES:
        source_minutes = TIMEFRAME_MINUTES[source_tf]
        target_minutes = TIMEFRAME_MINUTES[target_tf]
        if target_minutes <= source_minutes:
            raise ValueError(
                f"Cannot derive {target_tf} ({target_minutes}min) from "
                f"{source_tf} ({source_minutes}min) — target must be higher"
            )
        # Check derivation map if source is known
        if source_tf in DERIVATION_MAP and target_tf not in DERIVATION_MAP[source_tf]:
            raise ValueError(
                f"Derivation from {source_tf} to {target_tf} is not supported. "
                f"Valid targets for {source_tf}: {DERIVATION_MAP[source_tf]}"
            )

    target_seconds = _timeframe_seconds(target_tf)
    symbol = candles[0].symbol

    # Ensure candles are sorted
    sorted_candles = sorted(candles, key=lambda c: c.timestamp)

    # Group candles by target period
    groups: dict[float, list[Candle]] = {}
    for c in sorted_candles:
        period_start = _align_timestamp(c.timestamp, target_seconds)
        if period_start not in groups:
            groups[period_start] = []
        groups[period_start].append(c)

    # Aggregate each group
    result: list[Candle] = []
    for period_start in sorted(groups.keys()):
        group = groups[period_start]
        result.append(_aggregate_group(group, target_tf, symbol))

    return result
