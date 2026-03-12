"""Data Loss Prevention (DLP) gate for NovaCore.

Intercepts all outbound data (Telegram messages, file writes, memory stores)
and scans for secrets, PII, and sensitive system information before allowing
the operation to proceed.

Phase 3.2 of Security Hardening Plan (2026-03-12).
"""

from __future__ import annotations

import functools
import logging
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

_log = logging.getLogger("nova.dlp")

# ------------------------------------------------------------------
# Optional integration with utils.secrets (may not exist yet)
# ------------------------------------------------------------------

_secrets_scanner: Any = None
try:
    from utils import secrets as _secrets_mod  # type: ignore[import-untyped]

    _secrets_scanner = _secrets_mod
except (ImportError, ModuleNotFoundError):
    pass


# ------------------------------------------------------------------
# Data classes
# ------------------------------------------------------------------


@dataclass
class DLPFinding:
    """A single sensitive-data finding within scanned text."""

    category: str  # "secret", "pii", "system_path", "sensitive_file"
    pattern_name: str
    severity: str  # "critical", "high", "medium", "low"
    redacted_preview: str


@dataclass
class DLPResult:
    """Result of a DLP scan on a piece of text."""

    allowed: bool
    original_text: str
    redacted_text: str
    findings: list[DLPFinding] = field(default_factory=list)
    action: str = "allow"  # "allow", "redact", "block"


# ------------------------------------------------------------------
# Exception
# ------------------------------------------------------------------


class DLPBlockedError(Exception):
    """Raised when DLP blocks outbound data due to critical findings."""

    def __init__(self, result: DLPResult) -> None:
        self.result = result
        names = [f.pattern_name for f in result.findings if f.severity == "critical"]
        super().__init__(
            f"DLP blocked output: critical findings {names}"
        )


# ------------------------------------------------------------------
# Pattern definitions
# ------------------------------------------------------------------

# Each entry: (compiled_regex, pattern_name, category, severity)
_PatternEntry = tuple[re.Pattern[str], str, str, str]


def _build_secret_patterns() -> list[_PatternEntry]:
    """Patterns that detect secrets and credentials in text."""
    raw: list[tuple[str, str, str, str]] = [
        # API keys — common providers
        (r"sk-ant-api03-[A-Za-z0-9_-]{90,}", "anthropic_api_key", "secret", "critical"),
        (r"sk-proj-[A-Za-z0-9_-]{80,200}", "openai_api_key", "secret", "critical"),
        (r"ghp_[A-Za-z0-9]{36}", "github_pat", "secret", "critical"),
        (r"gho_[A-Za-z0-9]{36}", "github_oauth", "secret", "critical"),
        (r"glpat-[A-Za-z0-9_-]{20,}", "gitlab_pat", "secret", "critical"),
        (r"xox[pboa]-[0-9]{10,13}-[A-Za-z0-9-]+", "slack_token", "secret", "critical"),
        (r"AKIA[0-9A-Z]{16}", "aws_access_key", "secret", "critical"),
        (r"[0-9]{8,10}:[A-Za-z0-9_-]{35}", "telegram_bot_token", "secret", "critical"),
        # Generic high-entropy key patterns
        (
            r"(?i)(?:api[_-]?key|api[_-]?secret|access[_-]?token|auth[_-]?token)"
            r"\s*[:=]\s*['\"]?[A-Za-z0-9_/+=-]{20,}['\"]?",
            "generic_api_key",
            "secret",
            "high",
        ),
        (
            r"(?i)(?:password|passwd|pwd)\s*[:=]\s*['\"]?[^\s'\"]{8,}['\"]?",
            "password_assignment",
            "secret",
            "high",
        ),
    ]
    return [(re.compile(p), n, c, s) for p, n, c, s in raw]


def _build_pii_patterns() -> list[_PatternEntry]:
    """Patterns that detect personally identifiable information."""
    raw: list[tuple[str, str, str, str]] = [
        # Email addresses
        (
            r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
            "email_address",
            "pii",
            "medium",
        ),
        # US phone numbers: (xxx) xxx-xxxx, xxx-xxx-xxxx, +1xxxxxxxxxx
        (
            r"(?<!\d)(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}(?!\d)",
            "us_phone_number",
            "pii",
            "medium",
        ),
        # International phone numbers: +XX followed by 7-14 digits
        (
            r"(?<!\d)\+(?!1\d{10}(?!\d))[2-9]\d{1,2}[-.\s]?\d{4,14}(?!\d)",
            "intl_phone_number",
            "pii",
            "medium",
        ),
        # SSN: XXX-XX-XXXX (strict format to reduce false positives)
        (
            r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)",
            "ssn",
            "pii",
            "high",
        ),
        # IP addresses (matched everywhere; severity upgraded contextually)
        (
            r"(?<!\d)(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}"
            r"(?:25[0-5]|2[0-4]\d|[01]?\d\d?)(?!\d)",
            "ip_address",
            "pii",
            "low",
        ),
    ]
    return [(re.compile(p), n, c, s) for p, n, c, s in raw]


