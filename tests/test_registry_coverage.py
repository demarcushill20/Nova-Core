"""Expanded coverage tests for tools/registry.py.

Targets uncovered lines: get_tool, validate_registry edge cases,
resolve_sandbox_root, resolve_audit_log.
"""

import json

import pytest

from tools.registry import (
    get_tool,
    load_registry,
    resolve_audit_log,
    resolve_sandbox_root,
    validate_registry,
)

# ---------------------------------------------------------------------------
# Minimal valid registry fixture
# ---------------------------------------------------------------------------


def _valid_registry():
    return {
        "sandbox_root": "/tmp/nova-test",
        "audit_log": "STATE/audit.jsonl",
        "tools": {
            "test.echo": {
                "description": "Echo tool",
                "args_schema": {"text": {"type": "string"}},
                "returns": {"text": "string"},
                "safety": ["No risk."],
            },
        },
    }


# ---------------------------------------------------------------------------
# get_tool
# ---------------------------------------------------------------------------


class TestGetTool:
    def test_existing_tool(self):
        reg = _valid_registry()
        tool = get_tool(reg, "test.echo")
        assert tool["description"] == "Echo tool"

    def test_missing_tool_raises(self):
        reg = _valid_registry()
        with pytest.raises(KeyError, match="Unknown tool"):
            get_tool(reg, "missing.tool")

    def test_missing_tool_lists_available(self):
        reg = _valid_registry()
        with pytest.raises(KeyError, match=r"test\.echo"):
            get_tool(reg, "missing.tool")

    def test_empty_tools_shows_none(self):
        reg = _valid_registry()
        reg["tools"] = {}
        with pytest.raises(KeyError, match="none"):
            get_tool(reg, "any.tool")


# ---------------------------------------------------------------------------
# resolve_sandbox_root
# ---------------------------------------------------------------------------


class TestResolveSandboxRoot:
    def test_returns_absolute_path(self):
        reg = _valid_registry()
        p = resolve_sandbox_root(reg)
        assert p.is_absolute()

    def test_expands_tilde(self):
        reg = _valid_registry()
        reg["sandbox_root"] = "~/nova-core"
        p = resolve_sandbox_root(reg)
        assert "~" not in str(p)
        assert p.is_absolute()

    def test_empty_raises(self):
        reg = _valid_registry()
        reg["sandbox_root"] = ""
        with pytest.raises(ValueError, match="empty"):
            resolve_sandbox_root(reg)

    def test_missing_key_raises(self):
        reg = _valid_registry()
        del reg["sandbox_root"]
        # Should fall back to empty string
        with pytest.raises(ValueError, match="empty"):
            resolve_sandbox_root(reg)


# ---------------------------------------------------------------------------
# resolve_audit_log
# ---------------------------------------------------------------------------


class TestResolveAuditLog:
    def test_relative_to_sandbox(self):
        reg = _valid_registry()
        p = resolve_audit_log(reg)
        assert str(p).endswith("STATE/audit.jsonl")
        assert p.is_absolute()

    def test_empty_audit_log_raises(self):
        reg = _valid_registry()
        reg["audit_log"] = ""
        with pytest.raises(ValueError, match="empty"):
            resolve_audit_log(reg)


# ---------------------------------------------------------------------------
# validate_registry
# ---------------------------------------------------------------------------


