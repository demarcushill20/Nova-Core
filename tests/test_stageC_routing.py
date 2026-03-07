"""Tests for Stage C low-risk coding rollout routing.

Tests cover:
  1. Eligible low-risk coding tasks routed to governed coding-agent path
  2. High-risk coding tasks rejected by deterministic denylist
  3. Research tasks still work under Stage C (Stage B superset)
  4. Ineligible task classes blocked
  5. Feature-flag-off preserves prior behavior
  6. Confidence threshold enforcement
  7. Stage C plan validation (allowed/blocked skills, mandatory verifier)
  8. Verifier enforcement (mandatory for coding, rejection blocks finalization)
  9. Fallback on uncertain classification
  10. Fail-closed defaults
  11. Stage C constant definitions
"""

import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from tools.task_classifier import (
    classify_task,
    classify_and_route,
    has_mutation_signals,
    has_high_risk_signals,
    is_stageB_eligible,
    is_stageC_eligible,
    should_use_orchestrator,
    load_feature_flags,
    STAGE_B_CLASSES,
    STAGE_C_CLASSES,
    _default_flags,
)
from tools.orchestrator_adapter import (
    build_plan_from_task,
    validate_stageB_plan,
    validate_stageC_plan,
    _build_stageB_research_steps,
    _build_stageC_coding_steps,
    _STAGE_B_ALLOWED_SKILLS,
    _STAGE_B_BLOCKED_SKILLS,
    _STAGE_C_ALLOWED_SKILLS,
    _STAGE_C_BLOCKED_SKILLS,
)


# ---------------------------------------------------------------------------
# Feature flag fixtures
# ---------------------------------------------------------------------------

def _stageC_flags(enabled=True, classes=None, min_confidence=0.5):
    """Build Stage C feature flags for testing."""
    return {
        "enabled": enabled,
        "stage": "C",
        "supported_classes": classes or ["research", "code_impl", "code_review"],
        "allowed_roles": ["research", "coding"],
        "min_confidence": min_confidence,
        "verifier_required": True,
        "fallback_to_worker": True,
        "audit_routing": True,
    }


def _disabled_flags():
    """Feature flags with orchestrator disabled."""
    return {
        "enabled": False,
        "stage": "C",
        "supported_classes": ["research", "code_impl", "code_review"],
        "min_confidence": 0.5,
        "fallback_to_worker": True,
        "audit_routing": True,
    }


def _stageB_flags():
    """Feature flags for Stage B (regression check)."""
    return {
        "enabled": True,
        "stage": "B",
        "supported_classes": ["research"],
        "allowed_roles": ["research"],
        "min_confidence": 0.5,
        "fallback_to_worker": True,
        "audit_routing": True,
    }


# ---------------------------------------------------------------------------
# 1. Eligible low-risk coding tasks routed correctly
# ---------------------------------------------------------------------------

class TestEligibleCodingRouting:
    """Low-risk coding tasks matching Stage C criteria should be routed."""

    def test_refactor_task_eligible(self):
        task = "Refactor and optimize the data validation module to reduce code duplication"
        cls, conf = classify_task(task)
        assert cls == "code_impl"
        assert conf >= 0.5
        eligible, reason = is_stageC_eligible(cls, conf, task, _stageC_flags())
        assert eligible is True
        assert reason == "stageC_coding_eligible"

    def test_code_review_task_eligible(self):
        task = "Review code changes and audit the API handler for code quality and correctness"
        cls, conf = classify_task(task)
        assert cls == "code_review"
        assert conf >= 0.5
        eligible, reason = is_stageC_eligible(cls, conf, task, _stageC_flags())
        assert eligible is True
        assert reason == "stageC_coding_eligible"

    def test_bug_fix_low_risk_eligible(self):
        task = "Fix the bug in the JSON parser and refactor the error handling path"
        cls, conf = classify_task(task)
        assert cls == "code_impl"
        assert conf >= 0.5
        eligible, reason = is_stageC_eligible(cls, conf, task, _stageC_flags())
        assert eligible is True

    def test_optimize_task_eligible(self):
        task = "Optimize and refactor the search function to reduce unnecessary iterations"
        cls, conf = classify_task(task)
        assert cls == "code_impl"
        assert conf >= 0.5
        eligible, reason = is_stageC_eligible(cls, conf, task, _stageC_flags())
        assert eligible is True

    def test_code_audit_eligible(self):
        task = "Audit the authentication module for code quality issues"
        cls, conf = classify_task(task)
        assert cls == "code_review"
        eligible, reason = is_stageC_eligible(cls, conf, task, _stageC_flags())
        assert eligible is True

    def test_routing_dict_includes_verifier_required(self):
        task = "Refactor and optimize the error handling in the parser module"
        with patch("tools.task_classifier.load_feature_flags", return_value=_stageC_flags()):
            routing = classify_and_route(task)
        assert routing["use_orchestrator"] is True
        assert routing["stage"] == "C"
        assert routing["verifier_required"] is True
        assert routing["fallback_reason"] is None

    def test_routing_dict_has_coding_roles(self):
        task = "Refactor and optimize the data validation module"
        with patch("tools.task_classifier.load_feature_flags", return_value=_stageC_flags()):
            routing = classify_and_route(task)
        assert "coding" in routing["allowed_roles"]


