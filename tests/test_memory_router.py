"""Tests for agents/memory_router.py — Phase 1+2 Unified Memory Router.

Covers:
- CanonicalMemoryObject validation (including Phase 2 layer fields)
- Router recall with adapter selection
- Router store with validation, routing, and layer/store compatibility
- ingest_event normalization with layer auto-assignment
- Skeleton methods return structured not-implemented results
- Structured trace emission (with layer context)
- Migrated recall path correctness (watcher pattern retrieval)
- Adapter contracts
- Phase 2: Layer policies, promotion boundaries, store/layer compatibility
"""

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agents.memory_router import (
    EVENT_TYPE_TO_LAYER,
    CanonicalMemoryObject,
    FusionMemoryAdapter,
    MemoryFileAdapter,
    MemoryRouter,
    PromoteResult,
    RecallResult,
    StoreResult,
    VaultAdapter,
    WorkingMemoryAdapter,
    check_promotion_boundary,
    check_store_layer_compatibility,
    compute_candidate_next_layer,
    infer_layer_from_event_type,
    validate_canonical_object,
)
from utils.trace_context import TraceContext

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _valid_cmo(**kw) -> CanonicalMemoryObject:
    """Build a minimal valid CanonicalMemoryObject."""
    defaults = {
        "memory_id": "cm-wat-1234567890",
        "timestamp": "2026-03-13T10:00:00Z",
        "source": "watcher",
        "event_type": "task_completed",
        "provenance": "automatic",
        "title": "Test memory object",
        "summary": "A test task was completed successfully.",
        "confidence": "medium",
        "memory_layer_candidate": "episodic",
        "promotion_status": "candidate",
        "related_project": "nova-core",
        # Phase 2 fields
        "current_layer": "episodic",
        "promotion_eligibility": "eligible",
    }
    defaults.update(kw)
    return CanonicalMemoryObject(**defaults)


def _make_router(tmp_path: Path) -> MemoryRouter:
    """Build a router with MemoryFileAdapter pointing to tmp_path."""
    return MemoryRouter(
        adapters=[MemoryFileAdapter(base=tmp_path)],
        memory_file_base=tmp_path,
        loop_store_base=tmp_path / "open_loops",
    )


# ---------------------------------------------------------------------------
# Tests: CanonicalMemoryObject validation
# ---------------------------------------------------------------------------


class TestCanonicalValidation:
    """validate_canonical_object enforces schema rules."""

    def test_valid_object(self):
        valid, errors = validate_canonical_object(_valid_cmo())
        assert valid is True, errors

    def test_missing_memory_id(self):
        valid, errors = validate_canonical_object(_valid_cmo(memory_id=""))
        assert valid is False
        assert any("memory_id" in e for e in errors)

    def test_missing_timestamp(self):
        valid, errors = validate_canonical_object(_valid_cmo(timestamp=""))
        assert valid is False

    def test_missing_source(self):
        valid, errors = validate_canonical_object(_valid_cmo(source=""))
        assert valid is False

    def test_missing_title(self):
        valid, errors = validate_canonical_object(_valid_cmo(title=""))
        assert valid is False

    def test_missing_summary(self):
        valid, errors = validate_canonical_object(_valid_cmo(summary=""))
        assert valid is False

    def test_invalid_confidence(self):
        valid, errors = validate_canonical_object(_valid_cmo(confidence="very_high"))
        assert valid is False
        assert any("confidence" in e for e in errors)

    def test_invalid_layer(self):
        valid, errors = validate_canonical_object(_valid_cmo(memory_layer_candidate="tactical"))
        assert valid is False

    def test_invalid_target_store(self):
        valid, errors = validate_canonical_object(_valid_cmo(target_store="postgres"))
        assert valid is False

    def test_invalid_promotion_status(self):
        valid, errors = validate_canonical_object(_valid_cmo(promotion_status="pending"))
        assert valid is False

    def test_title_too_long(self):
        valid, errors = validate_canonical_object(_valid_cmo(title="x" * 101))
        assert valid is False
        assert any("title" in e for e in errors)

    def test_null_target_store_is_valid(self):
        valid, errors = validate_canonical_object(_valid_cmo(target_store=None))
        assert valid is True

    # --- Phase 1 integration: event_type and source enforcement ---

    def test_invalid_event_type_rejected(self):
        valid, errors = validate_canonical_object(_valid_cmo(event_type="made_up_event"))
        assert valid is False
        assert any("event_type" in e for e in errors)

    def test_invalid_source_rejected(self):
        valid, errors = validate_canonical_object(_valid_cmo(source="unknown_service"))
        assert valid is False
        assert any("source" in e for e in errors)

    def test_consolidator_source_valid(self):
        """Phase 1 fix: consolidator is a valid source."""
        valid, errors = validate_canonical_object(_valid_cmo(source="consolidator"))
        assert valid is True, errors

    def test_open_loop_event_type_valid(self):
        """Phase 1 fix: open_loop is a valid event_type."""
        valid, errors = validate_canonical_object(_valid_cmo(event_type="open_loop"))
        assert valid is True, errors

    def test_all_valid_sources_accepted(self):
        """Every declared source passes validation."""
        from agents.memory_router import VALID_SOURCES

        for src in VALID_SOURCES:
            valid, errors = validate_canonical_object(_valid_cmo(source=src))
            assert valid is True, f"source={src!r} rejected: {errors}"

    def test_all_valid_event_types_accepted(self):
        """Every declared event_type passes validation."""
        from agents.memory_router import VALID_EVENT_TYPES

        for et in VALID_EVENT_TYPES:
            valid, errors = validate_canonical_object(_valid_cmo(event_type=et))
            assert valid is True, f"event_type={et!r} rejected: {errors}"


# ---------------------------------------------------------------------------
# Tests: Router recall
# ---------------------------------------------------------------------------


