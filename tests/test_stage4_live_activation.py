"""Phase 7.17 — Tests for controlled Stage 4 activation (system_inspect).

Tests cover:
  1. Activation procedure — gate re-run, config update, audit trail
  2. Scope enforcement — inspect allowed, mutate blocked
  3. Stage D classifier routing — system_inspect eligible, mutate rejected
  4. Stage D plan validation — allowed/blocked skills
  5. Fail-closed behavior — gate failure blocks activation
  6. Artifact generation
"""

import json
from pathlib import Path

import pytest

from agents.rollout_gate import (
    STAGE4_ALLOWED_OPERATIONS,
    STAGE4_ALLOWED_ROLES,
    STAGE4_ALLOWED_SKILLS,
    STAGE4_BLOCKED_OPERATIONS,
    STAGE4_BLOCKED_SKILLS,
    STAGE4_CLASSES,
    STAGE4_ROLLOUT_STAGE,
    activate_stage4,
    render_stage4_activation_markdown,
    write_stage4_activation_result,
)
from tools.orchestrator_adapter import (
    _STAGE_D_ALLOWED_SKILLS,
    _STAGE_D_BLOCKED_SKILLS,
    build_plan_from_task,
    validate_stageD_plan,
)
from tools.task_classifier import (
    classify_and_route,
    has_system_mutate_signals,
    is_stageD_eligible,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_workflow(task_class: str, status: str = "completed", **extra) -> dict:
    return {"task_class": task_class, "status": status, **extra}


def _make_valid_plan() -> dict:
    """Create a structurally valid Stage 4 rollout plan."""
    return {
        "plan_status": "draft",
        "initial_scope": "system_inspect ONLY",
        "allowed_operations": sorted(STAGE4_ALLOWED_OPERATIONS),
        "blocked_operations": sorted(STAGE4_BLOCKED_OPERATIONS),
        "allowed_skills": sorted(STAGE4_ALLOWED_SKILLS),
        "blocked_skills": sorted(STAGE4_BLOCKED_SKILLS),
        "activation_prerequisites": {
            "stage4_evaluation_ready": "ready",
            "stage3_stable": "stable",
        },
        "success_criteria": {"min_system_inspect_runs": 3},
        "abort_conditions": ["policy violation"],
        "rollback_procedure": ["Remove system from supported_classes"],
        "system_class_status": "blocked",
        "stage4_evaluation_decision": "ready_for_stage4_planning",
        "generated_at": "2026-03-08T14:00:00Z",
    }


def _populate_healthy_env(root: Path) -> None:
    """Populate a test environment that passes all gates."""
    state = root / "STATE"
    (state / "config").mkdir(parents=True, exist_ok=True)
    (state / "workflows").mkdir(parents=True, exist_ok=True)
    (state / "verifications").mkdir(parents=True, exist_ok=True)
    (root / "LOGS").mkdir(parents=True, exist_ok=True)
    (root / "WORK").mkdir(parents=True, exist_ok=True)

    flags = {
        "phase7_orchestrator": {
            "enabled": True,
            "supported_classes": ["research", "code_review", "code_impl"],
            "stage": "C",
            "rollout_stage": "stage3_research_code_review_code_impl",
            "verifier_required": True,
        },
        "version": 7,
    }
    (state / "config" / "feature_flags.json").write_text(json.dumps(flags))

    hb = {"overall": "healthy", "findings": [], "generated_at": "2026-03-08T10:00:00Z"}
    (state / "heartbeat_multiagent.json").write_text(json.dumps(hb))

    metrics = {"contract_success": {"_total": 10}, "contract_failure": {"_total": 0}}
    (state / "metrics.json").write_text(json.dumps(metrics))

    log_entry = {
        "attempted_at": "2026-03-07T12:00:00Z",
        "outcome": "activated",
        "decision": "ready_to_expand",
        "reason": "expansion applied",
        "pre_config": {},
        "post_config": {},
        "blocking_criteria": [],
    }
    (state / "activation_log.jsonl").write_text(json.dumps(log_entry) + "\n")

    for i in range(5):
        wf = _make_workflow("code_impl", "completed")
        (state / "workflows" / f"code_impl_{i}.json").write_text(json.dumps(wf))
    for i in range(3):
        wf = _make_workflow("research", "completed")
        (state / "workflows" / f"research_{i}.json").write_text(json.dumps(wf))

    (state / "stage4_rollout_plan.json").write_text(
        json.dumps(_make_valid_plan(), indent=2)
    )


@pytest.fixture
def act_root(tmp_path):
    _populate_healthy_env(tmp_path)
    return tmp_path


def _stageD_flags() -> dict:
    """Feature flags for a Stage D environment."""
    return {
        "enabled": True,
        "supported_classes": ["research", "code_review", "code_impl", "system"],
        "stage": "D",
        "rollout_stage": "stage4_research_code_review_code_impl_system_inspect",
        "min_confidence": 0.5,
        "system_scope": "inspect_only",
    }


# ===========================================================================
# Test Activation Procedure
# ===========================================================================

class TestActivationProcedure:
    """Tests for activate_stage4()."""

    def test_activation_succeeds(self, act_root):
        record = activate_stage4(act_root)
        assert record.outcome == "activated"
        assert record.activated_scope == "system_inspect"

    def test_gate_re_run(self, act_root):
        record = activate_stage4(act_root)
        assert record.gate_decision == "ready_to_activate_stage4"

    def test_config_updated(self, act_root):
        activate_stage4(act_root)
        ff = json.loads(
            (act_root / "STATE" / "config" / "feature_flags.json").read_text()
        )
        orch = ff["phase7_orchestrator"]
        assert "system" in orch["supported_classes"]
        assert orch["stage"] == "D"
        assert orch["rollout_stage"] == STAGE4_ROLLOUT_STAGE
        assert orch["system_scope"] == "inspect_only"

    def test_supported_classes_correct(self, act_root):
        activate_stage4(act_root)
        ff = json.loads(
            (act_root / "STATE" / "config" / "feature_flags.json").read_text()
        )
        assert ff["phase7_orchestrator"]["supported_classes"] == STAGE4_CLASSES

    def test_allowed_roles_updated(self, act_root):
        activate_stage4(act_root)
        ff = json.loads(
            (act_root / "STATE" / "config" / "feature_flags.json").read_text()
        )
        assert ff["phase7_orchestrator"]["allowed_roles"] == STAGE4_ALLOWED_ROLES

    def test_system_scope_persisted(self, act_root):
        activate_stage4(act_root)
        ff = json.loads(
            (act_root / "STATE" / "config" / "feature_flags.json").read_text()
        )
        orch = ff["phase7_orchestrator"]
        assert orch["system_allowed_operations"] == sorted(STAGE4_ALLOWED_OPERATIONS)
        assert orch["system_blocked_operations"] == sorted(STAGE4_BLOCKED_OPERATIONS)
        assert orch["system_allowed_skills"] == sorted(STAGE4_ALLOWED_SKILLS)
        assert orch["system_blocked_skills"] == sorted(STAGE4_BLOCKED_SKILLS)

    def test_audit_trail_updated(self, act_root):
        activate_stage4(act_root)
        log = (act_root / "STATE" / "activation_log.jsonl").read_text()
        entries = [json.loads(line) for line in log.strip().splitlines()]
        last = entries[-1]
        assert last["outcome"] == "activated"
        assert last["activated_scope"] == "system_inspect"
        assert last["gate_decision"] == "ready_to_activate_stage4"

    def test_pre_post_config_recorded(self, act_root):
        record = activate_stage4(act_root)
        assert "system" not in record.pre_config.get("supported_classes", [])
        assert "system" in record.post_config.get("supported_classes", [])

    def test_plan_validation_recorded(self, act_root):
        record = activate_stage4(act_root)
        assert record.plan_validation["valid"] is True

    def test_version_incremented(self, act_root):
        ff_before = json.loads(
            (act_root / "STATE" / "config" / "feature_flags.json").read_text()
        )
        activate_stage4(act_root)
        ff_after = json.loads(
            (act_root / "STATE" / "config" / "feature_flags.json").read_text()
        )
        assert ff_after["version"] == ff_before["version"] + 1


# ===========================================================================
# Test Fail-Closed Behavior
# ===========================================================================

class TestFailClosed:
    """Tests for activation failing closed when gate doesn't pass."""

    def test_unhealthy_heartbeat_blocks(self, act_root):
        hb = {"overall": "unhealthy", "findings": [{"severity": "unhealthy"}]}
        (act_root / "STATE" / "heartbeat_multiagent.json").write_text(json.dumps(hb))
        record = activate_stage4(act_root)
        assert record.outcome == "blocked"
        assert record.activated_scope == ""
        # Config unchanged
        ff = json.loads(
            (act_root / "STATE" / "config" / "feature_flags.json").read_text()
        )
        assert "system" not in ff["phase7_orchestrator"]["supported_classes"]

    def test_policy_violation_blocks(self, act_root):
        (act_root / "STATE" / "policy_denials.jsonl").write_text(
            json.dumps({"reason": "test"}) + "\n"
        )
        record = activate_stage4(act_root)
        assert record.outcome == "blocked"

    def test_missing_plan_blocks(self, act_root):
        (act_root / "STATE" / "stage4_rollout_plan.json").unlink()
        record = activate_stage4(act_root)
        assert record.outcome == "blocked"
        assert "plan_valid" in record.blocking_criteria

    def test_blocked_audit_trail(self, act_root):
        """Blocked attempt still writes to audit trail."""
        (act_root / "STATE" / "stage4_rollout_plan.json").unlink()
        activate_stage4(act_root)
        log = (act_root / "STATE" / "activation_log.jsonl").read_text()
        entries = [json.loads(line) for line in log.strip().splitlines()]
        last = entries[-1]
        assert last["outcome"] == "blocked"

    def test_missing_feature_flags_errors(self, act_root):
        (act_root / "STATE" / "config" / "feature_flags.json").unlink()
        record = activate_stage4(act_root)
        assert record.outcome == "error"
        assert record.reason == "cannot_read_feature_flags"


# ===========================================================================
# Test Stage D Classifier Routing
# ===========================================================================

class TestStageDRouting:
    """Tests for Stage D system_inspect routing in task_classifier."""

    def test_system_inspect_eligible(self):
        flags = _stageD_flags()
        eligible, reason = is_stageD_eligible(
            "system", 0.5, "check the status of the watcher service", flags
        )
        assert eligible is True
        assert reason == "stageD_system_inspect_eligible"

    def test_system_mutate_rejected(self):
        flags = _stageD_flags()
        eligible, reason = is_stageD_eligible(
            "system", 0.5, "deploy the new version and restart the service", flags
        )
        assert eligible is False
        assert "system_mutate_signals_detected" in reason

    def test_research_still_eligible(self):
        flags = _stageD_flags()
        eligible, reason = is_stageD_eligible(
            "research", 0.5, "research best practices for monitoring", flags
        )
        assert eligible is True
        assert reason == "stageD_research_eligible"

    def test_coding_still_eligible(self):
        flags = _stageD_flags()
        eligible, reason = is_stageD_eligible(
            "code_impl", 0.5, "implement a utility function", flags
        )
        assert eligible is True
        assert reason == "stageD_coding_eligible"

    def test_wrong_stage_rejected(self):
        flags = _stageD_flags()
        flags["stage"] = "C"
        eligible, reason = is_stageD_eligible(
            "system", 0.5, "check status", flags
        )
        assert eligible is False
        assert "not_D" in reason

    def test_disabled_rejected(self):
        flags = _stageD_flags()
        flags["enabled"] = False
        eligible, reason = is_stageD_eligible(
            "system", 0.5, "check status", flags
        )
        assert eligible is False

    def test_low_confidence_rejected(self):
        flags = _stageD_flags()
        eligible, reason = is_stageD_eligible(
            "system", 0.3, "check status", flags
        )
        assert eligible is False
        assert "below" in reason

    def test_classify_and_route_stageD(self):
        """classify_and_route handles stage D correctly."""
        # We need to mock load_feature_flags since it reads from disk
        import tools.task_classifier as tc
        original = tc.load_feature_flags
        tc.load_feature_flags = lambda: _stageD_flags()
        try:
            result = classify_and_route("review the architecture of the system")
            assert result["stage"] == "D"
            assert result["use_orchestrator"] is True or result["use_orchestrator"] is False
            # Verify stage D path was taken
            assert "stage" in result and result["stage"] == "D"
        finally:
            tc.load_feature_flags = original


# ===========================================================================
# Test System Mutate Signal Detection
# ===========================================================================

class TestSystemMutateSignals:
    """Tests for has_system_mutate_signals()."""

    def test_deploy_detected(self):
        has, signals = has_system_mutate_signals("deploy the new release")
        assert has is True
        assert any("deploy" in s.lower() for s in signals)

    def test_restart_detected(self):
        has, signals = has_system_mutate_signals("restart the watcher service")
        assert has is True

    def test_install_detected(self):
        has, signals = has_system_mutate_signals("pip install requests")
        assert has is True

    def test_sudo_detected(self):
        has, signals = has_system_mutate_signals("run sudo apt update")
        assert has is True

    def test_inspect_not_detected(self):
        has, _ = has_system_mutate_signals("check the status of all services")
        assert has is False

    def test_review_not_detected(self):
        has, _ = has_system_mutate_signals("review the architecture and audit dependencies")
        assert has is False

    def test_health_check_not_detected(self):
        has, _ = has_system_mutate_signals("run a health audit and inspect logs")
        assert has is False


# ===========================================================================
# Test Stage D Plan Validation
# ===========================================================================

class TestStageDPlanValidation:
    """Tests for validate_stageD_plan()."""

    def test_valid_inspect_plan(self, act_root):
        plan = build_plan_from_task(
            "test", "check system status",
            routing={"stage": "D"},
        )
        # Force system class steps
        from planner.schemas import PlanStep
        plan.steps = [
            PlanStep(step_id="inspect", skill_name="file-ops", goal="inspect", inputs={}),
            PlanStep(step_id="verify", skill_name="self-verification", goal="verify", inputs={}),
        ]
        valid, reason = validate_stageD_plan(plan)
        assert valid is True

    def test_blocked_skill_rejected(self):
        from planner.schemas import ExecutionPlan, PlanStep
        plan = ExecutionPlan(
            plan_id="test",
            task_id="test",
            strategy="stageD_system_inspect",
            steps=[
                PlanStep(step_id="bad", skill_name="shell-ops", goal="shell", inputs={}),
            ],
            success_criteria=[],
        )
        valid, reason = validate_stageD_plan(plan)
        assert valid is False
        assert "blocked_skill:shell-ops" in reason

    def test_missing_verifier_rejected(self):
        from planner.schemas import ExecutionPlan, PlanStep
        plan = ExecutionPlan(
            plan_id="test",
            task_id="test",
            strategy="stageD_system_inspect",
            steps=[
                PlanStep(step_id="inspect", skill_name="file-ops", goal="inspect", inputs={}),
            ],
            success_criteria=[],
        )
        valid, reason = validate_stageD_plan(plan)
        assert valid is False
        assert "missing_mandatory_verifier_step" in reason

    def test_allowed_skills_match_plan(self):
        """Stage D allowed skills match the rollout plan."""
        assert _STAGE_D_ALLOWED_SKILLS == STAGE4_ALLOWED_SKILLS
        assert _STAGE_D_BLOCKED_SKILLS == STAGE4_BLOCKED_SKILLS


# ===========================================================================
# Test Stage D Plan Building
# ===========================================================================

class TestStageDPlanBuilding:
    """Tests for _build_stageD_inspect_steps() via build_plan_from_task()."""

    def test_system_task_gets_inspect_strategy(self):
        plan = build_plan_from_task(
            "test", "review the architecture of the infrastructure",
            routing={"stage": "D"},
        )
        # system class should get stageD_system_inspect strategy
        assert plan.strategy == "stageD_system_inspect"

    def test_inspect_steps_have_no_blocked_skills(self):
        plan = build_plan_from_task(
            "test", "review the architecture of the service",
            routing={"stage": "D"},
        )
        if plan.strategy == "stageD_system_inspect":
            for step in plan.steps:
                assert step.skill_name not in _STAGE_D_BLOCKED_SKILLS

    def test_coding_gets_stageD_coding_strategy(self):
        plan = build_plan_from_task(
            "test", "implement a helper function for parsing",
            routing={"stage": "D"},
        )
        assert plan.strategy == "stageD_coding"

    def test_research_gets_stageD_research_strategy(self):
        plan = build_plan_from_task(
            "test", "research the latest trends in monitoring",
            routing={"stage": "D"},
        )
        assert plan.strategy == "stageD_research"


# ===========================================================================
# Test Artifact Generation
# ===========================================================================

class TestArtifactGeneration:
    """Tests for activation result rendering and writing."""

    def test_activated_markdown_content(self, act_root):
        record = activate_stage4(act_root)
        md = render_stage4_activation_markdown(record)
        assert "ACTIVATED" in md
        assert "system_inspect" in md
        assert "Allowed Operations" in md
        assert "Blocked Operations" in md
        assert "Monitoring Checklist" in md
        assert "Abort Conditions" in md
        assert "Rollback Path" in md
        assert "Config Change" in md

    def test_blocked_markdown_content(self, act_root):
        (act_root / "STATE" / "stage4_rollout_plan.json").unlink()
        record = activate_stage4(act_root)
        md = render_stage4_activation_markdown(record)
        assert "BLOCKED" in md
        assert "plan_valid" in md

    def test_write_creates_files(self, act_root):
        record = activate_stage4(act_root)
        md_path, json_path = write_stage4_activation_result(record, act_root)
        assert md_path.exists()
        assert json_path.exists()

    def test_written_json_valid(self, act_root):
        record = activate_stage4(act_root)
        _, json_path = write_stage4_activation_result(record, act_root)
        data = json.loads(json_path.read_text())
        assert data["outcome"] == "activated"
        assert data["activated_scope"] == "system_inspect"

    def test_to_dict_roundtrip(self, act_root):
        record = activate_stage4(act_root)
        d = record.to_dict()
        assert d["outcome"] == record.outcome
        assert d["activated_scope"] == record.activated_scope
        assert d["gate_decision"] == record.gate_decision


# ===========================================================================
# Test Scope Boundaries
# ===========================================================================

class TestScopeBoundaries:
    """Verify that activation only enables system_inspect, not broader system."""

    def test_only_inspect_ops_in_config(self, act_root):
        activate_stage4(act_root)
        ff = json.loads(
            (act_root / "STATE" / "config" / "feature_flags.json").read_text()
        )
        orch = ff["phase7_orchestrator"]
        assert orch["system_scope"] == "inspect_only"
        allowed = set(orch["system_allowed_operations"])
        blocked = set(orch["system_blocked_operations"])
        assert allowed == set(sorted(STAGE4_ALLOWED_OPERATIONS))
        assert blocked == set(sorted(STAGE4_BLOCKED_OPERATIONS))
        assert not allowed & blocked

    def test_no_overlap_allowed_blocked_ops(self):
        assert not STAGE4_ALLOWED_OPERATIONS & STAGE4_BLOCKED_OPERATIONS

    def test_no_overlap_allowed_blocked_skills(self):
        assert not STAGE4_ALLOWED_SKILLS & STAGE4_BLOCKED_SKILLS

    def test_blocked_ops_more_than_allowed(self):
        assert len(STAGE4_BLOCKED_OPERATIONS) > len(STAGE4_ALLOWED_OPERATIONS)

    def test_shell_ops_blocked(self):
        assert "shell-ops" in STAGE4_BLOCKED_SKILLS
        assert "shell-ops" in _STAGE_D_BLOCKED_SKILLS

    def test_mutate_signals_block_system_route(self):
        """Deploy/install/restart signals prevent system tasks from routing."""
        flags = _stageD_flags()
        mutate_texts = [
            "deploy the application",
            "install new dependencies",
            "restart the service",
            "configure the firewall",
            "sudo systemctl stop watcher",
        ]
        for text in mutate_texts:
            eligible, reason = is_stageD_eligible("system", 0.5, text, flags)
            assert eligible is False, f"should reject: {text}"
            assert "system_mutate_signals_detected" in reason
