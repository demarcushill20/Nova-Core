"""Prompt version registry for Langfuse tracking and A/B testing.

Usage:
    from prompts.registry import get_prompt_version, register_prompt

    register_prompt("heartbeat_research", version="1.2", description="Research cycle prompt")
    version = get_prompt_version("heartbeat_research")  # "1.2"
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# In-memory registry — populated at import time or via register_prompt()
_REGISTRY: dict[str, dict] = {}


def register_prompt(name: str, *, version: str = "1.0", description: str = "") -> None:
    """Register a prompt with a name and version."""
    _REGISTRY[name] = {
        "version": version,
        "description": description,
    }
    logger.debug("Registered prompt %s v%s", name, version)


def get_prompt_version(name: str) -> str:
    """Get the version of a registered prompt, or '0.0' if not registered."""
    entry = _REGISTRY.get(name)
    return entry["version"] if entry else "0.0"


def get_prompt_info(name: str) -> dict:
    """Get full info for a registered prompt."""
    return _REGISTRY.get(name, {"version": "0.0", "description": ""})


def list_prompts() -> dict[str, dict]:
    """Return all registered prompts."""
    return dict(_REGISTRY)


# Register known prompts
register_prompt("heartbeat_research", version="1.0", description="Autonomous research cycle prompt")
register_prompt("heartbeat_planning", version="1.0", description="Autonomous planning cycle prompt")
register_prompt("heartbeat_agent", version="1.0", description="Heartbeat agent decision prompt")
register_prompt("task_execution", version="1.0", description="Task execution prompt")