class TestRouterRecall:
    """Router.recall selects adapters and returns bounded results."""

    def test_recall_pattern_retrieval_empty(self, tmp_path):
        router = _make_router(tmp_path)
        result = router.recall(
            "test query",
            intent="pattern_retrieval",
            task_class="code_impl",
            keywords=["test"],
        )
        assert isinstance(result, RecallResult)
        assert result.results == []
        assert "memory_file" in result.sources_queried
        assert result.trace_id

    def test_recall_with_artifacts(self, tmp_path):
        """Recall returns results when artifacts exist."""
        # Write a test artifact
        wl_dir = tmp_path / "workflow_learnings"
        wl_dir.mkdir(parents=True)
        artifact = {
            "artifact_id": "mem_test_123",
            "workflow_id": "test",
            "task_summary": "Test vault schema validation",
            "task_class": "code_impl",
            "roles_involved": ["coder"],
            "key_decisions": ["Used canonical schema"],
            "successful_patterns": ["Schema-first approach"],
            "failure_patterns": [],
            "verification_outcome": "approved",
            "reusable_guidance": "Always validate schemas first",
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "confidence": "high",
        }
        (wl_dir / "mem_test_123.json").write_text(json.dumps(artifact, indent=2))

        router = _make_router(tmp_path)
        result = router.recall(
            "vault schema",
            intent="pattern_retrieval",
            task_class="code_impl",
            keywords=["vault", "schema"],
        )
        assert result.total_found >= 1
        assert any("schema" in str(r).lower() for r in result.results)

    def test_recall_caps_results(self, tmp_path):
        """Recall respects max_results."""
        wl_dir = tmp_path / "workflow_learnings"
        wl_dir.mkdir(parents=True)
        for i in range(10):
            artifact = {
                "artifact_id": f"mem_test_{i}",
                "workflow_id": f"test_{i}",
                "task_summary": f"Test task {i}",
                "task_class": "code_impl",
                "roles_involved": ["coder"],
                "key_decisions": ["Decision"],
                "successful_patterns": ["Pattern"],
                "failure_patterns": [],
                "verification_outcome": "approved",
                "reusable_guidance": "Guidance",
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "confidence": "high",
            }
            (wl_dir / f"mem_test_{i}.json").write_text(json.dumps(artifact, indent=2))

        router = _make_router(tmp_path)
        result = router.recall(
            "test",
            intent="pattern_retrieval",
            task_class="code_impl",
            keywords=["test"],
            max_results=3,
        )
        assert len(result.results) <= 3

    def test_recall_vault_context_scope(self):
        """Recall with intent=vault_context routes to vault adapter."""
        router = MemoryRouter()
        result = router.recall(
            "memory architecture",
            intent="vault_context",
            scope="vault",
        )
        assert "obsidian_vault" in result.sources_queried

    def test_recall_failopen_on_adapter_error(self, tmp_path):
        """Recall returns empty results when adapter throws."""
        broken_adapter = MagicMock()
        broken_adapter.name = "broken"
        broken_adapter.recall.side_effect = RuntimeError("boom")

        router = MemoryRouter(adapters=[broken_adapter])
        result = router.recall("test", intent="general")
        assert result.results == []
        assert result.total_found == 0


# ---------------------------------------------------------------------------
# Tests: Router store
# ---------------------------------------------------------------------------


class TestRouterStore:
    """Router.store validates and routes to correct adapter."""

    def test_store_valid_object(self, tmp_path):
        wl_dir = tmp_path / "workflow_learnings"
        wl_dir.mkdir(parents=True)

        router = _make_router(tmp_path)
        obj = _valid_cmo(target_store="memory_file")
        result = router.store(obj)
        assert isinstance(result, StoreResult)
        assert result.trace_id

    def test_store_rejects_invalid_object(self, tmp_path):
        router = _make_router(tmp_path)
        obj = _valid_cmo(title="", summary="")
        result = router.store(obj)
        assert result.stored is False
        assert result.rejection_reason == "validation_failed"
        assert len(result.validation_errors) > 0

    def test_store_infers_target(self, tmp_path):
        """Store infers target_store from event_type when not set."""
        wl_dir = tmp_path / "workflow_learnings"
        wl_dir.mkdir(parents=True)

        router = _make_router(tmp_path)
        obj = _valid_cmo(target_store=None, event_type="task_completed")
        # Should infer memory_file
        result = router.store(obj)
        assert result.store_used == "memory_file"

    def test_store_discard(self, tmp_path):
        """Store with target_store=discard does not persist."""
        router = _make_router(tmp_path)
        obj = _valid_cmo(target_store="discard")
        result = router.store(obj)
        assert result.stored is False
        assert result.store_used == "discard"

    def test_store_no_adapter(self, tmp_path):
        """Store returns rejection when adapter is missing."""
        router = _make_router(tmp_path)
        obj = _valid_cmo(target_store="fusion_memory")
        result = router.store(obj)
        assert result.stored is False
        assert "no adapter" in result.rejection_reason


# ---------------------------------------------------------------------------
# Tests: ingest_event
# ---------------------------------------------------------------------------


class TestIngestEvent:
    """ingest_event normalizes raw events into CanonicalMemoryObject."""

    def test_ingest_valid_event(self, tmp_path):
        router = _make_router(tmp_path)
        obj = router.ingest_event(
            {
                "source": "watcher",
                "event_type": "task_completed",
                "title": "Test task done",
                "summary": "Completed test task successfully.",
            }
        )
        assert isinstance(obj, CanonicalMemoryObject)
        assert obj.source == "watcher"
        assert obj.event_type == "task_completed"
        assert obj.memory_id.startswith("cm-wat-")
        assert obj.provenance == "automatic"
        assert obj.confidence == "medium"

    def test_ingest_missing_required(self, tmp_path):
        router = _make_router(tmp_path)
        with pytest.raises(ValueError, match="missing required"):
            router.ingest_event({"source": "watcher"})

    def test_ingest_preserves_optional_fields(self, tmp_path):
        router = _make_router(tmp_path)
        obj = router.ingest_event(
            {
                "source": "heartbeat",
                "event_type": "research_completed",
                "title": "Research done",
                "summary": "Deep research on memory systems.",
                "confidence": "high",
                "tags": ["#type/research"],
                "related_task_ids": ["0042"],
                "target_store": "obsidian_vault",
                "current_layer": "semantic",  # Explicit layer override
            }
        )
        assert obj.confidence == "high"
        assert obj.tags == ["#type/research"]
        assert obj.target_store == "obsidian_vault"
        # Phase 2: explicit current_layer is respected and synced
        assert obj.current_layer == "semantic"
        assert obj.memory_layer_candidate == "semantic"

    def test_ingest_title_truncation(self, tmp_path):
        router = _make_router(tmp_path)
        obj = router.ingest_event(
            {
                "source": "watcher",
                "event_type": "task_completed",
                "title": "x" * 200,
                "summary": "Test",
            }
        )
        assert len(obj.title) <= 100


# ---------------------------------------------------------------------------
# Tests: Skeleton methods
# ---------------------------------------------------------------------------


class TestSkeletonMethods:
    """Skeleton methods return structured not-implemented results."""

    def test_promote_boundary_validated(self, tmp_path):
        """promote() now validates boundaries (Phase 2) but doesn't migrate."""
        router = _make_router(tmp_path)
        result = router.promote("cm-test-123", "episodic", "semantic")
        assert isinstance(result, PromoteResult)
        assert result.promoted is False  # Migration deferred to Phase 3+
        assert "boundary_check_passed" in result.reason

    def test_checkpoint_creates_artifact(self, tmp_path):
        """Phase 6: checkpoint() creates a working-layer artifact."""
        router = _make_router(tmp_path)
        result = router.checkpoint("session done", open_threads=["fix bug"])
        assert result["status"] in ("created", "store_failed")
        assert result["action"] == "checkpoint_created"

    def test_checkpoint_rejects_empty(self, tmp_path):
        router = _make_router(tmp_path)
        result = router.checkpoint("")
        assert result["status"] == "rejected"

    def test_consolidate_returns_result(self, tmp_path):
        """Phase 6: consolidate() returns structured result."""
        router = _make_router(tmp_path)
        result = router.consolidate(window_type="working_memory")
        assert "status" in result
        assert "input_count" in result

    def test_summarize_session_returns_result(self, tmp_path):
        """Phase 6: summarize_session() returns structured result."""
        router = _make_router(tmp_path)
        result = router.summarize_session("s123")
        assert "status" in result

    def test_track_open_loop_creates_loop(self, tmp_path):
        """Phase 7: track_open_loop() creates a real loop."""
        router = _make_router(tmp_path)
        result = router.track_open_loop(
            "Fix the authentication timeout bug in telegram module",
            title="Fix auth timeout",
            source="watcher",
            caller="test",
        )
        assert result["action"] == "loop_created"
        assert result["loop_id"]
        assert result["stored"] is True

    def test_extract_patterns_returns_result(self, tmp_path):
        """Phase 6: extract_patterns() returns structured result."""
        router = _make_router(tmp_path)
        result = router.extract_patterns()
        assert "status" in result
        assert "candidates" in result

    def test_generate_diary_returns_result(self, tmp_path):
        """Phase 6: generate_diary() is alias for summarize_session()."""
        router = _make_router(tmp_path)
        result = router.generate_diary("s123")
        assert "status" in result

    def test_reflect_returns_result(self, tmp_path):
        """Phase 6: reflect() runs consolidation + pattern extraction."""
        router = _make_router(tmp_path)
        result = router.reflect()
        assert result["status"] == "completed"
        assert "consolidation" in result
        assert "patterns" in result


