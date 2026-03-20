"""Data quality validation and auto-fix for NovaTrade candle data.

Validates OHLCV candle integrity, detects anomalies, and provides
auto-fix capabilities for common data quality issues.
"""

from __future__ import annotations

import math
from enum import Enum

from pydantic import BaseModel, Field

from novatrade.models import Candle

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class CheckStatus(str, Enum):
    PASS = "pass"  # noqa: S105
    WARN = "warn"
    FAIL = "fail"


class QualityCheck(BaseModel):
    """Result of a single data quality check."""

    name: str
    status: CheckStatus
    message: str
    auto_fix_available: bool = False
    affected_indices: list[int] = Field(default_factory=list)


class DataQualityReport(BaseModel):
    """Aggregated data quality report across all checks."""

    checks: list[QualityCheck] = Field(default_factory=list)
    overall_status: CheckStatus = CheckStatus.PASS
    summary: str = ""
    total_candles: int = 0
    fixable_issues: int = 0

    def model_post_init(self, __context: object) -> None:
        """Compute overall_status and summary from checks."""
        if not self.checks:
            return
        if any(c.status == CheckStatus.FAIL for c in self.checks):
            self.overall_status = CheckStatus.FAIL
        elif any(c.status == CheckStatus.WARN for c in self.checks):
            self.overall_status = CheckStatus.WARN
        else:
            self.overall_status = CheckStatus.PASS

        self.fixable_issues = sum(1 for c in self.checks if c.auto_fix_available and c.status != CheckStatus.PASS)

        fail_count = sum(1 for c in self.checks if c.status == CheckStatus.FAIL)
        warn_count = sum(1 for c in self.checks if c.status == CheckStatus.WARN)
        pass_count = sum(1 for c in self.checks if c.status == CheckStatus.PASS)
        self.summary = (
            f"{len(self.checks)} checks: {pass_count} passed, "
            f"{warn_count} warnings, {fail_count} failures "
            f"({self.fixable_issues} auto-fixable) "
            f"across {self.total_candles} candles"
        )


# ---------------------------------------------------------------------------
# TIMEFRAME_SECONDS — used by gap detection
# ---------------------------------------------------------------------------

TIMEFRAME_SECONDS: dict[str, int] = {
    "M1": 60,
    "M5": 300,
    "M15": 900,
    "M30": 1800,
    "H1": 3600,
    "H4": 14400,
    "D1": 86400,
    "W1": 604800,
}


# ---------------------------------------------------------------------------
# Individual quality checks
# ---------------------------------------------------------------------------


def _check_nan(candles: list[Candle]) -> QualityCheck:
    """Detect NaN values in any OHLCV field."""
    affected: list[int] = []
    for i, c in enumerate(candles):
        values = [c.open, c.high, c.low, c.close, c.volume]
        if any(math.isnan(v) for v in values):
            affected.append(i)

    if not affected:
        return QualityCheck(
            name="nan_check",
            status=CheckStatus.PASS,
            message="No NaN values detected",
            auto_fix_available=True,
        )
    return QualityCheck(
        name="nan_check",
        status=CheckStatus.FAIL,
        message=f"NaN values found in {len(affected)} candle(s) at indices: {affected[:20]}",
        auto_fix_available=True,
        affected_indices=affected,
    )


def _check_ohlc_integrity(candles: list[Candle]) -> QualityCheck:
    """Verify high >= max(open,close) and low <= min(open,close)."""
    affected: list[int] = []
    for i, c in enumerate(candles):
        # Skip NaN candles — handled by nan_check
        values = [c.open, c.high, c.low, c.close]
        if any(math.isnan(v) for v in values):
            continue
        if c.high < max(c.open, c.close) or c.low > min(c.open, c.close):
            affected.append(i)

    if not affected:
        return QualityCheck(
            name="ohlc_integrity",
            status=CheckStatus.PASS,
            message="All candles pass OHLC integrity (high >= max(O,C), low <= min(O,C))",
        )
    return QualityCheck(
        name="ohlc_integrity",
        status=CheckStatus.FAIL,
        message=f"OHLC integrity violation in {len(affected)} candle(s) at indices: {affected[:20]}",
        affected_indices=affected,
    )


def _check_timestamp_monotonicity(candles: list[Candle]) -> QualityCheck:
    """Verify timestamps are strictly increasing."""
    affected: list[int] = []
    for i in range(1, len(candles)):
        if candles[i].timestamp <= candles[i - 1].timestamp:
            affected.append(i)

    if not affected:
        return QualityCheck(
            name="timestamp_monotonicity",
            status=CheckStatus.PASS,
            message="Timestamps are strictly increasing",
            auto_fix_available=True,
        )
    return QualityCheck(
        name="timestamp_monotonicity",
        status=CheckStatus.FAIL,
        message=f"Non-monotonic timestamps at {len(affected)} position(s): {affected[:20]}",
        auto_fix_available=True,
        affected_indices=affected,
    )


def _check_duplicates(candles: list[Candle]) -> QualityCheck:
    """Detect duplicate timestamps."""
    seen: dict[float, int] = {}
    affected: list[int] = []
    for i, c in enumerate(candles):
        if c.timestamp in seen:
            affected.append(i)
        else:
            seen[c.timestamp] = i

    if not affected:
        return QualityCheck(
            name="duplicate_check",
            status=CheckStatus.PASS,
            message="No duplicate timestamps",
            auto_fix_available=True,
        )
    return QualityCheck(
        name="duplicate_check",
        status=CheckStatus.FAIL,
        message=f"Duplicate timestamps at {len(affected)} position(s): {affected[:20]}",
        auto_fix_available=True,
        affected_indices=affected,
    )


