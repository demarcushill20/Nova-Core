"""Pre-execution task content validator.

Scans task file content for security risks before Claude CLI executes.
Part of Phase 1.5 — watcher path security integration.

Phase 1.5 of Security Hardening Plan (2026-03-12).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from pathlib import Path

_log = logging.getLogger("nova.task_validator")

STATE_DIR = Path("/home/nova/nova-core/STATE")
AUDIT_FILE = STATE_DIR / "task_audit.jsonl"

# --- Risk threshold ---
BLOCK_THRESHOLD = 80
FLAG_THRESHOLD = 50

# --- Dangerous patterns in task content ---

_RISK_PATTERNS = [
    # Reverse shell / remote code execution
    (r"(?i)\b(?:reverse\s+shell|bind\s+shell|meterpreter|netcat\s+-[le]|nc\s+-[le])\b", 40, "reverse_shell"),
    (r"(?i)\bbash\s+-i\s+>&\s+/dev/tcp/", 50, "reverse_shell"),
    (r"(?i)\b(?:python|perl|ruby|php)\s+-[ce]\s+.*(?:socket|connect|exec)\b", 35, "remote_code_exec"),
    # Crypto mining
    (r"(?i)\b(?:xmrig|cryptominer|monero|stratum|nicehash|coinhive)\b", 40, "crypto_mining"),
    # Credential harvesting
    (r"(?i)\b(?:dump|steal|harvest|exfil)\w*\s+(?:cred|password|token|secret|key)\w*\b", 35, "credential_harvesting"),
    (r"(?i)(?:cat|read|print)\s+(?:/etc/shadow|/etc/passwd|~/.ssh/id_)", 30, "credential_access"),
    # Security infrastructure tampering
    (
        r"(?i)(?:modify|edit|delete|remove|disable)\s+(?:the\s+)?"
        r"(?:kill\s*switch|firewall|iptables|egress|security)",
        40,
        "security_tampering",
    ),
    (r"(?i)(?:rm|unlink|delete)\s+/tmp/nova-(?:kill|pause|readonly)", 50, "kill_switch_tampering"),
    (r"(?i)(?:modify|overwrite|replace)\s+.*(?:systemd|\.service|novacore-)", 30, "service_tampering"),
    # Path traversal outside sandbox
    (r"(?:\.\.\/){3,}", 20, "path_traversal"),
    (r"(?i)(?:read|write|access|modify)\s+(?:/etc/|/var/|/root/|/proc/|/sys/)", 25, "system_path_access"),
    # Package installation of suspicious tools
    (r"(?i)pip\s+install\s+(?:pwntools|impacket|responder|mimikatz|lazagne)", 35, "suspicious_package"),
    # Data exfiltration
    (r"(?i)(?:curl|wget|fetch)\s+.*(?:webhook|pastebin|requestbin|ngrok|burp)", 30, "data_exfiltration"),
    (r"(?i)(?:tar|zip|7z)\s+.*(?:\.ssh|\.env|/etc/novacore|secrets)", 25, "sensitive_archive"),
]

_RISK_RE = [(re.compile(p), score, name) for p, score, name in _RISK_PATTERNS]

# --- Secret patterns (should not appear in task text) ---

_SECRET_IN_TASK = [
    (r"sk-ant-api03-[A-Za-z0-9_-]{90,}", "anthropic_api_key"),
    (r"sk-proj-[A-Za-z0-9_-]{80,200}", "openai_api_key"),
    (r"ghp_[A-Za-z0-9]{36}", "github_token"),
    (r"[0-9]{8,10}:[A-Za-z0-9_-]{35}", "telegram_bot_token"),
    (r"xox[pboa]-[0-9]{10,13}-", "slack_token"),
    (r"AKIA[0-9A-Z]{16}", "aws_access_key"),
]

_SECRET_RE = [(re.compile(p), name) for p, name in _SECRET_IN_TASK]


def validate_task_content(task_text: str, task_stem: str = "") -> dict:
    """Validate task content for security risks.

    Returns:
        {
            "safe": bool,
            "risk_score": int (0-100),
            "risks": [{"name": str, "score": int}],
            "secrets_found": [str],
            "blocked": bool,
            "block_reason": str,
        }
    """
    risks = []
    total_score = 0

    # Check risk patterns
    for pattern, score, name in _RISK_RE:
        if pattern.search(task_text):
            risks.append({"name": name, "score": score})
            total_score += score

    # Check for embedded secrets
    secrets_found = []
    for pattern, name in _SECRET_RE:
        if pattern.search(task_text):
            secrets_found.append(name)
            total_score += 20  # Any secret in task text is suspicious

    total_score = min(total_score, 100)
    blocked = total_score >= BLOCK_THRESHOLD

    result = {
        "safe": total_score < FLAG_THRESHOLD,
        "risk_score": total_score,
        "risks": risks,
        "secrets_found": secrets_found,
        "blocked": blocked,
        "block_reason": f"risk_score={total_score}" if blocked else "",
    }

    if blocked:
        _log.warning(
            "TASK_BLOCKED stem=%s risk=%d risks=%s secrets=%s",
            task_stem,
            total_score,
            [r["name"] for r in risks],
            secrets_found,
        )
    elif risks or secrets_found:
        _log.info(
            "TASK_FLAGGED stem=%s risk=%d risks=%s secrets=%s",
            task_stem,
            total_score,
            [r["name"] for r in risks],
            secrets_found,
        )

    return result


def audit_task_execution(
    task_stem: str,
    task_text: str,
    validation_result: dict,
    classification: str = "unknown",
    execution_mode: str = "worker",
) -> None:
    """Write task execution audit record to STATE/task_audit.jsonl."""
    record = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "task_stem": task_stem,
        "content_hash": hashlib.sha256(task_text.encode()).hexdigest()[:16],
        "content_length": len(task_text),
        "classification": classification,
        "execution_mode": execution_mode,
        "risk_score": validation_result["risk_score"],
        "risks": [r["name"] for r in validation_result.get("risks", [])],
        "secrets_found": validation_result.get("secrets_found", []),
        "blocked": validation_result.get("blocked", False),
    }

    try:
        AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except OSError as exc:
        _log.warning("Failed to write task audit: %s", exc)
