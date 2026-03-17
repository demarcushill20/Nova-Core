"""Tests for utils/self_healing.py — Phase 6A self-healing runtime.

Covers:
  - Error classification (transient, timeout, resource, permanent, unknown)
  - Error recording and summary
  - Memory monitoring
  - Dead man's switch
  - Degradation tier management
  - Feature gating per tier
  - check_self_healing() heartbeat integration
"""

import time

from utils.self_healing import (
    DegradationState,
    DegradationTier,
    ErrorCategory,
    MemorySnapshot,
    _load_degradation_state,
    _save_degradation_state,
    check_dead_man_switch,
    check_self_healing,
    classify_error,
    evaluate_degradation,
    get_error_summary,
    get_memory_snapshot,
    get_memory_trend,
    record_error,
    record_memory_snapshot,
    should_allow_feature,
    touch_dead_man_switch,
)

# ---------------------------------------------------------------------------
# Error Classifier
# ---------------------------------------------------------------------------


class TestClassifyError:
    def test_transient_connection_reset(self):
        result = classify_error("Connection reset by peer")
        assert result.category == ErrorCategory.TRANSIENT
        assert result.should_retry is True
        assert result.max_retries == 3

    def test_transient_503(self):
        result = classify_error("HTTP 503 Service Unavailable")
        assert result.category == ErrorCategory.TRANSIENT

    def test_timeout_error(self):
        result = classify_error("asyncio.TimeoutError: operation timed out")
        assert result.category == ErrorCategory.TIMEOUT
        assert result.should_retry is True
        assert result.max_retries == 2
        assert result.backoff_seconds == 30.0

    def test_timeout_deadline(self):
        result = classify_error("deadline exceeded for RPC call")
        assert result.category == ErrorCategory.TIMEOUT

    def test_resource_oom(self):
        result = classify_error("MemoryError: out of memory")
        assert result.category == ErrorCategory.RESOURCE
        assert result.should_retry is True
        assert result.max_retries == 1
        assert result.backoff_seconds == 60.0

    def test_resource_rate_limit(self):
        result = classify_error("429 Too Many Requests")
        assert result.category == ErrorCategory.RESOURCE

    def test_resource_disk_full(self):
        result = classify_error("OSError: No space left on device")
        assert result.category == ErrorCategory.RESOURCE

    def test_permanent_permission(self):
        result = classify_error("PermissionError: Permission denied")
        assert result.category == ErrorCategory.PERMANENT
        assert result.should_retry is False
        assert result.max_retries == 0

    def test_permanent_not_found(self):
        result = classify_error("FileNotFoundError: No such file")
        assert result.category == ErrorCategory.PERMANENT

    def test_permanent_auth(self):
        result = classify_error("401 Authentication required")
        assert result.category == ErrorCategory.PERMANENT

    def test_unknown_error(self):
        result = classify_error("Something completely weird happened xyz123")
        assert result.category == ErrorCategory.UNKNOWN
        assert result.should_retry is True
        assert result.max_retries == 1

    def test_exception_object(self):
        result = classify_error(ConnectionResetError("connection reset by peer"))
        assert result.category == ErrorCategory.TRANSIENT

    def test_priority_timeout_over_transient(self):
        """Timeout patterns should match before transient (e.g., 'timed out' contains 'retry'-like words)."""
        result = classify_error("request timed out after 30s")
        assert result.category == ErrorCategory.TIMEOUT


# ---------------------------------------------------------------------------
# Error Recording & Summary
# ---------------------------------------------------------------------------