# ---------------------------------------------------------------------------
# 2. High-risk coding tasks rejected
# ---------------------------------------------------------------------------

class TestHighRiskCodingRejected:
    """Coding tasks with high-risk signals must be rejected."""

    def test_deploy_task_rejected(self):
        task = "Implement the feature and deploy to production"
        has_risk, signals = has_high_risk_signals(task)
        assert has_risk is True
        assert any("deploy" in s.lower() for s in signals)

    def test_secret_handling_rejected(self):
        task = "Add code to manage API secrets and credentials"
        eligible, reason = is_stageC_eligible(
            "code_impl", 0.8, task, _stageC_flags()
        )
        assert eligible is False
        assert "high_risk_signals_detected" in reason

    def test_destructive_command_rejected(self):
        task = "Implement a cleanup function that runs rm -rf on old directories"
        has_risk, signals = has_high_risk_signals(task)
        assert has_risk is True

    def test_infrastructure_task_rejected(self):
        task = "Refactor the infrastructure configuration and systemd units"
        eligible, reason = is_stageC_eligible(
            "code_impl", 0.8, task, _stageC_flags()
        )
        assert eligible is False
        assert "high_risk" in reason

    def test_migration_task_rejected(self):
        task = "Implement the database migration for the new schema"
        has_risk, signals = has_high_risk_signals(task)
        assert has_risk is True
        assert any("migrat" in s.lower() for s in signals)

    def test_shell_execution_rejected(self):
        task = "Build a module that runs shell commands to check system health"
        has_risk, signals = has_high_risk_signals(task)
        assert has_risk is True

    def test_policy_change_rejected(self):
        task = "Modify the policy engine config to allow additional tools"
        has_risk, signals = has_high_risk_signals(task)
        assert has_risk is True

    def test_pip_install_rejected(self):
        task = "Add code to pip install the new dependency automatically"
        has_risk, signals = has_high_risk_signals(task)
        assert has_risk is True

    def test_force_push_rejected(self):
        task = "Fix the branch and git push --force to remote"
        has_risk, signals = has_high_risk_signals(task)
        assert has_risk is True

    def test_sudo_rejected(self):
        task = "Implement a function that uses sudo to modify system files"
        has_risk, signals = has_high_risk_signals(task)
        assert has_risk is True

    def test_production_rejected(self):
        task = "Optimize the handler for production traffic patterns"
        eligible, reason = is_stageC_eligible(
            "code_impl", 0.8, task, _stageC_flags()
        )
        assert eligible is False
        assert "high_risk" in reason

    def test_cross_repo_rejected(self):
        task = "Refactor the shared library across cross-repo dependencies"
        has_risk, signals = has_high_risk_signals(task)
        assert has_risk is True

    def test_rewrite_everything_rejected(self):
        task = "Rewrite everything in the authentication module from scratch"
        has_risk, signals = has_high_risk_signals(task)
        assert has_risk is True

    def test_low_risk_coding_no_high_risk_signals(self):
        task = "Refactor the string parsing function to handle edge cases"
        has_risk, signals = has_high_risk_signals(task)
        assert has_risk is False
        assert signals == []


# ---------------------------------------------------------------------------
# 3. Research tasks still work under Stage C
# ---------------------------------------------------------------------------

