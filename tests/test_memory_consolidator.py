"""Tests for agents/memory_consolidator.py — Phase 6 Consolidation.

Covers:
- Window validation and minimum size
- Deduplication
- Anti-amplification safeguards
- Session summary creation
- Working memory checkpoint creation
- Task batch consolidation
- Failure window consolidation
- Heartbeat summary consolidation
- Plan execution summary
- Pattern extraction from repeated evidence
- Insufficient evidence rejection
- Router integration (end-to-end consolidation)
- Trace output for consolidation decisions
"""

from pathlib import Path
from unittest.mock import patch

from agents.memory_consolidator import (
    VALID_CONSOLIDATION_ACTIONS,
    VALID_WINDOW_TYPES,
    PatternCandidate,
    WindowSpec,
    check_anti_amplification,
    consolidate_window,
    dedupe_window_items,
    extract_pattern_candidates,
    summarize_session,
)
from agents.memory_router import (
    MemoryFileAdapter,
    MemoryRouter,
    WorkingMemoryAdapter,
)
from utils.trace_context import TraceContext

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_router(tmp_path: Path) -> MemoryRouter:
    (tmp_path / "workflow_learnings").mkdir(parents=True, exist_ok=True)
    return MemoryRouter(
        adapters=[
            MemoryFileAdapter(base=tmp_path),
            WorkingMemoryAdapter(base=tmp_path),
        ],
        memory_file_base=tmp_path,
    )


def _task_item(stem="task_1", status="completed", summary="Did the thing.", **kw):
    base = {
        "stem": stem,
        "status": status,
        "event_type": "task_completed" if status == "completed" else "task_failed",
        "title": f"{stem}: {summary[:40]}",
        "summary": summary,
        "confidence": "medium",
        "source": "watcher",
    }
    base.update(kw)
    return base


def _heartbeat_item(healthy=True, **kw):
    status = "HEALTHY" if healthy else "UNHEALTHY"
    base = {
        "event_type": "heartbeat_cycle",
        "title": f"heartbeat_cycle: {status}",
        "summary": f"Heartbeat: {status}. 5 checks, 0 failed.",
        "confidence": "high",
        "source": "heartbeat",
    }
    base.update(kw)
    return base


def _working_memory_item(event_type="heartbeat_cycle", **kw):
    base = {
        "event_type": event_type,
        "title": f"{event_type}: ok",
        "summary": f"{event_type} completed normally.",
        "confidence": "medium",
        "source": "watcher",
    }
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# Tests: Window validation
# ---------------------------------------------------------------------------


class TestWindowValidation:
    """Consolidation validates window type and minimum size."""

    def test_invalid_window_type_returns_no_action(self):
        spec = WindowSpec(window_type="invalid", source_items=[_task_item()])
        result = consolidate_window(spec)
        assert result.action == "no_action"
        assert "invalid_window_type" in result.reason

    def test_empty_items_rejected(self):
        spec = WindowSpec(window_type="session", source_items=[])
        result = consolidate_window(spec)
        assert result.action == "rejected_insufficient_evidence"

    def test_below_minimum_rejected(self):
        spec = WindowSpec(window_type="working_memory", source_items=[_working_memory_item()])
        result = consolidate_window(spec)
        assert result.action == "rejected_insufficient_evidence"
        assert "window_too_small" in result.reason

    def test_all_valid_window_types_accepted(self):
        for wt in VALID_WINDOW_TYPES:
            spec = WindowSpec(
                window_type=wt,
                source_items=[
                    _task_item(stem=f"t{i}", summary=f"Task {i} with enough detail to pass.") for i in range(5)
                ],
            )
            result = consolidate_window(spec)
            assert result.action in VALID_CONSOLIDATION_ACTIONS, f"{wt} → {result.action}"


# ---------------------------------------------------------------------------
# Tests: Deduplication
# ---------------------------------------------------------------------------


