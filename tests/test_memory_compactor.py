"""Phase 9 — Memory Compaction, Deduplication & Supersession Tests.

Tests for exact duplicate detection, near-duplicate handling, safe merge,
supersession metadata, provenance preservation, protection skip behavior,
compaction workflows, and router integration.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from agents.memory_compactor import (
    VALID_COMPACTION_ACTIONS,
    CompactionEngine,
    CompactionResult,
    CompactionSweepSummary,
    DedupMatch,
    MatchClass,
    _is_merge_protected,
    compact_group,
    content_hash,
    deduplicate_exact,
    detect_duplicates,
    run_compaction_sweep,
    supersede_artifact,
    title_key,
    workflow_key,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DAY = 86400
_NOW = 1773600000.0


def _write_json(path: Path, data: dict, mtime: float | None = None) -> None:
    """Write JSON and optionally set mtime."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))
    if mtime is not None:
        os.utime(str(path), (mtime, mtime))


def _make_artifact(
    path: Path,
    name: str = "mem_test_123.json",
    *,
    task_summary: str = "Completed the deployment task successfully.",
    task_class: str = "system",
    workflow_id: str = "test_wf",
    source: str = "watcher",
    confidence: str = "medium",
    age_days: float = 0,
    **extra: object,
) -> tuple[Path, dict]:
    """Write a mock workflow learning artifact and return (path, metadata)."""
    filepath = path / name
    mtime = _NOW - (age_days * _DAY)
    artifact_id = name.replace(".json", "")
    data = {
        "artifact_id": artifact_id,
        "workflow_id": workflow_id,
        "task_summary": task_summary,
        "task_class": task_class,
        "source": source,
        "confidence": confidence,
        "roles_involved": ["direct_worker"],
        "key_decisions": [],
        "successful_patterns": [],
        "failure_patterns": [],
        "verification_outcome": "approved",
        "reusable_guidance": "",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(mtime)),
        **extra,
    }
    _write_json(filepath, data, mtime=mtime)
    return filepath, data


def _make_wm(path: Path, name: str = "wm_test.json", age_days: float = 0, **extra: object) -> tuple[Path, dict]:
    """Write a mock working memory file."""
    filepath = path / name
    mtime = _NOW - (age_days * _DAY)
    data = {
        "event_type": "heartbeat_cycle",
        "title": "Heartbeat check",
        "summary": "System healthy",
        "source": "heartbeat",
        **extra,
    }
    _write_json(filepath, data, mtime=mtime)
    return filepath, data


def _make_engine(tmp_path: Path) -> tuple[CompactionEngine, Path, Path]:
    """Create engine with isolated test directories."""
    memory = tmp_path / "MEMORY"
    state = tmp_path / "STATE"
    memory.mkdir()
    state.mkdir()
    engine = CompactionEngine(memory_dir=memory, state_dir=state, now=_NOW)
    return engine, memory, state


# ---------------------------------------------------------------------------
# Test: Data structures
# ---------------------------------------------------------------------------


class TestCompactionStructures:
    """Validate data structures."""

    def test_valid_actions(self):
        expected = {
            "no_action",
            "deduplicated",
            "compacted",
            "superseded",
            "protected_skip",
            "rejected_ambiguous",
            "rejected_unsafe",
        }
        assert VALID_COMPACTION_ACTIONS == expected

    def test_dedup_match_to_dict(self):
        m = DedupMatch(
            match_class=MatchClass.EXACT_DUPLICATE,
            artifact_a_id="a1",
            artifact_b_id="b1",
            artifact_a_path="/a",
            artifact_b_path="/b",
            confidence="high",
            matching_fields=["content_hash"],
            reason="test",
        )
        d = m.to_dict()
        assert d["match_class"] == "exact_duplicate"
        assert d["confidence"] == "high"

    def test_compaction_result_to_dict(self):
        r = CompactionResult(
            action="deduplicated",
            target_store="workflow_learnings",
            surviving_path="/a",
            affected_paths=["/b"],
            rule_name="test_rule",
            dry_run=False,
        )
        d = r.to_dict()
        assert d["action"] == "deduplicated"
        assert d["dry_run"] is False

    def test_sweep_summary_to_dict(self):
        s = CompactionSweepSummary(
            sweep_id="compact-1",
            dry_run=True,
            started_at="2026-03-13T00:00:00Z",
        )
        d = s.to_dict()
        assert d["sweep_id"] == "compact-1"
        assert isinstance(d["results"], list)