class TestResearchUnderStageC:
    """Stage C is a superset of Stage B — research tasks should still work."""

    def test_research_task_still_eligible(self):
        task = "Research the latest developments in transformer architectures and summarize findings"
        eligible, reason = is_stageC_eligible(
            "research", 0.8, task, _stageC_flags()
        )
        assert eligible is True
        assert reason == "stageC_research_eligible"

    def test_research_with_mutation_still_rejected(self):
        task = "Research caching strategies and implement a prototype"
        eligible, reason = is_stageC_eligible(
            "research", 0.8, task, _stageC_flags()
        )
        assert eligible is False
        assert "mutation_signals_detected" in reason

    def test_research_routing_under_stageC(self):
        task = "Research best practices for API rate limiting and summarize"
        with patch("tools.task_classifier.load_feature_flags", return_value=_stageC_flags()):
            routing = classify_and_route(task)
        assert routing["use_orchestrator"] is True
        assert routing["stage"] == "C"
        # Research tasks do NOT require verifier (only coding does)
        assert routing["verifier_required"] is False

    def test_research_verifier_not_required(self):
        task = "Analyze the performance characteristics of different algorithms"
        with patch("tools.task_classifier.load_feature_flags", return_value=_stageC_flags()):
            routing = classify_and_route(task)
        assert routing["verifier_required"] is False


# ---------------------------------------------------------------------------
# 4. Ineligible task classes blocked
# ---------------------------------------------------------------------------

class TestIneligibleClassStageC:
    """Non-eligible task classes must NOT enter Stage C multi-agent path."""

    def test_system_class_rejected(self):
        task = "Deploy the new service configuration to production infrastructure"
        cls, conf = classify_task(task)
        assert cls == "system"
        eligible, reason = is_stageC_eligible(cls, conf, task, _stageC_flags())
        assert eligible is False
        assert "not_in_stageC" in reason

    def test_simple_class_rejected(self):
        task = "Check the status of running tasks"
        cls, conf = classify_task(task)
        assert cls == "simple"
        eligible, reason = is_stageC_eligible(cls, conf, task, _stageC_flags())
        assert eligible is False

    def test_unknown_class_rejected(self):
        eligible, reason = is_stageC_eligible(
            "unknown", 0.0, "xyzzy", _stageC_flags()
        )
        assert eligible is False

    def test_system_routing_stays_default(self):
        task = "Configure the systemd service for the new daemon"
        with patch("tools.task_classifier.load_feature_flags", return_value=_stageC_flags()):
            routing = classify_and_route(task)
        assert routing["use_orchestrator"] is False
        assert routing["stage"] == "C"


# ---------------------------------------------------------------------------
# 5. Feature-flag-off preserves prior behavior
# ---------------------------------------------------------------------------

class TestDisabledFlagStageC:
    """When orchestrator is disabled, all tasks use direct worker path."""

    def test_coding_rejected_when_disabled(self):
        task = "Refactor the data validation module"
        eligible, reason = is_stageC_eligible(
            "code_impl", 0.9, task, _disabled_flags()
        )
        assert eligible is False
        assert reason == "orchestrator_disabled"

    def test_research_rejected_when_disabled(self):
        task = "Research the latest developments in quantum computing"
        eligible, reason = is_stageC_eligible(
            "research", 0.9, task, _disabled_flags()
        )
        assert eligible is False
        assert reason == "orchestrator_disabled"

    def test_routing_disabled_returns_false(self):
        task = "Refactor the error handling module"
        with patch("tools.task_classifier.load_feature_flags", return_value=_disabled_flags()):
            routing = classify_and_route(task)
        assert routing["use_orchestrator"] is False


# ---------------------------------------------------------------------------
# 6. Confidence threshold enforcement
# ---------------------------------------------------------------------------

