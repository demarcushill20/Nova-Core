"""Tests for tools/vault_sync.py — Phase 10 Obsidian Headless Auto-Sync."""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest

from tools.vault_sync import (
    check_sync_after_write,
    _load_sync_config,
    _is_daemon_running,
    _check_ob_available,
    _trigger_oneshot_sync,
    _DEFAULT_SYNC_CONFIG,
)


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

class TestSyncConfigLoading:
    def test_missing_config_returns_defaults(self):
        with patch("tools.vault_sync._SYNC_CONFIG_PATH", Path("/nonexistent/config.json")):
            config = _load_sync_config()
            assert config["enabled"] is False
            assert config["mode"] == "continuous"

    def test_valid_config_loaded(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"enabled": True, "mode": "oneshot"}, f)
            f.flush()
            with patch("tools.vault_sync._SYNC_CONFIG_PATH", Path(f.name)):
                config = _load_sync_config()
                assert config["enabled"] is True
                assert config["mode"] == "oneshot"
                # Defaults merged in
                assert config["ob_binary"] == "ob"

    def test_corrupt_config_returns_defaults(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("not json {{{")
            f.flush()
            with patch("tools.vault_sync._SYNC_CONFIG_PATH", Path(f.name)):
                config = _load_sync_config()
                assert config["enabled"] is False


# ---------------------------------------------------------------------------
# Skip conditions
# ---------------------------------------------------------------------------

class TestSyncSkipConditions:
    def test_skipped_when_write_failed(self):
        result = check_sync_after_write("30-workflow-learnings/test.md", "error")
        assert result["obsidian_sync_attempted"] is False
        assert result["obsidian_sync_result"] == "skipped"

    def test_skipped_when_write_rejected(self):
        result = check_sync_after_write("30-workflow-learnings/test.md", "rejected")
        assert result["obsidian_sync_attempted"] is False
        assert result["obsidian_sync_result"] == "skipped"

    def test_skipped_when_write_status_empty(self):
        result = check_sync_after_write("test.md", "")
        assert result["obsidian_sync_result"] == "skipped"

    def test_disabled_when_config_says_disabled(self):
        with patch("tools.vault_sync._SYNC_CONFIG_PATH", Path("/nonexistent")):
            result = check_sync_after_write("test.md", "created")
            assert result["obsidian_sync_result"] == "disabled"

    def test_unavailable_when_ob_not_found(self):
        config_data = {"enabled": True, "mode": "continuous", "ob_binary": "ob"}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(config_data, f)
            f.flush()
            with patch("tools.vault_sync._SYNC_CONFIG_PATH", Path(f.name)):
                with patch("tools.vault_sync._check_ob_available", return_value=False):
                    result = check_sync_after_write("test.md", "created")
                    assert result["obsidian_sync_result"] == "unavailable"
                    assert "not found" in result["obsidian_sync_error"]


# ---------------------------------------------------------------------------
# Continuous mode (daemon)
# ---------------------------------------------------------------------------

class TestContinuousDaemonMode:
    def _enabled_config(self):
        return {"enabled": True, "mode": "continuous", "ob_binary": "ob"}

    def test_daemon_active(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(self._enabled_config(), f)
            f.flush()
            with patch("tools.vault_sync._SYNC_CONFIG_PATH", Path(f.name)):
                with patch("tools.vault_sync._check_ob_available", return_value=True):
                    with patch("tools.vault_sync._is_daemon_running", return_value=True):
                        result = check_sync_after_write("30-workflow-learnings/test.md", "created")
                        assert result["obsidian_sync_attempted"] is True
                        assert result["obsidian_sync_result"] == "daemon_active"
                        assert result["obsidian_sync_mode"] == "continuous"
                        assert result["obsidian_sync_error"] is None

    def test_daemon_inactive_fallback_to_oneshot(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(self._enabled_config(), f)
            f.flush()
            with patch("tools.vault_sync._SYNC_CONFIG_PATH", Path(f.name)):
                with patch("tools.vault_sync._check_ob_available", return_value=True):
                    with patch("tools.vault_sync._is_daemon_running", return_value=False):
                        with patch("tools.vault_sync._trigger_oneshot_sync", return_value={
                            "oneshot_triggered": True,
                            "oneshot_result": "success",
                        }):
                            result = check_sync_after_write("test.md", "created")
                            assert result["obsidian_sync_result"] == "oneshot_success"

    def test_daemon_inactive_oneshot_fails(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(self._enabled_config(), f)
            f.flush()
            with patch("tools.vault_sync._SYNC_CONFIG_PATH", Path(f.name)):
                with patch("tools.vault_sync._check_ob_available", return_value=True):
                    with patch("tools.vault_sync._is_daemon_running", return_value=False):
                        with patch("tools.vault_sync._trigger_oneshot_sync", return_value={
                            "oneshot_triggered": True,
                            "oneshot_result": "failed",
                            "oneshot_error": "auth expired",
                        }):
                            result = check_sync_after_write("test.md", "created")
                            assert result["obsidian_sync_result"] == "oneshot_failed"
                            assert "auth expired" in result["obsidian_sync_error"]


# ---------------------------------------------------------------------------
# Oneshot mode
# ---------------------------------------------------------------------------

class TestOneshotMode:
    def _enabled_config(self):
        return {"enabled": True, "mode": "oneshot", "ob_binary": "ob"}

    def test_oneshot_success(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(self._enabled_config(), f)
            f.flush()
            with patch("tools.vault_sync._SYNC_CONFIG_PATH", Path(f.name)):
                with patch("tools.vault_sync._check_ob_available", return_value=True):
                    with patch("tools.vault_sync._trigger_oneshot_sync", return_value={
                        "oneshot_triggered": True,
                        "oneshot_result": "success",
                    }):
                        result = check_sync_after_write("test.md", "created")
                        assert result["obsidian_sync_attempted"] is True
                        assert result["obsidian_sync_result"] == "oneshot_success"
                        assert result["obsidian_sync_mode"] == "oneshot"

    def test_oneshot_failure(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(self._enabled_config(), f)
            f.flush()
            with patch("tools.vault_sync._SYNC_CONFIG_PATH", Path(f.name)):
                with patch("tools.vault_sync._check_ob_available", return_value=True):
                    with patch("tools.vault_sync._trigger_oneshot_sync", return_value={
                        "oneshot_triggered": True,
                        "oneshot_result": "timeout",
                        "oneshot_error": "sync timeout",
                    }):
                        result = check_sync_after_write("test.md", "created")
                        assert result["obsidian_sync_result"] == "oneshot_failed"


# ---------------------------------------------------------------------------
# Fail-open behavior
# ---------------------------------------------------------------------------

class TestFailOpen:
    def test_exception_in_sync_returns_error_dict(self):
        """Sync exceptions are caught and returned, never raised."""
        with patch("tools.vault_sync._load_sync_config", side_effect=RuntimeError("kaboom")):
            # Force past the write_status check
            with patch("tools.vault_sync._SYNC_CONFIG_PATH"):
                result = check_sync_after_write("test.md", "created")
                assert result["obsidian_sync_result"] == "error"
                assert "kaboom" in result["obsidian_sync_error"]

    def test_unknown_mode_handled(self):
        config = {"enabled": True, "mode": "weird_mode", "ob_binary": "ob"}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(config, f)
            f.flush()
            with patch("tools.vault_sync._SYNC_CONFIG_PATH", Path(f.name)):
                with patch("tools.vault_sync._check_ob_available", return_value=True):
                    result = check_sync_after_write("test.md", "created")
                    assert result["obsidian_sync_result"] == "unknown_mode"


# ---------------------------------------------------------------------------
# Audit output contract
# ---------------------------------------------------------------------------

class TestAuditOutputContract:
    def test_all_required_fields_present(self):
        result = check_sync_after_write("test.md", "error")
        required = {
            "obsidian_sync_attempted",
            "obsidian_sync_trigger",
            "obsidian_sync_mode",
            "obsidian_sync_result",
            "obsidian_sync_error",
            "obsidian_sync_timestamp",
        }
        assert required.issubset(result.keys())

    def test_trigger_records_write_path(self):
        result = check_sync_after_write("30-workflow-learnings/my-note.md", "error")
        assert result["obsidian_sync_trigger"] == "30-workflow-learnings/my-note.md"

    def test_timestamp_present(self):
        result = check_sync_after_write("test.md", "error")
        assert result["obsidian_sync_timestamp"] is not None
        assert "T" in result["obsidian_sync_timestamp"]  # ISO format


# ---------------------------------------------------------------------------
# Orchestrator integration
# ---------------------------------------------------------------------------

class TestOrchestratorIntegration:
    def test_vault_sync_imported_in_orchestrator(self):
        import tools.orchestrator_adapter as oa
        from tools.vault_sync import check_sync_after_write
        assert callable(check_sync_after_write)

    def test_sync_in_orchestrator_source(self):
        import tools.orchestrator_adapter as oa
        import inspect
        source = inspect.getsource(oa.execute_via_orchestrator)
        assert "check_sync_after_write" in source
        assert "obsidian_sync" in source

    def test_sync_key_in_return(self):
        import tools.orchestrator_adapter as oa
        import inspect
        source = inspect.getsource(oa.execute_via_orchestrator)
        assert '"obsidian_sync"' in source


# ---------------------------------------------------------------------------
# Safety boundaries
# ---------------------------------------------------------------------------

class TestSafetyBoundaries:
    def test_no_vault_write_imports(self):
        """vault_sync module does not import vault write functions."""
        import tools.vault_sync as vs
        # Check the module has no vault write/update function references
        assert not hasattr(vs, "vault_write")
        assert not hasattr(vs, "vault_update")
        # Check no import from mcp_vault_server
        import inspect
        source = inspect.getsource(vs)
        assert "from tools.mcp_vault_server import" not in source
        assert "import vault_write" not in source

    def test_no_vault_mutation_capability(self):
        """Module does not import or use vault mutation tools."""
        import tools.vault_sync as vs
        import inspect
        source = inspect.getsource(vs)
        assert "mcp_vault_server" not in source

    def test_default_config_is_disabled(self):
        """Default sync config has enabled=False for safety."""
        assert _DEFAULT_SYNC_CONFIG["enabled"] is False
