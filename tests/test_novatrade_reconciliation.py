"""Tests for novatrade.monitor.reconciliation and position_tracker."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from novatrade.models import (
    ExecutionOutcome,
    ExecutionResult,
    MismatchType,
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    PositionMismatch,
    ReconciliationResult,
    RiskDecision,
    RiskVerdict,
)
from novatrade.monitor.position_tracker import PositionTracker
from novatrade.monitor.reconciliation import compare_positions, reconcile

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pos(
    pid: str = "1",
    symbol: str = "EURUSD",
    side: OrderSide = OrderSide.BUY,
    volume: float = 0.10,
    stop_loss: float | None = None,
    take_profit: float | None = None,
) -> Position:
    return Position(
        position_id=pid,
        symbol=symbol,
        side=side,
        volume=volume,
        open_price=1.08000,
        stop_loss=stop_loss,
        take_profit=take_profit,
    )


def _filled_result(
    order_id: str = "ORD-1",
    symbol: str = "EURUSD",
    side: OrderSide = OrderSide.BUY,
    volume: float = 0.10,
    stop_loss: float | None = None,
    take_profit: float | None = None,
) -> ExecutionResult:
    return ExecutionResult(
        outcome=ExecutionOutcome.FILLED,
        risk_decision=RiskDecision(
            verdict=RiskVerdict.ALLOW,
            request=OrderRequest(
                symbol=symbol,
                side=side,
                order_type=OrderType.MARKET,
                volume=volume,
                stop_loss=stop_loss,
                take_profit=take_profit,
            ),
        ),
        order_result=OrderResult(
            ok=True,
            order_id=order_id,
            status=OrderStatus.FILLED,
            fill_price=1.08000,
        ),
    )


# ===========================================================================
# PositionTracker tests
# ===========================================================================


class TestPositionTracker:
    def test_record_filled(self):
        tracker = PositionTracker()
        tracker.record(_filled_result("ORD-1", "EURUSD"))
        assert tracker.count == 1
        positions = tracker.expected_positions
        assert len(positions) == 1
        assert positions[0].position_id == "ORD-1"
        assert positions[0].symbol == "EURUSD"

    def test_record_non_filled_ignored(self):
        tracker = PositionTracker()
        result = ExecutionResult(outcome=ExecutionOutcome.DENIED)
        tracker.record(result)
        assert tracker.count == 0

    def test_record_no_order_result_ignored(self):
        tracker = PositionTracker()
        result = ExecutionResult(
            outcome=ExecutionOutcome.FILLED,
            risk_decision=RiskDecision(verdict=RiskVerdict.ALLOW),
        )
        tracker.record(result)
        assert tracker.count == 0

    def test_record_no_request_ignored(self):
        tracker = PositionTracker()
        result = ExecutionResult(
            outcome=ExecutionOutcome.FILLED,
            risk_decision=RiskDecision(verdict=RiskVerdict.ALLOW, request=None),
            order_result=OrderResult(ok=True, order_id="ORD-1"),
        )
        tracker.record(result)
        assert tracker.count == 0

    def test_record_empty_order_id_ignored(self):
        tracker = PositionTracker()
        result = _filled_result(order_id="")
        tracker.record(result)
        assert tracker.count == 0

    def test_remove(self):
        tracker = PositionTracker()
        tracker.record(_filled_result("ORD-1"))
        tracker.remove("ORD-1")
        assert tracker.count == 0

    def test_remove_nonexistent_noop(self):
        tracker = PositionTracker()
        tracker.remove("NONEXIST")  # should not raise
        assert tracker.count == 0

    def test_clear(self):
        tracker = PositionTracker()
        tracker.record(_filled_result("ORD-1"))
        tracker.record(_filled_result("ORD-2", "GBPUSD"))
        tracker.clear()
        assert tracker.count == 0

    def test_multiple_positions(self):
        tracker = PositionTracker()
        tracker.record(_filled_result("ORD-1", "EURUSD"))
        tracker.record(_filled_result("ORD-2", "GBPUSD"))
        tracker.record(_filled_result("ORD-3", "USDJPY", OrderSide.SELL))
        assert tracker.count == 3

    def test_overwrite_same_order_id(self):
        tracker = PositionTracker()
        tracker.record(_filled_result("ORD-1", "EURUSD", volume=0.10))
        tracker.record(_filled_result("ORD-1", "EURUSD", volume=0.20))
        assert tracker.count == 1
        assert tracker.expected_positions[0].volume == 0.20

    def test_stop_loss_take_profit_recorded(self):
        tracker = PositionTracker()
        tracker.record(_filled_result("ORD-1", stop_loss=1.0700, take_profit=1.0900))
        pos = tracker.expected_positions[0]
        assert pos.stop_loss == 1.0700
        assert pos.take_profit == 1.0900


# ===========================================================================
# compare_positions tests
# ===========================================================================


class TestComparePositions:
    def test_identical_positions_no_mismatches(self):
        expected = [_pos("1", "EURUSD", OrderSide.BUY, 0.10)]
        actual = [_pos("A", "EURUSD", OrderSide.BUY, 0.10)]
        mismatches = compare_positions(expected, actual)
        assert len(mismatches) == 0

    def test_missing_position(self):
        expected = [_pos("1", "EURUSD", OrderSide.BUY, 0.10)]
        actual = []
        mismatches = compare_positions(expected, actual)
        assert len(mismatches) == 1
        assert mismatches[0].mismatch_type == MismatchType.MISSING
        assert mismatches[0].symbol == "EURUSD"

    def test_unexpected_position(self):
        expected = []
        actual = [_pos("A", "EURUSD", OrderSide.BUY, 0.10)]
        mismatches = compare_positions(expected, actual)
        assert len(mismatches) == 1
        assert mismatches[0].mismatch_type == MismatchType.UNEXPECTED
        assert mismatches[0].symbol == "EURUSD"

    def test_volume_mismatch(self):
        expected = [_pos("1", "EURUSD", OrderSide.BUY, 0.10)]
        actual = [_pos("A", "EURUSD", OrderSide.BUY, 0.20)]
        mismatches = compare_positions(expected, actual)
        assert len(mismatches) == 1
        assert mismatches[0].mismatch_type == MismatchType.VOLUME
        assert mismatches[0].expected_value == "0.1"
        assert mismatches[0].actual_value == "0.2"

    def test_volume_within_tolerance(self):
        expected = [_pos("1", "EURUSD", OrderSide.BUY, 0.100)]
        actual = [_pos("A", "EURUSD", OrderSide.BUY, 0.1005)]
        mismatches = compare_positions(expected, actual)
        assert len(mismatches) == 0

    def test_stop_loss_mismatch(self):
        expected = [_pos("1", "EURUSD", stop_loss=1.07000)]
        actual = [_pos("A", "EURUSD", stop_loss=1.06500)]
        mismatches = compare_positions(expected, actual)
        assert any(m.mismatch_type == MismatchType.STOP_LOSS for m in mismatches)

    def test_take_profit_mismatch(self):
        expected = [_pos("1", "EURUSD", take_profit=1.09000)]
        actual = [_pos("A", "EURUSD", take_profit=1.09500)]
        mismatches = compare_positions(expected, actual)
        assert any(m.mismatch_type == MismatchType.TAKE_PROFIT for m in mismatches)

    def test_sl_tp_within_tolerance(self):
        expected = [_pos("1", "EURUSD", stop_loss=1.07000, take_profit=1.09000)]
        actual = [_pos("A", "EURUSD", stop_loss=1.07000, take_profit=1.09000)]
        mismatches = compare_positions(expected, actual)
        assert len(mismatches) == 0

    def test_sl_none_skipped(self):
        """If expected or actual SL is None, no SL mismatch is raised."""
        expected = [_pos("1", "EURUSD", stop_loss=1.07000)]
        actual = [_pos("A", "EURUSD", stop_loss=None)]
        mismatches = compare_positions(expected, actual)
        # Should not report SL mismatch when one side is None
        assert not any(m.mismatch_type == MismatchType.STOP_LOSS for m in mismatches)

    def test_tp_none_skipped(self):
        expected = [_pos("1", "EURUSD", take_profit=None)]
        actual = [_pos("A", "EURUSD", take_profit=1.09000)]
        mismatches = compare_positions(expected, actual)
        assert not any(m.mismatch_type == MismatchType.TAKE_PROFIT for m in mismatches)

    def test_multiple_positions_matched(self):
        expected = [
            _pos("1", "EURUSD", OrderSide.BUY, 0.10),
            _pos("2", "GBPUSD", OrderSide.SELL, 0.05),
        ]
        actual = [
            _pos("A", "EURUSD", OrderSide.BUY, 0.10),
            _pos("B", "GBPUSD", OrderSide.SELL, 0.05),
        ]
        mismatches = compare_positions(expected, actual)
        assert len(mismatches) == 0

    def test_mixed_mismatches(self):
        expected = [
            _pos("1", "EURUSD", OrderSide.BUY, 0.10),
            _pos("2", "GBPUSD", OrderSide.SELL, 0.05),
        ]
        actual = [
            _pos("A", "EURUSD", OrderSide.BUY, 0.10),
            _pos("B", "USDJPY", OrderSide.BUY, 0.02),
        ]
        mismatches = compare_positions(expected, actual)
        # GBPUSD SELL expected but missing, USDJPY BUY unexpected
        assert any(m.mismatch_type == MismatchType.MISSING for m in mismatches)
        assert any(m.mismatch_type == MismatchType.UNEXPECTED for m in mismatches)

    def test_same_symbol_different_side_not_matched(self):
        expected = [_pos("1", "EURUSD", OrderSide.BUY, 0.10)]
        actual = [_pos("A", "EURUSD", OrderSide.SELL, 0.10)]
        mismatches = compare_positions(expected, actual)
        assert len(mismatches) == 2  # MISSING + UNEXPECTED
        types = {m.mismatch_type for m in mismatches}
        assert types == {MismatchType.MISSING, MismatchType.UNEXPECTED}

    def test_best_volume_match_selection(self):
        """When multiple actuals match same key, closest volume wins."""
        expected = [_pos("1", "EURUSD", OrderSide.BUY, 0.10)]
        actual = [
            _pos("A", "EURUSD", OrderSide.BUY, 0.50),
            _pos("B", "EURUSD", OrderSide.BUY, 0.1005),  # within tolerance
        ]
        mismatches = compare_positions(expected, actual)
        # B (0.1005) is closest match — within tolerance, so no VOLUME mismatch
        # A (0.50) is unmatched → UNEXPECTED
        volume_mismatches = [m for m in mismatches if m.mismatch_type == MismatchType.VOLUME]
        unexpected = [m for m in mismatches if m.mismatch_type == MismatchType.UNEXPECTED]
        assert len(volume_mismatches) == 0
        assert len(unexpected) == 1


# ===========================================================================
# reconcile() integration tests
# ===========================================================================


class TestReconcile:
    @pytest.mark.asyncio
    async def test_reconcile_all_match(self):
        tracker = PositionTracker()
        tracker.record(_filled_result("ORD-1", "EURUSD", OrderSide.BUY, 0.10))

        adapter = MagicMock()
        adapter.get_positions = AsyncMock(
            return_value=[
                _pos("BROKER-1", "EURUSD", OrderSide.BUY, 0.10),
            ]
        )

        result = await reconcile(adapter, tracker)
        assert result.ok is True
        assert len(result.mismatches) == 0
        assert result.expected_count == 1
        assert result.actual_count == 1

    @pytest.mark.asyncio
    async def test_reconcile_missing_at_broker(self):
        tracker = PositionTracker()
        tracker.record(_filled_result("ORD-1", "EURUSD"))

        adapter = MagicMock()
        adapter.get_positions = AsyncMock(return_value=[])

        result = await reconcile(adapter, tracker)
        assert result.ok is False
        assert any(m.mismatch_type == MismatchType.MISSING for m in result.mismatches)

    @pytest.mark.asyncio
    async def test_reconcile_unexpected_at_broker(self):
        tracker = PositionTracker()  # empty — no expected positions

        adapter = MagicMock()
        adapter.get_positions = AsyncMock(
            return_value=[
                _pos("BROKER-1", "EURUSD"),
            ]
        )

        result = await reconcile(adapter, tracker)
        assert result.ok is False
        assert any(m.mismatch_type == MismatchType.UNEXPECTED for m in result.mismatches)

    @pytest.mark.asyncio
    async def test_reconcile_adapter_failure(self):
        tracker = PositionTracker()
        tracker.record(_filled_result("ORD-1", "EURUSD"))

        adapter = MagicMock()
        adapter.get_positions = AsyncMock(side_effect=RuntimeError("broker down"))

        result = await reconcile(adapter, tracker)
        assert result.ok is False
        assert result.actual_count == 0
        assert any("could not fetch" in m.detail for m in result.mismatches)

    @pytest.mark.asyncio
    async def test_reconcile_empty_both_sides(self):
        tracker = PositionTracker()

        adapter = MagicMock()
        adapter.get_positions = AsyncMock(return_value=[])

        result = await reconcile(adapter, tracker)
        assert result.ok is True
        assert len(result.mismatches) == 0


# ===========================================================================
# Model tests
# ===========================================================================


class TestReconciliationModels:
    def test_mismatch_type_values(self):
        assert MismatchType.MISSING.value == "MISSING"
        assert MismatchType.UNEXPECTED.value == "UNEXPECTED"
        assert MismatchType.VOLUME.value == "VOLUME"
        assert MismatchType.SIDE.value == "SIDE"
        assert MismatchType.STOP_LOSS.value == "STOP_LOSS"
        assert MismatchType.TAKE_PROFIT.value == "TAKE_PROFIT"

    def test_position_mismatch_defaults(self):
        m = PositionMismatch(
            mismatch_type=MismatchType.MISSING,
            symbol="EURUSD",
            detail="test",
        )
        assert m.expected_value == ""
        assert m.actual_value == ""
        assert m.position_id == ""

    def test_reconciliation_result_defaults(self):
        r = ReconciliationResult(ok=True)
        assert r.mismatches == []
        assert r.expected_count == 0
        assert r.actual_count == 0
        assert r.timestamp > 0
