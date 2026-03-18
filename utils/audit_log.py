"""Structured, hash-chained audit logging for NovaCore.

Provides tamper-evident logging with SHA-256 chain integrity,
correlation IDs, and security event classification.
"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class AuditEvent:
    """A single tamper-evident audit log entry."""

    timestamp: str
    event: str
    correlation_id: str
    agent: str
    detail: dict[str, Any]
    chain_hash: str = ""
    prev_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"), sort_keys=True)


def _hash_entry(prev_hash: str, entry_dict: dict[str, Any]) -> str:
    """Compute SHA-256 chain hash for an entry.

    The hash covers prev_hash concatenated with the JSON-serialised entry
    (with chain_hash zeroed out so the hash is deterministic).
    """
    hashable = dict(entry_dict)
    hashable["chain_hash"] = ""
    payload = prev_hash + json.dumps(hashable, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class AuditLogger:
    """Hash-chained, daily-rotated JSONL audit logger.

    Parameters
    ----------
    agent_name:
        Identifier for the component writing logs (e.g. "watcher",
        "telegram", "heartbeat").
    log_dir:
        Directory where audit log files are written.  Defaults to
        ``LOGS/audit/`` relative to the nova-core root.
    """

    _BASE = Path("/home/nova/nova-core")

    def __init__(self, agent_name: str, log_dir: Path | str | None = None) -> None:
        self.agent_name = agent_name
        self.log_dir = Path(log_dir) if log_dir else self._BASE / "LOGS" / "audit"
        self._lock = threading.Lock()
        self._current_date: str = ""
        self._prev_hash: str = ""
        # Initialise chain from today's file (if any).
        self._ensure_chain()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _today(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _log_path(self, date_str: str) -> Path:
        return self.log_dir / f"audit_{date_str}.jsonl"

    def _ensure_chain(self) -> None:
        """Load the last chain_hash from today's log file, or start fresh."""
        today = self._today()
        if today == self._current_date:
            return
        self._current_date = today
        log_file = self._log_path(today)
        if log_file.exists():
            last_line = ""
            with open(log_file, encoding="utf-8") as fh:
                for line in fh:
                    stripped = line.strip()
                    if stripped:
                        last_line = stripped
            if last_line:
                try:
                    entry = json.loads(last_line)
                    self._prev_hash = entry.get("chain_hash", "")
                    return
                except json.JSONDecodeError:
                    pass
        # Fresh chain for the day.
        self._prev_hash = hashlib.sha256(f"genesis:{today}".encode()).hexdigest()

    def _write_event(self, event: AuditEvent) -> None:
        """Append an event to the current day's log file."""
        self.log_dir.mkdir(parents=True, exist_ok=True)
        log_file = self._log_path(self._current_date)
        with open(log_file, "a", encoding="utf-8") as fh:
            fh.write(event.to_json() + "\n")

    # ------------------------------------------------------------------
    # Public logging API
    # ------------------------------------------------------------------

    def log(
        self,
        event: str,
        detail: dict[str, Any] | None = None,
        correlation_id: str = "",
    ) -> AuditEvent:
        """Write a hash-chained JSONL audit entry.

        Parameters
        ----------
        event:
            Event type string, e.g. ``"tool_call"``, ``"auth_denied"``.
        detail:
            Arbitrary event-specific payload.
        correlation_id:
            Optional ID for correlating related events across components.
            Auto-generated (UUID4) when omitted or empty.

        Returns
        -------
        AuditEvent
            The persisted event (with hashes populated).
        """
        if detail is None:
            detail = {}
        if not correlation_id:
            correlation_id = uuid.uuid4().hex[:12]

        with self._lock:
            # Handle day rollover.
            self._ensure_chain()

            entry = AuditEvent(
                timestamp=datetime.now(timezone.utc).isoformat(),
                event=event,
                correlation_id=correlation_id,
                agent=self.agent_name,
                detail=detail,
                prev_hash=self._prev_hash,
            )
            entry.chain_hash = _hash_entry(self._prev_hash, entry.to_dict())
            self._prev_hash = entry.chain_hash
            self._write_event(entry)
            return entry

    # ------------------------------------------------------------------
    # Convenience methods for common security events
    # ------------------------------------------------------------------

    def log_security(
        self,
        event: str,
        detail: dict[str, Any] | None = None,
        severity: str = "info",
        correlation_id: str = "",
    ) -> AuditEvent:
        """Log a security-classified event with a severity tag."""
        if detail is None:
            detail = {}
        detail = {"severity": severity, **detail}
        return self.log(f"security.{event}", detail, correlation_id)

    def log_auth_failure(
        self,
        chat_id: int | str,
        user_id: int | str,
        username: str,
        text_preview: str,
    ) -> AuditEvent:
        """Log an authentication / authorisation denial."""
        return self.log_security(
            "auth_denied",
            {
                "chat_id": str(chat_id),
                "user_id": str(user_id),
                "username": username,
                "text_preview": text_preview[:120],
            },
            severity="warning",
        )

    def log_kill_switch(
        self,
        old_mode: str,
        new_mode: str,
        source: str,
    ) -> AuditEvent:
        """Log a kill-switch / operating-mode state transition."""
        return self.log_security(
            "kill_switch_change",
            {
                "old_mode": old_mode,
                "new_mode": new_mode,
                "source": source,
            },
            severity="critical",
        )

    def log_circuit_breaker(
        self,
        breaker_name: str,
        old_state: str,
        new_state: str,
    ) -> AuditEvent:
        """Log a circuit-breaker trip or recovery."""
        severity = "warning" if new_state == "open" else "info"
        return self.log_security(
            "circuit_breaker",
            {
                "breaker": breaker_name,
                "old_state": old_state,
                "new_state": new_state,
            },
            severity=severity,
        )

    def log_budget(
        self,
        scope: str,
        usage: float,
        limit: float,
        action: str,
    ) -> AuditEvent:
        """Log a budget threshold event (warning / enforcement)."""
        return self.log_security(
            "budget",
            {
                "scope": scope,
                "usage": usage,
                "limit": limit,
                "action": action,
            },
            severity="warning" if action == "warn" else "critical",
        )

    def log_injection(
        self,
        input_text_hash: str,
        risk_score: float,
        patterns_matched: list[str],
    ) -> AuditEvent:
        """Log a prompt-injection detection event."""
        return self.log_security(
            "injection_detected",
            {
                "input_text_hash": input_text_hash,
                "risk_score": risk_score,
                "patterns_matched": patterns_matched,
            },
            severity="critical",
        )

    def log_secret_detected(
        self,
        context: str,
        pattern_name: str,
        severity: str = "critical",
    ) -> AuditEvent:
        """Log detection of a secret/credential in output."""
        return self.log_security(
            "secret_detected",
            {
                "context": context[:200],
                "pattern_name": pattern_name,
            },
            severity=severity,
        )


