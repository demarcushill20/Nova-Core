"""Tests for prompts/registry.py — prompt version registry."""

from __future__ import annotations

import importlib

# ===========================================================================
# Helpers
# ===========================================================================


def _fresh_registry():
    """Re-import the registry module to get a fresh _REGISTRY."""
    import prompts.registry as mod

    importlib.reload(mod)
    return mod


# ===========================================================================
# 1. register_prompt() and get_prompt_version()
# ===========================================================================


class TestRegisterAndGet:
    """Tests for registering and retrieving prompt versions."""

    def test_register_and_retrieve(self):
        """Register a prompt and retrieve its version."""
        mod = _fresh_registry()
        mod.register_prompt("test_prompt", version="2.5", description="A test")
        assert mod.get_prompt_version("test_prompt") == "2.5"

    def test_register_updates_existing(self):
        """Re-registering a prompt updates its version."""
        mod = _fresh_registry()
        mod.register_prompt("updatable", version="1.0", description="First")
        assert mod.get_prompt_version("updatable") == "1.0"

        mod.register_prompt("updatable", version="2.0", description="Updated")
        assert mod.get_prompt_version("updatable") == "2.0"

    def test_register_default_version(self):
        """Registering without version defaults to '1.0'."""
        mod = _fresh_registry()
        mod.register_prompt("default_ver")
        assert mod.get_prompt_version("default_ver") == "1.0"


# ===========================================================================
# 2. get_prompt_version() for unknown prompt
# ===========================================================================


class TestUnknownPrompt:
    """Tests for unknown prompt lookups."""

    def test_unknown_returns_zero(self):
        """Unknown prompt name returns '0.0'."""
        mod = _fresh_registry()
        assert mod.get_prompt_version("nonexistent_prompt") == "0.0"

    def test_unknown_after_registering_others(self):
        """Unknown prompt still returns '0.0' even when registry is populated."""
        mod = _fresh_registry()
        mod.register_prompt("known", version="3.0")
        assert mod.get_prompt_version("unknown") == "0.0"


# ===========================================================================
# 3. get_prompt_info()
# ===========================================================================


class TestGetPromptInfo:
    """Tests for get_prompt_info()."""

    def test_registered_prompt_info(self):
        """Returns full dict for registered prompt."""
        mod = _fresh_registry()
        mod.register_prompt("info_test", version="1.5", description="Info desc")
        info = mod.get_prompt_info("info_test")
        assert info["version"] == "1.5"
        assert info["description"] == "Info desc"

    def test_unknown_prompt_info(self):
        """Returns default dict for unknown prompt."""
        mod = _fresh_registry()
        info = mod.get_prompt_info("no_such_prompt")
        assert info["version"] == "0.0"
        assert info["description"] == ""

    def test_info_has_expected_keys(self):
        """Info dict has version and description keys."""
        mod = _fresh_registry()
        mod.register_prompt("key_test", version="1.0", description="Desc")
        info = mod.get_prompt_info("key_test")
        assert "version" in info
        assert "description" in info


# ===========================================================================
# 4. list_prompts()
# ===========================================================================


class TestListPrompts:
    """Tests for list_prompts()."""

    def test_list_returns_all_registered(self):
        """list_prompts() returns all registered prompts."""
        mod = _fresh_registry()
        # Fresh registry includes the 4 default prompts
        prompts = mod.list_prompts()
        assert len(prompts) >= 4

    def test_list_includes_custom_prompt(self):
        """Custom registered prompt appears in list."""
        mod = _fresh_registry()
        mod.register_prompt("custom_list_test", version="7.0")
        prompts = mod.list_prompts()
        assert "custom_list_test" in prompts
        assert prompts["custom_list_test"]["version"] == "7.0"

    def test_list_returns_copy(self):
        """list_prompts() returns a copy, not the internal dict."""
        mod = _fresh_registry()
        prompts = mod.list_prompts()
        prompts["injected"] = {"version": "999"}
        # Internal registry should not be affected
        assert "injected" not in mod._REGISTRY


# ===========================================================================
# 5. Default prompts registered on import
# ===========================================================================


class TestDefaultPrompts:
    """Tests that default prompts are registered on module import."""

    def test_heartbeat_research_registered(self):
        mod = _fresh_registry()
        assert mod.get_prompt_version("heartbeat_research") == "1.0"

    def test_heartbeat_planning_registered(self):
        mod = _fresh_registry()
        assert mod.get_prompt_version("heartbeat_planning") == "1.0"

    def test_heartbeat_agent_registered(self):
        mod = _fresh_registry()
        assert mod.get_prompt_version("heartbeat_agent") == "1.0"

    def test_task_execution_registered(self):
        mod = _fresh_registry()
        assert mod.get_prompt_version("task_execution") == "1.0"

    def test_default_prompts_have_descriptions(self):
        """All default prompts have non-empty descriptions."""
        mod = _fresh_registry()
        for name in ("heartbeat_research", "heartbeat_planning", "heartbeat_agent", "task_execution"):
            info = mod.get_prompt_info(name)
            assert info["description"], f"{name} should have a description"
