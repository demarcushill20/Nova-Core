"""Phase 10 — Memory Metrics, Health Reporting, Regression & Chaos Tests.

Tests for:
- Metrics collection and snapshot correctness
- Health dashboard/report generation
- Failure counter behavior
- Regression coverage for scoring, intent routing, schema validation,
  promotion rules, dedup, supersession, open loop behavior
- Chaos/failure-mode tests for routing under store failures
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agents.memory_metrics import (
    LayerCounts,
    MemoryMetrics,
    OpenLoopCounts,
    SlogCounters,
    collect_layer_counts,
    collect_metrics,
    collect_open_loop_counts,
    collect_slog_counters,
    generate_health_report,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = 1773700000.0


def _write_json(path: Path, data: dict, mtime: float | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))
    if mtime is not None:
        os.utime(str(path), (mtime, mtime))


def _write_slog(path: Path, events: list[dict]) -> None:
    """Write a structured log file with multiple events."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for event in events:
            f.write(json.dumps(event) + "\n")


def _make_slog_event(event_name: str, **data) -> dict:
    return {
        "ts": "2026-03-13T12:00:00Z",
        "event": event_name,
        "level": "info",
        "data": data,
    }


# ---------------------------------------------------------------------------
# Test: LayerCounts
# ---------------------------------------------------------------------------


class TestLayerCounts:
    """Layer count collection from file system."""

    def test_counts_from_files(self, tmp_path):
        state = tmp_path / "STATE"
        memory = tmp_path / "MEMORY"
        (state / "working_memory").mkdir(parents=True)
        (memory / "workflow_learnings").mkdir(parents=True)
        (memory / "agent_patterns").mkdir(parents=True)

        _write_json(state / "working_memory" / "wm_1.json", {})
        _write_json(state / "working_memory" / "wm_2.json", {})
        _write_json(memory / "workflow_learnings" / "mem_a_1.json", {})
        _write_json(memory / "agent_patterns" / "mem_p_1.json", {})

        lc = collect_layer_counts(state_dir=state, memory_dir=memory)
        assert lc.working == 2
        assert lc.episodic == 1
        assert lc.procedural == 1
        assert lc.semantic == 0  # Not measurable
        assert lc.total() == 4  # 2 working + 1 episodic + 0 semantic + 1 procedural

    def test_empty_dirs(self, tmp_path):
        lc = collect_layer_counts(state_dir=tmp_path, memory_dir=tmp_path)
        assert lc.total() == 0

    def test_to_dict_includes_total(self, tmp_path):
        lc = LayerCounts(working=5, episodic=10)
        d = lc.to_dict()
        assert d["total"] == 15


# ---------------------------------------------------------------------------
# Test: OpenLoopCounts
# ---------------------------------------------------------------------------


class TestOpenLoopCounts:
    """Open loop status counting."""

    def test_counts_by_status(self, tmp_path):
        state = tmp_path / "STATE"
        loop_dir = state / "open_loops"
        loop_dir.mkdir(parents=True)

        _write_json(loop_dir / "loop_1.json", {"status": "open", "loop_id": "1"})
        _write_json(loop_dir / "loop_2.json", {"status": "open", "loop_id": "2"})
        _write_json(loop_dir / "loop_3.json", {"status": "resolved", "loop_id": "3"})
        _write_json(loop_dir / "loop_4.json", {"status": "stale", "loop_id": "4"})

        olc = collect_open_loop_counts(state_dir=state)
        assert olc.open == 2
        assert olc.resolved == 1
        assert olc.stale == 1
        assert olc.active() == 2
        assert olc.terminal() == 1
        assert olc.total() == 4

    def test_empty_dir(self, tmp_path):
        olc = collect_open_loop_counts(state_dir=tmp_path)
        assert olc.total() == 0


# ---------------------------------------------------------------------------
# Test: SlogCounters
# ---------------------------------------------------------------------------


