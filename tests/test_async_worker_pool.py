"""Tests for Phase 4.3 — Async Worker Pool.

Tests cover: concurrency, priority ordering, cancellation, pool limits, and load.
"""

import asyncio
import time
from pathlib import Path

import pytest

from utils.async_worker_pool import (
    AsyncWorkerPool,
    TaskPriority,
    WorkerStatus,
    parse_task_priority,
)

# ---------------------------------------------------------------------------
# Priority parsing tests
# ---------------------------------------------------------------------------


class TestParseTaskPriority:
    def test_high_priority(self):
        text = "---\npriority: high\n---\n# Task"
        assert parse_task_priority(text) == TaskPriority.HIGH

    def test_critical_priority(self):
        text = "---\npriority: critical\n---\n# Task"
        assert parse_task_priority(text) == TaskPriority.CRITICAL

    def test_low_priority(self):
        text = "---\npriority: low\n---\n# Task"
        assert parse_task_priority(text) == TaskPriority.LOW

    def test_normal_explicit(self):
        text = "---\npriority: normal\n---\n# Task"
        assert parse_task_priority(text) == TaskPriority.NORMAL

    def test_no_frontmatter(self):
        text = "# Just a task with no frontmatter"
        assert parse_task_priority(text) == TaskPriority.NORMAL

    def test_frontmatter_without_priority(self):
        text = "---\nauthor: nova\n---\n# Task"
        assert parse_task_priority(text) == TaskPriority.NORMAL

    def test_case_insensitive(self):
        text = "---\npriority: HIGH\n---\n# Task"
        assert parse_task_priority(text) == TaskPriority.HIGH

    def test_unknown_priority_defaults_normal(self):
        text = "---\npriority: urgent\n---\n# Task"
        assert parse_task_priority(text) == TaskPriority.NORMAL

    def test_priority_with_extra_fields(self):
        text = "---\nauthor: nova\npriority: critical\ntags: [deploy]\n---\n# Task"
        assert parse_task_priority(text) == TaskPriority.CRITICAL


class TestTaskPriority:
    def test_ordering(self):
        assert TaskPriority.CRITICAL < TaskPriority.HIGH
        assert TaskPriority.HIGH < TaskPriority.NORMAL
        assert TaskPriority.NORMAL < TaskPriority.LOW

    def test_from_string(self):
        assert TaskPriority.from_string("critical") == TaskPriority.CRITICAL
        assert TaskPriority.from_string("HIGH") == TaskPriority.HIGH
        assert TaskPriority.from_string("unknown") == TaskPriority.NORMAL


# ---------------------------------------------------------------------------
# Async Worker Pool tests
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_tasks(tmp_path):
    """Create temporary task files for testing."""
    tasks = []
    for i in range(10):
        p = tmp_path / f"task_{i:04d}.md"
        p.write_text(f"# Task {i}\nDo thing {i}\n")
        tasks.append(p)
    return tasks


class TestAsyncWorkerPoolConcurrency:
    """Test that the pool runs exactly max_workers tasks concurrently."""

    @pytest.mark.asyncio
    async def test_two_tasks_run_in_parallel(self):
        """Two tasks should execute concurrently with max_workers=2."""
        timestamps: list[tuple[str, float, str]] = []  # (stem, time, event)

        async def mock_worker(task_path: Path):
            stem = task_path.stem
            timestamps.append((stem, time.monotonic(), "start"))
            await asyncio.sleep(0.2)
            timestamps.append((stem, time.monotonic(), "end"))

        pool = AsyncWorkerPool(max_workers=2, worker_fn=mock_worker)
        pool.start()

        task_a = Path("/tmp/task_a.md")
        task_b = Path("/tmp/task_b.md")

        await pool.submit(task_a, stem="task_a")
        await pool.submit(task_b, stem="task_b")

        # Wait for completion
        await asyncio.sleep(0.5)
        await pool.shutdown(timeout=5)

        # Both should have started before either ended
        starts = [(s, t) for s, t, e in timestamps if e == "start"]
        ends = [(s, t) for s, t, e in timestamps if e == "end"]

        assert len(starts) == 2, f"Expected 2 starts, got {len(starts)}"
        assert len(ends) == 2, f"Expected 2 completions, got {len(ends)}"

        # Both started before the first end
        latest_start = max(t for _, t in starts)
        earliest_end = min(t for _, t in ends)
        assert latest_start < earliest_end, "Tasks should overlap in execution"

    @pytest.mark.asyncio
    async def test_pool_limit_enforced(self):
        """With max_workers=2, only 2 tasks should run concurrently."""
        concurrent_count = 0
        max_concurrent = 0
        lock = asyncio.Lock()

        async def counting_worker(task_path: Path):
            nonlocal concurrent_count, max_concurrent
            async with lock:
                concurrent_count += 1
                max_concurrent = max(max_concurrent, concurrent_count)
            await asyncio.sleep(0.1)
            async with lock:
                concurrent_count -= 1

        pool = AsyncWorkerPool(max_workers=2, worker_fn=counting_worker)
        pool.start()

        for i in range(6):
            await pool.submit(Path(f"/tmp/task_{i}.md"), stem=f"task_{i}")

        await asyncio.sleep(1.0)
        await pool.shutdown(timeout=5)

        assert max_concurrent == 2, f"Max concurrent should be 2, got {max_concurrent}"
        assert pool.completed == 6, f"All 6 tasks should complete, got {pool.completed}"