# ---------------------------------------------------------------------------
# Test: Content hashing
# ---------------------------------------------------------------------------


class TestContentHashing:
    """Content hash and key generation."""

    def test_identical_items_same_hash(self):
        a = {"event_type": "task_completed", "title": "Deploy", "summary": "Done", "source": "watcher"}
        b = {"event_type": "task_completed", "title": "Deploy", "summary": "Done", "source": "watcher"}
        assert content_hash(a) == content_hash(b)

    def test_different_items_different_hash(self):
        a = {"event_type": "task_completed", "title": "Deploy", "summary": "Done", "source": "watcher"}
        b = {"event_type": "task_failed", "title": "Deploy", "summary": "Error", "source": "watcher"}
        assert content_hash(a) != content_hash(b)

    def test_title_key_normalized(self):
        a = {"title": "Fix Auth Bug", "related_project": "nova-core"}
        b = {"title": "fix auth bug", "related_project": "nova-core"}
        assert title_key(a) == title_key(b)

    def test_title_key_different_projects(self):
        a = {"title": "Fix Bug", "related_project": "nova-core"}
        b = {"title": "Fix Bug", "related_project": "other-project"}
        assert title_key(a) != title_key(b)

    def test_workflow_key_from_field(self):
        assert workflow_key({"workflow_id": "deploy_v2"}) == "deploy_v2"

    def test_workflow_key_from_artifact_id(self):
        assert workflow_key({"artifact_id": "mem_deploy_v2_123456"}) == "deploy_v2"

    def test_workflow_key_none_when_missing(self):
        assert workflow_key({"title": "no workflow"}) is None


# ---------------------------------------------------------------------------
# Test: Protection check
# ---------------------------------------------------------------------------


class TestMergeProtection:
    """Protection classification for merge/compaction."""

    def test_explicit_protected_flag(self, tmp_path):
        p = tmp_path / "test.json"
        protected, reason = _is_merge_protected(p, {"protected": True})
        assert protected is True
        assert reason == "explicit_protected_flag"

    def test_agent_patterns_protected(self, tmp_path):
        p = tmp_path / "agent_patterns" / "test.json"
        protected, _ = _is_merge_protected(p, {})
        assert protected is True

    def test_active_open_loop_protected(self, tmp_path):
        p = tmp_path / "open_loops" / "loop_1.json"
        protected, _ = _is_merge_protected(p, {"status": "open"})
        assert protected is True

    def test_operator_authored_protected(self, tmp_path):
        p = tmp_path / "test.json"
        protected, _ = _is_merge_protected(p, {"source": "operator"})
        assert protected is True

    def test_normal_artifact_not_protected(self, tmp_path):
        p = tmp_path / "workflow_learnings" / "test.json"
        protected, _ = _is_merge_protected(p, {"source": "watcher", "importance": 0.3})
        assert protected is False

    def test_none_metadata_protected(self, tmp_path):
        p = tmp_path / "test.json"
        protected, _ = _is_merge_protected(p, None)
        assert protected is True


# ---------------------------------------------------------------------------
# Test: Exact duplicate detection
# ---------------------------------------------------------------------------


class TestExactDuplicateDetection:
    """Detect exact duplicates by content hash."""

    def test_two_identical_artifacts(self, tmp_path):
        wl = tmp_path / "wl"
        a_path, a_meta = _make_artifact(wl, "mem_a_100.json", age_days=0)
        b_path, b_meta = _make_artifact(wl, "mem_b_200.json", age_days=5)

        matches = detect_duplicates([(a_path, a_meta), (b_path, b_meta)])
        exact = [m for m in matches if m.match_class == MatchClass.EXACT_DUPLICATE]
        assert len(exact) == 1
        assert exact[0].confidence == "high"

    def test_different_content_no_exact_match(self, tmp_path):
        wl = tmp_path / "wl"
        a_path, a_meta = _make_artifact(wl, "mem_a_100.json", task_summary="Deploy succeeded")
        b_path, b_meta = _make_artifact(wl, "mem_b_200.json", task_summary="Rollback failed")

        matches = detect_duplicates([(a_path, a_meta), (b_path, b_meta)])
        exact = [m for m in matches if m.match_class == MatchClass.EXACT_DUPLICATE]
        assert len(exact) == 0

    def test_single_artifact_no_matches(self, tmp_path):
        wl = tmp_path / "wl"
        a_path, a_meta = _make_artifact(wl, "mem_a_100.json")
        matches = detect_duplicates([(a_path, a_meta)])
        assert len(matches) == 0


