"""Async worker pool with priority queue for NovaCore task execution.

Phase 4.3: Enables bounded concurrent task processing with priority ordering,
graceful cancellation, and observability.

Usage:
    pool = AsyncWorkerPool(max_workers=2, worker_fn=dispatch)
    pool.start()
    await pool.submit(task_path, stem="0001_hello", priority=TaskPriority.HIGH)
    ...
    await pool.shutdown(timeout=30)
"""

from __future__ import annotations

import asyncio
import enum
import logging
import re
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("novacore.async_pool")


# ---------------------------------------------------------------------------
# Priority
# ---------------------------------------------------------------------------


class TaskPriority(enum.IntEnum):
    """Task priority levels. Lower value = higher priority."""

    CRITICAL = 0
    HIGH = 1
    NORMAL = 2
    LOW = 3

    @classmethod
    def from_string(cls, s: str) -> TaskPriority:
        """Parse priority from string (case-insensitive)."""
        mapping = {
            "critical": cls.CRITICAL,
            "high": cls.HIGH,
            "normal": cls.NORMAL,
            "low": cls.LOW,
        }
        return mapping.get(s.strip().lower(), cls.NORMAL)


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)
_PRIORITY_RE = re.compile(r"^priority:\s*(\w+)", re.MULTILINE)


def parse_task_priority(task_text: str) -> TaskPriority:
    """Extract priority from task YAML frontmatter.

    Looks for:
        ---
        priority: high
        ---
    Returns TaskPriority.NORMAL if not found or unparseable.
    """
    fm = _FRONTMATTER_RE.match(task_text)
    if fm:
        m = _PRIORITY_RE.search(fm.group(1))
        if m:
            return TaskPriority.from_string(m.group(1))
    return TaskPriority.NORMAL


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


@dataclass
class WorkerStatus:
    """Snapshot of a single active worker."""

    stem: str
    priority: TaskPriority
    started_at: str  # ISO-8601 UTC


# ---------------------------------------------------------------------------
# Internal queue item (ordered by priority, then submission order)
# ---------------------------------------------------------------------------


@dataclass(order=True)
class _QueueItem:
    priority: int
    sequence: int
    stem: str = field(compare=False)
    task_path: Path = field(compare=False)


# ---------------------------------------------------------------------------
# Pool
# ---------------------------------------------------------------------------


