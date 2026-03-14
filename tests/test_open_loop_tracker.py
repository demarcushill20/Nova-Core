"""Tests for agents/open_loop_tracker.py — Phase 7 Open-Loop Tracking.

Covers:
- Loop creation and validation
- State machine transitions
- Duplicate suppression
- Loop update/block/defer/resolve lifecycle
- Invalid closure rejection
- Loop detection from events
- LoopStore persistence
- Active loop recall
- Router track_open_loop() integration
- Router detect_open_loops() integration
- Router resolve_open_loop() integration
- Router get_open_loops() integration
- open_loop_recall intent integration
- Trace output
- Stale loop marking
"""

import time
from pathlib import Path
from unittest.mock import patch

from agents.memory_router import (
    MemoryFileAdapter,
    MemoryRouter,
    RecallResult,
    WorkingMemoryAdapter,
)
from agents.open_loop_tracker import (
    _ALLOWED_TRANSITIONS,
    VALID_LOOP_ACTIONS,
    VALID_LOOP_STATUSES,
    LoopStore,
    OpenLoop,
    create_loop,
    detect_loop_from_event,
    get_active_loops_for_recall,
    mark_stale,
    reject_loop,
    resolve_loop,
    update_loop,
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
        loop_store_base=tmp_path / "open_loops",
    )


def _store(tmp_path: Path) -> LoopStore:
    return LoopStore(base=tmp_path / "open_loops")


def _make_loop(store: LoopStore, **kw) -> OpenLoop:
    """Create a loop via create_loop and return the OpenLoop object."""
    defaults = {
        "title": "Fix auth timeout in module",
        "summary": "Repeated 504 errors need investigation and fix.",
        "source": "watcher",
    }
    defaults.update(kw)
    result = create_loop(**defaults, store=store)
    assert result.action == "loop_created"
    return result.loop


# ---------------------------------------------------------------------------
# Tests: Loop creation and validation
# ---------------------------------------------------------------------------


class TestLoopCreation:
    """Create loops with validation."""

    def test_create_basic_loop(self, tmp_path):
        store = _store(tmp_path)
        result = create_loop(
            title="Fix auth timeout",
            summary="Repeated 504 errors in telegram module.",
            source="watcher",
            store=store,
        )
        assert result.action == "loop_created"
        assert result.loop is not None
        assert result.loop.status == "open"
        assert result.stored is True

    def test_create_blocked_loop(self, tmp_path):
        store = _store(tmp_path)
        result = create_loop(
            title="Waiting for API key",
            summary="Cannot proceed without third-party API credentials.",
            source="operator",
            blocker="Waiting for vendor response",
            store=store,
        )
        assert result.action == "loop_created"
        assert result.loop.status == "blocked"
        assert result.loop.blocker == "Waiting for vendor response"

    def test_reject_short_title(self, tmp_path):
        store = _store(tmp_path)
        result = create_loop(title="Hi", summary="Valid summary here.", source="x", store=store)
        assert result.action == "loop_rejected_insufficient_evidence"
        assert "title_too_short" in result.reason

    def test_reject_short_summary(self, tmp_path):
        store = _store(tmp_path)
        result = create_loop(title="Valid title", summary="No", source="x", store=store)
        assert result.action == "loop_rejected_insufficient_evidence"
        assert "summary_too_short" in result.reason

    def test_loop_has_all_fields(self, tmp_path):
        store = _store(tmp_path)
        result = create_loop(
            title="Test loop with all fields",
            summary="Comprehensive test of field population.",
            source="test",
            project="nova-core",
            confidence="high",
            blocker="none",
            due_hint="end of week",
            owner="nova-agent",
            related_task_ids=["task_1"],
            related_files=["agents/foo.py"],
            tags=["#test"],
            store=store,
        )
        loop = result.loop
        assert loop.loop_id
        assert loop.opened_at
        assert loop.updated_at
        assert loop.project == "nova-core"
        assert loop.confidence == "high"
        assert loop.owner == "nova-agent"
        assert "task_1" in loop.related_task_ids

    def test_loop_to_dict(self, tmp_path):
        store = _store(tmp_path)
        loop = _make_loop(store)
        d = loop.to_dict()
        assert "loop_id" in d
        assert "title" in d
        assert "status" in d
        assert "history" in d

    def test_loop_persisted_to_disk(self, tmp_path):
        store = _store(tmp_path)
        loop = _make_loop(store)
        loaded = store.load(loop.loop_id)
        assert loaded is not None
        assert loaded.loop_id == loop.loop_id
        assert loaded.title == loop.title


