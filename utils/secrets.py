"""Secret detection, redaction, and management for NovaCore.

Provides comprehensive regex-based scanning for 40+ secret patterns,
text redaction, and secret storage validation.

Phase 2.4 of Security Hardening Plan (2026-03-12).
"""
from __future__ import annotations

import math
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SecretPattern:
    """A single secret detection pattern."""

    name: str
    pattern: re.Pattern[str]
    severity: str  # "critical", "high", "medium"


@dataclass
class SecretMatch:
    """A match found by scanning text."""

    pattern_name: str
    severity: str
    start: int
    end: int
    redacted_preview: str = ""


# ---------------------------------------------------------------------------
# Pattern catalogue (40+ patterns)
# ---------------------------------------------------------------------------

def _p(name: str, regex: str, severity: str, flags: int = 0) -> SecretPattern:
    """Helper to build a SecretPattern with compiled regex."""
    return SecretPattern(name=name, pattern=re.compile(regex, flags), severity=severity)


SECRET_PATTERNS: list[SecretPattern] = [
    # ── Cloud providers ────────────────────────────────────────────────
    _p("aws_access_key",         r"(?<![A-Z0-9])AKIA[0-9A-Z]{16}(?![A-Z0-9])",                  "critical"),
    _p("aws_secret_key",         r"(?<![A-Za-z0-9/+=])[A-Za-z0-9/+=]{40}(?![A-Za-z0-9/+=])",    "critical"),
    # AWS secret key is very broad; real usage should pair with aws_access_key context.
    # Narrower: look for assignment-style references.
    _p("aws_secret_key_named",
     r"""(?i)(?:aws_secret_access_key|aws_secret)"""
     r"""\s*[=:]\s*['"]?([A-Za-z0-9/+=]{40})['"]?""", "critical"),
    _p("gcp_service_account",    r'"type"\s*:\s*"service_account"',                              "critical"),
    _p("gcp_api_key",            r"AIza[0-9A-Za-z_-]{35}",                                      "high"),
    _p("azure_connection_string",
     r"(?i)DefaultEndpointsProtocol=https?;"
     r"AccountName=[^;]+;AccountKey=[A-Za-z0-9+/=]+", "critical"),
    _p("azure_sas_token",        r"(?i)[?&]sig=[A-Za-z0-9%+/=]{43,}",                           "high"),

    # ── AI / ML services ──────────────────────────────────────────────
    _p("anthropic_api_key",      r"sk-ant-api03-[A-Za-z0-9_-]{90,}",                            "critical"),
    _p("openai_api_key",         r"sk-proj-[A-Za-z0-9_-]{80,200}",                              "critical"),
    _p("openai_api_key_legacy",  r"sk-[A-Za-z0-9]{48}",                                         "high"),
    _p("huggingface_token",      r"hf_[A-Za-z0-9]{34,}",                                        "high"),
    _p("replicate_api_token",    r"r8_[A-Za-z0-9]{36,}",                                        "high"),
    _p("cohere_api_key",         r"(?i)cohere[_-]?(?:api[_-]?)?key\s*[=:]\s*['\"]?[A-Za-z0-9]{40}['\"]?", "high"),

    # ── Code hosting / CI ─────────────────────────────────────────────
    _p("github_pat",             r"ghp_[A-Za-z0-9]{36}",                                        "critical"),
    _p("github_oauth",           r"gho_[A-Za-z0-9]{36}",                                        "high"),
    _p("github_app_token",       r"ghs_[A-Za-z0-9]{36}",                                        "high"),
    _p("github_refresh_token",   r"ghr_[A-Za-z0-9]{36}",                                        "high"),
    _p("github_fine_grained",    r"github_pat_[A-Za-z0-9]{22}_[A-Za-z0-9]{59}",                 "critical"),
    _p("gitlab_token",           r"glpat-[A-Za-z0-9_-]{20,}",                                   "critical"),
    _p("npm_token",              r"npm_[A-Za-z0-9]{36}",                                         "high"),
    _p("pypi_token",             r"pypi-[A-Za-z0-9_-]{100,}",                                   "high"),
    _p("circleci_token",         r"(?i)circle[_-]?(?:ci[_-]?)?token\s*[=:]\s*['\"]?[a-f0-9]{40}['\"]?", "high"),

    # ── Communication platforms ───────────────────────────────────────
    _p("telegram_bot_token",     r"[0-9]{8,10}:[A-Za-z0-9_-]{35}",                              "critical"),
    _p("slack_token",            r"xox[bsrap]-[0-9]{10,13}-[A-Za-z0-9-]+",                      "critical"),
    _p("slack_webhook",          r"https://hooks\.slack\.com/services/T[A-Z0-9]+/B[A-Z0-9]+/[A-Za-z0-9]+", "high"),
    _p("discord_token",          r"[MN][A-Za-z0-9_-]{23,}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,}", "critical"),
    _p("discord_webhook",        r"https://discord(?:app)?\.com/api/webhooks/\d+/[A-Za-z0-9_-]+", "high"),
    _p("twilio_api_key",         r"SK[0-9a-fA-F]{32}",                                          "high"),

    # ── Payment services ──────────────────────────────────────────────
    _p("stripe_secret_key",      r"sk_live_[A-Za-z0-9]{24,}",                                   "critical"),
    _p("stripe_publishable_key", r"pk_live_[A-Za-z0-9]{24,}",                                   "medium"),
    _p("stripe_restricted_key",  r"rk_live_[A-Za-z0-9]{24,}",                                   "critical"),
    _p("paypal_braintree_token", r"access_token\$production\$[A-Za-z0-9]+\$[A-Fa-f0-9]+",       "critical"),
    _p("square_access_token",    r"sq0atp-[A-Za-z0-9_-]{22,}",                                  "critical"),

    # ── Auth / Tokens ─────────────────────────────────────────────────
    _p("jwt_token",              r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}", "high"),
    _p("bearer_token",           r"(?i)(?:bearer|token)\s+[A-Za-z0-9_-]{20,}",                  "high"),
    _p("basic_auth_header",      r"(?i)(?:basic)\s+[A-Za-z0-9+/=]{20,}",                        "high"),
    _p("oauth_client_secret",    r"(?i)client[_-]?secret\s*[=:]\s*['\"]?[A-Za-z0-9_-]{20,}['\"]?", "high"),
    _p("session_token_generic",
     r"(?i)(?:session|sess)[_-]?(?:token|id|key)"
     r"\s*[=:]\s*['\"]?[A-Za-z0-9_-]{20,}['\"]?", "medium"),

    # ── Private keys ──────────────────────────────────────────────────
    _p("rsa_private_key",        r"-----BEGIN RSA PRIVATE KEY-----",                             "critical"),
    _p("ec_private_key",         r"-----BEGIN EC PRIVATE KEY-----",                              "critical"),
    _p("openssh_private_key",    r"-----BEGIN OPENSSH PRIVATE KEY-----",                         "critical"),
    _p("generic_private_key",    r"-----BEGIN PRIVATE KEY-----",                                 "critical"),
    _p("pgp_private_key",        r"-----BEGIN PGP PRIVATE KEY BLOCK-----",                       "critical"),
    _p("dsa_private_key",        r"-----BEGIN DSA PRIVATE KEY-----",                             "critical"),

    # ── Database connection strings ───────────────────────────────────
    _p("postgres_uri",           r"postgres(?:ql)?://[^\s'\"<>]+:[^\s'\"<>]+@[^\s'\"<>]+",      "critical"),
    _p("mysql_uri",              r"mysql://[^\s'\"<>]+:[^\s'\"<>]+@[^\s'\"<>]+",                 "critical"),
    _p("mongodb_uri",            r"mongodb(?:\+srv)?://[^\s'\"<>]+:[^\s'\"<>]+@[^\s'\"<>]+",    "critical"),
    _p("redis_uri",              r"redis(?:s)?://[^\s'\"<>]*:[^\s'\"<>]+@[^\s'\"<>]+",          "high"),
    _p("amqp_uri",               r"amqps?://[^\s'\"<>]+:[^\s'\"<>]+@[^\s'\"<>]+",              "high"),

    # ── Generic / Catch-all ───────────────────────────────────────────
    _p("password_in_url",        r"://[^/\s'\"<>]+:[^/\s'\"<>]+@",                              "high"),
    _p("password_assignment",    r"(?i)(?:password|passwd|pwd)\s*[=:]\s*['\"]?[^\s'\"]{8,}['\"]?", "medium"),
    _p("api_key_assignment",     r"(?i)(?:api[_-]?key|apikey)\s*[=:]\s*['\"]?[A-Za-z0-9_-]{16,}['\"]?", "medium"),
    _p("secret_assignment",      r"(?i)(?:secret|secret_key)\s*[=:]\s*['\"]?[A-Za-z0-9_-]{16,}['\"]?", "medium"),
    _p("sendgrid_api_key",       r"SG\.[A-Za-z0-9_-]{22}\.[A-Za-z0-9_-]{43}",                  "critical"),
    _p("mailgun_api_key",        r"key-[A-Za-z0-9]{32}",                                        "high"),
]


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