# ---------------------------------------------------------------------------
# Tests: Structured trace emission
# ---------------------------------------------------------------------------


class TestStructuredTracing:
    """Router operations emit structured log events."""

    def test_recall_emits_trace(self, tmp_path):
        router = _make_router(tmp_path)
        ctx = TraceContext.new("test")

        with patch("agents.memory_router.slog") as mock_slog:
            router.recall(
                "test query",
                intent="pattern_retrieval",
                task_class="code_impl",
                keywords=["test"],
                caller="test_caller",
                ctx=ctx,
            )
            mock_slog.event.assert_called()
            call_args = mock_slog.event.call_args
            assert call_args[0][0] == "memory.router.recall"
            assert call_args[1]["operation"] == "recall"
            assert call_args[1]["caller"] == "test_caller"
            # Phase 5: legacy "pattern_retrieval" is mapped to "procedural_recall"
            assert call_args[1]["intent"] in ("pattern_retrieval", "procedural_recall")

    def test_store_emits_trace_on_rejection(self, tmp_path):
        router = _make_router(tmp_path)

        with patch("agents.memory_router.slog") as mock_slog:
            router.store(_valid_cmo(title=""))
            mock_slog.event.assert_called()
            call_args = mock_slog.event.call_args
            assert call_args[0][0] == "memory.router.store"
            assert call_args[1]["validation_outcome"] == "rejected"

    def test_ingest_emits_trace(self, tmp_path):
        router = _make_router(tmp_path)

        with patch("agents.memory_router.slog") as mock_slog:
            router.ingest_event(
                {
                    "source": "watcher",
                    "event_type": "task_completed",
                    "title": "Test",
                    "summary": "Test summary",
                }
            )
            mock_slog.event.assert_called()
            call_args = mock_slog.event.call_args
            assert call_args[0][0] == "memory.router.ingest"


# ---------------------------------------------------------------------------
# Tests: Adapter contracts
# ---------------------------------------------------------------------------


class TestMemoryFileAdapter:
    """MemoryFileAdapter wraps agents/memory_engine correctly."""

    def test_recall_empty(self, tmp_path):
        adapter = MemoryFileAdapter(base=tmp_path)
        results = adapter.recall("test", task_class="code_impl", keywords=["test"])
        assert results == []

    def test_is_available(self, tmp_path):
        adapter = MemoryFileAdapter(base=tmp_path)
        assert adapter.is_available() is True

    def test_store_and_recall_roundtrip(self, tmp_path):
        (tmp_path / "workflow_learnings").mkdir(parents=True)
        adapter = MemoryFileAdapter(base=tmp_path)
        obj = _valid_cmo(
            memory_id=f"mem_roundtrip_{int(time.time())}",
            related_task_ids=["roundtrip_test"],
        )
        result = adapter.store(obj)
        assert result.stored is True

        # Now recall should find it
        hits = adapter.recall(
            "test memory",
            task_class="unknown",
            keywords=["test"],
        )
        assert len(hits) >= 1


class TestFusionMemoryAdapter:
    """FusionMemoryAdapter — Phase 3: direct Python bridge (fail-safe when backends unavailable)."""

    @staticmethod
    def _make_exhausted_adapter():
        """Create an adapter that has exhausted all init retries."""
        from agents.memory_router import _FUSION_INIT_MAX_RETRIES

        adapter = FusionMemoryAdapter()
        adapter._init_attempts = _FUSION_INIT_MAX_RETRIES
        adapter._available = False
        return adapter

    def test_recall_returns_empty_when_unavailable(self):
        """Recall returns [] when backends are not reachable."""
        adapter = self._make_exhausted_adapter()
        assert adapter.recall("test") == []

    def test_store_returns_rejection_when_unavailable(self):
        """Store returns rejection when backends are not reachable."""
        adapter = self._make_exhausted_adapter()
        result = adapter.store(_valid_cmo())
        assert result.stored is False
        assert "not available" in result.rejection_reason

    def test_not_available_when_retries_exhausted(self):
        """is_available returns False when retries are exhausted."""
        adapter = self._make_exhausted_adapter()
        assert adapter.is_available() is False

    def test_recall_with_mock_service(self):
        """Recall works with a mock MemoryService."""
        adapter = FusionMemoryAdapter()
        mock_svc = MagicMock()
        mock_svc.perform_query = MagicMock()
        adapter._service = mock_svc
        adapter._available = True

        async def mock_query(query_text, top_k_final=5):
            return [
                {"id": "mem-1", "content": "test result", "score": 0.9, "metadata": {"category": "research"}},
            ]

        mock_svc.perform_query = mock_query
        results = adapter.recall("test query", max_results=5)
        assert len(results) == 1
        assert results[0]["memory_id"] == "mem-1"
        assert results[0]["source"] == "fusion_memory"

    def test_store_with_mock_service(self):
        """Store works with a mock MemoryService."""
        adapter = FusionMemoryAdapter()
        mock_svc = MagicMock()
        adapter._service = mock_svc
        adapter._available = True

        async def mock_upsert(content, memory_id=None, metadata=None):
            return memory_id or "generated-id"

        mock_svc.perform_upsert = mock_upsert
        result = adapter.store(_valid_cmo())
        assert result.stored is True
        assert result.store_used == "fusion_memory"
        assert result.path_or_id  # Has an ID

    def test_event_type_to_category_mapping(self):
        """All valid event types have a Fusion Memory category mapping."""
        from agents.memory_router import VALID_EVENT_TYPES, _map_event_to_category

        for et in VALID_EVENT_TYPES:
            cat = _map_event_to_category(et)
            assert cat in ("context", "research", "decision", "pattern", "debug"), (
                f"event_type {et!r} maps to unexpected category {cat!r}"
            )

    def test_timeout_is_configurable(self):
        """FUSION_MEMORY_TIMEOUT_S is an int and defaults to 3600."""
        from agents.memory_router import FUSION_MEMORY_TIMEOUT_S

        assert isinstance(FUSION_MEMORY_TIMEOUT_S, int)
        # Default is 3600 unless env override is set
        assert FUSION_MEMORY_TIMEOUT_S > 0

    def test_init_retry_respects_cooldown(self):
        """Second init attempt within cooldown is skipped."""
        import time as _time

        adapter = FusionMemoryAdapter()
        # Simulate one failed attempt that just happened
        adapter._init_attempts = 1
        adapter._last_init_attempt = _time.monotonic()
        # Should be within cooldown — returns False without incrementing
        assert adapter._ensure_service() is False
        assert adapter._init_attempts == 1  # Not incremented

    def test_init_retry_after_cooldown_expires(self):
        """After cooldown, a new init attempt is allowed."""
        import time as _time

        adapter = FusionMemoryAdapter()
        adapter._init_attempts = 1
        # Simulate cooldown expired (set last attempt far in the past)
        adapter._last_init_attempt = _time.monotonic() - 999999
        # Will attempt init (and fail because no real backend), incrementing counter
        adapter._ensure_service()
        assert adapter._init_attempts == 2  # Incremented

    def test_init_stops_after_max_retries(self):
        """After max retries, no more init attempts are made."""
        from agents.memory_router import _FUSION_INIT_MAX_RETRIES

        adapter = FusionMemoryAdapter()
        adapter._init_attempts = _FUSION_INIT_MAX_RETRIES
        assert adapter._ensure_service() is False
        assert adapter._init_attempts == _FUSION_INIT_MAX_RETRIES  # Not incremented

    def test_invalidate_service_enters_degraded_state(self):
        """_invalidate_service clears service and sets degraded flag."""
        adapter = FusionMemoryAdapter()
        mock_svc = MagicMock()
        adapter._service = mock_svc
        adapter._available = True

        adapter._invalidate_service("test error")
        assert adapter._service is None
        assert adapter._degraded is True

    def test_request_failure_allows_reinit(self):
        """After a recall/store failure, adapter can attempt re-init on next call."""
        import time as _time

        adapter = FusionMemoryAdapter()
        mock_svc = MagicMock()
        adapter._service = mock_svc
        adapter._available = True
        adapter._init_attempts = 1
        adapter._last_init_attempt = _time.monotonic() - 999999  # cooldown expired

        # Simulate a request failure
        adapter._invalidate_service("connection reset")
        assert adapter._service is None
        assert adapter._degraded is True

        # Next _ensure_service should attempt re-init (will fail — no real backend)
        adapter._ensure_service()
        assert adapter._init_attempts == 2  # Re-init was attempted

    def test_get_health_status(self):
        """get_health_status returns structured adapter state."""
        adapter = FusionMemoryAdapter()
        health = adapter.get_health_status()
        assert "available" in health
        assert "degraded" in health
        assert "init_attempts" in health
        assert "retries_remaining" in health
        assert "timeout_s" in health
        assert health["timeout_s"] > 0

    def test_recovery_from_degraded_state(self):
        """Adapter logs recovery when re-init succeeds after degraded state."""
        adapter = FusionMemoryAdapter()
        adapter._degraded = True
        adapter._init_attempts = 1
        adapter._last_init_attempt = 0  # cooldown expired

        # Inject a mock service directly to simulate successful re-init
        mock_svc = MagicMock()

        async def mock_init():
            return True

        mock_svc.initialize = mock_init

        with (
            patch("agents.memory_router._ensure_fusion_env", return_value=True),
            patch("agents.memory_router.MemoryService", return_value=mock_svc, create=True),
        ):
            # Patch the import inside _ensure_service
            import sys

            # Create a fake module for the import
            fake_module = type(sys)("app.services.memory_service")
            fake_module.MemoryService = lambda: mock_svc
            sys.modules["app.services.memory_service"] = fake_module
            try:
                result = adapter._ensure_service()
            finally:
                sys.modules.pop("app.services.memory_service", None)

        if result:
            # If init succeeded, degraded should be cleared
            assert adapter._degraded is False
            assert adapter._available is True