class TestSlogCounters:
    """Structured log event counter extraction."""

    def test_store_counters(self, tmp_path):
        log = tmp_path / "structured.jsonl"
        _write_slog(
            log,
            [
                _make_slog_event("memory.router.store", stored=True),
                _make_slog_event("memory.router.store", stored=True, promotion_eligibility="eligible"),
                _make_slog_event("memory.router.store", stored=False),
                _make_slog_event("memory.router.store", stored=False, validation_errors=["bad"]),
            ],
        )
        sc = collect_slog_counters(log_path=log)
        assert sc.store_calls == 4
        assert sc.store_successes == 2
        assert sc.store_rejections == 2
        assert sc.promotion_eligible == 1
        assert sc.schema_validation_failures == 1

    def test_store_all_rejection_paths_counted(self, tmp_path):
        """Every early rejection path emits stored=False and is counted."""
        log = tmp_path / "structured.jsonl"
        _write_slog(
            log,
            [
                # validation_failed
                _make_slog_event("memory.router.store", stored=False, rejection_reason="validation_failed"),
                # evaluation_rejected
                _make_slog_event(
                    "memory.router.store", stored=False, rejection_reason="evaluation_rejected: low_importance"
                ),
                # layer_store_mismatch
                _make_slog_event(
                    "memory.router.store", stored=False, rejection_reason="layer_store_mismatch: working->memory_file"
                ),
                # no adapter
                _make_slog_event("memory.router.store", stored=False, rejection_reason="no adapter for fusion_memory"),
                # discard
                _make_slog_event("memory.router.store", stored=False, rejection_reason="discarded"),
                # success
                _make_slog_event("memory.router.store", stored=True),
            ],
        )
        sc = collect_slog_counters(log_path=log)
        assert sc.store_calls == 6
        assert sc.store_successes == 1
        assert sc.store_rejections == 5
        assert sc.store_calls == sc.store_successes + sc.store_rejections

    def test_recall_counters_legacy_total_found(self, tmp_path):
        """Backward compat: collector reads legacy 'total_found' field."""
        log = tmp_path / "structured.jsonl"
        _write_slog(
            log,
            [
                _make_slog_event("memory.router.recall", total_found=3),
                _make_slog_event("memory.router.recall", total_found=0),
                _make_slog_event("memory.router.recall", total_found=1),
            ],
        )
        sc = collect_slog_counters(log_path=log)
        assert sc.recall_calls == 3
        assert sc.recall_with_results == 2
        assert sc.recall_empty == 1
        assert sc.recall_success_rate() == pytest.approx(2 / 3, abs=0.01)

    def test_recall_counters_results_count(self, tmp_path):
        """Primary path: collector reads router-emitted 'results_count' field."""
        log = tmp_path / "structured.jsonl"
        _write_slog(
            log,
            [
                _make_slog_event("memory.router.recall", results_count=5),
                _make_slog_event("memory.router.recall", results_count=0),
                _make_slog_event("memory.router.recall", results_count=1),
                _make_slog_event("memory.router.recall", results_count=0),
            ],
        )
        sc = collect_slog_counters(log_path=log)
        assert sc.recall_calls == 4
        assert sc.recall_with_results == 2
        assert sc.recall_empty == 2
        assert sc.recall_success_rate() == pytest.approx(0.5)

    def test_recall_counters_mixed_fields(self, tmp_path):
        """Handles logs with a mix of old and new field names."""
        log = tmp_path / "structured.jsonl"
        _write_slog(
            log,
            [
                _make_slog_event("memory.router.recall", results_count=3),
                _make_slog_event("memory.router.recall", total_found=2),
                _make_slog_event("memory.router.recall", results_count=0),
            ],
        )
        sc = collect_slog_counters(log_path=log)
        assert sc.recall_calls == 3
        assert sc.recall_with_results == 2
        assert sc.recall_empty == 1

    def test_open_loop_counters(self, tmp_path):
        log = tmp_path / "structured.jsonl"
        _write_slog(
            log,
            [
                _make_slog_event("memory.router.track_open_loop", loop_action="loop_created"),
                _make_slog_event("memory.router.detect_open_loop", loop_action="loop_created"),
                _make_slog_event("memory.router.resolve_open_loop", loop_action="loop_resolved"),
            ],
        )
        sc = collect_slog_counters(log_path=log)
        assert sc.open_loops_created == 2
        assert sc.open_loops_resolved == 1

    def test_governance_and_compaction(self, tmp_path):
        log = tmp_path / "structured.jsonl"
        _write_slog(
            log,
            [
                _make_slog_event("memory.governance.sweep"),
                _make_slog_event("memory.governance.sweep"),
                _make_slog_event("memory.compaction.sweep"),
            ],
        )
        sc = collect_slog_counters(log_path=log)
        assert sc.governance_sweeps == 2
        assert sc.compaction_sweeps == 1

    def test_empty_log(self, tmp_path):
        log = tmp_path / "structured.jsonl"
        log.write_text("")
        sc = collect_slog_counters(log_path=log)
        assert sc.store_calls == 0

    def test_missing_log(self, tmp_path):
        sc = collect_slog_counters(log_path=tmp_path / "nonexistent.jsonl")
        assert sc.store_calls == 0

    def test_rates(self):
        sc = SlogCounters(
            store_calls=100, store_rejections=10, promotion_eligible=25, recall_calls=50, recall_with_results=40
        )
        assert sc.rejection_rate() == pytest.approx(0.10)
        assert sc.recall_success_rate() == pytest.approx(0.80)
        assert sc.promotion_rate() == pytest.approx(0.25)

    def test_zero_division_rates(self):
        sc = SlogCounters()
        assert sc.rejection_rate() == 0.0
        assert sc.recall_success_rate() == 0.0
        assert sc.promotion_rate() == 0.0