def _build_system_path_patterns() -> list[_PatternEntry]:
    """Patterns that detect sensitive system paths."""
    raw: list[tuple[str, str, str, str]] = [
        (r"/etc/novacore/[^\s]+", "novacore_config_path", "system_path", "high"),
        (r"/home/nova/\.ssh/[^\s]+", "ssh_path", "system_path", "high"),
        (r"/home/nova/\.gnupg/[^\s]+", "gpg_path", "system_path", "high"),
        # .env file contents (KEY=VALUE lines that look like env)
        (
            r"(?i)(?:^|\n)[A-Z_]{2,40}=\S+(?:secret|key|token|password|pwd)\S*",
            "env_file_content",
            "system_path",
            "high",
        ),
        # Sensitive directories
        (r"/etc/shadow(?:\s|$|:)", "etc_shadow", "system_path", "critical"),
        (r"/etc/passwd(?:\s|$|:)", "etc_passwd", "system_path", "medium"),
        (r"/root/[^\s]+", "root_home_path", "system_path", "medium"),
    ]
    return [(re.compile(p), n, c, s) for p, n, c, s in raw]


def _build_sensitive_file_patterns() -> list[_PatternEntry]:
    """Patterns that detect sensitive file content embedded in text."""
    raw: list[tuple[str, str, str, str]] = [
        # Private key blocks
        (
            r"-----BEGIN\s+(?:RSA\s+|EC\s+|DSA\s+|OPENSSH\s+)?PRIVATE\s+KEY-----",
            "private_key_block",
            "sensitive_file",
            "critical",
        ),
        # Certificate contents
        (
            r"-----BEGIN\s+CERTIFICATE-----",
            "certificate_block",
            "sensitive_file",
            "medium",
        ),
        # Database connection strings
        (
            r"(?i)(?:mysql|postgres(?:ql)?|mongodb(?:\+srv)?|redis|sqlite)"
            r"://[^\s]{10,}",
            "database_connection_string",
            "sensitive_file",
            "high",
        ),
        # AWS secret key (40-char base64 after known prefix)
        (
            r"(?i)aws[_-]?secret[_-]?access[_-]?key\s*[:=]\s*[A-Za-z0-9/+=]{40}",
            "aws_secret_key",
            "sensitive_file",
            "critical",
        ),
    ]
    return [(re.compile(p), n, c, s) for p, n, c, s in raw]


# Pre-compile all patterns at import time.
_SECRET_PATTERNS = _build_secret_patterns()
_PII_PATTERNS = _build_pii_patterns()
_SYSTEM_PATH_PATTERNS = _build_system_path_patterns()
_SENSITIVE_FILE_PATTERNS = _build_sensitive_file_patterns()

_ALL_PATTERNS: list[_PatternEntry] = (
    _SECRET_PATTERNS
    + _PII_PATTERNS
    + _SYSTEM_PATH_PATTERNS
    + _SENSITIVE_FILE_PATTERNS
)

# Contexts where IP addresses are elevated to "high" severity.
_IP_SENSITIVE_CONTEXTS = frozenset({
    "telegram_outbound",
    "memory_store",
    "file_write",
    "api_response",
})


# ------------------------------------------------------------------
# Credit card validation (Luhn algorithm)
# ------------------------------------------------------------------

_CC_CANDIDATE_RE = re.compile(
    r"(?<!\d)(\d[ -]?){13,19}(?!\d)"
)


def _luhn_check(number: str) -> bool:
    """Return True if *number* (digits only) passes the Luhn algorithm."""
    digits = [int(d) for d in number]
    digits.reverse()
    total = 0
    for i, d in enumerate(digits):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _find_credit_cards(text: str) -> list[re.Match[str]]:
    """Find Luhn-valid credit-card-like digit sequences in *text*."""
    matches: list[re.Match[str]] = []
    for m in _CC_CANDIDATE_RE.finditer(text):
        digits_only = re.sub(r"[^0-9]", "", m.group())
        if 13 <= len(digits_only) <= 19 and _luhn_check(digits_only):
            matches.append(m)
    return matches


# ------------------------------------------------------------------
# DLPGate — main scanner
# ------------------------------------------------------------------


