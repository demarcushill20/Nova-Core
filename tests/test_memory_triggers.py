"""Tests for agents/memory_triggers.py — Phase 3 Automatic Memory Triggers.

Covers:
- Trigger eligibility (class validation, event_type matching)
- Content quality gates (min title/summary length)
- Dedupe / cooldown / rate limit suppression
- Event-to-candidate translation via router
- Layer assignment from triggered events
- Router integration for triggered memory writes
- Structured trace emission
- TriggerResult correctness
- DedupeTracker isolation
"""

from pathlib import Path
from unittest.mock import patch

from agents.memory_router import (
    MemoryFileAdapter,
    MemoryRouter,
    WorkingMemoryAdapter,
)
from agents.memory_triggers import (
    TRIGGER_CLASS_EVENT_TYPES,
    VALID_TRIGGER_CLASSES,
    DedupeTracker,
    TriggerEngine,
    TriggerPolicy,
    TriggerResult,
    trigger_engine,
)
from utils.trace_context import TraceContext

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_engine(tmp_path: Path, policy: TriggerPolicy | None = None) -> TriggerEngine:
    """Build a trigger engine with real routers pointing to tmp_path."""
    router = MemoryRouter(
        adapters=[
            MemoryFileAdapter(base=tmp_path),
            WorkingMemoryAdapter(base=tmp_path),
        ],
        memory_file_base=tmp_path,
    )
    (tmp_path / "workflow_learnings").mkdir(parents=True, exist_ok=True)
    return TriggerEngine(
        policy=policy
        or TriggerPolicy(
            dedupe_window_s=5,
            cooldown_window_s=1,
            rate_limit_window_s=60,
            rate_limit_max=100,
        ),
        router=router,
    )


def _fire_task_completed(engine: TriggerEngine, stem: str = "0200_test", **kw):
    """Helper to fire a standard task_completed trigger."""
    defaults = dict(
        trigger_class="task_lifecycle",
        event_type="task_completed",
        source="watcher",
        title=f"task_completed: {stem}",
        summary=f"Task {stem} completed successfully with all tests passing.",
        caller="test",
        task_stem=stem,
    )
    defaults.update(kw)
    return engine.fire(**defaults)


# ---------------------------------------------------------------------------
# Tests: Trigger eligibility
# ---------------------------------------------------------------------------


class TestTriggerEligibility:
    """Trigger engine validates class and event_type before firing."""

    def test_valid_trigger_fires(self, tmp_path):
        engine = _make_engine(tmp_path)
        result = _fire_task_completed(engine)
        assert result.fired is True
        assert result.trigger_class == "task_lifecycle"
        assert result.event_type == "task_completed"

    def test_invalid_trigger_class_rejected(self, tmp_path):
        engine = _make_engine(tmp_path)
        result = engine.fire(
            trigger_class="made_up_class",
            event_type="task_completed",
            source="watcher",
            title="Test title here",
            summary="Test summary here.",
        )
        assert result.fired is False
        assert "invalid_trigger_class" in result.rejection_reason

    def test_event_type_mismatch_rejected(self, tmp_path):
        engine = _make_engine(tmp_path)
        result = engine.fire(
            trigger_class="task_lifecycle",
            event_type="session_end",  # Wrong class for task_lifecycle
            source="watcher",
            title="Test title here",
            summary="Test summary here.",
        )
        assert result.fired is False
        assert "event_type_mismatch" in result.rejection_reason

    def test_all_trigger_classes_have_event_types(self):
        """Every valid trigger class has at least one allowed event type."""
        for cls in VALID_TRIGGER_CLASSES:
            assert cls in TRIGGER_CLASS_EVENT_TYPES
            assert len(TRIGGER_CLASS_EVENT_TYPES[cls]) > 0

    def test_task_failed_trigger_fires(self, tmp_path):
        engine = _make_engine(tmp_path)
        result = engine.fire(
            trigger_class="task_lifecycle",
            event_type="task_failed",
            source="watcher",
            title="task_failed: 0300_broken",
            summary="Task 0300_broken failed with exit code 1.",
            task_stem="0300_broken",
        )
        assert result.fired is True
        assert result.event_type == "task_failed"

    def test_plan_lifecycle_trigger_fires(self, tmp_path):
        engine = _make_engine(tmp_path)
        result = engine.fire(
            trigger_class="plan_lifecycle",
            event_type="plan_created",
            source="orchestrator",
            title="plan_created: plan_001 (done)",
            summary="Plan plan_001 completed with grade A, 3 steps.",
        )
        assert result.fired is True

    def test_session_boundary_trigger_fires(self, tmp_path):
        engine = _make_engine(tmp_path)
        result = engine.fire(
            trigger_class="session_boundary",
            event_type="heartbeat_cycle",
            source="heartbeat",
            title="heartbeat_cycle: HEALTHY",
            summary="Heartbeat: 5 checks, 0 failed. All systems nominal.",
        )
        assert result.fired is True


