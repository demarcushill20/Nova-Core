"""Tests for SimpleCircuitBreaker enhancements (Phase 6B Step 6.10).

Covers:
  A1 — Rolling-window failure counting
  A2 — Excluded exceptions
  A3 — Async wrapper (acall)
  A4 — on_open callback
  + Backward compatibility
"""

import asyncio
import time
from unittest.mock import MagicMock

import pytest

from agents.circuit_breakers import CircuitBreakerError, SimpleCircuitBreaker

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_cb(**kwargs):
    """Create a breaker with no Redis and short defaults for testing."""
    kwargs.setdefault("name", "test")
    kwargs.setdefault("failure_threshold", 3)
    kwargs.setdefault("reset_timeout_seconds", 0.2)
    kwargs.setdefault("redis_client", False)  # skip Redis probe
    return SimpleCircuitBreaker(**kwargs)


def _fail(msg="boom"):
    raise RuntimeError(msg)


async def _async_ok(val=42):
    return val


async def _async_fail(msg="async boom"):
    raise RuntimeError(msg)


# ---------------------------------------------------------------------------
# A1: Rolling-window failure counting
# ---------------------------------------------------------------------------


class TestRollingWindow:
    def test_failures_within_window_trip_breaker(self):
        cb = _make_cb(failure_threshold=3, window_seconds=5.0, window_size=10)
        for _ in range(3):
            cb.record_failure()
        assert cb.state == "OPEN"

    def test_old_failures_outside_window_not_counted(self):
        """Failures older than window_seconds should not count."""
        cb = _make_cb(failure_threshold=3, window_seconds=0.1, window_size=10)
        # Record 2 failures, wait for them to expire, then add 1 more
        cb.record_failure()
        cb.record_failure()
        time.sleep(0.15)
        cb.record_failure()
        # Only 1 failure is within the window -> should stay CLOSED
        assert cb.state == "CLOSED"
        assert cb.failure_count == 1

    def test_single_success_does_not_reset_window(self):
        """Unlike consecutive counting, one success amid failures should NOT
        clear the failure count if failures are still within the window."""
        cb = _make_cb(failure_threshold=3, window_seconds=5.0, window_size=20)
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        # 2 failures still in window
        assert cb.failure_count == 2
        # One more failure should trip it
        cb.record_failure()
        assert cb.state == "OPEN"

    def test_failure_count_property(self):
        cb = _make_cb(failure_threshold=5, window_seconds=5.0, window_size=10)
        cb.record_failure()
        cb.record_success()
        cb.record_failure()
        assert cb.failure_count == 2

    def test_window_size_caps_events(self):
        """Deque maxlen should cap the number of events stored."""
        cb = _make_cb(failure_threshold=100, window_seconds=60.0, window_size=5)
        for _ in range(10):
            cb.record_failure()
        # Only last 5 events kept
        assert len(cb._window) == 5


# ---------------------------------------------------------------------------
# A2: Excluded exceptions
# ---------------------------------------------------------------------------


class TestExcludedExceptions:
    def test_excluded_exception_not_counted(self):
        cb = _make_cb(failure_threshold=3, excluded_exceptions=(ValueError, KeyError))

        def raise_val():
            raise ValueError("bad input")

        for _ in range(5):
            with pytest.raises(ValueError):
                cb.call(raise_val)
        # None of those should have counted
        assert cb.failure_count == 0
        assert cb.state == "CLOSED"

    def test_excluded_exception_still_reraised(self):
        cb = _make_cb(excluded_exceptions=(ValueError,))

        def raise_val():
            raise ValueError("bad")

        with pytest.raises(ValueError, match="bad"):
            cb.call(raise_val)

    def test_non_excluded_exception_counted(self):
        cb = _make_cb(failure_threshold=2, excluded_exceptions=(ValueError,))
        for _ in range(2):
            with pytest.raises(RuntimeError):
                cb.call(_fail)
        assert cb.state == "OPEN"

    def test_excluded_via_call_does_not_trip(self):
        """Excluded exceptions through call() should not trip breaker."""
        cb = _make_cb(
            failure_threshold=1,
            excluded_exceptions=(KeyError,),
        )

        def raise_key_error():
            raise KeyError("missing")

        for _ in range(5):
            with pytest.raises(KeyError):
                cb.call(raise_key_error)
        assert cb.state == "CLOSED"
        assert cb.failure_count == 0