class DLPGate:
    """Thread-safe Data Loss Prevention scanner.

    Scans text for secrets, PII, system paths, and sensitive file content.
    Applies action rules based on severity:
        critical -> block
        high     -> redact
        medium / low -> allow (but log)
    """

    _REDACT_PLACEHOLDER = "[REDACTED]"

    def __init__(self) -> None:
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _redact_preview(match_text: str, max_visible: int = 4) -> str:
        """Build a short preview showing first/last chars with masking."""
        if len(match_text) <= max_visible * 2:
            return "*" * len(match_text)
        return match_text[:max_visible] + "..." + match_text[-max_visible:]

    def _scan_patterns(
        self,
        text: str,
        context: str,
    ) -> tuple[list[DLPFinding], list[tuple[int, int, str]]]:
        """Run all regex patterns and return findings + replacement spans.

        Returns
        -------
        findings : list[DLPFinding]
        replacements : list[tuple[start, end, severity]]
            Spans to redact, sorted by start position.
        """
        findings: list[DLPFinding] = []
        replacements: list[tuple[int, int, str]] = []

        for pattern, name, category, severity in _ALL_PATTERNS:
            for m in pattern.finditer(text):
                effective_severity = severity
                # Elevate IP address severity in sensitive contexts.
                if name == "ip_address" and context in _IP_SENSITIVE_CONTEXTS:
                    effective_severity = "high"

                finding = DLPFinding(
                    category=category,
                    pattern_name=name,
                    severity=effective_severity,
                    redacted_preview=self._redact_preview(m.group()),
                )
                findings.append(finding)
                replacements.append((m.start(), m.end(), effective_severity))

        # Credit card scan (Luhn-validated, separate pass)
        for m in _find_credit_cards(text):
            finding = DLPFinding(
                category="pii",
                pattern_name="credit_card_number",
                severity="high",
                redacted_preview=self._redact_preview(m.group()),
            )
            findings.append(finding)
            replacements.append((m.start(), m.end(), "high"))

        # Sort replacements by start position for clean redaction.
        replacements.sort(key=lambda r: r[0])
        return findings, replacements

    @staticmethod
    def _apply_redactions(
        text: str,
        replacements: list[tuple[int, int, str]],
    ) -> str:
        """Replace spans marked high or critical with [REDACTED]."""
        if not replacements:
            return text

        parts: list[str] = []
        prev_end = 0

        # Merge overlapping/adjacent spans.
        merged: list[tuple[int, int]] = []
        for start, end, severity in replacements:
            if severity not in ("high", "critical"):
                continue
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))

        for start, end in merged:
            parts.append(text[prev_end:start])
            parts.append("[REDACTED]")
            prev_end = end
        parts.append(text[prev_end:])
        return "".join(parts)

    @staticmethod
    def _determine_action(findings: list[DLPFinding]) -> str:
        """Choose the strictest action based on finding severities."""
        if not findings:
            return "allow"
        severities = {f.severity for f in findings}
        if "critical" in severities:
            return "block"
        if "high" in severities:
            return "redact"
        return "allow"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def scan(self, text: str, context: str = "") -> DLPResult:
        """Scan *text* for sensitive data and return a DLPResult.

        Parameters
        ----------
        text:
            The outbound text to scan.
        context:
            Optional context label (e.g. ``"telegram_outbound"``,
            ``"memory_store"``).  Used to adjust severity rules.

        Returns
        -------
        DLPResult
        """
        with self._lock:
            findings, replacements = self._scan_patterns(text, context)

            action = self._determine_action(findings)

            if action == "block":
                redacted = text  # Don't modify; caller must not send.
                allowed = False
            elif action == "redact":
                redacted = self._apply_redactions(text, replacements)
                allowed = True
            else:
                redacted = text
                allowed = True

            result = DLPResult(
                allowed=allowed,
                original_text=text,
                redacted_text=redacted,
                findings=findings,
                action=action,
            )

            # Log findings.
            if findings:
                names = [f.pattern_name for f in findings]
                _log.log(
                    logging.WARNING if action in ("block", "redact") else logging.INFO,
                    "DLP scan [%s] action=%s findings=%s context=%s",
                    action.upper(),
                    action,
                    names,
                    context or "unspecified",
                )

            return result

    def scan_and_redact(self, text: str, context: str = "") -> str:
        """Convenience method: scan and return the redacted text.

        If the scan results in a ``block`` action, the original text is
        returned unchanged (the caller is expected to handle blocking
        via the full :meth:`scan` API or the :func:`dlp_protected`
        decorator).
        """
        result = self.scan(text, context)
        return result.redacted_text


# ------------------------------------------------------------------
# Module-level singleton
# ------------------------------------------------------------------

dlp = DLPGate()


# ------------------------------------------------------------------
# Decorator
# ------------------------------------------------------------------


def dlp_protected(
    action: str = "redact",
    context: str = "",
) -> Callable[..., Any]:
    """Decorator that scans a function's return value through the DLP gate.

    Parameters
    ----------
    action:
        Default DLP action override.  ``"redact"`` replaces sensitive
        data in the return value; ``"block"`` raises
        :class:`DLPBlockedError` on any critical finding.
    context:
        Context label forwarded to :meth:`DLPGate.scan`.

    Usage::

        @dlp_protected(action="redact", context="telegram_outbound")
        def send_message(text: str) -> str:
            ...

    The decorator inspects the return value.  If it is a ``str``, the
    DLP gate scans it and either redacts or blocks depending on the
    findings.  Non-string return values pass through unmodified.
    """

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            result = fn(*args, **kwargs)

            # Only scan string return values.
            if not isinstance(result, str):
                return result

            scan_result = dlp.scan(result, context=context)

            if scan_result.action == "block" or (
                action == "block" and scan_result.findings
            ):
                raise DLPBlockedError(scan_result)

            if scan_result.action == "redact":
                return scan_result.redacted_text

            return result

        return wrapper

    return decorator