class TestDeduplication:
    """Window items are deduplicated before consolidation."""

    def test_exact_duplicates_removed(self):
        items = [_task_item(stem="t1", summary="Same thing.")] * 3
        deduped, removed = dedupe_window_items(items)
        assert len(deduped) == 1
        assert removed == 2

    def test_different_items_preserved(self):
        items = [
            _task_item(stem="t1", summary="First task."),
            _task_item(stem="t2", summary="Second task."),
        ]
        deduped, removed = dedupe_window_items(items)
        assert len(deduped) == 2
        assert removed == 0

    def test_dedupe_after_minimum_check(self):
        """If dedupe reduces below minimum, consolidation is rejected."""
        items = [_working_memory_item()] * 5  # all identical
        spec = WindowSpec(window_type="working_memory", source_items=items)
        result = consolidate_window(spec)
        # After dedupe: 1 unique item < 2 minimum
        assert result.action == "rejected_insufficient_evidence"
        assert result.dedupe_removed == 4


# ---------------------------------------------------------------------------
# Tests: Anti-amplification safeguards
# ---------------------------------------------------------------------------


class TestAntiAmplification:
    """Prevent consolidation from multiplying noise."""

    def test_heartbeat_noise_blocked_in_non_heartbeat_window(self):
        items = [_heartbeat_item() for _ in range(5)]
        reasons = check_anti_amplification(items, "session")
        assert any("heartbeat_noise" in r for r in reasons)

    def test_heartbeat_allowed_in_heartbeat_window(self):
        items = [_heartbeat_item() for _ in range(5)]
        reasons = check_anti_amplification(items, "heartbeat")
        assert not any("heartbeat_noise" in r for r in reasons)

    def test_all_low_confidence_blocked(self):
        items = [_task_item(confidence="low") for _ in range(3)]
        reasons = check_anti_amplification(items, "task_batch")
        assert any("all_low_confidence" in r for r in reasons)

    def test_mixed_confidence_allowed(self):
        items = [
            _task_item(confidence="high"),
            _task_item(stem="t2", summary="Other.", confidence="low"),
        ]
        reasons = check_anti_amplification(items, "task_batch")
        assert not any("all_low_confidence" in r for r in reasons)

    def test_uniform_thin_content_blocked(self):
        items = [
            {"event_type": "heartbeat_cycle", "title": "hb", "summary": "ok", "source": "hb", "confidence": "medium"},
            {
                "event_type": "heartbeat_cycle",
                "title": "hb2",
                "summary": "fine",
                "source": "hb",
                "confidence": "medium",
            },
            {"event_type": "heartbeat_cycle", "title": "hb3", "summary": "yes", "source": "hb", "confidence": "medium"},
        ]
        reasons = check_anti_amplification(items, "heartbeat")
        assert any("uniform_thin" in r for r in reasons)

    def test_anti_amplification_prevents_consolidation(self):
        """Full pipeline: anti-amplification stops session from heartbeat noise."""
        items = [_heartbeat_item() for _ in range(5)]
        spec = WindowSpec(window_type="session", source_items=items)
        result = consolidate_window(spec)
        assert result.action == "rejected_insufficient_evidence"
        assert len(result.suppression_reasons) > 0


# ---------------------------------------------------------------------------
# Tests: Session summary
# ---------------------------------------------------------------------------