# ---------------------------------------------------------------------------
# A3: Async wrapper (acall)
# ---------------------------------------------------------------------------


class TestAcall:
    @pytest.fixture(autouse=True)
    def _fresh_loop(self):
        """Ensure a fresh event loop for each async test."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        yield loop
        loop.close()

    def test_acall_success(self, _fresh_loop):
        cb = _make_cb()
        result = _fresh_loop.run_until_complete(cb.acall(_async_ok, val=99))
        assert result == 99
        assert cb.state == "CLOSED"

    def test_acall_failure_trips_breaker(self, _fresh_loop):
        cb = _make_cb(failure_threshold=2)
        for _ in range(2):
            with pytest.raises(RuntimeError, match="async boom"):
                _fresh_loop.run_until_complete(cb.acall(_async_fail))
        assert cb.state == "OPEN"

    def test_acall_rejects_when_open(self, _fresh_loop):
        cb = _make_cb(failure_threshold=1)
        with pytest.raises(RuntimeError):
            _fresh_loop.run_until_complete(cb.acall(_async_fail))
        assert cb.state == "OPEN"
        with pytest.raises(CircuitBreakerError):
            _fresh_loop.run_until_complete(cb.acall(_async_ok))

    def test_acall_excluded_exception(self, _fresh_loop):
        cb = _make_cb(
            failure_threshold=1,
            excluded_exceptions=(ValueError,),
        )

        async def raise_value():
            raise ValueError("async bad input")

        with pytest.raises(ValueError):
            _fresh_loop.run_until_complete(cb.acall(raise_value))
        assert cb.state == "CLOSED"
        assert cb.failure_count == 0


# ---------------------------------------------------------------------------
# A4: on_open callback
# ---------------------------------------------------------------------------


class TestOnOpenCallback:
    def test_on_open_fires_on_transition(self):
        callback = MagicMock()
        cb = _make_cb(failure_threshold=2, on_open=callback)
        cb.record_failure()
        callback.assert_not_called()
        cb.record_failure()
        callback.assert_called_once_with("test")

    def test_on_open_does_not_fire_when_already_open(self):
        """Repeated failures after OPEN should NOT re-fire on_open."""
        callback = MagicMock()
        cb = _make_cb(failure_threshold=2, on_open=callback, window_size=20)
        for _ in range(5):
            cb.record_failure()
        # Only called once for the initial transition
        callback.assert_called_once_with("test")

    def test_on_open_exception_does_not_break_breaker(self):
        def bad_callback(name):
            raise RuntimeError("callback exploded")

        cb = _make_cb(failure_threshold=1, on_open=bad_callback)
        # Should not raise despite callback blowing up
        cb.record_failure()
        assert cb.state == "OPEN"


# ---------------------------------------------------------------------------
# Backward compatibility
# ---------------------------------------------------------------------------


class TestBackwardCompat:
    def test_default_params_work(self):
        """Breaker with no new params should behave like original."""
        cb = SimpleCircuitBreaker(
            name="compat",
            failure_threshold=3,
            reset_timeout_seconds=0.1,
            redis_client=False,
        )
        # Old-style: 3 failures trip it
        for _ in range(3):
            cb.record_failure()
        assert cb.state == "OPEN"
        assert cb.trip_count == 1

    def test_call_success_and_failure(self):
        cb = _make_cb(failure_threshold=2)
        assert cb.call(lambda: "ok") == "ok"
        with pytest.raises(RuntimeError):
            cb.call(_fail)
        with pytest.raises(RuntimeError):
            cb.call(_fail)
        assert cb.state == "OPEN"
        with pytest.raises(CircuitBreakerError):
            cb.call(lambda: "ok")

    def test_half_open_transition(self):
        cb = _make_cb(failure_threshold=1, reset_timeout_seconds=0.05)
        cb.record_failure()
        assert cb.state == "OPEN"
        time.sleep(0.1)
        assert cb.state == "HALF_OPEN"
        cb.record_success()
        assert cb.state == "CLOSED"