# ---------------------------------------------------------------------------
# Tests: Content quality gates
# ---------------------------------------------------------------------------


class TestContentQualityGates:
    """Trigger engine rejects low-quality content."""

    def test_short_title_rejected(self, tmp_path):
        engine = _make_engine(tmp_path)
        result = engine.fire(
            trigger_class="task_lifecycle",
            event_type="task_completed",
            source="watcher",
            title="Hi",  # Too short
            summary="This is a valid summary with enough content.",
        )
        assert result.fired is False
        assert "title_too_short" in result.rejection_reason

    def test_short_summary_rejected(self, tmp_path):
        engine = _make_engine(tmp_path)
        result = engine.fire(
            trigger_class="task_lifecycle",
            event_type="task_completed",
            source="watcher",
            title="Valid title here",
            summary="Short",  # Too short
        )
        assert result.fired is False
        assert "summary_too_short" in result.rejection_reason

    def test_empty_title_rejected(self, tmp_path):
        engine = _make_engine(tmp_path)
        result = engine.fire(
            trigger_class="task_lifecycle",
            event_type="task_completed",
            source="watcher",
            title="",
            summary="Valid summary content here.",
        )
        assert result.fired is False
        assert "title_too_short" in result.rejection_reason


# ---------------------------------------------------------------------------
# Tests: Dedupe / cooldown / rate limit
# ---------------------------------------------------------------------------


class TestDedupeTracker:
    """DedupeTracker prevents duplicate and noisy trigger creation."""

    def test_identical_event_suppressed(self):
        tracker = DedupeTracker(TriggerPolicy(dedupe_window_s=10))
        ok1, _ = tracker.check_and_record("task_lifecycle", "watcher", "task_completed", "Test", "Summary here")
        assert ok1 is True

        ok2, reason = tracker.check_and_record("task_lifecycle", "watcher", "task_completed", "Test", "Summary here")
        assert ok2 is False
        assert "content_dedupe" in reason

    def test_different_content_allowed(self):
        tracker = DedupeTracker(
            TriggerPolicy(
                dedupe_window_s=10,
                cooldown_window_s=0,  # No cooldown, testing content dedupe only
            )
        )
        ok1, _ = tracker.check_and_record("task_lifecycle", "watcher", "task_completed", "Task A done", "Summary A")
        assert ok1 is True

        ok2, _ = tracker.check_and_record("task_lifecycle", "watcher", "task_completed", "Task B done", "Summary B")
        assert ok2 is True

    def test_cooldown_suppresses_same_class_source(self):
        tracker = DedupeTracker(
            TriggerPolicy(
                cooldown_window_s=10,
                dedupe_window_s=0,  # Disable content dedupe
            )
        )
        ok1, _ = tracker.check_and_record("task_lifecycle", "watcher", "task_completed", "Task A done", "Summary A")
        assert ok1 is True

        ok2, reason = tracker.check_and_record(
            "task_lifecycle", "watcher", "task_completed", "Task B done", "Summary B"
        )
        assert ok2 is False
        assert "cooldown" in reason

    def test_different_source_bypasses_cooldown(self):
        tracker = DedupeTracker(
            TriggerPolicy(
                cooldown_window_s=10,
                dedupe_window_s=0,
            )
        )
        ok1, _ = tracker.check_and_record("task_lifecycle", "watcher", "task_completed", "Task A done", "Summary A")
        ok2, _ = tracker.check_and_record(
            "task_lifecycle", "orchestrator", "task_completed", "Task B done", "Summary B"
        )
        # Different source, so cooldown doesn't apply
        assert ok1 is True
        assert ok2 is True

    def test_rate_limit_enforced(self):
        tracker = DedupeTracker(
            TriggerPolicy(
                rate_limit_max=3,
                rate_limit_window_s=3600,
                cooldown_window_s=0,
                dedupe_window_s=0,
            )
        )
        for i in range(3):
            ok, _ = tracker.check_and_record(
                "task_lifecycle",
                f"src_{i}",
                "task_completed",
                f"Task {i}",
                f"Summary for task {i}",
            )
            assert ok is True

        ok, reason = tracker.check_and_record(
            "task_lifecycle",
            "src_extra",
            "task_completed",
            "Task extra",
            "Summary extra",
        )
        assert ok is False
        assert "rate_limit" in reason

    def test_reset_clears_state(self):
        tracker = DedupeTracker(TriggerPolicy(dedupe_window_s=10))
        tracker.check_and_record("task_lifecycle", "watcher", "task_completed", "Test", "Summary")
        tracker.reset()
        ok, _ = tracker.check_and_record("task_lifecycle", "watcher", "task_completed", "Test", "Summary")
        assert ok is True

    def test_hash_is_case_insensitive(self):
        """Content hash normalizes to lowercase."""
        tracker = DedupeTracker(TriggerPolicy(dedupe_window_s=10))
        ok1, _ = tracker.check_and_record(
            "task_lifecycle",
            "watcher",
            "task_completed",
            "Task DONE",
            "This Is A Summary",
        )
        assert ok1 is True

        ok2, reason = tracker.check_and_record(
            "task_lifecycle",
            "watcher",
            "task_completed",
            "task done",
            "this is a summary",
        )
        assert ok2 is False
        assert "content_dedupe" in reason