# ---------------------------------------------------------------------------
# Test: Near-duplicate detection
# ---------------------------------------------------------------------------


class TestNearDuplicateDetection:
    """Detect near-duplicates by title key."""

    def test_same_title_different_content(self, tmp_path):
        wl = tmp_path / "wl"
        # Same task_summary (used as title key) but different other fields
        a_path, a_meta = _make_artifact(
            wl,
            "mem_a_100.json",
            task_summary="Deploy completed for staging environment",
            workflow_id="wf_a",
            confidence="high",
        )
        b_path, b_meta = _make_artifact(
            wl,
            "mem_b_200.json",
            task_summary="Deploy completed for staging environment",
            workflow_id="wf_b",
            confidence="low",
            source="heartbeat",  # different source → different content hash
            age_days=5,
        )

        matches = detect_duplicates([(a_path, a_meta), (b_path, b_meta)])
        near = [m for m in matches if m.match_class == MatchClass.NEAR_DUPLICATE]
        assert len(near) == 1
        assert "title_key" in near[0].matching_fields

    def test_different_event_types_rejected_ambiguous(self, tmp_path):
        wl = tmp_path / "wl"
        a_path, a_meta = _make_artifact(
            wl,
            "mem_a_100.json",
            task_class="system",
            workflow_id="wf_a",
        )
        b_path, b_meta = _make_artifact(
            wl,
            "mem_b_200.json",
            task_class="research",
            workflow_id="wf_b",
            age_days=5,
        )

        matches = detect_duplicates([(a_path, a_meta), (b_path, b_meta)])
        ambiguous = [m for m in matches if m.match_class == MatchClass.REJECTED_AMBIGUOUS]
        assert len(ambiguous) == 1
        assert "different event_types" in ambiguous[0].reason


# ---------------------------------------------------------------------------
# Test: Supersession candidate detection
# ---------------------------------------------------------------------------


class TestSupersessionDetection:
    """Detect supersession candidates by workflow_id."""

    def test_same_workflow_different_content(self, tmp_path):
        wl = tmp_path / "wl"
        a_path, a_meta = _make_artifact(
            wl,
            "mem_deploy_v2_100.json",
            task_summary="Deploy v2 completed with fixes",
            workflow_id="deploy_v2",
        )
        b_path, b_meta = _make_artifact(
            wl,
            "mem_deploy_v2_200.json",
            task_summary="Deploy v2 initial attempt",
            workflow_id="deploy_v2",
            age_days=5,
        )

        matches = detect_duplicates([(a_path, a_meta), (b_path, b_meta)])
        supersession = [m for m in matches if m.match_class == MatchClass.SUPERSESSION_CANDIDATE]
        assert len(supersession) == 1
        assert "workflow_id" in supersession[0].matching_fields


# ---------------------------------------------------------------------------
# Test: Supersession metadata
# ---------------------------------------------------------------------------


