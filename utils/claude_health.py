from __future__ import annotations

import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class ClaudeHealth:
    ok: bool
    reason: str
    detail: str = ""


def classify_claude_error(text: str) -> str:
    lowered = text.lower()
    if "failed to authenticate" in lowered or "401 invalid authentication" in lowered:
        return "auth_failed"
    if "usage limit" in lowered or "rate_limit" in lowered or "rate limit" in lowered:
        return "usage_limited"
    if "not found" in lowered or "no such file" in lowered:
        return "missing_binary"
    return "cli_error"


def check_claude_health(claude_bin: str, timeout_s: int = 20) -> ClaudeHealth:
    cmd = [
        claude_bin,
        "-p",
        "--model",
        "haiku",
        "--no-session-persistence",
        "Reply with exactly OK",
    ]
    try:
        proc = subprocess.run(
            cmd,
            cwd="/home/nova/nova-core",
            text=True,
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
    except FileNotFoundError as exc:
        return ClaudeHealth(False, "missing_binary", str(exc))
    except subprocess.TimeoutExpired:
        return ClaudeHealth(False, "timeout", f"exceeded {timeout_s}s")
    except Exception as exc:
        return ClaudeHealth(False, "exception", str(exc))

    combined = f"{proc.stdout}\n{proc.stderr}".strip()
    if proc.returncode == 0 and proc.stdout.strip() == "OK":
        return ClaudeHealth(True, "ok", "")
    return ClaudeHealth(False, classify_claude_error(combined), combined[:500])