# ---------------------------------------------------------------------------
# Tests: Engine-level dedupe integration
# ---------------------------------------------------------------------------


class TestEngineDedupe:
    """TriggerEngine respects dedupe at the fire() level."""

    def test_duplicate_fire_suppressed(self, tmp_path):
        engine = _make_engine(
            tmp_path,
            TriggerPolicy(
                dedupe_window_s=10,
                cooldown_window_s=0,
            ),
        )
        r1 = _fire_task_completed(engine, stem="0200_test")
        assert r1.fired is True

        r2 = _fire_task_completed(engine, stem="0200_test")
        assert r2.fired is False
        assert r2.suppressed is True
        assert "content_dedupe" in r2.suppression_reason

    def test_different_tasks_both_fire(self, tmp_path):
        engine = _make_engine(
            tmp_path,
            TriggerPolicy(
                dedupe_window_s=10,
                cooldown_window_s=0,
            ),
        )
        r1 = _fire_task_completed(engine, stem="0200_first")
        r2 = _fire_task_completed(engine, stem="0201_second")
        assert r1.fired is True
        assert r2.fired is True


# ---------------------------------------------------------------------------
# Tests: Layer assignment
# ---------------------------------------------------------------------------


class TestLayerAssignment:
    """Triggered events get correct initial layer assignment."""

    def test_task_completed_gets_episodic(self, tmp_path):
        engine = _make_engine(tmp_path)
        result = _fire_task_completed(engine)
        assert result.assigned_layer == "episodic"

    def test_task_failed_gets_episodic(self, tmp_path):
        engine = _make_engine(tmp_path)
        result = engine.fire(
            trigger_class="task_lifecycle",
            event_type="task_failed",
            source="watcher",
            title="task_failed: broken_task",
            summary="Task broken_task failed with exit code 1.",
        )
        assert result.assigned_layer == "episodic"

    def test_heartbeat_cycle_gets_working(self, tmp_path):
        engine = _make_engine(tmp_path)
        result = engine.fire(
            trigger_class="session_boundary",
            event_type="heartbeat_cycle",
            source="heartbeat",
            title="heartbeat_cycle: HEALTHY",
            summary="Heartbeat: 5 checks, 0 failed.",
        )
        assert result.assigned_layer == "working"

    def test_plan_created_gets_episodic(self, tmp_path):
        engine = _make_engine(tmp_path)
        result = engine.fire(
            trigger_class="plan_lifecycle",
            event_type="plan_created",
            source="orchestrator",
            title="plan_created: plan_xyz (done)",
            summary="Plan plan_xyz completed with grade B.",
        )
        assert result.assigned_layer == "episodic"


# ---------------------------------------------------------------------------
# Tests: Router integration
# ---------------------------------------------------------------------------