class TestSupersessionMetadata:
    """Supersession updates metadata correctly."""

    def test_supersede_sets_metadata(self, tmp_path):
        wl = tmp_path / "wl"
        a_path, _ = _make_artifact(wl, "mem_new_100.json", workflow_id="wf1")
        b_path, _ = _make_artifact(wl, "mem_old_200.json", workflow_id="wf1", age_days=5)

        result = supersede_artifact(a_path, b_path, dry_run=False)
        assert result.action == "superseded"
        assert result.provenance_links == ["mem_old_200"]

        # Check surviving artifact
        new_meta = json.loads(a_path.read_text())
        assert new_meta["supersedes"] == "mem_old_200"

        # Check superseded artifact
        old_meta = json.loads(b_path.read_text())
        assert old_meta["promotion_status"] == "superseded"
        assert old_meta["superseded_by"] == "mem_new_100"

    def test_supersede_dry_run_no_changes(self, tmp_path):
        wl = tmp_path / "wl"
        a_path, _ = _make_artifact(wl, "mem_new_100.json")
        b_path, _ = _make_artifact(wl, "mem_old_200.json", age_days=5)

        result = supersede_artifact(a_path, b_path, dry_run=True)
        assert result.action == "superseded"
        assert result.dry_run is True

        # Files should be unchanged
        old_meta = json.loads(b_path.read_text())
        assert old_meta.get("promotion_status") != "superseded"

    def test_supersede_protected_skips(self, tmp_path):
        wl = tmp_path / "wl"
        a_path, _ = _make_artifact(wl, "mem_new_100.json")
        b_path, _ = _make_artifact(wl, "mem_old_200.json", protected=True)

        result = supersede_artifact(a_path, b_path, dry_run=False)
        assert result.action == "protected_skip"

    def test_self_supersession_rejected(self, tmp_path):
        wl = tmp_path / "wl"
        a_path, _ = _make_artifact(wl, "mem_same_100.json")

        result = supersede_artifact(a_path, a_path, dry_run=False)
        assert result.action == "rejected_unsafe"
        assert result.rejection_reason == "self_supersession"

    def test_already_superseded_no_action(self, tmp_path):
        wl = tmp_path / "wl"
        a_path, _ = _make_artifact(wl, "mem_new_100.json", supersedes="mem_old_200")
        b_path, _ = _make_artifact(wl, "mem_old_200.json", age_days=5)

        result = supersede_artifact(a_path, b_path, dry_run=False)
        assert result.action == "no_action"

    def test_reverse_circular_supersession_rejected(self, tmp_path):
        """B already supersedes A — making A supersede B would create A↔B cycle."""
        wl = tmp_path / "wl"
        a_path, _ = _make_artifact(wl, "mem_a_100.json")
        b_path, _ = _make_artifact(wl, "mem_b_200.json", supersedes="mem_a_100", age_days=5)

        result = supersede_artifact(a_path, b_path, dry_run=False)
        assert result.action == "rejected_unsafe"
        assert result.rejection_reason == "circular_supersession"

    def test_already_superseded_by_other_rejected(self, tmp_path):
        """B is already superseded by C — making A supersede B would conflict."""
        wl = tmp_path / "wl"
        a_path, _ = _make_artifact(wl, "mem_a_100.json")
        b_path, _ = _make_artifact(
            wl,
            "mem_b_200.json",
            superseded_by="mem_c_300",
            promotion_status="superseded",
            age_days=5,
        )

        result = supersede_artifact(a_path, b_path, dry_run=False)
        assert result.action == "rejected_unsafe"
        assert result.rejection_reason == "already_superseded_by_other"


# ---------------------------------------------------------------------------
# Test: Exact dedup workflow
# ---------------------------------------------------------------------------


class TestExactDedupWorkflow:
    """End-to-end exact deduplication."""

    def test_dedup_archives_older(self, tmp_path):
        wl = tmp_path / "wl"
        a_path, a_meta = _make_artifact(wl, "mem_a_100.json", age_days=0)
        b_path, b_meta = _make_artifact(wl, "mem_b_200.json", age_days=5)

        match = DedupMatch(
            match_class=MatchClass.EXACT_DUPLICATE,
            artifact_a_id="mem_a_100",
            artifact_b_id="mem_b_200",
            artifact_a_path=str(a_path),
            artifact_b_path=str(b_path),
            confidence="high",
            matching_fields=["content_hash"],
            reason="Identical hash",
        )
        archive_dir = tmp_path / "_archive"
        result = deduplicate_exact(match, archive_dir=archive_dir, dry_run=False)

        assert result.action == "deduplicated"
        assert not b_path.exists()  # Older archived
        assert a_path.exists()  # Newer kept
        assert archive_dir.exists()

    def test_dedup_dry_run_preserves(self, tmp_path):
        wl = tmp_path / "wl"
        a_path, _ = _make_artifact(wl, "mem_a_100.json")
        b_path, _ = _make_artifact(wl, "mem_b_200.json", age_days=5)

        match = DedupMatch(
            match_class=MatchClass.EXACT_DUPLICATE,
            artifact_a_id="mem_a_100",
            artifact_b_id="mem_b_200",
            artifact_a_path=str(a_path),
            artifact_b_path=str(b_path),
            confidence="high",
            matching_fields=["content_hash"],
            reason="Identical hash",
        )
        result = deduplicate_exact(match, archive_dir=tmp_path / "_archive", dry_run=True)
        assert result.action == "deduplicated"
        assert b_path.exists()  # Still present

    def test_dedup_rejects_non_exact(self, tmp_path):
        match = DedupMatch(
            match_class=MatchClass.NEAR_DUPLICATE,
            artifact_a_id="a",
            artifact_b_id="b",
            artifact_a_path="/a",
            artifact_b_path="/b",
            confidence="medium",
            matching_fields=[],
            reason="near",
        )
        result = deduplicate_exact(match, archive_dir=tmp_path / "_archive", dry_run=False)
        assert result.action == "rejected_unsafe"