# ---------------------------------------------------------------------------
# Test: Full metrics collection
# ---------------------------------------------------------------------------


class TestMetricsCollection:
    """End-to-end metrics snapshot."""

    def test_collect_all(self, tmp_path):
        state = tmp_path / "STATE"
        memory = tmp_path / "MEMORY"
        logs = tmp_path / "LOGS"

        (state / "working_memory").mkdir(parents=True)
        (state / "sessions").mkdir(parents=True)
        (state / "open_loops").mkdir(parents=True)
        (memory / "workflow_learnings").mkdir(parents=True)
        (memory / "agent_patterns").mkdir(parents=True)
        logs.mkdir(parents=True)

        _write_json(state / "working_memory" / "wm_1.json", {})
        _write_json(memory / "workflow_learnings" / "mem_a_1.json", {})
        _write_json(state / "open_loops" / "loop_1.json", {"status": "open", "loop_id": "1"})
        _write_slog(
            logs / "structured.jsonl",
            [
                _make_slog_event("memory.router.store", stored=True),
            ],
        )

        metrics = collect_metrics(state_dir=state, memory_dir=memory, logs_dir=logs)
        assert metrics.layer_counts.working == 1
        assert metrics.layer_counts.episodic == 1
        assert metrics.open_loop_counts.open == 1
        assert metrics.slog_counters.store_calls == 1
        assert metrics.file_counts["working_memory"] == 1
        assert len(metrics.blind_spots) > 0
        assert metrics.collected_at

    def test_to_dict(self, tmp_path):
        metrics = collect_metrics(state_dir=tmp_path, memory_dir=tmp_path, logs_dir=tmp_path)
        d = metrics.to_dict()
        assert "layer_counts" in d
        assert "slog_counters" in d
        assert "blind_spots" in d


# ---------------------------------------------------------------------------
# Test: Health report generation
# ---------------------------------------------------------------------------


