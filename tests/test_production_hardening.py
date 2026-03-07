"""Tests for Phase 7.7 — Production Hardening.

Covers:
  - Feature flags (fail-closed behavior)
  - Rate limiting (deterministic, bounded)
  - Archive/cleanup rules
  - Manual approval hooks
  - Policy denial auditing
  - Restart recovery (workflows survive restart)
"""

import json
import os
import time

import pytest

from agents.production_hardening import (
    FeatureFlags,
    RateLimiter,
    RateCheckResult,
    ArchiveManager,
    ApprovalGate,
    RestartRecovery,
    audit_policy_denial,
    run_production_hardening,
    ARCHIVE_AFTER_S,
    MAX_ARCHIVE_KEEP,
    MAX_WORKFLOWS_PER_HOUR,
    MAX_AGENT_SPAWNS_PER_HOUR,
    APPROVAL_TIMEOUT_S,
    HIGH_RISK_TOOLS,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def setup_tmpdir(tmp_path):
    """Create standard STATE/ layout for isolated tests."""
    os.environ["NOVACORE_ROOT"] = str(tmp_path)
    (tmp_path / "STATE" / "config").mkdir(parents=True)
    (tmp_path / "STATE" / "workflows").mkdir(parents=True)
    (tmp_path / "STATE" / "leases").mkdir(parents=True)
    (tmp_path / "STATE" / "agents" / "runtime").mkdir(parents=True)
    (tmp_path / "STATE" / "running").mkdir(parents=True)
    (tmp_path / "STATE" / "approvals").mkdir(parents=True)
    (tmp_path / "TASKS").mkdir(parents=True)
    (tmp_path / "LOGS").mkdir(parents=True)
    yield tmp_path
    del os.environ["NOVACORE_ROOT"]


def _write_feature_flags(tmp_path, flags):
    path = tmp_path / "STATE" / "config" / "feature_flags.json"
    path.write_text(json.dumps(flags, indent=2))


# ===========================================================================
# Feature Flags — fail-closed
# ===========================================================================

class TestFeatureFlags:

    def test_missing_file_defaults_off(self, tmp_path):
        """Missing feature_flags.json → all flags OFF."""
        ff = FeatureFlags(tmp_path)
        assert ff.is_multi_agent_enabled() is False
        assert ff.is_manual_approval_enabled() is False
        assert ff.is_archive_enabled() is False
        assert ff.is_rate_limiting_enabled() is False

    def test_corrupt_file_defaults_off(self, tmp_path):
        """Corrupt JSON → all flags OFF."""
        (tmp_path / "STATE" / "config" / "feature_flags.json").write_text(
            "NOT VALID JSON {{{")
        ff = FeatureFlags(tmp_path)
        assert ff.is_multi_agent_enabled() is False

    def test_empty_object_defaults_off(self, tmp_path):
        _write_feature_flags(tmp_path, {})
        ff = FeatureFlags(tmp_path)
        assert ff.is_multi_agent_enabled() is False

    def test_orchestrator_enabled(self, tmp_path):
        _write_feature_flags(tmp_path, {
            "phase7_orchestrator": {"enabled": True}
        })
        ff = FeatureFlags(tmp_path)
        assert ff.is_multi_agent_enabled() is True

    def test_orchestrator_disabled(self, tmp_path):
        _write_feature_flags(tmp_path, {
            "phase7_orchestrator": {"enabled": False}
        })
        ff = FeatureFlags(tmp_path)
        assert ff.is_multi_agent_enabled() is False

    def test_hardening_flags(self, tmp_path):
        _write_feature_flags(tmp_path, {
            "phase7_hardening": {
                "manual_approval": True,
                "archive_cleanup": True,
                "rate_limiting": False,
            }
        })
        ff = FeatureFlags(tmp_path)
        assert ff.is_manual_approval_enabled() is True
        assert ff.is_archive_enabled() is True
        assert ff.is_rate_limiting_enabled() is False

    def test_reload_picks_up_changes(self, tmp_path):
        _write_feature_flags(tmp_path, {
            "phase7_orchestrator": {"enabled": False}
        })
        ff = FeatureFlags(tmp_path)
        assert ff.is_multi_agent_enabled() is False

        _write_feature_flags(tmp_path, {
            "phase7_orchestrator": {"enabled": True}
        })
        ff.reload()
        assert ff.is_multi_agent_enabled() is True

    def test_non_bool_enabled_fails_closed(self, tmp_path):
        """enabled: "yes" (string, not bool) → OFF."""
        _write_feature_flags(tmp_path, {
            "phase7_orchestrator": {"enabled": "yes"}
        })
        ff = FeatureFlags(tmp_path)
        assert ff.is_multi_agent_enabled() is False

    def test_orchestrator_config(self, tmp_path):
        cfg = {"enabled": True, "supported_classes": ["research"]}
        _write_feature_flags(tmp_path, {"phase7_orchestrator": cfg})
        ff = FeatureFlags(tmp_path)
        assert ff.orchestrator_config() == cfg


# ===========================================================================
# Rate Limiting
# ===========================================================================

class TestRateLimiter:

    def test_empty_state_allows(self, tmp_path):
        rl = RateLimiter(tmp_path)
        result = rl.check_rate("test", 5, 3600)
        assert result.allowed is True
        assert result.count == 0
        assert result.remaining == 5

    def test_record_and_check(self, tmp_path):
        rl = RateLimiter(tmp_path)
        rl.record_event("test", 3600)
        rl.record_event("test", 3600)
        result = rl.check_rate("test", 5, 3600)
        assert result.allowed is True
        assert result.count == 2
        assert result.remaining == 3

    def test_limit_reached_blocks(self, tmp_path):
        rl = RateLimiter(tmp_path)
        for _ in range(5):
            rl.record_event("test", 3600)
        result = rl.check_rate("test", 5, 3600)
        assert result.allowed is False
        assert result.count == 5
        assert result.remaining == 0

    def test_expired_events_pruned(self, tmp_path):
        rl = RateLimiter(tmp_path)
        # Write old events directly
        state = {"test": {"events": [time.time() - 7200, time.time() - 7100]}}
        (tmp_path / "STATE" / "rate_limits.json").write_text(json.dumps(state))
        # Old events should be pruned for a 1-hour window
        result = rl.check_rate("test", 5, 3600)
        assert result.count == 0
        assert result.allowed is True

    def test_workflow_launch_check(self, tmp_path):
        rl = RateLimiter(tmp_path)
        result = rl.check_workflow_launch()
        assert result.allowed is True
        assert result.limit == MAX_WORKFLOWS_PER_HOUR

    def test_agent_spawn_check(self, tmp_path):
        rl = RateLimiter(tmp_path)
        result = rl.check_agent_spawn()
        assert result.allowed is True
        assert result.limit == MAX_AGENT_SPAWNS_PER_HOUR

    def test_corrupt_state_file_handled(self, tmp_path):
        (tmp_path / "STATE" / "rate_limits.json").write_text("NOT JSON")
        rl = RateLimiter(tmp_path)
        result = rl.check_rate("test", 5, 3600)
        assert result.allowed is True
        assert result.count == 0

    def test_categories_are_independent(self, tmp_path):
        rl = RateLimiter(tmp_path)
        for _ in range(5):
            rl.record_event("alpha", 3600)
        assert rl.check_rate("alpha", 5, 3600).allowed is False
        assert rl.check_rate("beta", 5, 3600).allowed is True


# ===========================================================================
# Archive / Cleanup
# ===========================================================================

class TestArchiveManager:

    def test_empty_state_no_errors(self, tmp_path):
        am = ArchiveManager(tmp_path)
        result = am.run_cleanup()
        assert result["archived_workflows"] == []
        assert result["archived_agents"] == []
        assert result["cleaned_leases"] == []

    def test_active_workflow_not_archived(self, tmp_path):
        wf = {"status": "executing", "created_at": time.time()}
        (tmp_path / "STATE" / "workflows" / "wf_001.json").write_text(
            json.dumps(wf))
        am = ArchiveManager(tmp_path)
        assert am.archive_completed_workflows() == []

    def test_recent_completed_workflow_not_archived(self, tmp_path):
        wf = {"status": "completed", "completed_at": time.time()}
        (tmp_path / "STATE" / "workflows" / "wf_001.json").write_text(
            json.dumps(wf))
        am = ArchiveManager(tmp_path)
        assert am.archive_completed_workflows() == []

    def test_old_completed_workflow_archived(self, tmp_path):
        wf = {"status": "completed",
              "completed_at": time.time() - ARCHIVE_AFTER_S - 100}
        wf_path = tmp_path / "STATE" / "workflows" / "wf_001.json"
        wf_path.write_text(json.dumps(wf))
        am = ArchiveManager(tmp_path)
        archived = am.archive_completed_workflows()
        assert "wf_001.json" in archived
        assert not wf_path.exists()
        assert (tmp_path / "STATE" / "archive" / "workflows" / "wf_001.json").exists()

    def test_halted_workflow_archived(self, tmp_path):
        wf = {"status": "halted",
              "updated_at": time.time() - ARCHIVE_AFTER_S - 100}
        (tmp_path / "STATE" / "workflows" / "wf_h.json").write_text(
            json.dumps(wf))
        am = ArchiveManager(tmp_path)
        assert "wf_h.json" in am.archive_completed_workflows()

    def test_failed_workflow_archived(self, tmp_path):
        wf = {"status": "failed",
              "completed_at": time.time() - ARCHIVE_AFTER_S - 100}
        (tmp_path / "STATE" / "workflows" / "wf_f.json").write_text(
            json.dumps(wf))
        am = ArchiveManager(tmp_path)
        assert "wf_f.json" in am.archive_completed_workflows()

    def test_agent_runtime_archived(self, tmp_path):
        rt = {"status": "completed",
              "updated_at": time.time() - ARCHIVE_AFTER_S - 100}
        rt_path = tmp_path / "STATE" / "agents" / "runtime" / "agent_001.json"
        rt_path.write_text(json.dumps(rt))
        am = ArchiveManager(tmp_path)
        archived = am.archive_agent_runtime()
        assert "agent_001.json" in archived
        assert not rt_path.exists()
        assert (tmp_path / "STATE" / "archive" / "agents" / "agent_001.json").exists()

    def test_active_agent_not_archived(self, tmp_path):
        rt = {"status": "executing", "updated_at": time.time()}
        (tmp_path / "STATE" / "agents" / "runtime" / "agent_002.json").write_text(
            json.dumps(rt))
        am = ArchiveManager(tmp_path)
        assert am.archive_agent_runtime() == []

    def test_expired_lease_cleaned(self, tmp_path):
        lease = {"acquired_at": time.time() - 1200, "ttl_s": 600}
        (tmp_path / "STATE" / "leases" / "wf_n.json").write_text(
            json.dumps(lease))
        am = ArchiveManager(tmp_path)
        cleaned = am.cleanup_expired_leases()
        assert "wf_n.json" in cleaned
        assert not (tmp_path / "STATE" / "leases" / "wf_n.json").exists()

    def test_active_lease_not_cleaned(self, tmp_path):
        lease = {"acquired_at": time.time(), "ttl_s": 600}
        (tmp_path / "STATE" / "leases" / "wf_a.json").write_text(
            json.dumps(lease))
        am = ArchiveManager(tmp_path)
        assert am.cleanup_expired_leases() == []

    def test_stale_tmp_cleaned(self, tmp_path):
        tmp_file = tmp_path / "STATE" / "workflows" / "wf.tmp"
        tmp_file.write_text("orphan")
        # Set mtime to 2 hours ago
        old_time = time.time() - 7200
        os.utime(tmp_file, (old_time, old_time))
        am = ArchiveManager(tmp_path)
        cleaned = am.cleanup_stale_tmp_files()
        assert len(cleaned) == 1
        assert not tmp_file.exists()

    def test_recent_tmp_not_cleaned(self, tmp_path):
        tmp_file = tmp_path / "STATE" / "workflows" / "recent.tmp"
        tmp_file.write_text("recent")
        am = ArchiveManager(tmp_path)
        assert am.cleanup_stale_tmp_files() == []

    def test_archive_limit_enforced(self, tmp_path):
        archive_dir = tmp_path / "STATE" / "archive" / "workflows"
        archive_dir.mkdir(parents=True)
        # Create more than MAX_ARCHIVE_KEEP files
        for i in range(MAX_ARCHIVE_KEEP + 5):
            f = archive_dir / f"wf_{i:04d}.json"
            f.write_text("{}")
            # Stagger mtimes to make ordering deterministic
            os.utime(f, (time.time() + i, time.time() + i))

        am = ArchiveManager(tmp_path)
        removed = am.enforce_archive_limits()
        assert removed == 5
        remaining = list(archive_dir.glob("*.json"))
        assert len(remaining) == MAX_ARCHIVE_KEEP

    def test_run_cleanup_returns_summary(self, tmp_path):
        am = ArchiveManager(tmp_path)
        result = am.run_cleanup()
        assert "archived_workflows" in result
        assert "archived_agents" in result
        assert "cleaned_leases" in result
        assert "cleaned_tmp" in result
        assert "archive_trimmed" in result


# ===========================================================================
# Manual Approval Hooks
# ===========================================================================

class TestApprovalGate:

    def test_approval_disabled_by_default(self, tmp_path):
        """No feature flag → approval never required."""
        gate = ApprovalGate(tmp_path)
        assert gate.is_approval_required("repo.git.commit") is False

    def test_approval_enabled_for_high_risk(self, tmp_path):
        _write_feature_flags(tmp_path, {
            "phase7_hardening": {"manual_approval": True}
        })
        gate = ApprovalGate(tmp_path)
        assert gate.is_approval_required("repo.git.commit") is True
        assert gate.is_approval_required("system.service.restart") is True

    def test_approval_not_required_for_safe_tools(self, tmp_path):
        _write_feature_flags(tmp_path, {
            "phase7_hardening": {"manual_approval": True}
        })
        gate = ApprovalGate(tmp_path)
        assert gate.is_approval_required("repo.files.read") is False
        assert gate.is_approval_required("repo.search") is False

    def test_request_and_approve(self, tmp_path):
        gate = ApprovalGate(tmp_path)
        path = gate.request_approval("act_001", "repo.git.commit",
                                     "coder_001", "commit changes")
        assert path.exists()

        # Initially pending
        approved, reason = gate.check_approval("act_001")
        assert approved is False
        assert "pending" in reason

        # Approve
        assert gate.approve("act_001", approver="admin") is True
        approved, reason = gate.check_approval("act_001")
        assert approved is True
        assert "approved" in reason

    def test_request_and_deny(self, tmp_path):
        gate = ApprovalGate(tmp_path)
        gate.request_approval("act_002", "shell.run",
                              "coder_001", "run tests")

        assert gate.deny("act_002", reason="unsafe", denier="admin") is True
        approved, reason = gate.check_approval("act_002")
        assert approved is False
        assert "unsafe" in reason

    def test_approval_timeout(self, tmp_path):
        gate = ApprovalGate(tmp_path)
        path = gate.request_approval("act_003", "repo.git.commit",
                                     "coder_001", "commit")

        # Artificially expire the timeout
        data = json.loads(path.read_text())
        data["timeout_at"] = time.time() - 1
        path.write_text(json.dumps(data))

        approved, reason = gate.check_approval("act_003")
        assert approved is False
        assert "timed out" in reason

    def test_check_nonexistent_approval(self, tmp_path):
        gate = ApprovalGate(tmp_path)
        approved, reason = gate.check_approval("nonexistent")
        assert approved is False
        assert "no approval request found" in reason

    def test_approve_nonexistent(self, tmp_path):
        gate = ApprovalGate(tmp_path)
        assert gate.approve("nonexistent") is False

    def test_deny_nonexistent(self, tmp_path):
        gate = ApprovalGate(tmp_path)
        assert gate.deny("nonexistent") is False

    def test_high_risk_tools_set(self):
        """Verify the high-risk tools set is non-empty and bounded."""
        assert len(HIGH_RISK_TOOLS) >= 2
        assert "repo.git.commit" in HIGH_RISK_TOOLS


# ===========================================================================
# Policy Denial Auditing
# ===========================================================================

class TestPolicyDenialAudit:

    def test_audit_creates_file(self, tmp_path):
        audit_policy_denial("agent_x", "shell.run",
                            "tool not allowed", base=tmp_path)
        audit_path = tmp_path / "STATE" / "policy_denials.jsonl"
        assert audit_path.exists()
        records = [json.loads(l) for l in audit_path.read_text().strip().split("\n")]
        assert len(records) == 1
        assert records[0]["agent_id"] == "agent_x"
        assert records[0]["tool_name"] == "shell.run"
        assert records[0]["allowed"] is False

    def test_audit_appends(self, tmp_path):
        audit_policy_denial("a1", "t1", "r1", base=tmp_path)
        audit_policy_denial("a2", "t2", "r2", base=tmp_path)
        audit_path = tmp_path / "STATE" / "policy_denials.jsonl"
        records = [json.loads(l) for l in audit_path.read_text().strip().split("\n")]
        assert len(records) == 2
        assert records[0]["agent_id"] == "a1"
        assert records[1]["agent_id"] == "a2"


# ===========================================================================
# Restart Recovery
# ===========================================================================

class TestRestartRecovery:

    def test_empty_state_no_errors(self, tmp_path):
        rr = RestartRecovery(tmp_path)
        result = rr.reconcile()
        assert result["total_actions"] == 0
        assert "recovered_at" in result

    def test_stale_pid_removed(self, tmp_path):
        pid_file = tmp_path / "STATE" / "running" / "task_001.pid"
        pid_file.write_text("999999")  # PID that doesn't exist
        rr = RestartRecovery(tmp_path)
        result = rr.reconcile()
        actions = [a for a in result["actions"]
                   if a["type"] == "stale_pid_removed"]
        assert len(actions) == 1
        assert not pid_file.exists()

    def test_expired_lease_recovered(self, tmp_path):
        lease = {"acquired_at": time.time() - 1200, "ttl_s": 600,
                 "workflow_id": "wf1", "node_id": "n1", "holder": "agent_x"}
        lease_path = tmp_path / "STATE" / "leases" / "wf1_n1.json"
        lease_path.write_text(json.dumps(lease))
        rr = RestartRecovery(tmp_path)
        result = rr.reconcile()
        lease_actions = [a for a in result["actions"]
                         if a["type"] == "lease_recovered"]
        assert len(lease_actions) == 1
        assert not lease_path.exists()

    def test_active_lease_not_recovered(self, tmp_path):
        lease = {"acquired_at": time.time(), "ttl_s": 600,
                 "workflow_id": "wf1", "node_id": "n1", "holder": "agent_x"}
        lease_path = tmp_path / "STATE" / "leases" / "wf1_n1.json"
        lease_path.write_text(json.dumps(lease))
        rr = RestartRecovery(tmp_path)
        result = rr.reconcile()
        lease_actions = [a for a in result["actions"]
                         if a["type"] == "lease_recovered"]
        assert len(lease_actions) == 0
        assert lease_path.exists()

    def test_stale_workflow_halted(self, tmp_path):
        """Workflow with no activity for 2x SLA gets halted."""
        wf = {
            "status": "executing",
            "created_at": time.time() - 7200,
            "updated_at": time.time() - 7200,
            "budget": {"max_runtime_s": 1800},
        }
        wf_path = tmp_path / "STATE" / "workflows" / "wf_stale.json"
        wf_path.write_text(json.dumps(wf))

        rr = RestartRecovery(tmp_path)
        result = rr.reconcile()

        halt_actions = [a for a in result["actions"]
                        if a["type"] == "workflow_halted"]
        assert len(halt_actions) == 1

        # Verify state on disk
        data = json.loads(wf_path.read_text())
        assert data["status"] == "halted"
        assert "restart_recovery_stale" in data["halt_reason"]

    def test_recoverable_workflow_nodes_reset(self, tmp_path):
        """Recent workflow with executing nodes → nodes reset to pending."""
        wf = {
            "status": "executing",
            "created_at": time.time() - 100,
            "updated_at": time.time() - 100,
            "budget": {"max_runtime_s": 1800},
            "node_states": {
                "n1": {"status": "executing", "retry_count": 0,
                       "max_retries": 1, "node_id": "n1",
                       "workflow_id": "wf_r"},
                "n2": {"status": "completed", "node_id": "n2",
                       "workflow_id": "wf_r"},
            },
        }
        wf_path = tmp_path / "STATE" / "workflows" / "wf_r.json"
        wf_path.write_text(json.dumps(wf))

        rr = RestartRecovery(tmp_path)
        result = rr.reconcile()

        reset_actions = [a for a in result["actions"]
                         if a["type"] == "workflow_nodes_reset"]
        assert len(reset_actions) == 1
        assert "1" in reset_actions[0]["detail"]

        # Verify state on disk
        data = json.loads(wf_path.read_text())
        assert data["node_states"]["n1"]["status"] == "pending"
        assert data["node_states"]["n1"]["retry_count"] == 1
        assert data["node_states"]["n2"]["status"] == "completed"

    def test_max_retries_exceeded_marks_failed(self, tmp_path):
        """Node with max retries already used → marked failed, not reset."""
        wf = {
            "status": "executing",
            "created_at": time.time() - 100,
            "updated_at": time.time() - 100,
            "budget": {"max_runtime_s": 1800},
            "node_states": {
                "n1": {"status": "executing", "retry_count": 1,
                       "max_retries": 1, "node_id": "n1",
                       "workflow_id": "wf_m"},
            },
        }
        wf_path = tmp_path / "STATE" / "workflows" / "wf_m.json"
        wf_path.write_text(json.dumps(wf))

        rr = RestartRecovery(tmp_path)
        result = rr.reconcile()

        data = json.loads(wf_path.read_text())
        assert data["node_states"]["n1"]["status"] == "failed"

    def test_completed_workflow_not_touched(self, tmp_path):
        wf = {"status": "completed", "completed_at": time.time()}
        wf_path = tmp_path / "STATE" / "workflows" / "wf_done.json"
        wf_path.write_text(json.dumps(wf))

        rr = RestartRecovery(tmp_path)
        result = rr.reconcile()

        wf_actions = [a for a in result["actions"]
                      if "wf_done" in a.get("file", "")]
        assert len(wf_actions) == 0

    def test_inprogress_task_requeued(self, tmp_path):
        """Task .inprogress with no running worker → requeued to .md."""
        ip = tmp_path / "TASKS" / "0042_test.md.inprogress"
        ip.write_text("# Test task")

        rr = RestartRecovery(tmp_path)
        result = rr.reconcile()

        requeue_actions = [a for a in result["actions"]
                           if a["type"] == "task_requeued"]
        assert len(requeue_actions) == 1
        assert not ip.exists()
        assert (tmp_path / "TASKS" / "0042_test.md").exists()

    def test_inprogress_task_with_running_worker_not_requeued(self, tmp_path):
        """Task .inprogress with a live PID → not requeued."""
        ip = tmp_path / "TASKS" / "0043_test.md.inprogress"
        ip.write_text("# Test task")

        # Write a PID file with our own PID (which is alive)
        pid_file = tmp_path / "STATE" / "running" / "0043_test.pid"
        pid_file.write_text(str(os.getpid()))

        rr = RestartRecovery(tmp_path)
        result = rr.reconcile()

        requeue_actions = [a for a in result["actions"]
                           if a["type"] == "task_requeued"]
        assert len(requeue_actions) == 0
        assert ip.exists()

    def test_recovery_log_written(self, tmp_path):
        """Recovery actions are logged to LOGS/recovery.log."""
        pid_file = tmp_path / "STATE" / "running" / "task_x.pid"
        pid_file.write_text("999999")

        rr = RestartRecovery(tmp_path)
        rr.reconcile()

        log_path = tmp_path / "LOGS" / "recovery.log"
        assert log_path.exists()
        content = log_path.read_text()
        assert "stale_pid_removed" in content

    def test_no_actions_no_log(self, tmp_path):
        """No recovery actions → no log entry."""
        rr = RestartRecovery(tmp_path)
        rr.reconcile()

        log_path = tmp_path / "LOGS" / "recovery.log"
        assert not log_path.exists()


# ===========================================================================
# Feature-flag-off path preserves safe single-agent behavior
# ===========================================================================

class TestFeatureFlagOffPath:

    def test_flag_off_disables_archive(self, tmp_path):
        """With archive_cleanup disabled, cleanup is skipped."""
        _write_feature_flags(tmp_path, {
            "phase7_hardening": {"archive_cleanup": False}
        })
        result = run_production_hardening(tmp_path)
        assert result["cleanup"] == "disabled"

    def test_flag_on_enables_archive(self, tmp_path):
        _write_feature_flags(tmp_path, {
            "phase7_hardening": {"archive_cleanup": True}
        })
        result = run_production_hardening(tmp_path)
        assert isinstance(result["cleanup"], dict)
        assert "archived_workflows" in result["cleanup"]

    def test_multi_agent_status_reported(self, tmp_path):
        _write_feature_flags(tmp_path, {
            "phase7_orchestrator": {"enabled": False}
        })
        result = run_production_hardening(tmp_path)
        assert result["multi_agent_enabled"] is False


# ===========================================================================
# Cleanup of completed workflows (full lifecycle test)
# ===========================================================================

class TestCleanupCompletedWorkflows:

    def test_full_lifecycle_archive(self, tmp_path):
        """Workflow created → completed → aged → archived → trimmed."""
        am = ArchiveManager(tmp_path)

        # Create 3 completed workflows with stale timestamps
        for i in range(3):
            wf = {"status": "completed",
                  "completed_at": time.time() - ARCHIVE_AFTER_S - 100 - i}
            (tmp_path / "STATE" / "workflows" / f"wf_{i}.json").write_text(
                json.dumps(wf))

        # Archive
        archived = am.archive_completed_workflows()
        assert len(archived) == 3

        # Verify moved
        assert list((tmp_path / "STATE" / "workflows").glob("*.json")) == []
        assert len(list((tmp_path / "STATE" / "archive" / "workflows").glob("*.json"))) == 3

    def test_delegations_and_contracts_preserved_for_active(self, tmp_path):
        """Active workflow state is never archived."""
        wf = {"status": "executing", "created_at": time.time() - 100000}
        wf_path = tmp_path / "STATE" / "workflows" / "wf_active.json"
        wf_path.write_text(json.dumps(wf))

        am = ArchiveManager(tmp_path)
        assert am.archive_completed_workflows() == []
        assert wf_path.exists()


# ===========================================================================
# Integration: run_production_hardening
# ===========================================================================

class TestIntegration:

    def test_returns_summary(self, tmp_path):
        _write_feature_flags(tmp_path, {
            "phase7_orchestrator": {"enabled": True},
            "phase7_hardening": {"archive_cleanup": True},
        })
        result = run_production_hardening(tmp_path)
        assert result["multi_agent_enabled"] is True
        assert isinstance(result["cleanup"], dict)