class TestSessionSummary:
    """Session consolidation produces episodic summary artifacts."""

    def test_session_with_tasks_produces_summary(self):
        items = [
            _task_item(stem="task_1", summary="Implemented feature A."),
            _task_item(stem="task_2", summary="Fixed bug in module B."),
        ]
        spec = WindowSpec(window_type="session", source_items=items, session_id="ses_123")
        result = consolidate_window(spec)
        assert result.action == "summary_created"
        assert result.output_layer == "episodic"
        assert result.output_artifact is not None
        assert "ses_123" in result.output_artifact["title"]

    def test_session_summary_includes_task_count(self):
        items = [_task_item(stem=f"task_{i}", summary=f"Completed task number {i} successfully.") for i in range(3)]
        spec = WindowSpec(window_type="session", source_items=items)
        result = consolidate_window(spec)
        assert result.action == "summary_created"
        assert "3" in result.output_artifact["summary"]

    def test_session_with_failures_has_lower_confidence(self):
        items = [
            _task_item(stem="t1", status="failed", summary="Crash in module A."),
            _task_item(stem="t2", status="failed", summary="Timeout on API call."),
            _task_item(stem="t3", status="completed", summary="Done."),
        ]
        spec = WindowSpec(window_type="session", source_items=items)
        result = consolidate_window(spec)
        assert result.confidence in ("low", "medium")

    def test_summarize_session_function(self):
        session_data = {
            "session_id": "ses_abc",
            "tasks": [
                _task_item(stem="t1", summary="Built the feature."),
                _task_item(stem="t2", summary="Wrote tests."),
            ],
        }
        result = summarize_session(session_data)
        assert result.action == "summary_created"

    def test_empty_session_rejected(self):
        result = summarize_session({"session_id": "empty", "tasks": []})
        assert result.action == "rejected_insufficient_evidence"


# ---------------------------------------------------------------------------
# Tests: Working memory checkpoint
# ---------------------------------------------------------------------------


class TestWorkingMemoryCheckpoint:
    """Working memory consolidation produces checkpoint artifacts."""

    def test_working_memory_checkpoint(self):
        items = [
            _working_memory_item(event_type="heartbeat_cycle"),
            _working_memory_item(event_type="session_end", summary="Session ended normally."),
        ]
        spec = WindowSpec(window_type="working_memory", source_items=items)
        result = consolidate_window(spec)
        assert result.action == "checkpoint_created"
        assert result.output_layer == "working"

    def test_checkpoint_includes_event_breakdown(self):
        items = [
            _working_memory_item(event_type="heartbeat_cycle"),
            _working_memory_item(event_type="heartbeat_cycle", title="hb2", summary="Second cycle."),
            _working_memory_item(event_type="session_end", summary="Done for now."),
        ]
        spec = WindowSpec(window_type="working_memory", source_items=items)
        result = consolidate_window(spec)
        assert "heartbeat_cycle" in result.output_artifact["summary"]


# ---------------------------------------------------------------------------
# Tests: Task batch consolidation
# ---------------------------------------------------------------------------


class TestTaskBatchConsolidation:
    """Task batch produces episodic summary with promotion eligibility."""

    def test_batch_with_completed_tasks(self):
        items = [_task_item(stem=f"t{i}", summary=f"Completed task {i} successfully.") for i in range(3)]
        spec = WindowSpec(window_type="task_batch", source_items=items)
        result = consolidate_window(spec)
        assert result.action == "summary_created"
        assert result.output_layer == "episodic"

    def test_batch_no_completed_rejected(self):
        items = [
            {
                "event_type": "heartbeat_cycle",
                "title": "hb",
                "summary": "Normal cycle ok.",
                "source": "hb",
                "confidence": "medium",
            },
            {
                "event_type": "session_end",
                "title": "end",
                "summary": "Session ended ok fine.",
                "source": "watcher",
                "confidence": "medium",
            },
        ]
        spec = WindowSpec(window_type="task_batch", source_items=items)
        result = consolidate_window(spec)
        assert result.action == "rejected_insufficient_evidence"

    def test_high_confidence_batch_promotion_eligible(self):
        items = [_task_item(stem=f"t{i}", summary=f"High quality task {i}.", confidence="high") for i in range(4)]
        spec = WindowSpec(window_type="task_batch", source_items=items)
        result = consolidate_window(spec)
        assert result.promotion_eligible is True


# ---------------------------------------------------------------------------
# Tests: Failure window
# ---------------------------------------------------------------------------


class TestFailureWindow:
    """Failure window consolidates repeated failures."""

    def test_failure_incident_created(self):
        items = [
            _task_item(stem="t1", status="failed", summary="Connection timeout to API."),
            _task_item(stem="t2", status="failed", summary="API endpoint unreachable."),
        ]
        spec = WindowSpec(window_type="failure_window", source_items=items)
        result = consolidate_window(spec)
        assert result.action == "summary_created"
        assert "failure_incident" in result.reason

    def test_single_failure_rejected(self):
        items = [_task_item(stem="t1", status="failed", summary="One-off error.")]
        spec = WindowSpec(window_type="failure_window", source_items=items)
        result = consolidate_window(spec)
        assert result.action == "rejected_insufficient_evidence"