class TestHealthReport:
    """Dashboard / report generation."""

    def test_generates_markdown(self, tmp_path):
        metrics = MemoryMetrics(
            collected_at="2026-03-13T12:00:00Z",
            layer_counts=LayerCounts(working=5, episodic=20, procedural=2),
            open_loop_counts=OpenLoopCounts(open=3, resolved=1),
            slog_counters=SlogCounters(store_calls=100, store_rejections=5, recall_calls=50, recall_with_results=40),
            file_counts={"workflow_learnings": 20, "working_memory": 5},
            blind_spots=["Test blind spot"],
        )
        report = generate_health_report(metrics)
        assert "# Memory Health Dashboard" in report
        assert "Layer Distribution" in report
        assert "| Working | 5 |" in report
        assert "| Episodic | 20 |" in report
        assert "5.0%" in report  # rejection rate
        assert "80.0%" in report  # recall success rate
        assert "Test blind spot" in report

    def test_handles_zero_counts(self):
        report = generate_health_report(MemoryMetrics(collected_at="2026-01-01T00:00:00Z"))
        assert "0.0%" in report
        assert "# Memory Health Dashboard" in report


# ---------------------------------------------------------------------------
# Test: Router integration
# ---------------------------------------------------------------------------


class TestRouterMetricsIntegration:
    """Router.collect_metrics() integration."""

    def test_router_collect_metrics(self, tmp_path):
        from agents.memory_router import MemoryFileAdapter, MemoryRouter, WorkingMemoryAdapter

        (tmp_path / "workflow_learnings").mkdir(parents=True)
        router = MemoryRouter(
            adapters=[
                MemoryFileAdapter(base=tmp_path),
                WorkingMemoryAdapter(base=tmp_path),
            ],
            memory_file_base=tmp_path,
            loop_store_base=tmp_path / "open_loops",
        )
        result = router.collect_metrics(caller="test")
        assert "layer_counts" in result
        assert "slog_counters" in result


# ===========================================================================
# REGRESSION TESTS (Step 10.4)
# ===========================================================================


class TestScoringThresholdRegression:
    """Regression: scoring thresholds produce expected decisions."""

    def test_low_importance_downgraded(self):
        from agents.memory_evaluator import evaluate_candidate
        from agents.memory_router import CanonicalMemoryObject

        obj = CanonicalMemoryObject(
            memory_id="cm-test-1",
            timestamp="2026-03-13T00:00:00Z",
            source="heartbeat",
            event_type="heartbeat_cycle",
            provenance="automatic",
            title="Heartbeat check",
            summary="ok",
            confidence="low",
            current_layer="episodic",
        )
        result = evaluate_candidate(obj)
        # Transient + low confidence + thin summary → should downgrade to working
        assert result.action in ("working_only", "reject")

    def test_high_importance_kept(self):
        from agents.memory_evaluator import evaluate_candidate
        from agents.memory_router import CanonicalMemoryObject

        obj = CanonicalMemoryObject(
            memory_id="cm-test-2",
            timestamp="2026-03-13T00:00:00Z",
            source="watcher",
            event_type="task_completed",
            provenance="automatic",
            title="Deploy production service",
            summary="Deployed auth service to prod with zero downtime, all tests passing.",
            confidence="high",
            current_layer="episodic",
        )
        result = evaluate_candidate(obj)
        assert result.action in ("keep_in_current_layer", "keep_and_mark_promotable")


class TestIntentRoutingRegression:
    """Regression: intent classification routes correctly."""

    def test_temporal_query_classified(self):
        from agents.recall_intent import classify_intent

        result = classify_intent("when did we recently change the auth timeline?")
        assert result.intent == "temporal_recall"

    def test_decision_query_classified(self):
        from agents.recall_intent import classify_intent

        result = classify_intent("why did we decide to use PostgreSQL?")
        assert result.intent == "decision_recall"

    def test_procedural_query_classified(self):
        from agents.recall_intent import classify_intent

        result = classify_intent("how do we deploy to staging?")
        assert result.intent == "procedural_recall"


