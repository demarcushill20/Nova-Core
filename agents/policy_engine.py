"""Centralized policy enforcement for multi-agent system.

Five-layer tool permission evaluation (matches OpenClaw model):
  1. Global deny  — absolute blocklist, no override possible
  2. Agent deny   — role-specific restrictions
  3. Global allow — baseline permissions for all agents
  4. Agent allow  — role-specific permissions
  5. Default      — configurable per agent (default: deny)

Elevated tools require operator approval before execution.

Policy sources:
  STATE/policies/global_policy.json  — global deny/allow/elevated lists
  STATE/agents/registry.json         — per-agent allowed/denied tools
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
    layer: str = ""        # which policy layer made the decision
    elevated: bool = False  # tool requires operator approval before execution


class PolicyViolation(Exception):
    """Raised when an agent attempts an unauthorized tool call."""

    def __init__(self, decision: PolicyDecision):
        self.decision = decision
        super().__init__(
            f"POLICY VIOLATION: agent={decision.agent_id} "
            f"tool={decision.tool_name} — {decision.reason}"
        )


class ElevatedToolRequest(Exception):
    """Raised when a tool call requires operator approval."""

    def __init__(self, decision: PolicyDecision):
        self.decision = decision
        super().__init__(
            f"ELEVATED TOOL: agent={decision.agent_id} "
            f"tool={decision.tool_name} — requires approval"
        )


# Default global policy when no file exists
_DEFAULT_GLOBAL_POLICY = {
    "global_deny": [
        "shell.rm_rf",
        "shell.drop_database",
        "shell.sudo_rm",
        "system.shutdown",
        "system.reboot",
    ],
    "global_allow": [
        "repo.files.read",
        "repo.search",
    ],
    "elevated": [
        "shell.run",
        "repo.git.push",
        "system.service_restart",
    ],
    "default": "deny",
}


class PolicyEngine:
    """Enforce role-based tool access and budget limits for all agents.

    Five-layer evaluation order:
      1. Global deny  → always blocked, no override
      2. Agent deny   → role-specific deny
      3. Global allow → baseline permissions
      4. Agent allow  → role-specific allow
      5. Default      → agent-level or global default (deny)

    Elevated tools are allowed but flagged for operator approval.
    """

    def __init__(self, registry_path: Path | None = None,
                 policies_path: Path | None = None,
                 global_policy_path: Path | None = None):
        self._registry_path = registry_path or (STATE / "agents" / "registry.json")
        self._policies_path = policies_path or (STATE / "policies" / "agent_policies.json")
        self._global_policy_path = global_policy_path or (STATE / "policies" / "global_policy.json")
        self._registry: dict | None = None
        self._policies: dict | None = None
        self._global_policy: dict | None = None

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

    def _load_global_policy(self) -> dict:
        if self._global_policy is not None:
            return self._global_policy
        if self._global_policy_path.exists():
            try:
                self._global_policy = json.loads(self._global_policy_path.read_text())
            except (json.JSONDecodeError, OSError):
                self._global_policy = _DEFAULT_GLOBAL_POLICY
        else:
            self._global_policy = _DEFAULT_GLOBAL_POLICY
        return self._global_policy

    def _get_agent_def(self, agent_id: str) -> dict | None:
        registry = self._load_registry()
        for agent in registry.get("agents", []):
            if agent["agent_id"] == agent_id:
                return agent
        return None

    def check_tool_access(self, agent_id: str, tool_name: str) -> PolicyDecision:
        """Check whether agent_id is allowed to call tool_name.

        Five-layer evaluation:
          1. Global deny  → blocked
          2. Agent deny   → blocked
          3. Global allow → permitted
          4. Agent allow  → permitted
          5. Default      → agent default or global default

        Returns PolicyDecision with allowed=True/False, layer, and elevated flag.
        """
        now = time.time()
        gp = self._load_global_policy()

        # --- Layer 1: Global deny (absolute, no override) ---
        for pattern in gp.get("global_deny", []):
            if self._matches(tool_name, pattern):
                return PolicyDecision(
                    allowed=False, agent_id=agent_id, tool_name=tool_name,
                    reason=f"Tool {tool_name!r} is globally denied",
                    checked_at=now, layer="global_deny",
                )

        # Agent lookup (needed for layers 2-5)
        agent_def = self._get_agent_def(agent_id)
        if agent_def is None:
            return PolicyDecision(
                allowed=False, agent_id=agent_id, tool_name=tool_name,
                reason=f"Agent {agent_id!r} not found in registry",
                checked_at=now, layer="agent_lookup",
            )

        # --- Layer 2: Agent deny ---
        for pattern in agent_def.get("denied_tools", []):
            if self._matches(tool_name, pattern):
                return PolicyDecision(
                    allowed=False, agent_id=agent_id, tool_name=tool_name,
                    reason=f"Tool {tool_name!r} is in denied_tools for {agent_id}",
                    checked_at=now, layer="agent_deny",
                )

        # Check if tool is elevated (for flagging, not blocking)
        is_elevated = any(
            self._matches(tool_name, p)
            for p in gp.get("elevated", [])
        )

        # --- Layer 3: Global allow ---
        for pattern in gp.get("global_allow", []):
            if self._matches(tool_name, pattern):
                return PolicyDecision(
                    allowed=True, agent_id=agent_id, tool_name=tool_name,
                    reason=f"Tool {tool_name!r} permitted by global allow",
                    checked_at=now, layer="global_allow",
                    elevated=is_elevated,
                )

        # --- Layer 4: Agent allow ---
        for pattern in agent_def.get("allowed_tools", []):
            if self._matches(tool_name, pattern):
                return PolicyDecision(
                    allowed=True, agent_id=agent_id, tool_name=tool_name,
                    reason=f"Tool {tool_name!r} permitted by agent allowlist",
                    checked_at=now, layer="agent_allow",
                    elevated=is_elevated,
                )

        # --- Layer 5: Default ---
        agent_default = agent_def.get("default_policy", gp.get("default", "deny"))
        if agent_default == "allow":
            return PolicyDecision(
                allowed=True, agent_id=agent_id, tool_name=tool_name,
                reason=f"Tool {tool_name!r} permitted by default-allow policy",
                checked_at=now, layer="default",
                elevated=is_elevated,
            )

        return PolicyDecision(
            allowed=False, agent_id=agent_id, tool_name=tool_name,
            reason=f"Tool {tool_name!r} not in any allow list for {agent_id}",
            checked_at=now, layer="default",
        )

    def enforce(self, agent_id: str, tool_name: str) -> PolicyDecision:
        """Check and raise PolicyViolation if denied, ElevatedToolRequest if elevated."""
        decision = self.check_tool_access(agent_id, tool_name)
        if not decision.allowed:
            raise PolicyViolation(decision)
        if decision.elevated:
            raise ElevatedToolRequest(decision)
        return decision

    def check_without_elevation(self, agent_id: str, tool_name: str) -> PolicyDecision:
        """Check access but don't raise on elevated tools (just flag them)."""
        decision = self.check_tool_access(agent_id, tool_name)
        if not decision.allowed:
            raise PolicyViolation(decision)
        return decision

    def is_globally_denied(self, tool_name: str) -> bool:
        """Check if a tool is in the global deny list (absolute block)."""
        gp = self._load_global_policy()
        return any(
            self._matches(tool_name, p)
            for p in gp.get("global_deny", [])
        )

    def is_elevated(self, tool_name: str) -> bool:
        """Check if a tool requires operator approval."""
        gp = self._load_global_policy()
        return any(
            self._matches(tool_name, p)
            for p in gp.get("elevated", [])
        )

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
        """Force reload of registry, policies, and global policy from disk."""
        self._registry = None
        self._policies = None
        self._global_policy = None
