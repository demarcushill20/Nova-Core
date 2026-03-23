"""Tests for utils/warning_router.py — unified warning collection and routing."""

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure nova-core root is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.warning_router import (
    Warning,
    WarningSeverity,
    WarningCategory,
    emit_warning,
    emit,
    get_pending_warnings,
    mark_investigated,
    get_queue_stats,
    clear_queue,
    collect_from_heartbeat,
    collect_from_circuit_breakers,
    collect_from_budget,
    collect_from_drift,
    collect_from_runaway,
    collect_from_error_summary,
    collect_from_memory,
    _compute_fingerprint,
)


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """Redirect all state files to a temp directory."""
    monkeypatch.setattr("utils.warning_router.STATE_DIR", tmp_path)
    monkeypatch.setattr("utils.warning_router.QUEUE_FILE", tmp_path / "warning_queue.jsonl")
    monkeypatch.setattr("utils.warning_router.ARCHIVE_FILE", tmp_path / "warning_archive.jsonl")
    monkeypatch.setattr("utils.warning_router.COOLDOWN_FILE", tmp_path / "warning_cooldown.json")


class TestWarningCreation:
    def test_auto_fingerprint(self):
        w = Warning(
            timestamp="2026-03-23T12:00:00Z",
            source="heartbeat",
            severity=WarningSeverity.WARNING,
            category=WarningCategory.HEARTBEAT,
            message="Service down: novacore-watcher",
        )
        assert w.fingerprint != ""
        assert len(w.fingerprint) == 12

    def test_same_message_same_fingerprint(self):
        fp1 = _compute_fingerprint("heartbeat", "heartbeat", "Service down: novacore-watcher")
        fp2 = _compute_fingerprint("heartbeat", "heartbeat", "Service down: novacore-watcher")
        assert fp1 == fp2

    def test_different_message_different_fingerprint(self):
        fp1 = _compute_fingerprint("heartbeat", "heartbeat", "Service down: novacore-watcher")
        fp2 = _compute_fingerprint("budget", "budget", "Token limit exceeded")
        assert fp1 != fp2


class TestEmitAndQueue:
    def test_emit_queues_warning(self):
        queued = emit("heartbeat", "heartbeat", "Test warning", severity=WarningSeverity.WARNING)
        assert queued is True
        pending = get_pending_warnings()
        assert len(pending) == 1
        assert pending[0].message == "Test warning"

    def test_emit_dedup_within_cooldown(self):
        emit("heartbeat", "heartbeat", "Same warning", cooldown_secs=300)
        queued = emit("heartbeat", "heartbeat", "Same warning", cooldown_secs=300)
        assert queued is False
        pending = get_pending_warnings()
        assert len(pending) == 1

    def test_emit_different_messages_both_queued(self):
        emit("heartbeat", "heartbeat", "Warning A")
        emit("budget", "budget", "Warning B")
        pending = get_pending_warnings()
        assert len(pending) == 2

    def test_emit_warning_object(self):
        w = Warning(
            timestamp="2026-03-23T12:00:00Z",
            source="test",
            severity=WarningSeverity.CRITICAL,
            category="test",
            message="Critical test",
        )
        queued = emit_warning(w)
        assert queued is True
        pending = get_pending_warnings()
        assert len(pending) == 1
        assert pending[0].severity == WarningSeverity.CRITICAL

    def test_pending_sorted_by_severity(self):
        emit("src1", "cat1", "Info msg", severity=WarningSeverity.INFO)
        emit("src2", "cat2", "Critical msg", severity=WarningSeverity.CRITICAL)
        emit("src3", "cat3", "Warning msg", severity=WarningSeverity.WARNING)
        pending = get_pending_warnings()
        assert pending[0].severity == WarningSeverity.CRITICAL
        assert pending[1].severity == WarningSeverity.WARNING
        assert pending[2].severity == WarningSeverity.INFO


class TestMarkInvestigated:
    def test_mark_removes_from_queue(self):
        emit("test", "test", "To investigate")
        pending = get_pending_warnings()
        assert len(pending) == 1
        fp = pending[0].fingerprint
        mark_investigated(fp, "inv_001")
        assert len(get_pending_warnings()) == 0

    def test_mark_archives_entry(self, tmp_path):
        emit("test", "test", "Archive me")
        fp = get_pending_warnings()[0].fingerprint
        mark_investigated(fp, "inv_002")

        archive = tmp_path / "warning_archive.jsonl"
        assert archive.exists()
        lines = archive.read_text().strip().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["investigated"] is True
        assert entry["investigation_id"] == "inv_002"


