"""Tests for 5-layer policy engine (OpenClaw Phase 9).

Covers:
- Layer 1: Global deny (absolute block)
- Layer 2: Agent deny (role-specific)
- Layer 3: Global allow (baseline)
- Layer 4: Agent allow (role-specific)
- Layer 5: Default policy (configurable)
- Elevated tool flagging
- Backward compatibility with existing enforce/check_budget/check_spawn
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

_here = str(Path(__file__).resolve().parent.parent)
if _here not in sys.path:
    sys.path.insert(0, _here)

from agents.policy_engine import (
    PolicyEngine, PolicyDecision, PolicyViolation, ElevatedToolRequest,
)


def _make_engine(tmp_path, registry_agents, global_policy=None):
    """Create a PolicyEngine with test fixtures."""
    reg_path = tmp_path / "registry.json"
    reg_path.write_text(json.dumps({"agents": registry_agents}))

    gp_path = tmp_path / "global_policy.json"
    if global_policy:
        gp_path.write_text(json.dumps(global_policy))

    pol_path = tmp_path / "agent_policies.json"
    pol_path.write_text("{}")

    return PolicyEngine(
        registry_path=reg_path,
        policies_path=pol_path,
        global_policy_path=gp_path,
    )


# --- Test fixtures ---

_BASIC_AGENT = {
    "agent_id": "test_agent",
    "role": "worker",
    "status": "idle",
    "allowed_tools": ["repo.files.write", "web.search"],
    "denied_tools": ["shell.run"],
    "max_actions": 10,
    "max_runtime_seconds": 60,
    "max_retries": 1,
    "feature_flags": {"allow_delegation": False},
}

_PERMISSIVE_AGENT = {
    "agent_id": "permissive_agent",
    "role": "admin",
    "status": "idle",
    "allowed_tools": ["*"],
    "denied_tools": [],
    "default_policy": "allow",
    "max_actions": 100,
    "max_runtime_seconds": 600,
    "max_retries": 3,
    "feature_flags": {"allow_delegation": True},
}

_BASIC_GLOBAL = {
    "global_deny": ["shell.rm_rf", "system.shutdown"],
    "global_allow": ["repo.files.read", "repo.search"],
    "elevated": ["shell.run", "repo.git.push"],
    "default": "deny",
}


class TestLayer1GlobalDeny(unittest.TestCase):
    """Global deny blocks ALL agents, no override possible."""

    def test_globally_denied_tool_blocked(self):
        tmp = Path(tempfile.mkdtemp())
        engine = _make_engine(tmp, [_BASIC_AGENT], _BASIC_GLOBAL)
        decision = engine.check_tool_access("test_agent", "shell.rm_rf")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.layer, "global_deny")

    def test_global_deny_overrides_agent_allow(self):
        """Even if agent has tool in allowed_tools, global deny wins."""
        tmp = Path(tempfile.mkdtemp())
        agent = {**_BASIC_AGENT, "allowed_tools": ["shell.rm_rf", "web.search"]}
        engine = _make_engine(tmp, [agent], _BASIC_GLOBAL)
        decision = engine.check_tool_access("test_agent", "shell.rm_rf")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.layer, "global_deny")

    def test_global_deny_overrides_default_allow(self):
        tmp = Path(tempfile.mkdtemp())
        agent = {**_PERMISSIVE_AGENT, "agent_id": "test_agent"}
        engine = _make_engine(tmp, [agent], _BASIC_GLOBAL)
        decision = engine.check_tool_access("test_agent", "system.shutdown")
        self.assertFalse(decision.allowed)

    def test_is_globally_denied_helper(self):
        tmp = Path(tempfile.mkdtemp())
        engine = _make_engine(tmp, [_BASIC_AGENT], _BASIC_GLOBAL)
        self.assertTrue(engine.is_globally_denied("shell.rm_rf"))
        self.assertFalse(engine.is_globally_denied("repo.files.read"))


class TestLayer2AgentDeny(unittest.TestCase):
    """Agent deny blocks tools for specific roles."""

    def test_agent_denied_tool_blocked(self):
        tmp = Path(tempfile.mkdtemp())
        engine = _make_engine(tmp, [_BASIC_AGENT], _BASIC_GLOBAL)
        decision = engine.check_tool_access("test_agent", "shell.run")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.layer, "agent_deny")

    def test_agent_deny_with_wildcard(self):
        tmp = Path(tempfile.mkdtemp())
        agent = {**_BASIC_AGENT, "denied_tools": ["vault.*"]}
        engine = _make_engine(tmp, [agent], _BASIC_GLOBAL)
        decision = engine.check_tool_access("test_agent", "vault.write")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.layer, "agent_deny")


class TestLayer3GlobalAllow(unittest.TestCase):
    """Global allow provides baseline permissions for all agents."""

    def test_globally_allowed_tool_permitted(self):
        tmp = Path(tempfile.mkdtemp())
        # Agent doesn't have repo.files.read in its own allowed_tools
        agent = {**_BASIC_AGENT, "allowed_tools": ["web.search"]}
        engine = _make_engine(tmp, [agent], _BASIC_GLOBAL)
        decision = engine.check_tool_access("test_agent", "repo.files.read")
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.layer, "global_allow")

    def test_global_allow_overridden_by_agent_deny(self):
        """Agent deny beats global allow."""
        tmp = Path(tempfile.mkdtemp())
        gp = {**_BASIC_GLOBAL, "global_allow": ["shell.run"]}
        engine = _make_engine(tmp, [_BASIC_AGENT], gp)
        # shell.run is in agent's denied_tools
        decision = engine.check_tool_access("test_agent", "shell.run")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.layer, "agent_deny")


class TestLayer4AgentAllow(unittest.TestCase):
    """Agent allow provides role-specific permissions."""

    def test_agent_allowed_tool_permitted(self):
        tmp = Path(tempfile.mkdtemp())
        engine = _make_engine(tmp, [_BASIC_AGENT], _BASIC_GLOBAL)
        decision = engine.check_tool_access("test_agent", "web.search")
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.layer, "agent_allow")

    def test_agent_allow_wildcard(self):
        tmp = Path(tempfile.mkdtemp())
        agent = {**_BASIC_AGENT, "allowed_tools": ["web.*"]}
        engine = _make_engine(tmp, [agent], _BASIC_GLOBAL)
        decision = engine.check_tool_access("test_agent", "web.fetch")
        self.assertTrue(decision.allowed)


class TestLayer5Default(unittest.TestCase):
    """Default policy when tool not in any list."""

    def test_default_deny(self):
        tmp = Path(tempfile.mkdtemp())
        engine = _make_engine(tmp, [_BASIC_AGENT], _BASIC_GLOBAL)
        decision = engine.check_tool_access("test_agent", "unknown.tool")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.layer, "default")

    def test_default_allow_per_agent(self):
        tmp = Path(tempfile.mkdtemp())
        engine = _make_engine(tmp, [_PERMISSIVE_AGENT], _BASIC_GLOBAL)
        decision = engine.check_tool_access("permissive_agent", "unknown.tool")
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.layer, "default")

    def test_global_default_used_when_agent_has_none(self):
        tmp = Path(tempfile.mkdtemp())
        gp = {**_BASIC_GLOBAL, "default": "deny"}
        engine = _make_engine(tmp, [_BASIC_AGENT], gp)
        decision = engine.check_tool_access("test_agent", "unknown.tool")
        self.assertFalse(decision.allowed)


class TestElevatedTools(unittest.TestCase):
    """Elevated tools are allowed but flagged for operator approval."""

    def test_elevated_flag_set(self):
        tmp = Path(tempfile.mkdtemp())
        # shell.run is elevated but also in agent deny — deny wins
        # Use repo.git.push which is elevated and in agent allow
        agent = {**_BASIC_AGENT, "allowed_tools": ["repo.git.push"]}
        engine = _make_engine(tmp, [agent], _BASIC_GLOBAL)
        decision = engine.check_tool_access("test_agent", "repo.git.push")
        self.assertTrue(decision.allowed)
        self.assertTrue(decision.elevated)

    def test_non_elevated_tool_not_flagged(self):
        tmp = Path(tempfile.mkdtemp())
        engine = _make_engine(tmp, [_BASIC_AGENT], _BASIC_GLOBAL)
        decision = engine.check_tool_access("test_agent", "web.search")
        self.assertTrue(decision.allowed)
        self.assertFalse(decision.elevated)

    def test_is_elevated_helper(self):
        tmp = Path(tempfile.mkdtemp())
        engine = _make_engine(tmp, [_BASIC_AGENT], _BASIC_GLOBAL)
        self.assertTrue(engine.is_elevated("shell.run"))
        self.assertTrue(engine.is_elevated("repo.git.push"))
        self.assertFalse(engine.is_elevated("web.search"))

    def test_enforce_raises_elevated(self):
        tmp = Path(tempfile.mkdtemp())
        agent = {**_BASIC_AGENT, "allowed_tools": ["repo.git.push"],
                 "denied_tools": []}
        engine = _make_engine(tmp, [agent], _BASIC_GLOBAL)
        with self.assertRaises(ElevatedToolRequest) as ctx:
            engine.enforce("test_agent", "repo.git.push")
        self.assertTrue(ctx.exception.decision.elevated)

    def test_check_without_elevation_allows_elevated(self):
        tmp = Path(tempfile.mkdtemp())
        agent = {**_BASIC_AGENT, "allowed_tools": ["repo.git.push"],
                 "denied_tools": []}
        engine = _make_engine(tmp, [agent], _BASIC_GLOBAL)
        decision = engine.check_without_elevation("test_agent", "repo.git.push")
        self.assertTrue(decision.allowed)
        self.assertTrue(decision.elevated)


class TestEvaluationOrder(unittest.TestCase):
    """Verify the 5-layer evaluation order is correct."""

    def test_layer_order_global_deny_first(self):
        """Global deny evaluated before everything else."""
        tmp = Path(tempfile.mkdtemp())
        gp = {**_BASIC_GLOBAL, "global_deny": ["web.search"]}
        engine = _make_engine(tmp, [_BASIC_AGENT], gp)
        # web.search is in agent_allow AND global_deny — deny wins
        decision = engine.check_tool_access("test_agent", "web.search")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.layer, "global_deny")

    def test_agent_deny_before_global_allow(self):
        tmp = Path(tempfile.mkdtemp())
        gp = {**_BASIC_GLOBAL, "global_allow": ["shell.run"]}
        engine = _make_engine(tmp, [_BASIC_AGENT], gp)
        # shell.run in agent deny AND global allow — agent deny wins
        decision = engine.check_tool_access("test_agent", "shell.run")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.layer, "agent_deny")

    def test_global_allow_before_agent_allow(self):
        tmp = Path(tempfile.mkdtemp())
        engine = _make_engine(tmp, [_BASIC_AGENT], _BASIC_GLOBAL)
        # repo.files.read is in global_allow AND agent_allow — global first
        decision = engine.check_tool_access("test_agent", "repo.files.read")
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.layer, "global_allow")


class TestBackwardCompatibility(unittest.TestCase):
    """Existing PolicyEngine interface must still work."""

    def test_enforce_raises_violation(self):
        tmp = Path(tempfile.mkdtemp())
        engine = _make_engine(tmp, [_BASIC_AGENT], _BASIC_GLOBAL)
        with self.assertRaises(PolicyViolation):
            engine.enforce("test_agent", "unknown.tool")

    def test_enforce_returns_decision_on_allow(self):
        tmp = Path(tempfile.mkdtemp())
        engine = _make_engine(tmp, [_BASIC_AGENT], _BASIC_GLOBAL)
        decision = engine.enforce("test_agent", "web.search")
        self.assertTrue(decision.allowed)

    def test_check_budget_works(self):
        tmp = Path(tempfile.mkdtemp())
        engine = _make_engine(tmp, [_BASIC_AGENT], _BASIC_GLOBAL)
        d = engine.check_budget("test_agent", 5, 30.0, 0)
        self.assertTrue(d.allowed)

    def test_budget_exhaustion(self):
        tmp = Path(tempfile.mkdtemp())
        engine = _make_engine(tmp, [_BASIC_AGENT], _BASIC_GLOBAL)
        d = engine.check_budget("test_agent", 10, 30.0, 0)
        self.assertFalse(d.allowed)

    def test_check_spawn_allowed(self):
        tmp = Path(tempfile.mkdtemp())
        engine = _make_engine(tmp, [_BASIC_AGENT], _BASIC_GLOBAL)
        d = engine.check_spawn_allowed("test_agent")
        self.assertFalse(d.allowed)

    def test_requires_verification(self):
        tmp = Path(tempfile.mkdtemp())
        engine = _make_engine(tmp, [_BASIC_AGENT], _BASIC_GLOBAL)
        self.assertTrue(engine.requires_verification("shell.run"))
        self.assertFalse(engine.requires_verification("web.search"))

    def test_unknown_agent_denied(self):
        tmp = Path(tempfile.mkdtemp())
        engine = _make_engine(tmp, [_BASIC_AGENT], _BASIC_GLOBAL)
        d = engine.check_tool_access("nonexistent", "web.search")
        self.assertFalse(d.allowed)

    def test_wildcard_matching(self):
        self.assertTrue(PolicyEngine._matches("web.search", "web.*"))
        self.assertTrue(PolicyEngine._matches("web", "web.*"))
        self.assertFalse(PolicyEngine._matches("webhook", "web.*"))
        self.assertTrue(PolicyEngine._matches("shell.run", "shell.run"))
        self.assertFalse(PolicyEngine._matches("shell.exec", "shell.run"))

    def test_reload_clears_cache(self):
        tmp = Path(tempfile.mkdtemp())
        engine = _make_engine(tmp, [_BASIC_AGENT], _BASIC_GLOBAL)
        engine._load_global_policy()
        self.assertIsNotNone(engine._global_policy)
        engine.reload()
        self.assertIsNone(engine._global_policy)
        self.assertIsNone(engine._registry)
        self.assertIsNone(engine._policies)


class TestDefaultGlobalPolicy(unittest.TestCase):
    """When no global_policy.json exists, defaults should apply."""

    def test_default_policy_used(self):
        tmp = Path(tempfile.mkdtemp())
        # Don't create global_policy.json — engine should use defaults
        reg_path = tmp / "registry.json"
        reg_path.write_text(json.dumps({"agents": [_BASIC_AGENT]}))
        pol_path = tmp / "agent_policies.json"
        pol_path.write_text("{}")

        engine = PolicyEngine(
            registry_path=reg_path,
            policies_path=pol_path,
            global_policy_path=tmp / "nonexistent_global.json",
        )
        # shell.rm_rf is in default global deny
        self.assertTrue(engine.is_globally_denied("shell.rm_rf"))

    def test_default_elevated_tools(self):
        tmp = Path(tempfile.mkdtemp())
        reg_path = tmp / "registry.json"
        reg_path.write_text(json.dumps({"agents": [_BASIC_AGENT]}))

        engine = PolicyEngine(
            registry_path=reg_path,
            policies_path=tmp / "x.json",
            global_policy_path=tmp / "nonexistent.json",
        )
        # shell.run is in default elevated list
        self.assertTrue(engine.is_elevated("shell.run"))


class TestGlobalPolicyFileExists(unittest.TestCase):
    """Verify the global policy file was created properly."""

    def test_file_exists(self):
        gp = Path("/home/nova/nova-core/STATE/policies/global_policy.json")
        self.assertTrue(gp.exists())

    def test_has_required_keys(self):
        gp = json.loads(
            Path("/home/nova/nova-core/STATE/policies/global_policy.json").read_text()
        )
        self.assertIn("global_deny", gp)
        self.assertIn("global_allow", gp)
        self.assertIn("elevated", gp)
        self.assertIn("default", gp)

    def test_destructive_ops_globally_denied(self):
        gp = json.loads(
            Path("/home/nova/nova-core/STATE/policies/global_policy.json").read_text()
        )
        deny = gp["global_deny"]
        self.assertIn("shell.rm_rf", deny)
        self.assertIn("system.shutdown", deny)


if __name__ == "__main__":
    unittest.main()
