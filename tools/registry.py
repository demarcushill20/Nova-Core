"""Tool registry loader and validator for NovaCore.

Reads tools/tools_registry.json (code-owned, authoritative) and provides
typed access to tool definitions, sandbox root, and audit log paths.
"""

import json
import pathlib

_REQUIRED_TOP_KEYS = ("sandbox_root", "audit_log", "tools")
_REQUIRED_TOOL_KEYS = ("description", "args_schema", "returns", "safety")

_DEFAULT_REGISTRY = pathlib.Path(__file__).parent / "tools_registry.json"


def load_registry(path: str | pathlib.Path | None = None) -> dict:
    """Load and validate the tool registry from disk."""
    p = pathlib.Path(path) if path else _DEFAULT_REGISTRY
    if not p.exists():
        raise FileNotFoundError(f"Registry not found: {p}")
    with p.open() as f:
        registry = json.load(f)
    validate_registry(registry)
    return registry


def get_tool(registry: dict, tool_name: str) -> dict:
    """Return a tool definition by dotted name. Raises KeyError if missing."""
    tools = registry.get("tools", {})
    if tool_name not in tools:
        available = ", ".join(sorted(tools)) or "(none)"
        raise KeyError(f"Unknown tool {tool_name!r}. Available: {available}")
    return tools[tool_name]


def resolve_sandbox_root(registry: dict) -> pathlib.Path:
    """Expand and resolve sandbox_root to an absolute Path."""
    raw = registry.get("sandbox_root", "")
    if not raw:
        raise ValueError("sandbox_root is empty")
    return pathlib.Path(raw).expanduser().resolve()


def resolve_audit_log(registry: dict) -> pathlib.Path:
    """Resolve audit_log path relative to sandbox_root."""
    root = resolve_sandbox_root(registry)
    rel = registry.get("audit_log", "")
    if not rel:
        raise ValueError("audit_log is empty")
    return root / rel


def validate_registry(registry: dict) -> None:
    """Validate registry structure. Raises ValueError on problems."""
    if not isinstance(registry, dict):
        raise ValueError("Registry must be a JSON object")

    # Top-level keys
    for key in _REQUIRED_TOP_KEYS:
        if key not in registry:
            raise ValueError(f"Missing required top-level key: {key!r}")

    # sandbox_root
    sr = registry["sandbox_root"]
    if not isinstance(sr, str) or not sr.strip():
        raise ValueError("sandbox_root must be a non-empty string")

    # audit_log
    al = registry["audit_log"]
    if not isinstance(al, str) or not al.strip():
        raise ValueError("audit_log must be a non-empty string")

    # tools
    tools = registry["tools"]
    if not isinstance(tools, dict):
        raise ValueError("tools must be a JSON object")

    for name, defn in tools.items():
        # Name format
        if "." not in name:
            raise ValueError(
                f"Tool name {name!r} must contain a dot (e.g. 'files.read')"
            )

        if not isinstance(defn, dict):
            raise ValueError(f"Tool {name!r}: definition must be a JSON object")

        # Required fields per tool
        for key in _REQUIRED_TOOL_KEYS:
            if key not in defn:
                raise ValueError(f"Tool {name!r}: missing required key {key!r}")

        if not isinstance(defn["description"], str) or not defn["description"]:
            raise ValueError(f"Tool {name!r}: description must be a non-empty string")

        if not isinstance(defn["args_schema"], dict):
            raise ValueError(f"Tool {name!r}: args_schema must be a JSON object")

        if not isinstance(defn["safety"], list) or not defn["safety"]:
            raise ValueError(f"Tool {name!r}: safety must be a non-empty list")