class TestErrorRecording:
    def test_record_and_summary(self, tmp_path, monkeypatch):
        monkeypatch.setattr("utils.self_healing.STATE_DIR", tmp_path)
        monkeypatch.setattr("utils.self_healing.ERROR_HISTORY_FILE", tmp_path / "errors.jsonl")

        record_error("test_caller", "Connection reset", task_id="task_001")
        record_error("test_caller", "Permission denied", task_id="task_002")
        record_error("test_caller", "timed out", task_id="task_003")

        summary = get_error_summary(minutes=60)
        assert summary["total"] == 3
        assert "TRANSIENT" in summary["by_category"]
        assert "PERMANENT" in summary["by_category"]
        assert "TIMEOUT" in summary["by_category"]

    def test_empty_summary(self, tmp_path, monkeypatch):
        monkeypatch.setattr("utils.self_healing.ERROR_HISTORY_FILE", tmp_path / "nonexistent.jsonl")

        summary = get_error_summary(minutes=60)
        assert summary["total"] == 0
        assert summary["by_category"] == {}

    def test_record_returns_classified(self, tmp_path, monkeypatch):
        monkeypatch.setattr("utils.self_healing.STATE_DIR", tmp_path)
        monkeypatch.setattr("utils.self_healing.ERROR_HISTORY_FILE", tmp_path / "errors.jsonl")

        result = record_error("caller", "Connection reset")
        assert result.category == ErrorCategory.TRANSIENT
        assert result.should_retry is True


# ---------------------------------------------------------------------------
# Memory Monitor
# ---------------------------------------------------------------------------


class TestMemoryMonitor:
    def test_snapshot_returns_valid(self):
        snap = get_memory_snapshot()
        assert isinstance(snap, MemorySnapshot)
        assert snap.rss_mb >= 0
        assert snap.pid > 0
        assert snap.timestamp

    def test_snapshot_not_critical_in_tests(self):
        """Test process should not be using 3.5GB+."""
        snap = get_memory_snapshot()
        assert snap.critical is False

    def test_record_and_trend(self, tmp_path, monkeypatch):
        monkeypatch.setattr("utils.self_healing.STATE_DIR", tmp_path)
        monkeypatch.setattr("utils.self_healing.RSS_HISTORY_FILE", tmp_path / "rss.jsonl")

        snap = get_memory_snapshot()
        record_memory_snapshot(snap)
        record_memory_snapshot(snap)

        trend = get_memory_trend(entries=10)
        assert len(trend) == 2
        assert "rss_mb" in trend[0]

    def test_empty_trend(self, tmp_path, monkeypatch):
        monkeypatch.setattr("utils.self_healing.RSS_HISTORY_FILE", tmp_path / "nonexistent.jsonl")
        trend = get_memory_trend()
        assert trend == []


# ---------------------------------------------------------------------------
# Dead Man's Switch
# ---------------------------------------------------------------------------


class TestDeadManSwitch:
    def test_touch_and_check(self, tmp_path, monkeypatch):
        monkeypatch.setattr("utils.self_healing.STATE_DIR", tmp_path)
        monkeypatch.setattr("utils.self_healing.DMS_FILE", tmp_path / "dms.json")

        touch_dead_man_switch(component="test")
        result = check_dead_man_switch()
        assert result["ok"] is True
        assert "test" in result["detail"]

    def test_stale_switch(self, tmp_path, monkeypatch):
        monkeypatch.setattr("utils.self_healing.STATE_DIR", tmp_path)
        monkeypatch.setattr("utils.self_healing.DMS_FILE", tmp_path / "dms.json")
        monkeypatch.setattr("utils.self_healing.DMS_STALE_SECONDS", 1)

        touch_dead_man_switch(component="test")
        time.sleep(1.1)
        result = check_dead_man_switch()
        assert result["ok"] is False
        assert "STALE" in result["detail"]

    def test_missing_switch_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr("utils.self_healing.DMS_FILE", tmp_path / "nonexistent.json")
        result = check_dead_man_switch()
        assert result["ok"] is False
        assert "no switch file" in result["detail"]


# ---------------------------------------------------------------------------
# Degradation Tiers
# ---------------------------------------------------------------------------


