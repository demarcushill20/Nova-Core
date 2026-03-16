"""Integration tests for Phase 7 — auto-wiring of evidence, tracking, and review."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from novatrade.config import NovaTradeCfg
from novatrade.execution.demo_executor import DemoExecutor
from novatrade.models import (
    AccountState,
    EvidenceType,
    ExecutionOutcome,
    HealthState,
    HealthStatus,
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    ValidationReport,
)
from novatrade.monitor.position_tracker import PositionTracker
from novatrade.validation.evidence import EvidenceRecorder
from novatrade.validation.reporting import generate_report
from novatrade.validation.review import run_validation_cycle

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def evidence_path(tmp_path: Path) -> Path:
    return tmp_path / "evidence.jsonl"


@pytest.fixture
def recorder(evidence_path: Path) -> EvidenceRecorder:
    return EvidenceRecorder(evidence_path)


@pytest.fixture
def tracker() -> PositionTracker:
    return PositionTracker()


@pytest.fixture
def cfg() -> NovaTradeCfg:
    with patch.dict(
        "os.environ",
        {
            "METAAPI_TOKEN": "test-token",
            "METAAPI_ACCOUNT_ID": "test-account",
        },
    ):
        c = NovaTradeCfg.load()
        c.dry_run = True
        return c


def _make_adapter(
    *,
    health: HealthStatus | None = None,
    account: AccountState | None = None,
    positions: list[Position] | None = None,
    order_result: OrderResult | None = None,
) -> MagicMock:
    adapter = MagicMock()
    adapter.health_check = AsyncMock(
        return_value=health or HealthStatus(state=HealthState.OK, connected=True),
    )
    adapter.get_account = AsyncMock(
        return_value=account or AccountState(balance=10000.0, equity=10200.0),
    )
    adapter.get_positions = AsyncMock(return_value=positions or [])
    adapter.get_symbol_price = AsyncMock(return_value=None)
    if order_result:
        adapter.place_order = AsyncMock(return_value=order_result)
    return adapter


def _market_request(symbol: str = "EURUSD", volume: float = 0.01) -> OrderRequest:
    return OrderRequest(
        symbol=symbol,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        volume=volume,
        stop_loss=1.0700,
    )


# ===========================================================================
# Execution auto-records evidence
# ===========================================================================


class TestExecutionAutoEvidence:
    @pytest.mark.asyncio
    async def test_denied_execution_records_evidence(
        self,
        cfg: NovaTradeCfg,
        recorder: EvidenceRecorder,
        tracker: PositionTracker,
    ):
        """Dry-run denial should auto-record an EXECUTION evidence entry."""
        cfg.dry_run = True
        adapter = _make_adapter()
        executor = DemoExecutor(cfg, adapter, recorder=recorder, tracker=tracker)

        result = await executor.execute(_market_request())

        assert result.outcome == ExecutionOutcome.DENIED
        records = recorder.load()
        assert len(records) == 1
        assert records[0].event_type == EvidenceType.EXECUTION
        assert records[0].data["outcome"] == "DENIED"

    @pytest.mark.asyncio
    async def test_filled_execution_records_evidence(
        self,
        cfg: NovaTradeCfg,
        recorder: EvidenceRecorder,
        tracker: PositionTracker,
    ):
        """Filled execution should auto-record evidence with order details."""
        cfg.dry_run = False
        adapter = _make_adapter(
            order_result=OrderResult(
                ok=True,
                order_id="ORD-42",
                status=OrderStatus.FILLED,
                fill_price=1.08,
            ),
        )
        executor = DemoExecutor(cfg, adapter, recorder=recorder, tracker=tracker)

        with patch("nova_kill_switch.check_kill_switch", return_value="run"):
            result = await executor.execute(_market_request())

        assert result.outcome == ExecutionOutcome.FILLED
        records = recorder.load()
        assert len(records) == 1
        assert records[0].data["outcome"] == "FILLED"
        assert records[0].data["order_id"] == "ORD-42"

    @pytest.mark.asyncio
    async def test_adapter_error_records_evidence(
        self,
        cfg: NovaTradeCfg,
        recorder: EvidenceRecorder,
        tracker: PositionTracker,
    ):
        """Adapter error should be captured in evidence."""
        cfg.dry_run = False
        adapter = _make_adapter(
            order_result=OrderResult(ok=False, error="broker rejected"),
        )
        executor = DemoExecutor(cfg, adapter, recorder=recorder, tracker=tracker)

        with patch("nova_kill_switch.check_kill_switch", return_value="run"):
            result = await executor.execute(_market_request())

        assert result.outcome == ExecutionOutcome.ADAPTER_ERROR
        records = recorder.load()
        assert len(records) == 1
        assert records[0].data["outcome"] == "ADAPTER_ERROR"
        assert records[0].error == "broker rejected"

    @pytest.mark.asyncio
    async def test_exception_records_evidence(
        self,
        cfg: NovaTradeCfg,
        recorder: EvidenceRecorder,
        tracker: PositionTracker,
    ):
        """Unexpected exception should be captured in evidence."""
        adapter = _make_adapter()
        adapter.health_check = AsyncMock(side_effect=RuntimeError("boom"))
        executor = DemoExecutor(cfg, adapter, recorder=recorder, tracker=tracker)

        result = await executor.execute(_market_request())

        assert result.outcome == ExecutionOutcome.EXCEPTION
        records = recorder.load()
        assert len(records) == 1
        assert records[0].data["outcome"] == "EXCEPTION"

    @pytest.mark.asyncio
    async def test_no_recorder_still_works(self, cfg: NovaTradeCfg):
        """Executor without recorder should work normally (backward compat)."""
        adapter = _make_adapter()
        executor = DemoExecutor(cfg, adapter)  # no recorder, no tracker

        result = await executor.execute(_market_request())
        assert result.outcome == ExecutionOutcome.DENIED  # dry_run blocks


# ===========================================================================
# Execution auto-updates position tracker
# ===========================================================================


class TestExecutionAutoTracking:
    @pytest.mark.asyncio
    async def test_filled_updates_tracker(
        self,
        cfg: NovaTradeCfg,
        recorder: EvidenceRecorder,
        tracker: PositionTracker,
    ):
        """Filled execution should add expected position to tracker."""
        cfg.dry_run = False
        adapter = _make_adapter(
            order_result=OrderResult(
                ok=True,
                order_id="ORD-99",
                status=OrderStatus.FILLED,
                fill_price=1.08,
            ),
        )
        executor = DemoExecutor(cfg, adapter, recorder=recorder, tracker=tracker)

        with patch("nova_kill_switch.check_kill_switch", return_value="run"):
            await executor.execute(_market_request())

        assert tracker.count == 1
        pos = tracker.expected_positions[0]
        assert pos.position_id == "ORD-99"
        assert pos.symbol == "EURUSD"

    @pytest.mark.asyncio
    async def test_denied_does_not_update_tracker(
        self,
        cfg: NovaTradeCfg,
        recorder: EvidenceRecorder,
        tracker: PositionTracker,
    ):
        """Denied execution should NOT create expected positions."""
        cfg.dry_run = True
        adapter = _make_adapter()
        executor = DemoExecutor(cfg, adapter, recorder=recorder, tracker=tracker)

        await executor.execute(_market_request())

        assert tracker.count == 0

    @pytest.mark.asyncio
    async def test_adapter_error_does_not_update_tracker(
        self,
        cfg: NovaTradeCfg,
        recorder: EvidenceRecorder,
        tracker: PositionTracker,
    ):
        """Adapter error should NOT create expected positions."""
        cfg.dry_run = False
        adapter = _make_adapter(
            order_result=OrderResult(ok=False, error="rejected"),
        )
        executor = DemoExecutor(cfg, adapter, recorder=recorder, tracker=tracker)

        with patch("nova_kill_switch.check_kill_switch", return_value="run"):
            await executor.execute(_market_request())

        assert tracker.count == 0

    @pytest.mark.asyncio
    async def test_no_tracker_still_works(
        self,
        cfg: NovaTradeCfg,
        recorder: EvidenceRecorder,
    ):
        """Executor without tracker should work normally."""
        cfg.dry_run = False
        adapter = _make_adapter(
            order_result=OrderResult(
                ok=True,
                order_id="ORD-1",
                status=OrderStatus.FILLED,
                fill_price=1.08,
            ),
        )
        executor = DemoExecutor(cfg, adapter, recorder=recorder)  # no tracker

        with patch("nova_kill_switch.check_kill_switch", return_value="run"):
            result = await executor.execute(_market_request())

        assert result.outcome == ExecutionOutcome.FILLED


# ===========================================================================
# Validation review cycle
# ===========================================================================


class TestValidationCycle:
    @pytest.mark.asyncio
    async def test_review_cycle_records_health_and_reconciliation(
        self,
        recorder: EvidenceRecorder,
        tracker: PositionTracker,
    ):
        """Validation cycle should record health + reconciliation evidence."""
        adapter = _make_adapter()

        report = await run_validation_cycle(adapter, tracker, recorder)

        records = recorder.load()
        assert len(records) == 2
        types = {r.event_type for r in records}
        assert EvidenceType.HEALTH_SNAPSHOT in types
        assert EvidenceType.RECONCILIATION in types
        assert isinstance(report, ValidationReport)

    @pytest.mark.asyncio
    async def test_review_cycle_healthy_adapter(
        self,
        recorder: EvidenceRecorder,
        tracker: PositionTracker,
    ):
        """Healthy adapter should produce a stable report."""
        adapter = _make_adapter()

        report = await run_validation_cycle(adapter, tracker, recorder)

        assert report.health_snapshots == 1
        assert report.health_ok_count == 1
        assert report.reconciliation_count == 1
        assert report.reconciliation_ok_count == 1
        assert report.stable is True

    @pytest.mark.asyncio
    async def test_review_cycle_down_adapter(
        self,
        recorder: EvidenceRecorder,
        tracker: PositionTracker,
    ):
        """Down adapter should still complete and record evidence."""
        adapter = _make_adapter(
            health=HealthStatus(state=HealthState.DOWN, connected=False, message="offline"),
        )

        report = await run_validation_cycle(adapter, tracker, recorder)

        assert report.health_snapshots == 1
        assert report.health_ok_count == 0
        records = recorder.load()
        assert len(records) == 2  # health + reconciliation both recorded

    @pytest.mark.asyncio
    async def test_review_cycle_with_prior_evidence(
        self,
        recorder: EvidenceRecorder,
        tracker: PositionTracker,
    ):
        """Review cycle should include prior evidence in its report."""
        # Simulate a prior execution
        from novatrade.models import ExecutionResult

        recorder.record_execution(
            ExecutionResult(
                outcome=ExecutionOutcome.FILLED,
                order_result=OrderResult(ok=True, order_id="ORD-1", status=OrderStatus.FILLED),
                timestamp=100.0,
            )
        )

        adapter = _make_adapter()
        report = await run_validation_cycle(adapter, tracker, recorder)

        assert report.total_records == 3  # 1 prior + 2 from cycle
        assert report.execution_count == 1
        assert report.health_snapshots == 1
        assert report.reconciliation_count == 1


# ===========================================================================
# End-to-end: execute → review → report
# ===========================================================================


class TestEndToEndWorkflow:
    @pytest.mark.asyncio
    async def test_full_workflow(
        self,
        cfg: NovaTradeCfg,
        recorder: EvidenceRecorder,
        tracker: PositionTracker,
    ):
        """Full operator workflow: execute (denied) → review → report."""
        cfg.dry_run = True
        adapter = _make_adapter()

        # Step 1: Execute (will be denied by dry_run)
        executor = DemoExecutor(cfg, adapter, recorder=recorder, tracker=tracker)
        result = await executor.execute(_market_request())
        assert result.denied

        # Step 2: Run validation cycle
        report = await run_validation_cycle(adapter, tracker, recorder)

        # Step 3: Verify report includes everything
        assert report.total_records == 3  # 1 execution + 1 health + 1 reconciliation
        assert report.execution_count == 1
        assert report.denied_count == 1
        assert report.health_snapshots == 1
        assert report.reconciliation_count == 1
        assert report.stable is True  # denied is not an error
        assert tracker.count == 0  # no positions tracked (denied)

    @pytest.mark.asyncio
    async def test_full_workflow_with_fill(
        self,
        cfg: NovaTradeCfg,
        recorder: EvidenceRecorder,
        tracker: PositionTracker,
    ):
        """Full workflow: execute (filled) → review → report with tracked position."""
        cfg.dry_run = False
        filled_result = OrderResult(
            ok=True,
            order_id="ORD-77",
            status=OrderStatus.FILLED,
            fill_price=1.08,
        )
        # No positions during execution (so duplicate_position check passes)
        adapter = _make_adapter(order_result=filled_result)

        # Step 1: Execute
        executor = DemoExecutor(cfg, adapter, recorder=recorder, tracker=tracker)
        with patch("nova_kill_switch.check_kill_switch", return_value="run"):
            result = await executor.execute(_market_request())
        assert result.ok
        assert tracker.count == 1

        # Step 2: Simulate broker now showing the filled position for review
        broker_position = Position(
            position_id="BROKER-77",
            symbol="EURUSD",
            side=OrderSide.BUY,
            volume=0.01,
            open_price=1.08,
        )
        adapter.get_positions = AsyncMock(return_value=[broker_position])

        report = await run_validation_cycle(adapter, tracker, recorder)

        # Step 3: Verify
        assert report.total_records == 3
        assert report.filled_count == 1
        assert report.health_ok_count == 1
        assert report.reconciliation_count == 1

    @pytest.mark.asyncio
    async def test_multiple_executions_accumulated(
        self,
        cfg: NovaTradeCfg,
        recorder: EvidenceRecorder,
        tracker: PositionTracker,
    ):
        """Multiple executions should accumulate evidence correctly."""
        cfg.dry_run = True
        adapter = _make_adapter()
        executor = DemoExecutor(cfg, adapter, recorder=recorder, tracker=tracker)

        # Execute 3 times (all denied)
        for _ in range(3):
            await executor.execute(_market_request())

        records = recorder.load()
        assert len(records) == 3

        report = generate_report(records)
        assert report.execution_count == 3
        assert report.denied_count == 3
        assert report.stable is True
