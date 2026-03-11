"""Phase 7.15 — Tests for Stage 4 rollout plan (system class).

Tests cover:
  1. Plan structure and completeness
  2. Allowed vs blocked operations scope
  3. Allowed vs blocked skills
  4. Signal pattern coverage
  5. Activation prerequisites
  6. Success criteria and abort conditions
  7. Rollback procedure
  8. Artifact generation (markdown + JSON)
  9. Plan does not modify state
  10. Scope narrowness validation
"""

import json
import re
import time

import pytest

from agents.rollout_gate import (
    STAGE4_ABORT_CONDITIONS,
    STAGE4_ACTIVATION_PREREQUISITES,
    STAGE4_ALLOWED_OPERATIONS,
    STAGE4_ALLOWED_SKILLS,
    STAGE4_BLOCKED_OPERATIONS,
    STAGE4_BLOCKED_SKILLS,
    STAGE4_INSPECT_SIGNALS,
    STAGE4_MUTATE_SIGNALS,
    STAGE4_SUCCESS_CRITERIA,
    build_stage4_rollout_plan,
    render_stage4_plan_markdown,
    write_stage4_rollout_plan,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def plan_root(tmp_path):
    """Set up a Stage 3 environment with Stage 4 evaluation ready."""
    state = tmp_path / "STATE"
    (state / "config").mkdir(parents=True)
    (state / "workflows").mkdir(parents=True)
    (state / "verifications").mkdir(parents=True)
    (tmp_path / "LOGS").mkdir(parents=True)
    (tmp_path / "WORK").mkdir(parents=True)

    # Feature flags — Stage 3 active (system blocked)
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

    # Healthy heartbeat
    hb = {
        "overall": "healthy",
        "findings": [],
        "generated_at": "2026-03-08T10:00:00Z",
    }
    (state / "heartbeat_multiagent.json").write_text(json.dumps(hb))

    # Clean metrics
    metrics = {
        "contract_success": {"_total": 30},
        "contract_failure": {"_total": 0},
    }
    (state / "metrics.json").write_text(json.dumps(metrics))

    # Activation log
    activation = {
        "attempted_at": "2026-03-08T08:11:44Z",
        "outcome": "activated",
        "decision": "ready_to_expand",
        "reason": "Stage 3 expansion applied",
    }
    (state / "activation_log.jsonl").write_text(
        json.dumps(activation) + "\n"
    )

    # Add enough workflows for Stage 4 evaluation to pass
    for i in range(5):
        wf = {
            "workflow_id": f"ci_{i}",
            "status": "completed",
            "task_class": "code_impl",
            "stage": "C",
            "execution_path": "orchestrator",
            "created_at": time.time(),
            "completed_at": time.time(),
        }
        (state / "workflows" / f"ci_{i}.json").write_text(json.dumps(wf))

    wf = {
        "workflow_id": "res_1",
        "status": "completed",
        "task_class": "research",
        "stage": "C",
        "execution_path": "orchestrator",
        "created_at": time.time(),
        "completed_at": time.time(),
    }
    (state / "workflows" / "res_1.json").write_text(json.dumps(wf))

    return tmp_path


# ===================================================================
# Part 1 — Plan structure and completeness
# ===================================================================

class TestPlanStructure:
    """Rollout plan has all required fields."""

    def test_plan_has_all_fields(self, plan_root):
        """Plan dataclass includes every required field."""
        plan = build_stage4_rollout_plan(plan_root)
        d = plan.to_dict()
        required = {
            "plan_status", "initial_scope",
            "allowed_operations", "blocked_operations",
            "allowed_skills", "blocked_skills",
            "activation_prerequisites", "success_criteria",
            "abort_conditions", "rollback_procedure",
            "system_class_status", "stage4_evaluation_decision",
            "generated_at",
        }
        assert required <= set(d.keys())

    def test_plan_status_is_draft(self, plan_root):
        """Plan status starts as draft."""
        plan = build_stage4_rollout_plan(plan_root)
        assert plan.plan_status == "draft"

    def test_system_class_blocked(self, plan_root):
        """system class status is blocked."""
        plan = build_stage4_rollout_plan(plan_root)
        assert plan.system_class_status == "blocked"

    def test_evaluation_decision_recorded(self, plan_root):
        """Stage 4 evaluation decision is included."""
        plan = build_stage4_rollout_plan(plan_root)
        assert plan.stage4_evaluation_decision == "ready_for_stage4_planning"

    def test_initial_scope_describes_inspect_only(self, plan_root):
        """Initial scope explicitly restricts to inspect-only."""
        plan = build_stage4_rollout_plan(plan_root)
        assert "inspect" in plan.initial_scope.lower()
        assert "read-only" in plan.initial_scope.lower() or "read only" in plan.initial_scope.lower()

    def test_generated_at_is_set(self, plan_root):
        """Plan has a generated_at timestamp."""
        plan = build_stage4_rollout_plan(plan_root)
        assert plan.generated_at
        assert "T" in plan.generated_at


# ===================================================================
# Part 2 — Allowed vs blocked operations
# ===================================================================

class TestOperationsScope:
    """Verify allowed and blocked operations are correct."""

    def test_allowed_operations_are_read_only(self):
        """All allowed operations are read-only / inspection."""
        read_only_keywords = {"check", "inspect", "review", "audit", "monitor", "scan"}
        for op in STAGE4_ALLOWED_OPERATIONS:
            # Each allowed operation should relate to inspection
            assert any(kw in op for kw in read_only_keywords), \
                f"Allowed operation '{op}' doesn't appear read-only"

    def test_blocked_operations_are_mutation(self):
        """All blocked operations involve mutation."""
        mutation_keywords = {"modif", "deploy", "manage", "change", "mutation", "operation"}
        for op in STAGE4_BLOCKED_OPERATIONS:
            assert any(kw in op for kw in mutation_keywords), \
                f"Blocked operation '{op}' doesn't appear mutation-capable"

    def test_no_overlap_between_allowed_and_blocked(self):
        """No operation appears in both allowed and blocked."""
        overlap = STAGE4_ALLOWED_OPERATIONS & STAGE4_BLOCKED_OPERATIONS
        assert overlap == frozenset(), f"Operations in both sets: {overlap}"

    def test_blocked_outnumber_allowed(self):
        """More operations blocked than allowed (narrow scope)."""
        assert len(STAGE4_BLOCKED_OPERATIONS) > len(STAGE4_ALLOWED_OPERATIONS)

    def test_dangerous_ops_are_blocked(self):
        """Specifically dangerous operations are in blocked set."""
        must_block = {
            "deployment", "service_modification", "self_modification",
            "permission_changes", "user_management",
        }
        assert must_block <= STAGE4_BLOCKED_OPERATIONS

    def test_plan_includes_sorted_operations(self, plan_root):
        """Plan operations are sorted for deterministic output."""
        plan = build_stage4_rollout_plan(plan_root)
        assert plan.allowed_operations == sorted(plan.allowed_operations)
        assert plan.blocked_operations == sorted(plan.blocked_operations)


# ===================================================================
# Part 3 — Allowed vs blocked skills
# ===================================================================

class TestSkillsScope:
    """Verify skill allowlist and blocklist."""

    def test_shell_ops_blocked(self):
        """shell-ops is blocked — mutation-capable."""
        assert "shell-ops" in STAGE4_BLOCKED_SKILLS

    def test_git_ops_blocked(self):
        """git-ops is blocked — mutation-capable."""
        assert "git-ops" in STAGE4_BLOCKED_SKILLS

    def test_task_execution_blocked(self):
        """task-execution is blocked — unconstrained."""
        assert "task-execution" in STAGE4_BLOCKED_SKILLS

    def test_self_verification_allowed(self):
        """self-verification is allowed — essential for maker-checker."""
        assert "self-verification" in STAGE4_ALLOWED_SKILLS

    def test_file_ops_allowed(self):
        """file-ops is allowed — for read-only inspection."""
        assert "file-ops" in STAGE4_ALLOWED_SKILLS

    def test_no_skill_overlap(self):
        """No skill in both allowed and blocked."""
        overlap = STAGE4_ALLOWED_SKILLS & STAGE4_BLOCKED_SKILLS
        assert overlap == frozenset(), f"Skills in both sets: {overlap}"


# ===================================================================
# Part 4 — Signal patterns
# ===================================================================

class TestSignalPatterns:
    """Verify inspect and mutate signal pattern coverage."""

    def test_inspect_signals_match_safe_tasks(self):
        """Inspect signals match read-only system task descriptions."""
        safe_tasks = [
            "check the status of the watcher service",
            "inspect the log files for errors",
            "review the system health",
            "audit dependencies for vulnerabilities",
            "monitor disk usage",
            "scan for security issues",
            "show the current configuration",
            "list all running processes",
        ]
        compiled = [re.compile(p, re.IGNORECASE) for p in STAGE4_INSPECT_SIGNALS]
        for task in safe_tasks:
            matched = any(p.search(task) for p in compiled)
            assert matched, f"Safe task not matched by inspect signals: {task}"

    def test_mutate_signals_match_dangerous_tasks(self):
        """Mutate signals match mutation-capable system task descriptions."""
        dangerous_tasks = [
            "deploy the new version to production",
            "restart the watcher service",
            "install the latest package",
            "configure the systemd unit",
            "delete the old log files",
            "sudo chmod the config file",
            "create a new cron job",
            "stop the telegram bot service",
        ]
        compiled = [re.compile(p, re.IGNORECASE) for p in STAGE4_MUTATE_SIGNALS]
        for task in dangerous_tasks:
            matched = any(p.search(task) for p in compiled)
            assert matched, f"Dangerous task not matched by mutate signals: {task}"

    def test_inspect_and_mutate_signals_are_distinct(self):
        """No pattern appears in both inspect and mutate signal lists."""
        inspect_set = set(STAGE4_INSPECT_SIGNALS)
        mutate_set = set(STAGE4_MUTATE_SIGNALS)
        overlap = inspect_set & mutate_set
        assert overlap == set(), f"Patterns in both sets: {overlap}"


# ===================================================================
# Part 5 — Activation prerequisites
# ===================================================================

class TestActivationPrerequisites:
    """Verify activation prerequisites are complete."""

    def test_requires_evaluation_gate(self):
        """Must pass Stage 4 evaluation gate."""
        assert "stage4_evaluation_ready" in STAGE4_ACTIVATION_PREREQUISITES

    def test_requires_stage3_stable(self):
        """Must have stable Stage 3."""
        assert "stage3_stable" in STAGE4_ACTIVATION_PREREQUISITES

    def test_requires_operator_approval(self):
        """Must have explicit operator approval."""
        assert "operator_approval" in STAGE4_ACTIVATION_PREREQUISITES

    def test_requires_verifier(self):
        """Must have verifier (maker-checker) enforced."""
        assert "verifier_required" in STAGE4_ACTIVATION_PREREQUISITES

    def test_requires_plan_reviewed(self):
        """Must have rollout plan reviewed."""
        assert "rollout_plan_reviewed" in STAGE4_ACTIVATION_PREREQUISITES

    def test_requires_manual_approval(self):
        """Must have manual approval hook enabled."""
        assert "manual_approval_enabled" in STAGE4_ACTIVATION_PREREQUISITES

    def test_minimum_six_prerequisites(self):
        """At least 6 prerequisites for Stage 4 activation."""
        assert len(STAGE4_ACTIVATION_PREREQUISITES) >= 6


# ===================================================================
# Part 6 — Success criteria and abort conditions
# ===================================================================

class TestSuccessCriteriaAndAbort:
    """Verify success criteria and abort conditions."""

    def test_success_criteria_has_min_runs(self):
        """Success requires minimum clean system runs."""
        assert "min_system_inspect_runs" in STAGE4_SUCCESS_CRITERIA
        assert STAGE4_SUCCESS_CRITERIA["min_system_inspect_runs"] >= 3

    def test_success_criteria_zero_tolerance_policy(self):
        """Zero tolerance for policy violations."""
        assert STAGE4_SUCCESS_CRITERIA["max_policy_violations"] == 0

    def test_success_criteria_zero_tolerance_budget(self):
        """Zero tolerance for budget exhaustions."""
        assert STAGE4_SUCCESS_CRITERIA["max_budget_exhaustions"] == 0

    def test_success_criteria_zero_tolerance_recovery(self):
        """Zero tolerance for recovery anomalies in system tasks."""
        assert STAGE4_SUCCESS_CRITERIA["max_recovery_anomalies"] == 0

    def test_success_criteria_tight_failure_rate(self):
        """Failure rate threshold is 20% or less."""
        assert STAGE4_SUCCESS_CRITERIA["max_system_failure_rate"] <= 0.20

    def test_success_criteria_healthy_heartbeat(self):
        """Heartbeat must be healthy (not just not-unhealthy)."""
        assert STAGE4_SUCCESS_CRITERIA["heartbeat_required"] == "healthy"

    def test_abort_conditions_nonempty(self):
        """At least 5 abort conditions defined."""
        assert len(STAGE4_ABORT_CONDITIONS) >= 5

    def test_abort_includes_policy_violation(self):
        """Abort on policy violation."""
        assert any("policy" in c.lower() for c in STAGE4_ABORT_CONDITIONS)

    def test_abort_includes_budget(self):
        """Abort on budget exhaustion."""
        assert any("budget" in c.lower() for c in STAGE4_ABORT_CONDITIONS)

    def test_abort_includes_heartbeat(self):
        """Abort on heartbeat failure."""
        assert any("heartbeat" in c.lower() for c in STAGE4_ABORT_CONDITIONS)

    def test_abort_includes_operator_cancel(self):
        """Abort on operator manual cancel."""
        assert any("operator" in c.lower() or "cancel" in c.lower()
                    for c in STAGE4_ABORT_CONDITIONS)


# ===================================================================
# Part 7 — Rollback procedure
# ===================================================================

class TestRollbackProcedure:
    """Verify rollback steps are complete."""

    def test_rollback_removes_system_from_classes(self, plan_root):
        """Rollback removes system from supported_classes."""
        plan = build_stage4_rollout_plan(plan_root)
        assert any("supported_classes" in s for s in plan.rollback_procedure)

    def test_rollback_resets_stage(self, plan_root):
        """Rollback resets rollout_stage to Stage 3."""
        plan = build_stage4_rollout_plan(plan_root)
        assert any("stage3" in s.lower() for s in plan.rollback_procedure)

    def test_rollback_restarts_watcher(self, plan_root):
        """Rollback restarts the watcher service."""
        plan = build_stage4_rollout_plan(plan_root)
        assert any("restart" in s.lower() and "watcher" in s.lower()
                    for s in plan.rollback_procedure)

    def test_rollback_writes_audit(self, plan_root):
        """Rollback writes to activation audit log."""
        plan = build_stage4_rollout_plan(plan_root)
        assert any("activation_log" in s or "audit" in s.lower()
                    for s in plan.rollback_procedure)

    def test_rollback_has_verification_step(self, plan_root):
        """Rollback includes a verification step."""
        plan = build_stage4_rollout_plan(plan_root)
        assert any("verify" in s.lower() or "confirm" in s.lower()
                    for s in plan.rollback_procedure)


# ===================================================================
# Part 8 — Artifact generation
# ===================================================================

class TestArtifactGeneration:
    """Test markdown rendering and file writing."""

    def test_markdown_contains_plan_status(self, plan_root):
        """Markdown includes plan status."""
        plan = build_stage4_rollout_plan(plan_root)
        md = render_stage4_plan_markdown(plan)
        assert "DRAFT" in md

    def test_markdown_contains_initial_scope(self, plan_root):
        """Markdown describes initial scope."""
        plan = build_stage4_rollout_plan(plan_root)
        md = render_stage4_plan_markdown(plan)
        assert "system_inspect" in md

    def test_markdown_contains_allowed_operations(self, plan_root):
        """Markdown lists allowed operations."""
        plan = build_stage4_rollout_plan(plan_root)
        md = render_stage4_plan_markdown(plan)
        assert "status_check" in md
        assert "log_inspection" in md

    def test_markdown_contains_blocked_operations(self, plan_root):
        """Markdown lists blocked operations."""
        plan = build_stage4_rollout_plan(plan_root)
        md = render_stage4_plan_markdown(plan)
        assert "deployment" in md
        assert "service_modification" in md

    def test_markdown_contains_protections(self, plan_root):
        """Markdown documents required protections."""
        plan = build_stage4_rollout_plan(plan_root)
        md = render_stage4_plan_markdown(plan)
        assert "maker-checker" in md.lower() or "verifier" in md.lower()
        assert "shell-ops" in md

    def test_markdown_contains_prerequisites(self, plan_root):
        """Markdown lists activation prerequisites."""
        plan = build_stage4_rollout_plan(plan_root)
        md = render_stage4_plan_markdown(plan)
        assert "operator_approval" in md

    def test_markdown_contains_rollback(self, plan_root):
        """Markdown includes rollback procedure."""
        plan = build_stage4_rollout_plan(plan_root)
        md = render_stage4_plan_markdown(plan)
        assert "Rollback" in md
        assert "Rolled back to Stage 3" in md

    def test_markdown_states_no_activation(self, plan_root):
        """Markdown explicitly says plan does NOT activate system."""
        plan = build_stage4_rollout_plan(plan_root)
        md = render_stage4_plan_markdown(plan)
        assert "Does NOT activate system" in md

    def test_markdown_mentions_why_allowed(self, plan_root):
        """Markdown explains why Stage 4 planning is now allowed."""
        plan = build_stage4_rollout_plan(plan_root)
        md = render_stage4_plan_markdown(plan)
        assert "Why Stage 4 Planning Is Allowed" in md

    def test_write_creates_files(self, plan_root):
        """write_stage4_rollout_plan creates md and json files."""
        plan = build_stage4_rollout_plan(plan_root)
        md_path, json_path = write_stage4_rollout_plan(plan, plan_root)

        assert md_path.exists()
        assert json_path.exists()
        assert md_path.name == "phase7_stage4_rollout_plan.md"
        assert json_path.name == "stage4_rollout_plan.json"

    def test_written_json_is_valid(self, plan_root):
        """Written JSON is valid and contains key fields."""
        plan = build_stage4_rollout_plan(plan_root)
        _, json_path = write_stage4_rollout_plan(plan, plan_root)

        data = json.loads(json_path.read_text())
        assert data["plan_status"] == "draft"
        assert data["system_class_status"] == "blocked"
        assert len(data["allowed_operations"]) == 8
        assert len(data["blocked_operations"]) == 12
        assert "activation_prerequisites" in data
        assert "success_criteria" in data
        assert "abort_conditions" in data
        assert "rollback_procedure" in data


# ===================================================================
# Part 9 — Plan does not modify state
# ===================================================================

class TestPlanReadOnly:
    """Verify plan building does not modify any state."""

    def test_flags_unchanged(self, plan_root):
        """Feature flags unchanged after plan generation."""
        flags_before = (plan_root / "STATE" / "config" / "feature_flags.json").read_text()
        build_stage4_rollout_plan(plan_root)
        flags_after = (plan_root / "STATE" / "config" / "feature_flags.json").read_text()
        assert flags_before == flags_after

    def test_system_not_added_to_classes(self, plan_root):
        """system not added to supported_classes."""
        build_stage4_rollout_plan(plan_root)
        flags = json.loads(
            (plan_root / "STATE" / "config" / "feature_flags.json").read_text()
        )
        assert "system" not in flags["phase7_orchestrator"]["supported_classes"]

    def test_activation_log_unchanged(self, plan_root):
        """Activation log unchanged — no activation records added."""
        log_before = (plan_root / "STATE" / "activation_log.jsonl").read_text()
        build_stage4_rollout_plan(plan_root)
        log_after = (plan_root / "STATE" / "activation_log.jsonl").read_text()
        assert log_before == log_after


# ===================================================================
# Part 10 — Scope narrowness validation
# ===================================================================

class TestScopeNarrowness:
    """Verify the plan defines the narrowest safe initial slice."""

    def test_more_blocked_than_allowed_ops(self):
        """Blocked operations outnumber allowed (conservative scope)."""
        assert len(STAGE4_BLOCKED_OPERATIONS) > len(STAGE4_ALLOWED_OPERATIONS)
        # Specifically: 12 blocked vs 8 allowed
        assert len(STAGE4_BLOCKED_OPERATIONS) >= 12
        assert len(STAGE4_ALLOWED_OPERATIONS) <= 8

    def test_more_blocked_than_allowed_skills(self):
        """Blocked skills present — not everything allowed."""
        assert len(STAGE4_BLOCKED_SKILLS) >= 3

    def test_no_shell_execution_allowed(self):
        """No path to shell execution in initial scope."""
        assert "shell-ops" not in STAGE4_ALLOWED_SKILLS
        assert "task-execution" not in STAGE4_ALLOWED_SKILLS

    def test_initial_scope_is_subset_of_system(self):
        """Allowed operations are a strict subset of all system capabilities."""
        all_ops = STAGE4_ALLOWED_OPERATIONS | STAGE4_BLOCKED_OPERATIONS
        assert STAGE4_ALLOWED_OPERATIONS < all_ops  # strict subset

    def test_success_criteria_tighter_than_stage3(self):
        """Stage 4 success criteria are tighter than Stage 3 thresholds."""
        # Stage 3 code_impl failure rate: 50%, Stage 4 system: 20%
        assert STAGE4_SUCCESS_CRITERIA["max_system_failure_rate"] < 0.50
        # Stage 3 recovery anomalies: 3, Stage 4 system: 0
        assert STAGE4_SUCCESS_CRITERIA["max_recovery_anomalies"] < 3