# ---------------------------------------------------------------------------
# Tests: Heartbeat window
# ---------------------------------------------------------------------------


class TestHeartbeatWindow:
    """Heartbeat window produces operational summary."""

    def test_healthy_heartbeat_summary(self):
        items = [_heartbeat_item(healthy=True) for _ in range(3)]
        # Make items unique enough to pass dedupe
        for i, item in enumerate(items):
            item["summary"] = f"Heartbeat cycle {i}: HEALTHY. All checks passed."
        spec = WindowSpec(window_type="heartbeat", source_items=items)
        result = consolidate_window(spec)
        assert result.action == "checkpoint_created"
        assert result.confidence == "high"

    def test_unhealthy_heartbeat_summary(self):
        items = [
            _heartbeat_item(healthy=True),
            _heartbeat_item(healthy=True, summary="Cycle 2: HEALTHY. All ok."),
            _heartbeat_item(healthy=False, summary="UNHEALTHY: disk full warning."),
        ]
        spec = WindowSpec(window_type="heartbeat", source_items=items)
        result = consolidate_window(spec)
        assert result.action == "checkpoint_created"
        assert result.confidence == "medium"
        assert "DEGRADED" in result.output_artifact["title"]


# ---------------------------------------------------------------------------
# Tests: Plan execution
# ---------------------------------------------------------------------------


class TestPlanExecution:
    """Plan execution window produces plan summary."""

    def test_plan_summary_created(self):
        items = [
            _task_item(stem="step_1", summary="Step 1 completed.", plan_id="plan_001"),
            _task_item(stem="step_2", summary="Step 2 done."),
        ]
        spec = WindowSpec(window_type="plan_execution", source_items=items)
        result = consolidate_window(spec)
        assert result.action == "summary_created"
        assert "plan_001" in result.output_artifact.get("title", "")


# ---------------------------------------------------------------------------
# Tests: Pattern extraction
# ---------------------------------------------------------------------------


class TestPatternExtraction:
    """Extract pattern candidates from repeated evidence."""

    def test_no_patterns_from_few_items(self):
        items = [_task_item(stem="t1"), _task_item(stem="t2", summary="Other.")]
        candidates = extract_pattern_candidates(items)
        assert candidates == []

    def test_pattern_from_repeated_type(self):
        items = [
            _task_item(stem=f"t{i}", summary=f"Task {i} done.", confidence="high", task_class="code_impl")
            for i in range(4)
        ]
        candidates = extract_pattern_candidates(items, min_evidence=3)
        assert len(candidates) >= 1
        assert candidates[0].evidence_count >= 3

    def test_low_confidence_not_promotable(self):
        items = [
            _task_item(stem=f"t{i}", summary=f"Task {i}.", confidence="low", task_class="code_impl") for i in range(4)
        ]
        candidates = extract_pattern_candidates(items, min_evidence=3)
        # Low confidence items don't meet the quality threshold
        assert all(not c.promotion_eligible for c in candidates) or len(candidates) == 0

    def test_pattern_candidate_structure(self):
        items = [
            _task_item(stem=f"t{i}", summary=f"Implemented feature {i}.", confidence="high", task_class="feature_impl")
            for i in range(4)
        ]
        candidates = extract_pattern_candidates(items, min_evidence=3)
        if candidates:
            c = candidates[0]
            assert isinstance(c, PatternCandidate)
            d = c.to_dict()
            assert "pattern_description" in d
            assert "evidence_count" in d
            assert "promotion_eligible" in d


# ---------------------------------------------------------------------------
# Tests: ConsolidationResult structure
# ---------------------------------------------------------------------------