def _infer_interval(candles: list[Candle]) -> float:
    """Infer the expected candle interval from the most common delta."""
    if len(candles) < 2:
        return 0.0
    deltas: list[float] = []
    for i in range(1, min(len(candles), 200)):
        d = candles[i].timestamp - candles[i - 1].timestamp
        if d > 0:
            deltas.append(d)
    if not deltas:
        return 0.0
    # Use the median as the expected interval (robust to outliers)
    deltas.sort()
    return deltas[len(deltas) // 2]


def _check_gap_detection(candles: list[Candle]) -> QualityCheck:
    """Detect gaps larger than 2x the expected candle interval."""
    if len(candles) < 2:
        return QualityCheck(
            name="gap_detection",
            status=CheckStatus.PASS,
            message="Not enough candles for gap detection",
        )

    # Use timeframe hint if available, otherwise infer
    expected = 0.0
    if candles[0].timeframe and candles[0].timeframe in TIMEFRAME_SECONDS:
        expected = float(TIMEFRAME_SECONDS[candles[0].timeframe])
    if expected == 0.0:
        expected = _infer_interval(candles)
    if expected == 0.0:
        return QualityCheck(
            name="gap_detection",
            status=CheckStatus.WARN,
            message="Could not determine expected interval for gap detection",
        )

    threshold = expected * 2.0
    affected: list[int] = []
    for i in range(1, len(candles)):
        delta = candles[i].timestamp - candles[i - 1].timestamp
        if delta > threshold:
            affected.append(i)

    if not affected:
        return QualityCheck(
            name="gap_detection",
            status=CheckStatus.PASS,
            message=f"No gaps exceeding 2x interval ({expected}s)",
        )
    return QualityCheck(
        name="gap_detection",
        status=CheckStatus.WARN,
        message=(f"{len(affected)} gap(s) exceeding 2x interval ({expected}s) at indices: {affected[:20]}"),
        affected_indices=affected,
    )


def _check_volume_sanity(candles: list[Candle]) -> QualityCheck:
    """Detect negative volume values."""
    affected: list[int] = []
    for i, c in enumerate(candles):
        if not math.isnan(c.volume) and c.volume < 0:
            affected.append(i)

    if not affected:
        return QualityCheck(
            name="volume_sanity",
            status=CheckStatus.PASS,
            message="No negative volumes detected",
        )
    return QualityCheck(
        name="volume_sanity",
        status=CheckStatus.FAIL,
        message=f"Negative volume in {len(affected)} candle(s) at indices: {affected[:20]}",
        affected_indices=affected,
    )


def _check_timezone_consistency(candles: list[Candle]) -> QualityCheck:
    """Check that all H1 timestamps are whole-hour aligned (mod 3600 == 0)."""
    # Only relevant for H1 timeframe
    if not candles:
        return QualityCheck(
            name="timezone_consistency",
            status=CheckStatus.PASS,
            message="No candles to check",
        )

    tf = candles[0].timeframe
    if tf and tf != "H1":
        return QualityCheck(
            name="timezone_consistency",
            status=CheckStatus.PASS,
            message=f"Timezone alignment check skipped for {tf} (only applies to H1)",
        )

    affected: list[int] = []
    for i, c in enumerate(candles):
        if c.timestamp % 3600 != 0:
            affected.append(i)

    if not affected:
        return QualityCheck(
            name="timezone_consistency",
            status=CheckStatus.PASS,
            message="All timestamps are whole-hour aligned",
        )
    return QualityCheck(
        name="timezone_consistency",
        status=CheckStatus.WARN,
        message=(f"{len(affected)} timestamp(s) not whole-hour aligned at indices: {affected[:20]}"),
        affected_indices=affected,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_candles(candles: list[Candle]) -> DataQualityReport:
    """Run all quality checks on a list of candles.

    Args:
        candles: List of Candle objects to validate.

    Returns:
        DataQualityReport with individual check results and overall status.
    """
    if not candles:
        return DataQualityReport(
            checks=[],
            overall_status=CheckStatus.PASS,
            summary="No candles to validate",
            total_candles=0,
        )

    checks = [
        _check_nan(candles),
        _check_ohlc_integrity(candles),
        _check_timestamp_monotonicity(candles),
        _check_duplicates(candles),
        _check_gap_detection(candles),
        _check_volume_sanity(candles),
        _check_timezone_consistency(candles),
    ]

    return DataQualityReport(checks=checks, total_candles=len(candles))


def auto_fix_candles(candles: list[Candle], report: DataQualityReport) -> list[Candle]:
    """Apply automatic fixes for issues flagged in the report.

    Fixes applied (in order):
    1. Remove candles with NaN values in OHLCV fields.
    2. Remove duplicate timestamps (keep first occurrence).
    3. Sort by timestamp to restore monotonicity.

    Args:
        candles: Original candle list.
        report: Quality report from validate_candles().

    Returns:
        New list of candles with fixable issues resolved.
    """
    if not candles:
        return []

    # Build set of indices to remove
    remove_indices: set[int] = set()

    for check in report.checks:
        if not check.auto_fix_available or check.status == CheckStatus.PASS:
            continue

        if check.name == "nan_check" or check.name == "duplicate_check":
            remove_indices.update(check.affected_indices)

    # Filter out bad candles
    fixed = [c for i, c in enumerate(candles) if i not in remove_indices]

    # Sort by timestamp to fix monotonicity
    fixed.sort(key=lambda c: c.timestamp)

    return fixed