class TestRouterIntegration:
    """Triggered events flow through the memory router correctly."""

    def test_trigger_creates_memory_artifact(self, tmp_path):
        engine = _make_engine(tmp_path)
        result = _fire_task_completed(engine)
        assert result.fired is True
        # Task completed → episodic → memory_file adapter should accept
        assert result.store_used == "memory_file"
        assert result.stored is True
        assert result.path_or_id  # Should have a file path

    def test_trigger_result_has_memory_id(self, tmp_path):
        engine = _make_engine(tmp_path)
        result = _fire_task_completed(engine)
        assert result.memory_id
        assert result.memory_id.startswith("cm-")

    def test_trigger_result_has_trace_id(self, tmp_path):
        engine = _make_engine(tmp_path)
        ctx = TraceContext.new("test")
        result = _fire_task_completed(engine, ctx=ctx)
        assert result.trace_id == ctx.trace_id

    def test_working_layer_trigger_persists_to_state_working(self, tmp_path):
        """Working-layer triggers (heartbeat) persist via state_working adapter."""
        engine = _make_engine(tmp_path)
        result = engine.fire(
            trigger_class="session_boundary",
            event_type="heartbeat_cycle",
            source="heartbeat",
            title="heartbeat_cycle: HEALTHY",
            summary="Heartbeat: 5 checks, 0 failed.",
        )
        assert result.fired is True
        assert result.stored is True
        assert result.store_used == "state_working"
        assert result.assigned_layer == "working"


# ---------------------------------------------------------------------------
# Tests: Structured trace emission
# ---------------------------------------------------------------------------


class TestTriggerTracing:
    """Trigger operations emit structured log events."""

    def test_fire_emits_trace(self, tmp_path):
        engine = _make_engine(tmp_path)
        with patch("agents.memory_triggers.slog") as mock_slog:
            _fire_task_completed(engine)
            # Should have at least one memory.trigger.fired event
            fired_calls = [c for c in mock_slog.event.call_args_list if c[0][0] == "memory.trigger.fired"]
            assert len(fired_calls) >= 1
            call_kwargs = fired_calls[0][1]
            assert call_kwargs["trigger_class"] == "task_lifecycle"
            assert call_kwargs["memory_event_type"] == "task_completed"
            assert call_kwargs["assigned_layer"] == "episodic"

    def test_rejection_emits_trace(self, tmp_path):
        engine = _make_engine(tmp_path)
        with patch("agents.memory_triggers.slog") as mock_slog:
            engine.fire(
                trigger_class="invalid_class",
                event_type="task_completed",
                source="watcher",
                title="Valid title",
                summary="Valid summary.",
            )
            rejected_calls = [c for c in mock_slog.event.call_args_list if c[0][0] == "memory.trigger.rejected"]
            assert len(rejected_calls) >= 1

    def test_suppression_emits_trace(self, tmp_path):
        engine = _make_engine(tmp_path, TriggerPolicy(dedupe_window_s=10))
        with patch("agents.memory_triggers.slog") as mock_slog:
            _fire_task_completed(engine, stem="0200_dup")
            _fire_task_completed(engine, stem="0200_dup")
            suppressed_calls = [c for c in mock_slog.event.call_args_list if c[0][0] == "memory.trigger.suppressed"]
            assert len(suppressed_calls) >= 1
            assert "content_dedupe" in suppressed_calls[0][1]["suppression_reason"]


# ---------------------------------------------------------------------------
# Tests: TriggerResult structure
# ---------------------------------------------------------------------------


class TestTriggerResultStructure:
    """TriggerResult has all required fields."""

    def test_successful_result_fields(self, tmp_path):
        engine = _make_engine(tmp_path)
        result = _fire_task_completed(engine)
        assert isinstance(result, TriggerResult)
        d = result.to_dict()
        assert "fired" in d
        assert "stored" in d
        assert "suppressed" in d
        assert "trigger_class" in d
        assert "event_type" in d
        assert "assigned_layer" in d
        assert "trace_id" in d
        assert "memory_id" in d

    def test_rejected_result_fields(self, tmp_path):
        engine = _make_engine(tmp_path)
        result = engine.fire(
            trigger_class="bad",
            event_type="bad",
            source="test",
            title="Title",
            summary="Summary.",
        )
        assert result.fired is False
        assert result.rejection_reason

    def test_suppressed_result_fields(self, tmp_path):
        engine = _make_engine(tmp_path, TriggerPolicy(dedupe_window_s=60))
        _fire_task_completed(engine, stem="dup")
        result = _fire_task_completed(engine, stem="dup")
        assert result.suppressed is True
        assert result.suppression_reason


# ---------------------------------------------------------------------------
# Tests: Singleton
# ---------------------------------------------------------------------------


class TestSingleton:
    """Module-level trigger_engine singleton exists."""

    def test_singleton_exists(self):
        assert trigger_engine is not None
        assert isinstance(trigger_engine, TriggerEngine)

    def test_singleton_has_router(self):
        # Accessing .router should lazy-import the module-level router
        assert trigger_engine.router is not None


