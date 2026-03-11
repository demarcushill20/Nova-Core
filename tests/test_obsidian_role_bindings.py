"""Tests for Phase 9 — Obsidian skill-to-role bindings.

Validates that each Nova-Core agent role has the correct Obsidian vault
tool access: research and planner get read-only, memory gets bounded write,
and all other roles are denied vault access entirely.
"""

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def registry():
    path = ROOT / "STATE" / "agents" / "registry.json"
    return json.loads(path.read_text())


@pytest.fixture(scope="module")
def policies():
    path = ROOT / "STATE" / "policies" / "agent_policies.json"
    return json.loads(path.read_text())


def _agent_by_id(registry, agent_id):
    for agent in registry["agents"]:
        if agent["agent_id"] == agent_id:
            return agent
    raise KeyError(f"Agent {agent_id} not found")


def _agent_by_role(registry, role):
    for agent in registry["agents"]:
        if agent["role"] == role:
            return agent
    raise KeyError(f"Role {role} not found")


# ---------------------------------------------------------------------------
# Vault tool name constants
# ---------------------------------------------------------------------------

VAULT_READ_TOOLS = {"vault.read", "vault.search", "vault.frontmatter"}
VAULT_WRITE_TOOLS = {"vault.write", "vault.validate"}
VAULT_UPDATE_TOOLS = {"vault.update"}
ALL_VAULT_TOOLS = VAULT_READ_TOOLS | VAULT_WRITE_TOOLS | VAULT_UPDATE_TOOLS


# ===================================================================
# Research role — read-only vault access
# ===================================================================

class TestResearchRoleBindings:
    def test_research_has_vault_read_tools(self, registry):
        agent = _agent_by_role(registry, "research")
        allowed = set(agent["allowed_tools"])
        assert VAULT_READ_TOOLS.issubset(allowed)

    def test_research_denied_vault_write(self, registry):
        agent = _agent_by_role(registry, "research")
        denied = set(agent["denied_tools"])
        assert "vault.write" in denied
        assert "vault.validate" in denied
        assert "vault.update" in denied

    def test_research_policy_has_vault_read(self, policies):
        profile = policies["profiles"]["research_safe"]
        allowed = set(profile["allow"])
        assert VAULT_READ_TOOLS.issubset(allowed)

    def test_research_policy_denies_vault_write(self, policies):
        profile = policies["profiles"]["research_safe"]
        denied = set(profile["deny"])
        assert "vault.write" in denied
        assert "vault.update" in denied
        assert "vault.validate" in denied

    def test_research_policy_has_skill_binding(self, policies):
        profile = policies["profiles"]["research_safe"]
        assert "reading-obsidian-memory" in profile["obsidian_skills"]

    def test_research_agent_md_has_vault_section(self):
        path = ROOT / "AGENTS" / "research" / "AGENT.md"
        content = path.read_text()
        assert "reading-obsidian-memory" in content
        assert "vault.read" in content
        assert "No vault mutation" in content


# ===================================================================
# Planner role — read-only vault access (retrieval)
# ===================================================================

class TestPlannerRoleBindings:
    def test_planner_has_vault_read_tools(self, registry):
        agent = _agent_by_role(registry, "planner")
        allowed = set(agent["allowed_tools"])
        assert VAULT_READ_TOOLS.issubset(allowed)

    def test_planner_denied_vault_write(self, registry):
        agent = _agent_by_role(registry, "planner")
        denied = set(agent["denied_tools"])
        assert "vault.write" in denied
        assert "vault.validate" in denied
        assert "vault.update" in denied

    def test_planner_policy_has_vault_read(self, policies):
        profile = policies["profiles"]["planner_readonly"]
        allowed = set(profile["allow"])
        assert VAULT_READ_TOOLS.issubset(allowed)

    def test_planner_policy_denies_vault_write(self, policies):
        profile = policies["profiles"]["planner_readonly"]
        denied = set(profile["deny"])
        assert "vault.write" in denied
        assert "vault.update" in denied
        assert "vault.validate" in denied

    def test_planner_policy_has_skill_binding(self, policies):
        profile = policies["profiles"]["planner_readonly"]
        assert "retrieving-task-patterns" in profile["obsidian_skills"]

    def test_planner_agent_md_has_vault_section(self):
        path = ROOT / "AGENTS" / "planner" / "AGENT.md"
        content = path.read_text()
        assert "retrieving-task-patterns" in content
        assert "vault.read" in content
        assert "No vault mutation" in content


# ===================================================================
# Memory role — bounded vault write access
# ===================================================================

class TestMemoryRoleBindings:
    def test_memory_has_vault_read_tools(self, registry):
        agent = _agent_by_role(registry, "memory")
        allowed = set(agent["allowed_tools"])
        assert VAULT_READ_TOOLS.issubset(allowed)

    def test_memory_has_vault_write_tools(self, registry):
        agent = _agent_by_role(registry, "memory")
        allowed = set(agent["allowed_tools"])
        assert "vault.write" in allowed
        assert "vault.validate" in allowed

    def test_memory_denied_vault_update(self, registry):
        """Memory is create-only — vault.update is denied."""
        agent = _agent_by_role(registry, "memory")
        denied = set(agent["denied_tools"])
        assert "vault.update" in denied

    def test_memory_policy_has_vault_write(self, policies):
        profile = policies["profiles"]["memory_scoped_write"]
        allowed = set(profile["allow"])
        assert "vault.write" in allowed
        assert "vault.validate" in allowed
        assert VAULT_READ_TOOLS.issubset(allowed)

    def test_memory_policy_denies_vault_update(self, policies):
        profile = policies["profiles"]["memory_scoped_write"]
        denied = set(profile["deny"])
        assert "vault.update" in denied

    def test_memory_policy_has_skill_bindings(self, policies):
        profile = policies["profiles"]["memory_scoped_write"]
        skills = profile["obsidian_skills"]
        assert "capturing-workflow-learnings" in skills
        assert "writing-agent-patterns" in skills

    def test_memory_agent_md_has_vault_section(self):
        path = ROOT / "AGENTS" / "memory" / "AGENT.md"
        content = path.read_text()
        assert "capturing-workflow-learnings" in content
        assert "writing-agent-patterns" in content
        assert "vault.write" in content
        assert "vault.validate" in content
        assert "create-only" in content.lower()