class TestDegradationTiers:
    def test_default_is_full(self, tmp_path, monkeypatch):
        monkeypatch.setattr("utils.self_healing.DEGRADATION_FILE", tmp_path / "deg.json")
        state = _load_degradation_state()
        assert state.tier == DegradationTier.FULL

    def test_persistence(self, tmp_path, monkeypatch):
        monkeypatch.setattr("utils.self_healing.STATE_DIR", tmp_path)
        monkeypatch.setattr("utils.self_healing.DEGRADATION_FILE", tmp_path / "deg.json")

        state = DegradationState(
            tier=DegradationTier.REDUCED,
            reason="test",
            since="2026-01-01T00:00:00+00:00",
        )
        _save_degradation_state(state)
        loaded = _load_degradation_state()
        assert loaded.tier == DegradationTier.REDUCED
        assert loaded.reason == "test"

    def test_escalate_on_memory_critical(self, tmp_path, monkeypatch):
        monkeypatch.setattr("utils.self_healing.DEGRADATION_FILE", tmp_path / "deg.json")
        monkeypatch.setattr("utils.self_healing.STATE_DIR", tmp_path)
        monkeypatch.setattr("utils.self_healing.ERROR_HISTORY_FILE", tmp_path / "errors.jsonl")

        memory = MemorySnapshot(
            rss_mb=4000,
            rss_peak_mb=4000,
            timestamp="2026-01-01T00:00:00+00:00",
            pid=1,
            healthy=False,
            warning=True,
            critical=True,
            detail="CRITICAL: 4000MB",
        )
        state = evaluate_degradation(memory=memory, dms_ok=True)
        assert state.tier == DegradationTier.EMERGENCY

    def test_escalate_on_memory_warning(self, tmp_path, monkeypatch):
        monkeypatch.setattr("utils.self_healing.DEGRADATION_FILE", tmp_path / "deg.json")
        monkeypatch.setattr("utils.self_healing.STATE_DIR", tmp_path)
        monkeypatch.setattr("utils.self_healing.ERROR_HISTORY_FILE", tmp_path / "errors.jsonl")

        memory = MemorySnapshot(
            rss_mb=2500,
            rss_peak_mb=2500,
            timestamp="2026-01-01T00:00:00+00:00",
            pid=1,
            healthy=False,
            warning=True,
            critical=False,
            detail="WARNING: 2500MB",
        )
        state = evaluate_degradation(memory=memory, dms_ok=True)
        assert state.tier == DegradationTier.REDUCED

    def test_escalate_on_high_errors(self, tmp_path, monkeypatch):
        monkeypatch.setattr("utils.self_healing.DEGRADATION_FILE", tmp_path / "deg.json")
        monkeypatch.setattr("utils.self_healing.STATE_DIR", tmp_path)
        monkeypatch.setattr("utils.self_healing.ERROR_HISTORY_FILE", tmp_path / "errors.jsonl")

        # Write 15 error entries
        for i in range(15):
            record_error("test", f"error_{i}")

        memory = MemorySnapshot(
            rss_mb=100,
            rss_peak_mb=100,
            timestamp="2026-01-01T00:00:00+00:00",
            pid=1,
            healthy=True,
            warning=False,
            critical=False,
            detail="ok",
        )
        state = evaluate_degradation(memory=memory, dms_ok=True)
        assert state.tier == DegradationTier.REDUCED

    def test_escalate_on_stale_dms(self, tmp_path, monkeypatch):
        monkeypatch.setattr("utils.self_healing.DEGRADATION_FILE", tmp_path / "deg.json")
        monkeypatch.setattr("utils.self_healing.STATE_DIR", tmp_path)
        monkeypatch.setattr("utils.self_healing.ERROR_HISTORY_FILE", tmp_path / "errors.jsonl")

        memory = MemorySnapshot(
            rss_mb=100,
            rss_peak_mb=100,
            timestamp="2026-01-01T00:00:00+00:00",
            pid=1,
            healthy=True,
            warning=False,
            critical=False,
            detail="ok",
        )
        state = evaluate_degradation(memory=memory, dms_ok=False)
        assert state.tier == DegradationTier.REDUCED

    def test_deescalate_requires_calm(self, tmp_path, monkeypatch):
        monkeypatch.setattr("utils.self_healing.DEGRADATION_FILE", tmp_path / "deg.json")
        monkeypatch.setattr("utils.self_healing.STATE_DIR", tmp_path)
        monkeypatch.setattr("utils.self_healing.ERROR_HISTORY_FILE", tmp_path / "errors.jsonl")

        # First: escalate to REDUCED
        warn_memory = MemorySnapshot(
            rss_mb=2500,
            rss_peak_mb=2500,
            timestamp="2026-01-01T00:00:00+00:00",
            pid=1,
            healthy=False,
            warning=True,
            critical=False,
            detail="WARNING",
        )
        state = evaluate_degradation(memory=warn_memory, dms_ok=True)
        assert state.tier == DegradationTier.REDUCED

        # Now: conditions improve but too soon — should stay REDUCED
        ok_memory = MemorySnapshot(
            rss_mb=100,
            rss_peak_mb=100,
            timestamp="2026-01-01T00:00:00+00:00",
            pid=1,
            healthy=True,
            warning=False,
            critical=False,
            detail="ok",
        )
        state = evaluate_degradation(memory=ok_memory, dms_ok=True)
        assert state.tier == DegradationTier.REDUCED  # held, not enough calm time

    def test_memory_warning_plus_errors_goes_minimal(self, tmp_path, monkeypatch):
        monkeypatch.setattr("utils.self_healing.DEGRADATION_FILE", tmp_path / "deg.json")
        monkeypatch.setattr("utils.self_healing.STATE_DIR", tmp_path)
        monkeypatch.setattr("utils.self_healing.ERROR_HISTORY_FILE", tmp_path / "errors.jsonl")

        for i in range(12):
            record_error("test", f"error_{i}")

        warn_memory = MemorySnapshot(
            rss_mb=2500,
            rss_peak_mb=2500,
            timestamp="2026-01-01T00:00:00+00:00",
            pid=1,
            healthy=False,
            warning=True,
            critical=False,
            detail="WARNING",
        )
        state = evaluate_degradation(memory=warn_memory, dms_ok=True)
        assert state.tier == DegradationTier.MINIMAL