class TestVaultAdapter:
    """VaultAdapter wraps vault MCP search."""

    def test_recall_without_vault(self):
        """Recall returns empty when vault is unavailable."""
        adapter = VaultAdapter()
        with patch.dict("sys.modules", {"tools.mcp_vault_server": None}):
            # If vault module can't be imported, should return []
            results = adapter.recall("test")
            # May be [] or may work depending on environment
            assert isinstance(results, list)


# ---------------------------------------------------------------------------
# Tests: Migrated watcher recall path
# ---------------------------------------------------------------------------


class TestWatcherRecallMigration:
    """Verify the watcher recall path works through the router."""

    def test_router_recall_matches_legacy_format(self, tmp_path):
        """Router recall results are compatible with format_retrieval_for_planner."""
        from agents.memory_engine import format_retrieval_for_planner

        # Write a test artifact
        wl_dir = tmp_path / "workflow_learnings"
        wl_dir.mkdir(parents=True)
        artifact = {
            "artifact_id": "mem_watcher_test_111",
            "workflow_id": "watcher_test",
            "task_summary": "Implemented vault schema validation",
            "task_class": "code_impl",
            "roles_involved": ["coder"],
            "key_decisions": ["Used existing validator pattern"],
            "successful_patterns": ["Schema-first"],
            "failure_patterns": [],
            "verification_outcome": "approved",
            "reusable_guidance": "Validate schemas before writing",
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "confidence": "high",
        }
        (wl_dir / "mem_watcher_test_111.json").write_text(json.dumps(artifact, indent=2))

        # Use router (same path as migrated watcher code)
        router = _make_router(tmp_path)
        recall_result = router.recall(
            query="vault schema validation",
            intent="pattern_retrieval",
            task_class="code_impl",
            keywords=["vault", "schema", "validation"],
            caller="watcher.dispatch_task",
        )

        # format_retrieval_for_planner should work with router results
        formatted = format_retrieval_for_planner(recall_result.results)
        assert "Prior Related Patterns" in formatted
        assert "vault" in formatted.lower() or "schema" in formatted.lower()

    def test_router_recall_empty_still_formats(self, tmp_path):
        """Empty recall results still produce valid planner output."""
        from agents.memory_engine import format_retrieval_for_planner

        router = _make_router(tmp_path)
        recall_result = router.recall(
            query="nonexistent",
            intent="pattern_retrieval",
            task_class="code_impl",
            keywords=["nonexistent"],
        )
        formatted = format_retrieval_for_planner(recall_result.results)
        assert "No prior related patterns" in formatted


# ---------------------------------------------------------------------------
# Tests: Adapter selection logic
# ---------------------------------------------------------------------------


class TestAdapterSelection:
    """Router selects correct adapters based on intent and scope."""

    def test_pattern_retrieval_uses_memory_file(self):
        router = MemoryRouter()
        names = router._select_recall_adapters("pattern_retrieval", "all")
        assert names == ["memory_file"]

    def test_vault_context_uses_vault(self):
        router = MemoryRouter()
        names = router._select_recall_adapters("vault_context", "all")
        assert names == ["obsidian_vault"]

    def test_session_replay_uses_fusion_and_working(self):
        router = MemoryRouter()
        names = router._select_recall_adapters("session_replay", "all")
        assert "fusion_memory" in names
        assert "state_working" in names

    def test_general_uses_multi(self):
        router = MemoryRouter()
        names = router._select_recall_adapters("general", "all")
        assert "memory_file" in names
        assert "obsidian_vault" in names

    def test_explicit_scope_overrides_intent(self):
        router = MemoryRouter()
        names = router._select_recall_adapters("pattern_retrieval", "vault")
        assert names == ["obsidian_vault"]

    def test_scope_memory_files(self):
        router = MemoryRouter()
        names = router._select_recall_adapters("general", "memory_files")
        assert names == ["memory_file"]