class TestConsolidationResultStructure:
    """ConsolidationResult has all required fields."""

    def test_all_actions_are_valid(self):
        for wt in VALID_WINDOW_TYPES:
            items = [_task_item(stem=f"t{i}", summary=f"Task {i} complete.") for i in range(5)]
            spec = WindowSpec(window_type=wt, source_items=items)
            result = consolidate_window(spec)
            assert result.action in VALID_CONSOLIDATION_ACTIONS

    def test_to_dict_includes_all_fields(self):
        spec = WindowSpec(window_type="session", source_items=[_task_item()])
        result = consolidate_window(spec)
        d = result.to_dict()
        assert "action" in d
        assert "reason" in d
        assert "input_count" in d
        assert "dedupe_removed" in d
        assert "confidence" in d


# ---------------------------------------------------------------------------
# Tests: Router integration
# ---------------------------------------------------------------------------


class TestRouterConsolidationIntegration:
    """Router consolidation methods work end-to-end."""

    def test_checkpoint_creates_artifact(self, tmp_path):
        router = _make_router(tmp_path)
        result = router.checkpoint(
            "Session completed with 3 tasks done.",
            open_threads=["phase 7 planning"],
            next_actions=["run tests"],
            caller="test",
        )
        assert result["action"] == "checkpoint_created"

    def test_checkpoint_rejects_empty(self, tmp_path):
        router = _make_router(tmp_path)
        result = router.checkpoint("", caller="test")
        assert result["status"] == "rejected"

    def test_consolidate_working_memory(self, tmp_path):
        router = _make_router(tmp_path)
        result = router.consolidate(window_type="working_memory", caller="test")
        assert "status" in result
        assert "input_count" in result
        assert "dedupe_removed" in result

    def test_summarize_session_with_data(self, tmp_path):
        router = _make_router(tmp_path)
        session_data = {
            "session_id": "ses_test",
            "tasks": [
                _task_item(stem="t1", summary="Built feature A."),
                _task_item(stem="t2", summary="Fixed bug B."),
            ],
        }
        result = router.summarize_session("ses_test", session_data=session_data, caller="test")
        assert result["status"] == "summary_created"
        assert result["input_count"] >= 2

    def test_summarize_session_empty(self, tmp_path):
        router = _make_router(tmp_path)
        result = router.summarize_session("ses_empty", session_data={"session_id": "x", "tasks": []})
        assert result["status"] == "rejected_insufficient_evidence"

    def test_extract_patterns_returns_structure(self, tmp_path):
        router = _make_router(tmp_path)
        result = router.extract_patterns(caller="test")
        assert "status" in result
        assert "candidates" in result
        assert isinstance(result["candidates"], list)

    def test_reflect_runs_consolidation_and_patterns(self, tmp_path):
        router = _make_router(tmp_path)
        result = router.reflect(caller="test")
        assert result["status"] == "completed"
        assert "consolidation" in result
        assert "patterns" in result

    def test_generate_diary_delegates_to_summarize(self, tmp_path):
        router = _make_router(tmp_path)
        session_data = {
            "session_id": "ses_diary",
            "tasks": [_task_item(stem="t1", summary="Did work.")],
        }
        result = router.generate_diary("ses_diary", session_data=session_data)
        assert result["status"] == "summary_created"

    def test_consolidation_trace_emitted(self, tmp_path):
        router = _make_router(tmp_path)
        ctx = TraceContext.new("test")
        with patch("agents.memory_router.slog") as mock_slog:
            router.consolidate(window_type="working_memory", caller="test", ctx=ctx)
            consolidate_calls = [c for c in mock_slog.event.call_args_list if c[0][0] == "memory.router.consolidate"]
            assert len(consolidate_calls) >= 1
            kw = consolidate_calls[0][1]
            assert "action" in kw
            assert "input_count" in kw
            assert "dedupe_removed" in kw
            assert "window_type" in kw

    def test_checkpoint_trace_emitted(self, tmp_path):
        router = _make_router(tmp_path)
        ctx = TraceContext.new("test")
        with patch("agents.memory_router.slog") as mock_slog:
            router.checkpoint("Session done.", caller="test", ctx=ctx)
            cp_calls = [c for c in mock_slog.event.call_args_list if c[0][0] == "memory.router.checkpoint"]
            assert len(cp_calls) >= 1

    def test_session_summary_stored_via_router(self, tmp_path):
        """End-to-end: session summary is stored through the router store path."""
        router = _make_router(tmp_path)
        session_data = {
            "session_id": "ses_e2e",
            "tasks": [
                _task_item(stem="t1", summary="Implemented vault validation."),
                _task_item(stem="t2", summary="Wrote comprehensive tests."),
                _task_item(stem="t3", summary="Created documentation."),
            ],
        }
        result = router.summarize_session("ses_e2e", session_data=session_data, caller="test")
        assert result["status"] == "summary_created"
        # The artifact should have been stored (either in working or memory_file)
        assert "stored" in result