# ---------------------------------------------------------------------------
# Feature Gating
# ---------------------------------------------------------------------------


class TestFeatureGating:
    def test_full_tier_allows_all(self, tmp_path, monkeypatch):
        monkeypatch.setattr("utils.self_healing.DEGRADATION_FILE", tmp_path / "deg.json")
        assert should_allow_feature("research") is True
        assert should_allow_feature("task_execution") is True
        assert should_allow_feature("health_check") is True

    def test_reduced_blocks_optional(self, tmp_path, monkeypatch):
        monkeypatch.setattr("utils.self_healing.DEGRADATION_FILE", tmp_path / "deg.json")
        monkeypatch.setattr("utils.self_healing.STATE_DIR", tmp_path)
        state = DegradationState(tier=DegradationTier.REDUCED, reason="test", since="2026-01-01T00:00:00")
        _save_degradation_state(state)

        assert should_allow_feature("research") is False
        assert should_allow_feature("planning") is False
        assert should_allow_feature("proactive_seed") is False
        assert should_allow_feature("task_execution") is True
        assert should_allow_feature("health_check") is True

    def test_minimal_blocks_standard(self, tmp_path, monkeypatch):
        monkeypatch.setattr("utils.self_healing.DEGRADATION_FILE", tmp_path / "deg.json")
        monkeypatch.setattr("utils.self_healing.STATE_DIR", tmp_path)
        state = DegradationState(tier=DegradationTier.MINIMAL, reason="test", since="2026-01-01T00:00:00")
        _save_degradation_state(state)

        assert should_allow_feature("notification") is False
        assert should_allow_feature("memory_maintenance") is False
        assert should_allow_feature("task_execution") is True
        assert should_allow_feature("heartbeat") is True

    def test_emergency_blocks_core(self, tmp_path, monkeypatch):
        monkeypatch.setattr("utils.self_healing.DEGRADATION_FILE", tmp_path / "deg.json")
        monkeypatch.setattr("utils.self_healing.STATE_DIR", tmp_path)
        state = DegradationState(tier=DegradationTier.EMERGENCY, reason="test", since="2026-01-01T00:00:00")
        _save_degradation_state(state)

        assert should_allow_feature("task_execution") is False
        assert should_allow_feature("research") is False
        assert should_allow_feature("health_check") is True
        assert should_allow_feature("heartbeat") is True
        assert should_allow_feature("telegram_alert") is True