# ---------------------------------------------------------------------------
# Tests: State machine transitions
# ---------------------------------------------------------------------------


class TestStateMachine:
    """State transitions follow the allowed transition map."""

    def test_open_to_blocked(self, tmp_path):
        store = _store(tmp_path)
        loop = _make_loop(store)
        result = update_loop(loop, new_status="blocked", reason="waiting on CI", store=store)
        assert result.action == "loop_blocked"
        assert result.new_status == "blocked"

    def test_open_to_deferred(self, tmp_path):
        store = _store(tmp_path)
        loop = _make_loop(store)
        result = update_loop(loop, new_status="deferred", reason="not urgent", store=store)
        assert result.action == "loop_deferred"
        assert result.new_status == "deferred"

    def test_open_to_resolved(self, tmp_path):
        store = _store(tmp_path)
        loop = _make_loop(store)
        result = resolve_loop(loop, closure_reason="Fixed in commit abc123", store=store)
        assert result.action == "loop_resolved"
        assert result.new_status == "resolved"

    def test_blocked_to_open(self, tmp_path):
        store = _store(tmp_path)
        loop = _make_loop(store)
        update_loop(loop, new_status="blocked", reason="waiting", store=store)
        result = update_loop(loop, new_status="open", reason="unblocked", store=store)
        assert result.action == "loop_updated"
        assert result.new_status == "open"

    def test_resolved_is_terminal(self, tmp_path):
        store = _store(tmp_path)
        loop = _make_loop(store)
        resolve_loop(loop, closure_reason="Done and verified", store=store)
        result = update_loop(loop, new_status="open", reason="reopen", store=store)
        assert result.action == "no_loop_action"
        assert "not allowed" in result.reason

    def test_closed_rejected_is_terminal(self, tmp_path):
        store = _store(tmp_path)
        loop = _make_loop(store)
        reject_loop(loop, reason="Not actionable", store=store)
        result = update_loop(loop, new_status="open", reason="reopen", store=store)
        assert result.action == "no_loop_action"

    def test_all_transitions_documented(self):
        """Every valid status has an entry in the transition map."""
        for status in VALID_LOOP_STATUSES:
            assert status in _ALLOWED_TRANSITIONS

    def test_history_tracks_transitions(self, tmp_path):
        store = _store(tmp_path)
        loop = _make_loop(store)
        update_loop(loop, new_status="blocked", reason="CI down", store=store)
        update_loop(loop, new_status="open", reason="CI back", store=store)
        assert len(loop.history) == 3  # created + blocked + unblocked
        assert loop.history[0]["from_status"] == "proposed"
        assert loop.history[1]["to_status"] == "blocked"
        assert loop.history[2]["to_status"] == "open"


# ---------------------------------------------------------------------------
# Tests: Duplicate suppression
# ---------------------------------------------------------------------------


class TestDuplicateSuppression:
    """Same loop is not created twice."""

    def test_duplicate_rejected(self, tmp_path):
        store = _store(tmp_path)
        r1 = create_loop(
            title="Fix auth timeout",
            summary="Repeated errors need fix.",
            source="watcher",
            store=store,
        )
        assert r1.action == "loop_created"

        r2 = create_loop(
            title="Fix auth timeout",
            summary="Same issue different description.",
            source="watcher",
            store=store,
        )
        assert r2.action == "loop_rejected_duplicate"
        assert r1.loop_id in r2.reason

    def test_different_titles_not_duplicates(self, tmp_path):
        store = _store(tmp_path)
        r1 = create_loop(
            title="Fix auth timeout",
            summary="Timeout issue.",
            source="watcher",
            store=store,
        )
        r2 = create_loop(
            title="Fix database connection",
            summary="Connection drops under load.",
            source="watcher",
            store=store,
        )
        assert r1.action == "loop_created"
        assert r2.action == "loop_created"
        assert r1.loop_id != r2.loop_id

    def test_resolved_loop_allows_recreation(self, tmp_path):
        store = _store(tmp_path)
        r1 = create_loop(
            title="Fix auth timeout",
            summary="Timeout issue.",
            source="watcher",
            store=store,
        )
        resolve_loop(r1.loop, closure_reason="Fixed in commit abc", store=store)

        r2 = create_loop(
            title="Fix auth timeout",
            summary="Came back after deploy.",
            source="watcher",
            store=store,
        )
        assert r2.action == "loop_created"