def _make_redacted_preview(text: str, start: int, end: int, *, context: int = 12) -> str:
    """Build a short preview with the matched portion replaced by ***."""
    prefix_start = max(0, start - context)
    suffix_end = min(len(text), end + context)
    prefix = text[prefix_start:start]
    suffix = text[end:suffix_end]
    matched = text[start:end]
    # Show first 4 and last 4 chars of match, mask the rest
    if len(matched) > 10:
        visible = matched[:4] + "***" + matched[-4:]
    else:
        visible = "***"
    return f"...{prefix}{visible}{suffix}..."


def scan_text(text: str) -> list[SecretMatch]:
    """Scan text against all secret patterns and return matches.

    Returns a list of SecretMatch objects, deduplicated by span so
    overlapping pattern matches at the same position are reported once
    (keeping the higher-severity hit).
    """
    severity_rank = {"critical": 3, "high": 2, "medium": 1}
    seen: dict[tuple[int, int], SecretMatch] = {}

    for sp in SECRET_PATTERNS:
        for m in sp.pattern.finditer(text):
            start, end = m.start(), m.end()
            preview = _make_redacted_preview(text, start, end)
            match = SecretMatch(
                pattern_name=sp.name,
                severity=sp.severity,
                start=start,
                end=end,
                redacted_preview=preview,
            )
            key = (start, end)
            existing = seen.get(key)
            if existing is None or severity_rank.get(sp.severity, 0) > severity_rank.get(existing.severity, 0):
                seen[key] = match

    # Return sorted by position
    return sorted(seen.values(), key=lambda m: m.start)