class TestValidateRegistry:
    def test_valid_passes(self):
        validate_registry(_valid_registry())

    def test_not_dict_raises(self):
        with pytest.raises(ValueError, match="JSON object"):
            validate_registry([])

    def test_missing_sandbox_root(self):
        reg = _valid_registry()
        del reg["sandbox_root"]
        with pytest.raises(ValueError, match="sandbox_root"):
            validate_registry(reg)

    def test_missing_audit_log(self):
        reg = _valid_registry()
        del reg["audit_log"]
        with pytest.raises(ValueError, match="audit_log"):
            validate_registry(reg)

    def test_missing_tools(self):
        reg = _valid_registry()
        del reg["tools"]
        with pytest.raises(ValueError, match="tools"):
            validate_registry(reg)

    def test_sandbox_root_not_string(self):
        reg = _valid_registry()
        reg["sandbox_root"] = 123
        with pytest.raises(ValueError, match="non-empty string"):
            validate_registry(reg)

    def test_sandbox_root_whitespace(self):
        reg = _valid_registry()
        reg["sandbox_root"] = "   "
        with pytest.raises(ValueError, match="non-empty string"):
            validate_registry(reg)

    def test_audit_log_not_string(self):
        reg = _valid_registry()
        reg["audit_log"] = None
        with pytest.raises(ValueError, match="non-empty string"):
            validate_registry(reg)

    def test_tools_not_dict(self):
        reg = _valid_registry()
        reg["tools"] = []
        with pytest.raises(ValueError, match="JSON object"):
            validate_registry(reg)

    def test_tool_name_no_dot(self):
        reg = _valid_registry()
        reg["tools"]["nodot"] = {
            "description": "x",
            "args_schema": {},
            "returns": {},
            "safety": ["ok"],
        }
        with pytest.raises(ValueError, match="must contain a dot"):
            validate_registry(reg)

    def test_tool_defn_not_dict(self):
        reg = _valid_registry()
        reg["tools"]["bad.tool"] = "not a dict"
        with pytest.raises(ValueError, match="must be a JSON object"):
            validate_registry(reg)

    def test_tool_missing_description(self):
        reg = _valid_registry()
        reg["tools"]["bad.tool"] = {
            "args_schema": {},
            "returns": {},
            "safety": ["ok"],
        }
        with pytest.raises(ValueError, match="missing required key"):
            validate_registry(reg)

    def test_tool_empty_description(self):
        reg = _valid_registry()
        reg["tools"]["bad.tool"] = {
            "description": "",
            "args_schema": {},
            "returns": {},
            "safety": ["ok"],
        }
        with pytest.raises(ValueError, match="non-empty string"):
            validate_registry(reg)

    def test_tool_description_not_string(self):
        reg = _valid_registry()
        reg["tools"]["bad.tool"] = {
            "description": 42,
            "args_schema": {},
            "returns": {},
            "safety": ["ok"],
        }
        with pytest.raises(ValueError, match="non-empty string"):
            validate_registry(reg)

    def test_tool_args_schema_not_dict(self):
        reg = _valid_registry()
        reg["tools"]["bad.tool"] = {
            "description": "x",
            "args_schema": "not dict",
            "returns": {},
            "safety": ["ok"],
        }
        with pytest.raises(ValueError, match="args_schema must be a JSON"):
            validate_registry(reg)

    def test_tool_safety_empty(self):
        reg = _valid_registry()
        reg["tools"]["bad.tool"] = {
            "description": "x",
            "args_schema": {},
            "returns": {},
            "safety": [],
        }
        with pytest.raises(ValueError, match="non-empty list"):
            validate_registry(reg)

    def test_tool_safety_not_list(self):
        reg = _valid_registry()
        reg["tools"]["bad.tool"] = {
            "description": "x",
            "args_schema": {},
            "returns": {},
            "safety": "not list",
        }
        with pytest.raises(ValueError, match="non-empty list"):
            validate_registry(reg)

    def test_tool_missing_safety(self):
        reg = _valid_registry()
        reg["tools"]["bad.tool"] = {
            "description": "x",
            "args_schema": {},
            "returns": {},
        }
        with pytest.raises(ValueError, match=r"missing required key.*safety"):
            validate_registry(reg)


# ---------------------------------------------------------------------------
# load_registry
# ---------------------------------------------------------------------------


class TestLoadRegistry:
    def test_load_valid_file(self, tmp_path):
        reg = _valid_registry()
        p = tmp_path / "tools_registry.json"
        p.write_text(json.dumps(reg))
        loaded = load_registry(p)
        assert loaded["sandbox_root"] == "/tmp/nova-test"

    def test_file_not_found(self, tmp_path):
        p = tmp_path / "nonexistent.json"
        with pytest.raises(FileNotFoundError):
            load_registry(p)

    def test_invalid_json(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("{not valid json")
        with pytest.raises(json.JSONDecodeError):
            load_registry(p)

    def test_invalid_structure(self, tmp_path):
        p = tmp_path / "bad_struct.json"
        p.write_text(json.dumps({"only": "partial"}))
        with pytest.raises(ValueError):
            load_registry(p)
