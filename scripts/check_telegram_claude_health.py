#!/usr/bin/env python3
"""Standalone Claude Code health probe for the NovaCore Telegram control plane.

Mirrors what the Telegram bot's /status path reports, but runnable from a shell
for runbook diagnostics. Exits 0 if healthy, 1 if degraded.

Usage:
    python3 scripts/check_telegram_claude_health.py
"""

from __future__ import annotations

import os
import sys

# Ensure repo root is importable when run from anywhere.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from utils.claude_health import check_claude_health  # noqa: E402


def main() -> int:
    claude_bin = os.environ.get("CLAUDE_BIN", "/home/nova/.local/bin/claude")
    timeout_s = int(os.environ.get("NOVA_CLAUDE_HEALTH_TIMEOUT_S", "10"))
    health = check_claude_health(claude_bin, timeout_s=timeout_s)
    if health.ok:
        print(f"Claude Code: healthy (bin={claude_bin})")
        return 0
    print(f"Claude Code: degraded (reason={health.reason}) bin={claude_bin}")
    if health.detail:
        print(f"detail: {health.detail}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