# ---------------------------------------------------------------------------
# Tests: Loop resolution
# ---------------------------------------------------------------------------


class TestLoopResolution:
    """Resolve loops with explicit evidence."""

    def test_resolve_with_reason(self, tmp_path):
        store = _store(tmp_path)
        loop = _make_loop(store)
        result = resolve_loop(
            loop,
            closure_reason="Bug fixed in PR #42",
            closure_evidence="commit abc123",
            store=store,
        )
        assert result.action == "loop_resolved"
        assert loop.closure_reason == "Bug fixed in PR #42"
        assert loop.closure_evidence == "commit abc123"

    def test_resolve_requires_reason(self, tmp_path):
        store = _store(tmp_path)
        loop = _make_loop(store)
        result = resolve_loop(loop, closure_reason="", store=store)
        assert result.action == "no_loop_action"
        assert "closure_reason_required" in result.reason

    def test_resolve_requires_substantive_reason(self, tmp_path):
        store = _store(tmp_path)
        loop = _make_loop(store)
        result = resolve_loop(loop, closure_reason="ok", store=store)
        assert result.action == "no_loop_action"

    def test_reject_loop(self, tmp_path):
        store = _store(tmp_path)
        loop = _make_loop(store)
        reject_loop(loop, reason="Not actionable, already handled", store=store)
        assert loop.status == "closed_rejected"


# ---------------------------------------------------------------------------
# Tests: Loop detection from events
# ---------------------------------------------------------------------------


class TestLoopDetection:
    """Detect open loops from runtime events conservatively."""

    def test_task_failure_creates_loop(self, tmp_path):
        store = _store(tmp_path)
        result = detect_loop_from_event(
            {
                "event_type": "task_failed",
                "title": "task_failed: code_review_task",
                "summary": "Linting failed with 3 violations.",
                "source": "watcher",
                "stem": "code_review_task",
            },
            store=store,
        )
        assert result is not None
        assert result.action == "loop_created"
        assert "failure" in result.loop.tags[0]

    def test_plan_with_next_actions_creates_loop(self, tmp_path):
        store = _store(tmp_path)
        result = detect_loop_from_event(
            {
                "event_type": "plan_created",
                "title": "Plan plan_001 completed",
                "summary": "Plan done but follow-ups remain.",
                "source": "orchestrator",
                "next_actions": ["run integration tests", "update docs"],
            },
            store=store,
        )
        assert result is not None
        assert result.action == "loop_created"
        assert "plan_followup" in result.loop.tags[0]

    def test_session_end_with_threads_creates_loop(self, tmp_path):
        store = _store(tmp_path)
        result = detect_loop_from_event(
            {
                "event_type": "session_end",
                "title": "Session ended",
                "summary": "Session complete with open items.",
                "source": "heartbeat",
                "open_threads": ["finish Phase 7 tests", "review PR #42"],
            },
            store=store,
        )
        assert result is not None
        assert result.action == "loop_created"
        assert "session_continuity" in result.loop.tags[0]

    def test_completed_task_no_loop(self, tmp_path):
        store = _store(tmp_path)
        result = detect_loop_from_event(
            {
                "event_type": "task_completed",
                "title": "task_completed: build",
                "summary": "Build succeeded.",
                "source": "watcher",
            },
            store=store,
        )
        assert result is None

    def test_heartbeat_no_loop(self, tmp_path):
        store = _store(tmp_path)
        result = detect_loop_from_event(
            {
                "event_type": "heartbeat_cycle",
                "title": "heartbeat: HEALTHY",
                "summary": "All checks passed.",
                "source": "heartbeat",
            },
            store=store,
        )
        assert result is None


# ---------------------------------------------------------------------------
# Tests: LoopStore persistence
# ---------------------------------------------------------------------------