def redact_text(text: str) -> str:
    """Replace all detected secrets with ``[REDACTED:<type>]`` markers.

    Processes matches from right to left so earlier indices stay valid.
    """
    matches = scan_text(text)
    if not matches:
        return text

    # Work backwards to preserve offsets
    result = list(text)
    for match in sorted(matches, key=lambda m: m.start, reverse=True):
        replacement = f"[REDACTED:{match.pattern_name}]"
        result[match.start:match.end] = list(replacement)

    return "".join(result)


def has_secrets(text: str) -> bool:
    """Quick boolean check -- returns True if any secret pattern matches."""
    return any(sp.pattern.search(text) for sp in SECRET_PATTERNS)


# ---------------------------------------------------------------------------
# File-permission checks
# ---------------------------------------------------------------------------

_WORLD_READ = stat.S_IROTH
_WORLD_WRITE = stat.S_IWOTH
_GROUP_WRITE = stat.S_IWGRP


def check_file_permissions(path: Path) -> list[str]:
    """Check that a secret-bearing file has safe permissions.

    Returns a list of warning strings.  Empty list means OK.
    Checks:
      - File must not be world-readable (o+r)
      - File must not be world-writable (o+w)
      - File must not be group-writable (g+w)
      - File should be owned by current uid or root
    """
    warnings: list[str] = []
    resolved = Path(path).resolve()

    if not resolved.exists():
        warnings.append(f"File does not exist: {resolved}")
        return warnings

    if not resolved.is_file():
        warnings.append(f"Not a regular file: {resolved}")
        return warnings

    try:
        st = resolved.stat()
    except OSError as exc:
        warnings.append(f"Cannot stat {resolved}: {exc}")
        return warnings

    mode = st.st_mode

    if mode & _WORLD_READ:
        warnings.append(
            f"{resolved}: world-readable (mode {oct(stat.S_IMODE(mode))}). "
            f"Secret files should be chmod 600 or 640."
        )
    if mode & _WORLD_WRITE:
        warnings.append(
            f"{resolved}: world-writable (mode {oct(stat.S_IMODE(mode))}). "
            f"This is dangerous for any file."
        )
    if mode & _GROUP_WRITE:
        warnings.append(
            f"{resolved}: group-writable (mode {oct(stat.S_IMODE(mode))}). "
            f"Secret files should not be group-writable."
        )

    # Ownership check
    current_uid = os.getuid()
    if st.st_uid != current_uid and st.st_uid != 0:
        warnings.append(
            f"{resolved}: owned by uid {st.st_uid}, expected {current_uid} or root (0)."
        )

    return warnings