class AsyncWorkerPool:
    """Bounded async worker pool with priority queue.

    * Consumer loop dequeues tasks in priority order.
    * Semaphore bounds how many workers run concurrently.
    * Graceful shutdown waits for in-flight tasks then cancels stragglers.
    """

    def __init__(
        self,
        max_workers: int = 2,
        worker_fn: Callable[[Path], Coroutine[Any, Any, None]] | None = None,
    ):
        self._max_workers = max_workers
        self._worker_fn = worker_fn
        self._semaphore = asyncio.Semaphore(max_workers)
        self._queue: asyncio.PriorityQueue[_QueueItem] = asyncio.PriorityQueue()
        self._active: dict[str, asyncio.Task] = {}
        self._active_meta: dict[str, WorkerStatus] = {}
        self._consumer_task: asyncio.Task | None = None
        self._shutdown_flag = False
        self._seq = 0

        # Counters
        self.submitted = 0
        self.completed = 0
        self.failed = 0
        self.cancelled = 0

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Start the consumer loop. Must be called inside a running event loop."""
        self._consumer_task = asyncio.create_task(self._consumer_loop())
        logger.info("POOL STARTED: max_workers=%d", self._max_workers)

    async def shutdown(self, timeout: float = 30.0) -> None:
        """Gracefully shut down the pool.

        1. Stop accepting new work.
        2. Wait up to *timeout* seconds for active tasks.
        3. Cancel any remaining tasks.
        """
        self._shutdown_flag = True
        active_count = len(self._active)
        logger.info(
            "POOL SHUTDOWN: waiting up to %.0fs for %d active task(s)",
            timeout,
            active_count,
        )

        # Stop consumer
        if self._consumer_task and not self._consumer_task.done():
            self._consumer_task.cancel()
            try:
                await self._consumer_task
            except asyncio.CancelledError:
                pass

        # Wait for active workers
        if self._active:
            tasks = list(self._active.values())
            done, pending = await asyncio.wait(tasks, timeout=timeout)
            if pending:
                logger.warning(
                    "POOL SHUTDOWN: force-cancelling %d task(s)",
                    len(pending),
                )
                for t in pending:
                    t.cancel()
                await asyncio.wait(pending, timeout=5.0)

        logger.info(
            "POOL SHUTDOWN COMPLETE: submitted=%d completed=%d failed=%d cancelled=%d",
            self.submitted,
            self.completed,
            self.failed,
            self.cancelled,
        )

    # -- public API ----------------------------------------------------------

    async def submit(
        self,
        task_path: Path,
        stem: str,
        priority: TaskPriority = TaskPriority.NORMAL,
    ) -> None:
        """Enqueue a task for execution."""
        if self._shutdown_flag:
            logger.warning("POOL SUBMIT REJECTED (shutting down): %s", stem)
            return

        self._seq += 1
        item = _QueueItem(
            priority=priority.value,
            sequence=self._seq,
            stem=stem,
            task_path=task_path,
        )
        await self._queue.put(item)
        self.submitted += 1
        logger.info(
            "POOL SUBMIT: %s priority=%s queue_depth=%d",
            stem,
            priority.name,
            self._queue.qsize(),
        )

    async def cancel_task(self, stem: str) -> bool:
        """Cancel a running task by stem. Returns True if found and cancelled."""
        task = self._active.get(stem)
        if task and not task.done():
            task.cancel()
            self.cancelled += 1
            logger.info("POOL CANCEL: %s", stem)
            return True
        return False

    def status(self) -> dict[str, Any]:
        """Return pool status snapshot."""
        return {
            "active_workers": self._max_workers - self._semaphore._value,
            "max_workers": self._max_workers,
            "queue_depth": self._queue.qsize(),
            "active_tasks": list(self._active.keys()),
            "submitted": self.submitted,
            "completed": self.completed,
            "failed": self.failed,
            "cancelled": self.cancelled,
        }

    def get_worker_statuses(self) -> list[WorkerStatus]:
        """Return list of active worker snapshots."""
        return list(self._active_meta.values())

    # -- internals -----------------------------------------------------------

    async def _consumer_loop(self) -> None:
        """Dequeue tasks in priority order and dispatch to workers."""
        while not self._shutdown_flag:
            try:
                item: _QueueItem = await asyncio.wait_for(
                    self._queue.get(),
                    timeout=1.0,
                )
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            # Wait for a free worker slot
            await self._semaphore.acquire()

            if self._shutdown_flag:
                self._semaphore.release()
                break

            # Record metadata
            now = datetime.now(timezone.utc).isoformat()
            self._active_meta[item.stem] = WorkerStatus(
                stem=item.stem,
                priority=TaskPriority(item.priority),
                started_at=now,
            )

            # Launch worker
            task = asyncio.create_task(
                self._run_worker(item.stem, item.task_path),
            )
            self._active[item.stem] = task

            logger.info(
                "POOL DISPATCH: %s priority=%s active=%d/%d",
                item.stem,
                TaskPriority(item.priority).name,
                self._max_workers - self._semaphore._value,
                self._max_workers,
            )

    async def _run_worker(self, stem: str, task_path: Path) -> None:
        """Execute the worker function with cleanup."""
        try:
            if self._worker_fn:
                await self._worker_fn(task_path)
            self.completed += 1
        except asyncio.CancelledError:
            self.cancelled += 1
            logger.info("POOL WORKER CANCELLED: %s", stem)
            raise
        except Exception:
            self.failed += 1
            logger.exception("POOL WORKER FAILED: %s", stem)
        finally:
            self._semaphore.release()
            self._active.pop(stem, None)
            self._active_meta.pop(stem, None)
            logger.info(
                "POOL WORKER DONE: %s active=%d/%d queue=%d",
                stem,
                self._max_workers - self._semaphore._value,
                self._max_workers,
                self._queue.qsize(),
            )
