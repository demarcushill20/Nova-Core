"""Phase 7.9 — Feature-Flagged Rollout Tests.

Deterministic tests for Stage 1 research-only rollout behavior:
- Research class enabled → multi-agent path
- Non-research classes → single-agent fallback
- Flag off → safe fallback
- UNHEALTHY heartbeat → auto-degrade
- Rollback behavior (flag change)
- Health gating edge cases
"""

import json
import os
import time

import pytest
from pathlib import Path

from agents.production_hardening import (
    DegradationResult,
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
        _write_heartbeat(tmp_path, "unhealthy", findings=[
            {"category": "workflow_stuck", "severity": "unhealthy",
             "subject": "wf_001", "detail": "stuck"}
        ])
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
        task_class, confidence = classify_task(
            "Research the best practices for API rate limiting"
        )
        assert task_class == "research"
        assert confidence > 0.0

    def test_classify_coding_task_not_research(self, tmp_path):
        """Coding task classified correctly (not research)."""
        from tools.task_classifier import classify_task
        task_class, _ = classify_task(
            "Implement a new authentication module with JWT tokens"
        )
        assert task_class != "research"

    def test_research_eligible_with_stage1_flags(self, tmp_path):
        """Research task + Stage 1 flags → eligible via is_stageC_eligible."""
        from tools.task_classifier import is_stageC_eligible
        eligible, reason = is_stageC_eligible(
            "research", 0.8,
            "Investigate how other projects handle caching",
            _stage1_flags(),
        )
        assert eligible is True
        assert "research_eligible" in reason

    def test_coding_ineligible_with_stage1_flags(self, tmp_path):
        """Coding task + Stage 1 flags → ineligible (class not supported)."""
        from tools.task_classifier import is_stageC_eligible
        eligible, reason = is_stageC_eligible(
            "code_impl", 0.8,
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
        _write_flags(tmp_path, _stage1_flags(), {
            "archive_cleanup": True,
            "rate_limiting": True,
            "manual_approval": False,
        })
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
        _write_flags(tmp_path, _stage1_flags(), {
            "archive_cleanup": True,
            "rate_limiting": True,
        })
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