def validate_secret_storage() -> list[str]:
    """Validate permissions on well-known NovaCore secret files.

    Scans:
      - /etc/novacore/*.env
      - ~/.mcp.env / /home/nova/.mcp.env
      - /home/nova/nova-core/.env (if exists)
      - Any .env file in nova-core root

    Returns list of warning strings.  Empty means all OK.
    """
    warnings: list[str] = []

    # 1. /etc/novacore/*.env
    etc_dir = Path("/etc/novacore")
    if etc_dir.is_dir():
        for env_file in sorted(etc_dir.glob("*.env")):
            warnings.extend(check_file_permissions(env_file))
    else:
        # Not an error -- may not exist yet
        pass

    # 2. Home-level .mcp.env
    home = Path.home()
    for name in (".mcp.env",):
        candidate = home / name
        if candidate.exists():
            warnings.extend(check_file_permissions(candidate))

    # 3. Project-level .env files
    project_root = Path("/home/nova/nova-core")
    for env_file in sorted(project_root.glob("*.env")):
        warnings.extend(check_file_permissions(env_file))
    for env_file in sorted(project_root.glob(".env*")):
        if env_file.is_file():
            warnings.extend(check_file_permissions(env_file))

    return warnings


# ---------------------------------------------------------------------------
# High-entropy string detection (optional / opt-in)
# ---------------------------------------------------------------------------

def _shannon_entropy(s: str) -> float:
    """Calculate Shannon entropy of a string."""
    if not s:
        return 0.0
    length = len(s)
    freq: dict[str, int] = {}
    for ch in s:
        freq[ch] = freq.get(ch, 0) + 1
    return -sum(
        (count / length) * math.log2(count / length)
        for count in freq.values()
    )


def find_high_entropy_strings(
    text: str,
    *,
    min_length: int = 20,
    min_entropy: float = 4.5,
) -> list[SecretMatch]:
    """Find high-entropy strings that may be secrets.

    This is an opt-in scanner for strings that don't match known patterns
    but have suspiciously high entropy (randomness).  Useful as a
    second-pass check.

    Args:
        text: Input text to scan.
        min_length: Minimum token length to consider.
        min_entropy: Minimum Shannon entropy threshold (bits per char).
                     Typical thresholds: 4.0-4.5 for hex, 4.5-5.0 for base64.

    Returns:
        List of SecretMatch for high-entropy tokens found.
    """
    matches: list[SecretMatch] = []
    # Match contiguous non-whitespace tokens that look like credentials
    token_re = re.compile(r"[A-Za-z0-9+/=_-]{" + str(min_length) + r",}")

    for m in token_re.finditer(text):
        token = m.group()
        entropy = _shannon_entropy(token)
        if entropy >= min_entropy:
            preview = _make_redacted_preview(text, m.start(), m.end())
            matches.append(SecretMatch(
                pattern_name="high_entropy_string",
                severity="medium",
                start=m.start(),
                end=m.end(),
                redacted_preview=preview,
            ))

    return matches