class TestLoopStore:
    """File-based loop persistence."""

    def test_save_and_load(self, tmp_path):
        store = _store(tmp_path)
        loop = _make_loop(store)
        loaded = store.load(loop.loop_id)
        assert loaded is not None
        assert loaded.title == loop.title

    def test_load_nonexistent(self, tmp_path):
        store = _store(tmp_path)
        assert store.load("nonexistent") is None

    def test_load_all(self, tmp_path):
        store = _store(tmp_path)
        _make_loop(store, title="Loop one")
        _make_loop(store, title="Loop two")
        all_loops = store.load_all()
        assert len(all_loops) == 2

    def test_get_active_loops(self, tmp_path):
        store = _store(tmp_path)
        l1 = _make_loop(store, title="Active loop")
        l2 = _make_loop(store, title="Resolved loop")
        resolve_loop(l2, closure_reason="Done and verified", store=store)
        active = store.get_active_loops()
        assert len(active) == 1
        assert active[0].loop_id == l1.loop_id

    def test_find_by_dedupe_key(self, tmp_path):
        store = _store(tmp_path)
        loop = _make_loop(store)
        found = store.find_by_dedupe_key(loop.dedupe_key())
        assert found is not None
        assert found.loop_id == loop.loop_id


# ---------------------------------------------------------------------------
# Tests: Stale marking
# ---------------------------------------------------------------------------


class TestStaleMarking:
    """Mark old unupdated loops as stale."""

    def test_recent_loop_not_stale(self, tmp_path):
        store = _store(tmp_path)
        loop = _make_loop(store)
        results = mark_stale([loop], store=store)
        assert len(results) == 0

    def test_old_loop_marked_stale(self, tmp_path):
        store = _store(tmp_path)
        loop = _make_loop(store)
        # Set updated_at to 30 days ago
        old_time = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 30 * 86400))
        loop.updated_at = old_time
        store.save(loop)
        results = mark_stale([loop], store=store)
        assert len(results) == 1
        assert results[0].new_status == "stale"


# ---------------------------------------------------------------------------
# Tests: Active loop recall
# ---------------------------------------------------------------------------


class TestActiveLoopRecall:
    """Get active loops formatted for recall."""

    def test_recall_format(self, tmp_path):
        store = _store(tmp_path)
        _make_loop(store, title="Open issue alpha")
        results = get_active_loops_for_recall(store=store)
        assert len(results) >= 1
        r = results[0]
        assert "loop_id" in r
        assert "title" in r
        assert "status" in r
        assert r["_source_adapter"] == "open_loop_tracker"
        assert r["event_type"] == "open_loop"

    def test_filter_by_project(self, tmp_path):
        store = _store(tmp_path)
        _make_loop(store, title="Nova loop", project="nova-core")
        _make_loop(store, title="Other loop", project="other-project")
        results = get_active_loops_for_recall(store=store, project="nova-core")
        assert all(r["project"] == "nova-core" for r in results)


# ---------------------------------------------------------------------------
# Tests: Router integration
# ---------------------------------------------------------------------------


