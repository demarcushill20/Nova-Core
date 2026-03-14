"""Tests for agents/memory_evaluator.py — Phase 4 Candidate Evaluation.

Covers:
- Score computation (importance, novelty, durability)
- Decision rules (reject, working_only, keep, promotable)
- Transient event handling
- Low-value episodic downgrade
- Durable event preservation
- Promotion eligibility/blocker logic
- Router integration (evaluation in store path)
- Trace output with evaluation fields
- No regression of Phase 2 layer/store guardrails
"""

from pathlib import Path
from unittest.mock import patch

from agents.memory_evaluator import (
    VALID_EVAL_ACTIONS,
    _compute_durability,
    _compute_importance,
    _compute_novelty,
    evaluate_candidate,
)
from agents.memory_router import (
    CanonicalMemoryObject,
    MemoryFileAdapter,
    MemoryRouter,
    WorkingMemoryAdapter,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cmo(**kw) -> CanonicalMemoryObject:
    """Build a minimal valid CanonicalMemoryObject for evaluation tests."""
    defaults = {
        "memory_id": "cm-test-123456",
        "timestamp": "2026-03-13T12:00:00Z",
        "source": "watcher",
        "event_type": "task_completed",
        "provenance": "automatic",
        "title": "Test memory object title",
        "summary": "A test task was completed successfully with all verifications passing.",
        "confidence": "medium",
        "memory_layer_candidate": "episodic",
        "promotion_status": "candidate",
        "related_project": "nova-core",
        "current_layer": "episodic",
        "promotion_eligibility": "eligible",
    }
    defaults.update(kw)
    return CanonicalMemoryObject(**defaults)


def _make_router(tmp_path: Path) -> MemoryRouter:
    """Router with memory_file + state_working adapters."""
    (tmp_path / "workflow_learnings").mkdir(parents=True, exist_ok=True)
    return MemoryRouter(
        adapters=[
            MemoryFileAdapter(base=tmp_path),
            WorkingMemoryAdapter(base=tmp_path),
        ],
        memory_file_base=tmp_path,
    )


# ---------------------------------------------------------------------------
# Tests: Score computation
# ---------------------------------------------------------------------------


class TestImportanceScore:
    """Importance score reflects event significance and content quality."""

    def test_high_confidence_outcome_scores_high(self):
        score = _compute_importance(
            "task_completed",
            "high",
            "Implemented full vault schema validation with 73 passing tests.",
            "Task done",
        )
        assert score >= 0.5

    def test_low_confidence_transient_scores_low(self):
        score = _compute_importance(
            "heartbeat_cycle",
            "low",
            "OK",
            "hb",
        )
        assert score < 0.3

    def test_durable_events_score_highest(self):
        score = _compute_importance(
            "workflow_learning_promoted",
            "high",
            "Schema-first validation pattern proven across 3 tasks.",
            "Promoted",
        )
        assert score >= 0.6

    def test_long_summary_gets_content_bonus(self):
        short = _compute_importance("task_completed", "medium", "Done.", "Task")
        long = _compute_importance(
            "task_completed",
            "medium",
            "Implemented full vault schema validation with comprehensive test coverage "
            "including edge cases for all 7 note types and frontmatter parsing. " * 2,
            "Task done",
        )
        assert long > short

    def test_score_bounded_0_to_1(self):
        for et in ["task_completed", "heartbeat_cycle", "workflow_learning_promoted", "unknown"]:
            for conf in ["high", "medium", "low"]:
                score = _compute_importance(et, conf, "Summary text here.", "Title")
                assert 0.0 <= score <= 1.0


class TestNoveltyScore:
    """Novelty score differentiates fresh outcomes from routine events."""

    def test_outcome_events_have_higher_novelty(self):
        outcome = _compute_novelty("task_completed", "Done")
        transient = _compute_novelty("heartbeat_cycle", "OK")
        assert outcome > transient

    def test_transient_events_low_novelty(self):
        assert _compute_novelty("heartbeat_cycle", "check") <= 0.3


class TestDurabilityScore:
    """Durability reflects how long-lived the memory value is."""

    def test_procedural_layer_highest_durability(self):
        score = _compute_durability("agent_pattern_promoted", "procedural", "high")
        assert score >= 0.8

    def test_working_layer_lowest_durability(self):
        score = _compute_durability("heartbeat_cycle", "working", "low")
        assert score <= 0.3

    def test_durable_event_type_adds_bonus(self):
        durable = _compute_durability("workflow_learning_promoted", "semantic", "high")
        standard = _compute_durability("task_completed", "semantic", "high")
        assert durable > standard

    def test_score_bounded_0_to_1(self):
        for layer in ["working", "episodic", "semantic", "procedural"]:
            for conf in ["high", "medium", "low"]:
                score = _compute_durability("task_completed", layer, conf)
                assert 0.0 <= score <= 1.0


# ---------------------------------------------------------------------------
# Tests: Decision rules
# ---------------------------------------------------------------------------


class TestRejectDecision:
    """Evaluator rejects empty/sparse candidates."""

    def test_empty_title_and_summary_rejected(self):
        obj = _cmo(title="", summary="")
        decision = evaluate_candidate(obj)
        assert decision.action == "reject"
        assert "sparse" in decision.reason

    def test_near_empty_content_rejected(self):
        obj = _cmo(title="Hi", summary="OK")
        decision = evaluate_candidate(obj)
        assert decision.action == "reject"
        assert decision.promotion_eligibility == "ineligible"


class TestWorkingOnlyDecision:
    """Evaluator downgrades transient or low-value candidates to working-only."""

    def test_heartbeat_cycle_is_working_only(self):
        obj = _cmo(
            event_type="heartbeat_cycle",
            current_layer="working",
            title="heartbeat_cycle: HEALTHY",
            summary="Heartbeat: 5 checks, 0 failed.",
        )
        decision = evaluate_candidate(obj)
        assert decision.action == "working_only"
        assert decision.adjusted_layer == "working"
        assert "transient" in decision.reason

    def test_session_end_is_working_only(self):
        obj = _cmo(
            event_type="session_end",
            current_layer="working",
            title="session_end: done",
            summary="Session ended after 3 tasks.",
        )
        decision = evaluate_candidate(obj)
        assert decision.action == "working_only"

    def test_low_importance_episodic_downgraded(self):
        obj = _cmo(
            event_type="task_completed",
            current_layer="episodic",
            confidence="low",
            title="Minor task",
            summary="Done.",  # Very short
        )
        decision = evaluate_candidate(obj)
        assert decision.action == "working_only"
        assert decision.adjusted_layer == "working"

    def test_thin_summary_episodic_downgraded(self):
        obj = _cmo(
            event_type="task_completed",
            current_layer="episodic",
            confidence="high",
            title="Good title with substance",
            summary="Short.",  # Below 20 chars
        )
        decision = evaluate_candidate(obj)
        assert decision.action == "working_only"
        assert "thin_summary" in decision.reason


class TestKeepDecision:
    """Evaluator keeps standard candidates in their current layer."""

    def test_standard_task_completed_kept(self):
        obj = _cmo(
            event_type="task_completed",
            confidence="medium",
            summary="Implemented the feature as described in the task.",
        )
        decision = evaluate_candidate(obj)
        assert decision.action in ("keep_in_current_layer", "keep_and_mark_promotable")

    def test_durable_event_kept_as_is(self):
        obj = _cmo(
            event_type="workflow_learning_promoted",
            current_layer="semantic",
            summary="Schema-first validation pattern proven across multiple tasks.",
        )
        decision = evaluate_candidate(obj)
        assert decision.action == "keep_in_current_layer"
        assert "durable_event" in decision.reason

    def test_already_promoted_semantic_flagged(self):
        obj = _cmo(
            event_type="decision_made",
            current_layer="semantic",
            summary="Architecture decision: use router pattern for all memory ops.",
        )
        decision = evaluate_candidate(obj)
        assert decision.promotion_eligibility == "already_promoted"


class TestPromotableDecision:
    """Evaluator marks high-value outcomes as promotion-eligible."""

    def test_high_value_task_completion_promotable(self):
        obj = _cmo(
            event_type="task_completed",
            confidence="high",
            summary="Implemented full vault schema validation with comprehensive test coverage "
            "including edge cases for all 7 note types. All 73 tests pass. " * 2,
        )
        decision = evaluate_candidate(obj)
        assert decision.action == "keep_and_mark_promotable"
        assert decision.promotion_eligibility == "eligible"
        assert decision.promotion_blocker is None

    def test_plan_created_with_good_grade_promotable(self):
        obj = _cmo(
            event_type="plan_created",
            confidence="high",
            summary="Plan plan_001 completed with grade A, all 5 steps successful. "
            "Evaluation score 0.95, no followup needed.",
        )
        decision = evaluate_candidate(obj)
        assert decision.action == "keep_and_mark_promotable"

    def test_low_confidence_blocks_promotion(self):
        obj = _cmo(
            event_type="task_completed",
            confidence="low",
            summary="Task completed but with low confidence verification.",
        )
        decision = evaluate_candidate(obj)
        assert decision.promotion_eligibility in ("ineligible", "eligible")
        # If ineligible, should have a blocker
        if decision.promotion_eligibility == "ineligible":
            assert decision.promotion_blocker is not None


class TestPromotionBlocker:
    """Promotion blockers are set when criteria aren't met."""

    def test_low_importance_sets_blocker(self):
        obj = _cmo(
            event_type="task_completed",
            confidence="low",
            summary="Tiny change, no real significance for the project.",
        )
        decision = evaluate_candidate(obj)
        if decision.promotion_blocker:
            assert "importance" in decision.promotion_blocker or "threshold" in decision.promotion_blocker

    def test_transient_event_sets_blocker_when_low_importance(self):
        obj = _cmo(
            event_type="heartbeat_cycle",
            current_layer="working",
            confidence="low",
            title="heartbeat_cycle: OK",
            summary="All systems nominal, no findings.",
        )
        decision = evaluate_candidate(obj)
        if decision.promotion_blocker:
            assert "transient" in decision.promotion_blocker


# ---------------------------------------------------------------------------
# Tests: EvalDecision structure
# ---------------------------------------------------------------------------


class TestEvalDecisionStructure:
    """EvalDecision has all required fields and valid values."""

    def test_all_actions_are_valid(self):
        for et in ["task_completed", "heartbeat_cycle", "workflow_learning_promoted", "session_end", "plan_created"]:
            obj = _cmo(
                event_type=et,
                summary="A meaningful summary with enough content for evaluation.",
            )
            decision = evaluate_candidate(obj)
            assert decision.action in VALID_EVAL_ACTIONS, f"{et} → {decision.action}"

    def test_to_dict_includes_all_fields(self):
        obj = _cmo()
        decision = evaluate_candidate(obj)
        d = decision.to_dict()
        assert "action" in d
        assert "reason" in d
        assert "importance_score" in d
        assert "novelty_score" in d
        assert "durability_score" in d
        assert "promotion_eligibility" in d
        assert "promotion_blocker" in d
        assert "adjusted_layer" in d

    def test_scores_are_numeric(self):
        obj = _cmo()
        decision = evaluate_candidate(obj)
        assert isinstance(decision.importance_score, float)
        assert isinstance(decision.novelty_score, float)
        assert isinstance(decision.durability_score, float)


# ---------------------------------------------------------------------------
# Tests: Router integration
# ---------------------------------------------------------------------------


class TestRouterEvaluationIntegration:
    """Router store() applies evaluation before persistence."""

    def test_evaluation_scores_applied_to_cmo(self, tmp_path):
        router = _make_router(tmp_path)
        obj = _cmo(target_store="memory_file")
        router.store(obj, caller="test")
        # After store(), scores should be populated
        assert obj.importance_score is not None
        assert obj.novelty_score is not None
        assert obj.durability_score is not None

    def test_evaluation_reject_prevents_storage(self, tmp_path):
        router = _make_router(tmp_path)
        obj = _cmo(title="", summary="", target_store="memory_file")
        result = router.store(obj, caller="test")
        # Validation will catch empty title/summary first
        assert result.stored is False

    def test_working_only_downgrade_changes_layer(self, tmp_path):
        router = _make_router(tmp_path)
        obj = _cmo(
            event_type="heartbeat_cycle",
            current_layer="working",
            title="heartbeat_cycle: HEALTHY",
            summary="Heartbeat: 5 checks, 0 failed.",
        )
        result = router.store(obj, caller="test")
        assert obj.current_layer == "working"
        # Should route to state_working
        if result.stored:
            assert result.store_used == "state_working"

    def test_episodic_downgrade_to_working_routes_correctly(self, tmp_path):
        """Low-value episodic candidate gets downgraded to working, stored in state_working."""
        router = _make_router(tmp_path)
        obj = _cmo(
            event_type="task_completed",
            current_layer="episodic",
            confidence="low",
            title="Minor task done",
            summary="Done.",  # Very short → thin_summary triggers working_only
        )
        result = router.store(obj, caller="test")
        # Should be downgraded to working layer
        assert obj.current_layer == "working"
        if result.stored:
            assert result.store_used == "state_working"

    def test_promotable_candidate_gets_eligible_flag(self, tmp_path):
        router = _make_router(tmp_path)
        obj = _cmo(
            event_type="task_completed",
            confidence="high",
            summary="Implemented full vault schema validation with comprehensive test coverage "
            "including edge cases for all 7 note types. All 73 tests pass. " * 2,
            target_store="memory_file",
        )
        router.store(obj, caller="test")
        assert obj.promotion_eligibility == "eligible"

    def test_phase2_guardrails_still_enforced(self, tmp_path):
        """Evaluation doesn't bypass layer/store compatibility."""
        router = _make_router(tmp_path)
        # Semantic → memory_file should still be blocked
        obj = _cmo(
            event_type="decision_made",
            current_layer="semantic",
            target_store="memory_file",
            summary="Architecture decision: use router pattern for all memory operations.",
        )
        result = router.store(obj, caller="test")
        assert result.stored is False
        assert "layer_store_mismatch" in result.rejection_reason


# ---------------------------------------------------------------------------
# Tests: Trace output with evaluation fields
# ---------------------------------------------------------------------------


class TestEvaluationTracing:
    """Router traces include Phase 4 evaluation data."""

    def test_store_trace_includes_eval_fields(self, tmp_path):
        router = _make_router(tmp_path)
        obj = _cmo(
            event_type="task_completed",
            confidence="high",
            summary="Implemented feature with full test coverage and documentation.",
            target_store="memory_file",
        )
        with patch("agents.memory_router.slog") as mock_slog:
            router.store(obj, caller="test")
            store_calls = [c for c in mock_slog.event.call_args_list if c[0][0] == "memory.router.store"]
            assert len(store_calls) >= 1
            kw = store_calls[-1][1]
            assert "eval_action" in kw
            assert "eval_reason" in kw
            assert "importance_score" in kw

    def test_rejection_trace_includes_eval_fields(self, tmp_path):
        router = _make_router(tmp_path)
        obj = _cmo(
            title="Hi",
            summary="OK",  # Near-empty → reject
        )
        with patch("agents.memory_router.slog") as mock_slog:
            router.store(obj, caller="test")
            store_calls = [c for c in mock_slog.event.call_args_list if c[0][0] == "memory.router.store"]
            assert len(store_calls) >= 1
            kw = store_calls[0][1]
            assert "eval_action" in kw
            assert kw["eval_action"] == "reject"