# ---------------------------------------------------------------------------
# Test: Compact group
# ---------------------------------------------------------------------------


class TestCompactGroup:
    """Group compaction for thin artifacts."""

    def test_compact_two_thin_artifacts(self, tmp_path):
        wl = tmp_path / "wl"
        a_path, a_meta = _make_artifact(wl, "mem_a_100.json", task_summary="ok", workflow_id="wf_a")
        b_path, b_meta = _make_artifact(wl, "mem_b_200.json", task_summary="done", workflow_id="wf_b", age_days=5)

        archive_dir = tmp_path / "_archive"
        result = compact_group(
            [(a_path, a_meta), (b_path, b_meta)],
            archive_dir=archive_dir,
            dry_run=False,
        )

        assert result.action == "compacted"
        assert len(result.provenance_links) == 1  # One archived

        # Survivor should have compacted_from
        survivor_path = Path(result.surviving_path)
        survivor_meta = json.loads(survivor_path.read_text())
        assert "compacted_from" in survivor_meta
        assert survivor_meta["provenance"] == "compaction"

    def test_compact_keeps_richest(self, tmp_path):
        wl = tmp_path / "wl"
        a_path, a_meta = _make_artifact(
            wl,
            "mem_a_100.json",
            task_summary="short",
            confidence="low",
        )
        b_path, b_meta = _make_artifact(
            wl,
            "mem_b_200.json",
            task_summary="This is a much longer and richer description of what happened",
            confidence="high",
            age_days=5,
        )

        result = compact_group(
            [(a_path, a_meta), (b_path, b_meta)],
            archive_dir=tmp_path / "_archive",
            dry_run=False,
        )

        assert result.action == "compacted"
        # b has longer summary + higher confidence, should survive
        assert result.surviving_path == str(b_path)

    def test_compact_single_item_no_action(self, tmp_path):
        wl = tmp_path / "wl"
        a_path, a_meta = _make_artifact(wl, "mem_a_100.json")
        result = compact_group(
            [(a_path, a_meta)],
            archive_dir=tmp_path / "_archive",
            dry_run=True,
        )
        assert result.action == "no_action"

    def test_compact_protected_skips(self, tmp_path):
        wl = tmp_path / "wl"
        a_path, a_meta = _make_artifact(wl, "mem_a_100.json", source="operator")
        b_path, b_meta = _make_artifact(wl, "mem_b_200.json")

        result = compact_group(
            [(a_path, a_meta), (b_path, b_meta)],
            archive_dir=tmp_path / "_archive",
            dry_run=False,
        )
        assert result.action == "protected_skip"


# ---------------------------------------------------------------------------
# Test: Full workflow — workflow learnings
# ---------------------------------------------------------------------------


class TestWorkflowLearningsCompaction:
    """End-to-end compaction of workflow learnings."""

    def test_exact_duplicates_deduped(self, tmp_path):
        engine, memory, _ = _make_engine(tmp_path)
        wl = memory / "workflow_learnings"
        _make_artifact(wl, "mem_a_100.json", age_days=0)
        _make_artifact(wl, "mem_b_200.json", age_days=5)

        results = engine.compact_workflow_learnings(dry_run=False)
        deduped = [r for r in results if r.action == "deduplicated"]
        assert len(deduped) == 1

    def test_supersession_candidates_processed(self, tmp_path):
        engine, memory, _ = _make_engine(tmp_path)
        wl = memory / "workflow_learnings"
        _make_artifact(
            wl,
            "mem_deploy_v2_100.json",
            task_summary="Deploy v2 final result",
            workflow_id="deploy_v2",
        )
        _make_artifact(
            wl,
            "mem_deploy_v2_200.json",
            task_summary="Deploy v2 initial run",
            workflow_id="deploy_v2",
            age_days=5,
        )

        results = engine.compact_workflow_learnings(dry_run=False)
        superseded = [r for r in results if r.action == "superseded"]
        assert len(superseded) == 1

    def test_no_artifacts_no_results(self, tmp_path):
        engine, _, _ = _make_engine(tmp_path)
        results = engine.compact_workflow_learnings(dry_run=True)
        assert len(results) == 0


