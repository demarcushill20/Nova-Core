"""Concurrent thread safety tests for Phase 6B/6C modules.

M2 fix: validates thread safety of task_checkpoint, error_catalog,
and circuit breakers under concurrent access.
"""

from __future__ import annotations

import threading
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# task_checkpoint: concurrent update_status + increment_retry (H1 fix)
# ---------------------------------------------------------------------------


@pytest.fixture()
def checkpoint_dir(tmp_path, monkeypatch):
    """Redirect checkpoint storage to a temp directory."""
    cp_dir = tmp_path / "checkpoints"
    cp_dir.mkdir()
    import utils.task_checkpoint as tc

    monkeypatch.setattr(tc, "CHECKPOINT_DIR", cp_dir)
    return cp_dir


class TestCheckpointThreadSafety:
    """Verify that update_status and increment_retry are atomic under concurrency."""

    def test_concurrent_increment_retry(self, checkpoint_dir):
        from utils.task_checkpoint import TaskCheckpoint, increment_retry, load_checkpoint, save_checkpoint

        # Create initial checkpoint with retry_count=0
        cp = TaskCheckpoint(
            task_id="test_concurrent",
            task_file="TASKS/test_concurrent.md",
            status="dispatched",
            started_at="2026-01-01T00:00:00Z",
            last_updated="2026-01-01T00:00:00Z",
            retry_count=0,
        )
        save_checkpoint(cp)

        n_threads = 10
        barrier = threading.Barrier(n_threads)
        errors: list[str] = []

        def _increment():
            try:
                barrier.wait(timeout=5)
                increment_retry("test_concurrent")
            except Exception as exc:
                errors.append(str(exc))

        threads = [threading.Thread(target=_increment) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"Thread errors: {errors}"
        final = load_checkpoint("test_concurrent")
        assert final is not None
        # Each thread incremented once — should be exactly n_threads
        assert final.retry_count == n_threads

    def test_concurrent_update_status(self, checkpoint_dir):
        from utils.task_checkpoint import TaskCheckpoint, load_checkpoint, save_checkpoint, update_status

        cp = TaskCheckpoint(
            task_id="test_status",
            task_file="TASKS/test_status.md",
            status="dispatched",
            started_at="2026-01-01T00:00:00Z",
            last_updated="2026-01-01T00:00:00Z",
        )
        save_checkpoint(cp)

        n_threads = 10
        barrier = threading.Barrier(n_threads)
        errors: list[str] = []

        def _update(idx):
            try:
                barrier.wait(timeout=5)
                update_status("test_status", f"in_progress_{idx}", partial_output=f"output_{idx}")
            except Exception as exc:
                errors.append(str(exc))

        threads = [threading.Thread(target=_update, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors
        final = load_checkpoint("test_status")
        assert final is not None
        # Status and partial_output should be from the same thread (atomic update)
        assert final.status.startswith("in_progress_")
        idx = final.status.split("_")[-1]
        assert final.partial_output == f"output_{idx}"


# ---------------------------------------------------------------------------
# error_catalog: concurrent register_entry (M5 fix)
# ---------------------------------------------------------------------------


class TestErrorCatalogThreadSafety:
    """Verify catalog thread safety under concurrent registration."""

    def test_concurrent_register(self):
        from utils.error_catalog import ErrorEntry, get_catalog, register_entry, reset_catalog

        reset_catalog()
        initial_size = len(get_catalog())

        n_threads = 20
        barrier = threading.Barrier(n_threads)
        errors: list[str] = []

        def _register(idx):
            try:
                barrier.wait(timeout=5)
                entry = ErrorEntry(
                    code=f"E-TEST-{idx:03d}",
                    category="transient",
                    pattern=f"test_pattern_{idx}",
                    recommended_action="retry",
                    max_retries=1,
                    backoff_base=1.0,
                )
                register_entry(entry)
            except Exception as exc:
                errors.append(str(exc))

        threads = [threading.Thread(target=_register, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors
        final = get_catalog()
        assert len(final) == initial_size + n_threads
        reset_catalog()

    def test_catalog_size_limit(self):
        from utils.error_catalog import MAX_CATALOG_SIZE, ErrorEntry, register_entry, reset_catalog

        reset_catalog()
        # Fill up to limit
        for i in range(MAX_CATALOG_SIZE - 12):  # 12 built-in entries
            entry = ErrorEntry(
                code=f"E-FILL-{i:03d}",
                category="transient",
                pattern=f"fill_{i}",
                recommended_action="retry",
                max_retries=1,
                backoff_base=1.0,
            )
            register_entry(entry)

        # Next registration should raise
        with pytest.raises(ValueError, match="catalog full"):
            register_entry(
                ErrorEntry(
                    code="E-OVERFLOW",
                    category="transient",
                    pattern="overflow",
                    recommended_action="retry",
                    max_retries=1,
                    backoff_base=1.0,
                )
            )
        reset_catalog()


# ---------------------------------------------------------------------------
# circuit_breakers: concurrent record_failure/record_success
# ---------------------------------------------------------------------------


class TestBreakerThreadSafety:
    """Verify circuit breaker state consistency under concurrent access."""

    def test_concurrent_failures(self):
        from agents.circuit_breakers import SimpleCircuitBreaker

        n_threads = 20
        failures_per_thread = 3
        total_failures = n_threads * failures_per_thread

        cb = SimpleCircuitBreaker(
            name="test_concurrent",
            failure_threshold=total_failures + 1,  # don't trip
            reset_timeout_seconds=60.0,
            window_size=total_failures + 10,  # large enough window
            redis_client=None,
        )
        cb._redis = None

        barrier = threading.Barrier(n_threads)
        errors: list[str] = []

        def _fail():
            try:
                barrier.wait(timeout=5)
                for _ in range(failures_per_thread):
                    cb.record_failure()
            except Exception as exc:
                errors.append(str(exc))

        threads = [threading.Thread(target=_fail) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors
        # All failures should be counted in the window
        assert cb.failure_count == total_failures

    def test_is_open_property(self):
        from agents.circuit_breakers import SimpleCircuitBreaker

        cb = SimpleCircuitBreaker(
            name="test_is_open",
            failure_threshold=2,
            reset_timeout_seconds=60.0,
            redis_client=None,
        )
        cb._redis = None

        assert not cb.is_open
        cb.record_failure()
        cb.record_failure()
        assert cb.is_open

    def test_callback_fires_outside_lock(self):
        """H4: Verify on_open callback fires without holding the breaker's lock."""
        from agents.circuit_breakers import SimpleCircuitBreaker

        lock_was_free = []

        def _check_lock(name):
            # If the lock is free, we can acquire it
            acquired = cb._lock.acquire(blocking=False)
            lock_was_free.append(acquired)
            if acquired:
                cb._lock.release()

        cb = SimpleCircuitBreaker(
            name="test_callback_lock",
            failure_threshold=2,
            reset_timeout_seconds=60.0,
            on_open=_check_lock,
            redis_client=None,
        )
        cb._redis = None

        cb.record_failure()
        cb.record_failure()  # triggers OPEN + callback

        assert len(lock_was_free) == 1
        assert lock_was_free[0] is True, "on_open callback was called while lock was held"


# ---------------------------------------------------------------------------
# kill switch: is_novatrade_task classifier (H7 fix)
# ---------------------------------------------------------------------------


class TestNovATradeClassifier:
    """H7: Verify reduced false positives in task classification."""

    def test_explicit_keywords_match(self):
        from nova_kill_switch import is_novatrade_task

        assert is_novatrade_task("deploy_novatrade_v2")
        assert is_novatrade_task("ftmo_challenge_setup")
        assert is_novatrade_task("metaapi_reconnect")

    def test_generic_single_keyword_no_match(self):
        """Single generic keyword should NOT classify as NovaTrade."""
        from nova_kill_switch import is_novatrade_task

        assert not is_novatrade_task("Position paper at 2pm")
        assert not is_novatrade_task("signal handler cleanup")
        assert not is_novatrade_task("update order of operations in docs")
        assert not is_novatrade_task("webhook refactor for notifications")

    def test_generic_multi_keyword_match(self):
        """Two generic keywords together should classify as NovaTrade."""
        from nova_kill_switch import is_novatrade_task

        assert is_novatrade_task("trade signal analysis")
        assert is_novatrade_task("position order management")

    def test_domain_keyword_match(self):
        from nova_kill_switch import is_novatrade_task

        assert is_novatrade_task("forex session check")
        assert is_novatrade_task("drawdown monitoring")


# ---------------------------------------------------------------------------
# dead man's switch fail-closed (H3 fix)
# ---------------------------------------------------------------------------


class TestDeadManSwitch:
    """H3: DMS should return False (fail closed) when Redis is unreachable."""

    def test_dms_fails_closed_on_redis_error(self):
        """When Redis raises on .get(), DMS should return False."""
        import redis as redis_mod

        from nova_kill_switch import check_dead_man_switch

        mock_client = type(
            "MockRedis",
            (),
            {"get": staticmethod(lambda *a, **kw: (_ for _ in ()).throw(ConnectionError("Redis down")))},
        )()

        with patch.object(redis_mod, "Redis", return_value=mock_client):
            result = check_dead_man_switch()
            assert result is False

    def test_dms_fails_closed_on_import_error(self):
        """When redis module is unavailable, DMS should return False."""
        import builtins

        from nova_kill_switch import check_dead_man_switch

        original_import = builtins.__import__

        def _fail_redis(name, *args, **kwargs):
            if name == "redis":
                raise ImportError("no redis")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=_fail_redis):
            result = check_dead_man_switch()
            assert result is False