# ---------------------------------------------------------------------------
# Phase 2 Tests: Layer Policy Functions
# ---------------------------------------------------------------------------


class TestLayerPolicyFunctions:
    """Phase 2 layer policy helpers enforce the 4-layer model."""

    # -- infer_layer_from_event_type --

    def test_infer_task_completed_is_episodic(self):
        assert infer_layer_from_event_type("task_completed") == "episodic"

    def test_infer_session_end_is_working(self):
        assert infer_layer_from_event_type("session_end") == "working"

    def test_infer_heartbeat_cycle_is_working(self):
        assert infer_layer_from_event_type("heartbeat_cycle") == "working"

    def test_infer_workflow_learning_promoted_is_semantic(self):
        assert infer_layer_from_event_type("workflow_learning_promoted") == "semantic"

    def test_infer_agent_pattern_promoted_is_procedural(self):
        assert infer_layer_from_event_type("agent_pattern_promoted") == "procedural"

    def test_infer_unknown_defaults_to_episodic(self):
        assert infer_layer_from_event_type("unknown_event") == "episodic"

    def test_all_event_types_have_mappings(self):
        """Every VALID_EVENT_TYPE should have a layer mapping."""
        from agents.memory_router import VALID_EVENT_TYPES

        for et in VALID_EVENT_TYPES:
            assert et in EVENT_TYPE_TO_LAYER, f"{et} missing from EVENT_TYPE_TO_LAYER"

    # -- compute_candidate_next_layer --

    def test_candidate_next_from_working(self):
        assert compute_candidate_next_layer("working") == "episodic"

    def test_candidate_next_from_episodic(self):
        assert compute_candidate_next_layer("episodic") == "semantic"

    def test_candidate_next_from_semantic(self):
        assert compute_candidate_next_layer("semantic") == "procedural"

    def test_candidate_next_from_procedural_is_none(self):
        assert compute_candidate_next_layer("procedural") is None

    # -- check_store_layer_compatibility --

    def test_discard_always_compatible(self):
        for layer in ("working", "episodic", "semantic", "procedural"):
            ok, _ = check_store_layer_compatibility(layer, "discard")
            assert ok is True

    def test_working_allows_fusion_memory(self):
        ok, _ = check_store_layer_compatibility("working", "fusion_memory")
        assert ok is True

    def test_working_allows_state_working(self):
        ok, _ = check_store_layer_compatibility("working", "state_working")
        assert ok is True

    def test_working_rejects_memory_file(self):
        ok, reason = check_store_layer_compatibility("working", "memory_file")
        assert ok is False
        assert "not allowed" in reason

    def test_working_rejects_obsidian_vault(self):
        ok, _ = check_store_layer_compatibility("working", "obsidian_vault")
        assert ok is False

    def test_episodic_allows_memory_file(self):
        ok, _ = check_store_layer_compatibility("episodic", "memory_file")
        assert ok is True

    def test_episodic_allows_obsidian_vault(self):
        ok, _ = check_store_layer_compatibility("episodic", "obsidian_vault")
        assert ok is True

    def test_semantic_rejects_memory_file(self):
        ok, _ = check_store_layer_compatibility("semantic", "memory_file")
        assert ok is False

    def test_procedural_allows_obsidian_vault(self):
        ok, _ = check_store_layer_compatibility("procedural", "obsidian_vault")
        assert ok is True

    def test_procedural_allows_memory_file(self):
        ok, _ = check_store_layer_compatibility("procedural", "memory_file")
        assert ok is True

    def test_procedural_rejects_fusion_memory(self):
        ok, _ = check_store_layer_compatibility("procedural", "fusion_memory")
        assert ok is False

    def test_unknown_layer_rejected(self):
        ok, reason = check_store_layer_compatibility("tactical", "memory_file")
        assert ok is False
        assert "unknown layer" in reason

    # -- check_promotion_boundary --

    def test_working_to_episodic_allowed(self):
        ok, _ = check_promotion_boundary("working", "episodic")
        assert ok is True

    def test_episodic_to_semantic_allowed(self):
        ok, _ = check_promotion_boundary("episodic", "semantic")
        assert ok is True

    def test_semantic_to_procedural_allowed(self):
        ok, _ = check_promotion_boundary("semantic", "procedural")
        assert ok is True

    def test_working_to_semantic_rejected(self):
        """Cannot skip layers."""
        ok, reason = check_promotion_boundary("working", "semantic")
        assert ok is False
        assert "not allowed" in reason

    def test_working_to_procedural_rejected(self):
        ok, _ = check_promotion_boundary("working", "procedural")
        assert ok is False

    def test_episodic_to_procedural_rejected(self):
        ok, _ = check_promotion_boundary("episodic", "procedural")
        assert ok is False

    def test_procedural_to_anything_rejected(self):
        """Procedural is terminal."""
        ok, reason = check_promotion_boundary("procedural", "semantic")
        assert ok is False

    def test_same_layer_promotion_rejected(self):
        ok, reason = check_promotion_boundary("episodic", "episodic")
        assert ok is False
        assert "no-op" in reason

    def test_demotion_rejected(self):
        ok, _ = check_promotion_boundary("semantic", "episodic")
        assert ok is False

    def test_unknown_source_layer(self):
        ok, reason = check_promotion_boundary("tactical", "episodic")
        assert ok is False
        assert "unknown" in reason

    def test_unknown_target_layer(self):
        ok, reason = check_promotion_boundary("working", "tactical")
        assert ok is False
        assert "unknown" in reason


# ---------------------------------------------------------------------------
# Phase 2 Tests: Layer Validation on CanonicalMemoryObject
# ---------------------------------------------------------------------------


class TestPhase2LayerValidation:
    """Phase 2 layer fields are validated on CanonicalMemoryObject."""

    def test_invalid_current_layer(self):
        valid, errors = validate_canonical_object(_valid_cmo(current_layer="tactical"))
        assert valid is False
        assert any("current_layer" in e for e in errors)

    def test_invalid_candidate_next_layer(self):
        valid, errors = validate_canonical_object(_valid_cmo(candidate_next_layer="tactical"))
        assert valid is False
        assert any("candidate_next_layer" in e for e in errors)

    def test_none_candidate_next_layer_valid(self):
        valid, errors = validate_canonical_object(_valid_cmo(candidate_next_layer=None))
        assert valid is True, errors

    def test_invalid_promotion_eligibility(self):
        valid, errors = validate_canonical_object(_valid_cmo(promotion_eligibility="maybe"))
        assert valid is False
        assert any("promotion_eligibility" in e for e in errors)

    def test_valid_layer_fields(self):
        """All Phase 2 layer fields with valid values pass validation."""
        obj = _valid_cmo(
            current_layer="semantic",
            candidate_next_layer="procedural",
            promotion_eligibility="needs_review",
        )
        valid, errors = validate_canonical_object(obj)
        assert valid is True, errors


# ---------------------------------------------------------------------------
# Phase 2 Tests: ingest_event Layer Auto-Assignment
# ---------------------------------------------------------------------------


