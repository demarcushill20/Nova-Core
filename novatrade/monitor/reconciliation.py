"""Position reconciliation for NovaTrade.

Compares expected positions (from local PositionTracker) against actual
broker positions (from the adapter).  Returns structured mismatches.

Provider-neutral — only uses NovaTrade-native Position models.
"""

from __future__ import annotations

import logging

from novatrade.adapter.base import MT5Adapter
from novatrade.models import (
    MismatchType,
    Position,
    PositionMismatch,
    ReconciliationResult,
)
from novatrade.monitor.position_tracker import PositionTracker

log = logging.getLogger("novatrade.monitor.reconciliation")

# Volume tolerance for floating-point comparison (0.001 lots).
_VOLUME_TOLERANCE = 0.001

# Price tolerance for SL/TP comparison (points).
_PRICE_TOLERANCE = 0.00001


async def reconcile(
    adapter: MT5Adapter,
    tracker: PositionTracker,
) -> ReconciliationResult:
    """Compare expected positions against actual broker state.

    Returns a ReconciliationResult with any mismatches found.
    Never raises — returns error info in the result on failure.
    """
    expected = tracker.expected_positions

    try:
        actual = await adapter.get_positions()
    except Exception as exc:
        log.error("reconciliation: get_positions failed: %s", exc)
        return ReconciliationResult(
            ok=False,
            mismatches=[
                PositionMismatch(
                    mismatch_type=MismatchType.UNEXPECTED,
                    symbol="",
                    detail=f"could not fetch broker positions: {exc}",
                )
            ],
            expected_count=len(expected),
            actual_count=0,
        )

    mismatches = compare_positions(expected, actual)

    ok = len(mismatches) == 0
    if ok:
        log.info(
            "reconciliation OK: expected=%d actual=%d",
            len(expected),
            len(actual),
        )
    else:
        log.warning(
            "reconciliation MISMATCH: %d issue(s) — expected=%d actual=%d",
            len(mismatches),
            len(expected),
            len(actual),
        )
        for m in mismatches:
            log.warning("  %s %s: %s", m.mismatch_type.value, m.symbol, m.detail)

    return ReconciliationResult(
        ok=ok,
        mismatches=mismatches,
        expected_count=len(expected),
        actual_count=len(actual),
    )


def compare_positions(
    expected: list[Position],
    actual: list[Position],
) -> list[PositionMismatch]:
    """Pure comparison logic — no I/O, fully testable.

    Matching strategy: match by symbol+side first (since position IDs may
    differ between NovaTrade's order_id and the broker's position_id in
    some providers).  Each expected position tries to find a matching
    actual position.
    """
    mismatches: list[PositionMismatch] = []

    # Build a mutable pool of actual positions keyed by (symbol, side).
    actual_pool: dict[tuple[str, str], list[Position]] = {}
    for p in actual:
        key = (p.symbol, p.side.value)
        actual_pool.setdefault(key, []).append(p)

    matched_actual_ids: set[str] = set()

    # -- Check each expected position against actuals -------------------------
    for exp in expected:
        key = (exp.symbol, exp.side.value)
        candidates = actual_pool.get(key, [])

        # Find best match by volume proximity.
        match: Position | None = None
        for cand in candidates:
            if cand.position_id in matched_actual_ids:
                continue
            if match is None or abs(cand.volume - exp.volume) < abs(match.volume - exp.volume):
                match = cand

        if match is None:
            mismatches.append(
                PositionMismatch(
                    mismatch_type=MismatchType.MISSING,
                    symbol=exp.symbol,
                    detail=f"expected {exp.side.value} {exp.symbol} vol={exp.volume} — not found at broker",
                    expected_value=f"{exp.side.value} vol={exp.volume}",
                    position_id=exp.position_id,
                )
            )
            continue

        matched_actual_ids.add(match.position_id)

        # Volume mismatch.
        if abs(match.volume - exp.volume) > _VOLUME_TOLERANCE:
            mismatches.append(
                PositionMismatch(
                    mismatch_type=MismatchType.VOLUME,
                    symbol=exp.symbol,
                    detail=f"volume mismatch on {exp.symbol}",
                    expected_value=str(exp.volume),
                    actual_value=str(match.volume),
                    position_id=match.position_id,
                )
            )

        # SL mismatch.
        if (
            exp.stop_loss is not None
            and match.stop_loss is not None
            and abs(match.stop_loss - exp.stop_loss) > _PRICE_TOLERANCE
        ):
            mismatches.append(
                PositionMismatch(
                    mismatch_type=MismatchType.STOP_LOSS,
                    symbol=exp.symbol,
                    detail=f"SL mismatch on {exp.symbol}",
                    expected_value=str(exp.stop_loss),
                    actual_value=str(match.stop_loss),
                    position_id=match.position_id,
                )
            )

        # TP mismatch.
        if (
            exp.take_profit is not None
            and match.take_profit is not None
            and abs(match.take_profit - exp.take_profit) > _PRICE_TOLERANCE
        ):
            mismatches.append(
                PositionMismatch(
                    mismatch_type=MismatchType.TAKE_PROFIT,
                    symbol=exp.symbol,
                    detail=f"TP mismatch on {exp.symbol}",
                    expected_value=str(exp.take_profit),
                    actual_value=str(match.take_profit),
                    position_id=match.position_id,
                )
            )

    # -- Check for unexpected broker positions --------------------------------
    for p in actual:
        if p.position_id not in matched_actual_ids:
            mismatches.append(
                PositionMismatch(
                    mismatch_type=MismatchType.UNEXPECTED,
                    symbol=p.symbol,
                    detail=f"unexpected {p.side.value} {p.symbol} vol={p.volume} at broker",
                    actual_value=f"{p.side.value} vol={p.volume}",
                    position_id=p.position_id,
                )
            )

    return mismatches
