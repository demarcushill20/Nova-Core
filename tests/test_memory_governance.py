"""Phase 8 — Memory Governance Tests.

Tests for retention rules, dry-run behavior, safe execution, protection
logic, stale marking, archival, pruning, and router integration.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from agents.memory_governance import (
    VALID_GOVERNANCE_ACTIONS,
    GovernanceEngine,
    GovernanceResult,
    ProtectionLevel,
    SweepSummary,
    classify_protection,
    is_protected,
    run_governance_sweep,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DAY = 86400
_NOW = 1773500000.0  # Fixed timestamp for deterministic tests


def _setup_state(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Create STATE/, MEMORY/, LOGS/ under tmp_path and return paths."""
    state = tmp_path / "STATE"
    memory = tmp_path / "MEMORY"
    logs = tmp_path / "LOGS"
    state.mkdir()
    memory.mkdir()
    logs.mkdir()
    return state, memory, logs


def _make_engine(
    tmp_path: Path,
    now: float = _NOW,
) -> tuple[GovernanceEngine, Path, Path, Path]:
    """Create engine with isolated test directories."""
    state, memory, logs = _setup_state(tmp_path)
    engine = GovernanceEngine(
        state_dir=state,
        memory_dir=memory,
        logs_dir=logs,
        now=now,
    )
    return engine, state, memory, logs


