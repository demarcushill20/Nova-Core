"""Tests for P11.6 — Budget and Policy Runtime Enforcement."""

from __future__ import annotations

import time

import pytest

from agents.runtime_policy import (
    AgentBudgetState,
    BudgetExhausted,
    PolicyViolation,
    RuntimePolicyEnforcer,
    WorkflowBudgetState,
)


@pytest.fixture
def enforcer(tmp_path):
    return RuntimePolicyEnforcer(persist_dir=tmp_path / "violations")


class TestPolicyChecks:
    def test_allowed_tool(self, enforcer):
        enforcer.register_agent("coder_001", "wf-1", allowed_tools=["repo.files.write"])
        result = enforcer.check_tool(
            "coder_001",
            "wf-1",
            "repo.files.write",
            allowed_tools=["repo.files.write"],
        )
        assert result.allowed is True

    def test_denied_tool(self, enforcer):
        enforcer.register_agent("coder_001", "wf-1")
        with pytest.raises(PolicyViolation):
            enforcer.check_tool(
                "coder_001",
                "wf-1",
                "shell.run",
                denied_tools=["shell.run"],
            )

    def test_wildcard_deny(self, enforcer):
        enforcer.register_agent("coder_001", "wf-1")
        with pytest.raises(PolicyViolation):
            enforcer.check_tool(
                "coder_001",
                "wf-1",
                "vault.write",
                denied_tools=["vault.*"],
            )

    def test_tool_not_in_allow_list(self, enforcer):
        enforcer.register_agent("coder_001", "wf-1")
        with pytest.raises(PolicyViolation):
            enforcer.check_tool(
                "coder_001",
                "wf-1",
                "web.search",
                allowed_tools=["repo.files.read", "repo.files.write"],
            )

    def test_no_restrictions(self, enforcer):
        enforcer.register_agent("coder_001", "wf-1")
        # No allowed/denied lists → everything passes
        result = enforcer.check_tool("coder_001", "wf-1", "anything")
        assert result.allowed is True

    def test_violation_persisted(self, enforcer, tmp_path):
        enforcer.register_agent("coder_001", "wf-1")
        with pytest.raises(PolicyViolation):
            enforcer.check_tool(
                "coder_001",
                "wf-1",
                "shell.run",
                denied_tools=["shell.run"],
            )
        path = tmp_path / "violations" / "wf-1.jsonl"
        assert path.exists()


class TestBudgetTracking:
    def test_action_counting(self, enforcer):
        enforcer.register_agent("coder_001", "wf-1", max_actions=5)
        for i in range(4):
            enforcer.check_tool("coder_001", "wf-1", f"tool_{i}")
        # 5th should exhaust
        with pytest.raises(BudgetExhausted):
            enforcer.check_tool("coder_001", "wf-1", "tool_5")

    def test_workflow_budget(self, enforcer):
        enforcer.register_agent("a1", "wf-1", max_actions=50)
        enforcer.register_agent("a2", "wf-1", max_actions=50)
        wf = enforcer._workflow_budgets["wf-1"]
        wf.max_total_actions = 5
        for i in range(4):
            enforcer.check_tool("a1", "wf-1", f"tool_{i}")
        # Total hits 5 (workflow limit)
        with pytest.raises(BudgetExhausted, match="Workflow"):
            enforcer.check_tool("a2", "wf-1", "tool_5")

    def test_budget_report(self, enforcer):
        enforcer.register_agent("coder_001", "wf-1", max_actions=10)
        enforcer.check_tool("coder_001", "wf-1", "tool_1")
        report = enforcer.get_budget_report("wf-1")
        assert report is not None
        assert report["total_actions"] == 1
        assert report["agents"]["coder_001"]["actions_used"] == 1

    def test_nonexistent_workflow_report(self, enforcer):
        assert enforcer.get_budget_report("wf-x") is None


class TestAgentBudgetState:
    def test_within_budget(self):
        b = AgentBudgetState(agent_id="a", max_actions=5, started_at=time.time())
        assert b.is_within_budget
        assert b.actions_remaining == 5

    def test_actions_exceeded(self):
        b = AgentBudgetState(agent_id="a", max_actions=2)
        b.record_action()
        b.record_action()
        assert not b.is_within_budget
        assert b.actions_remaining == 0

    def test_to_dict(self):
        b = AgentBudgetState(agent_id="a", max_actions=5, started_at=time.time())
        b.record_action()
        d = b.to_dict()
        assert d["actions_used"] == 1
        assert d["actions_remaining"] == 4
        assert d["is_within_budget"] is True


class TestWorkflowBudgetState:
    def test_aggregation(self):
        wf = WorkflowBudgetState(workflow_id="wf-1", started_at=time.time())
        a1 = AgentBudgetState(agent_id="a1", max_actions=10)
        a2 = AgentBudgetState(agent_id="a2", max_actions=10)
        a1.record_action()
        a1.record_action()
        a2.record_action()
        wf.agent_budgets = {"a1": a1, "a2": a2}
        assert wf.total_actions == 3


class TestMetrics:
    def test_enforcer_metrics(self, enforcer):
        enforcer.register_agent("a", "wf-1")
        enforcer.check_tool("a", "wf-1", "tool")
        metrics = enforcer.get_metrics()
        assert metrics["total_checks"] == 1
        assert metrics["total_violations"] == 0
        assert metrics["active_workflows"] == 1