class TestSchemaValidationRegression:
    """Regression: schema validation catches bad input."""

    def test_missing_required_fields(self):
        from agents.memory_router import CanonicalMemoryObject, validate_canonical_object

        # Create object with empty required fields
        obj = CanonicalMemoryObject(
            memory_id="",
            timestamp="",
            source="",
            event_type="",
            provenance="",
            title="",
            summary="",
            confidence="",
        )
        valid, errors = validate_canonical_object(obj)
        assert not valid
        assert len(errors) > 0

    def test_valid_object_passes(self):
        from agents.memory_router import CanonicalMemoryObject, validate_canonical_object

        obj = CanonicalMemoryObject(
            memory_id="cm-test-1",
            timestamp="2026-03-13T00:00:00Z",
            source="watcher",
            event_type="task_completed",
            provenance="automatic",
            title="Test task",
            summary="Test summary",
            confidence="medium",
        )
        valid, errors = validate_canonical_object(obj)
        assert valid
        assert len(errors) == 0

    def test_bad_source_rejected(self):
        from agents.memory_router import CanonicalMemoryObject, validate_canonical_object

        obj = CanonicalMemoryObject(
            memory_id="cm-test-1",
            timestamp="2026-03-13T00:00:00Z",
            source="invalid_source",
            event_type="task_completed",
            provenance="automatic",
            title="Test",
            summary="Test",
            confidence="medium",
        )
        valid, errors = validate_canonical_object(obj)
        assert not valid
        assert any("source" in e for e in errors)


class TestPromotionRulesRegression:
    """Regression: promotion eligibility is gated correctly."""

    def test_low_scores_not_promotable(self):
        from agents.memory_evaluator import evaluate_candidate
        from agents.memory_router import CanonicalMemoryObject

        obj = CanonicalMemoryObject(
            memory_id="cm-test-1",
            timestamp="2026-03-13T00:00:00Z",
            source="heartbeat",
            event_type="heartbeat_cycle",
            provenance="automatic",
            title="Heartbeat",
            summary="System healthy, no issues detected",
            confidence="low",
            current_layer="episodic",
        )
        result = evaluate_candidate(obj)
        # Low importance transient event should not be promotable
        assert result.promotion_eligibility != "eligible" or result.action == "working_only"


class TestOpenLoopRegression:
    """Regression: open loop lifecycle behavior."""

    def test_create_and_resolve(self, tmp_path):
        from agents.open_loop_tracker import LoopStore, create_loop, resolve_loop

        store = LoopStore(base=tmp_path / "loops")
        result = create_loop(
            title="Fix deployment script",
            summary="The deploy script crashes on staging when Redis is down.",
            source="watcher",
            store=store,
        )
        assert result.action == "loop_created"
        loop = result.loop

        res = resolve_loop(
            loop,
            closure_reason="Fixed Redis connection handling in deploy script",
            store=store,
        )
        assert res.action == "loop_resolved"
        assert res.new_status == "resolved"

    def test_duplicate_rejected(self, tmp_path):
        from agents.open_loop_tracker import LoopStore, create_loop

        store = LoopStore(base=tmp_path / "loops")
        create_loop(title="Fix the bug", summary="Some detailed description here", source="test", store=store)
        result2 = create_loop(title="Fix the bug", summary="Another description here", source="test", store=store)
        assert result2.action == "loop_rejected_duplicate"


