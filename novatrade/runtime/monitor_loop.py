"""Monitoring loop caller — Phase 8 (resolves B-P7-1).

Provides the runtime caller that invokes OpsMonitor.run_cycle() at
regular intervals.  This is the missing piece identified in Phase 7
as blocker B-P7-1.

Design decisions:
  D-P8-1: The loop is an async coroutine, not a thread or process.
           This integrates cleanly with the FastAPI event loop.
  D-P8-2: Cycle interval is configurable (default 60s for dry-run).
  D-P8-3: The loop can be started/stopped cleanly via start()/stop().
  D-P8-4: Each cycle is logged with elapsed time for auditability.
  D-P8-5: Errors in individual cycles do not kill the loop.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from novatrade.models import EvidenceRecord, EvidenceType
from novatrade.monitor.ops_monitor import CycleResult, OpsMonitor
from novatrade.validation.evidence import EvidenceRecorder

log = logging.getLogger("novatrade.runtime.monitor_loop")


@dataclass
class LoopStats:
    """Accumulated statistics for the monitor loop."""

    cycles_run: int = 0
    cycles_ok: int = 0
    cycles_failed: int = 0
    last_cycle_time: float = 0.0
    last_cycle_ok: bool = True
    total_elapsed_ms: float = 0.0

    def to_dict(self) -> dict:
        return {
            "cycles_run": self.cycles_run,
            "cycles_ok": self.cycles_ok,
            "cycles_failed": self.cycles_failed,
            "last_cycle_time": self.last_cycle_time,
            "last_cycle_ok": self.last_cycle_ok,
            "avg_cycle_ms": (self.total_elapsed_ms / self.cycles_run if self.cycles_run else 0),
        }


class MonitorLoop:
    """Async loop that calls OpsMonitor.run_cycle() at regular intervals.

    Usage::

        loop = MonitorLoop(monitor, interval_seconds=60)
        task = asyncio.create_task(loop.start())  # runs in background
        ...
        loop.stop()         # request graceful shutdown
        await task           # wait for completion
    """

    def __init__(
        self,
        monitor: OpsMonitor,
        *,
        interval_seconds: float = 60.0,
        recorder: EvidenceRecorder | None = None,
        max_cycles: int = 0,
    ) -> None:
        self._monitor = monitor
        self._interval = interval_seconds
        self._recorder = recorder
        self._max_cycles = max_cycles  # 0 = unlimited
        self._running = False
        self._task: asyncio.Task | None = None
        self.stats = LoopStats()

    @property
    def running(self) -> bool:
        return self._running

    @property
    def interval(self) -> float:
        return self._interval

    async def start(self) -> None:
        """Run the monitoring loop until stopped or max_cycles reached."""
        self._running = True
        log.info(
            "monitor loop started: interval=%.1fs max_cycles=%s",
            self._interval,
            self._max_cycles or "unlimited",
        )
        self._record("MONITOR_LOOP_STARTED", {"interval_seconds": self._interval})

        try:
            while self._running:
                if self._max_cycles and self.stats.cycles_run >= self._max_cycles:
                    log.info("monitor loop: max_cycles (%d) reached", self._max_cycles)
                    break

                await self._run_one_cycle()

                if self._running:
                    await asyncio.sleep(self._interval)
        finally:
            self._running = False
            log.info("monitor loop stopped: %s", self.stats.to_dict())
            self._record("MONITOR_LOOP_STOPPED", self.stats.to_dict())

    def stop(self) -> None:
        """Request the loop to stop after the current cycle."""
        log.info("monitor loop: stop requested")
        self._running = False

    async def run_once(self) -> CycleResult | None:
        """Run exactly one monitoring cycle (for testing / manual trigger)."""
        return await self._run_one_cycle()

    async def _run_one_cycle(self) -> CycleResult | None:
        """Execute one monitoring cycle with error handling."""
        t0 = time.monotonic()
        self.stats.cycles_run += 1
        cycle_num = self.stats.cycles_run
        result: CycleResult | None = None

        try:
            result = await self._monitor.run_cycle()

            # Execute pending actions if any were queued
            actions = await self._monitor.execute_pending_actions()

            elapsed = (time.monotonic() - t0) * 1000
            self.stats.total_elapsed_ms += elapsed
            self.stats.last_cycle_time = time.time()
            self.stats.last_cycle_ok = result.ok
            self.stats.cycles_ok += 1

            log.info(
                "cycle #%d: ok=%s health_ok=%s recon_actions=%d risk_actions=%d alerts=%d elapsed=%.0fms",
                cycle_num,
                result.ok,
                result.health_ok,
                len(result.reconciliation_actions),
                len(result.risk_actions_executed),
                len(result.alerts),
                elapsed,
            )
            self._record(
                "MONITOR_CYCLE_COMPLETE",
                {
                    "cycle": cycle_num,
                    "ok": result.ok,
                    "health_ok": result.health_ok,
                    "recon_actions": len(result.reconciliation_actions),
                    "risk_actions": len(result.risk_actions_executed),
                    "pending_actions": len(actions),
                    "alerts": len(result.alerts),
                    "elapsed_ms": elapsed,
                },
            )

        except Exception as exc:
            elapsed = (time.monotonic() - t0) * 1000
            self.stats.total_elapsed_ms += elapsed
            self.stats.last_cycle_time = time.time()
            self.stats.last_cycle_ok = False
            self.stats.cycles_failed += 1

            log.error("cycle #%d FAILED: %s (%.0fms)", cycle_num, exc, elapsed)
            self._record(
                "MONITOR_CYCLE_FAILED",
                {
                    "cycle": cycle_num,
                    "error": str(exc)[:500],
                    "elapsed_ms": elapsed,
                },
            )

        return result

    def _record(self, event_name: str, data: dict) -> None:
        if self._recorder is None:
            return
        self._recorder._append(
            EvidenceRecord(
                event_type=EvidenceType.MONITORING,
                data={"event": event_name, **data},
            )
        )
