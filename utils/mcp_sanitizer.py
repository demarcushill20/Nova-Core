"""MCP response sanitization and tool integrity monitoring.

Provides defense against rug-pull attacks (modified tool descriptions),
prompt injection via tool responses, secret leakage in MCP results,
and canary token detection for system prompt leaks.

Phase 3.1 of Security Hardening Plan (2026-03-12).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

_log = logging.getLogger("nova.mcp_sanitizer")

_BASE = Path("/home/nova/nova-core")
_STATE_DIR = _BASE / "STATE"
_TOOL_HASHES_FILE = _STATE_DIR / "mcp_tool_hashes.json"

# ---------------------------------------------------------------------------
# Size limits
# ---------------------------------------------------------------------------

MAX_RESPONSE_BYTES = 100 * 1024  # 100 KB

# ---------------------------------------------------------------------------
# Invisible / control character patterns
# ---------------------------------------------------------------------------

_ZERO_WIDTH = re.compile(
    r"[\u200b\u200c\u200d\u200e\u200f\u2060\u2061\u2062\u2063"
    r"\u2064\ufeff\u00ad\u034f\u061c\u115f\u1160\u17b4\u17b5"
    r"\u180e\u2066\u2067\u2068\u2069\u206a\u206b\u206c\u206d"
    r"\u206e\u206f]"
)

_UNICODE_TAGS = re.compile(r"[\U000e0001-\U000e007f]")

# Control characters except \n (\x0a) and \t (\x09).
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# ---------------------------------------------------------------------------
# Prompt injection patterns (used for MCP response scanning)
# ---------------------------------------------------------------------------

# Try to import patterns from the input_security module for reuse;
# fall back to a self-contained set if unavailable.
_INJECTION_PATTERNS: list[tuple[re.Pattern[str], int, str]] = []

try:
    from telegram.input_security import _INJECTION_RE as _IMPORTED_INJECTION_RE  # type: ignore[import-untyped]

    _INJECTION_PATTERNS = list(_IMPORTED_INJECTION_RE)
except Exception:
    _FALLBACK_INJECTION = [
        (
            r"(?i)ignore\s+(?:all\s+)?(?:previous|prior|above|earlier)"
            r"\s+(?:instructions?|prompts?|rules?)",
            30,
            "instruction_override",
        ),
        (
            r"(?i)disregard\s+(?:all\s+)?(?:previous|prior|above)"
            r"\s+(?:instructions?|prompts?)",
            30,
            "instruction_override",
        ),
        (r"(?i)forget\s+(?:all\s+)?(?:your|the)\s+(?:instructions?|rules?|guidelines?)", 25, "instruction_override"),
        (r"(?i)you\s+are\s+now\s+(?:a\s+)?(?:different|new|unrestricted|evil|DAN)", 35, "role_coercion"),
        (r"(?i)(?:enter|switch\s+to|activate)\s+(?:developer|god|admin|root|sudo|DAN)\s+mode", 35, "role_coercion"),
        (
            r"(?i)(?:show|reveal|display|print|output|repeat|tell\s+me)"
            r"\s+(?:your|the)\s+(?:system|initial|original|hidden)"
            r"\s+(?:prompt|instructions?|message)",
            25,
            "prompt_extraction",
        ),
        (r"<\|(?:endoftext|im_end|end_turn|system|assistant)\|>", 30, "delimiter_break"),
        (r"\[INST\]|\[/INST\]|<<SYS>>|<</SYS>>", 25, "delimiter_break"),
        (r"(?i)\b(?:jailbreak|DAN|do\s+anything\s+now|STAN|DUDE|AIM)\b", 20, "jailbreak_keyword"),
    ]
    _INJECTION_PATTERNS = [(re.compile(p), score, name) for p, score, name in _FALLBACK_INJECTION]

# Additional instruction-like language patterns specific to MCP responses.
_INSTRUCTION_PATTERNS = [
    (re.compile(r"(?i)\byou\s+must\b"), 15, "instruction_language"),
    (re.compile(r"(?i)\bignore\s+previous\b"), 30, "instruction_language"),
    (re.compile(r"(?i)\byour\s+new\s+instructions?\b"), 35, "instruction_language"),
    (
        re.compile(
            r"(?i)\bdo\s+not\s+(?:follow|obey)"
            r"\s+(?:your|the)\s+(?:original|previous)\b"
        ),
        30,
        "instruction_language",
    ),
    (
        re.compile(
            r"(?i)\boverride\s+(?:your|all)"
            r"\s+(?:previous|prior|existing)\b"
        ),
        30,
        "instruction_language",
    ),
    (re.compile(r"(?i)\bfrom\s+now\s+on\s+you\s+(?:are|will|should|must)\b"), 25, "instruction_language"),
    (
        re.compile(
            r"(?i)\bact\s+as\s+(?:if\s+)?(?:you\s+(?:are|were)\s+)?"
            r"(?:a\s+)?(?:hacker|admin|root|system)\b"
        ),
        30,
        "instruction_language",
    ),
]

# ---------------------------------------------------------------------------
# Secret detection patterns
# ---------------------------------------------------------------------------

# Reuse from task_validator if importable; otherwise define our own.
_SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = []

try:
    from utils.task_validator import _SECRET_RE as _IMPORTED_SECRET_RE  # type: ignore[import-untyped]

    _SECRET_PATTERNS = list(_IMPORTED_SECRET_RE)
except Exception:
    _FALLBACK_SECRETS = [
        (r"sk-ant-api03-[A-Za-z0-9_-]{90,}", "anthropic_api_key"),
        (r"sk-proj-[A-Za-z0-9_-]{80,200}", "openai_api_key"),
        (r"ghp_[A-Za-z0-9]{36}", "github_token"),
        (r"[0-9]{8,10}:[A-Za-z0-9_-]{35}", "telegram_bot_token"),
        (r"xox[pboa]-[0-9]{10,13}-", "slack_token"),
        (r"AKIA[0-9A-Z]{16}", "aws_access_key"),
        (r"-----BEGIN (?:RSA |EC )?PRIVATE KEY-----", "private_key"),
    ]
    _SECRET_PATTERNS = [(re.compile(p), name) for p, name in _FALLBACK_SECRETS]


# ===================================================================
# SanitizeResult
# ===================================================================


@dataclass
class SanitizeResult:
    """Result of MCP response sanitization."""

    clean_text: str
    original_length: int
    was_truncated: bool = False
    injection_detected: bool = False
    secrets_detected: bool = False
    patterns_found: list[str] = field(default_factory=list)


# ===================================================================
# ToolIntegrityMonitor
# ===================================================================


class ToolIntegrityMonitor:
    """Detects rug-pull attacks by tracking tool description hashes.

    Tool descriptions are hashed on first registration and verified on
    subsequent calls.  Modified descriptions indicate a possible rug-pull
    (the MCP server changed what a tool claims to do).

    Hashes are persisted to ``STATE/mcp_tool_hashes.json`` so changes
    are detected across process restarts.
    """

    def __init__(self, hash_file: Path | str | None = None) -> None:
        self._hash_file = Path(hash_file) if hash_file else _TOOL_HASHES_FILE
        self._lock = threading.Lock()
        self._hashes: dict[str, str] = {}
        self._modified: set[str] = set()
        self._load()

    # ---- persistence ----

    def _load(self) -> None:
        """Load stored hashes from disk."""
        if self._hash_file.exists():
            try:
                data = json.loads(self._hash_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._hashes = data
            except (json.JSONDecodeError, OSError) as exc:
                _log.warning("Failed to load tool hashes: %s", exc)

    def _save(self) -> None:
        """Persist hashes to disk."""
        try:
            self._hash_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._hash_file.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self._hashes, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            tmp.replace(self._hash_file)
        except OSError as exc:
            _log.warning("Failed to save tool hashes: %s", exc)

    # ---- helpers ----

    @staticmethod
    def _hash_description(description: str) -> str:
        return hashlib.sha256(description.encode("utf-8")).hexdigest()

    # ---- public API ----

    def register_tool(self, name: str, description: str) -> None:
        """Hash and store a tool's description.

        If the tool has already been registered and the description
        differs, it is recorded as modified but the new hash is stored
        so that future checks compare against the latest version.
        """
        new_hash = self._hash_description(description)
        with self._lock:
            existing = self._hashes.get(name)
            if existing is not None and existing != new_hash:
                self._modified.add(name)
                _log.warning(
                    "TOOL_DESCRIPTION_CHANGED tool=%s old_hash=%.12s new_hash=%.12s",
                    name,
                    existing,
                    new_hash,
                )
            self._hashes[name] = new_hash
            self._save()

    def verify_tool(self, name: str, description: str) -> bool:
        """Return True if the tool description matches the stored hash.

        Returns True for unknown tools (first encounter).
        """
        new_hash = self._hash_description(description)
        with self._lock:
            existing = self._hashes.get(name)
            if existing is None:
                return True
            if existing != new_hash:
                self._modified.add(name)
                _log.warning(
                    "TOOL_INTEGRITY_FAIL tool=%s expected=%.12s got=%.12s",
                    name,
                    existing,
                    new_hash,
                )
                return False
            return True

    def get_modified_tools(self) -> list[str]:
        """Return the list of tools whose descriptions have changed."""
        with self._lock:
            return sorted(self._modified)


# ===================================================================
# MCPResponseSanitizer
# ===================================================================


class MCPResponseSanitizer:
    """Sanitize MCP tool responses before they reach the LLM.

    Checks performed:
    - Size limit enforcement (truncation at MAX_RESPONSE_BYTES)
    - Invisible Unicode stripping (zero-width, RTL/LTR overrides)
    - Control character stripping (except ``\\n``, ``\\t``)
    - Prompt injection pattern detection
    - Instruction-like language detection
    - Secret / credential detection
    """

    def sanitize(self, response: str, tool_name: str = "") -> SanitizeResult:
        """Run all sanitization checks on an MCP response.

        Parameters
        ----------
        response:
            Raw MCP tool response text.
        tool_name:
            Optional tool name for log context.

        Returns
        -------
        SanitizeResult
            Contains the cleaned text and detection flags.
        """
        original_length = len(response)
        patterns_found: list[str] = []
        was_truncated = False

        text = response

        # --- Size limit ---
        if len(text.encode("utf-8", errors="replace")) > MAX_RESPONSE_BYTES:
            # Truncate by character count (approximate; safe side).
            # Walk backwards to find the byte boundary.
            while len(text.encode("utf-8", errors="replace")) > MAX_RESPONSE_BYTES and text:
                text = text[: len(text) - 1024]
            was_truncated = True
            patterns_found.append("size_truncated")
            _log.info(
                "MCP_RESPONSE_TRUNCATED tool=%s original=%d truncated=%d",
                tool_name,
                original_length,
                len(text),
            )

        # --- Strip invisible unicode ---
        zw_count = len(_ZERO_WIDTH.findall(text))
        tag_count = len(_UNICODE_TAGS.findall(text))
        invisible_count = zw_count + tag_count
        if invisible_count > 0:
            text = _ZERO_WIDTH.sub("", text)
            text = _UNICODE_TAGS.sub("", text)
            patterns_found.append(f"invisible_chars_stripped:{invisible_count}")
            _log.info(
                "MCP_INVISIBLE_CHARS tool=%s count=%d",
                tool_name,
                invisible_count,
            )

        # --- Strip control characters (except \n, \t) ---
        ctrl_matches = _CONTROL_CHARS.findall(text)
        if ctrl_matches:
            text = _CONTROL_CHARS.sub("", text)
            patterns_found.append(f"control_chars_stripped:{len(ctrl_matches)}")

        # --- Prompt injection detection ---
        injection_score = 0
        injection_detected = False
        for pattern, score, name in _INJECTION_PATTERNS:
            if pattern.search(text):
                injection_score += score
                patterns_found.append(f"injection:{name}")

        # --- Instruction-like language ---
        for pattern, score, name in _INSTRUCTION_PATTERNS:
            if pattern.search(text):
                injection_score += score
                patterns_found.append(f"instruction:{name}")

        if injection_score > 0:
            injection_detected = True
            _log.warning(
                "MCP_INJECTION_DETECTED tool=%s score=%d patterns=%s",
                tool_name,
                injection_score,
                [p for p in patterns_found if p.startswith(("injection:", "instruction:"))],
            )

        # --- Secret detection ---
        secrets_detected = False
        for pattern, name in _SECRET_PATTERNS:
            if pattern.search(text):
                secrets_detected = True
                patterns_found.append(f"secret:{name}")
                _log.warning(
                    "MCP_SECRET_DETECTED tool=%s pattern=%s",
                    tool_name,
                    name,
                )

        return SanitizeResult(
            clean_text=text,
            original_length=original_length,
            was_truncated=was_truncated,
            injection_detected=injection_detected,
            secrets_detected=secrets_detected,
            patterns_found=patterns_found,
        )


# ===================================================================
# CanaryTokenManager
# ===================================================================


class CanaryTokenManager:
    """Manages canary tokens for detecting system prompt leakage.

    Canary tokens are unique UUID-based strings injected into system
    prompts.  If a canary appears in tool responses or user-visible
    output, it indicates the system prompt has leaked.
    """

    _PREFIX = "CANARY-"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # canary_id -> context string
        self._canaries: dict[str, str] = {}

    def generate_canary(self, context: str = "") -> str:
        """Create a unique canary token.

        Parameters
        ----------
        context:
            Optional description of where this canary was placed
            (e.g. ``"system_prompt_v2"``).

        Returns
        -------
        str
            A canary token string of the form ``CANARY-<uuid4>``.
        """
        canary_id = f"{self._PREFIX}{uuid.uuid4()}"
        with self._lock:
            self._canaries[canary_id] = context
        _log.debug("CANARY_GENERATED id=%s context=%s", canary_id, context)
        return canary_id

    def inject_canary(self, system_prompt: str) -> tuple[str, str]:
        """Inject a canary token into a system prompt.

        The canary is appended as an invisible tracking marker at the
        end of the prompt.

        Parameters
        ----------
        system_prompt:
            The original system prompt text.

        Returns
        -------
        tuple[str, str]
            ``(modified_prompt, canary_id)`` where the prompt now
            contains the canary token.
        """
        canary_id = self.generate_canary(context="system_prompt")
        modified = f"{system_prompt}\n<!-- TRACKING: {canary_id} -->"
        return modified, canary_id

    def check_for_canary(self, text: str) -> list[str]:
        """Scan text for any known canary tokens.

        Parameters
        ----------
        text:
            Text to scan (e.g. MCP response, user output).

        Returns
        -------
        list[str]
            List of canary IDs found in the text.
        """
        found: list[str] = []
        with self._lock:
            for canary_id in self._canaries:
                if canary_id in text:
                    found.append(canary_id)
                    context = self._canaries[canary_id]
                    _log.warning(
                        "CANARY_LEAKED id=%s context=%s",
                        canary_id,
                        context,
                    )
        return found


# ===================================================================
# ToolRateLimiter
# ===================================================================


class ToolRateLimiter:
    """Per-tool sliding-window rate limiter.

    Tracks call timestamps in memory and rejects calls that exceed the
    configured rate.  Thread-safe.
    """

    def __init__(
        self,
        max_calls: int = 60,
        window_seconds: float = 60.0,
    ) -> None:
        """
        Parameters
        ----------
        max_calls:
            Maximum number of calls allowed per tool within the window.
        window_seconds:
            Length of the sliding window in seconds.
        """
        self.max_calls = max_calls
        self.window_seconds = window_seconds
        self._lock = threading.Lock()
        # tool_name -> deque of timestamps
        self._windows: dict[str, deque[float]] = {}

    def _get_window(self, tool_name: str) -> deque[float]:
        """Return (or create) the timestamp deque for a tool."""
        if tool_name not in self._windows:
            self._windows[tool_name] = deque()
        return self._windows[tool_name]

    def _prune(self, window: deque[float], now: float) -> None:
        """Remove timestamps outside the sliding window."""
        cutoff = now - self.window_seconds
        while window and window[0] < cutoff:
            window.popleft()

    def check(self, tool_name: str) -> bool:
        """Return True if the tool is within its rate limit.

        Does **not** record a call; use :meth:`record` after a
        successful invocation.
        """
        now = time.monotonic()
        with self._lock:
            window = self._get_window(tool_name)
            self._prune(window, now)
            if len(window) >= self.max_calls:
                _log.warning(
                    "MCP_RATE_LIMIT tool=%s calls=%d limit=%d window=%.0fs",
                    tool_name,
                    len(window),
                    self.max_calls,
                    self.window_seconds,
                )
                return False
            return True

    def record(self, tool_name: str) -> None:
        """Record a tool call timestamp."""
        now = time.monotonic()
        with self._lock:
            window = self._get_window(tool_name)
            self._prune(window, now)
            window.append(now)


# ===================================================================
# Module-level convenience singletons
# ===================================================================

sanitizer = MCPResponseSanitizer()
tool_monitor = ToolIntegrityMonitor()
canary_manager = CanaryTokenManager()
rate_limiter = ToolRateLimiter()