# ---------------------------------------------------------------------------
# Test: Full workflow — working memory
# ---------------------------------------------------------------------------


class TestWorkingMemoryCompaction:
    """End-to-end dedup of working memory."""

    def test_exact_wm_duplicates_deduped(self, tmp_path):
        engine, _, state = _make_engine(tmp_path)
        wm = state / "working_memory"
        _make_wm(wm, "wm_a.json", age_days=0)
        _make_wm(wm, "wm_b.json", age_days=1)

        results = engine.compact_working_memory(dry_run=False)
        deduped = [r for r in results if r.action == "deduplicated"]
        assert len(deduped) == 1

    def test_different_wm_no_dedup(self, tmp_path):
        engine, _, state = _make_engine(tmp_path)
        wm = state / "working_memory"
        _make_wm(wm, "wm_a.json", summary="System healthy")
        _make_wm(wm, "wm_b.json", summary="System degraded", title="Alert")

        results = engine.compact_working_memory(dry_run=True)
        deduped = [r for r in results if r.action == "deduplicated"]
        assert len(deduped) == 0


# ---------------------------------------------------------------------------
# Test: Full sweep
# ---------------------------------------------------------------------------


class TestFullCompactionSweep:
    """End-to-end compaction sweep across all stores."""

    def test_sweep_aggregates_correctly(self, tmp_path):
        engine, memory, state = _make_engine(tmp_path)

        wl = memory / "workflow_learnings"
        _make_artifact(wl, "mem_a_100.json", age_days=0)
        _make_artifact(wl, "mem_b_200.json", age_days=5)

        wm = state / "working_memory"
        _make_wm(wm, "wm_a.json", age_days=0)
        _make_wm(wm, "wm_b.json", age_days=1)

        summary = engine.run_compaction(dry_run=True)
        assert summary.items_examined >= 2
        assert summary.duplicates_found >= 1
        assert summary.sweep_id.startswith("compact-")

    def test_empty_stores_no_errors(self, tmp_path):
        engine, _, _ = _make_engine(tmp_path)
        summary = engine.run_compaction(dry_run=True)
        assert summary.items_examined == 0
        assert len(summary.errors) == 0

    def test_dry_run_flag_propagated(self, tmp_path):
        engine, memory, _ = _make_engine(tmp_path)
        wl = memory / "workflow_learnings"
        _make_artifact(wl, "mem_a_100.json")
        _make_artifact(wl, "mem_b_200.json", age_days=5)

        summary = engine.run_compaction(dry_run=True)
        assert summary.dry_run is True
        for r in summary.results:
            assert r.dry_run is True


# ---------------------------------------------------------------------------
# Test: Bad merge prevention
# ---------------------------------------------------------------------------


class TestBadMergePrevention:
    """Safeguards against unsafe compaction/merge."""

    def test_different_event_types_rejected(self, tmp_path):
        engine, memory, _ = _make_engine(tmp_path)
        wl = memory / "workflow_learnings"
        _make_artifact(wl, "mem_a_100.json", task_class="system", workflow_id="wf_a")
        _make_artifact(wl, "mem_b_200.json", task_class="research", workflow_id="wf_b", age_days=5)

        results = engine.compact_workflow_learnings(dry_run=True)
        ambiguous = [r for r in results if r.action == "rejected_ambiguous"]
        assert len(ambiguous) == 1

    def test_protected_artifacts_skipped(self, tmp_path):
        engine, memory, _ = _make_engine(tmp_path)
        wl = memory / "workflow_learnings"
        _make_artifact(wl, "mem_a_100.json", source="operator", workflow_id="wf_a")
        _make_artifact(wl, "mem_b_200.json", source="operator", workflow_id="wf_a", age_days=5)

        results = engine.compact_workflow_learnings(dry_run=True)
        # Both are operator-authored → supersession should be protected_skip
        protected = [r for r in results if r.action == "protected_skip"]
        assert len(protected) >= 1

    def test_unreadable_metadata_rejected(self, tmp_path):
        a = tmp_path / "a.json"
        b = tmp_path / "b.json"
        a.write_text("not json")
        b.write_text("{}")
        result = supersede_artifact(a, b, dry_run=False)
        assert result.action == "rejected_unsafe"