class TestQueueStats:
    def test_empty_stats(self):
        stats = get_queue_stats()
        assert stats["total_queued"] == 0
        assert stats["pending"] == 0

    def test_stats_with_warnings(self):
        emit("src1", "heartbeat", "W1", severity=WarningSeverity.CRITICAL)
        emit("src2", "budget", "W2", severity=WarningSeverity.WARNING)
        emit("src3", "drift", "W3", severity=WarningSeverity.INFO)
        stats = get_queue_stats()
        assert stats["pending"] == 3
        assert stats["by_severity"]["critical"] == 1
        assert stats["by_severity"]["warning"] == 1
        assert stats["by_severity"]["info"] == 1


class TestClearQueue:
    def test_clear_returns_count(self):
        emit("s1", "c1", "W1")
        emit("s2", "c2", "W2")
        count = clear_queue()
        assert count == 2
        assert len(get_pending_warnings()) == 0


class TestCollectors:
    def test_collect_from_heartbeat_failed_checks(self):
        checks = [
            {"name": "service_watcher", "ok": False, "detail": "inactive"},
            {"name": "disk", "ok": True, "detail": "42% used"},
            {"name": "service_telegram", "ok": False, "detail": "failed"},
        ]
        count = collect_from_heartbeat(checks)
        assert count == 2
        pending = get_pending_warnings()
        assert len(pending) == 2

    def test_collect_from_heartbeat_all_ok(self):
        checks = [
            {"name": "disk", "ok": True, "detail": "42% used"},
        ]
        count = collect_from_heartbeat(checks)
        assert count == 0

    def test_collect_from_circuit_breakers(self):
        states = {"claude_api": "OPEN", "mcp": "CLOSED", "redis": "OPEN"}
        count = collect_from_circuit_breakers(states)
        assert count == 2

    def test_collect_from_budget(self):
        alerts = [
            "Daily input tokens at 90% of limit",
            "Session output tokens at 75% of limit",
        ]
        count = collect_from_budget(alerts)
        assert count == 2
        pending = get_pending_warnings()
        # 90% alert should be CRITICAL
        critical = [w for w in pending if w.severity == WarningSeverity.CRITICAL]
        assert len(critical) == 1

    def test_collect_from_drift_no_drift(self):
        report = {"drift_detected": False, "signals": []}
        count = collect_from_drift(report)
        assert count == 0

    def test_collect_from_drift_with_signals(self):
        report = {
            "drift_detected": True,
            "signals": [
                {"metric": "success_rate", "severity": "warning", "message": "Success rate dropped 20%"},
                {"metric": "duration", "severity": "critical", "message": "Duration 8x baseline"},
            ],
        }
        count = collect_from_drift(report)
        assert count == 2

    def test_collect_from_runaway_not_detected(self):
        count = collect_from_runaway({"detected": False})
        assert count == 0

    def test_collect_from_runaway_detected(self):
        detection = {
            "detected": True,
            "reasons": ["Same caller 30 calls in 60m", "Burn rate 5x"],
            "severity": "critical",
        }
        count = collect_from_runaway(detection)
        assert count == 1
        pending = get_pending_warnings()
        assert pending[0].severity == WarningSeverity.CRITICAL

    def test_collect_from_error_summary_low_count(self):
        summary = {"total": 2, "by_category": {}, "retry_eligible": 2, "window_minutes": 60}
        count = collect_from_error_summary(summary)
        assert count == 0  # below threshold

    def test_collect_from_error_summary_spike(self):
        summary = {
            "total": 15,
            "by_category": {"PERMANENT": 4, "TRANSIENT": 11},
            "retry_eligible": 11,
            "window_minutes": 60,
        }
        count = collect_from_error_summary(summary)
        assert count == 1
        pending = get_pending_warnings()
        assert pending[0].severity == WarningSeverity.CRITICAL  # 4 permanent >= 3

    def test_collect_from_memory_ok(self):
        count = collect_from_memory({"rss_mb": 500, "warning": False, "critical": False})
        assert count == 0

    def test_collect_from_memory_warning(self):
        count = collect_from_memory({"rss_mb": 2200, "warning": True, "critical": False})
        assert count == 1

    def test_collect_from_memory_critical(self):
        count = collect_from_memory({"rss_mb": 3600, "warning": True, "critical": True})
        assert count == 1
        pending = get_pending_warnings()
        assert pending[0].severity == WarningSeverity.CRITICAL