class TestRouterOpenLoopIntegration:
    """Router open-loop methods work end-to-end."""

    def test_track_open_loop(self, tmp_path):
        router = _make_router(tmp_path)
        result = router.track_open_loop(
            "Fix the auth timeout in telegram module for production stability",
            title="Fix auth timeout",
            source="watcher",
            caller="test",
        )
        assert result["action"] == "loop_created"
        assert result["stored"] is True

    def test_track_open_loop_rejects_short(self, tmp_path):
        router = _make_router(tmp_path)
        result = router.track_open_loop("Hi", title="Hi", source="x", caller="test")
        assert result["action"] == "loop_rejected_insufficient_evidence"

    def test_detect_open_loops_from_failure(self, tmp_path):
        router = _make_router(tmp_path)
        result = router.detect_open_loops(
            {
                "event_type": "task_failed",
                "title": "task_failed: deploy",
                "summary": "Deployment script crashed on staging server.",
                "source": "watcher",
                "stem": "deploy",
            },
            caller="test",
        )
        assert result["action"] == "loop_created"

    def test_detect_no_loop_from_success(self, tmp_path):
        router = _make_router(tmp_path)
        result = router.detect_open_loops(
            {
                "event_type": "task_completed",
                "title": "task_completed: build",
                "summary": "Build succeeded with all tests passing.",
                "source": "watcher",
            },
            caller="test",
        )
        assert result["status"] == "no_loop_detected"

    def test_resolve_open_loop(self, tmp_path):
        router = _make_router(tmp_path)
        # Create a loop first
        create_result = router.track_open_loop(
            "Fix database connection pooling for production workloads",
            title="Fix db pool",
            source="watcher",
            caller="test",
        )
        loop_id = create_result["loop_id"]

        # Resolve it
        resolve_result = router.resolve_open_loop(
            loop_id,
            closure_reason="Fixed in commit def456 with connection pool tuning",
            closure_evidence="commit def456",
            caller="test",
        )
        assert resolve_result["action"] == "loop_resolved"

    def test_resolve_nonexistent_loop(self, tmp_path):
        router = _make_router(tmp_path)
        result = router.resolve_open_loop(
            "nonexistent-id",
            closure_reason="test closure",
            caller="test",
        )
        assert result["action"] == "no_loop_action"

    def test_get_open_loops(self, tmp_path):
        router = _make_router(tmp_path)
        router.track_open_loop(
            "Investigate memory leak in worker process during long sessions",
            title="Memory leak investigation",
            source="watcher",
            caller="test",
        )
        result = router.get_open_loops(caller="test")
        assert result["status"] == "ok"
        assert result["count"] >= 1
        assert len(result["loops"]) >= 1

    def test_open_loop_recall_includes_tracked_loops(self, tmp_path):
        """open_loop_recall intent includes real tracked loops."""
        router = _make_router(tmp_path)
        # Create a loop
        router.track_open_loop(
            "Complete Phase 7 implementation and testing for open loop tracking",
            title="Phase 7 completion",
            source="watcher",
            caller="test",
        )
        # Recall with open_loop_recall intent
        result = router.recall(
            "what tasks are pending?",
            intent="open_loop_recall",
            caller="test",
        )
        assert isinstance(result, RecallResult)
        assert "open_loop_tracker" in result.sources_queried
        # Should have at least the tracked loop
        loop_results = [r for r in result.results if r.get("_source_adapter") == "open_loop_tracker"]
        assert len(loop_results) >= 1

    def test_track_open_loop_trace(self, tmp_path):
        router = _make_router(tmp_path)
        ctx = TraceContext.new("test")
        with patch("agents.memory_router.slog") as mock_slog:
            router.track_open_loop(
                "Test loop for tracing verification purposes only",
                title="Trace test loop",
                source="test",
                caller="test",
                ctx=ctx,
            )
            track_calls = [c for c in mock_slog.event.call_args_list if c[0][0] == "memory.router.track_open_loop"]
            assert len(track_calls) >= 1
            kw = track_calls[0][1]
            assert "loop_action" in kw
            assert "loop_id" in kw
            assert "new_status" in kw

    def test_detect_open_loops_trace(self, tmp_path):
        router = _make_router(tmp_path)
        ctx = TraceContext.new("test")
        with patch("agents.memory_router.slog") as mock_slog:
            router.detect_open_loops(
                {
                    "event_type": "task_failed",
                    "title": "task_failed: lint_check",
                    "summary": "Linting failed with ruff violations on 3 files.",
                    "source": "watcher",
                },
                caller="test",
                ctx=ctx,
            )
            detect_calls = [c for c in mock_slog.event.call_args_list if c[0][0] == "memory.router.detect_open_loop"]
            assert len(detect_calls) >= 1


# ---------------------------------------------------------------------------
# Tests: LoopActionResult structure
# ---------------------------------------------------------------------------


class TestLoopActionResultStructure:
    """LoopActionResult has all required fields."""

    def test_to_dict(self, tmp_path):
        store = _store(tmp_path)
        result = create_loop(
            title="Test result structure",
            summary="Verify all fields present.",
            source="test",
            store=store,
        )
        d = result.to_dict()
        assert "action" in d
        assert "reason" in d
        assert "loop_id" in d
        assert "stored" in d
        assert "loop" in d

    def test_all_actions_valid(self):
        for action in VALID_LOOP_ACTIONS:
            assert isinstance(action, str)
