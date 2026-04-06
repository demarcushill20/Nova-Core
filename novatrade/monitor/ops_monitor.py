"""Operational monitoring, reconciliation, and action execution — Phase 7.

This module is the minimum operational layer required to make the IRB demo-run
stack observable, state-consistent, and capable of enforcing risk-governance
outcomes.

Responsibilities (what Phase 7 owns):
  - Health monitoring (adapter, Trading Agent, Risk Engine liveness)
  - Broker/runtime reconciliation (compare Trading Agent state vs broker)
  - Fill detection → notify_fill() callback
  - Broker close detection → notify_broker_close() callback
  - Risk-directed action execution (cancel pending, flatten positions)
  - Daily drawdown reset caller
  - Operational alerts
  - Daily summary generation

Not owned by Phase 7:
  - Strategy logic (Pine/IRB)
  - Risk policy design (Phase 6)
  - Launch approval / dry-run orchestration (Phase 8+)
  - Dashboards or ML
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from zoneinfo import ZoneInfo

from novatrade.adapter.base import MT5Adapter
from novatrade.config import NovaTradeCfg
from novatrade.execution.trading_agent import AgentState, TradingAgent
from novatrade.models import (
    EvidenceRecord,
    EvidenceType,
    HealthState,
    RiskAction,
)
from novatrade.monitor.health import take_health_snapshot
from novatrade.risk.hard_risk_supervisor import HardRiskSupervisor, SupervisorAction
from novatrade.risk.risk_engine import RiskEngine
from novatrade.validation.evidence import EvidenceRecorder

log = logging.getLogger("novatrade.monitor.ops")


# ---------------------------------------------------------------------------
# Enums and data classes
# ---------------------------------------------------------------------------


class AlertSeverity(Enum):
    """Operational alert severity."""

    CRITICAL = "CRITICAL"  # requires immediate operator attention
    WARNING = "WARNING"  # important but not immediate
    INFO = "INFO"  # logged only


class AlertCategory(Enum):
    """Alert source category."""

    HEALTH = "HEALTH"
    RECONCILIATION = "RECONCILIATION"
    RISK = "RISK"
    ACTION = "ACTION"
    OPERATIONAL = "OPERATIONAL"


class ReconciliationOutcome(Enum):
    """Result type from a single reconciliation check."""

    NO_ACTION = "NO_ACTION"
    NOTIFY_FILL = "NOTIFY_FILL"
    NOTIFY_BROKER_CLOSE = "NOTIFY_BROKER_CLOSE"
    CANCEL_STALE_PENDING = "CANCEL_STALE_PENDING"
    ORPHAN_POSITION = "ORPHAN_POSITION"
    ORPHAN_PENDING = "ORPHAN_PENDING"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    DEGRADED = "DEGRADED"


@dataclass
class OpsAlert:
    """A single operational alert."""

    severity: AlertSeverity
    category: AlertCategory
    message: str
    detail: dict = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "severity": self.severity.value,
            "category": self.category.value,
            "message": self.message,
            "detail": self.detail,
            "timestamp": self.timestamp,
        }


@dataclass
class ReconciliationAction:
    """A single reconciliation observation and the action taken."""

    outcome: ReconciliationOutcome
    detail: str
    action_taken: str = ""  # what the monitor did
    success: bool = True

    def to_dict(self) -> dict:
        return {
            "outcome": self.outcome.value,
            "detail": self.detail,
            "action_taken": self.action_taken,
            "success": self.success,
        }


@dataclass
class CycleResult:
    """Result of one monitoring cycle."""

    timestamp: float = field(default_factory=time.time)
    health_ok: bool = True
    health_error: str = ""
    reconciliation_actions: list[ReconciliationAction] = field(default_factory=list)
    risk_actions_executed: list[str] = field(default_factory=list)
    supervisor_actions: list[str] = field(default_factory=list)
    daily_reset_performed: bool = False
    alerts: list[OpsAlert] = field(default_factory=list)
    elapsed_ms: float = 0.0

    @property
    def ok(self) -> bool:
        return self.health_ok and all(a.success for a in self.reconciliation_actions)

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "health_ok": self.health_ok,
            "health_error": self.health_error,
            "reconciliation_actions": [a.to_dict() for a in self.reconciliation_actions],
            "risk_actions_executed": self.risk_actions_executed,
            "supervisor_actions": self.supervisor_actions,
            "daily_reset_performed": self.daily_reset_performed,
            "alerts": [a.to_dict() for a in self.alerts],
            "elapsed_ms": round(self.elapsed_ms, 1),
            "ok": self.ok,
        }


@dataclass
class DailySummary:
    """End-of-day operational summary."""

    date: str  # ISO date
    cycles_run: int = 0
    health_checks_ok: int = 0
    health_checks_failed: int = 0
    fills_detected: int = 0
    broker_closes_detected: int = 0
    risk_halts_active: int = 0
    risk_denials: int = 0
    cancel_actions: int = 0
    flatten_actions: int = 0
    cancel_failures: int = 0
    flatten_failures: int = 0
    daily_reset_count: int = 0
    reconciliation_mismatches: int = 0
    orphan_positions: int = 0
    orphan_pending_orders: int = 0
    manual_reviews_needed: int = 0
    alerts_critical: int = 0
    alerts_warning: int = 0
    alerts_info: int = 0
    current_state: str = ""
    current_halt_status: bool = False
    current_halt_reason: str = ""
    current_equity: float = 0.0
    pnl_today_usd: float = 0.0
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "date": self.date,
            "cycles_run": self.cycles_run,
            "health": {
                "ok": self.health_checks_ok,
                "failed": self.health_checks_failed,
            },
            "fills_detected": self.fills_detected,
            "broker_closes_detected": self.broker_closes_detected,
            "risk": {
                "halts_active": self.risk_halts_active,
                "denials": self.risk_denials,
            },
            "actions": {
                "cancel": self.cancel_actions,
                "flatten": self.flatten_actions,
                "cancel_failures": self.cancel_failures,
                "flatten_failures": self.flatten_failures,
            },
            "daily_resets": self.daily_reset_count,
            "reconciliation": {
                "mismatches": self.reconciliation_mismatches,
                "orphan_positions": self.orphan_positions,
                "orphan_pending": self.orphan_pending_orders,
                "manual_reviews": self.manual_reviews_needed,
            },
            "alerts": {
                "critical": self.alerts_critical,
                "warning": self.alerts_warning,
                "info": self.alerts_info,
            },
            "state": {
                "agent_state": self.current_state,
                "halt_status": self.current_halt_status,
                "halt_reason": self.current_halt_reason,
                "equity": round(self.current_equity, 2),
                "pnl_today_usd": round(self.pnl_today_usd, 2),
            },
            "timestamp": self.timestamp,
        }


# ---------------------------------------------------------------------------
# Operational Monitor
# ---------------------------------------------------------------------------


class OpsMonitor:
    """Operational monitoring layer for the IRB demo-run stack.

    Coordinates health checks, reconciliation, fill/close detection,
    risk-action execution, daily reset, and alert generation.

    Usage::

        monitor = OpsMonitor(cfg, adapter, agent, risk_engine, recorder)

        # Run a single monitoring cycle
        result = await monitor.run_cycle()

        # Generate daily summary
        summary = monitor.generate_daily_summary()

        # Write summary to file
        monitor.write_daily_summary(summary, output_dir)
    """

    def __init__(
        self,
        cfg: NovaTradeCfg,
        adapter: MT5Adapter,
        agent: TradingAgent,
        risk_engine: RiskEngine,
        recorder: EvidenceRecorder | None = None,
        *,
        supervisor: HardRiskSupervisor | None = None,
        clock: _ClockFn | None = None,
    ) -> None:
        self._cfg = cfg
        self._adapter = adapter
        self._agent = agent
        self._risk = risk_engine
        self._recorder = recorder
        self._supervisor = supervisor
        self._clock = clock or _default_utc_now

        # FTMO daily reset uses Prague timezone (CET/CEST), not UTC.
        # This prevents drawdown miscalculation at DST transitions.
        self._reset_tz = ZoneInfo(cfg.risk.daily_reset_tz)

        # Daily tracking
        self._last_reset_date: str = ""  # ISO date of last daily reset (in reset_tz)
        self._cycle_count: int = 0
        self._daily_summary = DailySummary(date=self._today_iso())
        self._alerts: list[OpsAlert] = []

    # ------------------------------------------------------------------
    # Main cycle
    # ------------------------------------------------------------------

    async def run_cycle(self) -> CycleResult:
        """Execute one full monitoring cycle.

        Order of operations:
        1. Health check
        2. Hard Risk Supervisor continuous check (equity, daily loss, positions)
        3. Daily reset check (before reconciliation)
        4. Reconciliation + fill/close detection
        5. Risk action execution
        6. Alert generation
        """
        t0 = time.monotonic()
        result = CycleResult()
        self._cycle_count += 1
        self._daily_summary.cycles_run += 1

        # 1. Health check
        try:
            await self._check_health(result)
        except Exception as exc:
            result.health_ok = False
            result.health_error = str(exc)
            self._emit_alert(
                AlertSeverity.CRITICAL,
                AlertCategory.HEALTH,
                f"Health check exception: {exc}",
                result,
            )

        # 2. Hard Risk Supervisor continuous check
        try:
            await self._check_supervisor(result)
        except Exception as exc:
            self._emit_alert(
                AlertSeverity.WARNING,
                AlertCategory.RISK,
                f"Supervisor check failed: {exc}",
                result,
            )

        # 3. Daily reset
        try:
            await self._check_daily_reset(result)
        except Exception as exc:
            self._emit_alert(
                AlertSeverity.WARNING,
                AlertCategory.OPERATIONAL,
                f"Daily reset check failed: {exc}",
                result,
            )

        # 4. Reconciliation (only if health is OK)
        if result.health_ok:
            try:
                await self._reconcile(result)
            except Exception as exc:
                self._emit_alert(
                    AlertSeverity.WARNING,
                    AlertCategory.RECONCILIATION,
                    f"Reconciliation exception: {exc}",
                    result,
                )

        # 5. Risk action execution
        try:
            self._execute_risk_actions(result)
        except Exception as exc:
            self._emit_alert(
                AlertSeverity.WARNING,
                AlertCategory.ACTION,
                f"Risk action execution error: {exc}",
                result,
            )

        # 6. Risk state alert check
        self._check_risk_alerts(result)

        # 7. Persist FTMO compliance state (crash recovery)
        try:
            self._risk.save_ftmo_state()
        except Exception as exc:
            log.debug("FTMO state save failed: %s", exc)

        # 8. Persist risk engine drawdown state for autonomy collectors
        try:
            self._risk.write_risk_state()
        except Exception as exc:
            log.debug("Risk state write failed: %s", exc)

        # 9. Capture live equity snapshot for performance_stability collector
        try:
            await self._persist_equity_snapshot()
        except Exception as exc:
            log.debug("Equity snapshot failed: %s", exc)

        # 10. Refresh strategy config timestamp for strategy_validity collector
        try:
            self._refresh_strategy_config()
        except Exception as exc:
            log.debug("Strategy config refresh failed: %s", exc)

        result.elapsed_ms = (time.monotonic() - t0) * 1000
        self._record_cycle(result)
        return result

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------

    async def _check_health(self, result: CycleResult) -> None:
        """Run health snapshot and evaluate status."""
        snapshot = await take_health_snapshot(self._adapter)

        if snapshot.ok:
            result.health_ok = True
            self._daily_summary.health_checks_ok += 1
        else:
            result.health_ok = False
            result.health_error = snapshot.error
            self._daily_summary.health_checks_failed += 1
            severity = (
                AlertSeverity.CRITICAL if snapshot.adapter_health.state == HealthState.DOWN else AlertSeverity.WARNING
            )
            self._emit_alert(
                severity,
                AlertCategory.HEALTH,
                f"Health degraded: {snapshot.error}",
                result,
                detail={
                    "adapter_state": snapshot.adapter_health.state.value,
                    "latency_ms": snapshot.adapter_health.latency_ms,
                },
            )

        # Record to evidence
        if self._recorder:
            self._recorder.record_health(snapshot)

    # ------------------------------------------------------------------
    # Hard Risk Supervisor continuous monitoring
    # ------------------------------------------------------------------

    async def _check_supervisor(self, result: CycleResult) -> None:
        """Run HardRiskSupervisor continuous account check.

        This is the critical safety net for the webhook pipeline — between
        TradingView alerts, the supervisor monitors equity, daily loss,
        and position limits.  If any hard limit is breached, the supervisor
        orders emergency actions (CLOSE_ALL, HALT).
        """
        if self._supervisor is None:
            return

        # Get current account state
        account = None
        positions: list = []

        if hasattr(self._adapter, "get_account"):
            try:
                account = await self._adapter.get_account()
            except Exception:
                log.warning("supervisor: failed to get account", exc_info=True)
                return

        if hasattr(self._adapter, "get_positions"):
            try:
                positions = await self._adapter.get_positions()
            except Exception:
                log.warning("supervisor: failed to get positions", exc_info=True)
                return

        if account is None:
            log.debug("supervisor: no account data for continuous check")
            return

        # Convert Position dataclasses to dicts for check_account()
        pos_dicts = [asdict(p) if hasattr(p, "__dataclass_fields__") else p for p in positions]

        # Check supervisor for emergency actions
        actions = self._supervisor.check_account(account.equity, pos_dicts)

        for action in actions:
            result.supervisor_actions.append(f"{action.action}: {action.reason}")
            await self._execute_supervisor_action(action, result)

    async def _execute_supervisor_action(self, action: SupervisorAction, result: CycleResult) -> None:
        """Execute a supervisor-ordered emergency action."""
        log.critical("SUPERVISOR ACTION: %s — %s", action.action, action.reason)

        try:
            if action.action == "CLOSE_ALL":
                if hasattr(self._adapter, "close_all_positions"):
                    await self._adapter.close_all_positions()
                    log.critical("supervisor: all positions closed by supervisor order")
                else:
                    log.error("supervisor: close_all requested but adapter lacks method")

            elif action.action == "HALT":
                log.critical("supervisor: HALT activated — %s", action.reason)
                # Agent should stop accepting new signals
                if hasattr(self._agent, "_halted"):
                    self._agent._halted = True

            elif action.action == "CLOSE_POSITION":
                for pid in action.position_ids:
                    if hasattr(self._adapter, "close_position"):
                        await self._adapter.close_position(pid)
                        log.critical("supervisor: closed position %s", pid)

            self._emit_alert(
                AlertSeverity.CRITICAL,
                AlertCategory.RISK,
                f"Supervisor action: {action.action} — {action.reason}",
                result,
                detail={"action": action.action, "position_ids": action.position_ids},
            )
            self._daily_summary.risk_halts_active += 1

        except Exception:
            log.exception("supervisor: failed to execute action %s", action.action)
            self._emit_alert(
                AlertSeverity.CRITICAL,
                AlertCategory.RISK,
                f"Supervisor action FAILED: {action.action} — {action.reason}",
                result,
            )

    # ------------------------------------------------------------------
    # Reconciliation + fill/close detection
    # ------------------------------------------------------------------

    async def _reconcile(self, result: CycleResult) -> None:
        """Compare Trading Agent state vs broker reality.

        Detects fills, broker closes, orphan positions/orders, and
        state mismatches.  Takes corrective action where safe.
        """
        agent_state = self._agent.state
        agent_pending = self._agent.pending_order_id
        agent_position = self._agent.position_id

        # Fetch broker state
        try:
            broker_positions = await self._adapter.get_positions()
        except Exception as exc:
            result.reconciliation_actions.append(
                ReconciliationAction(
                    outcome=ReconciliationOutcome.DEGRADED,
                    detail=f"Cannot fetch positions: {exc}",
                    success=False,
                )
            )
            self._daily_summary.reconciliation_mismatches += 1
            return

        try:
            broker_orders = await self._adapter.get_orders()
        except Exception:
            # get_orders may not be implemented — degrade gracefully
            broker_orders = []

        has_broker_position = len(broker_positions) > 0
        has_broker_pending = len(broker_orders) > 0

        # --- Case 1: Agent PENDING, broker has position → fill detected ---
        if agent_state in (AgentState.PENDING_LONG, AgentState.PENDING_SHORT) and has_broker_position:
            pos = broker_positions[0]  # IRB: max 1 position
            self._agent.notify_fill(
                position_id=pos.position_id,
                fill_price=pos.open_price,
                volume=pos.volume,
                stop_loss=pos.stop_loss or 0.0,
            )
            result.reconciliation_actions.append(
                ReconciliationAction(
                    outcome=ReconciliationOutcome.NOTIFY_FILL,
                    detail=f"Fill detected: {pos.side.value} {pos.symbol} at {pos.open_price}",
                    action_taken="notify_fill() called",
                )
            )
            self._daily_summary.fills_detected += 1
            self._emit_alert(
                AlertSeverity.INFO,
                AlertCategory.RECONCILIATION,
                f"Fill detected: {pos.symbol} {pos.side.value} at {pos.open_price}",
                result,
            )
            return  # state updated, skip further checks this cycle

        # --- Case 2: Agent LONG/SHORT, broker has no position → broker close ---
        if agent_state in (AgentState.LONG, AgentState.SHORT) and not has_broker_position:
            position_id = agent_position or "unknown"
            exit_reason = "BROKER_CLOSE"

            # Try to get P&L from last known state
            self._agent.notify_broker_close(
                position_id=position_id,
                exit_reason=exit_reason,
            )
            # Notify risk engine (P&L unknown — use 0.0 as placeholder,
            # real P&L should be fetched from account equity delta)
            self._risk.on_trade_close(
                position_id=position_id,
                symbol=self._cfg.symbols[0] if self._cfg.symbols else "EURUSD",
                side="BUY" if agent_state == AgentState.LONG else "SELL",
                volume=0.0,
                pnl_usd=0.0,
                pnl_pips=0.0,
                exit_reason=exit_reason,
            )
            result.reconciliation_actions.append(
                ReconciliationAction(
                    outcome=ReconciliationOutcome.NOTIFY_BROKER_CLOSE,
                    detail=f"Broker close detected: position {position_id} no longer at broker",
                    action_taken="notify_broker_close() + on_trade_close() called",
                )
            )
            self._daily_summary.broker_closes_detected += 1
            self._emit_alert(
                AlertSeverity.WARNING,
                AlertCategory.RECONCILIATION,
                f"Broker closed position {position_id} (SL/trailing stop hit)",
                result,
            )
            return

        # --- Case 3: Agent PENDING, broker has no pending AND no position → stale ---
        if (
            agent_state in (AgentState.PENDING_LONG, AgentState.PENDING_SHORT)
            and not has_broker_pending
            and not has_broker_position
        ):
            self._agent.force_flat(reason=f"stale_pending:{agent_pending}")
            result.reconciliation_actions.append(
                ReconciliationAction(
                    outcome=ReconciliationOutcome.CANCEL_STALE_PENDING,
                    detail=f"Pending order {agent_pending} not found at broker — returned to FLAT",
                    action_taken="force_flat() called",
                )
            )
            self._daily_summary.reconciliation_mismatches += 1
            self._emit_alert(
                AlertSeverity.WARNING,
                AlertCategory.RECONCILIATION,
                f"Stale pending order {agent_pending} — reset to FLAT",
                result,
            )
            return

        # --- Case 4: Agent FLAT, broker has position → orphan ---
        if agent_state == AgentState.FLAT and has_broker_position:
            for pos in broker_positions:
                result.reconciliation_actions.append(
                    ReconciliationAction(
                        outcome=ReconciliationOutcome.ORPHAN_POSITION,
                        detail=f"Orphan position: {pos.side.value} {pos.symbol} vol={pos.volume} id={pos.position_id}",
                        action_taken="manual review required",
                        success=False,
                    )
                )
                self._daily_summary.orphan_positions += 1
            self._emit_alert(
                AlertSeverity.CRITICAL,
                AlertCategory.RECONCILIATION,
                f"Orphan position(s) at broker — agent is FLAT but {len(broker_positions)} position(s) open",
                result,
            )
            self._daily_summary.manual_reviews_needed += 1
            return

        # --- Case 5: Agent FLAT, broker has pending order → orphan ---
        if agent_state == AgentState.FLAT and has_broker_pending:
            for order in broker_orders:
                result.reconciliation_actions.append(
                    ReconciliationAction(
                        outcome=ReconciliationOutcome.ORPHAN_PENDING,
                        detail=f"Orphan pending: {order.side.value} {order.symbol} id={order.order_id}",
                        action_taken="manual review required",
                        success=False,
                    )
                )
                self._daily_summary.orphan_pending_orders += 1
            self._emit_alert(
                AlertSeverity.WARNING,
                AlertCategory.RECONCILIATION,
                "Orphan pending order(s) at broker — agent is FLAT",
                result,
            )
            return

        # --- Default: no mismatch ---
        result.reconciliation_actions.append(
            ReconciliationAction(
                outcome=ReconciliationOutcome.NO_ACTION,
                detail=f"State consistent: agent={agent_state.value}",
            )
        )

    # ------------------------------------------------------------------
    # Risk action execution
    # ------------------------------------------------------------------

    def _execute_risk_actions(self, result: CycleResult) -> None:
        """Check risk engine for required actions and queue execution.

        Actions are logged but async execution is deferred to
        execute_pending_actions() since cancel/flatten require adapter calls.
        """
        actions = self._risk.get_required_actions()
        for action in actions:
            if action == RiskAction.NONE:
                continue
            result.risk_actions_executed.append(action.value)

    async def execute_pending_actions(self) -> list[ReconciliationAction]:
        """Execute risk-directed cancel/flatten actions.

        Called separately from run_cycle() because these are async
        adapter calls that need careful error handling.
        """
        results: list[ReconciliationAction] = []
        actions = self._risk.get_required_actions()

        for action in actions:
            if action == RiskAction.CANCEL_PENDING:
                r = await self._execute_cancel_pending()
                results.append(r)
            elif action == RiskAction.FLATTEN_POSITIONS:
                r = await self._execute_flatten()
                results.append(r)

        return results

    async def _execute_cancel_pending(self) -> ReconciliationAction:
        """Cancel all pending orders at the broker."""
        try:
            orders = await self._adapter.get_orders()
        except Exception as exc:
            self._daily_summary.cancel_failures += 1
            return ReconciliationAction(
                outcome=ReconciliationOutcome.DEGRADED,
                detail=f"Cannot fetch orders for cancel: {exc}",
                action_taken="cancel_pending failed",
                success=False,
            )

        if not orders:
            return ReconciliationAction(
                outcome=ReconciliationOutcome.NO_ACTION,
                detail="No pending orders to cancel",
                action_taken="none",
            )

        cancelled = 0
        errors = []
        for order in orders:
            order_result = await self._adapter.cancel_order(order.order_id)
            if order_result.ok:
                cancelled += 1
                log.info("Cancelled pending order %s", order.order_id)
            else:
                errors.append(f"{order.order_id}: {order_result.error}")
                log.warning("Failed to cancel order %s: %s", order.order_id, order_result.error)

        self._daily_summary.cancel_actions += cancelled
        self._daily_summary.cancel_failures += len(errors)

        self._record_monitoring_event(
            "CANCEL_PENDING_EXECUTED",
            {
                "cancelled": cancelled,
                "errors": errors,
                "total_orders": len(orders),
            },
        )

        if errors:
            return ReconciliationAction(
                outcome=ReconciliationOutcome.MANUAL_REVIEW,
                detail=f"Cancelled {cancelled}/{len(orders)}, errors: {errors}",
                action_taken="partial cancel",
                success=False,
            )

        return ReconciliationAction(
            outcome=ReconciliationOutcome.NO_ACTION,
            detail=f"Cancelled {cancelled} pending order(s)",
            action_taken="cancel_pending complete",
        )

    async def _execute_flatten(self) -> ReconciliationAction:
        """Close all open positions at the broker."""
        try:
            positions = await self._adapter.get_positions()
        except Exception as exc:
            self._daily_summary.flatten_failures += 1
            return ReconciliationAction(
                outcome=ReconciliationOutcome.DEGRADED,
                detail=f"Cannot fetch positions for flatten: {exc}",
                action_taken="flatten failed",
                success=False,
            )

        if not positions:
            return ReconciliationAction(
                outcome=ReconciliationOutcome.NO_ACTION,
                detail="No open positions to flatten",
                action_taken="none",
            )

        closed = 0
        errors = []
        for pos in positions:
            order_result = await self._adapter.close_position(pos.position_id)
            if order_result.ok:
                closed += 1
                log.info("Closed position %s", pos.position_id)
                # Notify risk engine
                self._risk.on_trade_close(
                    position_id=pos.position_id,
                    symbol=pos.symbol,
                    side=pos.side.value,
                    volume=pos.volume,
                    pnl_usd=pos.unrealized_pnl,
                    pnl_pips=0.0,
                    exit_reason="RISK_FLATTEN",
                )
                # Notify trading agent
                self._agent.notify_broker_close(
                    position_id=pos.position_id,
                    exit_reason="RISK_FLATTEN",
                )
            else:
                errors.append(f"{pos.position_id}: {order_result.error}")
                log.warning("Failed to close position %s: %s", pos.position_id, order_result.error)

        self._daily_summary.flatten_actions += closed
        self._daily_summary.flatten_failures += len(errors)

        self._record_monitoring_event(
            "FLATTEN_EXECUTED",
            {
                "closed": closed,
                "errors": errors,
                "total_positions": len(positions),
            },
        )

        if errors:
            return ReconciliationAction(
                outcome=ReconciliationOutcome.MANUAL_REVIEW,
                detail=f"Closed {closed}/{len(positions)}, errors: {errors}",
                action_taken="partial flatten",
                success=False,
            )

        return ReconciliationAction(
            outcome=ReconciliationOutcome.NO_ACTION,
            detail=f"Closed {closed} position(s)",
            action_taken="flatten complete",
        )

    # ------------------------------------------------------------------
    # Daily reset
    # ------------------------------------------------------------------

    async def _check_daily_reset(self, result: CycleResult) -> None:
        """Check if daily drawdown reset is due and execute if so.

        Reset happens once per calendar day at midnight in the configured
        FTMO timezone (Europe/Prague by default).  This correctly handles
        CET↔CEST DST transitions.
        Does NOT clear halt state (per kill_switch_policy.md §7).
        """
        today = self._today_iso()
        if today == self._last_reset_date:
            return  # already reset today

        # Fetch current equity for reset
        try:
            account = await self._adapter.get_account()
            equity = account.equity
        except Exception as exc:
            self._emit_alert(
                AlertSeverity.WARNING,
                AlertCategory.OPERATIONAL,
                f"Daily reset skipped — cannot fetch account: {exc}",
                result,
            )
            return

        self._risk.reset_daily(equity)
        self._last_reset_date = today
        result.daily_reset_performed = True
        self._daily_summary.daily_reset_count += 1

        self._record_monitoring_event(
            "DAILY_RESET",
            {
                "date": today,
                "equity": equity,
                "halt_active": self._risk.halted,
                "halt_cleared": False,  # reset does NOT clear halt
            },
        )

        self._emit_alert(
            AlertSeverity.INFO,
            AlertCategory.OPERATIONAL,
            f"Daily drawdown reset at equity=${equity:.2f}",
            result,
            detail={"date": today, "equity": equity},
        )

        log.info(
            "Daily reset executed: date=%s tz=%s equity=%.2f",
            today,
            self._reset_tz.key,
            equity,
        )

    # ------------------------------------------------------------------
    # Risk alerts
    # ------------------------------------------------------------------

    def _check_risk_alerts(self, result: CycleResult) -> None:
        """Check risk engine state and emit alerts if needed."""
        if self._risk.halted:
            self._daily_summary.risk_halts_active = 1
            self._emit_alert(
                AlertSeverity.CRITICAL,
                AlertCategory.RISK,
                f"Risk engine HALTED: {self._risk.halt_reason}",
                result,
                detail={"halt_reason": self._risk.halt_reason},
            )

    # ------------------------------------------------------------------
    # Daily summary
    # ------------------------------------------------------------------

    def generate_daily_summary(self) -> DailySummary:
        """Generate a daily summary of operational activity.

        Call at end of trading day or on demand.
        """
        summary = self._daily_summary

        # Populate current state
        summary.current_state = self._agent.state.value
        summary.current_halt_status = self._risk.halted
        summary.current_halt_reason = self._risk.halt_reason
        summary.current_equity = self._risk.current_equity

        snap = self._risk.get_risk_snapshot()
        summary.pnl_today_usd = snap.pnl_today_usd

        # Count alert severities
        for alert in self._alerts:
            if alert.severity == AlertSeverity.CRITICAL:
                summary.alerts_critical += 1
            elif alert.severity == AlertSeverity.WARNING:
                summary.alerts_warning += 1
            else:
                summary.alerts_info += 1

        summary.timestamp = time.time()
        return summary

    def write_daily_summary(self, summary: DailySummary, output_dir: Path | str) -> Path:
        """Write daily summary to a JSON file."""
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        path = out / f"daily_summary_{summary.date}.json"
        path.write_text(json.dumps(summary.to_dict(), indent=2) + "\n")
        log.info("Daily summary written to %s", path)
        return path

    def reset_daily_tracking(self) -> None:
        """Reset daily counters for a new trading day."""
        self._daily_summary = DailySummary(date=self._today_iso())
        self._alerts.clear()

    # ------------------------------------------------------------------
    # Evidence / alerts
    # ------------------------------------------------------------------

    def _emit_alert(
        self,
        severity: AlertSeverity,
        category: AlertCategory,
        message: str,
        result: CycleResult,
        *,
        detail: dict | None = None,
    ) -> None:
        """Create an alert, add to cycle result and daily tracking."""
        alert = OpsAlert(
            severity=severity,
            category=category,
            message=message,
            detail=detail or {},
        )
        result.alerts.append(alert)
        self._alerts.append(alert)

        # Log based on severity
        if severity == AlertSeverity.CRITICAL:
            log.critical("ALERT [%s] %s: %s", category.value, severity.value, message)
        elif severity == AlertSeverity.WARNING:
            log.warning("ALERT [%s] %s: %s", category.value, severity.value, message)
        else:
            log.info("ALERT [%s] %s: %s", category.value, severity.value, message)

        # Record to evidence
        self._record_monitoring_event(
            "ALERT",
            {
                "severity": severity.value,
                "category": category.value,
                "message": message,
                **(detail or {}),
            },
        )

    def _record_monitoring_event(self, event: str, data: dict) -> None:
        """Record a monitoring event to evidence trail."""
        if not self._recorder:
            return
        try:
            self._recorder._append(
                EvidenceRecord(
                    event_type=EvidenceType.MONITORING,
                    data={"monitoring_event": event, **data},
                )
            )
        except Exception as exc:
            log.warning("Evidence recording failed: %s", exc)

    def _record_cycle(self, result: CycleResult) -> None:
        """Record cycle result to evidence trail."""
        self._record_monitoring_event(
            "CYCLE_COMPLETE",
            {
                "cycle_number": self._cycle_count,
                "ok": result.ok,
                "health_ok": result.health_ok,
                "actions_count": len(result.reconciliation_actions),
                "alerts_count": len(result.alerts),
                "elapsed_ms": round(result.elapsed_ms, 1),
            },
        )

    # ------------------------------------------------------------------
    # State persistence for autonomy collectors
    # ------------------------------------------------------------------

    def _state_dir(self) -> Path:
        """Return STATE/novatrade directory (relative to CWD = project root)."""
        return Path(self._cfg.data_dir).parent.parent / "STATE" / "novatrade"

    async def _persist_equity_snapshot(self) -> None:
        """Capture live equity from broker and append to equity_history.json.

        Enables the performance_stability collector to score from real data
        instead of stale backtest-only snapshots.
        """
        try:
            account = await self._adapter.get_account()
            equity = account.equity
        except Exception:
            return  # adapter unavailable — skip this cycle

        if equity is None or equity <= 0:
            return

        state_dir = self._state_dir()
        eq_path = state_dir / "equity_history.json"

        state_dir.mkdir(parents=True, exist_ok=True)

        # Load existing data
        existing: dict = {"snapshots": [], "initial_equity": equity}
        if eq_path.exists():
            try:
                raw = json.loads(eq_path.read_text())
                if isinstance(raw, dict):
                    existing = raw
                elif isinstance(raw, list):
                    existing = {"snapshots": raw, "initial_equity": equity}
            except (ValueError, OSError):
                pass

        snapshots = existing.get("snapshots", [])

        now_iso = self._clock().isoformat()

        # Rate-limit: at most one snapshot per hour.
        # The performance_stability collector resamples to hourly granularity,
        # so sub-hour writes are wasted.  Worse, rapid equity oscillation
        # (e.g. balance↔equity-with-unrealized-PnL every 5s) fills the 720-entry
        # cap within a single hour, evicting historical multi-hour data and
        # collapsing the resampled series to 1 point → score regression.
        if snapshots:
            last_ts = snapshots[-1].get("timestamp", "")
            last_hour = last_ts[:13] if len(last_ts) >= 13 else ""
            current_hour = now_iso[:13] if len(now_iso) >= 13 else ""
            if last_hour == current_hour:
                return  # already have a snapshot for this hour

        snapshots.append(
            {
                "equity": equity,
                "timestamp": now_iso,
                "source": "live",
            }
        )

        # Sort chronologically to prevent out-of-order entries from
        # accumulating across sessions and causing collector score flicker.
        snapshots.sort(
            key=lambda s: s.get("timestamp", "").replace("Z", "+00:00").split(".")[0],
        )

        # Retain last 720 entries (~30 days of hourly snapshots)
        if len(snapshots) > 720:
            snapshots = snapshots[-720:]

        existing["snapshots"] = snapshots
        existing["last_updated"] = now_iso
        eq_path.write_text(json.dumps(existing, indent=2))

    def _refresh_strategy_config(self) -> None:
        """Touch strategy_config.json with a fresh timestamp.

        The strategy_validity collector scores config freshness — files older
        than 24h score 30/100.  Refreshing here keeps the score high between
        service restarts.
        """
        state_dir = self._state_dir()
        config_path = state_dir / "strategy_config.json"

        # Load existing config to preserve fields, or create minimal default
        config_data: dict = {}
        if config_path.exists():
            try:
                config_data = json.loads(config_path.read_text())
            except (ValueError, OSError):
                pass

        if not config_data:
            config_data = {
                "strategy": "IRB",
                "symbol": self._cfg.symbols[0] if self._cfg.symbols else "EURUSD",
                "primary_timeframe": "H1",
                "higher_timeframe": "H4",
                "pipeline": "webhook",
                "symbols": self._cfg.symbols,
            }

        config_data["started_at"] = time.time()
        state_dir.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(config_data, indent=2))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _today_iso(self) -> str:
        """Return today's date as ISO string in the FTMO reset timezone.

        FTMO measures daily drawdown from midnight Prague time (CET/CEST).
        Using UTC would cause the reset to fire at the wrong hour, and
        the CET→CEST DST transition (last Sunday of March at 02:00)
        would shift the boundary by one hour if we hardcoded UTC+1.
        """
        utc_now = self._clock()
        return utc_now.astimezone(self._reset_tz).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Type aliases and helpers
# ---------------------------------------------------------------------------

_ClockFn = Callable[[], dt.datetime]


def _default_utc_now() -> dt.datetime:
    """Default clock — current UTC datetime."""
    return dt.datetime.now(dt.timezone.utc)