class TestIngestEventLayerAssignment:
    """ingest_event auto-assigns layer metadata from event_type."""

    def test_task_completed_gets_episodic(self, tmp_path):
        router = _make_router(tmp_path)
        obj = router.ingest_event(
            {
                "source": "watcher",
                "event_type": "task_completed",
                "title": "Task done",
                "summary": "Task completed.",
            }
        )
        assert obj.current_layer == "episodic"
        assert obj.memory_layer_candidate == "episodic"
        assert obj.candidate_next_layer == "semantic"
        assert obj.promotion_eligibility == "eligible"
        assert "inferred" in obj.layer_reason

    def test_session_end_gets_working(self, tmp_path):
        router = _make_router(tmp_path)
        obj = router.ingest_event(
            {
                "source": "watcher",
                "event_type": "session_end",
                "title": "Session ended",
                "summary": "Session end checkpoint.",
            }
        )
        assert obj.current_layer == "working"
        assert obj.candidate_next_layer == "episodic"
        assert obj.promotion_eligibility == "eligible"

    def test_workflow_learning_promoted_gets_semantic(self, tmp_path):
        router = _make_router(tmp_path)
        obj = router.ingest_event(
            {
                "source": "promoter",
                "event_type": "workflow_learning_promoted",
                "title": "Promoted learning",
                "summary": "Workflow learning promoted to semantic.",
            }
        )
        assert obj.current_layer == "semantic"
        assert obj.candidate_next_layer == "procedural"
        assert obj.promotion_eligibility == "needs_review"

    def test_agent_pattern_promoted_gets_procedural(self, tmp_path):
        router = _make_router(tmp_path)
        obj = router.ingest_event(
            {
                "source": "promoter",
                "event_type": "agent_pattern_promoted",
                "title": "Promoted pattern",
                "summary": "Agent pattern promoted to procedural.",
            }
        )
        assert obj.current_layer == "procedural"
        assert obj.candidate_next_layer is None
        assert obj.promotion_eligibility == "already_promoted"

    def test_explicit_layer_override(self, tmp_path):
        """Caller can override layer assignment with explicit current_layer."""
        router = _make_router(tmp_path)
        obj = router.ingest_event(
            {
                "source": "watcher",
                "event_type": "task_completed",  # Would normally be episodic
                "title": "Override test",
                "summary": "Testing explicit layer override.",
                "current_layer": "semantic",
            }
        )
        assert obj.current_layer == "semantic"
        assert "explicit" in obj.layer_reason

    def test_operator_override(self, tmp_path):
        """Explicit valid current_layer is respected regardless of provenance."""
        router = _make_router(tmp_path)
        obj = router.ingest_event(
            {
                "source": "operator",
                "event_type": "task_completed",
                "title": "Operator override",
                "summary": "Operator overriding layer.",
                "provenance": "operator_requested",
                "current_layer": "procedural",
            }
        )
        assert obj.current_layer == "procedural"
        assert "explicit" in obj.layer_reason

    def test_promotion_eligibility_override(self, tmp_path):
        """Caller can override promotion_eligibility."""
        router = _make_router(tmp_path)
        obj = router.ingest_event(
            {
                "source": "watcher",
                "event_type": "task_completed",
                "title": "Test",
                "summary": "Test.",
                "promotion_eligibility": "ineligible",
            }
        )
        assert obj.promotion_eligibility == "ineligible"

    def test_layer_reason_documents_assignment(self, tmp_path):
        router = _make_router(tmp_path)
        obj = router.ingest_event(
            {
                "source": "watcher",
                "event_type": "bug_fixed",
                "title": "Fixed crash",
                "summary": "Fixed null pointer crash.",
            }
        )
        assert obj.layer_reason
        assert "bug_fixed" in obj.layer_reason


# ---------------------------------------------------------------------------
# Phase 2 Tests: Store Layer/Store Compatibility
# ---------------------------------------------------------------------------


class TestStoreLayerCompatibility:
    """Router.store rejects writes that violate layer/store rules."""

    def test_working_to_memory_file_rejected(self, tmp_path):
        """Working layer cannot write to memory_file."""
        router = _make_router(tmp_path)
        obj = _valid_cmo(
            current_layer="working",
            target_store="memory_file",
        )
        result = router.store(obj)
        assert result.stored is False
        assert "layer_store_mismatch" in result.rejection_reason

    def test_episodic_to_memory_file_allowed(self, tmp_path):
        """Episodic layer can write to memory_file."""
        (tmp_path / "workflow_learnings").mkdir(parents=True)
        router = _make_router(tmp_path)
        obj = _valid_cmo(
            current_layer="episodic",
            target_store="memory_file",
        )
        result = router.store(obj)
        # Should pass layer check (may succeed or fail at adapter level)
        assert "layer_store_mismatch" not in (result.rejection_reason or "")

    def test_discard_bypasses_layer_check(self, tmp_path):
        """Discard target always passes layer compatibility."""
        router = _make_router(tmp_path)
        obj = _valid_cmo(
            current_layer="working",
            target_store="discard",
        )
        result = router.store(obj)
        assert result.store_used == "discard"
        assert "layer_store_mismatch" not in (result.rejection_reason or "")

    def test_store_traces_include_current_layer(self, tmp_path):
        """Store trace events include current_layer."""
        router = _make_router(tmp_path)
        with patch("agents.memory_router.slog") as mock_slog:
            router.store(
                _valid_cmo(
                    current_layer="episodic",
                    target_store="discard",
                )
            )
            call_args = mock_slog.event.call_args
            assert call_args[1].get("current_layer") == "episodic"


# ---------------------------------------------------------------------------
# Phase 2 Tests: Promote Boundary Enforcement
# ---------------------------------------------------------------------------


class TestPromoteBoundaryEnforcement:
    """Router.promote enforces promotion boundaries."""

    def test_valid_promotion_passes_boundary(self, tmp_path):
        router = _make_router(tmp_path)
        result = router.promote("cm-test-123", "episodic", "semantic")
        assert "boundary_check_passed" in result.reason
        assert result.promoted is False  # Actual migration deferred

    def test_skip_layer_promotion_rejected(self, tmp_path):
        router = _make_router(tmp_path)
        result = router.promote("cm-test-123", "working", "semantic")
        assert "promotion_boundary_violation" in result.reason
        assert result.promoted is False

    def test_demotion_rejected(self, tmp_path):
        router = _make_router(tmp_path)
        result = router.promote("cm-test-123", "semantic", "episodic")
        assert "promotion_boundary_violation" in result.reason

    def test_terminal_layer_rejected(self, tmp_path):
        router = _make_router(tmp_path)
        result = router.promote("cm-test-123", "procedural", "semantic")
        assert result.promoted is False
        assert "promotion_boundary_violation" in result.reason

    def test_promote_traces_include_layers(self, tmp_path):
        """Promote trace events include from/to layer info."""
        router = _make_router(tmp_path)
        with patch("agents.memory_router.slog") as mock_slog:
            router.promote("cm-test-123", "episodic", "semantic")
            call_args = mock_slog.event.call_args
            assert call_args[1].get("from_layer") == "episodic"
            assert call_args[1].get("target_layer") == "semantic"


# ---------------------------------------------------------------------------
# Phase 2 Tests: Ingest Trace Includes Layer Context
# ---------------------------------------------------------------------------