class TestDedupSupersessionRegression:
    """Regression: dedup and supersession behavior."""

    def test_exact_dup_detected(self, tmp_path):
        from agents.memory_compactor import MatchClass, detect_duplicates

        wl = tmp_path / "wl"
        data = {"event_type": "task_completed", "title": "Deploy", "summary": "Done", "source": "watcher"}
        _write_json(wl / "a.json", data)
        _write_json(wl / "b.json", data, mtime=_NOW - 86400)

        matches = detect_duplicates(
            [
                (wl / "a.json", data),
                (wl / "b.json", data),
            ]
        )
        exact = [m for m in matches if m.match_class == MatchClass.EXACT_DUPLICATE]
        assert len(exact) == 1

    def test_supersession_sets_status(self, tmp_path):
        from agents.memory_compactor import supersede_artifact

        wl = tmp_path / "wl"
        _write_json(wl / "new.json", {"artifact_id": "new", "workflow_id": "wf"})
        _write_json(wl / "old.json", {"artifact_id": "old", "workflow_id": "wf"}, mtime=_NOW - 86400)

        result = supersede_artifact(wl / "new.json", wl / "old.json", dry_run=False)
        assert result.action == "superseded"

        old = json.loads((wl / "old.json").read_text())
        assert old["promotion_status"] == "superseded"


# ===========================================================================
# CHAOS / FAILURE-MODE TESTS (Step 10.5)
# ===========================================================================