class TestConfidenceStageC:
    """Stage C requires confidence >= min_confidence (default 0.5)."""

    def test_low_confidence_coding_rejected(self):
        eligible, reason = is_stageC_eligible(
            "code_impl", 0.3, "Refactor task", _stageC_flags(min_confidence=0.5)
        )
        assert eligible is False
        assert "below" in reason

    def test_exact_threshold_accepted(self):
        eligible, reason = is_stageC_eligible(
            "code_impl", 0.5, "Refactor task", _stageC_flags(min_confidence=0.5)
        )
        assert eligible is True

    def test_above_threshold_accepted(self):
        eligible, reason = is_stageC_eligible(
            "code_impl", 0.9, "Refactor task", _stageC_flags(min_confidence=0.5)
        )
        assert eligible is True

    def test_zero_confidence_rejected(self):
        eligible, reason = is_stageC_eligible(
            "code_impl", 0.0, "Refactor task", _stageC_flags()
        )
        assert eligible is False


# ---------------------------------------------------------------------------
# 7. Stage C plan validation
# ---------------------------------------------------------------------------

class TestStageCPlanValidation:
    """Stage C plans must contain only allowed skills + mandatory verifier."""

    def test_coding_plan_valid(self):
        plan = build_plan_from_task(
            "test_task", "Refactor the module to reduce duplication",
            routing={"stage": "C"}
        )
        valid, reason = validate_stageC_plan(plan)
        assert valid is True
        assert "verifier_present" in reason

    def test_code_review_plan_valid(self):
        plan = build_plan_from_task(
            "test_task", "Review code changes for quality issues",
            routing={"stage": "C"}
        )
        valid, reason = validate_stageC_plan(plan)
        assert valid is True

    def test_research_plan_under_stageC_valid(self):
        plan = build_plan_from_task(
            "test_task", "Research AI safety approaches and summarize",
            routing={"stage": "C"}
        )
        valid, reason = validate_stageC_plan(plan)
        assert valid is True

    def test_plan_without_verifier_rejected(self):
        from planner.schemas import PlanStep, ExecutionPlan
        plan = ExecutionPlan(
            plan_id="test_plan",
            task_id="test",
            strategy="stageC_coding",
            steps=[
                PlanStep(
                    step_id="test_analyze",
                    skill_name="file-ops",
                    goal="Analyze code",
                    inputs={},
                ),
                PlanStep(
                    step_id="test_implement",
                    skill_name="file-ops",
                    goal="Implement changes",
                    inputs={},
                ),
                # Missing self-verification step
            ],
            success_criteria=["done"],
        )
        valid, reason = validate_stageC_plan(plan)
        assert valid is False
        assert "missing_mandatory_verifier_step" in reason

    def test_plan_with_shell_ops_rejected(self):
        from planner.schemas import PlanStep, ExecutionPlan
        plan = ExecutionPlan(
            plan_id="test_plan",
            task_id="test",
            strategy="stageC_coding",
            steps=[
                PlanStep(
                    step_id="test_shell",
                    skill_name="shell-ops",
                    goal="Run shell command",
                    inputs={},
                ),
            ],
            success_criteria=["done"],
        )
        valid, reason = validate_stageC_plan(plan)
        assert valid is False
        assert "blocked_skill:shell-ops" in reason

    def test_plan_with_git_ops_rejected(self):
        from planner.schemas import PlanStep, ExecutionPlan
        plan = ExecutionPlan(
            plan_id="test_plan",
            task_id="test",
            strategy="stageC_coding",
            steps=[
                PlanStep(
                    step_id="test_git",
                    skill_name="git-ops",
                    goal="Git operations",
                    inputs={},
                ),
            ],
            success_criteria=["done"],
        )
        valid, reason = validate_stageC_plan(plan)
        assert valid is False
        assert "blocked_skill:git-ops" in reason

    def test_plan_with_task_execution_rejected(self):
        from planner.schemas import PlanStep, ExecutionPlan
        plan = ExecutionPlan(
            plan_id="test_plan",
            task_id="test",
            strategy="stageC_coding",
            steps=[
                PlanStep(
                    step_id="test_exec",
                    skill_name="task-execution",
                    goal="Execute arbitrary task",
                    inputs={},
                ),
            ],
            success_criteria=["done"],
        )
        valid, reason = validate_stageC_plan(plan)
        assert valid is False
        assert "blocked_skill:task-execution" in reason

    def test_coding_plan_strategy_name(self):
        plan = build_plan_from_task(
            "test_task", "Refactor the module",
            routing={"stage": "C"}
        )
        assert plan.strategy == "stageC_coding"

    def test_research_plan_under_stageC_strategy(self):
        plan = build_plan_from_task(
            "test_task", "Research AI safety and summarize approaches",
            routing={"stage": "C"}
        )
        assert plan.strategy == "stageC_research"


