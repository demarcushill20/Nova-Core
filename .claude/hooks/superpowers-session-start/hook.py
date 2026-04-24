# /// script
# requires-python = ">=3.8"
# dependencies = []
# ///
"""
Superpowers SessionStart Hook
=============================

Emits a single line of additionalContext at session start (or clear/compact)
so Claude knows which structured-development slash commands are available.

Vendored/adapted from the Superpowers plugin (obra/superpowers, v5.0.7, MIT).
The NovaCore version is intentionally minimal — it does not preload any skill
bodies; the full SKILL.md contents load only when a skill is invoked.

Runtime contract:
  - Input:  SessionStart hook JSON on stdin (we do not currently read it)
  - Output: JSON with hookSpecificOutput.additionalContext
  - Exit:   0 on success; 0 on any failure too (never block a session start)

Keep the emitted context to ≤1 line — it is injected into every session.
"""

from __future__ import annotations

import json
import sys

HINT = (
    "Structured development workflows available: "
    "/brainstorm, /write-plan, /debug, /worktree, /superpowers. "
    "Skills auto-activate on matching triggers; invoke via slash for explicit entry."
)


def main() -> int:
    try:
        payload = {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": HINT,
            }
        }
        sys.stdout.write(json.dumps(payload))
        sys.stdout.flush()
    except Exception as exc:
        # Never block a session start — fail open.
        # stderr keeps stdout clean for the JSON payload; the hook runner
        # routes stderr to logs so failures are still visible.
        sys.stderr.write(f"superpowers-session-start: fail-open on {exc!r}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