class TestChaosStoreFailures:
    """Simulate store backend failures during routing."""

    def test_fusion_store_unavailable(self, tmp_path):
        """Simulate Fusion Memory being unreachable."""
        from agents.memory_router import (
            CanonicalMemoryObject,
            MemoryFileAdapter,
            MemoryRouter,
            WorkingMemoryAdapter,
        )

        (tmp_path / "workflow_learnings").mkdir(parents=True)
        router = MemoryRouter(
            adapters=[
                MemoryFileAdapter(base=tmp_path),
                WorkingMemoryAdapter(base=tmp_path),
            ],
            memory_file_base=tmp_path,
            loop_store_base=tmp_path / "open_loops",
        )
        # FusionMemoryAdapter is not in the adapter list → store should
        # fall back to file adapter or reject gracefully
        obj = CanonicalMemoryObject(
            memory_id="cm-test-chaos-1",
            timestamp="2026-03-13T00:00:00Z",
            source="watcher",
            event_type="task_completed",
            provenance="automatic",
            title="Test task during chaos",
            summary="This should handle missing Fusion Memory gracefully.",
            confidence="medium",
        )
        result = router.store(obj, caller="chaos_test")
        # Should either store to fallback or reject cleanly — not crash
        assert result.stored or result.rejection_reason

    def test_vault_write_rejected(self, tmp_path):
        """Simulate Obsidian vault refusing a write."""
        from agents.memory_router import (
            MemoryFileAdapter,
            MemoryRouter,
            WorkingMemoryAdapter,
        )

        (tmp_path / "workflow_learnings").mkdir(parents=True)

        # Create a VaultAdapter that always fails
        class FailingVaultAdapter:
            name = "obsidian_vault"

            def recall(self, query, **kw):
                return []

            def store(self, obj):
                raise ConnectionError("Vault write rejected")

            def is_available(self):
                return False

        router = MemoryRouter(
            adapters=[
                MemoryFileAdapter(base=tmp_path),
                WorkingMemoryAdapter(base=tmp_path),
                FailingVaultAdapter(),
            ],
            memory_file_base=tmp_path,
            loop_store_base=tmp_path / "open_loops",
        )
        # Router should not use the failing adapter for normal store
        # (VaultAdapter is only used for vault-targeted stores)
        from agents.memory_router import CanonicalMemoryObject

        obj = CanonicalMemoryObject(
            memory_id="cm-test-chaos-2",
            timestamp="2026-03-13T00:00:00Z",
            source="watcher",
            event_type="task_completed",
            provenance="automatic",
            title="Test with broken vault",
            summary="Should store to file adapter instead.",
            confidence="medium",
        )
        result = router.store(obj, caller="chaos_test")
        assert result.stored or result.rejection_reason

    def test_malformed_event_handled(self, tmp_path):
        """Malformed event dict should not crash ingest."""
        from agents.memory_router import MemoryFileAdapter, MemoryRouter, WorkingMemoryAdapter

        (tmp_path / "workflow_learnings").mkdir(parents=True)
        router = MemoryRouter(
            adapters=[
                MemoryFileAdapter(base=tmp_path),
                WorkingMemoryAdapter(base=tmp_path),
            ],
            memory_file_base=tmp_path,
            loop_store_base=tmp_path / "open_loops",
        )
        # Ingest with minimal/broken data — should raise ValueError for missing fields
        with pytest.raises(ValueError, match="missing required fields"):
            router.ingest_event(
                {"event_type": "???", "title": "", "summary": ""},
                caller="chaos_test",
            )

        # Ingest with required fields but garbage values — should reject, not crash
        result = router.ingest_event(
            {"event_type": "???", "source": "test", "title": "x", "summary": "x"},
            caller="chaos_test",
        )
        assert result is not None

    def test_duplicated_event_storm(self, tmp_path):
        """Simulate rapid duplicate events."""
        from agents.memory_router import MemoryFileAdapter, MemoryRouter, WorkingMemoryAdapter

        (tmp_path / "workflow_learnings").mkdir(parents=True)
        router = MemoryRouter(
            adapters=[
                MemoryFileAdapter(base=tmp_path),
                WorkingMemoryAdapter(base=tmp_path),
            ],
            memory_file_base=tmp_path,
            loop_store_base=tmp_path / "open_loops",
        )
        results = []
        for _i in range(20):
            r = router.ingest_event(
                {
                    "event_type": "heartbeat_cycle",
                    "title": "System heartbeat",
                    "summary": "Everything healthy, no issues.",
                    "source": "heartbeat",
                    "stem": "hb",
                },
                caller="storm_test",
            )
            results.append(r)

        # Should handle all without crashing. Many may be stored to working
        # layer, some may be rejected, but none should error.
        assert len(results) == 20
        assert all(r is not None for r in results)

    def test_low_confidence_conflicting_memories(self, tmp_path):
        """Store conflicting low-confidence memories — both should be stored
        since dedup happens at compaction time, not store time."""
        from agents.memory_router import MemoryFileAdapter, MemoryRouter, WorkingMemoryAdapter

        (tmp_path / "workflow_learnings").mkdir(parents=True)
        router = MemoryRouter(
            adapters=[
                MemoryFileAdapter(base=tmp_path),
                WorkingMemoryAdapter(base=tmp_path),
            ],
            memory_file_base=tmp_path,
            loop_store_base=tmp_path / "open_loops",
        )
        r1 = router.ingest_event(
            {
                "event_type": "decision_made",
                "title": "Database choice",
                "summary": "We decided to use PostgreSQL for the main store.",
                "source": "watcher",
                "confidence": "low",
                "stem": "db_choice",
            },
            caller="conflict_test",
        )
        r2 = router.ingest_event(
            {
                "event_type": "decision_made",
                "title": "Database choice",
                "summary": "We decided to use MySQL for the main store.",
                "source": "watcher",
                "confidence": "low",
                "stem": "db_choice2",
            },
            caller="conflict_test",
        )
        # Both should process without crashing. Conflict detection is
        # a compaction-time concern, not a store-time concern.
        assert r1 is not None
        assert r2 is not None

    def test_governance_on_corrupted_files(self, tmp_path):
        """Governance should handle corrupted JSON gracefully."""
        from agents.memory_governance import GovernanceEngine

        state = tmp_path / "STATE"
        memory = tmp_path / "MEMORY"
        logs = tmp_path / "LOGS"
        state.mkdir()
        memory.mkdir()
        logs.mkdir()

        # Write corrupted files
        wl_dir = memory / "workflow_learnings"
        wl_dir.mkdir()
        (wl_dir / "mem_corrupt_1.json").write_text("not json at all {{{")
        (wl_dir / "mem_corrupt_2.json").write_text("")

        engine = GovernanceEngine(state_dir=state, memory_dir=memory, logs_dir=logs)
        summary = engine.run_sweep(dry_run=True)
        # Should not crash, corrupted files should be handled gracefully
        assert len(summary.errors) == 0  # Errors are per-workflow, not per-file

    def test_compaction_on_corrupted_files(self, tmp_path):
        """Compaction should handle corrupted JSON gracefully."""
        from agents.memory_compactor import CompactionEngine

        memory = tmp_path / "MEMORY"
        state = tmp_path / "STATE"
        memory.mkdir()
        state.mkdir()

        wl_dir = memory / "workflow_learnings"
        wl_dir.mkdir()
        (wl_dir / "mem_corrupt_1.json").write_text("{invalid")
        _write_json(
            wl_dir / "mem_good_1.json",
            {
                "artifact_id": "mem_good_1",
                "task_summary": "Good artifact",
                "task_class": "system",
                "source": "watcher",
            },
        )

        engine = CompactionEngine(memory_dir=memory, state_dir=state)
        summary = engine.run_compaction(dry_run=True)
        # Corrupted files are skipped, not crashed
        assert len(summary.errors) == 0


