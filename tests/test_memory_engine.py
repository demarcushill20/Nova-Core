"""Tests for Phase 7.5 — Memory and Context Routing.

Acceptance criteria covered:
  1. completed workflow creates a memory artifact
  2. memory artifact is written through a bounded validated path
  3. malformed memory artifact writes are rejected
  4. planner can retrieve prior related memory safely
  5. retrieval remains bounded and advisory
"""

import json
import time
from pathlib import Path

import pytest

from agents.memory_engine import (
    MemoryArtifact,
    validate_memory_artifact,
    write_memory_artifact,
    compact_workflow_summary,
    retrieve_related_patterns,
    format_retrieval_for_planner,
    capture_workflow_memory,
    MAX_RETRIEVAL_RESULTS,
    MAX_ARTIFACT_SIZE,
    REQUIRED_FIELDS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _good_artifact(**overrides) -> MemoryArtifact:
    """Create a valid MemoryArtifact with sensible defaults."""
    defaults = dict(
        artifact_id="mem_wf001_1709900000",
        workflow_id="wf001",
        task_summary="Implement authentication module",
        task_class="code_impl",
        roles_involved=["coder", "critic", "verifier"],
        key_decisions=["Use JWT tokens", "Add rate limiting"],
        successful_patterns=["coder: implement auth module"],
        failure_patterns=[],
        verification_outcome="approved",
        reusable_guidance="All subtasks completed successfully.",
        created_at="2026-03-07T12:00:00Z",
        confidence="high",
    )
    defaults.update(overrides)
    return MemoryArtifact(**defaults)


def _good_artifact_dict(**overrides) -> dict:
    return _good_artifact(**overrides).to_dict()


def _sample_delegations() -> list[dict]:
    return [
        {
            "workflow_id": "wf001",
            "subtask_id": "sub1",
            "agent_id": "coder_01",
            "role": "coder",
            "goal": "Implement JWT auth",
            "status": "completed",
            "claimed_at": 1000.0,
            "completed_at": 1060.0,
        },
        {
            "workflow_id": "wf001",
            "subtask_id": "sub2",
            "agent_id": "critic_01",
            "role": "critic",
            "goal": "Review auth implementation",
            "status": "completed",
            "claimed_at": 1060.0,
            "completed_at": 1090.0,
        },
    ]


def _sample_contracts() -> list[dict]:
    return [
        {
            "agent_id": "coder_01",
            "workflow_id": "wf001",
            "subtask_id": "sub1",
            "role": "coder",
            "status": "completed",
            "summary": "Implemented JWT authentication with refresh tokens",
        },
    ]


def _sample_metrics() -> dict:
    return {
        "workflow_id": "wf001",
        "total_delegations": 2,
        "completed": 2,
        "failed": 0,
        "pending": 0,
        "contracts_received": 1,
        "mean_subtask_latency_s": 45.0,
    }


def _write_artifact_to_dir(directory: Path, artifact_dict: dict) -> Path:
    """Write an artifact dict to a directory for retrieval tests."""
    directory.mkdir(parents=True, exist_ok=True)
    aid = artifact_dict.get("artifact_id", "mem_test_0")
    path = directory / f"{aid}.json"
    path.write_text(json.dumps(artifact_dict, indent=2))
    return path


# ---------------------------------------------------------------------------
# 1. Validation tests
# ---------------------------------------------------------------------------

class TestValidateMemoryArtifact:
    """Test memory artifact validation — fail-closed behavior."""

    def test_valid_artifact_passes(self):
        data = _good_artifact_dict()
        valid, errors = validate_memory_artifact(data)
        assert valid is True
        assert errors == []

    def test_missing_required_field_rejected(self):
        for field_name in REQUIRED_FIELDS:
            data = _good_artifact_dict()
            del data[field_name]
            valid, errors = validate_memory_artifact(data)
            assert valid is False, f"Should reject missing {field_name}"
            assert any(field_name in e for e in errors)

    def test_empty_required_field_rejected(self):
        data = _good_artifact_dict(task_summary="")
        valid, errors = validate_memory_artifact(data)
        assert valid is False
        assert any("empty" in e for e in errors)

    def test_invalid_confidence_rejected(self):
        data = _good_artifact_dict(confidence="maybe")
        valid, errors = validate_memory_artifact(data)
        assert valid is False
        assert any("confidence" in e for e in errors)

    def test_invalid_task_class_rejected(self):
        data = _good_artifact_dict(task_class="magic")
        valid, errors = validate_memory_artifact(data)
        assert valid is False
        assert any("task_class" in e for e in errors)

    def test_invalid_verification_outcome_rejected(self):
        data = _good_artifact_dict(verification_outcome="maybe_ok")
        valid, errors = validate_memory_artifact(data)
        assert valid is False
        assert any("verification_outcome" in e for e in errors)

    def test_invalid_artifact_id_format_rejected(self):
        data = _good_artifact_dict(artifact_id="bad-format")
        valid, errors = validate_memory_artifact(data)
        assert valid is False
        assert any("artifact_id" in e for e in errors)

    def test_list_field_not_list_rejected(self):
        data = _good_artifact_dict()
        data["roles_involved"] = "not a list"
        valid, errors = validate_memory_artifact(data)
        assert valid is False
        assert any("roles_involved" in e for e in errors)

    def test_oversized_artifact_rejected(self):
        data = _good_artifact_dict(
            key_decisions=["x" * 5000 for _ in range(10)]
        )
        valid, errors = validate_memory_artifact(data)
        assert valid is False
        assert any("too large" in e for e in errors)

    def test_multiple_errors_collected(self):
        data = _good_artifact_dict(
            confidence="nope",
            task_class="invalid",
            artifact_id="bad",
        )
        valid, errors = validate_memory_artifact(data)
        assert valid is False
        assert len(errors) >= 3


# ---------------------------------------------------------------------------
# 2. Write path tests
# ---------------------------------------------------------------------------

class TestWriteMemoryArtifact:
    """Test bounded, validated write path."""

    def test_write_valid_artifact(self, tmp_path):
        art = _good_artifact()
        path = write_memory_artifact(art, target="workflow_learnings", base=tmp_path)
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["artifact_id"] == "mem_wf001_1709900000"
        assert data["task_summary"] == "Implement authentication module"

    def test_write_to_agent_patterns(self, tmp_path):
        art = _good_artifact(artifact_id="mem_wf002_1709900001", workflow_id="wf002")
        path = write_memory_artifact(art, target="agent_patterns", base=tmp_path)
        assert "agent_patterns" in str(path)
        assert path.exists()

    def test_write_invalid_artifact_rejected(self, tmp_path):
        art = _good_artifact(confidence="nope")
        with pytest.raises(ValueError, match="validation failed"):
            write_memory_artifact(art, base=tmp_path)

    def test_write_invalid_target_rejected(self, tmp_path):
        art = _good_artifact()
        with pytest.raises(ValueError, match="Invalid target"):
            write_memory_artifact(art, target="random_dir", base=tmp_path)

    def test_write_duplicate_rejected(self, tmp_path):
        art = _good_artifact()
        write_memory_artifact(art, base=tmp_path)
        with pytest.raises(ValueError, match="already exists"):
            write_memory_artifact(art, base=tmp_path)

    def test_write_creates_directories(self, tmp_path):
        art = _good_artifact()
        path = write_memory_artifact(art, base=tmp_path)
        assert path.parent.exists()


# ---------------------------------------------------------------------------
# 3. Workflow compaction tests
# ---------------------------------------------------------------------------

class TestCompactWorkflowSummary:
    """Test workflow state → memory artifact compaction."""

    def test_basic_compaction(self):
        art = compact_workflow_summary(
            workflow_id="wf001",
            task_summary="Build auth module",
            task_class="code_impl",
            delegations=_sample_delegations(),
            contracts=_sample_contracts(),
            metrics=_sample_metrics(),
            verification_outcome="approved",
        )
        assert art.workflow_id == "wf001"
        assert art.task_class == "code_impl"
        assert "coder" in art.roles_involved
        assert "critic" in art.roles_involved
        assert len(art.successful_patterns) == 2
        assert len(art.failure_patterns) == 0
        assert art.confidence == "high"
        assert art.verification_outcome == "approved"

    def test_compaction_with_failures(self):
        delegations = _sample_delegations()
        delegations[1]["status"] = "failed"
        delegations[1]["error"] = "contract missing"

        metrics = _sample_metrics()
        metrics["completed"] = 1
        metrics["failed"] = 1

        art = compact_workflow_summary(
            workflow_id="wf002",
            task_summary="Failing workflow",
            task_class="research",
            delegations=delegations,
            contracts=_sample_contracts(),
            metrics=metrics,
            verification_outcome="incomplete",
        )
        assert len(art.failure_patterns) == 1
        assert "contract missing" in art.failure_patterns[0]
        assert art.confidence == "medium"

    def test_compaction_produces_valid_artifact(self):
        art = compact_workflow_summary(
            workflow_id="wf003",
            task_summary="Test compaction",
            task_class="simple",
            delegations=_sample_delegations(),
            contracts=_sample_contracts(),
            metrics=_sample_metrics(),
            verification_outcome="approved",
        )
        valid, errors = validate_memory_artifact(art.to_dict())
        assert valid is True, f"Compacted artifact should be valid: {errors}"

    def test_compaction_caps_long_summary(self):
        art = compact_workflow_summary(
            workflow_id="wf004",
            task_summary="x" * 1000,
            task_class="code_impl",
            delegations=_sample_delegations(),
            contracts=_sample_contracts(),
            metrics=_sample_metrics(),
        )
        assert len(art.task_summary) <= 500

    def test_guidance_includes_failure_mode_warning(self):
        delegations = [
            {"role": "coder", "goal": "write code", "status": "failed",
             "error": "timeout"},
        ]
        metrics = {"total_delegations": 1, "completed": 0, "failed": 1,
                    "mean_subtask_latency_s": None}
        art = compact_workflow_summary(
            workflow_id="wf005",
            task_summary="Failing task",
            task_class="code_impl",
            delegations=delegations,
            contracts=[],
            metrics=metrics,
        )
        assert "failure" in art.reusable_guidance.lower() or \
               "restructuring" in art.reusable_guidance.lower()


# ---------------------------------------------------------------------------
# 4. Planner retrieval tests
# ---------------------------------------------------------------------------

class TestRetrieveRelatedPatterns:
    """Test bounded, safe planner retrieval."""

    def test_empty_memory_returns_empty(self, tmp_path):
        results = retrieve_related_patterns(
            task_class="code_impl",
            keywords=["auth"],
            base=tmp_path,
        )
        assert results == []

    def test_retrieve_matching_class(self, tmp_path):
        art = _good_artifact_dict(task_class="code_impl")
        _write_artifact_to_dir(tmp_path / "workflow_learnings", art)

        results = retrieve_related_patterns(
            task_class="code_impl",
            keywords=[],
            base=tmp_path,
        )
        assert len(results) == 1
        assert results[0]["task_class"] == "code_impl"
        assert "_relevance_score" in results[0]

    def test_retrieve_matching_keywords(self, tmp_path):
        art = _good_artifact_dict(
            task_summary="Implement authentication module",
        )
        _write_artifact_to_dir(tmp_path / "workflow_learnings", art)

        results = retrieve_related_patterns(
            task_class="research",  # different class
            keywords=["authentication"],
            base=tmp_path,
        )
        assert len(results) == 1

    def test_retrieval_bounded_at_max(self, tmp_path):
        # Write more artifacts than MAX_RETRIEVAL_RESULTS
        for i in range(MAX_RETRIEVAL_RESULTS + 5):
            art = _good_artifact_dict(
                artifact_id=f"mem_wf{i:03d}_{1709900000 + i}",
                workflow_id=f"wf{i:03d}",
                task_class="code_impl",
            )
            _write_artifact_to_dir(tmp_path / "workflow_learnings", art)

        results = retrieve_related_patterns(
            task_class="code_impl",
            keywords=[],
            base=tmp_path,
        )
        assert len(results) <= MAX_RETRIEVAL_RESULTS

    def test_retrieval_ranked_by_relevance(self, tmp_path):
        # High relevance: same class + keyword match
        art1 = _good_artifact_dict(
            artifact_id="mem_wf010_1709900010",
            workflow_id="wf010",
            task_class="code_impl",
            task_summary="Build authentication system",
            confidence="high",
        )
        # Low relevance: different class, no keyword
        art2 = _good_artifact_dict(
            artifact_id="mem_wf011_1709900011",
            workflow_id="wf011",
            task_class="research",
            task_summary="Study database patterns",
            confidence="low",
        )
        _write_artifact_to_dir(tmp_path / "workflow_learnings", art1)
        _write_artifact_to_dir(tmp_path / "workflow_learnings", art2)

        results = retrieve_related_patterns(
            task_class="code_impl",
            keywords=["authentication"],
            base=tmp_path,
        )
        assert len(results) >= 1
        # First result should be the more relevant one
        assert results[0]["workflow_id"] == "wf010"

    def test_retrieval_searches_both_dirs(self, tmp_path):
        art1 = _good_artifact_dict(
            artifact_id="mem_wf020_1709900020",
            workflow_id="wf020",
            task_class="code_impl",
        )
        art2 = _good_artifact_dict(
            artifact_id="mem_wf021_1709900021",
            workflow_id="wf021",
            task_class="code_impl",
        )
        _write_artifact_to_dir(tmp_path / "workflow_learnings", art1)
        _write_artifact_to_dir(tmp_path / "agent_patterns", art2)

        results = retrieve_related_patterns(
            task_class="code_impl",
            keywords=[],
            base=tmp_path,
        )
        assert len(results) == 2

    def test_malformed_files_skipped(self, tmp_path):
        d = tmp_path / "workflow_learnings"
        d.mkdir(parents=True)
        (d / "bad.json").write_text("not valid json{{{")
        art = _good_artifact_dict()
        _write_artifact_to_dir(d, art)

        results = retrieve_related_patterns(
            task_class="code_impl",
            keywords=[],
            base=tmp_path,
        )
        assert len(results) == 1  # only the valid one

    def test_max_results_cap_enforced(self, tmp_path):
        art = _good_artifact_dict()
        _write_artifact_to_dir(tmp_path / "workflow_learnings", art)

        # Even if caller asks for more, hard cap applies
        results = retrieve_related_patterns(
            task_class="code_impl",
            keywords=[],
            max_results=100,
            base=tmp_path,
        )
        assert len(results) <= MAX_RETRIEVAL_RESULTS


# ---------------------------------------------------------------------------
# 5. Planner formatting tests
# ---------------------------------------------------------------------------

class TestFormatRetrievalForPlanner:
    """Test planner-readable output formatting."""

    def test_empty_results(self):
        output = format_retrieval_for_planner([])
        assert "No prior related patterns" in output

    def test_formatted_output_contains_key_fields(self):
        art = _good_artifact_dict()
        art["_relevance_score"] = 4.5
        output = format_retrieval_for_planner([art])
        assert "Pattern 1" in output
        assert "code_impl" in output
        assert "approved" in output
        assert "Advisory only" in output

    def test_output_bounded(self):
        arts = []
        for i in range(10):
            art = _good_artifact_dict(
                artifact_id=f"mem_wf{i:03d}_{1709900000 + i}",
                task_summary="A" * 200,
                reusable_guidance="B" * 200,
            )
            art["_relevance_score"] = 1.0
            arts.append(art)
        output = format_retrieval_for_planner(arts)
        assert len(output) <= 4096 + 10  # allow small margin


# ---------------------------------------------------------------------------
# 6. End-to-end integration tests
# ---------------------------------------------------------------------------

class TestCaptureWorkflowMemory:
    """Test the full capture pipeline: compact + validate + write."""

    def test_capture_creates_artifact_file(self, tmp_path):
        path = capture_workflow_memory(
            workflow_id="wf_e2e",
            task_summary="End-to-end test workflow",
            task_class="code_impl",
            delegations=_sample_delegations(),
            contracts=_sample_contracts(),
            metrics=_sample_metrics(),
            verification_outcome="approved",
            base=tmp_path,
        )
        assert path is not None
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["workflow_id"] == "wf_e2e"
        assert data["task_class"] == "code_impl"

    def test_capture_empty_delegations_returns_none(self, tmp_path):
        result = capture_workflow_memory(
            workflow_id="wf_empty",
            task_summary="No work done",
            task_class="simple",
            delegations=[],
            contracts=[],
            metrics={"total_delegations": 0, "completed": 0, "failed": 0},
            base=tmp_path,
        )
        assert result is None

    def test_capture_then_retrieve(self, tmp_path):
        """Full round-trip: capture → retrieve → format."""
        capture_workflow_memory(
            workflow_id="wf_roundtrip",
            task_summary="Build API endpoint for user login",
            task_class="code_impl",
            delegations=_sample_delegations(),
            contracts=_sample_contracts(),
            metrics=_sample_metrics(),
            verification_outcome="approved",
            base=tmp_path,
        )

        results = retrieve_related_patterns(
            task_class="code_impl",
            keywords=["login", "API"],
            base=tmp_path,
        )
        assert len(results) == 1
        assert results[0]["task_class"] == "code_impl"

        output = format_retrieval_for_planner(results)
        assert "API endpoint" in output
        assert "Advisory only" in output

    def test_capture_writes_to_correct_target(self, tmp_path):
        path = capture_workflow_memory(
            workflow_id="wf_target",
            task_summary="Test target routing",
            task_class="research",
            delegations=_sample_delegations(),
            contracts=_sample_contracts(),
            metrics=_sample_metrics(),
            target="agent_patterns",
            base=tmp_path,
        )
        assert "agent_patterns" in str(path)