class TestConsolidationMissingAdapter:
    """Router.consolidate short-circuits when required adapter is missing."""

    def test_no_state_working_adapter_skips(self, tmp_path):
        """Router without state_working adapter returns skipped_no_adapter."""
        router = MemoryRouter(
            adapters=[MemoryFileAdapter(base=tmp_path)],
            memory_file_base=tmp_path,
        )
        result = router.consolidate(window_type="working_memory", caller="test")
        assert result["status"] == "skipped_no_adapter"
        assert "state_working" in result["reason"]
        assert result["input_count"] == 0

    def test_no_memory_file_adapter_skips(self, tmp_path):
        """Router without memory_file adapter returns skipped_no_adapter for task_batch."""
        router = MemoryRouter(
            adapters=[WorkingMemoryAdapter(base=tmp_path)],
            memory_file_base=tmp_path,
        )
        result = router.consolidate(window_type="task_batch", caller="test")
        assert result["status"] == "skipped_no_adapter"
        assert "memory_file" in result["reason"]

    def test_with_adapter_proceeds_normally(self, tmp_path):
        """Router with correct adapter proceeds to consolidation pipeline."""
        router = _make_router(tmp_path)
        result = router.consolidate(window_type="working_memory", caller="test")
        # Should reach the consolidator (which may reject on empty window)
        # but should NOT be skipped_no_adapter
        assert result["status"] != "skipped_no_adapter"


class TestConsolidationHeartbeatDedup:
    """Working memory with identical heartbeats is correctly rejected."""

    def test_all_identical_heartbeats_rejected(self, tmp_path):
        """Identical heartbeat events dedup to 1 → below minimum → rejected."""
        from agents.memory_consolidator import WindowSpec, consolidate_window

        items = [
            {
                "event_type": "heartbeat_cycle",
                "title": "heartbeat_cycle: HEALTHY",
                "summary": "All systems healthy.",
                "source": "heartbeat",
                "confidence": "high",
            }
            for _ in range(10)
        ]
        spec = WindowSpec(window_type="working_memory", source_items=items, caller="test")
        result = consolidate_window(spec)
        assert result.action == "rejected_insufficient_evidence"
        assert "after_dedupe" in result.reason
        assert result.dedupe_removed == 9

    def test_diverse_working_memory_succeeds(self, tmp_path):
        """Working memory with diverse events consolidates successfully."""
        from agents.memory_consolidator import WindowSpec, consolidate_window

        items = [
            {
                "event_type": "task_completed",
                "title": "Built feature A",
                "summary": "Implemented the vault validation feature.",
                "source": "watcher",
                "confidence": "high",
            },
            {
                "event_type": "decision_made",
                "title": "Chose PostgreSQL",
                "summary": "Selected PostgreSQL for main store due to JSONB support.",
                "source": "watcher",
                "confidence": "medium",
            },
            {
                "event_type": "bug_fixed",
                "title": "Fixed auth race condition",
                "summary": "Resolved race condition in auth middleware token refresh.",
                "source": "watcher",
                "confidence": "high",
            },
        ]
        spec = WindowSpec(window_type="working_memory", source_items=items, caller="test")
        result = consolidate_window(spec)
        assert result.action == "checkpoint_created"
        assert result.input_count == 3
        assert result.output_artifact is not None
