"""P11.6 — Budget and Policy Runtime Enforcement.

Wraps every agent tool call with policy checks and budget tracking at runtime.
Provides per-agent and per-workflow budget aggregation with auto-cancel
on budget exceed.

Integrates with:
  - PolicyEngine (Phase 7): tool allowlists/denylists
  - AgentProcess (P11.1): per-agent budget tracking
  - TimeoutManager (P11.5): failure recording
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agents.validation import validate_id

logger = logging.getLogger(__name__)

BASE = Path("/home/nova/nova-core")
STATE = BASE / "STATE"
VIOLATIONS_DIR = STATE / "policy_violations"


# ---------------------------------------------------------------------------
# Policy check result
# ---------------------------------------------------------------------------


@dataclass
class PolicyCheckResult:
    """Result of a runtime policy check."""

    allowed: bool
    agent_id: str
    tool_name: str
    reason: str
    checked_at: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Budget tracker
# ---------------------------------------------------------------------------


@dataclass
class AgentBudgetState:
    """Runtime budget state for a single agent."""

    agent_id: str
    max_actions: int = 20
    max_runtime_s: float = 300.0
    actions_used: int = 0
    started_at: float | None = None
    violations: list[dict] = field(default_factory=list)

    @property
    def actions_remaining(self) -> int:
        return max(0, self.max_actions - self.actions_used)

    @property
    def runtime_elapsed_s(self) -> float:
        if self.started_at is None:
            return 0.0
        return time.time() - self.started_at

    @property
    def runtime_remaining_s(self) -> float:
        return max(0.0, self.max_runtime_s - self.runtime_elapsed_s)

    @property
    def is_within_budget(self) -> bool:
        return self.actions_used < self.max_actions and self.runtime_elapsed_s < self.max_runtime_s

    def record_action(self) -> None:
        self.actions_used += 1

    def to_dict(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "max_actions": self.max_actions,
            "max_runtime_s": self.max_runtime_s,
            "actions_used": self.actions_used,
            "actions_remaining": self.actions_remaining,
            "runtime_elapsed_s": self.runtime_elapsed_s,
            "runtime_remaining_s": self.runtime_remaining_s,
            "is_within_budget": self.is_within_budget,
            "violations": self.violations,
        }


@dataclass
class WorkflowBudgetState:
    """Aggregate budget tracking across all agents in a workflow."""

    workflow_id: str
    max_total_actions: int = 100
    max_total_runtime_s: float = 1800.0
    agent_budgets: dict[str, AgentBudgetState] = field(default_factory=dict)
    started_at: float | None = None

    @property
    def total_actions(self) -> int:
        return sum(b.actions_used for b in self.agent_budgets.values())

    @property
    def total_runtime_s(self) -> float:
        if self.started_at is None:
            return 0.0
        return time.time() - self.started_at

    @property
    def is_within_budget(self) -> bool:
        return self.total_actions < self.max_total_actions and self.total_runtime_s < self.max_total_runtime_s

    def to_dict(self) -> dict:
        return {
            "workflow_id": self.workflow_id,
            "total_actions": self.total_actions,
            "max_total_actions": self.max_total_actions,
            "total_runtime_s": self.total_runtime_s,
            "max_total_runtime_s": self.max_total_runtime_s,
            "is_within_budget": self.is_within_budget,
            "agents": {aid: b.to_dict() for aid, b in self.agent_budgets.items()},
        }


# ---------------------------------------------------------------------------
# Runtime Policy Enforcer
# ---------------------------------------------------------------------------


class PolicyViolation(Exception):
    """Raised when an agent violates its policy."""

    def __init__(self, check: PolicyCheckResult):
        self.check = check
        super().__init__(f"Policy violation: {check.agent_id} → {check.tool_name}: {check.reason}")


class BudgetExhausted(Exception):
    """Raised when an agent or workflow exceeds its budget."""


class RuntimePolicyEnforcer:
    """Per-action policy and budget enforcement at runtime.

    Wraps every tool call with:
      1. Policy check (allowed/denied tools)
      2. Budget check (actions, runtime)
      3. Violation logging
      4. Budget reporting
    """

    def __init__(self, persist_dir: Path | None = None, bridge: Any | None = None):
        self._workflow_budgets: dict[str, WorkflowBudgetState] = {}
        self._persist_dir = persist_dir or VIOLATIONS_DIR
        self._persist_dir.mkdir(parents=True, exist_ok=True)
        self._total_checks = 0
        self._total_violations = 0
        self._bridge = bridge  # Optional RuntimeBridge for PolicyEngine delegation

    def register_agent(
        self,
        agent_id: str,
        workflow_id: str,
        *,
        max_actions: int = 20,
        max_runtime_s: float = 300.0,
        allowed_tools: list[str] | None = None,
        denied_tools: list[str] | None = None,
    ) -> AgentBudgetState:
        """Register an agent for policy and budget tracking."""
        if workflow_id not in self._workflow_budgets:
            self._workflow_budgets[workflow_id] = WorkflowBudgetState(
                workflow_id=workflow_id,
                started_at=time.time(),
            )

        wf = self._workflow_budgets[workflow_id]
        budget = AgentBudgetState(
            agent_id=agent_id,
            max_actions=max_actions,
            max_runtime_s=max_runtime_s,
            started_at=time.time(),
        )
        wf.agent_budgets[agent_id] = budget
        return budget

    def check_tool(
        self,
        agent_id: str,
        workflow_id: str,
        tool_name: str,
        *,
        allowed_tools: list[str] | None = None,
        denied_tools: list[str] | None = None,
    ) -> PolicyCheckResult:
        """Check if an agent is allowed to call a tool.

        Also increments action count and checks budget.

        Raises:
            PolicyViolation: If the tool is denied.
            BudgetExhausted: If the agent or workflow exceeds budget.
        """
        self._total_checks += 1

        # Delegate to 5-layer PolicyEngine if bridge is present
        if self._bridge is not None and not self._bridge.check_tool_access(agent_id, tool_name):
            result = PolicyCheckResult(
                allowed=False,
                agent_id=agent_id,
                tool_name=tool_name,
                reason=f"Denied by PolicyEngine for agent '{agent_id}'",
            )
            self._record_violation(result, workflow_id)
            raise PolicyViolation(result)

        # Denied tools check
        if denied_tools and tool_name in denied_tools:
            result = PolicyCheckResult(
                allowed=False,
                agent_id=agent_id,
                tool_name=tool_name,
                reason=f"Tool '{tool_name}' is in deny list",
            )
            self._record_violation(result, workflow_id)
            raise PolicyViolation(result)

        # Wildcard deny check (e.g. "vault.*")
        if denied_tools:
            for pattern in denied_tools:
                if pattern.endswith(".*"):
                    prefix = pattern[:-2]
                    if tool_name.startswith(prefix + "."):
                        result = PolicyCheckResult(
                            allowed=False,
                            agent_id=agent_id,
                            tool_name=tool_name,
                            reason=f"Tool '{tool_name}' matches deny pattern '{pattern}'",
                        )
                        self._record_violation(result, workflow_id)
                        raise PolicyViolation(result)

        # Allowed tools check (if specified, tool must be in the list)
        if allowed_tools and tool_name not in allowed_tools:
            result = PolicyCheckResult(
                allowed=False,
                agent_id=agent_id,
                tool_name=tool_name,
                reason=f"Tool '{tool_name}' not in allow list",
            )
            self._record_violation(result, workflow_id)
            raise PolicyViolation(result)

        # Budget check
        wf = self._workflow_budgets.get(workflow_id)
        if wf is not None:
            agent_budget = wf.agent_budgets.get(agent_id)
            if agent_budget is not None:
                agent_budget.record_action()
                if not agent_budget.is_within_budget:
                    raise BudgetExhausted(
                        f"Agent {agent_id} exceeded budget: "
                        f"actions={agent_budget.actions_used}/{agent_budget.max_actions}, "
                        f"runtime={agent_budget.runtime_elapsed_s:.0f}/{agent_budget.max_runtime_s:.0f}s"
                    )
            if not wf.is_within_budget:
                raise BudgetExhausted(
                    f"Workflow {workflow_id} exceeded budget: actions={wf.total_actions}/{wf.max_total_actions}"
                )

        return PolicyCheckResult(
            allowed=True,
            agent_id=agent_id,
            tool_name=tool_name,
            reason="allowed",
        )

    def _record_violation(self, result: PolicyCheckResult, workflow_id: str) -> None:
        """Log a policy violation."""
        self._total_violations += 1
        wf = self._workflow_budgets.get(workflow_id)
        if wf is not None:
            agent_budget = wf.agent_budgets.get(result.agent_id)
            if agent_budget is not None:
                agent_budget.violations.append(
                    {
                        "tool": result.tool_name,
                        "reason": result.reason,
                        "at": result.checked_at,
                    }
                )
        # Persist
        try:
            validate_id(workflow_id, "workflow_id")
            path = self._persist_dir / f"{workflow_id}.jsonl"
            entry = {
                "agent_id": result.agent_id,
                "tool": result.tool_name,
                "reason": result.reason,
                "at": result.checked_at,
            }
            with open(path, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError:
            pass

    def get_budget_report(self, workflow_id: str) -> dict | None:
        """Get budget utilization report for a workflow."""
        wf = self._workflow_budgets.get(workflow_id)
        if wf is None:
            return None
        return wf.to_dict()

    def get_metrics(self) -> dict[str, Any]:
        return {
            "total_checks": self._total_checks,
            "total_violations": self._total_violations,
            "active_workflows": len(self._workflow_budgets),
        }