# ---------------------------------------------------------------------------
# 8. Verifier enforcement
# ---------------------------------------------------------------------------

class TestVerifierEnforcement:
    """Verifier approval is mandatory for Stage C coding tasks."""

    def test_verifier_required_in_routing_for_coding(self):
        task = "Refactor and optimize the data parsing module for clarity"
        with patch("tools.task_classifier.load_feature_flags", return_value=_stageC_flags()):
            routing = classify_and_route(task)
        assert routing["verifier_required"] is True

    def test_verifier_not_required_for_research(self):
        task = "Research the latest papers on large language model alignment and summarize"
        with patch("tools.task_classifier.load_feature_flags", return_value=_stageC_flags()):
            routing = classify_and_route(task)
        assert routing["verifier_required"] is False

    def test_stageC_coding_plan_always_has_verifier_step(self):
        plan = build_plan_from_task(
            "test_code", "Refactor the validation module",
            routing={"stage": "C"}
        )
        skill_names = [s.skill_name for s in plan.steps]
        assert "self-verification" in skill_names

    def test_stageC_code_review_plan_has_verifier_step(self):
        plan = build_plan_from_task(
            "test_review", "Review code for quality issues in the API",
            routing={"stage": "C"}
        )
        skill_names = [s.skill_name for s in plan.steps]
        assert "self-verification" in skill_names

    def test_coding_steps_have_three_steps(self):
        steps = _build_stageC_coding_steps("test", "code_impl", "Refactor module")
        assert len(steps) == 3
        assert steps[-1].skill_name == "self-verification"

    def test_code_review_steps_have_three_steps(self):
        steps = _build_stageC_coding_steps("test", "code_review", "Review code")
        assert len(steps) == 3
        assert steps[-1].skill_name == "self-verification"

    def test_ineligible_coding_task_verifier_not_set(self):
        task = "Deploy the new service to production infrastructure"
        with patch("tools.task_classifier.load_feature_flags", return_value=_stageC_flags()):
            routing = classify_and_route(task)
        # System class → not eligible → verifier_required is False
        assert routing["verifier_required"] is False


# ---------------------------------------------------------------------------
# 9. Fallback on uncertain classification
# ---------------------------------------------------------------------------

class TestStageCFallback:
    """Uncertain or unsupported tasks must fall back safely."""

    def test_empty_task_text_falls_back(self):
        cls, conf = classify_task("")
        assert cls == "unknown"
        eligible, reason = is_stageC_eligible(cls, conf, "", _stageC_flags())
        assert eligible is False

    def test_nonsense_task_falls_back(self):
        cls, conf = classify_task("xyzzy plugh qwerty")
        assert cls == "unknown"
        eligible, reason = is_stageC_eligible(
            cls, conf, "xyzzy plugh qwerty", _stageC_flags()
        )
        assert eligible is False

    def test_high_risk_falls_back_safely(self):
        task = "Implement a feature that deploys changes to production automatically"
        cls, conf = classify_task(task)
        eligible, reason = is_stageC_eligible(cls, conf, task, _stageC_flags())
        assert eligible is False

    def test_ambiguous_with_low_confidence_falls_back(self):
        task = "Look at the code"
        cls, conf = classify_task(task)
        eligible, reason = is_stageC_eligible(
            cls, conf, task, _stageC_flags(min_confidence=0.8)
        )
        assert eligible is False


# ---------------------------------------------------------------------------
# 10. Fail-closed defaults
# ---------------------------------------------------------------------------