# ---------------------------------------------------------------------------
# Tests: Open-loop automation (Phase B Priority 1)
# ---------------------------------------------------------------------------


class TestOpenLoopAutomation:
    """Verify that trigger engine automatically creates open loops from
    failure/continuity events and that watcher resolves them on completion."""

    def test_task_failed_creates_open_loop(self, tmp_path):
        """task_failed trigger should auto-detect and create an open loop."""
        engine = _make_engine(tmp_path)
        # Set up loop store directory
        loop_dir = tmp_path / "open_loops"
        loop_dir.mkdir(parents=True, exist_ok=True)
        engine.router._loop_store_base = loop_dir

        result = engine.fire(
            trigger_class="task_lifecycle",
            event_type="task_failed",
            source="watcher",
            title="task_failed: 0300_broken_task",
            summary="Task 0300_broken_task failed due to missing artifacts.",
            caller="test",
            task_stem="0300_broken_task",
        )
        assert result.fired is True

        # Verify a loop was created
        from agents.open_loop_tracker import LoopStore

        store = LoopStore(base=loop_dir)
        active = store.get_active_loops()
        assert len(active) >= 1
        loop = active[0]
        assert "0300_broken_task" in loop.related_task_ids
        assert loop.status in ("open", "blocked", "proposed")

    def test_task_completed_does_not_create_loop(self, tmp_path):
        """task_completed should NOT create a loop."""
        engine = _make_engine(tmp_path)
        loop_dir = tmp_path / "open_loops"
        loop_dir.mkdir(parents=True, exist_ok=True)
        engine.router._loop_store_base = loop_dir

        result = _fire_task_completed(engine)
        assert result.fired is True

        from agents.open_loop_tracker import LoopStore

        store = LoopStore(base=loop_dir)
        assert len(store.get_active_loops()) == 0

    def test_session_end_with_open_threads_creates_loop(self, tmp_path):
        """session_end with open_threads should create a loop."""
        engine = _make_engine(tmp_path)
        loop_dir = tmp_path / "open_loops"
        loop_dir.mkdir(parents=True, exist_ok=True)
        engine.router._loop_store_base = loop_dir

        result = engine.fire(
            trigger_class="session_boundary",
            event_type="session_end",
            source="heartbeat",
            title="Session end with pending work",
            summary="Session ended with 2 open threads remaining.",
            caller="test",
            extra={"open_threads": ["finish auth migration", "review PR #42"]},
        )
        assert result.fired is True

        from agents.open_loop_tracker import LoopStore

        store = LoopStore(base=loop_dir)
        active = store.get_active_loops()
        assert len(active) >= 1
        assert "#loop/session_continuity" in active[0].tags

    def test_loop_detection_failure_is_non_fatal(self, tmp_path):
        """If detect_open_loops raises, trigger still succeeds."""
        engine = _make_engine(tmp_path)
        # Don't set loop_store_base — detect will use default which may not exist
        # but the try/except should catch any error

        with patch.object(engine.router, "detect_open_loops", side_effect=RuntimeError("boom")):
            result = engine.fire(
                trigger_class="task_lifecycle",
                event_type="task_failed",
                source="watcher",
                title="task_failed: 0400_crasher",
                summary="Task failed but loop detection should not block trigger.",
                caller="test",
                task_stem="0400_crasher",
            )
        # Trigger still fired despite loop detection failure
        assert result.fired is True

    def test_end_to_end_loop_lifecycle(self, tmp_path):
        """Full lifecycle: task_failed creates loop, task_completed resolves it."""
        engine = _make_engine(tmp_path)
        loop_dir = tmp_path / "open_loops"
        loop_dir.mkdir(parents=True, exist_ok=True)
        engine.router._loop_store_base = loop_dir

        # Step 1: Fire task_failed → should create loop
        engine.fire(
            trigger_class="task_lifecycle",
            event_type="task_failed",
            source="watcher",
            title="task_failed: 0500_flaky",
            summary="Task 0500_flaky failed with import error.",
            caller="test",
            task_stem="0500_flaky",
        )

        from agents.open_loop_tracker import LoopStore

        store = LoopStore(base=loop_dir)
        active = store.get_active_loops()
        assert len(active) == 1
        loop_id = active[0].loop_id
        assert "0500_flaky" in active[0].related_task_ids

        # Step 2: Resolve via router (simulating watcher completion path)
        resolve_result = engine.router.resolve_open_loop(
            loop_id,
            closure_reason="Task 0500_flaky completed successfully",
            closure_evidence="0500_flaky",
            caller="watcher.dispatch",
        )
        assert resolve_result["action"] == "loop_resolved"

        # Step 3: Verify loop is no longer active
        active_after = store.get_active_loops()
        assert len(active_after) == 0

        # Step 4: Verify resolved loop still exists with correct status
        resolved = store.load(loop_id)
        assert resolved is not None
        assert resolved.status == "resolved"
        assert "0500_flaky" in resolved.closure_evidence

    def test_plan_failure_with_next_actions_creates_loop(self, tmp_path):
        """plan_revised with next_actions (from failed plan) creates a loop."""
        engine = _make_engine(tmp_path)
        loop_dir = tmp_path / "open_loops"
        loop_dir.mkdir(parents=True, exist_ok=True)
        engine.router._loop_store_base = loop_dir

        result = engine.fire(
            trigger_class="plan_lifecycle",
            event_type="plan_revised",
            source="orchestrator",
            title="plan_revised: plan p-001 (failed)",
            summary="Plan p-001 for task 0600_deploy failed at step 2.",
            caller="orchestrator.run_plan",
            task_stem="0600_deploy",
            extra={
                "plan_status": "failed",
                "next_actions": ["Resolve: step 2 contract violation"],
            },
        )
        assert result.fired is True

        from agents.open_loop_tracker import LoopStore

        store = LoopStore(base=loop_dir)
        active = store.get_active_loops()
        assert len(active) >= 1
        loop = active[0]
        assert "#loop/plan_followup" in loop.tags
        assert "0600_deploy" in loop.related_task_ids

    def test_plan_failure_without_next_actions_creates_loop(self, tmp_path):
        """plan_revised with plan_status=failed creates a loop even without next_actions."""
        engine = _make_engine(tmp_path)
        loop_dir = tmp_path / "open_loops"
        loop_dir.mkdir(parents=True, exist_ok=True)
        engine.router._loop_store_base = loop_dir

        result = engine.fire(
            trigger_class="plan_lifecycle",
            event_type="plan_revised",
            source="orchestrator",
            title="plan_revised: plan p-002 (failed)",
            summary="Plan p-002 failed due to supervisor abort.",
            caller="orchestrator.run_plan",
            task_stem="0700_migrate",
            extra={"plan_status": "failed"},
        )
        assert result.fired is True

        from agents.open_loop_tracker import LoopStore

        store = LoopStore(base=loop_dir)
        active = store.get_active_loops()
        assert len(active) >= 1
        loop = active[0]
        assert "#loop/plan_failure" in loop.tags
        assert "0700_migrate" in loop.related_task_ids

    def test_successful_plan_does_not_create_loop(self, tmp_path):
        """plan_created (successful) should NOT create a loop."""
        engine = _make_engine(tmp_path)
        loop_dir = tmp_path / "open_loops"
        loop_dir.mkdir(parents=True, exist_ok=True)
        engine.router._loop_store_base = loop_dir

        result = engine.fire(
            trigger_class="plan_lifecycle",
            event_type="plan_created",
            source="orchestrator",
            title="plan_created: plan p-003 (done)",
            summary="Plan p-003 completed all steps successfully.",
            caller="orchestrator.run_plan",
            extra={"plan_status": "done"},
        )
        assert result.fired is True

        from agents.open_loop_tracker import LoopStore

        store = LoopStore(base=loop_dir)
        assert len(store.get_active_loops()) == 0

    def test_duplicate_plan_failures_deduplicated(self, tmp_path):
        """Same plan failure fired twice creates only one loop."""
        engine = _make_engine(tmp_path)
        loop_dir = tmp_path / "open_loops"
        loop_dir.mkdir(parents=True, exist_ok=True)
        engine.router._loop_store_base = loop_dir

        for _ in range(2):
            engine.fire(
                trigger_class="plan_lifecycle",
                event_type="plan_revised",
                source="orchestrator",
                title="plan_revised: plan p-004 (failed)",
                summary="Plan p-004 failed due to timeout.",
                caller="orchestrator.run_plan",
                task_stem="0800_retry",
                extra={"plan_status": "failed"},
            )

        from agents.open_loop_tracker import LoopStore

        store = LoopStore(base=loop_dir)
        active = store.get_active_loops()
        # Only 1 loop despite 2 fires (dedupe by title hash)
        assert len(active) == 1
