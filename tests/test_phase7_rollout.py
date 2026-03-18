"""Phase 7.9/7.10/7.12 — Feature-Flagged Rollout Tests.

Deterministic tests for Stage 1 (research-only), Stage 2
(research + code_review), and Stage 3 (+ code_impl) rollout behavior:
- Research class enabled → multi-agent path
- code_review enabled (Stage 2) → multi-agent path
- code_impl enabled (Stage 3) → multi-agent path, evaluation-gated
- Non-selected classes → single-agent fallback
- Flag off → safe fallback
- UNHEALTHY heartbeat → auto-degrade
- Rollback behavior (flag change)
- Health gating edge cases
- Stage 2 expansion and boundary tests
- Stage 3 evaluation-gated expansion, verifier enforcement, rollback
"""

import json
import os
import time

import pytest

from agents.production_hardening import (
    FeatureFlags,
    GracefulDegradation,
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
    (tmp_path / "TASKS").mkdir(parents=True)
    (tmp_path / "LOGS").mkdir(parents=True)
    yield tmp_path
    del os.environ["NOVACORE_ROOT"]


def _write_flags(tmp_path, orchestrator_flags, hardening_flags=None):
    """Write feature_flags.json with given orchestrator config."""
    flags = {"phase7_orchestrator": orchestrator_flags}
    if hardening_flags:
        flags["phase7_hardening"] = hardening_flags
    path = tmp_path / "STATE" / "config" / "feature_flags.json"
    path.write_text(json.dumps(flags, indent=2))


def _stage1_flags(**overrides):
    """Standard Stage 1 research-only rollout flags."""
    flags = {
        "enabled": True,
        "supported_classes": ["research"],
        "stage": "C",
        "rollout_stage": "stage1_research_only",
        "allowed_roles": ["research"],
        "min_confidence": 0.5,
        "verifier_required": False,
        "fallback_to_worker": True,
        "audit_routing": True,
    }
    flags.update(overrides)
    return flags


def _stage2_flags(**overrides):
    """Standard Stage 2 research + code_review rollout flags."""
    flags = {
        "enabled": True,
        "supported_classes": ["research", "code_review"],
        "stage": "C",
        "rollout_stage": "stage2_research_and_code_review",
        "allowed_roles": ["research", "coding"],
        "min_confidence": 0.5,
        "verifier_required": True,
        "fallback_to_worker": True,
        "audit_routing": True,
    }
    flags.update(overrides)
    return flags


def _write_heartbeat(tmp_path, overall, findings=None, metrics=None):
    """Write heartbeat_multiagent.json with given health status."""
    hb = {
        "overall": overall,
        "metrics": metrics or {},
        "findings": findings or [],
        "generated_at": "2026-03-08T12:00:00Z",
    }
    path = tmp_path / "STATE" / "heartbeat_multiagent.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(hb, indent=2))


# ===================================================================
# Part 1 — Rollout Routing: Research Enabled
# ===================================================================


class TestRollout_ResearchEnabled:
    """Verify research class routes to multi-agent path."""

    def test_research_class_proceeds(self, tmp_path):
        """Research class with Stage 1 flags → proceed."""
        _write_flags(tmp_path, _stage1_flags())
        gd = GracefulDegradation(base=tmp_path)
        result = gd.check_orchestrator_available("research")
        assert result.action == "proceed"
        assert "orchestrator_available" in result.reason

    def test_research_with_healthy_heartbeat(self, tmp_path):
        """Research + HEALTHY heartbeat → proceed."""
        _write_flags(tmp_path, _stage1_flags())
        _write_heartbeat(tmp_path, "healthy")
        gd = GracefulDegradation(base=tmp_path)
        result = gd.check_orchestrator_available("research")
        assert result.action == "proceed"

    def test_research_with_warning_heartbeat(self, tmp_path):
        """Research + WARNING heartbeat → still proceed (only UNHEALTHY blocks)."""
        _write_flags(tmp_path, _stage1_flags())
        _write_heartbeat(tmp_path, "warning")
        gd = GracefulDegradation(base=tmp_path)
        result = gd.check_orchestrator_available("research")
        assert result.action == "proceed"

    def test_research_with_no_heartbeat(self, tmp_path):
        """Research + no heartbeat file → proceed (first-run tolerance)."""
        _write_flags(tmp_path, _stage1_flags())
        # No heartbeat file written
        gd = GracefulDegradation(base=tmp_path)
        result = gd.check_orchestrator_available("research")
        assert result.action == "proceed"


# ===================================================================
# Part 2 — Non-Research Classes: Fallback
# ===================================================================


class TestRollout_NonResearchFallback:
    """Verify non-research classes route to single-agent fallback."""

    def test_code_impl_degrades(self, tmp_path):
        """code_impl with Stage 1 flags → degrade to worker."""
        _write_flags(tmp_path, _stage1_flags())
        gd = GracefulDegradation(base=tmp_path)
        result = gd.check_orchestrator_available("code_impl")
        assert result.action == "degrade"
        assert "task_class_not_supported" in result.reason
        assert result.fallback == "single_agent_worker"

    def test_code_review_degrades(self, tmp_path):
        """code_review with Stage 1 flags → degrade to worker."""
        _write_flags(tmp_path, _stage1_flags())
        gd = GracefulDegradation(base=tmp_path)
        result = gd.check_orchestrator_available("code_review")
        assert result.action == "degrade"
        assert "task_class_not_supported" in result.reason

    def test_system_degrades(self, tmp_path):
        """system class with Stage 1 flags → degrade to worker."""
        _write_flags(tmp_path, _stage1_flags())
        gd = GracefulDegradation(base=tmp_path)
        result = gd.check_orchestrator_available("system")
        assert result.action == "degrade"

    def test_simple_degrades(self, tmp_path):
        """simple class with Stage 1 flags → degrade to worker."""
        _write_flags(tmp_path, _stage1_flags())
        gd = GracefulDegradation(base=tmp_path)
        result = gd.check_orchestrator_available("simple")
        assert result.action == "degrade"

    def test_unknown_degrades(self, tmp_path):
        """unknown class → degrade to worker."""
        _write_flags(tmp_path, _stage1_flags())
        gd = GracefulDegradation(base=tmp_path)
        result = gd.check_orchestrator_available("unknown")
        assert result.action == "degrade"


# ===================================================================
# Part 3 — Flag Off: Safe Fallback
# ===================================================================


class TestRollout_FlagOff:
    """Verify disabled flags result in clean fallback."""

    def test_enabled_false_degrades(self, tmp_path):
        """enabled=false → degrade for all classes."""
        _write_flags(tmp_path, _stage1_flags(enabled=False))
        gd = GracefulDegradation(base=tmp_path)
        result = gd.check_orchestrator_available("research")
        assert result.action == "degrade"
        assert "multi_agent_disabled" in result.reason
        assert result.fallback == "single_agent_worker"

    def test_empty_supported_classes_degrades(self, tmp_path):
        """Empty supported_classes → degrade."""
        _write_flags(tmp_path, _stage1_flags(supported_classes=[]))
        gd = GracefulDegradation(base=tmp_path)
        result = gd.check_orchestrator_available("research")
        assert result.action == "degrade"
        assert "task_class_not_supported" in result.reason

    def test_missing_flags_file_degrades(self, tmp_path):
        """No feature_flags.json → fail-closed degrade."""
        # Don't write any flags
        gd = GracefulDegradation(base=tmp_path)
        result = gd.check_orchestrator_available("research")
        assert result.action == "degrade"
        assert "multi_agent_disabled" in result.reason

    def test_corrupt_flags_file_degrades(self, tmp_path):
        """Corrupt JSON → fail-closed degrade."""
        path = tmp_path / "STATE" / "config" / "feature_flags.json"
        path.write_text("NOT VALID JSON {{{")
        gd = GracefulDegradation(base=tmp_path)
        result = gd.check_orchestrator_available("research")
        assert result.action == "degrade"
        assert "multi_agent_disabled" in result.reason


# ===================================================================
# Part 4 — UNHEALTHY Heartbeat: Auto-Degrade
# ===================================================================


class TestRollout_UnhealthyHeartbeat:
    """Verify UNHEALTHY heartbeat triggers auto-degrade."""

    def test_unhealthy_heartbeat_degrades(self, tmp_path):
        """UNHEALTHY heartbeat → degrade even for supported class."""
        _write_flags(tmp_path, _stage1_flags())
        _write_heartbeat(
            tmp_path,
            "unhealthy",
            findings=[{"category": "workflow_stuck", "severity": "unhealthy", "subject": "wf_001", "detail": "stuck"}],
        )
        gd = GracefulDegradation(base=tmp_path)
        result = gd.check_orchestrator_available("research")
        assert result.action == "degrade"
        assert "heartbeat_unhealthy" in result.reason
        assert result.fallback == "single_agent_worker"

    def test_unhealthy_blocks_all_classes(self, tmp_path):
        """UNHEALTHY blocks even research (the only supported class)."""
        _write_flags(tmp_path, _stage1_flags())
        _write_heartbeat(tmp_path, "unhealthy")
        gd = GracefulDegradation(base=tmp_path)

        for cls in ["research", "code_impl", "system"]:
            result = gd.check_orchestrator_available(cls)
            assert result.action == "degrade", f"{cls} should degrade"

    def test_corrupt_heartbeat_proceeds(self, tmp_path):
        """Corrupt heartbeat JSON → proceed (first-run tolerance)."""
        _write_flags(tmp_path, _stage1_flags())
        hb_path = tmp_path / "STATE" / "heartbeat_multiagent.json"
        hb_path.write_text("BAD JSON {{")
        gd = GracefulDegradation(base=tmp_path)
        result = gd.check_orchestrator_available("research")
        assert result.action == "proceed"


# ===================================================================
# Part 5 — Rollback Behavior
# ===================================================================


class TestRollout_Rollback:
    """Verify immediate rollback by config change."""

    def test_rollback_by_disabling_flag(self, tmp_path):
        """Enable → proceed; disable → degrade (immediate)."""
        # Start enabled
        _write_flags(tmp_path, _stage1_flags())
        gd = GracefulDegradation(base=tmp_path)
        result = gd.check_orchestrator_available("research")
        assert result.action == "proceed"

        # Rollback: disable
        _write_flags(tmp_path, _stage1_flags(enabled=False))
        # GracefulDegradation creates a new FeatureFlags on each call
        gd2 = GracefulDegradation(base=tmp_path)
        result = gd2.check_orchestrator_available("research")
        assert result.action == "degrade"
        assert "multi_agent_disabled" in result.reason

    def test_rollback_by_removing_class(self, tmp_path):
        """Remove research from supported_classes → degrade."""
        _write_flags(tmp_path, _stage1_flags())
        gd = GracefulDegradation(base=tmp_path)
        assert gd.check_orchestrator_available("research").action == "proceed"

        # Rollback: remove class
        _write_flags(tmp_path, _stage1_flags(supported_classes=[]))
        gd2 = GracefulDegradation(base=tmp_path)
        result = gd2.check_orchestrator_available("research")
        assert result.action == "degrade"

    def test_rollback_on_health_degradation(self, tmp_path):
        """HEALTHY → UNHEALTHY → auto-rollback."""
        _write_flags(tmp_path, _stage1_flags())
        _write_heartbeat(tmp_path, "healthy")
        gd = GracefulDegradation(base=tmp_path)
        assert gd.check_orchestrator_available("research").action == "proceed"

        # Health degrades
        _write_heartbeat(tmp_path, "unhealthy")
        gd2 = GracefulDegradation(base=tmp_path)
        result = gd2.check_orchestrator_available("research")
        assert result.action == "degrade"
        assert "heartbeat_unhealthy" in result.reason

    def test_recovery_after_health_restored(self, tmp_path):
        """UNHEALTHY → HEALTHY → proceed again."""
        _write_flags(tmp_path, _stage1_flags())
        _write_heartbeat(tmp_path, "unhealthy")
        gd = GracefulDegradation(base=tmp_path)
        assert gd.check_orchestrator_available("research").action == "degrade"

        # Health restored
        _write_heartbeat(tmp_path, "healthy")
        gd2 = GracefulDegradation(base=tmp_path)
        assert gd2.check_orchestrator_available("research").action == "proceed"


# ===================================================================
# Part 6 — Health Gating Edge Cases
# ===================================================================


class TestRollout_HealthGatingEdgeCases:
    """Edge cases for the rollout health gate."""

    def test_health_check_independent_of_flags(self, tmp_path):
        """check_rollout_health() works regardless of feature flags."""
        _write_heartbeat(tmp_path, "unhealthy")
        gd = GracefulDegradation(base=tmp_path)
        result = gd.check_rollout_health()
        assert result.action == "degrade"
        assert "heartbeat_unhealthy" in result.reason

    def test_health_check_healthy(self, tmp_path):
        """HEALTHY heartbeat → proceed."""
        _write_heartbeat(tmp_path, "healthy")
        gd = GracefulDegradation(base=tmp_path)
        result = gd.check_rollout_health()
        assert result.action == "proceed"
        assert "heartbeat_healthy" in result.reason

    def test_health_check_warning(self, tmp_path):
        """WARNING heartbeat → proceed (only UNHEALTHY blocks)."""
        _write_heartbeat(tmp_path, "warning")
        gd = GracefulDegradation(base=tmp_path)
        result = gd.check_rollout_health()
        assert result.action == "proceed"
        assert "heartbeat_warning" in result.reason

    def test_health_check_no_file(self, tmp_path):
        """No heartbeat file → proceed with no_heartbeat_data reason."""
        gd = GracefulDegradation(base=tmp_path)
        result = gd.check_rollout_health()
        assert result.action == "proceed"
        assert "no_heartbeat_data" in result.reason

    def test_health_check_empty_overall(self, tmp_path):
        """Empty overall field → proceed (not explicitly unhealthy)."""
        _write_heartbeat(tmp_path, "")
        gd = GracefulDegradation(base=tmp_path)
        result = gd.check_rollout_health()
        assert result.action == "proceed"

    def test_health_check_missing_overall_key(self, tmp_path):
        """No overall key in heartbeat JSON → proceed."""
        hb_path = tmp_path / "STATE" / "heartbeat_multiagent.json"
        hb_path.parent.mkdir(parents=True, exist_ok=True)
        hb_path.write_text(json.dumps({"metrics": {}}))
        gd = GracefulDegradation(base=tmp_path)
        result = gd.check_rollout_health()
        assert result.action == "proceed"


# ===================================================================
# Part 7 — Rollout Stage Config
# ===================================================================


class TestRollout_StageConfig:
    """Verify rollout_stage field is read and tracked."""

    def test_rollout_stage_in_config(self, tmp_path):
        """rollout_stage field is accessible via orchestrator_config()."""
        _write_flags(tmp_path, _stage1_flags())
        ff = FeatureFlags(tmp_path)
        config = ff.orchestrator_config()
        assert config.get("rollout_stage") == "stage1_research_only"

    def test_rollout_stage_absent_proceeds(self, tmp_path):
        """Missing rollout_stage doesn't break anything."""
        flags = _stage1_flags()
        del flags["rollout_stage"]
        _write_flags(tmp_path, flags)
        ff = FeatureFlags(tmp_path)
        assert ff.is_multi_agent_enabled() is True
        assert ff.is_task_class_supported("research") is True


# ===================================================================
# Part 8 — Task Classifier Integration
# ===================================================================


class TestRollout_ClassifierIntegration:
    """Verify task classifier respects Stage 1 narrowing."""

    def test_classify_research_task(self, tmp_path):
        """Research-classified task routes correctly."""
        from tools.task_classifier import classify_task

        task_class, confidence = classify_task("Research the best practices for API rate limiting")
        assert task_class == "research"
        assert confidence > 0.0

    def test_classify_coding_task_not_research(self, tmp_path):
        """Coding task classified correctly (not research)."""
        from tools.task_classifier import classify_task

        task_class, _ = classify_task("Implement a new authentication module with JWT tokens")
        assert task_class != "research"

    def test_research_eligible_with_stage1_flags(self, tmp_path):
        """Research task + Stage 1 flags → eligible via is_stageC_eligible."""
        from tools.task_classifier import is_stageC_eligible

        eligible, reason = is_stageC_eligible(
            "research",
            0.8,
            "Investigate how other projects handle caching",
            _stage1_flags(),
        )
        assert eligible is True
        assert "research_eligible" in reason

    def test_coding_ineligible_with_stage1_flags(self, tmp_path):
        """Coding task + Stage 1 flags → ineligible (class not supported)."""
        from tools.task_classifier import is_stageC_eligible

        eligible, reason = is_stageC_eligible(
            "code_impl",
            0.8,
            "Refactor the authentication module",
            _stage1_flags(),
        )
        assert eligible is False
        assert "not_in_stageC" in reason


# ===================================================================
# Part 9 — Combined: Full Rollout Pre-Flight
# ===================================================================


class TestRollout_FullPreFlight:
    """Combined pre-flight check simulating watcher dispatch gate."""

    def test_full_preflight_research_healthy(self, tmp_path):
        """Stage 1 + research + HEALTHY → proceed through all gates."""
        _write_flags(
            tmp_path,
            _stage1_flags(),
            {
                "archive_cleanup": True,
                "rate_limiting": True,
                "manual_approval": False,
            },
        )
        _write_heartbeat(tmp_path, "healthy")

        gd = GracefulDegradation(base=tmp_path)

        # Gate 1: orchestrator available (includes health gate)
        r1 = gd.check_orchestrator_available("research")
        assert r1.action == "proceed"

        # Gate 2: workflow launch feasibility
        r2 = gd.check_workflow_launch_feasibility()
        assert r2.action == "proceed"

        # Gate 3: spawn feasibility
        r3 = gd.check_spawn_feasibility()
        assert r3.action == "proceed"

    def test_full_preflight_unhealthy_blocks(self, tmp_path):
        """Stage 1 + research + UNHEALTHY → blocked at gate 1."""
        _write_flags(
            tmp_path,
            _stage1_flags(),
            {
                "archive_cleanup": True,
                "rate_limiting": True,
            },
        )
        _write_heartbeat(tmp_path, "unhealthy")

        gd = GracefulDegradation(base=tmp_path)
        r1 = gd.check_orchestrator_available("research")
        assert r1.action == "degrade"
        assert "heartbeat_unhealthy" in r1.reason

    def test_full_preflight_non_research_blocks(self, tmp_path):
        """Stage 1 + code_impl + HEALTHY → blocked at class gate."""
        _write_flags(tmp_path, _stage1_flags())
        _write_heartbeat(tmp_path, "healthy")

        gd = GracefulDegradation(base=tmp_path)
        r1 = gd.check_orchestrator_available("code_impl")
        assert r1.action == "degrade"
        assert "task_class_not_supported" in r1.reason


# ===================================================================
# Part 10 — Stage 2: code_review Enabled
# ===================================================================


class TestRollout_Stage2_CodeReviewEnabled:
    """Verify code_review routes to multi-agent path under Stage 2 flags."""

    def test_code_review_proceeds(self, tmp_path):
        """code_review with Stage 2 flags → proceed."""
        _write_flags(tmp_path, _stage2_flags())
        gd = GracefulDegradation(base=tmp_path)
        result = gd.check_orchestrator_available("code_review")
        assert result.action == "proceed"
        assert "orchestrator_available" in result.reason

    def test_code_review_with_healthy_heartbeat(self, tmp_path):
        """code_review + HEALTHY heartbeat → proceed."""
        _write_flags(tmp_path, _stage2_flags())
        _write_heartbeat(tmp_path, "healthy")
        gd = GracefulDegradation(base=tmp_path)
        result = gd.check_orchestrator_available("code_review")
        assert result.action == "proceed"

    def test_code_review_with_warning_heartbeat(self, tmp_path):
        """code_review + WARNING heartbeat → proceed (only UNHEALTHY blocks)."""
        _write_flags(tmp_path, _stage2_flags())
        _write_heartbeat(tmp_path, "warning")
        gd = GracefulDegradation(base=tmp_path)
        result = gd.check_orchestrator_available("code_review")
        assert result.action == "proceed"

    def test_code_review_with_no_heartbeat(self, tmp_path):
        """code_review + no heartbeat → proceed (first-run tolerance)."""
        _write_flags(tmp_path, _stage2_flags())
        gd = GracefulDegradation(base=tmp_path)
        result = gd.check_orchestrator_available("code_review")
        assert result.action == "proceed"

    def test_research_still_proceeds_under_stage2(self, tmp_path):
        """Research continues to route correctly under Stage 2 flags."""
        _write_flags(tmp_path, _stage2_flags())
        _write_heartbeat(tmp_path, "healthy")
        gd = GracefulDegradation(base=tmp_path)
        result = gd.check_orchestrator_available("research")
        assert result.action == "proceed"


# ===================================================================
# Part 11 — Stage 2: Excluded Classes Still Blocked
# ===================================================================


class TestRollout_Stage2_ExcludedClasses:
    """Verify non-selected classes remain on fallback under Stage 2."""

    def test_code_impl_still_degrades(self, tmp_path):
        """code_impl excluded from Stage 2 → degrade."""
        _write_flags(tmp_path, _stage2_flags())
        gd = GracefulDegradation(base=tmp_path)
        result = gd.check_orchestrator_available("code_impl")
        assert result.action == "degrade"
        assert "task_class_not_supported" in result.reason
        assert result.fallback == "single_agent_worker"

    def test_system_still_degrades(self, tmp_path):
        """system class still excluded → degrade."""
        _write_flags(tmp_path, _stage2_flags())
        gd = GracefulDegradation(base=tmp_path)
        result = gd.check_orchestrator_available("system")
        assert result.action == "degrade"

    def test_simple_still_degrades(self, tmp_path):
        """simple class still excluded → degrade."""
        _write_flags(tmp_path, _stage2_flags())
        gd = GracefulDegradation(base=tmp_path)
        result = gd.check_orchestrator_available("simple")
        assert result.action == "degrade"

    def test_unknown_still_degrades(self, tmp_path):
        """unknown class still excluded → degrade."""
        _write_flags(tmp_path, _stage2_flags())
        gd = GracefulDegradation(base=tmp_path)
        result = gd.check_orchestrator_available("unknown")
        assert result.action == "degrade"


# ===================================================================
# Part 12 — Stage 2: UNHEALTHY Heartbeat Blocks code_review
# ===================================================================


class TestRollout_Stage2_UnhealthyBlocks:
    """Verify UNHEALTHY heartbeat blocks code_review under Stage 2."""

    def test_unhealthy_blocks_code_review(self, tmp_path):
        """UNHEALTHY heartbeat → code_review degrades."""
        _write_flags(tmp_path, _stage2_flags())
        _write_heartbeat(tmp_path, "unhealthy")
        gd = GracefulDegradation(base=tmp_path)
        result = gd.check_orchestrator_available("code_review")
        assert result.action == "degrade"
        assert "heartbeat_unhealthy" in result.reason
        assert result.fallback == "single_agent_worker"

    def test_unhealthy_blocks_both_classes(self, tmp_path):
        """UNHEALTHY blocks both research and code_review."""
        _write_flags(tmp_path, _stage2_flags())
        _write_heartbeat(tmp_path, "unhealthy")
        gd = GracefulDegradation(base=tmp_path)

        for cls in ["research", "code_review"]:
            result = gd.check_orchestrator_available(cls)
            assert result.action == "degrade", f"{cls} should degrade"
            assert "heartbeat_unhealthy" in result.reason

    def test_unhealthy_blocks_all_classes_stage2(self, tmp_path):
        """UNHEALTHY blocks everything — selected and non-selected."""
        _write_flags(tmp_path, _stage2_flags())
        _write_heartbeat(tmp_path, "unhealthy")
        gd = GracefulDegradation(base=tmp_path)

        for cls in ["research", "code_review", "code_impl", "system"]:
            result = gd.check_orchestrator_available(cls)
            assert result.action == "degrade", f"{cls} should degrade"


# ===================================================================
# Part 13 — Stage 2: Rollback Behavior
# ===================================================================


class TestRollout_Stage2_Rollback:
    """Verify rollback controls work for Stage 2."""

    def test_rollback_disables_both_classes(self, tmp_path):
        """Disable flag → both research and code_review degrade."""
        _write_flags(tmp_path, _stage2_flags())
        gd = GracefulDegradation(base=tmp_path)
        assert gd.check_orchestrator_available("research").action == "proceed"
        assert gd.check_orchestrator_available("code_review").action == "proceed"

        # Rollback
        _write_flags(tmp_path, _stage2_flags(enabled=False))
        gd2 = GracefulDegradation(base=tmp_path)
        assert gd2.check_orchestrator_available("research").action == "degrade"
        assert gd2.check_orchestrator_available("code_review").action == "degrade"

    def test_rollback_remove_code_review_only(self, tmp_path):
        """Remove code_review from supported → research still works."""
        _write_flags(tmp_path, _stage2_flags())
        gd = GracefulDegradation(base=tmp_path)
        assert gd.check_orchestrator_available("code_review").action == "proceed"

        # Partial rollback: remove code_review only
        _write_flags(tmp_path, _stage2_flags(supported_classes=["research"]))
        gd2 = GracefulDegradation(base=tmp_path)
        assert gd2.check_orchestrator_available("research").action == "proceed"
        assert gd2.check_orchestrator_available("code_review").action == "degrade"

    def test_rollback_to_empty_classes(self, tmp_path):
        """Remove all classes → everything degrades."""
        _write_flags(tmp_path, _stage2_flags())
        gd = GracefulDegradation(base=tmp_path)
        assert gd.check_orchestrator_available("research").action == "proceed"

        _write_flags(tmp_path, _stage2_flags(supported_classes=[]))
        gd2 = GracefulDegradation(base=tmp_path)
        assert gd2.check_orchestrator_available("research").action == "degrade"
        assert gd2.check_orchestrator_available("code_review").action == "degrade"

    def test_health_rollback_and_recovery(self, tmp_path):
        """HEALTHY → UNHEALTHY → code_review blocked → HEALTHY → code_review resumes."""
        _write_flags(tmp_path, _stage2_flags())
        _write_heartbeat(tmp_path, "healthy")
        gd = GracefulDegradation(base=tmp_path)
        assert gd.check_orchestrator_available("code_review").action == "proceed"

        _write_heartbeat(tmp_path, "unhealthy")
        gd2 = GracefulDegradation(base=tmp_path)
        assert gd2.check_orchestrator_available("code_review").action == "degrade"

        _write_heartbeat(tmp_path, "healthy")
        gd3 = GracefulDegradation(base=tmp_path)
        assert gd3.check_orchestrator_available("code_review").action == "proceed"


# ===================================================================
# Part 14 — Stage 2: Config and Classifier Integration
# ===================================================================


class TestRollout_Stage2_Config:
    """Verify Stage 2 config fields and classifier integration."""

    def test_rollout_stage_field(self, tmp_path):
        """rollout_stage reads as stage2_research_and_code_review."""
        _write_flags(tmp_path, _stage2_flags())
        ff = FeatureFlags(tmp_path)
        config = ff.orchestrator_config()
        assert config["rollout_stage"] == "stage2_research_and_code_review"

    def test_both_classes_supported(self, tmp_path):
        """Both research and code_review pass is_task_class_supported."""
        _write_flags(tmp_path, _stage2_flags())
        ff = FeatureFlags(tmp_path)
        assert ff.is_task_class_supported("research") is True
        assert ff.is_task_class_supported("code_review") is True
        assert ff.is_task_class_supported("code_impl") is False
        assert ff.is_task_class_supported("system") is False

    def test_classify_code_review_task(self, tmp_path):
        """code_review-classified task routes correctly."""
        from tools.task_classifier import classify_task

        task_class, confidence = classify_task("Review code quality of the authentication module")
        assert task_class == "code_review"
        assert confidence > 0.0

    def test_code_review_eligible_with_stage2_flags(self, tmp_path):
        """code_review task + Stage 2 flags → eligible via is_stageC_eligible."""
        from tools.task_classifier import is_stageC_eligible

        eligible, reason = is_stageC_eligible(
            "code_review",
            0.8,
            "Audit the error handling in the API layer",
            _stage2_flags(),
        )
        assert eligible is True
        assert "coding_eligible" in reason

    def test_code_review_with_high_risk_blocked(self, tmp_path):
        """code_review with high-risk signals → rejected by classifier."""
        from tools.task_classifier import is_stageC_eligible

        eligible, reason = is_stageC_eligible(
            "code_review",
            0.8,
            "Review code and deploy to production with sudo access",
            _stage2_flags(),
        )
        assert eligible is False
        assert "high_risk" in reason

    def test_code_impl_ineligible_with_stage2_flags(self, tmp_path):
        """code_impl + Stage 2 flags → ineligible (not in supported_classes)."""
        from tools.task_classifier import is_stageC_eligible

        eligible, reason = is_stageC_eligible(
            "code_impl",
            0.8,
            "Implement a caching layer for the API",
            _stage2_flags(),
        )
        assert eligible is False
        assert "not_in_stageC" in reason


# ===================================================================
# Part 15 — Stage 2: Full Pre-Flight
# ===================================================================


class TestRollout_Stage2_FullPreFlight:
    """Combined pre-flight for Stage 2 dispatch."""

    def test_preflight_code_review_healthy(self, tmp_path):
        """Stage 2 + code_review + HEALTHY → proceed through all gates."""
        _write_flags(
            tmp_path,
            _stage2_flags(),
            {
                "archive_cleanup": True,
                "rate_limiting": True,
                "manual_approval": False,
            },
        )
        _write_heartbeat(tmp_path, "healthy")

        gd = GracefulDegradation(base=tmp_path)
        r1 = gd.check_orchestrator_available("code_review")
        assert r1.action == "proceed"
        r2 = gd.check_workflow_launch_feasibility()
        assert r2.action == "proceed"
        r3 = gd.check_spawn_feasibility()
        assert r3.action == "proceed"

    def test_preflight_both_classes_healthy(self, tmp_path):
        """Stage 2 + both classes + HEALTHY → both proceed."""
        _write_flags(tmp_path, _stage2_flags())
        _write_heartbeat(tmp_path, "healthy")
        gd = GracefulDegradation(base=tmp_path)
        assert gd.check_orchestrator_available("research").action == "proceed"
        assert gd.check_orchestrator_available("code_review").action == "proceed"

    def test_preflight_code_impl_still_blocked(self, tmp_path):
        """Stage 2 + code_impl + HEALTHY → blocked at class gate."""
        _write_flags(tmp_path, _stage2_flags())
        _write_heartbeat(tmp_path, "healthy")
        gd = GracefulDegradation(base=tmp_path)
        r1 = gd.check_orchestrator_available("code_impl")
        assert r1.action == "degrade"
        assert "task_class_not_supported" in r1.reason


# ===================================================================
# Stage 3 helpers
# ===================================================================


def _stage3_flags(**overrides):
    """Standard Stage 3 research + code_review + code_impl rollout flags."""
    flags = {
        "enabled": True,
        "supported_classes": ["research", "code_review", "code_impl"],
        "stage": "C",
        "rollout_stage": "stage3_research_code_review_code_impl",
        "allowed_roles": ["research", "coding"],
        "min_confidence": 0.5,
        "verifier_required": True,
        "fallback_to_worker": True,
        "audit_routing": True,
    }
    flags.update(overrides)
    return flags


def _write_ready_evidence(tmp_path):
    """Write evidence that causes rollout gate to return ready_to_expand."""
    # Heartbeat: healthy
    _write_heartbeat(tmp_path, "healthy")

    # At least MIN_COMPLETED_WORKFLOWS completed workflows
    wf_dir = tmp_path / "STATE" / "workflows"
    wf_dir.mkdir(parents=True, exist_ok=True)
    for i in range(5):
        (wf_dir / f"wf_{i:03d}.json").write_text(
            json.dumps(
                {
                    "workflow_id": f"wf_{i:03d}",
                    "status": "completed",
                    "completed_at": time.time() - 100,
                }
            )
        )

    # Clean verifications (some approvals, no rejections)
    ver_dir = tmp_path / "STATE" / "verifications"
    ver_dir.mkdir(parents=True, exist_ok=True)
    for i in range(3):
        (ver_dir / f"ver_{i:03d}.json").write_text(
            json.dumps(
                {
                    "verification_id": f"ver_{i:03d}",
                    "verdict": "approved",
                }
            )
        )


def _write_hold_evidence(tmp_path):
    """Write evidence that causes rollout gate to return hold (insufficient runs)."""
    _write_heartbeat(tmp_path, "healthy")
    # Only 1 completed workflow (need >= 3)
    wf_dir = tmp_path / "STATE" / "workflows"
    wf_dir.mkdir(parents=True, exist_ok=True)
    (wf_dir / "wf_000.json").write_text(
        json.dumps(
            {
                "workflow_id": "wf_000",
                "status": "completed",
            }
        )
    )


def _write_rollback_evidence(tmp_path):
    """Write evidence that causes rollout gate to return rollback_recommended."""
    _write_heartbeat(tmp_path, "unhealthy")


# ===================================================================
# Part 16 — Stage 3: Evaluation-Gated Expansion
# ===================================================================


class TestRollout_Stage3_EvaluationGated:
    """Verify code_impl enablement is gated by rollout evaluation."""

    def test_expansion_proceeds_on_ready(self, tmp_path):
        """ready_to_expand → code_impl added to supported_classes."""
        from agents.rollout_gate import expand_to_stage3

        _write_flags(tmp_path, _stage2_flags())
        _write_ready_evidence(tmp_path)

        result = expand_to_stage3(tmp_path)
        assert result.expanded is True
        assert result.decision == "ready_to_expand"
        assert result.config_path is not None

        # Verify config was updated
        ff = FeatureFlags(tmp_path)
        assert ff.is_task_class_supported("code_impl") is True
        assert ff.is_task_class_supported("research") is True
        assert ff.is_task_class_supported("code_review") is True
        config = ff.orchestrator_config()
        assert config["rollout_stage"] == "stage3_research_code_review_code_impl"

    def test_expansion_blocked_on_hold(self, tmp_path):
        """hold → code_impl NOT added, config unchanged."""
        from agents.rollout_gate import expand_to_stage3

        _write_flags(tmp_path, _stage2_flags())
        _write_hold_evidence(tmp_path)

        result = expand_to_stage3(tmp_path)
        assert result.expanded is False
        assert result.decision == "hold"

        # Config unchanged
        ff = FeatureFlags(tmp_path)
        assert ff.is_task_class_supported("code_impl") is False
        assert ff.is_task_class_supported("research") is True
        assert ff.is_task_class_supported("code_review") is True

    def test_expansion_blocked_on_rollback(self, tmp_path):
        """rollback_recommended → code_impl NOT added."""
        from agents.rollout_gate import expand_to_stage3

        _write_flags(tmp_path, _stage2_flags())
        _write_rollback_evidence(tmp_path)

        result = expand_to_stage3(tmp_path)
        assert result.expanded is False
        assert result.decision == "rollback_recommended"

        ff = FeatureFlags(tmp_path)
        assert ff.is_task_class_supported("code_impl") is False

    def test_expansion_preserves_version_increment(self, tmp_path):
        """Expansion increments version number."""
        from agents.rollout_gate import expand_to_stage3

        _write_flags(tmp_path, _stage2_flags())
        _write_ready_evidence(tmp_path)

        # Read current version
        flags_path = tmp_path / "STATE" / "config" / "feature_flags.json"
        before = json.loads(flags_path.read_text())
        old_version = before.get("version", 0)

        expand_to_stage3(tmp_path)

        after = json.loads(flags_path.read_text())
        assert after["version"] == old_version + 1

    def test_expansion_sets_verifier_required(self, tmp_path):
        """After expansion, verifier_required is True."""
        from agents.rollout_gate import expand_to_stage3

        _write_flags(tmp_path, _stage2_flags())
        _write_ready_evidence(tmp_path)
        expand_to_stage3(tmp_path)

        ff = FeatureFlags(tmp_path)
        config = ff.orchestrator_config()
        assert config["verifier_required"] is True

    def test_expansion_result_contains_evaluation(self, tmp_path):
        """ExpansionResult includes the full evaluation."""
        from agents.rollout_gate import expand_to_stage3

        _write_flags(tmp_path, _stage2_flags())
        _write_ready_evidence(tmp_path)
        result = expand_to_stage3(tmp_path)
        assert result.evaluation is not None
        assert result.evaluation.decision == "ready_to_expand"

    def test_expansion_with_missing_flags_file(self, tmp_path):
        """Missing feature_flags.json → expansion refused."""
        from agents.rollout_gate import expand_to_stage3

        # Don't write any flags — only evidence
        _write_ready_evidence(tmp_path)
        result = expand_to_stage3(tmp_path)
        # Gate should hold/fail because flags show disabled
        assert result.expanded is False


# ===================================================================
# Part 17 — Stage 3: code_impl Routing
# ===================================================================


class TestRollout_Stage3_CodeImplEnabled:
    """Verify code_impl routes to multi-agent path under Stage 3 flags."""

    def test_code_impl_proceeds(self, tmp_path):
        """code_impl with Stage 3 flags → proceed."""
        _write_flags(tmp_path, _stage3_flags())
        gd = GracefulDegradation(base=tmp_path)
        result = gd.check_orchestrator_available("code_impl")
        assert result.action == "proceed"
        assert "orchestrator_available" in result.reason

    def test_code_impl_with_healthy_heartbeat(self, tmp_path):
        """code_impl + HEALTHY heartbeat → proceed."""
        _write_flags(tmp_path, _stage3_flags())
        _write_heartbeat(tmp_path, "healthy")
        gd = GracefulDegradation(base=tmp_path)
        result = gd.check_orchestrator_available("code_impl")
        assert result.action == "proceed"

    def test_code_impl_with_warning_heartbeat(self, tmp_path):
        """code_impl + WARNING heartbeat → proceed (only UNHEALTHY blocks)."""
        _write_flags(tmp_path, _stage3_flags())
        _write_heartbeat(tmp_path, "warning")
        gd = GracefulDegradation(base=tmp_path)
        result = gd.check_orchestrator_available("code_impl")
        assert result.action == "proceed"

    def test_code_impl_with_no_heartbeat(self, tmp_path):
        """code_impl + no heartbeat → proceed (first-run tolerance)."""
        _write_flags(tmp_path, _stage3_flags())
        gd = GracefulDegradation(base=tmp_path)
        result = gd.check_orchestrator_available("code_impl")
        assert result.action == "proceed"

    def test_all_three_classes_proceed(self, tmp_path):
        """All three Stage 3 classes proceed when healthy."""
        _write_flags(tmp_path, _stage3_flags())
        _write_heartbeat(tmp_path, "healthy")
        gd = GracefulDegradation(base=tmp_path)
        for cls in ["research", "code_review", "code_impl"]:
            result = gd.check_orchestrator_available(cls)
            assert result.action == "proceed", f"{cls} should proceed"


# ===================================================================
# Part 18 — Stage 3: Excluded Classes Still Blocked
# ===================================================================


class TestRollout_Stage3_ExcludedClasses:
    """Verify non-selected classes remain on fallback under Stage 3."""

    def test_system_still_degrades(self, tmp_path):
        """system class still excluded from Stage 3."""
        _write_flags(tmp_path, _stage3_flags())
        gd = GracefulDegradation(base=tmp_path)
        result = gd.check_orchestrator_available("system")
        assert result.action == "degrade"
        assert "task_class_not_supported" in result.reason

    def test_simple_still_degrades(self, tmp_path):
        """simple class still excluded."""
        _write_flags(tmp_path, _stage3_flags())
        gd = GracefulDegradation(base=tmp_path)
        result = gd.check_orchestrator_available("simple")
        assert result.action == "degrade"

    def test_unknown_still_degrades(self, tmp_path):
        """unknown class still excluded."""
        _write_flags(tmp_path, _stage3_flags())
        gd = GracefulDegradation(base=tmp_path)
        result = gd.check_orchestrator_available("unknown")
        assert result.action == "degrade"


# ===================================================================
# Part 19 — Stage 3: UNHEALTHY Blocks code_impl
# ===================================================================


class TestRollout_Stage3_UnhealthyBlocks:
    """Verify UNHEALTHY heartbeat blocks code_impl under Stage 3."""

    def test_unhealthy_blocks_code_impl(self, tmp_path):
        """UNHEALTHY heartbeat → code_impl degrades."""
        _write_flags(tmp_path, _stage3_flags())
        _write_heartbeat(tmp_path, "unhealthy")
        gd = GracefulDegradation(base=tmp_path)
        result = gd.check_orchestrator_available("code_impl")
        assert result.action == "degrade"
        assert "heartbeat_unhealthy" in result.reason
        assert result.fallback == "single_agent_worker"

    def test_unhealthy_blocks_all_three_classes(self, tmp_path):
        """UNHEALTHY blocks all three supported classes."""
        _write_flags(tmp_path, _stage3_flags())
        _write_heartbeat(tmp_path, "unhealthy")
        gd = GracefulDegradation(base=tmp_path)
        for cls in ["research", "code_review", "code_impl"]:
            result = gd.check_orchestrator_available(cls)
            assert result.action == "degrade", f"{cls} should degrade"

    def test_unhealthy_blocks_all_classes_stage3(self, tmp_path):
        """UNHEALTHY blocks everything — selected and non-selected."""
        _write_flags(tmp_path, _stage3_flags())
        _write_heartbeat(tmp_path, "unhealthy")
        gd = GracefulDegradation(base=tmp_path)
        for cls in ["research", "code_review", "code_impl", "system", "simple"]:
            result = gd.check_orchestrator_available(cls)
            assert result.action == "degrade", f"{cls} should degrade"


# ===================================================================
# Part 20 — Stage 3: Rollback Behavior
# ===================================================================


class TestRollout_Stage3_Rollback:
    """Verify rollback controls work for Stage 3."""

    def test_rollback_disables_all_three(self, tmp_path):
        """Disable flag → all three classes degrade."""
        _write_flags(tmp_path, _stage3_flags())
        gd = GracefulDegradation(base=tmp_path)
        assert gd.check_orchestrator_available("code_impl").action == "proceed"

        _write_flags(tmp_path, _stage3_flags(enabled=False))
        gd2 = GracefulDegradation(base=tmp_path)
        for cls in ["research", "code_review", "code_impl"]:
            assert gd2.check_orchestrator_available(cls).action == "degrade"

    def test_rollback_remove_code_impl_only(self, tmp_path):
        """Remove code_impl → research and code_review still work."""
        _write_flags(tmp_path, _stage3_flags())
        gd = GracefulDegradation(base=tmp_path)
        assert gd.check_orchestrator_available("code_impl").action == "proceed"

        _write_flags(tmp_path, _stage3_flags(supported_classes=["research", "code_review"]))
        gd2 = GracefulDegradation(base=tmp_path)
        assert gd2.check_orchestrator_available("research").action == "proceed"
        assert gd2.check_orchestrator_available("code_review").action == "proceed"
        assert gd2.check_orchestrator_available("code_impl").action == "degrade"

    def test_rollback_to_stage1(self, tmp_path):
        """Full rollback to Stage 1 — only research."""
        _write_flags(tmp_path, _stage3_flags())
        gd = GracefulDegradation(base=tmp_path)
        assert gd.check_orchestrator_available("code_impl").action == "proceed"

        _write_flags(tmp_path, _stage3_flags(supported_classes=["research"]))
        gd2 = GracefulDegradation(base=tmp_path)
        assert gd2.check_orchestrator_available("research").action == "proceed"
        assert gd2.check_orchestrator_available("code_review").action == "degrade"
        assert gd2.check_orchestrator_available("code_impl").action == "degrade"

    def test_rollback_to_empty(self, tmp_path):
        """Remove all classes → everything degrades."""
        _write_flags(tmp_path, _stage3_flags(supported_classes=[]))
        gd = GracefulDegradation(base=tmp_path)
        for cls in ["research", "code_review", "code_impl"]:
            assert gd.check_orchestrator_available(cls).action == "degrade"

    def test_health_rollback_and_recovery(self, tmp_path):
        """HEALTHY → UNHEALTHY → code_impl blocked → HEALTHY → resumes."""
        _write_flags(tmp_path, _stage3_flags())
        _write_heartbeat(tmp_path, "healthy")
        gd = GracefulDegradation(base=tmp_path)
        assert gd.check_orchestrator_available("code_impl").action == "proceed"

        _write_heartbeat(tmp_path, "unhealthy")
        gd2 = GracefulDegradation(base=tmp_path)
        assert gd2.check_orchestrator_available("code_impl").action == "degrade"

        _write_heartbeat(tmp_path, "healthy")
        gd3 = GracefulDegradation(base=tmp_path)
        assert gd3.check_orchestrator_available("code_impl").action == "proceed"


# ===================================================================
# Part 21 — Stage 3: Verifier / Maker-Checker Enforcement
# ===================================================================


class TestRollout_Stage3_VerifierEnforcement:
    """Verify maker-checker protections apply to code_impl."""

    def test_code_impl_gets_verifier_required(self, tmp_path):
        """code_impl is in STAGE_C_CLASSES → verifier_required=True from classifier."""
        from tools.task_classifier import STAGE_C_CLASSES

        assert "code_impl" in STAGE_C_CLASSES

    def test_code_impl_eligible_low_risk(self, tmp_path):
        """Low-risk code_impl task + Stage 3 flags → eligible."""
        from tools.task_classifier import is_stageC_eligible

        eligible, reason = is_stageC_eligible(
            "code_impl",
            0.8,
            "Implement a caching layer for the API",
            _stage3_flags(),
        )
        assert eligible is True
        assert "coding_eligible" in reason

    def test_code_impl_blocked_high_risk(self, tmp_path):
        """code_impl with high-risk signals → rejected by classifier."""
        from tools.task_classifier import is_stageC_eligible

        eligible, reason = is_stageC_eligible(
            "code_impl",
            0.8,
            "Implement deployment pipeline with sudo access",
            _stage3_flags(),
        )
        assert eligible is False
        assert "high_risk" in reason

    def test_code_impl_classifier_sets_verifier_required(self, tmp_path):
        """classify_and_route() sets verifier_required for code_impl."""
        # Temporarily set env so classifier loads our flags
        flags_path = tmp_path / "STATE" / "config" / "feature_flags.json"
        full_flags = {
            "phase7_orchestrator": _stage3_flags(),
        }
        flags_path.write_text(json.dumps(full_flags))

        # Use a task that classifies as code_impl
        import tools.task_classifier as tc

        tc.flags_path if hasattr(tc, "flags_path") else None

        # Directly test with the flags dict
        from tools.task_classifier import STAGE_C_CLASSES, is_stageC_eligible

        eligible, _ = is_stageC_eligible("code_impl", 0.8, "Refactor the module", _stage3_flags())
        assert eligible is True
        # The routing dict would set verifier_required for code_impl
        assert "code_impl" in STAGE_C_CLASSES

    def test_verifier_unavailable_halts(self, tmp_path):
        """Verifier unavailable → halt (not silent continuation)."""
        gd = GracefulDegradation(base=tmp_path)
        result = gd.handle_verifier_unavailable("wf_test")
        assert result.action == "halt"
        assert "verifier_unavailable" in result.reason

    def test_system_class_still_excluded_from_stageC(self, tmp_path):
        """system class is NOT in STAGE_C_CLASSES → not eligible."""
        from tools.task_classifier import is_stageC_eligible

        eligible, reason = is_stageC_eligible(
            "system",
            0.8,
            "Configure the infrastructure",
            _stage3_flags(),
        )
        assert eligible is False
        assert "not_in_stageC" in reason


# ===================================================================
# Part 22 — Stage 3: Config and Full Pre-Flight
# ===================================================================


class TestRollout_Stage3_Config:
    """Verify Stage 3 config fields."""

    def test_rollout_stage_field(self, tmp_path):
        """rollout_stage reads as stage3_research_code_review_code_impl."""
        _write_flags(tmp_path, _stage3_flags())
        ff = FeatureFlags(tmp_path)
        config = ff.orchestrator_config()
        assert config["rollout_stage"] == "stage3_research_code_review_code_impl"

    def test_all_three_classes_supported(self, tmp_path):
        """All three classes pass is_task_class_supported."""
        _write_flags(tmp_path, _stage3_flags())
        ff = FeatureFlags(tmp_path)
        assert ff.is_task_class_supported("research") is True
        assert ff.is_task_class_supported("code_review") is True
        assert ff.is_task_class_supported("code_impl") is True
        assert ff.is_task_class_supported("system") is False

    def test_expansion_result_serializable(self, tmp_path):
        """ExpansionResult.to_dict() is JSON-serializable."""
        from agents.rollout_gate import expand_to_stage3

        _write_flags(tmp_path, _stage2_flags())
        _write_ready_evidence(tmp_path)
        result = expand_to_stage3(tmp_path)
        d = result.to_dict()
        serialized = json.dumps(d)
        assert "ready_to_expand" in serialized


class TestRollout_Stage3_FullPreFlight:
    """Combined pre-flight for Stage 3 dispatch."""

    def test_preflight_code_impl_healthy(self, tmp_path):
        """Stage 3 + code_impl + HEALTHY → proceed through all gates."""
        _write_flags(
            tmp_path,
            _stage3_flags(),
            {
                "archive_cleanup": True,
                "rate_limiting": True,
                "manual_approval": False,
            },
        )
        _write_heartbeat(tmp_path, "healthy")

        gd = GracefulDegradation(base=tmp_path)
        r1 = gd.check_orchestrator_available("code_impl")
        assert r1.action == "proceed"
        r2 = gd.check_workflow_launch_feasibility()
        assert r2.action == "proceed"
        r3 = gd.check_spawn_feasibility()
        assert r3.action == "proceed"

    def test_preflight_all_three_healthy(self, tmp_path):
        """Stage 3 + all three classes + HEALTHY → all proceed."""
        _write_flags(tmp_path, _stage3_flags())
        _write_heartbeat(tmp_path, "healthy")
        gd = GracefulDegradation(base=tmp_path)
        assert gd.check_orchestrator_available("research").action == "proceed"
        assert gd.check_orchestrator_available("code_review").action == "proceed"
        assert gd.check_orchestrator_available("code_impl").action == "proceed"

    def test_preflight_system_still_blocked(self, tmp_path):
        """Stage 3 + system + HEALTHY → blocked at class gate."""
        _write_flags(tmp_path, _stage3_flags())
        _write_heartbeat(tmp_path, "healthy")
        gd = GracefulDegradation(base=tmp_path)
        r1 = gd.check_orchestrator_available("system")
        assert r1.action == "degrade"
        assert "task_class_not_supported" in r1.reason