class TestAsyncWorkerPoolPriority:
    """Test priority queue ordering."""

    @pytest.mark.asyncio
    async def test_priority_ordering(self):
        """Higher priority tasks should execute before lower priority ones."""
        execution_order: list[str] = []
        gate = asyncio.Event()

        async def order_tracking_worker(task_path: Path):
            await gate.wait()  # block until all tasks are queued
            execution_order.append(task_path.stem)
            await asyncio.sleep(0.05)

        # max_workers=1 to force sequential execution and test ordering
        pool = AsyncWorkerPool(max_workers=1, worker_fn=order_tracking_worker)
        pool.start()

        # Submit in reverse priority order
        await pool.submit(Path("/tmp/low.md"), stem="low", priority=TaskPriority.LOW)
        await pool.submit(Path("/tmp/normal.md"), stem="normal", priority=TaskPriority.NORMAL)
        await pool.submit(Path("/tmp/critical.md"), stem="critical", priority=TaskPriority.CRITICAL)
        await pool.submit(Path("/tmp/high.md"), stem="high", priority=TaskPriority.HIGH)

        # Let consumer dequeue one item before opening gate
        await asyncio.sleep(0.1)
        gate.set()

        await asyncio.sleep(1.0)
        await pool.shutdown(timeout=5)

        # All tasks were queued before processing started, so execution
        # should follow strict priority order
        assert len(execution_order) == 4, f"All 4 tasks should run, got {execution_order}"
        assert execution_order == ["critical", "high", "normal", "low"], f"Priority order wrong: {execution_order}"


class TestAsyncWorkerPoolCancellation:
    """Test graceful task cancellation."""

    @pytest.mark.asyncio
    async def test_cancel_running_task(self):
        """Cancelling a running task should stop it."""
        started = asyncio.Event()
        cancelled = False

        async def long_worker(task_path: Path):
            nonlocal cancelled
            started.set()
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled = True
                raise

        pool = AsyncWorkerPool(max_workers=2, worker_fn=long_worker)
        pool.start()

        await pool.submit(Path("/tmp/long_task.md"), stem="long_task")

        # Wait for task to start
        await asyncio.wait_for(started.wait(), timeout=2.0)

        # Cancel it
        result = await pool.cancel_task("long_task")
        assert result is True, "Cancel should return True for running task"

        await asyncio.sleep(0.2)
        assert cancelled, "Task should have received CancelledError"

        await pool.shutdown(timeout=5)
        assert pool.cancelled >= 1

    @pytest.mark.asyncio
    async def test_cancel_nonexistent_task(self):
        """Cancelling a non-existent task should return False."""
        pool = AsyncWorkerPool(max_workers=2, worker_fn=None)
        pool.start()

        result = await pool.cancel_task("does_not_exist")
        assert result is False

        await pool.shutdown(timeout=5)

    @pytest.mark.asyncio
    async def test_graceful_shutdown(self):
        """Shutdown should wait for active tasks then stop."""
        completed = []

        async def slow_worker(task_path: Path):
            await asyncio.sleep(0.3)
            completed.append(task_path.stem)

        pool = AsyncWorkerPool(max_workers=2, worker_fn=slow_worker)
        pool.start()

        await pool.submit(Path("/tmp/task_1.md"), stem="task_1")
        await pool.submit(Path("/tmp/task_2.md"), stem="task_2")

        await asyncio.sleep(0.1)  # let tasks start
        await pool.shutdown(timeout=5)

        assert len(completed) == 2, f"Both tasks should complete during shutdown, got {completed}"


