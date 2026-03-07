"""Centralized policy enforcement for Phase 7 multi-agent system.

The research is explicit: centralized policy enforcement for all tool calls
is mandatory in bounded multi-agent systems. No agent independently decides
tool policy — all calls are checked against the policy layer.

Policy sources:
  STATE/agents/registry.json   — per-agent allowed/denied tools
  STATE/policies/agent_policies.json — extended policy profiles
"""

import json
import time
from dataclasses import dataclass
from pathlib import Path

BASE = Path("/home/nova/nova-core")
STATE = BASE / "STATE"


@dataclass
class PolicyDecision:
    allowed: bool
    agent_id: str
    tool_name: str
    reason: str
    checked_at: float


class PolicyViolation(Exception):
    """Raised when an agent attempts an unauthorized tool call."""

    def __init__(self, decision: PolicyDecision):
        self.decision = decision
        super().__init__(
            f"POLICY VIOLATION: agent={decision.agent_id} "
            f"tool={decision.tool_name} — {decision.reason}"
        )


class PolicyEngine:
    """Enforce role-based tool access and budget limits for all agents.

    Check order:
      1. Agent exists in registry
      2. Tool is in allowed_tools (explicit allowlist)
      3. Tool is NOT in denied_tools (explicit denylist)
      4. Budget limits (actions, runtime, retries) not exceeded
      5. Risk class check (maker-checker for mutations)
    """

    def __init__(self, registry_path: Path | None = None,
                 policies_path: Path | None = None):
        self._registry_path = registry_path or (STATE / "agents" / "registry.json")
        self._policies_path = policies_path or (STATE / "policies" / "agent_policies.json")
        self._registry: dict | None = None
        self._policies: dict | None = None

    def _load_registry(self) -> dict:
        if self._registry is None:
            self._registry = json.loads(self._registry_path.read_text())
        return self._registry

    def _load_policies(self) -> dict:
        if self._policies is not None:
            return self._policies
        if self._policies_path.exists():
            self._policies = json.loads(self._policies_path.read_text())
        else:
            self._policies = {}
        return self._policies

    def _get_agent_def(self, agent_id: str) -> dict | None:
        registry = self._load_registry()
        for agent in registry.get("agents", []):
            if agent["agent_id"] == agent_id:
                return agent
        return None

    def check_tool_access(self, agent_id: str, tool_name: str) -> PolicyDecision:
        """Check whether agent_id is allowed to call tool_name.

        Returns PolicyDecision with allowed=True/False and reason.
        """
        now = time.time()

        agent_def = self._get_agent_def(agent_id)
        if agent_def is None:
            return PolicyDecision(
                allowed=False, agent_id=agent_id, tool_name=tool_name,
                reason=f"Agent {agent_id!r} not found in registry",
                checked_at=now,
            )

        # Check denied_tools first (explicit deny takes priority)
        denied = agent_def.get("denied_tools", [])
        for pattern in denied:
            if self._matches(tool_name, pattern):
                return PolicyDecision(
                    allowed=False, agent_id=agent_id, tool_name=tool_name,
                    reason=f"Tool {tool_name!r} is in denied_tools for {agent_id}",
                    checked_at=now,
                )

        # Check allowed_tools
        allowed = agent_def.get("allowed_tools", [])
        for pattern in allowed:
            if self._matches(tool_name, pattern):
                return PolicyDecision(
                    allowed=True, agent_id=agent_id, tool_name=tool_name,
                    reason=f"Tool {tool_name!r} permitted by allowlist",
                    checked_at=now,
                )

        # Not in either list — default deny
        return PolicyDecision(
            allowed=False, agent_id=agent_id, tool_name=tool_name,
            reason=f"Tool {tool_name!r} not in allowed_tools for {agent_id}",
            checked_at=now,
        )

    def enforce(self, agent_id: str, tool_name: str) -> PolicyDecision:
        """Check and raise PolicyViolation if denied."""
        decision = self.check_tool_access(agent_id, tool_name)
        if not decision.allowed:
            raise PolicyViolation(decision)
        return decision

    def check_budget(self, agent_id: str, action_count: int,
                     runtime_s: float, retry_count: int) -> PolicyDecision:
        """Check whether agent is within budget limits."""
        now = time.time()
        agent_def = self._get_agent_def(agent_id)
        if agent_def is None:
            return PolicyDecision(
                allowed=False, agent_id=agent_id, tool_name="*",
                reason=f"Agent {agent_id!r} not found", checked_at=now,
            )

        max_actions = agent_def.get("max_actions", 50)
        max_runtime = agent_def.get("max_runtime_seconds", 300)
        max_retries = agent_def.get("max_retries", 1)

        if action_count >= max_actions:
            return PolicyDecision(
                allowed=False, agent_id=agent_id, tool_name="*",
                reason=f"Action budget exhausted: {action_count}/{max_actions}",
                checked_at=now,
            )

        if runtime_s >= max_runtime:
            return PolicyDecision(
                allowed=False, agent_id=agent_id, tool_name="*",
                reason=f"Runtime budget exhausted: {runtime_s:.0f}s/{max_runtime}s",
                checked_at=now,
            )

        if retry_count > max_retries:
            return PolicyDecision(
                allowed=False, agent_id=agent_id, tool_name="*",
                reason=f"Retry budget exhausted: {retry_count}/{max_retries}",
                checked_at=now,
            )

        return PolicyDecision(
            allowed=True, agent_id=agent_id, tool_name="*",
            reason="Within budget limits", checked_at=now,
        )

    def check_spawn_allowed(self, agent_id: str) -> PolicyDecision:
        """Check if agent is allowed to spawn child agents."""
        now = time.time()
        agent_def = self._get_agent_def(agent_id)
        if agent_def is None:
            return PolicyDecision(
                allowed=False, agent_id=agent_id, tool_name="agent.spawn",
                reason="Agent not found", checked_at=now,
            )

        flags = agent_def.get("feature_flags", {})
        if not flags.get("allow_delegation", False):
            return PolicyDecision(
                allowed=False, agent_id=agent_id, tool_name="agent.spawn",
                reason=f"Agent {agent_id} does not have allow_delegation flag",
                checked_at=now,
            )

        return PolicyDecision(
            allowed=True, agent_id=agent_id, tool_name="agent.spawn",
            reason="Delegation permitted", checked_at=now,
        )

    def requires_verification(self, tool_name: str) -> bool:
        """Check if a tool call requires verifier approval (maker-checker)."""
        # Mutation tools require maker-checker
        mutation_tools = {
            "repo.files.write", "repo.files.patch",
            "repo.git.commit", "shell.run",
        }
        return tool_name in mutation_tools

    @staticmethod
    def _matches(tool_name: str, pattern: str) -> bool:
        """Match tool name against pattern. Supports wildcard suffix (web.*)."""
        if pattern.endswith(".*"):
            prefix = pattern[:-2]
            return tool_name == prefix or tool_name.startswith(prefix + ".")
        return tool_name == pattern

    def reload(self) -> None:
        """Force reload of registry and policies from disk."""
        self._registry = None
        self._policies = None