# ---------------------------------------------------------------------------
# Heartbeat Integration
# ---------------------------------------------------------------------------


class TestCheckSelfHealing:
    def test_healthy(self, tmp_path, monkeypatch):
        monkeypatch.setattr("utils.self_healing.STATE_DIR", tmp_path)
        monkeypatch.setattr("utils.self_healing.DMS_FILE", tmp_path / "dms.json")
        monkeypatch.setattr("utils.self_healing.DEGRADATION_FILE", tmp_path / "deg.json")
        monkeypatch.setattr("utils.self_healing.ERROR_HISTORY_FILE", tmp_path / "errors.jsonl")
        monkeypatch.setattr("utils.self_healing.RSS_HISTORY_FILE", tmp_path / "rss.jsonl")

        touch_dead_man_switch(component="test")
        result = check_self_healing()
        assert result["name"] == "self_healing"
        assert result["ok"] is True
        assert "tier=FULL" in result["detail"]

    def test_unhealthy_stale_dms(self, tmp_path, monkeypatch):
        monkeypatch.setattr("utils.self_healing.STATE_DIR", tmp_path)
        monkeypatch.setattr("utils.self_healing.DMS_FILE", tmp_path / "nonexistent.json")
        monkeypatch.setattr("utils.self_healing.DEGRADATION_FILE", tmp_path / "deg.json")
        monkeypatch.setattr("utils.self_healing.ERROR_HISTORY_FILE", tmp_path / "errors.jsonl")
        monkeypatch.setattr("utils.self_healing.RSS_HISTORY_FILE", tmp_path / "rss.jsonl")

        result = check_self_healing()
        assert result["ok"] is False
        assert "dms=STALE" in result["detail"]
        assert len(result.get("alerts", [])) >= 1

    def test_alerts_include_dms_warning(self, tmp_path, monkeypatch):
        monkeypatch.setattr("utils.self_healing.STATE_DIR", tmp_path)
        monkeypatch.setattr("utils.self_healing.DMS_FILE", tmp_path / "nonexistent.json")
        monkeypatch.setattr("utils.self_healing.DEGRADATION_FILE", tmp_path / "deg.json")
        monkeypatch.setattr("utils.self_healing.ERROR_HISTORY_FILE", tmp_path / "errors.jsonl")
        monkeypatch.setattr("utils.self_healing.RSS_HISTORY_FILE", tmp_path / "rss.jsonl")

        result = check_self_healing()
        alerts = result.get("alerts", [])
        dms_alerts = [a for a in alerts if "Dead man switch" in a.get("title", "")]
        assert len(dms_alerts) == 1

    def test_exception_safe(self, monkeypatch):
        """check_self_healing must never crash the heartbeat."""
        monkeypatch.setattr(
            "utils.self_healing.get_memory_snapshot", lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        )
        result = check_self_healing()
        assert result["name"] == "self_healing"
        assert result["ok"] is True
        assert "check skipped" in result["detail"]

    def test_high_errors_trigger_degradation(self, tmp_path, monkeypatch):
        monkeypatch.setattr("utils.self_healing.STATE_DIR", tmp_path)
        monkeypatch.setattr("utils.self_healing.DMS_FILE", tmp_path / "dms.json")
        monkeypatch.setattr("utils.self_healing.DEGRADATION_FILE", tmp_path / "deg.json")
        monkeypatch.setattr("utils.self_healing.ERROR_HISTORY_FILE", tmp_path / "errors.jsonl")
        monkeypatch.setattr("utils.self_healing.RSS_HISTORY_FILE", tmp_path / "rss.jsonl")

        touch_dead_man_switch(component="test")
        for i in range(12):
            record_error("test", f"error_{i}")

        result = check_self_healing()
        assert result["ok"] is False
        assert result["degradation_tier"] == "REDUCED"
