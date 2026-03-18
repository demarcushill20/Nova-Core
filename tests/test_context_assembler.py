"""Tests for P11.4 — Shared Context and Handoff Protocol."""

from __future__ import annotations

import pytest

from agents.context_assembler import (
    ContextAssembler,
    HandoffArtifact,
    _estimate_tokens,
)


@pytest.fixture
def assembler(tmp_path):
    return ContextAssembler(token_budget=1000, persist_dir=tmp_path / "handoffs")


class TestContextAssembly:
    def test_scoped_context_for_coder(self, assembler):
        ctx = assembler.assemble(
            "coder",
            {
                "source_files": "def foo(): pass",
                "spec": "build feature X",
                "unrelated_key": "should be excluded",
            },
        )
        assert "source_files" in ctx
        assert "spec" in ctx
        assert "unrelated_key" not in ctx

    def test_scoped_context_for_research(self, assembler):
        ctx = assembler.assemble(
            "research",
            {
                "query": "find info about X",
                "existing_findings": "prior results",
                "source_files": "should not appear for research",
            },
        )
        assert "query" in ctx
        assert "source_files" not in ctx

    def test_scoped_context_for_critic(self, assembler):
        ctx = assembler.assemble(
            "critic",
            {
                "work_product": "code output",
                "review_criteria": "correctness, security",
                "spec": "original spec",
            },
        )
        assert "work_product" in ctx
        assert "review_criteria" in ctx

    def test_extra_keys(self, assembler):
        ctx = assembler.assemble(
            "coder",
            {
                "source_files": "code",
                "custom_data": "extra",
            },
            extra_keys=["custom_data"],
        )
        assert "custom_data" in ctx

    def test_unknown_role_gets_empty(self, assembler):
        ctx = assembler.assemble("unknown_role", {"query": "test"})
        assert ctx == {}

    def test_missing_keys_ignored(self, assembler):
        ctx = assembler.assemble("coder", {})
        assert ctx == {}


class TestTokenBudget:
    def test_within_budget(self, assembler):
        ctx = assembler.assemble("coder", {"source_files": "short code"})
        assert assembler.context_utilization(ctx) < 1.0

    def test_truncation_at_budget(self, tmp_path):
        small_budget = ContextAssembler(token_budget=50, persist_dir=tmp_path / "h")
        # A large value that exceeds the budget
        big = "x" * 500
        ctx = small_budget.assemble("coder", {"source_files": big})
        # Should be truncated
        if "source_files" in ctx:
            assert len(ctx["source_files"]) < 500

    def test_estimate_tokens(self):
        assert _estimate_tokens("hello") >= 1
        assert _estimate_tokens("a" * 400) == 100  # 400 / 4

    def test_utilization(self, assembler):
        ctx = {"source_files": "short"}
        util = assembler.context_utilization(ctx)
        assert 0 < util < 1.0


class TestHandoffArtifact:
    def test_creation(self):
        h = HandoffArtifact(
            subtask_id="s1",
            from_role="coder",
            to_role="critic",
            status="completed",
            workflow_id="wf-1",
            output_summary="code written",
        )
        assert h.subtask_id == "s1"
        assert h.confidence == 1.0

    def test_serialization(self):
        h = HandoffArtifact(subtask_id="s1", from_role="a", to_role="b", status="completed")
        d = h.to_dict()
        restored = HandoffArtifact.from_dict(d)
        assert restored.subtask_id == "s1"
        assert restored.from_role == "a"

    def test_save_and_load(self, assembler):
        h = HandoffArtifact(
            subtask_id="s1",
            from_role="coder",
            to_role="critic",
            status="completed",
            workflow_id="wf-1",
        )
        assembler.save_handoff(h)
        loaded = assembler.load_handoff("wf-1", "s1")
        assert loaded is not None
        assert loaded.subtask_id == "s1"

    def test_load_nonexistent(self, assembler):
        assert assembler.load_handoff("wf-x", "s-x") is None

    def test_list_handoffs(self, assembler):
        for i in range(3):
            h = HandoffArtifact(
                subtask_id=f"s{i}",
                from_role="a",
                to_role="b",
                status="completed",
                workflow_id="wf-list",
            )
            assembler.save_handoff(h)
        handoffs = assembler.list_handoffs("wf-list")
        assert len(handoffs) == 3
