"""Minimal demo execution path for NovaTrade.

One-shot flow:  OrderRequest → PreTradeGate → MT5Adapter → result handling.

No retry logic, no polling loop, no streaming, no autonomous strategy
generation.  The executor is called once per order intent and returns a
structured ExecutionResult.
"""

from __future__ import annotations

import logging
import time

from novatrade.adapter.base import MT5Adapter
from novatrade.config import NovaTradeCfg
from novatrade.models import (
    ExecutionOutcome,
    ExecutionResult,
    HealthStatus,
    OrderRequest,
    SymbolPrice,
)
from novatrade.monitor.position_tracker import PositionTracker
from novatrade.risk.pre_trade_gate import PreTradeGate
from novatrade.validation.evidence import EvidenceRecorder

log = logging.getLogger("novatrade.execution.demo")


class DemoExecutor:
    """Minimal end-to-end demo execution path.

    Wires gate → adapter → trade recording into one call::

        executor = DemoExecutor(cfg, adapter, gate)
        result = await executor.execute(request)

    The executor fetches account state and positions from the adapter on
    each call so the gate always evaluates against fresh data.

    Optional integrations (Phase 7):
    - ``recorder``: auto-captures evidence for every execution attempt
    - ``tracker``: auto-updates expected positions on FILLED outcomes
    """

    def __init__(
        self,
        cfg: NovaTradeCfg,
        adapter: MT5Adapter,
        gate: PreTradeGate | None = None,
        recorder: EvidenceRecorder | None = None,
        tracker: PositionTracker | None = None,
    ) -> None:
        self._cfg = cfg
        self._adapter = adapter
        self._gate = gate or PreTradeGate(cfg)
        self._recorder = recorder
        self._tracker = tracker

    async def execute(
        self,
        request: OrderRequest,
        *,
        health: HealthStatus | None = None,
        price: SymbolPrice | None = None,
    ) -> ExecutionResult:
        """Execute a single order through the full gate → adapter path.

        Args:
            request: The order to attempt.
            health: Optional pre-fetched health status.  If not provided,
                    the executor will fetch it from the adapter.
            price: Optional pre-fetched symbol price for spread checks.
                   If not provided and the adapter is connected, the
                   executor will attempt to fetch it.

        Returns:
            ExecutionResult with outcome, risk decision, and order result.
        """
        t0 = time.monotonic()

        try:
            result = await self._execute_inner(request, health=health, price=price, t0=t0)
        except Exception as exc:
            elapsed = (time.monotonic() - t0) * 1000
            msg = str(exc)
            log.error("execution exception: %s", msg)
            result = ExecutionResult(
                outcome=ExecutionOutcome.EXCEPTION,
                error=msg,
                elapsed_ms=elapsed,
            )

        self._post_execute(result)
        return result

    def _post_execute(self, result: ExecutionResult) -> None:
        """Auto-record evidence and update position tracker after execution."""
        if self._recorder:
            try:
                self._recorder.record_execution(result)
            except Exception as exc:
                log.warning("evidence recording failed: %s", exc)

        if self._tracker:
            try:
                self._tracker.record(result)
            except Exception as exc:
                log.warning("position tracker update failed: %s", exc)

    async def _execute_inner(
        self,
        request: OrderRequest,
        *,
        health: HealthStatus | None,
        price: SymbolPrice | None,
        t0: float,
    ) -> ExecutionResult:
        # -- 1. Gather context ------------------------------------------------
        if health is None:
            health = await self._adapter.health_check()

        account = await self._adapter.get_account()
        positions = await self._adapter.get_positions()

        if price is None:
            try:
                price = await self._adapter.get_symbol_price(request.symbol)
            except Exception:
                log.debug("could not fetch price for %s — proceeding without", request.symbol)
                price = None

        # -- 2. Risk gate -----------------------------------------------------
        decision = self._gate.evaluate(
            request,
            account,
            positions,
            health=health,
            price=price,
        )

        if decision.denied:
            elapsed = (time.monotonic() - t0) * 1000
            log.info(
                "execution DENIED in %.0f ms: %s — %s",
                elapsed,
                decision.rule,
                decision.reason,
            )
            return ExecutionResult(
                outcome=ExecutionOutcome.DENIED,
                risk_decision=decision,
                elapsed_ms=elapsed,
            )

        # -- 3. Adapter execution ---------------------------------------------
        log.info(
            "gate ALLOW — placing order: %s %s %s vol=%.2f",
            request.side.value,
            request.order_type.value,
            request.symbol,
            request.volume,
        )
        order_result = await self._adapter.place_order(request)

        elapsed = (time.monotonic() - t0) * 1000

        if order_result.ok:
            # -- 4. Record trade for cooldown / daily tracking ----------------
            self._gate.record_trade(request.symbol, request.side.value)
            log.info(
                "execution FILLED in %.0f ms: order_id=%s",
                elapsed,
                order_result.order_id,
            )
            return ExecutionResult(
                outcome=ExecutionOutcome.FILLED,
                risk_decision=decision,
                order_result=order_result,
                elapsed_ms=elapsed,
            )

        # -- 5. Adapter returned failure — no retry --------------------------
        log.warning(
            "execution ADAPTER_ERROR in %.0f ms: %s",
            elapsed,
            order_result.error,
        )
        return ExecutionResult(
            outcome=ExecutionOutcome.ADAPTER_ERROR,
            risk_decision=decision,
            order_result=order_result,
            error=order_result.error,
            elapsed_ms=elapsed,
        )