# ===================================================================
# Roles that must NOT have vault access
# ===================================================================

class TestVaultDeniedRoles:
    """Coder, critic, verifier, and orchestrator must have no vault access."""

    @pytest.mark.parametrize("role", ["coder", "critic", "verifier", "orchestrator"])
    def test_no_vault_tools_allowed(self, registry, role):
        agent = _agent_by_role(registry, role)
        allowed = set(agent["allowed_tools"])
        vault_allowed = allowed & ALL_VAULT_TOOLS
        assert vault_allowed == set(), f"{role} should not have vault tools, found: {vault_allowed}"

    @pytest.mark.parametrize("role", ["coder", "critic", "verifier", "orchestrator"])
    def test_vault_wildcard_denied(self, registry, role):
        agent = _agent_by_role(registry, role)
        denied = set(agent["denied_tools"])
        assert "vault.*" in denied, f"{role} should have vault.* in denied_tools"

    @pytest.mark.parametrize("profile_name", [
        "coder_scoped_write", "critic_readonly",
        "verifier_readonly", "orchestrator_control",
    ])
    def test_policy_denies_vault(self, policies, profile_name):
        profile = policies["profiles"][profile_name]
        denied = set(profile["deny"])
        assert "vault.*" in denied, f"{profile_name} should deny vault.*"

    @pytest.mark.parametrize("profile_name", [
        "coder_scoped_write", "critic_readonly",
        "verifier_readonly", "orchestrator_control",
    ])
    def test_policy_no_vault_in_allow(self, policies, profile_name):
        profile = policies["profiles"][profile_name]
        allowed = set(profile["allow"])
        vault_allowed = {t for t in allowed if t.startswith("vault.")}
        assert vault_allowed == set(), f"{profile_name} should not allow vault tools"


# ===================================================================
# Operator / reviewer skill binding
# ===================================================================

class TestOperatorAuditBinding:
    def test_audit_skill_exists(self):
        skill_path = ROOT / ".claude" / "skills" / "auditing-obsidian-memory-safety" / "SKILL.md"
        assert skill_path.exists()

    def test_audit_skill_is_read_only(self):
        skill_path = ROOT / ".claude" / "skills" / "auditing-obsidian-memory-safety" / "SKILL.md"
        content = skill_path.read_text()
        if "allowed-tools" in content:
            assert "vault_write" not in content.split(
                "allowed-tools"
            )[0].split("---")[1]
        # Check the skill explicitly says it never writes
        assert "No vault_write" in content or "never writes" in content

    def test_audit_skill_not_bound_to_any_agent_role(self, policies):
        """Audit skill is operator-invocable, not bound to any agent."""
        for name, profile in policies["profiles"].items():
            skills = profile.get("obsidian_skills", [])
            assert "auditing-obsidian-memory-safety" not in skills, \
                f"Audit skill should not be bound to {name} — it's operator-invocable"


# ===================================================================
# Cross-cutting safety checks
# ===================================================================

class TestCrossCuttingSafety:
    def test_only_memory_has_vault_write(self, registry):
        """Only the memory role should have vault.write in allowed_tools."""
        for agent in registry["agents"]:
            if agent["role"] != "memory":
                assert "vault.write" not in agent["allowed_tools"], \
                    f"{agent['role']} should not have vault.write"

    def test_only_memory_has_vault_validate(self, registry):
        """Only the memory role should have vault.validate in allowed_tools."""
        for agent in registry["agents"]:
            if agent["role"] != "memory":
                assert "vault.validate" not in agent["allowed_tools"], \
                    f"{agent['role']} should not have vault.validate"

    def test_no_role_has_vault_update(self, registry):
        """No role should have vault.update in allowed_tools (create-only discipline)."""
        for agent in registry["agents"]:
            assert "vault.update" not in agent["allowed_tools"], \
                f"{agent['role']} should not have vault.update"

    def test_global_memory_writes_limit(self, policies):
        """Global limit on memory writes per workflow exists."""
        assert policies["global_limits"]["max_memory_writes_per_workflow"] == 2

    def test_memory_max_actions_bounded(self, registry):
        """Memory agent has bounded max_actions."""
        agent = _agent_by_role(registry, "memory")
        assert agent["max_actions"] <= 10

    def test_all_roles_present(self, registry):
        """All seven roles are in the registry."""
        roles = {a["role"] for a in registry["agents"]}
        assert roles == {"planner", "orchestrator", "research", "coder", "critic", "verifier", "memory"}

    def test_registry_schema_version(self, registry):
        assert registry["schema_version"] == "1.0"

    def test_policies_schema_version(self, policies):
        assert policies["schema_version"] == "1.0"