class TestAsyncWorkerPoolLoad:
    """Load test with many queued tasks."""

    @pytest.mark.asyncio
    async def test_ten_tasks_two_at_a_time(self):
        """10 tasks should all complete with max 2 running at once."""
        concurrent_count = 0
        max_concurrent = 0
        completed_count = 0
        lock = asyncio.Lock()

        async def load_worker(task_path: Path):
            nonlocal concurrent_count, max_concurrent, completed_count
            async with lock:
                concurrent_count += 1
                max_concurrent = max(max_concurrent, concurrent_count)
            await asyncio.sleep(0.05)
            async with lock:
                concurrent_count -= 1
                completed_count += 1

        pool = AsyncWorkerPool(max_workers=2, worker_fn=load_worker)
        pool.start()

        for i in range(10):
            await pool.submit(Path(f"/tmp/load_{i}.md"), stem=f"load_{i}")

        await asyncio.sleep(2.0)
        await pool.shutdown(timeout=5)

        assert max_concurrent <= 2, f"Never more than 2 concurrent, got {max_concurrent}"
        assert completed_count == 10, f"All 10 should complete, got {completed_count}"
        assert pool.completed == 10

    @pytest.mark.asyncio
    async def test_mixed_priority_load(self):
        """Mixed priority tasks should respect ordering under load."""
        execution_order: list[tuple[str, int]] = []
        lock = asyncio.Lock()
        gate = asyncio.Event()

        async def tracking_worker(task_path: Path):
            await gate.wait()
            async with lock:
                execution_order.append((task_path.stem, len(execution_order)))
            await asyncio.sleep(0.05)

        pool = AsyncWorkerPool(max_workers=1, worker_fn=tracking_worker)
        pool.start()

        # Submit a batch with mixed priorities
        await pool.submit(Path("/tmp/low_1.md"), stem="low_1", priority=TaskPriority.LOW)
        await pool.submit(Path("/tmp/crit.md"), stem="crit", priority=TaskPriority.CRITICAL)
        await pool.submit(Path("/tmp/norm.md"), stem="norm", priority=TaskPriority.NORMAL)
        await pool.submit(Path("/tmp/high_1.md"), stem="high_1", priority=TaskPriority.HIGH)
        await pool.submit(Path("/tmp/low_2.md"), stem="low_2", priority=TaskPriority.LOW)

        await asyncio.sleep(0.1)
        gate.set()

        await asyncio.sleep(1.5)
        await pool.shutdown(timeout=5)

        assert len(execution_order) == 5
        stems = [s for s, _ in execution_order]
        # All queued before processing, so strict priority order expected
        assert stems == ["crit", "high_1", "norm", "low_1", "low_2"], f"Priority order wrong: {stems}"


class TestAsyncWorkerPoolStatus:
    """Test status and observability."""

    @pytest.mark.asyncio
    async def test_status_reporting(self):
        """Pool status should reflect current state."""
        started = asyncio.Event()

        async def blocking_worker(task_path: Path):
            started.set()
            await asyncio.sleep(5)

        pool = AsyncWorkerPool(max_workers=2, worker_fn=blocking_worker)
        pool.start()

        # Initial status
        st = pool.status()
        assert st["active_workers"] == 0
        assert st["max_workers"] == 2
        assert st["queue_depth"] == 0

        await pool.submit(Path("/tmp/s1.md"), stem="s1")
        await asyncio.wait_for(started.wait(), timeout=2.0)

        st = pool.status()
        assert st["active_workers"] == 1
        assert "s1" in st["active_tasks"]
        assert st["submitted"] == 1

        # Worker statuses
        workers = pool.get_worker_statuses()
        assert len(workers) == 1
        assert workers[0].stem == "s1"
        assert isinstance(workers[0], WorkerStatus)

        await pool.shutdown(timeout=1)

    @pytest.mark.asyncio
    async def test_submit_rejected_after_shutdown(self):
        """Submitting after shutdown flag is set should be rejected."""
        pool = AsyncWorkerPool(max_workers=2, worker_fn=None)
        pool.start()
        await pool.shutdown(timeout=1)

        # This should be silently rejected
        await pool.submit(Path("/tmp/late.md"), stem="late")
        assert pool.submitted == 0  # not counted