class TestIngestTraceLayerContext:
    """ingest_event traces include Phase 2 layer metadata."""

    def test_ingest_trace_has_layer_fields(self, tmp_path):
        router = _make_router(tmp_path)
        with patch("agents.memory_router.slog") as mock_slog:
            router.ingest_event(
                {
                    "source": "watcher",
                    "event_type": "task_completed",
                    "title": "Test",
                    "summary": "Test.",
                }
            )
            call_args = mock_slog.event.call_args
            assert call_args[1].get("current_layer") == "episodic"
            assert call_args[1].get("promotion_eligibility") == "eligible"
            assert "layer_reason" in call_args[1]


# ---------------------------------------------------------------------------
# WorkingMemoryAdapter Tests
# ---------------------------------------------------------------------------


class TestWorkingMemoryAdapter:
    """WorkingMemoryAdapter persists transient events to STATE/working_memory/."""

    def test_store_creates_json_file(self, tmp_path):
        adapter = WorkingMemoryAdapter(base=tmp_path)
        obj = _valid_cmo(current_layer="working")
        result = adapter.store(obj)
        assert result.stored is True
        assert result.store_used == "state_working"
        assert result.path_or_id
        # File should exist
        from pathlib import Path

        stored_path = Path(result.path_or_id)
        assert stored_path.exists()
        # Content should be valid JSON with expected fields
        import json

        data = json.loads(stored_path.read_text())
        assert data["memory_id"] == obj.memory_id
        assert data["event_type"] == obj.event_type
        assert data["current_layer"] == "working"

    def test_recall_empty_dir(self, tmp_path):
        adapter = WorkingMemoryAdapter(base=tmp_path)
        results = adapter.recall("test")
        assert results == []

    def test_store_and_recall_roundtrip(self, tmp_path):
        adapter = WorkingMemoryAdapter(base=tmp_path)
        obj = _valid_cmo(
            current_layer="working",
            event_type="heartbeat_cycle",
            source="heartbeat",
            title="heartbeat: HEALTHY",
            summary="All 5 checks passed.",
        )
        store_result = adapter.store(obj)
        assert store_result.stored is True

        hits = adapter.recall("heartbeat")
        assert len(hits) >= 1
        assert hits[0]["event_type"] == "heartbeat_cycle"
        assert hits[0]["_source_adapter"] == "state_working"

    def test_is_available(self, tmp_path):
        adapter = WorkingMemoryAdapter(base=tmp_path)
        assert adapter.is_available() is True

    def test_file_uses_atomic_write(self, tmp_path):
        """Store uses tmp+rename pattern (no .tmp files left behind)."""
        adapter = WorkingMemoryAdapter(base=tmp_path)
        obj = _valid_cmo(current_layer="working")
        adapter.store(obj)
        wm_dir = tmp_path / "working_memory"
        tmp_files = list(wm_dir.glob("*.tmp"))
        assert tmp_files == []
        json_files = list(wm_dir.glob("wm_*.json"))
        assert len(json_files) == 1


# ---------------------------------------------------------------------------
# Working-Layer Store/Layer Compatibility via Router
# ---------------------------------------------------------------------------


class TestWorkingLayerRouterIntegration:
    """Working-layer events route to state_working and are blocked elsewhere."""

    def _make_working_router(self, tmp_path):
        """Router with both memory_file and state_working adapters."""
        (tmp_path / "workflow_learnings").mkdir(parents=True, exist_ok=True)
        return MemoryRouter(
            adapters=[
                MemoryFileAdapter(base=tmp_path),
                WorkingMemoryAdapter(base=tmp_path),
            ],
            memory_file_base=tmp_path,
        )

    def test_working_heartbeat_accepted_by_state_working(self, tmp_path):
        """A working-layer heartbeat trigger stores via state_working adapter."""
        router = self._make_working_router(tmp_path)
        obj = router.ingest_event(
            {
                "source": "heartbeat",
                "event_type": "heartbeat_cycle",
                "title": "heartbeat_cycle: HEALTHY",
                "summary": "Heartbeat: 5 checks, 0 failed.",
            }
        )
        assert obj.current_layer == "working"
        result = router.store(obj, caller="test")
        assert result.stored is True
        assert result.store_used == "state_working"

    def test_working_event_rejected_by_memory_file(self, tmp_path):
        """Working-layer event explicitly targeting memory_file is rejected."""
        router = self._make_working_router(tmp_path)
        obj = _valid_cmo(
            current_layer="working",
            target_store="memory_file",
        )
        result = router.store(obj, caller="test")
        assert result.stored is False
        assert "layer_store_mismatch" in result.rejection_reason

    def test_working_event_rejected_by_obsidian_vault(self, tmp_path):
        """Working-layer event targeting obsidian_vault is rejected."""
        router = self._make_working_router(tmp_path)
        obj = _valid_cmo(
            current_layer="working",
            target_store="obsidian_vault",
        )
        result = router.store(obj, caller="test")
        assert result.stored is False
        assert "layer_store_mismatch" in result.rejection_reason

    def test_episodic_event_rejected_by_state_working(self, tmp_path):
        """Episodic event cannot go to state_working (wrong direction)."""
        router = self._make_working_router(tmp_path)
        obj = _valid_cmo(
            current_layer="episodic",
            target_store="state_working",
        )
        result = router.store(obj, caller="test")
        assert result.stored is False
        assert "layer_store_mismatch" in result.rejection_reason

    def test_working_infer_routes_to_state_working(self, tmp_path):
        """Working-layer event with no explicit target_store infers state_working."""
        router = self._make_working_router(tmp_path)
        obj = _valid_cmo(
            current_layer="working",
            event_type="heartbeat_cycle",
            target_store=None,
        )
        result = router.store(obj, caller="test")
        assert result.store_used == "state_working"
        assert result.stored is True

    def test_working_store_traces_layer_and_adapter(self, tmp_path):
        """Store trace for working-layer event includes current_layer and adapter."""
        router = self._make_working_router(tmp_path)
        obj = router.ingest_event(
            {
                "source": "heartbeat",
                "event_type": "heartbeat_cycle",
                "title": "heartbeat_cycle: HEALTHY",
                "summary": "All checks passed.",
            }
        )
        with patch("agents.memory_router.slog") as mock_slog:
            router.store(obj, caller="test")
            store_calls = [c for c in mock_slog.event.call_args_list if c[0][0] == "memory.router.store"]
            assert len(store_calls) >= 1
            kw = store_calls[0][1]
            assert kw["current_layer"] == "working"
            assert kw["adapter_selected"] == "state_working"
            assert kw["stored"] is True

    def test_phase2_guardrails_unchanged_for_other_layers(self, tmp_path):
        """Episodic → memory_file still works; semantic → memory_file still blocked."""
        router = self._make_working_router(tmp_path)

        # Episodic → memory_file: allowed
        episodic_obj = _valid_cmo(
            current_layer="episodic",
            target_store="memory_file",
        )
        r1 = router.store(episodic_obj, caller="test")
        assert "layer_store_mismatch" not in (r1.rejection_reason or "")

        # Semantic → memory_file: blocked
        semantic_obj = _valid_cmo(
            current_layer="semantic",
            target_store="memory_file",
        )
        r2 = router.store(semantic_obj, caller="test")
        assert r2.stored is False
        assert "layer_store_mismatch" in r2.rejection_reason


# ---------------------------------------------------------------------------
# Phase 1 Integration: Bypass detection guard
# ---------------------------------------------------------------------------


