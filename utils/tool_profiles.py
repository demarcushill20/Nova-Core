"""MCP tool allowlist profiles for Claude CLI sub-agent invocations.

Maps task classes to tool allowlists so sub-agents only load the MCP tools
they actually need, instead of all 120+ tools from 14 servers.

Usage:
    from utils.tool_profiles import get_allowed_tools_args

    args = get_allowed_tools_args("research")
    # ["--allowedTools", "Bash,Read,Write,...,mcp__tavily__tavily_search,..."]

    args = get_allowed_tools_args("fallback")
    # []  (no restriction, all tools available)
"""

import logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tool name lists
# ---------------------------------------------------------------------------

# Core CLI tools (always available, no MCP needed)
CORE_TOOLS = [
    "Bash",
    "Read",
    "Write",
    "Edit",
    "Glob",
    "Grep",
    "Agent",
    "Skill",
    "ToolSearch",
]

# Fusion Memory MCP
MEMORY_TOOLS = [
    "mcp__nova-memory__query_memory",
    "mcp__nova-memory__upsert_memory",
    "mcp__nova-memory__bulk_upsert_memory",
    "mcp__nova-memory__create_checkpoint",
    "mcp__nova-memory__get_last_checkpoint",
    "mcp__nova-memory__get_recent_events",
    "mcp__nova-memory__get_session_events",
    "mcp__nova-memory__delete_memory",
    "mcp__nova-memory__check_health",
]

# Obsidian vault MCP
VAULT_TOOLS = [
    "mcp__nova-vault__vault_read",
    "mcp__nova-vault__vault_write",
    "mcp__nova-vault__vault_search",
    "mcp__nova-vault__vault_list",
    "mcp__nova-vault__vault_info",
    "mcp__nova-vault__vault_frontmatter",
    "mcp__nova-vault__vault_update",
    "mcp__nova-vault__vault_validate",
]

# Web search / fetch
SEARCH_TOOLS = [
    "mcp__tavily__tavily_search",
    "mcp__tavily__tavily_research",
    "mcp__tavily__tavily_extract",
    "mcp__tavily__tavily_crawl",
    "mcp__tavily__tavily_map",
    "mcp__fetch__fetch",
    "WebFetch",
    "WebSearch",
]

# GitHub MCP
GITHUB_TOOLS = [
    "mcp__github__search_code",
    "mcp__github__search_issues",
    "mcp__github__list_commits",
    "mcp__github__get_file_contents",
    "mcp__github__create_or_update_file",
    "mcp__github__create_pull_request",
    "mcp__github__list_pull_requests",
    "mcp__github__pull_request_read",
    "mcp__github__issue_read",
    "mcp__github__issue_write",
    "mcp__github__add_issue_comment",
]

# Context7 docs lookup
CONTEXT_TOOLS = [
    "mcp__context7__resolve-library-id",
    "mcp__context7__query-docs",
]

# ---------------------------------------------------------------------------
# Profiles: task class -> tool allowlist
# ---------------------------------------------------------------------------

PROFILES: dict[str, list[str] | None] = {
    "heartbeat_agent": CORE_TOOLS,
    "research": CORE_TOOLS + MEMORY_TOOLS + VAULT_TOOLS + SEARCH_TOOLS + CONTEXT_TOOLS,
    "planning": CORE_TOOLS + MEMORY_TOOLS + VAULT_TOOLS,
    "code_impl": CORE_TOOLS + MEMORY_TOOLS + GITHUB_TOOLS + CONTEXT_TOOLS,
    "code_review": CORE_TOOLS + MEMORY_TOOLS + GITHUB_TOOLS,
    "simple": CORE_TOOLS,
    "monitoring": CORE_TOOLS,
    "system": CORE_TOOLS + MEMORY_TOOLS + GITHUB_TOOLS,
    "fallback": None,  # None = no --allowedTools flag (all tools available)
}

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_tool_profile(task_class: str) -> list[str] | None:
    """Return the tool allowlist for a task class, or None for fallback/unknown.

    Args:
        task_class: One of the keys in PROFILES, or any string.

    Returns:
        List of tool name strings if the profile restricts tools,
        or None if all tools should be available (fallback).
    """
    profile = PROFILES.get(task_class)
    if profile is not None:
        logger.debug("tool_profile selected: %s (%d tools)", task_class, len(profile))
    else:
        if task_class not in PROFILES:
            logger.debug(
                "tool_profile: unknown task_class %r, using fallback (all tools)",
                task_class,
            )
        else:
            logger.debug("tool_profile: %s = fallback (all tools)", task_class)
    return profile


def get_allowed_tools_args(task_class: str) -> list[str]:
    """Return CLI args for --allowedTools, or empty list if unrestricted.

    Args:
        task_class: One of the keys in PROFILES, or any string.

    Returns:
        ["--allowedTools", "Tool1,Tool2,..."] if the profile restricts tools,
        or [] if all tools should be available.
    """
    profile = get_tool_profile(task_class)
    if profile is None:
        return []
    return ["--allowedTools", ",".join(profile)]


def get_profile_names() -> list[str]:
    """Return the list of available profile names."""
    return list(PROFILES.keys())
