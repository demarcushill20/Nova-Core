"""MCP resource definitions — read-only data exposed to clients.

Resources are registered on the FastMCP server by server.py.
Each function returns a plain string.
"""

from __future__ import annotations

from pathlib import Path

from novatrade.mcp.tools import BASELINE_CONFIG, bt_leaderboard


def leaderboard_current() -> str:
    """Return the current global leaderboard."""
    return bt_leaderboard()


def campaign_status() -> str:
    """Return active campaign status."""
    return "No active campaign"


def config_baseline() -> str:
    """Return the baseline config YAML contents."""
    path = Path(BASELINE_CONFIG)
    if not path.exists():
        return "Baseline config not found"
    return path.read_text()