class TestFailClosedDefaults:
    """Missing or corrupt feature flags must result in disabled routing."""

    def test_default_flags_disabled(self):
        defaults = _default_flags()
        assert defaults["enabled"] is False
        assert defaults["supported_classes"] == []

    def test_missing_stage_treated_as_not_C(self):
        flags = {"enabled": True, "supported_classes": ["code_impl"]}
        eligible, reason = is_stageC_eligible(
            "code_impl", 0.9, "Refactor task", flags
        )
        assert eligible is False
        assert "not_C" in reason

    def test_empty_supported_classes_rejects_all(self):
        flags = _stageC_flags()
        flags["supported_classes"] = []
        eligible, reason = is_stageC_eligible(
            "code_impl", 0.9, "Refactor task", flags
        )
        assert eligible is False

    def test_stageB_flag_rejects_stageC_check(self):
        """Stage B flags should NOT pass Stage C eligibility."""
        eligible, reason = is_stageC_eligible(
            "code_impl", 0.9, "Refactor task", _stageB_flags()
        )
        assert eligible is False
        assert "not_C" in reason

    def test_code_impl_not_in_supported_rejected(self):
        """If code_impl is removed from supported_classes, it's rejected."""
        flags = _stageC_flags(classes=["research"])
        eligible, reason = is_stageC_eligible(
            "code_impl", 0.9, "Refactor task", flags
        )
        assert eligible is False
        assert "not_in_stageC" in reason


# ---------------------------------------------------------------------------
# 11. Stage C constant definitions
# ---------------------------------------------------------------------------

class TestStageCConstants:
    """Verify Stage C constant definitions are correct."""

    def test_stage_c_classes(self):
        assert STAGE_C_CLASSES == frozenset({"code_impl", "code_review"})

    def test_stage_b_classes_unchanged(self):
        assert STAGE_B_CLASSES == frozenset({"research"})

    def test_no_overlap_between_b_and_c(self):
        assert STAGE_B_CLASSES & STAGE_C_CLASSES == set()

    def test_allowed_skills_no_overlap_with_blocked(self):
        for skill in _STAGE_C_ALLOWED_SKILLS:
            assert skill not in _STAGE_C_BLOCKED_SKILLS

    def test_blocked_skills_include_shell(self):
        assert "shell-ops" in _STAGE_C_BLOCKED_SKILLS

    def test_blocked_skills_include_git(self):
        assert "git-ops" in _STAGE_C_BLOCKED_SKILLS

    def test_blocked_skills_include_task_execution(self):
        assert "task-execution" in _STAGE_C_BLOCKED_SKILLS

    def test_allowed_skills_include_file_ops(self):
        assert "file-ops" in _STAGE_C_ALLOWED_SKILLS

    def test_allowed_skills_include_self_verification(self):
        assert "self-verification" in _STAGE_C_ALLOWED_SKILLS


# ---------------------------------------------------------------------------
# 12. Stage B backward compatibility under Stage C config
# ---------------------------------------------------------------------------

class TestStageBBackwardCompat:
    """Stage B eligibility still works when stage is set to B."""

    def test_stageB_research_still_works_with_B_flag(self):
        task = "Research the latest papers on alignment and summarize"
        eligible, reason = is_stageB_eligible(
            "research", 0.8, task, _stageB_flags()
        )
        assert eligible is True

    def test_stageB_rejects_coding_with_B_flag(self):
        task = "Refactor the parser module"
        eligible, reason = is_stageB_eligible(
            "code_impl", 0.8, task, _stageB_flags()
        )
        assert eligible is False


# ---------------------------------------------------------------------------
# Script runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    test_classes = [
        TestEligibleCodingRouting,
        TestHighRiskCodingRejected,
        TestResearchUnderStageC,
        TestIneligibleClassStageC,
        TestDisabledFlagStageC,
        TestConfidenceStageC,
        TestStageCPlanValidation,
        TestVerifierEnforcement,
        TestStageCFallback,
        TestFailClosedDefaults,
        TestStageCConstants,
        TestStageBBackwardCompat,
    ]

    total = 0
    passed = 0
    failed = 0

    for cls in test_classes:
        instance = cls()
        methods = [m for m in dir(cls) if m.startswith("test_")]
        for method_name in methods:
            total += 1
            try:
                method = getattr(instance, method_name)
                import inspect
                sig = inspect.signature(method)
                if "tmp_path" in sig.parameters:
                    import tempfile
                    with tempfile.TemporaryDirectory() as td:
                        method(Path(td))
                else:
                    method()
                passed += 1
                print(f"  PASS: {cls.__name__}.{method_name}")
            except Exception as e:
                failed += 1
                print(f"  FAIL: {cls.__name__}.{method_name}: {e}")

    print(f"\n{'='*60}")
    print(f"Total: {total}  Passed: {passed}  Failed: {failed}")
    print(f"{'='*60}")