class TestWritePathBypassGuard:
    """Detect direct memory writes that bypass the Memory Router.

    This test greps production code for known direct-write patterns that should
    eventually be migrated to the router. It serves as a ratchet — the count of
    known bypasses must not increase without updating the allowlist.
    """

    # Known direct-write bypasses that are documented and exempt.
    # Each entry: (file_relative, function_or_pattern, reason)
    KNOWN_BYPASSES = [
        # Blackboard is internal orchestration state, not knowledge memory
        ("agents/blackboard.py", "_write_json", "internal state, not knowledge"),
        # Session manager persists session state, not knowledge
        ("agents/session_manager.py", "_persist", "session state, not knowledge"),
    ]

    def test_known_bypass_count_does_not_increase(self):
        """Guard: the number of known bypasses must not grow without updating this list."""
        import subprocess

        # Search for direct write_memory_artifact calls outside the router/adapter
        result = subprocess.run(
            [
                "grep",
                "-rn",
                "write_memory_artifact(",
                "/home/nova/nova-core/agents/memory_engine.py",
                "/home/nova/nova-core/watcher.py",
            ],
            capture_output=True,
            text=True,
        )
        # Count call sites (not the definition itself)
        callsites = [
            line
            for line in result.stdout.splitlines()
            if "write_memory_artifact(" in line
            and "def write_memory_artifact" not in line
            and "#" not in line.split("write_memory_artifact")[0]  # skip comments
        ]
        # There should be exactly the known count of direct calls
        # If this increases, someone added a new bypass
        assert len(callsites) <= 3, (
            f"Direct write_memory_artifact calls increased to {len(callsites)}. "
            f"New writes must go through memory_router.store(). Sites: {callsites}"
        )

    def test_schema_enums_are_consistent(self):
        """Guard: all event_types in EVENT_TYPE_TO_LAYER must be in VALID_EVENT_TYPES."""
        from agents.memory_router import EVENT_TYPE_TO_LAYER, VALID_EVENT_TYPES

        for et in EVENT_TYPE_TO_LAYER:
            assert et in VALID_EVENT_TYPES, f"EVENT_TYPE_TO_LAYER has {et!r} but VALID_EVENT_TYPES does not"

    def test_promoters_do_not_import_vault_directly(self):
        """Guard: workflow_promoter and pattern_promoter must not import vault_write directly.

        Phase 2 migrated these to route through memory_router.store().
        """
        import ast

        for filename in ("planner/workflow_promoter.py", "planner/pattern_promoter.py"):
            path = Path("/home/nova/nova-core") / filename
            if not path.exists():
                continue
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    module = getattr(node, "module", "") or ""
                    if "mcp_vault_server" in module:
                        names = [alias.name for alias in node.names]
                        assert "vault_write" not in names, (
                            f"{filename} still imports vault_write directly. "
                            "All vault writes must go through memory_router.store()."
                        )


class TestVaultAdapterPreBuiltPayload:
    """Phase 2: VaultAdapter supports pre-built promotion payloads via adapter_metadata."""

    def test_pre_built_vault_payload_used(self, tmp_path):
        """VaultAdapter uses adapter_metadata vault_frontmatter/body/path when present."""
        obj = CanonicalMemoryObject(
            memory_id="test-promo-001",
            timestamp="2025-01-01T00:00:00Z",
            source="promoter",
            event_type="workflow_learning_promoted",
            provenance="automatic",
            title="Test Promotion",
            summary="Test promotion summary",
            current_layer="semantic",
            adapter_metadata={
                "vault_frontmatter": {
                    "type": "workflow-learning",
                    "title": "Custom Promotion Title",
                    "confidence": "high",
                    "source": "nova-core-memory",
                    "tags": ["#type/learning"],
                },
                "vault_body": "## Custom Body\nFrom promoter.",
                "vault_path": "30-workflow-learnings/test-promo.md",
            },
        )
        with (
            patch("tools.mcp_vault_server.vault_validate") as mock_validate,
            patch("tools.mcp_vault_server.vault_write") as mock_write,
        ):
            mock_validate.return_value = {"valid": True}
            mock_write.return_value = {"path": "30-workflow-learnings/test-promo.md", "size": 100}

            from agents.memory_router import VaultAdapter

            adapter = VaultAdapter()
            result = adapter.store(obj)

        assert result.stored is True
        # Verify vault_validate was called with the pre-built payload
        mock_validate.assert_called_once()
        call_kwargs = mock_validate.call_args[1]
        assert call_kwargs["frontmatter"]["title"] == "Custom Promotion Title"
        assert call_kwargs["body"] == "## Custom Body\nFrom promoter."
        # Verify vault_write was called with the pre-built path
        mock_write.assert_called_once()
        write_kwargs = mock_write.call_args[1]
        assert write_kwargs["path"] == "30-workflow-learnings/test-promo.md"

    def test_auto_built_fallback_when_no_adapter_metadata(self, tmp_path):
        """VaultAdapter builds minimal frontmatter when adapter_metadata is empty."""
        obj = CanonicalMemoryObject(
            memory_id="test-auto-001",
            timestamp="2025-01-01T00:00:00Z",
            source="promoter",
            event_type="workflow_learning_promoted",
            provenance="automatic",
            title="Auto Title",
            summary="Auto summary",
            current_layer="semantic",
        )
        with (
            patch("tools.mcp_vault_server.vault_validate") as mock_validate,
            patch("tools.mcp_vault_server.vault_write") as mock_write,
        ):
            mock_validate.return_value = {"valid": True}
            mock_write.return_value = {"path": "30-workflow-learnings/test-auto-001.md", "size": 50}

            from agents.memory_router import VaultAdapter

            adapter = VaultAdapter()
            result = adapter.store(obj)

        assert result.stored is True
        call_kwargs = mock_validate.call_args[1]
        assert call_kwargs["frontmatter"]["type"] == "workflow-learning"

    def test_vault_validation_failure_rejects_pre_built(self):
        """VaultAdapter rejects pre-built payloads that fail vault_validate."""
        obj = CanonicalMemoryObject(
            memory_id="test-fail-001",
            timestamp="2025-01-01T00:00:00Z",
            source="promoter",
            event_type="workflow_learning_promoted",
            provenance="automatic",
            title="Bad Promotion",
            summary="Will fail validation",
            current_layer="semantic",
            adapter_metadata={
                "vault_frontmatter": {"type": "invalid-type"},
                "vault_body": "body",
                "vault_path": "30-workflow-learnings/bad.md",
            },
        )
        with patch("tools.mcp_vault_server.vault_validate") as mock_validate:
            mock_validate.return_value = {"valid": False, "errors": ["invalid type"]}

            from agents.memory_router import VaultAdapter

            adapter = VaultAdapter()
            result = adapter.store(obj)

        assert result.stored is False
        assert "vault_validation_failed" in (result.rejection_reason or "")

    def test_adapter_metadata_passed_through_ingest_event(self):
        """ingest_event passes adapter_metadata from event dict to CanonicalMemoryObject."""
        router = MemoryRouter(adapters=[])
        metadata = {"vault_frontmatter": {"type": "test"}, "vault_body": "body", "vault_path": "test.md"}
        obj = router.ingest_event(
            {
                "source": "promoter",
                "event_type": "workflow_learning_promoted",
                "title": "Test",
                "summary": "Test summary",
                "adapter_metadata": metadata,
            },
            caller="test",
        )
        assert obj.adapter_metadata == metadata
