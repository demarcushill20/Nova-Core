"""Tests for utils/testing_mode.py — Testing-Mode Automation Loop.

50-60 tests covering mode management, canary registry, selector, executor,
validator, evidence ledger, anomaly detector, plan revision gate,
deep maintenance, and the cycle orchestrator.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest

# Patch BASE/STATE/OUTPUT before import so all paths point to tmp
_tmpdir = tempfile.mkdtemp()
_test_base = Path(_tmpdir)
_test_state = _test_base / "STATE" / "testing_mode"
_test_output = _test_base / "OUTPUT"
_test_state.mkdir(parents=True, exist_ok=True)
_test_output.mkdir(parents=True, exist_ok=True)

import utils.testing_mode as tm  # noqa: E402

# Redirect module paths to temp
tm.BASE = _test_base
tm.STATE_DIR = _test_state
tm.OUTPUT_DIR = _test_output
tm.CONFIG_FILE = _test_state / "config.json"
tm.EVIDENCE_FILE = _test_state / "evidence.jsonl"
tm.ANOMALY_FILE = _test_state / "anomaly_history.jsonl"
tm.CANARY_REGISTRY_FILE = _test_state / "canary_registry.json"
tm.LAST_CANARY_RUN = _test_state / "last_canary_run.json"
tm.LAST_CYCLE_SUMMARY = _test_state / "last_cycle_summary.json"
tm.LAST_DEEP_MAINTENANCE = _test_state / "last_deep_maintenance.json"
tm.CYCLE_SUMMARY_DIR = _test_state / "cycle_summaries"
tm.CYCLE_SUMMARY_DIR.mkdir(parents=True, exist_ok=True)


@pytest.fixture(autouse=True)
def _clean_state():
    """Clean state files between tests."""
    yield
    for f in _test_state.glob("*"):
        if f.is_file():
            f.unlink()
    for f in (_test_state / "cycle_summaries").glob("*"):
        f.unlink()
    for f in _test_output.glob("canary_*"):
        f.unlink()


# ===================================================================
# TestModeManagement
# ===================================================================


class TestModeManagement:
    """Mode get/set, persistence, defaults."""

    def test_default_is_normal(self):
        cfg = tm.get_testing_mode()
        assert cfg.mode == tm.TestingMode.NORMAL
        assert not cfg.dry_run

    def test_set_and_get_testing(self):
        tm.set_testing_mode(tm.TestingMode.TESTING, "test run")
        cfg = tm.get_testing_mode()
        assert cfg.mode == tm.TestingMode.TESTING
        assert cfg.reason == "test run"
        assert cfg.enabled_at != ""

    def test_set_dry_run(self):
        cfg = tm.set_testing_mode(tm.TestingMode.DRY_RUN, "dry run")
        assert cfg.dry_run is True
        loaded = tm.get_testing_mode()
        assert loaded.dry_run is True

    def test_persistence_survives_reload(self):
        tm.set_testing_mode(tm.TestingMode.TESTING, "persist test")
        # Re-read from disk
        cfg = tm.get_testing_mode()
        assert cfg.mode == tm.TestingMode.TESTING

    def test_is_testing_active(self):
        assert not tm.is_testing_active()
        tm.set_testing_mode(tm.TestingMode.DRY_RUN)
        assert tm.is_testing_active()

    def test_corrupt_config_returns_normal(self):
        tm.CONFIG_FILE.write_text("not json!!")
        cfg = tm.get_testing_mode()
        assert cfg.mode == tm.TestingMode.NORMAL


# ===================================================================
# TestCanaryRegistry
# ===================================================================


class TestCanaryRegistry:
    """Loading and validation of canary task registry."""

    def test_default_registry_has_6_tasks(self):
        registry = tm._default_canary_registry()
        assert len(registry) == 6
        assert all(isinstance(t, tm.CanaryTask) for t in registry)

    def test_load_defaults_when_no_file(self):
        registry = tm.load_canary_registry()
        assert len(registry) == 6
        ids = {t.id for t in registry}
        assert "canary_contract_check" in ids

    def test_load_custom_registry(self):
        custom = [
            {"id": "custom_1", "name": "Custom", "description": "test", "task_content": "do stuff"},
            {"id": "custom_2", "name": "Custom2", "description": "test2", "task_content": "do more"},
        ]
        tm.CANARY_REGISTRY_FILE.write_text(json.dumps(custom))
        registry = tm.load_canary_registry()
        assert len(registry) == 2
        assert registry[0].id == "custom_1"

    def test_corrupt_registry_falls_back(self):
        tm.CANARY_REGISTRY_FILE.write_text("{bad json")
        registry = tm.load_canary_registry()
        assert len(registry) == 6  # defaults

    def test_empty_list_falls_back(self):
        tm.CANARY_REGISTRY_FILE.write_text("[]")
        registry = tm.load_canary_registry()
        assert len(registry) == 6  # defaults

    def test_all_defaults_are_safe(self):
        registry = tm._default_canary_registry()
        assert all(t.safe for t in registry)


# ===================================================================
# TestCanarySelector
# ===================================================================


class TestCanarySelector:
    """Canary task selection logic."""

    def test_select_respects_count(self):
        registry = tm._default_canary_registry()
        selected = tm.select_canary_tasks(registry, count=2)
        assert len(selected) == 2

    def test_select_capped_at_registry_size(self):
        registry = tm._default_canary_registry()[:2]
        selected = tm.select_canary_tasks(registry, count=5)
        assert len(selected) == 2

    def test_empty_registry(self):
        selected = tm.select_canary_tasks([], count=2)
        assert selected == []

    def test_avoids_recent_repeats(self):
        registry = tm._default_canary_registry()
        # Mark all but one as recent
        recent = [{"entry_type": "canary_result", "data": {"task_id": t.id}} for t in registry[:-1]]
        # Run many times — the non-recent one should appear most
        counts: dict[str, int] = {}
        for _ in range(50):
            sel = tm.select_canary_tasks(registry, count=1, recent_evidence=recent)
            for s in sel:
                counts[s.id] = counts.get(s.id, 0) + 1
        # The non-recent task should be selected most often
        last_id = registry[-1].id
        assert counts.get(last_id, 0) >= 10  # should dominate (probabilistic)

    def test_unsafe_tasks_excluded(self):
        registry = tm._default_canary_registry()
        # Mark all as unsafe
        for t in registry:
            t.safe = False
        selected = tm.select_canary_tasks(registry, count=2)
        assert len(selected) == 0


# ===================================================================
# TestCanaryExecutor
# ===================================================================


class TestCanaryExecutor:
    """Canary task execution."""

    def test_dry_run_returns_pass(self):
        task = tm._default_canary_registry()[0]
        result = tm.execute_canary(task, "cycle1", "run1", dry_run=True)
        assert result.verdict == tm.CanaryVerdict.PASS.value
        assert result.dry_run is True
        assert result.confidence == 1.0

    def test_dry_run_has_output_path(self):
        task = tm._default_canary_registry()[0]
        result = tm.execute_canary(task, "c", "r", dry_run=True)
        assert "dryrun" in result.output_path

    @mock.patch("utils.max_plan_guard.should_allow_task", return_value=(True, "ok"))
    @mock.patch("subprocess.run")
    def test_live_success_with_contract(self, mock_run, mock_guard):
        mock_run.return_value = mock.MagicMock(returncode=0, stdout="ok", stderr="")
        task = tm._default_canary_registry()[0]
        # Create output file with valid contract
        out_file = _test_output / f"canary_{task.id}.md"
        out_file.write_text(
            "# Result\nAll good.\n\n## CONTRACT\n- status: complete\n- confidence: high\n- summary: test passed\n"
        )
        result = tm.execute_canary(task, "c1", "r1", dry_run=False)
        assert result.verdict == tm.CanaryVerdict.PASS.value
        assert result.contract_valid is True

    @mock.patch("utils.max_plan_guard.should_allow_task", return_value=(True, "ok"))
    @mock.patch("subprocess.run")
    def test_live_empty_output(self, mock_run, mock_guard):
        mock_run.return_value = mock.MagicMock(returncode=0)
        task = tm._default_canary_registry()[0]
        # No output file created
        result = tm.execute_canary(task, "c1", "r1", dry_run=False)
        assert result.verdict == tm.CanaryVerdict.HARD_FAIL.value

    @mock.patch("utils.max_plan_guard.should_allow_task", return_value=(True, "ok"))
    @mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 120))
    def test_timeout(self, mock_run, mock_guard):
        task = tm._default_canary_registry()[0]
        result = tm.execute_canary(task, "c1", "r1", dry_run=False)
        assert result.verdict == tm.CanaryVerdict.HARD_FAIL.value
        assert result.error == "timeout"

    @mock.patch("utils.max_plan_guard.should_allow_task", return_value=(True, "ok"))
    @mock.patch("subprocess.run", side_effect=OSError("no claude"))
    def test_subprocess_error(self, mock_run, mock_guard):
        task = tm._default_canary_registry()[0]
        result = tm.execute_canary(task, "c1", "r1", dry_run=False)
        assert result.verdict == tm.CanaryVerdict.HARD_FAIL.value
        assert "no claude" in result.error

    @mock.patch("utils.max_plan_guard.should_allow_task", return_value=(True, "ok"))
    @mock.patch("subprocess.run")
    def test_contract_invalid(self, mock_run, mock_guard):
        mock_run.return_value = mock.MagicMock(returncode=0)
        task = tm._default_canary_registry()[0]
        out_file = _test_output / f"canary_{task.id}.md"
        out_file.write_text("# Result\nNo contract here.\n")
        result = tm.execute_canary(task, "c1", "r1", dry_run=False)
        assert result.verdict in (tm.CanaryVerdict.SOFT_FAIL.value, tm.CanaryVerdict.HARD_FAIL.value)

    def test_timestamp_auto_set(self):
        task = tm._default_canary_registry()[0]
        result = tm.execute_canary(task, "c", "r", dry_run=True)
        assert result.timestamp != ""


# ===================================================================
# TestValidator
# ===================================================================


class TestValidator:
    """Contract validation logic."""

    def test_pass_with_valid_contract(self):
        text = "# Out\n\n## CONTRACT\n- status: done\n- confidence: 0.9\n- summary: ok\n"
        task = tm.CanaryTask(
            id="t",
            name="t",
            description="t",
            task_content="t",
            expected_contract_fields=["status", "confidence"],
        )
        verdict, reason, conf = tm.validate_canary_output(text, task)
        assert verdict == tm.CanaryVerdict.PASS.value
        assert conf > 0

    def test_soft_fail_missing_fields(self):
        text = "# Out\n\n## CONTRACT\n- status: done\n- confidence: high\n"
        task = tm.CanaryTask(
            id="t",
            name="t",
            description="t",
            task_content="t",
            expected_contract_fields=["status", "confidence", "nonexistent_field"],
        )
        verdict, reason, conf = tm.validate_canary_output(text, task)
        assert verdict == tm.CanaryVerdict.SOFT_FAIL.value

    def test_hard_fail_empty_output(self):
        task = tm.CanaryTask(id="t", name="t", description="t", task_content="t")
        verdict, reason, conf = tm.validate_canary_output("", task)
        assert verdict == tm.CanaryVerdict.HARD_FAIL.value
        assert conf == 0.0

    def test_soft_fail_no_contract_section(self):
        task = tm.CanaryTask(id="t", name="t", description="t", task_content="t")
        verdict, reason, conf = tm.validate_canary_output("just some text", task)
        assert verdict == tm.CanaryVerdict.SOFT_FAIL.value

    def test_confidence_mapping_high(self):
        text = "## CONTRACT\n- status: done\n- confidence: high\n"
        task = tm.CanaryTask(
            id="t",
            name="t",
            description="t",
            task_content="t",
            expected_contract_fields=["status", "confidence"],
        )
        verdict, reason, conf = tm.validate_canary_output(text, task)
        assert conf >= 0.8

    def test_confidence_mapping_low(self):
        text = "## CONTRACT\n- status: done\n- confidence: low\n"
        task = tm.CanaryTask(
            id="t",
            name="t",
            description="t",
            task_content="t",
            expected_contract_fields=["status", "confidence"],
        )
        verdict, reason, conf = tm.validate_canary_output(text, task)
        assert conf <= 0.5


# ===================================================================
# TestEvidenceLedger
# ===================================================================


class TestEvidenceLedger:
    """Evidence JSONL read/write."""

    def test_write_and_read(self):
        entry = tm.EvidenceEntry(
            cycle_id="c1",
            run_id="r1",
            timestamp=datetime.now(timezone.utc).isoformat(),
            entry_type="health_snapshot",
            data={"passed": 5},
        )
        tm.write_evidence(entry)
        evidence = tm.read_evidence(max_age_hours=1.0)
        assert len(evidence) == 1
        assert evidence[0]["data"]["passed"] == 5

    def test_empty_file(self):
        evidence = tm.read_evidence()
        assert evidence == []

    def test_max_age_filter(self):
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        entry = tm.EvidenceEntry(
            cycle_id="c1",
            run_id="r1",
            timestamp=old_ts,
            entry_type="test",
            data={},
        )
        tm.write_evidence(entry)
        # Within 1 hour — should find nothing
        evidence = tm.read_evidence(max_age_hours=1.0)
        assert len(evidence) == 0
        # Within 72 hours — should find it
        evidence = tm.read_evidence(max_age_hours=72.0)
        assert len(evidence) == 1

    def test_pruning(self):
        # Set low max for test
        original_max = tm.EVIDENCE_MAX_ENTRIES
        tm.EVIDENCE_MAX_ENTRIES = 10
        try:
            for i in range(20):
                entry = tm.EvidenceEntry(
                    cycle_id=f"c{i}",
                    run_id=f"r{i}",
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    entry_type="test",
                    data={"i": i},
                )
                tm.write_evidence(entry)
            lines = tm.EVIDENCE_FILE.read_text().strip().splitlines()
            assert len(lines) <= 10
        finally:
            tm.EVIDENCE_MAX_ENTRIES = original_max

    def test_corrupt_entries_skipped(self):
        tm._ensure_dirs()
        with open(tm.EVIDENCE_FILE, "w") as f:
            f.write("not json\n")
            f.write(
                json.dumps(
                    {
                        "cycle_id": "c1",
                        "run_id": "r1",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "entry_type": "test",
                        "data": {},
                    }
                )
                + "\n"
            )
        evidence = tm.read_evidence(max_age_hours=1.0)
        assert len(evidence) == 1

    def test_thread_safety(self):
        errors: list[str] = []

        def writer(n: int):
            try:
                entry = tm.EvidenceEntry(
                    cycle_id=f"c{n}",
                    run_id=f"r{n}",
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    entry_type="test",
                    data={"n": n},
                )
                tm.write_evidence(entry)
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        evidence = tm.read_evidence(max_age_hours=1.0)
        assert len(evidence) == 10


# ===================================================================
# TestAnomalyDetector
# ===================================================================


class TestAnomalyDetector:
    """Anomaly detection from evidence."""

    def _make_canary_entry(self, task_id: str, verdict: str, confidence: float = 0.8, ts: str | None = None) -> dict:
        if ts is None:
            ts = datetime.now(timezone.utc).isoformat()
        return {
            "entry_type": "canary_result",
            "timestamp": ts,
            "data": {"task_id": task_id, "verdict": verdict, "confidence": confidence},
            "verdict": verdict,
        }

    def test_no_anomalies_clean(self):
        evidence = [
            self._make_canary_entry("t1", "pass"),
            self._make_canary_entry("t2", "pass"),
        ]
        anomalies = tm.detect_anomalies(evidence)
        assert len(anomalies) == 0

    def test_repeated_task(self):
        evidence = [self._make_canary_entry("t1", "pass") for _ in range(5)]
        anomalies = tm.detect_anomalies(evidence)
        types = [a.anomaly_type for a in anomalies]
        assert "REPEATED_TASK" in types

    def test_stuck_retry(self):
        evidence = [
            self._make_canary_entry("t1", "hard_fail"),
            self._make_canary_entry("t1", "hard_fail"),
            self._make_canary_entry("t1", "hard_fail"),
        ]
        anomalies = tm.detect_anomalies(evidence)
        types = [a.anomaly_type for a in anomalies]
        assert "STUCK_RETRY" in types

    def test_silent_failure(self):
        evidence = [self._make_canary_entry("t1", "pass", confidence=0.1)]
        anomalies = tm.detect_anomalies(evidence)
        types = [a.anomaly_type for a in anomalies]
        assert "SILENT_FAILURE" in types

    def test_evidence_gap(self):
        now = datetime.now(timezone.utc)
        old = now - timedelta(hours=4)
        evidence = [
            {
                "entry_type": "health_snapshot",
                "timestamp": old.isoformat(),
                "data": {},
            },
            {
                "entry_type": "health_snapshot",
                "timestamp": now.isoformat(),
                "data": {},
            },
            # Need a canary result to trigger any analysis
            self._make_canary_entry("t1", "pass"),
        ]
        anomalies = tm.detect_anomalies(evidence)
        types = [a.anomaly_type for a in anomalies]
        assert "EVIDENCE_GAP" in types

    def test_trend_regression(self):
        evidence = [self._make_canary_entry(f"t{i}", "pass") for i in range(4)] + [
            self._make_canary_entry(f"t{i}", "hard_fail") for i in range(4)
        ]
        anomalies = tm.detect_anomalies(evidence)
        types = [a.anomaly_type for a in anomalies]
        assert "TREND_REGRESSION" in types

    def test_multiple_anomalies(self):
        evidence = [
            self._make_canary_entry("t1", "pass", confidence=0.1),
            self._make_canary_entry("t1", "hard_fail"),
            self._make_canary_entry("t1", "hard_fail"),
            self._make_canary_entry("t1", "hard_fail"),
            self._make_canary_entry("t1", "hard_fail"),
        ]
        anomalies = tm.detect_anomalies(evidence)
        assert len(anomalies) >= 2  # STUCK_RETRY + REPEATED_TASK at least


# ===================================================================
# TestPlanRevisionGate
# ===================================================================


class TestPlanRevisionGate:
    """Plan revision evidence gating."""

    def _make_entry(self, verdict: str) -> dict:
        return {
            "entry_type": "canary_result",
            "data": {"verdict": verdict},
        }

    def test_insufficient_evidence(self):
        allowed, reason = tm.should_allow_plan_revision(
            [
                self._make_entry("pass"),
            ]
        )
        assert not allowed
        assert "insufficient" in reason

    def test_sufficient_evidence_high_pass_rate(self):
        evidence = [self._make_entry("pass") for _ in range(5)]
        allowed, reason = tm.should_allow_plan_revision(evidence)
        assert allowed

    def test_low_pass_rate(self):
        evidence = [
            self._make_entry("pass"),
            self._make_entry("hard_fail"),
            self._make_entry("hard_fail"),
            self._make_entry("hard_fail"),
        ]
        allowed, reason = tm.should_allow_plan_revision(evidence)
        assert not allowed
        assert "pass rate" in reason

    def test_hard_fail_streak_blocks(self):
        evidence = [
            self._make_entry("pass"),
            self._make_entry("pass"),
            self._make_entry("pass"),
            self._make_entry("pass"),
            self._make_entry("pass"),
            self._make_entry("hard_fail"),
            self._make_entry("hard_fail"),
            self._make_entry("hard_fail"),
        ]
        allowed, reason = tm.should_allow_plan_revision(evidence)
        assert not allowed
        assert "HARD_FAIL streak" in reason

    def test_mixed_results_above_threshold(self):
        evidence = [
            self._make_entry("pass"),
            self._make_entry("pass"),
            self._make_entry("pass"),
            self._make_entry("soft_fail"),
            self._make_entry("pass"),
        ]
        allowed, reason = tm.should_allow_plan_revision(evidence)
        assert allowed


# ===================================================================
# TestDeepMaintenance
# ===================================================================


class TestDeepMaintenance:
    """Deep maintenance operations."""

    def test_dry_run_no_deletions(self):
        # Create a stale file
        stale = _test_output / "canary_old_test.md"
        stale.write_text("old")
        # Set mtime to 10 days ago
        old_time = time.time() - (10 * 86400)
        os.utime(stale, (old_time, old_time))

        report = tm.run_deep_maintenance(dry_run=True)
        assert report["dry_run"] is True
        assert report["stale_cleaned"] >= 1
        assert stale.exists()  # not deleted in dry run

    def test_live_deletes_stale(self):
        stale = _test_output / "canary_stale_test.md"
        stale.write_text("old")
        old_time = time.time() - (10 * 86400)
        os.utime(stale, (old_time, old_time))

        report = tm.run_deep_maintenance(dry_run=False)
        assert report["stale_cleaned"] >= 1
        assert not stale.exists()

    def test_registry_health_check(self):
        report = tm.run_deep_maintenance(dry_run=True)
        assert report["registry_ok"] is True
        assert report["registry_count"] == 6


# ===================================================================
# TestCooldown
# ===================================================================


class TestCooldown:
    """Cooldown file helpers."""

    def test_no_file_means_ready(self):
        assert tm._check_cooldown(tm.LAST_CANARY_RUN, 55) is True

    def test_recent_cooldown_blocks(self):
        tm._update_cooldown(tm.LAST_CANARY_RUN)
        assert tm._check_cooldown(tm.LAST_CANARY_RUN, 55) is False

    def test_expired_cooldown_allows(self):
        old_ts = (datetime.now(timezone.utc) - timedelta(minutes=60)).isoformat()
        tm.LAST_CANARY_RUN.write_text(json.dumps({"timestamp": old_ts}))
        assert tm._check_cooldown(tm.LAST_CANARY_RUN, 55) is True


# ===================================================================
# TestCycleRunner
# ===================================================================


class TestCycleRunner:
    """Orchestrator run_testing_mode_cycle."""

    def test_normal_mode_inactive(self):
        # Default is NORMAL
        result = tm.run_testing_mode_cycle([{"name": "test", "ok": True, "detail": "ok"}])
        assert result["ok"] is True
        assert "inactive" in result["detail"]

    def test_dry_run_cycle(self):
        tm.set_testing_mode(tm.TestingMode.DRY_RUN, "test")
        checks = [{"name": "test", "ok": True, "detail": "ok"}]
        result = tm.run_testing_mode_cycle(checks)
        assert result["ok"] is True
        assert result.get("dry_run") is True
        assert "DRY_RUN" in result["detail"]

    def test_degradation_blocks(self):
        tm.set_testing_mode(tm.TestingMode.TESTING, "test")
        with mock.patch("utils.self_healing.should_allow_feature", return_value=False):
            result = tm.run_testing_mode_cycle([{"name": "t", "ok": True, "detail": "ok"}])
        # Should be skipped due to degradation
        assert result["ok"] is True
        assert "degradation" in result["detail"]

    def test_evidence_written_on_cycle(self):
        tm.set_testing_mode(tm.TestingMode.DRY_RUN, "test")
        checks = [
            {"name": "disk", "ok": True, "detail": "ok"},
            {"name": "services", "ok": False, "detail": "down"},
        ]
        tm.run_testing_mode_cycle(checks)
        evidence = tm.read_evidence(max_age_hours=1.0)
        assert len(evidence) >= 1  # at least health snapshot

    def test_full_cycle_with_canary(self):
        tm.set_testing_mode(tm.TestingMode.DRY_RUN, "full test")
        # Ensure cooldowns allow running
        for f in [tm.LAST_CANARY_RUN, tm.LAST_CYCLE_SUMMARY, tm.LAST_DEEP_MAINTENANCE]:
            if f.exists():
                f.unlink()
        checks = [{"name": "t", "ok": True, "detail": "ok"}]
        result = tm.run_testing_mode_cycle(checks)
        assert "canaries" in result.get("detail", "")
        assert len(result.get("canary_results", [])) > 0

    def test_canary_results_in_evidence(self):
        tm.set_testing_mode(tm.TestingMode.DRY_RUN, "test")
        if tm.LAST_CANARY_RUN.exists():
            tm.LAST_CANARY_RUN.unlink()
        checks = [{"name": "t", "ok": True, "detail": "ok"}]
        tm.run_testing_mode_cycle(checks)
        evidence = tm.read_evidence(max_age_hours=1.0)
        canary_entries = [e for e in evidence if e.get("entry_type") == "canary_result"]
        assert len(canary_entries) >= 1


# ===================================================================
# TestHealthSnapshot
# ===================================================================


class TestHealthSnapshot:
    """Health snapshot evidence creation."""

    def test_snapshot_from_checks(self):
        checks = [
            {"name": "disk", "ok": True, "detail": "ok"},
            {"name": "svc", "ok": False, "detail": "down"},
            {"name": "mem", "ok": True, "detail": "ok"},
        ]
        entry = tm.health_snapshot_from_checks(checks, "c1", "r1")
        assert entry.entry_type == "health_snapshot"
        assert entry.data["total"] == 3
        assert entry.data["passed"] == 2
        assert entry.data["failed"] == 1
        assert "svc" in entry.data["failed_names"]

    def test_snapshot_all_pass(self):
        checks = [{"name": "a", "ok": True, "detail": "ok"}]
        entry = tm.health_snapshot_from_checks(checks, "c1", "r1")
        assert entry.verdict == "pass"
        assert entry.data["health_pct"] == 100.0

    def test_snapshot_empty_checks(self):
        entry = tm.health_snapshot_from_checks([], "c1", "r1")
        assert entry.data["total"] == 0
        assert entry.data["health_pct"] == 100.0


# ===================================================================
# TestEnums
# ===================================================================


class TestEnums:
    """Enum sanity checks."""

    def test_testing_mode_values(self):
        assert tm.TestingMode.NORMAL == 0
        assert tm.TestingMode.TESTING == 1
        assert tm.TestingMode.DRY_RUN == 2

    def test_canary_verdict_values(self):
        assert tm.CanaryVerdict.PASS.value == "pass"
        assert tm.CanaryVerdict.SOFT_FAIL.value == "soft_fail"
        assert tm.CanaryVerdict.HARD_FAIL.value == "hard_fail"

    def test_anomaly_severity_values(self):
        assert tm.AnomalySeverity.INFO.value == "info"
        assert tm.AnomalySeverity.WARNING.value == "warning"
        assert tm.AnomalySeverity.CRITICAL.value == "critical"