def _write_json(path: Path, data: dict, mtime: float | None = None) -> None:
    """Write JSON file and optionally backdate its mtime."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))
    if mtime is not None:
        import os

        os.utime(str(path), (mtime, mtime))


def _make_open_loop(
    path: Path,
    loop_id: str = "ol-test-1",
    status: str = "open",
    title: str = "Fix auth timeout",
    age_days: float = 0,
    **extra: object,
) -> Path:
    """Write a mock open loop JSON file."""
    mtime = _NOW - (age_days * _DAY) if age_days > 0 else _NOW
    updated_at = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ",
        time.gmtime(mtime),
    )
    data = {
        "loop_id": loop_id,
        "title": title,
        "summary": "Test loop for governance",
        "source": "test",
        "project": "nova-core",
        "status": status,
        "opened_at": updated_at,
        "updated_at": updated_at,
        "confidence": "medium",
        "history": [],
        **extra,
    }
    filepath = path / f"loop_{loop_id}.json"
    _write_json(filepath, data, mtime=mtime)
    return filepath


def _make_session(path: Path, name: str = "ses_test.json", age_days: float = 0) -> Path:
    """Write a mock session file."""
    mtime = _NOW - (age_days * _DAY)
    filepath = path / name
    _write_json(filepath, {"session_id": name, "status": "completed"}, mtime=mtime)
    return filepath


def _make_wm(path: Path, name: str = "wm_test.json", age_days: float = 0) -> Path:
    """Write a mock working memory file."""
    mtime = _NOW - (age_days * _DAY)
    filepath = path / name
    _write_json(filepath, {"event_type": "heartbeat_cycle", "summary": "ok"}, mtime=mtime)
    return filepath


def _make_notification(path: Path, name: str = "notif_1.txt", age_days: float = 0) -> Path:
    """Write a mock notification file."""
    mtime = _NOW - (age_days * _DAY)
    filepath = path / name
    filepath.parent.mkdir(parents=True, exist_ok=True)
    filepath.write_text("notified")
    import os

    os.utime(str(filepath), (mtime, mtime))
    return filepath


def _make_rotated_log(path: Path, name: str = "structured.20260301.jsonl", age_days: float = 0) -> Path:
    """Write a mock rotated log file."""
    mtime = _NOW - (age_days * _DAY)
    filepath = path / name
    filepath.parent.mkdir(parents=True, exist_ok=True)
    filepath.write_text('{"ts":"2026-03-01","event":"test"}\n')
    import os

    os.utime(str(filepath), (mtime, mtime))
    return filepath


def _make_workflow_learning(
    path: Path,
    name: str = "mem_test_123.json",
    age_days: float = 0,
    summary: str = "Completed deployment task successfully with no issues.",
    importance: float = 0.5,
    **extra: object,
) -> Path:
    """Write a mock workflow learning artifact."""
    mtime = _NOW - (age_days * _DAY)
    filepath = path / name
    data = {
        "artifact_id": name.replace(".json", ""),
        "task_summary": summary,
        "importance": importance,
        "confidence": "medium",
        **extra,
    }
    _write_json(filepath, data, mtime=mtime)
    return filepath


# ---------------------------------------------------------------------------
# Test: GovernanceResult and SweepSummary structures
# ---------------------------------------------------------------------------


class TestGovernanceStructures:
    """Validate governance data structures."""

    def test_valid_actions(self):
        expected = {
            "no_action",
            "marked_stale",
            "compacted",
            "archived",
            "pruned",
            "protected_skip",
            "rejected_unsafe",
            "rejected_insufficient_evidence",
        }
        assert VALID_GOVERNANCE_ACTIONS == expected

    def test_governance_result_to_dict(self):
        r = GovernanceResult(
            action="pruned",
            target_store="working_memory",
            target_path="/tmp/test.json",
            rule_name="test_rule",
            dry_run=False,
        )
        d = r.to_dict()
        assert d["action"] == "pruned"
        assert d["target_store"] == "working_memory"
        assert d["dry_run"] is False

    def test_sweep_summary_to_dict(self):
        s = SweepSummary(
            sweep_id="gov-1",
            dry_run=True,
            started_at="2026-03-13T00:00:00Z",
            items_examined=10,
            items_acted_on=3,
        )
        d = s.to_dict()
        assert d["sweep_id"] == "gov-1"
        assert d["items_examined"] == 10
        assert isinstance(d["results"], list)


# ---------------------------------------------------------------------------
# Test: Protection classification
# ---------------------------------------------------------------------------


class TestProtectionClassification:
    """Protection level assignment."""

    def test_explicit_protected_flag(self, tmp_path):
        p = tmp_path / "test.json"
        assert classify_protection(p, {"protected": True}) == ProtectionLevel.PROTECTED

    def test_agent_patterns_always_protected(self, tmp_path):
        p = tmp_path / "agent_patterns" / "mem_1.json"
        assert classify_protection(p) == ProtectionLevel.PROTECTED

    def test_config_protected(self, tmp_path):
        p = tmp_path / "config" / "flags.json"
        assert classify_protection(p) == ProtectionLevel.PROTECTED

    def test_active_open_loop_protected(self, tmp_path):
        p = tmp_path / "open_loops" / "loop_1.json"
        for status in ("proposed", "open", "blocked", "deferred"):
            assert classify_protection(p, {"status": status}) == ProtectionLevel.PROTECTED

    def test_stale_open_loop_low(self, tmp_path):
        p = tmp_path / "open_loops" / "loop_1.json"
        assert classify_protection(p, {"status": "stale"}) == ProtectionLevel.LOW

    def test_terminal_open_loop_none(self, tmp_path):
        p = tmp_path / "open_loops" / "loop_1.json"
        assert classify_protection(p, {"status": "resolved"}) == ProtectionLevel.NONE

    def test_high_importance_episodic_medium(self, tmp_path):
        p = tmp_path / "workflow_learnings" / "mem_1.json"
        assert classify_protection(p, {"importance": 0.7}) == ProtectionLevel.MEDIUM

    def test_low_importance_episodic_low(self, tmp_path):
        p = tmp_path / "workflow_learnings" / "mem_1.json"
        assert classify_protection(p, {"importance": 0.3}) == ProtectionLevel.LOW

    def test_operator_authored_high(self, tmp_path):
        p = tmp_path / "test.json"
        assert classify_protection(p, {"source": "operator"}) == ProtectionLevel.HIGH

    def test_is_protected_helper(self):
        assert is_protected(ProtectionLevel.PROTECTED) is True
        assert is_protected(ProtectionLevel.HIGH) is True
        assert is_protected(ProtectionLevel.MEDIUM) is False
        assert is_protected(ProtectionLevel.LOW) is False
        assert is_protected(ProtectionLevel.NONE) is False


# ---------------------------------------------------------------------------
# Test: Open loop governance
# ---------------------------------------------------------------------------


class TestOpenLoopGovernance:
    """Governance for STATE/open_loops/."""

    def test_active_loop_within_threshold_protected(self, tmp_path):
        engine, state, _, _ = _make_engine(tmp_path)
        loop_dir = state / "open_loops"
        _make_open_loop(loop_dir, age_days=5)  # 5d < 14d threshold

        results = engine.govern_open_loops(dry_run=True)
        assert len(results) == 1
        assert results[0].action == "protected_skip"

    def test_active_loop_over_threshold_marked_stale(self, tmp_path):
        engine, state, _, _ = _make_engine(tmp_path)
        loop_dir = state / "open_loops"
        _make_open_loop(loop_dir, age_days=20)  # 20d > 14d threshold

        results = engine.govern_open_loops(dry_run=True)
        assert len(results) == 1
        assert results[0].action == "marked_stale"
        assert results[0].rule_name == "open_loop_stale_14d"
        assert results[0].dry_run is True

    def test_stale_loop_archived_after_60d(self, tmp_path):
        engine, state, _, _ = _make_engine(tmp_path)
        loop_dir = state / "open_loops"
        _make_open_loop(loop_dir, status="stale", age_days=65)

        results = engine.govern_open_loops(dry_run=False)
        assert len(results) == 1
        assert results[0].action == "archived"
        assert results[0].rule_name == "open_loop_stale_archive_60d"
        # File should be moved to archive
        archive_dir = state / "_archive" / "open_loops"
        assert archive_dir.exists()

    def test_terminal_loop_archived_after_30d(self, tmp_path):
        engine, state, _, _ = _make_engine(tmp_path)
        loop_dir = state / "open_loops"
        _make_open_loop(loop_dir, status="resolved", age_days=35)

        results = engine.govern_open_loops(dry_run=False)
        assert len(results) == 1
        assert results[0].action == "archived"
        assert results[0].rule_name == "open_loop_terminal_archive_30d"

    def test_recent_terminal_loop_no_action(self, tmp_path):
        engine, state, _, _ = _make_engine(tmp_path)
        loop_dir = state / "open_loops"
        _make_open_loop(loop_dir, status="resolved", age_days=10)

        results = engine.govern_open_loops(dry_run=True)
        assert len(results) == 1
        assert results[0].action == "no_action"


# ---------------------------------------------------------------------------
# Test: Session governance
# ---------------------------------------------------------------------------


class TestSessionGovernance:
    """Governance for STATE/sessions/."""

    def test_recent_session_no_action(self, tmp_path):
        engine, state, _, _ = _make_engine(tmp_path)
        ses_dir = state / "sessions"
        _make_session(ses_dir, age_days=5)

        results = engine.govern_sessions(dry_run=True)
        assert len(results) == 1
        assert results[0].action == "no_action"

    def test_30d_session_marked_stale(self, tmp_path):
        engine, state, _, _ = _make_engine(tmp_path)
        ses_dir = state / "sessions"
        _make_session(ses_dir, age_days=35)

        results = engine.govern_sessions(dry_run=True)
        assert len(results) == 1
        assert results[0].action == "marked_stale"

    def test_60d_session_archived(self, tmp_path):
        engine, state, _, _ = _make_engine(tmp_path)
        ses_dir = state / "sessions"
        _make_session(ses_dir, age_days=65)

        results = engine.govern_sessions(dry_run=False)
        assert len(results) == 1
        assert results[0].action == "archived"
        # File should be gone from original location
        assert not (ses_dir / "ses_test.json").exists()
        # And present in archive
        archive_dir = state / "_archive" / "sessions"
        assert archive_dir.exists()
        assert len(list(archive_dir.iterdir())) == 1


# ---------------------------------------------------------------------------
# Test: Working memory governance
# ---------------------------------------------------------------------------


class TestWorkingMemoryGovernance:
    """Governance for STATE/working_memory/."""

    def test_recent_wm_no_action(self, tmp_path):
        engine, state, _, _ = _make_engine(tmp_path)
        wm_dir = state / "working_memory"
        _make_wm(wm_dir, age_days=3)

        results = engine.govern_working_memory(dry_run=True)
        assert len(results) == 1
        assert results[0].action == "no_action"

    def test_old_wm_pruned(self, tmp_path):
        engine, state, _, _ = _make_engine(tmp_path)
        wm_dir = state / "working_memory"
        _make_wm(wm_dir, age_days=10)

        results = engine.govern_working_memory(dry_run=False)
        assert len(results) == 1
        assert results[0].action == "pruned"
        assert results[0].rule_name == "working_memory_prune_7d"
        # File should be deleted
        assert not (wm_dir / "wm_test.json").exists()

    def test_old_wm_dry_run_preserves_file(self, tmp_path):
        engine, state, _, _ = _make_engine(tmp_path)
        wm_dir = state / "working_memory"
        f = _make_wm(wm_dir, age_days=10)

        results = engine.govern_working_memory(dry_run=True)
        assert results[0].action == "pruned"
        assert results[0].dry_run is True
        # File should still exist
        assert f.exists()


# ---------------------------------------------------------------------------
# Test: Notification governance
# ---------------------------------------------------------------------------


class TestNotificationGovernance:
    """Governance for STATE/notified/."""

    def test_old_notification_pruned(self, tmp_path):
        engine, state, _, _ = _make_engine(tmp_path)
        notif_dir = state / "notified"
        _make_notification(notif_dir, age_days=20)

        results = engine._sweep_notifications(dry_run=False)
        assert len(results) == 1
        assert results[0].action == "pruned"
        assert results[0].rule_name == "notification_prune_14d"

    def test_recent_notification_not_touched(self, tmp_path):
        engine, state, _, _ = _make_engine(tmp_path)
        notif_dir = state / "notified"
        _make_notification(notif_dir, age_days=5)

        results = engine._sweep_notifications(dry_run=True)
        assert len(results) == 0  # Below threshold, not reported


# ---------------------------------------------------------------------------
# Test: Log rotation governance
# ---------------------------------------------------------------------------


class TestLogRotationGovernance:
    """Governance for LOGS/structured.*.jsonl."""

    def test_old_rotated_log_pruned(self, tmp_path):
        engine, _, _, logs = _make_engine(tmp_path)
        _make_rotated_log(logs, age_days=35)

        results = engine._sweep_rotated_logs(dry_run=False)
        assert len(results) == 1
        assert results[0].action == "pruned"
        assert results[0].rule_name == "rotated_log_prune_30d"

    def test_recent_rotated_log_kept(self, tmp_path):
        engine, _, _, logs = _make_engine(tmp_path)
        _make_rotated_log(logs, age_days=10)

        results = engine._sweep_rotated_logs(dry_run=True)
        assert len(results) == 0


# ---------------------------------------------------------------------------
# Test: Workflow learning governance (episodic)
# ---------------------------------------------------------------------------


class TestWorkflowLearningGovernance:
    """Governance for MEMORY/workflow_learnings/."""

    def test_normal_artifact_no_action(self, tmp_path):
        engine, _, memory, _ = _make_engine(tmp_path)
        wl_dir = memory / "workflow_learnings"
        _make_workflow_learning(wl_dir, age_days=30, importance=0.3)

        results = engine._sweep_workflow_learnings(dry_run=True)
        assert len(results) == 1
        assert results[0].action == "no_action"

    def test_thin_old_artifact_archived(self, tmp_path):
        engine, _, memory, _ = _make_engine(tmp_path)
        wl_dir = memory / "workflow_learnings"
        _make_workflow_learning(
            wl_dir,
            age_days=100,
            summary="short",
            importance=0.2,
        )

        results = engine._sweep_workflow_learnings(dry_run=False)
        assert len(results) == 1
        assert results[0].action == "archived"
        assert results[0].rule_name == "episodic_thin_archive_90d"

    def test_high_importance_artifact_protected(self, tmp_path):
        engine, _, memory, _ = _make_engine(tmp_path)
        wl_dir = memory / "workflow_learnings"
        _make_workflow_learning(
            wl_dir,
            age_days=100,
            summary="short",
            importance=0.8,
        )

        results = engine._sweep_workflow_learnings(dry_run=True)
        assert len(results) == 1
        assert results[0].action == "protected_skip"
        assert results[0].protection_level == ProtectionLevel.MEDIUM


# ---------------------------------------------------------------------------
# Test: Operational state governance
# ---------------------------------------------------------------------------


class TestOperationalStateGovernance:
    """Governance for STATE/{workflows,plans,improvement_runs,intents}/."""

    def test_old_workflow_archived(self, tmp_path):
        engine, state, _, _ = _make_engine(tmp_path)
        wf_dir = state / "workflows"
        wf_dir.mkdir()
        _write_json(
            wf_dir / "wf_old.json",
            {"status": "completed"},
            mtime=_NOW - 40 * _DAY,
        )

        results = engine._sweep_operational_state(dry_run=False)
        assert len(results) == 1
        assert results[0].action == "archived"
        assert results[0].rule_name == "operational_workflows_archive_30d"

    def test_recent_workflow_not_touched(self, tmp_path):
        engine, state, _, _ = _make_engine(tmp_path)
        wf_dir = state / "workflows"
        wf_dir.mkdir()
        _write_json(wf_dir / "wf_new.json", {"status": "active"}, mtime=_NOW - 5 * _DAY)

        results = engine._sweep_operational_state(dry_run=True)
        assert len(results) == 0  # Within retention, not reported for non-protected


# ---------------------------------------------------------------------------
# Test: Dry-run behavior
# ---------------------------------------------------------------------------


class TestDryRunBehavior:
    """Verify dry-run mode preserves all files."""

    def test_dry_run_sweep_no_file_changes(self, tmp_path):
        engine, state, memory, logs = _make_engine(tmp_path)

        # Create files in multiple stores
        loop_dir = state / "open_loops"
        _make_open_loop(loop_dir, age_days=20)
        ses_dir = state / "sessions"
        _make_session(ses_dir, age_days=65)
        wm_dir = state / "working_memory"
        _make_wm(wm_dir, age_days=10)

        summary = engine.run_sweep(dry_run=True)

        # All files should still exist
        assert list(loop_dir.glob("loop_*.json"))
        assert list(ses_dir.glob("ses_*.json"))
        assert list(wm_dir.glob("wm_*.json"))
        assert summary.dry_run is True
        assert summary.items_acted_on > 0  # Actions were planned

    def test_execute_sweep_removes_files(self, tmp_path):
        engine, state, _, _ = _make_engine(tmp_path)
        wm_dir = state / "working_memory"
        _make_wm(wm_dir, age_days=10)

        summary = engine.run_sweep(dry_run=False)
        assert not list(wm_dir.glob("wm_*.json"))
        assert summary.items_acted_on >= 1


# ---------------------------------------------------------------------------
# Test: Full sweep integration
# ---------------------------------------------------------------------------


class TestFullSweep:
    """End-to-end governance sweep across all stores."""

    def test_mixed_stores_sweep(self, tmp_path):
        engine, state, memory, logs = _make_engine(tmp_path)

        # Open loops: 1 active (recent), 1 resolved (old)
        loop_dir = state / "open_loops"
        _make_open_loop(loop_dir, loop_id="ol-active", status="open", age_days=5)
        _make_open_loop(loop_dir, loop_id="ol-resolved", status="resolved", age_days=35)

        # Sessions: 1 recent, 1 old
        ses_dir = state / "sessions"
        _make_session(ses_dir, name="ses_new.json", age_days=5)
        _make_session(ses_dir, name="ses_old.json", age_days=65)

        # Working memory: 1 expired
        wm_dir = state / "working_memory"
        _make_wm(wm_dir, age_days=10)

        summary = engine.run_sweep(dry_run=True)

        assert summary.items_examined >= 5
        assert summary.items_protected >= 1  # Active loop
        assert summary.items_acted_on >= 2  # At least WM prune + session archive
        assert summary.sweep_id.startswith("gov-")
        assert summary.completed_at

    def test_empty_stores_no_errors(self, tmp_path):
        engine, _, _, _ = _make_engine(tmp_path)
        summary = engine.run_sweep(dry_run=True)
        assert summary.items_examined == 0
        assert len(summary.errors) == 0


# ---------------------------------------------------------------------------
# Test: Router integration
# ---------------------------------------------------------------------------


class TestRouterGovernanceIntegration:
    """Router.run_governance() delegates correctly."""

    def test_router_governance_dry_run(self, tmp_path):
        from agents.memory_router import MemoryFileAdapter, MemoryRouter, WorkingMemoryAdapter

        (tmp_path / "workflow_learnings").mkdir(parents=True, exist_ok=True)
        router = MemoryRouter(
            adapters=[
                MemoryFileAdapter(base=tmp_path),
                WorkingMemoryAdapter(base=tmp_path),
            ],
            memory_file_base=tmp_path,
            loop_store_base=tmp_path / "open_loops",
        )
        result = router.run_governance(dry_run=True, caller="test")
        assert result["dry_run"] is True
        assert "items_examined" in result

    def test_convenience_function(self, tmp_path):
        state, memory, logs = _setup_state(tmp_path)
        result = run_governance_sweep(
            dry_run=True,
            state_dir=state,
            memory_dir=memory,
            logs_dir=logs,
        )
        assert result["dry_run"] is True
        assert "results" in result


# ---------------------------------------------------------------------------
# Test: Stale marking specifics
# ---------------------------------------------------------------------------


class TestStaleMarking:
    """Stale marking is a first-class governance outcome."""

    def test_stale_marking_uses_updated_at(self, tmp_path):
        """Stale threshold uses updated_at from loop metadata, not file mtime."""
        engine, state, _, _ = _make_engine(tmp_path)
        loop_dir = state / "open_loops"
        # File mtime is recent, but updated_at is old
        old_ts = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(_NOW - 20 * _DAY),
        )
        _make_open_loop(
            loop_dir,
            age_days=0,  # File mtime is now
        )
        # Overwrite with old updated_at
        f = loop_dir / "loop_ol-test-1.json"
        data = json.loads(f.read_text())
        data["updated_at"] = old_ts
        f.write_text(json.dumps(data))

        results = engine.govern_open_loops(dry_run=True)
        assert results[0].action == "marked_stale"

    def test_stale_execute_modifies_loop(self, tmp_path):
        engine, state, _, _ = _make_engine(tmp_path)
        loop_dir = state / "open_loops"
        _make_open_loop(loop_dir, age_days=20)

        results = engine.govern_open_loops(dry_run=False)
        assert results[0].action == "marked_stale"

        # Verify loop file was updated
        f = loop_dir / "loop_ol-test-1.json"
        data = json.loads(f.read_text())
        assert data["status"] == "stale"