# ---------------------------------------------------------------------------
# Test: Router integration
# ---------------------------------------------------------------------------


class TestRouterCompactionIntegration:
    """Router.run_compaction() integration."""

    def test_router_compaction_dry_run(self, tmp_path):
        from agents.memory_router import MemoryFileAdapter, MemoryRouter, WorkingMemoryAdapter

        (tmp_path / "workflow_learnings").mkdir(parents=True, exist_ok=True)
        router = MemoryRouter(
            adapters=[
                MemoryFileAdapter(base=tmp_path),
                WorkingMemoryAdapter(base=tmp_path),
            ],
            memory_file_base=tmp_path,
            loop_store_base=tmp_path / "open_loops",
        )
        result = router.run_compaction(dry_run=True, caller="test")
        assert result["dry_run"] is True
        assert "items_examined" in result

    def test_convenience_function(self, tmp_path):
        memory = tmp_path / "MEMORY"
        state = tmp_path / "STATE"
        memory.mkdir()
        state.mkdir()
        result = run_compaction_sweep(
            dry_run=True,
            memory_dir=memory,
            state_dir=state,
        )
        assert result["dry_run"] is True
        assert "results" in result


# ---------------------------------------------------------------------------
# Test: Provenance preservation
# ---------------------------------------------------------------------------


class TestProvenancePreservation:
    """Provenance links are correctly maintained."""

    def test_dedup_preserves_provenance_link(self, tmp_path):
        wl = tmp_path / "wl"
        a_path, a_meta = _make_artifact(wl, "mem_a_100.json", age_days=0)
        b_path, b_meta = _make_artifact(wl, "mem_b_200.json", age_days=5)

        match = DedupMatch(
            match_class=MatchClass.EXACT_DUPLICATE,
            artifact_a_id="mem_a_100",
            artifact_b_id="mem_b_200",
            artifact_a_path=str(a_path),
            artifact_b_path=str(b_path),
            confidence="high",
            matching_fields=["content_hash"],
            reason="Identical",
        )
        result = deduplicate_exact(match, archive_dir=tmp_path / "_archive", dry_run=False)
        assert result.provenance_links == ["mem_b_200"]

    def test_compaction_preserves_source_ids(self, tmp_path):
        wl = tmp_path / "wl"
        a_path, a_meta = _make_artifact(wl, "mem_a_100.json", task_summary="ok", workflow_id="wf_a")
        b_path, b_meta = _make_artifact(wl, "mem_b_200.json", task_summary="ok", workflow_id="wf_b", age_days=5)

        result = compact_group(
            [(a_path, a_meta), (b_path, b_meta)],
            archive_dir=tmp_path / "_archive",
            dry_run=False,
        )
        assert "mem_b_200" in result.provenance_links or "mem_a_100" in result.provenance_links

        # Verify survivor has compacted_from
        # The survivor is whichever was kept
        survivor = json.loads(Path(result.surviving_path).read_text())
        assert len(survivor["compacted_from"]) >= 1

    def test_supersession_chain_integrity(self, tmp_path):
        """Verify supersession creates a proper chain, not a cycle."""
        wl = tmp_path / "wl"
        v1_path, _ = _make_artifact(wl, "mem_v1_100.json", workflow_id="chain", age_days=10)
        v2_path, _ = _make_artifact(wl, "mem_v2_200.json", workflow_id="chain", age_days=5)
        v3_path, _ = _make_artifact(wl, "mem_v3_300.json", workflow_id="chain", age_days=0)

        # v2 supersedes v1
        r1 = supersede_artifact(v2_path, v1_path, dry_run=False)
        assert r1.action == "superseded"

        # v3 supersedes v2
        r2 = supersede_artifact(v3_path, v2_path, dry_run=False)
        assert r2.action == "superseded"

        # Verify chain: v3 → v2 → v1
        v3_meta = json.loads(v3_path.read_text())
        assert v3_meta["supersedes"] == "mem_v2_200"

        v2_meta = json.loads(v2_path.read_text())
        assert v2_meta["supersedes"] == "mem_v1_100"
        assert v2_meta["promotion_status"] == "superseded"

        v1_meta = json.loads(v1_path.read_text())
        assert v1_meta["promotion_status"] == "superseded"
