"""Tests for P11.5 — Timeout, Cancellation and Error Recovery."""

from __future__ import annotations

import time

from agents.timeout_manager import (
    CompensationEntry,
    CompensationLog,
    RetryConfig,
    RetryStormDetector,
    RoleCircuitBreaker,
    TimeoutManager,
)


class TestRetryConfig:
    def test_default_retryable(self):
        rc = RetryConfig()
        assert rc.is_retryable("TimeoutError")
        assert rc.is_retryable("ConnectionError")
        assert not rc.is_retryable("ValueError")

    def test_delay_exponential(self):
        rc = RetryConfig(base_delay_s=1.0, jitter_factor=0.0)
        assert rc.delay_for_attempt(0) == 1.0
        assert rc.delay_for_attempt(1) == 2.0
        assert rc.delay_for_attempt(2) == 4.0

    def test_delay_max_cap(self):
        rc = RetryConfig(base_delay_s=1.0, max_delay_s=5.0, jitter_factor=0.0)
        assert rc.delay_for_attempt(10) == 5.0

    def test_delay_with_jitter(self):
        rc = RetryConfig(base_delay_s=1.0, jitter_factor=1.0)
        delays = {rc.delay_for_attempt(0) for _ in range(20)}
        # With full jitter, we should get varied delays
        assert len(delays) > 1


class TestCircuitBreaker:
    def test_starts_closed(self):
        cb = RoleCircuitBreaker(role="coder")
        assert not cb.is_open

    def test_opens_on_failures(self):
        cb = RoleCircuitBreaker(role="coder", failure_threshold=0.5, min_calls=3)
        cb.record_failure()
        cb.record_failure()
        cb.record_failure()
        assert cb.is_open

    def test_stays_closed_below_threshold(self):
        cb = RoleCircuitBreaker(role="coder", failure_threshold=0.5, min_calls=3)
        cb.record_success()
        cb.record_success()
        cb.record_failure()
        assert not cb.is_open

    def test_stays_closed_below_min_calls(self):
        cb = RoleCircuitBreaker(role="coder", failure_threshold=0.5, min_calls=5)
        cb.record_failure()
        cb.record_failure()
        assert not cb.is_open  # only 2 calls, need 5

    def test_failure_rate(self):
        cb = RoleCircuitBreaker(role="coder")
        cb.record_success()
        cb.record_failure()
        assert cb.failure_rate == 0.5

    def test_to_dict(self):
        cb = RoleCircuitBreaker(role="coder")
        cb.record_success()
        d = cb.to_dict()
        assert d["role"] == "coder"
        assert d["successes"] == 1


class TestRetryStormDetector:
    def test_no_storm_initially(self):
        d = RetryStormDetector()
        assert not d.is_storm

    def test_storm_detected(self):
        d = RetryStormDetector(max_retries_in_window=5)
        for _ in range(5):
            d.record_retry()
        assert d.is_storm

    def test_below_threshold(self):
        d = RetryStormDetector(max_retries_in_window=10)
        for _ in range(5):
            d.record_retry()
        assert not d.is_storm


class TestCompensationLog:
    def test_record_and_get(self, tmp_path):
        log = CompensationLog(persist_dir=tmp_path / "comp")
        entry = CompensationEntry(
            workflow_id="wf-1",
            node_id="n1",
            action="wrote file X",
            rollback_instruction="delete file X",
        )
        log.record(entry)
        entries = log.get_entries("wf-1")
        assert len(entries) == 1
        assert entries[0].action == "wrote file X"

    def test_persistence(self, tmp_path):
        log = CompensationLog(persist_dir=tmp_path / "comp")
        entry = CompensationEntry(
            workflow_id="wf-1",
            node_id="n1",
            action="did thing",
            rollback_instruction="undo thing",
        )
        log.record(entry)
        # Load from disk
        loaded = log.load("wf-1")
        assert len(loaded) == 1
        assert loaded[0].action == "did thing"

    def test_filter_by_workflow(self, tmp_path):
        log = CompensationLog(persist_dir=tmp_path / "comp")
        log.record(CompensationEntry(workflow_id="wf-1", node_id="n1", action="a", rollback_instruction="r"))
        log.record(CompensationEntry(workflow_id="wf-2", node_id="n2", action="b", rollback_instruction="r"))
        assert len(log.get_entries("wf-1")) == 1
        assert len(log.get_entries("wf-2")) == 1


class TestTimeoutManager:
    def test_should_retry_basic(self):
        tm = TimeoutManager()
        assert tm.should_retry("coder", "TimeoutError", attempt=0)
        assert not tm.should_retry("coder", "ValueError", attempt=0)

    def test_should_not_retry_max_attempts(self):
        tm = TimeoutManager(retry_config=RetryConfig(max_retries=2))
        assert tm.should_retry("coder", "TimeoutError", attempt=0)
        assert tm.should_retry("coder", "TimeoutError", attempt=1)
        assert not tm.should_retry("coder", "TimeoutError", attempt=2)

    def test_should_not_retry_circuit_open(self):
        tm = TimeoutManager()
        breaker = tm.get_breaker("coder")
        breaker._is_open = True
        breaker._opened_at = time.time()
        assert not tm.should_retry("coder", "TimeoutError", attempt=0)

    def test_should_not_retry_storm(self):
        tm = TimeoutManager()
        tm._storm_detector = RetryStormDetector(max_retries_in_window=3)
        for _ in range(3):
            tm.record_failure("coder")
        assert not tm.should_retry("coder", "TimeoutError", attempt=0)

    def test_record_success_and_failure(self):
        tm = TimeoutManager()
        tm.record_success("coder")
        tm.record_failure("coder")
        assert tm.get_breaker("coder").failure_rate == 0.5

    def test_get_metrics(self):
        tm = TimeoutManager()
        tm.record_success("coder")
        metrics = tm.get_metrics()
        assert "breakers" in metrics
        assert "retry_storm" in metrics
        assert metrics["retry_storm"] is False

    def test_compensation_log_accessible(self, tmp_path):
        tm = TimeoutManager(compensation_dir=tmp_path / "comp")
        tm.compensation_log.record(
            CompensationEntry(
                workflow_id="wf-1",
                node_id="n1",
                action="x",
                rollback_instruction="y",
            )
        )
        assert len(tm.compensation_log.get_entries("wf-1")) == 1