class TestStoreRejectionSlogCompleteness:
    """Verify every router store rejection path emits stored=False in slog."""

    def test_validation_rejection_emits_stored_false(self, tmp_path, monkeypatch):
        """Validation failure path emits stored=False."""
        from agents.memory_router import (
            CanonicalMemoryObject,
            MemoryFileAdapter,
            MemoryRouter,
            WorkingMemoryAdapter,
        )
        from utils.structured_log import slog as slog_instance

        (tmp_path / "workflow_learnings").mkdir(parents=True)
        logs = tmp_path / "LOGS"
        logs.mkdir()
        slog_path = logs / "structured.jsonl"

        # Redirect singleton slog output to tmp
        monkeypatch.setattr(slog_instance, "_path", slog_path)

        router = MemoryRouter(
            adapters=[MemoryFileAdapter(base=tmp_path), WorkingMemoryAdapter(base=tmp_path)],
            memory_file_base=tmp_path,
            loop_store_base=tmp_path / "open_loops",
        )
        # Object with empty required fields triggers validation rejection
        obj = CanonicalMemoryObject(
            memory_id="",
            timestamp="",
            source="",
            event_type="",
            provenance="",
            title="",
            summary="",
            confidence="",
        )
        result = router.store(obj, caller="test")
        assert result.stored is False

        # Check slog output has stored=False
        assert slog_path.exists()
        events = [json.loads(line) for line in slog_path.read_text().splitlines() if line.strip()]
        store_events = [e for e in events if e.get("event") == "memory.router.store"]
        assert len(store_events) >= 1
        last = store_events[-1]
        assert last["data"]["stored"] is False
        assert "validation_failed" in last["data"].get("rejection_reason", "")

    def test_no_adapter_rejection_emits_stored_false(self, tmp_path, monkeypatch):
        """No-adapter path emits stored=False."""
        from agents.memory_router import (
            CanonicalMemoryObject,
            MemoryRouter,
        )
        from utils.structured_log import slog as slog_instance

        (tmp_path / "workflow_learnings").mkdir(parents=True)
        logs = tmp_path / "LOGS"
        logs.mkdir()
        slog_path = logs / "structured.jsonl"

        monkeypatch.setattr(slog_instance, "_path", slog_path)

        # Router with NO adapters
        router = MemoryRouter(
            adapters=[],
            memory_file_base=tmp_path,
            loop_store_base=tmp_path / "open_loops",
        )
        obj = CanonicalMemoryObject(
            memory_id="cm-1",
            timestamp="2026-03-13T00:00:00Z",
            source="watcher",
            event_type="task_completed",
            provenance="automatic",
            title="Test",
            summary="Test summary",
            confidence="medium",
        )
        result = router.store(obj, caller="test")
        assert result.stored is False

        events = [json.loads(line) for line in slog_path.read_text().splitlines() if line.strip()]
        store_events = [e for e in events if e.get("event") == "memory.router.store"]
        assert len(store_events) >= 1
        assert store_events[-1]["data"]["stored"] is False