# ------------------------------------------------------------------
# Chain verification
# ------------------------------------------------------------------


def verify_chain(log_file: Path | str) -> tuple[bool, list[str]]:
    """Verify the hash-chain integrity of an audit log file.

    Parameters
    ----------
    log_file:
        Path to a ``audit_YYYY-MM-DD.jsonl`` file.

    Returns
    -------
    tuple[bool, list[str]]
        ``(valid, errors)`` where *valid* is ``True`` when every entry's
        hash is correct and the chain is unbroken.
    """
    log_file = Path(log_file)
    errors: list[str] = []

    if not log_file.exists():
        return False, [f"File not found: {log_file}"]

    prev_hash: str | None = None
    line_no = 0

    with open(log_file, encoding="utf-8") as fh:
        for raw_line in fh:
            stripped = raw_line.strip()
            if not stripped:
                continue
            line_no += 1
            try:
                entry = json.loads(stripped)
            except json.JSONDecodeError as exc:
                errors.append(f"Line {line_no}: invalid JSON — {exc}")
                continue

            # Verify prev_hash linkage.
            if prev_hash is not None and entry.get("prev_hash") != prev_hash:
                errors.append(
                    f"Line {line_no}: prev_hash mismatch (expected {prev_hash!r}, got {entry.get('prev_hash')!r})"
                )

            # Recompute and verify chain_hash.
            expected = _hash_entry(entry.get("prev_hash", ""), entry)
            actual = entry.get("chain_hash", "")
            if expected != actual:
                errors.append(f"Line {line_no}: chain_hash mismatch (expected {expected!r}, got {actual!r})")

            prev_hash = actual

    if line_no == 0:
        errors.append("File is empty")

    return (len(errors) == 0), errors


# ------------------------------------------------------------------
# Module-level singleton and factory
# ------------------------------------------------------------------

# Default logger — overridden per-component via get_audit_logger().
audit = AuditLogger("default")


def get_audit_logger(agent: str, log_dir: Path | str | None = None) -> AuditLogger:
    """Return an AuditLogger for a specific agent/component.

    Callers should keep a reference rather than calling this repeatedly,
    since each call creates a new instance with its own chain state.
    """
    return AuditLogger(agent, log_dir=log_dir)
