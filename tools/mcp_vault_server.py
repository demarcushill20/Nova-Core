#!/usr/bin/env python3
"""Nova-Core Obsidian Vault — MCP Server (Phase 2: Bounded Write).

Phase 1 tools (read-only):
    vault_list, vault_read, vault_search, vault_frontmatter

Phase 1.5 tools (read-only, vault identity):
    vault_info

Phase 2 tools (bounded write, feature-flagged):
    vault_write, vault_update, vault_validate

Usage:
    python3 tools/mcp_vault_server.py

Environment:
    NOVA_VAULT_PATH       — canonical synced vault directory
                            (default: /home/nova/nova-vault)
    NOVA_VAULT_WRITE_FLAG — path to write-enable config JSON
                            (default: <vault>/.nova-write-config.json)

Vault model:
    The configured vault path points to the canonical Obsidian vault
    synced via Obsidian Sync (paid). The same vault is viewed and edited
    by the human operator on phone/desktop. Nova-Core coexists with
    human editing — see _meta/human-coexistence.md in the vault.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

import yaml
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
logger = logging.getLogger("nova-vault")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

VAULT_ROOT = Path(
    os.environ.get("NOVA_VAULT_PATH", "/home/nova/nova-vault")
).resolve()

# Vault identity config file (read-only metadata about the vault)
_VAULT_CONFIG_PATH = VAULT_ROOT / ".nova-vault-config.json"

# Folders that are part of the vault structure (excludes .obsidian)
VAULT_FOLDERS = frozenset(
    {
        "00-inbox",
        "10-adrs",
        "20-agent-patterns",
        "30-workflow-learnings",
        "40-research",
        "50-playbooks",
        "60-project",
        "70-debugging",
        "80-references",
        "90-diary",
        "_meta",
    }
)

# Max file size we'll read (34 KB, per schema constraints)
MAX_READ_SIZE = 34 * 1024

# Max search results returned
MAX_SEARCH_RESULTS = 20

# Frontmatter delimiter
_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)

# ---------------------------------------------------------------------------
# Phase 2: Write configuration
# ---------------------------------------------------------------------------

# Folders where writes are allowed (per Phase O rollout plan)
WRITABLE_FOLDERS = frozenset(
    {
        "00-inbox",
        "20-agent-patterns",
        "30-workflow-learnings",
        "40-research",
        "70-debugging",
    }
)

# Max note size for writes (34 KB = 34816 bytes)
MAX_WRITE_SIZE = 34 * 1024

# Rate limit: max writes within a sliding window
RATE_LIMIT_WINDOW_SECONDS = 300  # 5 minutes
RATE_LIMIT_MAX_WRITES = 10       # max 10 writes per 5 min window

# Sensitive content patterns — fail-closed on match
_SECRET_PATTERNS: list[re.Pattern] = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"(?:api[_\-]?key|apikey)\s*[:=]\s*\S{8,}",
        r"(?:password|passwd)\s*[:=]\s*\S{4,}",
        r"(?:secret|token)\s*[:=]\s*\S{8,}",
        r"(?:credential)\s*[:=]\s*\S{4,}",
        r"(?:aws_access_key_id|aws_secret_access_key)\s*[:=]",
        r"-----BEGIN (?:RSA |EC |DSA )?PRIVATE KEY-----",
        r"ghp_[A-Za-z0-9_]{36,}",                        # GitHub PAT
        r"sk-[A-Za-z0-9]{32,}",                           # OpenAI-style key
        r"tvly-[A-Za-z0-9\-]{20,}",                       # Tavily key
        r"BSA[a-zA-Z0-9]{20,}",                            # Brave key
    ]
]

# Valid note types and their required tags
_VALID_NOTE_TYPES = {
    "agent-pattern":      "#type/pattern",
    "workflow-learning":  "#type/learning",
    "research-summary":   "#type/research",
    "debugging-guide":    "#type/debugging",
    "inbox":              "#type/inbox",
}

# Valid enum values used across schemas
_VALID_CONFIDENCES = {"high", "medium", "low"}
_VALID_SOURCES = {"operator", "nova-core-memory"}
_VALID_AGENT_ROLES = {"research", "coder", "critic", "verifier", "planner", "memory"}
_VALID_TASK_CLASSES = {"research", "code_impl", "code_review", "system", "simple", "unknown"}
_VALID_VERIFICATION_OUTCOMES = {"approved", "rejected", "partial", "not_verified"}

# Per-type required frontmatter fields
_REQUIRED_FIELDS: dict[str, list[str]] = {
    "agent-pattern": [
        "type", "pattern_id", "title", "agent_role", "confidence",
        "task_classes", "date_created", "source", "tags",
    ],
    "workflow-learning": [
        "type", "learning_id", "title", "workflow_id", "task_class",
        "verification_outcome", "confidence", "roles_involved",
        "date", "source", "tags",
    ],
    "research-summary": [
        "type", "research_id", "title", "topic", "date_researched",
        "sources_count", "confidence", "source", "tags",
    ],
    "debugging-guide": [
        "type", "title", "date_created", "source", "tags",
    ],
    "inbox": [
        "type", "title", "source", "tags",
    ],
}

# Rate-limit tracking: list of timestamps of recent writes
_write_timestamps: list[float] = []


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------


def _safe_resolve(relative_path: str) -> Path | None:
    """Resolve a relative vault path safely.

    Returns the resolved Path if it is within VAULT_ROOT,
    or None if the path escapes the vault or is invalid.
    """
    # Reject absolute paths
    if os.path.isabs(relative_path):
        return None

    # Reject paths with null bytes
    if "\0" in relative_path:
        return None

    resolved = (VAULT_ROOT / relative_path).resolve()

    # Must be within vault root
    try:
        resolved.relative_to(VAULT_ROOT)
    except ValueError:
        return None

    # Must not enter .obsidian
    rel = resolved.relative_to(VAULT_ROOT)
    parts = rel.parts
    if parts and parts[0] == ".obsidian":
        return None

    return resolved


def _is_markdown(path: Path) -> bool:
    """Check if a path points to a markdown file."""
    return path.is_file() and path.suffix.lower() == ".md"


# ---------------------------------------------------------------------------
# Vault identity / config
# ---------------------------------------------------------------------------


def _load_vault_config() -> dict[str, Any]:
    """Load the vault identity config (.nova-vault-config.json).

    Returns the parsed config dict, or a minimal default if the file is
    missing or unreadable.  This is informational — never blocks operation.
    """
    try:
        if _VAULT_CONFIG_PATH.is_file():
            raw = _VAULT_CONFIG_PATH.read_text(encoding="utf-8")
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Cannot load vault config (%s) — using defaults", exc)
    return {"vault_id": "unknown", "sync_model": "unknown", "canonical": False}


def validate_vault_path() -> tuple[bool, list[str]]:
    """Validate that VAULT_ROOT points to a usable Obsidian vault.

    Returns (valid, issues).  Issues are informational warnings, not fatal.
    """
    issues: list[str] = []

    if not VAULT_ROOT.is_dir():
        issues.append(f"vault root does not exist: {VAULT_ROOT}")
        return False, issues

    # Check for .obsidian marker (indicates Obsidian recognises this as a vault)
    if not (VAULT_ROOT / ".obsidian").is_dir():
        issues.append("missing .obsidian directory — Obsidian may not recognise this vault")

    # Check for vault config
    if not _VAULT_CONFIG_PATH.is_file():
        issues.append("missing .nova-vault-config.json — vault identity not configured")

    # Check at least one expected folder exists
    found_folders = [f for f in VAULT_FOLDERS if (VAULT_ROOT / f).is_dir()]
    if not found_folders:
        issues.append("no expected vault folders found")

    return len(issues) == 0, issues


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------


def _parse_frontmatter(content: str) -> dict | None:
    """Parse YAML frontmatter from markdown content.

    Returns parsed dict or None if no valid frontmatter found.
    """
    match = _FM_RE.match(content)
    if not match:
        return None
    try:
        parsed = yaml.safe_load(match.group(1))
        if isinstance(parsed, dict):
            return parsed
        return None
    except yaml.YAMLError:
        return None


# ---------------------------------------------------------------------------
# Phase 2: Write feature flag
# ---------------------------------------------------------------------------

# Default config when no file exists (writes disabled)
_DEFAULT_WRITE_CONFIG: dict[str, Any] = {
    "enabled": False,
    "allowed_folders": list(WRITABLE_FOLDERS),
    "max_writes_per_window": RATE_LIMIT_MAX_WRITES,
    "window_seconds": RATE_LIMIT_WINDOW_SECONDS,
    "max_note_size_bytes": MAX_WRITE_SIZE,
    "require_frontmatter_validation": True,
}


def _load_write_config() -> dict[str, Any]:
    """Load the write feature flag config. Fail-closed on any error."""
    config_path = Path(
        os.environ.get(
            "NOVA_VAULT_WRITE_FLAG",
            str(VAULT_ROOT / ".nova-write-config.json"),
        )
    )
    try:
        if not config_path.is_file():
            return dict(_DEFAULT_WRITE_CONFIG)
        raw = config_path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            logger.warning("Write config is not a dict — writes disabled")
            return dict(_DEFAULT_WRITE_CONFIG)
        # Merge with defaults so missing keys fail closed
        merged = dict(_DEFAULT_WRITE_CONFIG)
        merged.update(data)
        # Ensure enabled is strictly boolean
        if not isinstance(merged.get("enabled"), bool):
            merged["enabled"] = False
        return merged
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to load write config (%s) — writes disabled", e)
        return dict(_DEFAULT_WRITE_CONFIG)


def _is_write_enabled() -> tuple[bool, dict[str, Any]]:
    """Check if writes are enabled. Returns (enabled, config)."""
    config = _load_write_config()
    return config.get("enabled", False) is True, config


# ---------------------------------------------------------------------------
# Phase 2: Schema validation
# ---------------------------------------------------------------------------


def validate_frontmatter(fm: dict, note_type: str | None = None) -> tuple[bool, list[str]]:
    """Validate frontmatter against the canonical schemas.

    Returns (valid, errors). Fail-closed: any issue → rejected.
    """
    errors: list[str] = []

    # 1. type field
    actual_type = fm.get("type")
    if not actual_type:
        errors.append("missing required field: type")
        return False, errors

    if actual_type not in _VALID_NOTE_TYPES:
        errors.append(
            f"invalid type: {actual_type!r} "
            f"(allowed: {sorted(_VALID_NOTE_TYPES)})"
        )
        return False, errors

    if note_type and actual_type != note_type:
        errors.append(f"type mismatch: expected {note_type!r}, got {actual_type!r}")

    # 2. Required fields for this type
    required = _REQUIRED_FIELDS.get(actual_type, [])
    for field in required:
        if field not in fm:
            errors.append(f"missing required field: {field}")
        elif fm[field] is None:
            errors.append(f"null required field: {field}")
        elif isinstance(fm[field], str) and not fm[field].strip():
            errors.append(f"empty required field: {field}")

    if errors:
        return False, errors

    # 3. Title length
    title = fm.get("title", "")
    if isinstance(title, str) and len(title) > 100:
        errors.append(f"title too long: {len(title)} chars (max 100)")

    # 4. tags must be a non-empty list and include required type tag
    tags = fm.get("tags")
    if not isinstance(tags, list) or len(tags) == 0:
        errors.append("tags must be a non-empty list")
    else:
        required_tag = _VALID_NOTE_TYPES.get(actual_type, "")
        if required_tag and required_tag not in tags:
            errors.append(f"tags must include {required_tag!r}")
        if len(tags) > 10:
            errors.append(f"too many tags: {len(tags)} (max 10)")

    # 5. source field
    source = fm.get("source")
    if source and source not in _VALID_SOURCES:
        errors.append(f"invalid source: {source!r} (allowed: {sorted(_VALID_SOURCES)})")

    # 6. confidence field (if present for types that use it)
    confidence = fm.get("confidence")
    if confidence is not None and confidence not in _VALID_CONFIDENCES:
        errors.append(
            f"invalid confidence: {confidence!r} (allowed: {sorted(_VALID_CONFIDENCES)})"
        )

    # 7. Type-specific validations
    if actual_type == "agent-pattern":
        role = fm.get("agent_role")
        if role and role not in _VALID_AGENT_ROLES:
            errors.append(f"invalid agent_role: {role!r}")
        tc = fm.get("task_classes")
        if isinstance(tc, list):
            for c in tc:
                if c not in _VALID_TASK_CLASSES:
                    errors.append(f"invalid task_class in task_classes: {c!r}")
        elif tc is not None:
            errors.append("task_classes must be a list")

    elif actual_type == "workflow-learning":
        tc = fm.get("task_class")
        if tc and tc not in _VALID_TASK_CLASSES:
            errors.append(f"invalid task_class: {tc!r}")
        vo = fm.get("verification_outcome")
        if vo and vo not in _VALID_VERIFICATION_OUTCOMES:
            errors.append(f"invalid verification_outcome: {vo!r}")
        ri = fm.get("roles_involved")
        if isinstance(ri, list):
            if len(ri) == 0:
                errors.append("roles_involved must be non-empty")
        elif ri is not None:
            errors.append("roles_involved must be a list")

    elif actual_type == "research-summary":
        sc = fm.get("sources_count")
        if sc is not None and not isinstance(sc, int):
            errors.append(f"sources_count must be an integer, got {type(sc).__name__}")

    return (len(errors) == 0), errors


# ---------------------------------------------------------------------------
# Phase 2: Sensitive content detection
# ---------------------------------------------------------------------------


def detect_sensitive_content(content: str) -> tuple[bool, list[str]]:
    """Scan content for sensitive patterns. Returns (has_sensitive, matches).

    Fail-closed: any match → write rejected.
    """
    matches = []
    for pattern in _SECRET_PATTERNS:
        m = pattern.search(content)
        if m:
            # Include pattern description but NOT the matched value
            matches.append(f"sensitive_pattern:{pattern.pattern[:40]}...")
    return bool(matches), matches


# ---------------------------------------------------------------------------
# Phase 2: Rate limiting
# ---------------------------------------------------------------------------


def _check_rate_limit(config: dict) -> tuple[bool, str]:
    """Check if a write is within rate limits. Returns (allowed, reason)."""
    now = time.time()
    window = config.get("window_seconds", RATE_LIMIT_WINDOW_SECONDS)
    max_writes = config.get("max_writes_per_window", RATE_LIMIT_MAX_WRITES)

    # Prune old entries
    cutoff = now - window
    _write_timestamps[:] = [t for t in _write_timestamps if t > cutoff]

    if len(_write_timestamps) >= max_writes:
        return False, (
            f"rate_limit_exceeded: {len(_write_timestamps)} writes "
            f"in last {window}s (max {max_writes})"
        )
    return True, "ok"


def _record_write() -> None:
    """Record a successful write for rate limiting."""
    _write_timestamps.append(time.time())


# ---------------------------------------------------------------------------
# Phase 2: Audit logging
# ---------------------------------------------------------------------------

# Audit log path (within vault, not in nova-core LOGS/ — vault is separate)
_AUDIT_LOG_PATH = VAULT_ROOT / ".nova-audit.log"


def _audit_log(action: str, path: str, status: str, detail: str = "") -> None:
    """Append an entry to the vault audit log."""
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    entry = f"{ts} {action} path={path} status={status}"
    if detail:
        entry += f" detail={detail}"
    try:
        with open(_AUDIT_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(entry + "\n")
    except OSError:
        logger.warning("Failed to write audit log entry: %s", entry)


# ---------------------------------------------------------------------------
# Phase 2: Write helpers
# ---------------------------------------------------------------------------


def _get_write_folder(relative_path: str) -> str | None:
    """Extract the top-level folder from a relative vault path."""
    parts = Path(relative_path).parts
    if not parts:
        return None
    return parts[0]


def _is_writable_folder(folder: str, config: dict) -> bool:
    """Check if a folder is in the allowed write list."""
    allowed = set(config.get("allowed_folders", WRITABLE_FOLDERS))
    return folder in allowed


def _assemble_note(frontmatter: dict, body: str) -> str:
    """Assemble a complete markdown note from frontmatter dict and body."""
    fm_yaml = yaml.dump(
        frontmatter, default_flow_style=False, allow_unicode=True, sort_keys=False,
    )
    return f"---\n{fm_yaml}---\n\n{body}"


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

server = FastMCP(
    name="nova-vault",
    instructions=(
        "Access to the Nova-Core Obsidian vault (synced via Obsidian Sync). "
        "This vault is shared with a human operator — notes may be human-authored. "
        "Read tools: vault_list, vault_read, vault_search, vault_frontmatter, vault_info. "
        "Write tools (feature-flagged): vault_write, vault_update, vault_validate. "
        "Writes are restricted to approved folders with schema validation. "
        "Nova-Core never overwrites human-authored notes."
    ),
)


# ---- Phase 1: Read-only tools (unchanged) ----


@server.tool(
    name="vault_list",
    description=(
        "List files and folders within the Nova-Core Obsidian vault. "
        "Pass a relative folder path (e.g. '10-adrs') or leave empty "
        "to list top-level vault folders. Returns markdown filenames "
        "and subfolder names."
    ),
)
def vault_list(folder: str = "") -> dict:
    """List contents of a vault folder."""
    if folder:
        resolved = _safe_resolve(folder)
        if resolved is None:
            return {"error": f"Invalid or inaccessible path: {folder}"}
        if not resolved.is_dir():
            return {"error": f"Not a directory: {folder}"}
    else:
        resolved = VAULT_ROOT

    folders = []
    files = []

    try:
        for entry in sorted(resolved.iterdir()):
            # Skip hidden dirs like .obsidian
            if entry.name.startswith("."):
                continue
            rel = str(entry.relative_to(VAULT_ROOT))
            if entry.is_dir():
                folders.append(rel)
            elif _is_markdown(entry):
                files.append(rel)
    except PermissionError:
        return {"error": f"Permission denied: {folder}"}

    return {"folder": folder or "/", "folders": folders, "files": files}


@server.tool(
    name="vault_read",
    description=(
        "Read a markdown note from the Nova-Core Obsidian vault. "
        "Pass the relative path within the vault "
        "(e.g. '10-adrs/ADR-001-multi-agent-architecture.md'). "
        "Returns the full note content."
    ),
)
def vault_read(path: str) -> dict:
    """Read a vault note by relative path."""
    if not path:
        return {"error": "path is required"}

    resolved = _safe_resolve(path)
    if resolved is None:
        return {"error": f"Invalid or inaccessible path: {path}"}

    if not resolved.is_file():
        return {"error": f"File not found: {path}"}

    if not _is_markdown(resolved):
        return {"error": f"Not a markdown file: {path}"}

    try:
        size = resolved.stat().st_size
        if size > MAX_READ_SIZE:
            return {
                "error": f"File too large: {size} bytes (max {MAX_READ_SIZE})"
            }
        content = resolved.read_text(encoding="utf-8")
    except (PermissionError, OSError) as e:
        return {"error": f"Cannot read file: {e}"}

    return {"path": path, "size": len(content), "content": content}


@server.tool(
    name="vault_search",
    description=(
        "Search for notes in the Nova-Core Obsidian vault by keyword. "
        "Searches both filenames and note contents (including frontmatter). "
        "Returns matching note paths with a snippet of the matching context. "
        "Case-insensitive. Returns up to 20 results."
    ),
)
def vault_search(query: str, folder: str = "") -> dict:
    """Search vault notes by keyword."""
    if not query or not query.strip():
        return {"error": "query is required"}

    query_lower = query.strip().lower()

    # Determine search root
    if folder:
        search_root = _safe_resolve(folder)
        if search_root is None or not search_root.is_dir():
            return {"error": f"Invalid search folder: {folder}"}
    else:
        search_root = VAULT_ROOT

    results = []

    for md_file in sorted(search_root.rglob("*.md")):
        # Skip hidden directories
        rel = md_file.relative_to(VAULT_ROOT)
        if any(part.startswith(".") for part in rel.parts):
            continue

        # Skip oversized files
        try:
            if md_file.stat().st_size > MAX_READ_SIZE:
                continue
        except OSError:
            continue

        rel_str = str(rel)

        # Check filename match
        name_match = query_lower in md_file.stem.lower()

        # Check content match
        content_match = False
        snippet = ""
        try:
            content = md_file.read_text(encoding="utf-8")
            idx = content.lower().find(query_lower)
            if idx >= 0:
                content_match = True
                # Extract snippet: up to 50 chars before and 100 after
                start = max(0, idx - 50)
                end = min(len(content), idx + len(query) + 100)
                snippet = content[start:end].replace("\n", " ").strip()
                if start > 0:
                    snippet = "..." + snippet
                if end < len(content):
                    snippet = snippet + "..."
        except (OSError, UnicodeDecodeError):
            continue

        if name_match or content_match:
            results.append(
                {
                    "path": rel_str,
                    "name_match": name_match,
                    "content_match": content_match,
                    "snippet": snippet,
                }
            )

        if len(results) >= MAX_SEARCH_RESULTS:
            break

    return {
        "query": query,
        "folder": folder or "/",
        "results_count": len(results),
        "results": results,
    }


@server.tool(
    name="vault_frontmatter",
    description=(
        "Extract and return the YAML frontmatter from a vault note. "
        "Pass the relative path to a markdown file. Returns the parsed "
        "frontmatter fields as structured data, or an error if the note "
        "has no valid frontmatter."
    ),
)
def vault_frontmatter(path: str) -> dict:
    """Extract frontmatter from a vault note."""
    if not path:
        return {"error": "path is required"}

    resolved = _safe_resolve(path)
    if resolved is None:
        return {"error": f"Invalid or inaccessible path: {path}"}

    if not resolved.is_file():
        return {"error": f"File not found: {path}"}

    if not _is_markdown(resolved):
        return {"error": f"Not a markdown file: {path}"}

    try:
        content = resolved.read_text(encoding="utf-8")
    except (PermissionError, OSError) as e:
        return {"error": f"Cannot read file: {e}"}

    fm = _parse_frontmatter(content)
    if fm is None:
        return {"path": path, "has_frontmatter": False, "frontmatter": {}}

    return {"path": path, "has_frontmatter": True, "frontmatter": fm}


# ---- Phase 1.5: Vault identity / info ----


@server.tool(
    name="vault_info",
    description=(
        "Return metadata about the Nova-Core Obsidian vault: "
        "vault path, sync model, folder structure, vault health, "
        "and human-coexistence configuration.  Read-only."
    ),
)
def vault_info() -> dict:
    """Return vault identity and health information."""
    config = _load_vault_config()
    valid, issues = validate_vault_path()

    # Count notes per folder
    folder_stats: dict[str, int] = {}
    for folder_name in sorted(VAULT_FOLDERS):
        folder_path = VAULT_ROOT / folder_name
        if folder_path.is_dir():
            count = sum(1 for f in folder_path.iterdir() if _is_markdown(f))
            folder_stats[folder_name] = count

    return {
        "vault_root": str(VAULT_ROOT),
        "vault_id": config.get("vault_id", "unknown"),
        "vault_name": config.get("vault_name", ""),
        "sync_model": config.get("sync_model", "unknown"),
        "canonical": config.get("canonical", False),
        "human_editable": config.get("human_editable", True),
        "valid": valid,
        "issues": issues,
        "folder_note_counts": folder_stats,
        "nova_core_managed_folders": config.get(
            "nova_core_managed_folders", list(WRITABLE_FOLDERS)
        ),
        "human_managed_folders": config.get("human_managed_folders", []),
    }


# ---- Phase 2: Bounded write tools ----


@server.tool(
    name="vault_validate",
    description=(
        "Validate a proposed note's frontmatter against Nova-Core's "
        "Obsidian note schemas WITHOUT writing anything. "
        "Pass the frontmatter as a dict and optionally the body text. "
        "Returns validation result with any errors found. "
        "Use this to check a note before calling vault_write."
    ),
)
def vault_validate(frontmatter: dict, body: str = "") -> dict:
    """Validate frontmatter + body without writing."""
    if not isinstance(frontmatter, dict) or not frontmatter:
        return {"valid": False, "errors": ["frontmatter must be a non-empty dict"]}

    errors_all: list[str] = []

    # Schema validation
    valid, schema_errors = validate_frontmatter(frontmatter)
    errors_all.extend(schema_errors)

    # Size check
    assembled = _assemble_note(frontmatter, body)
    if len(assembled.encode("utf-8")) > MAX_WRITE_SIZE:
        errors_all.append(
            f"note too large: {len(assembled.encode('utf-8'))} bytes "
            f"(max {MAX_WRITE_SIZE})"
        )

    # Sensitive content check on full assembled note
    has_sensitive, sensitive_matches = detect_sensitive_content(assembled)
    if has_sensitive:
        errors_all.extend(sensitive_matches)

    return {
        "valid": len(errors_all) == 0,
        "errors": errors_all,
        "note_size_bytes": len(assembled.encode("utf-8")),
    }


@server.tool(
    name="vault_write",
    description=(
        "Create a new markdown note in the Nova-Core Obsidian vault. "
        "Writes are bounded: restricted to approved folders, validated "
        "against note schemas, checked for sensitive content, and "
        "rate-limited. Requires the obsidian_write feature flag to be "
        "enabled. Pass 'path' (relative, e.g. '20-agent-patterns/new-pattern.md'), "
        "'frontmatter' (dict), and 'body' (markdown string)."
    ),
)
def vault_write(path: str, frontmatter: dict, body: str) -> dict:
    """Create a new note in an approved vault folder."""
    # 1. Feature flag check
    enabled, config = _is_write_enabled()
    if not enabled:
        _audit_log("WRITE", path, "rejected", "feature_flag_disabled")
        return {"error": "vault writes are disabled (feature flag off)"}

    # 2. Path validation
    if not path or not path.strip():
        _audit_log("WRITE", path or "(empty)", "rejected", "empty_path")
        return {"error": "path is required"}

    if not path.lower().endswith(".md"):
        _audit_log("WRITE", path, "rejected", "not_markdown")
        return {"error": "path must end with .md"}

    resolved = _safe_resolve(path)
    if resolved is None:
        _audit_log("WRITE", path, "rejected", "path_safety_failed")
        return {"error": f"Invalid or unsafe path: {path}"}

    # 3. Folder restriction
    folder = _get_write_folder(path)
    if folder is None:
        _audit_log("WRITE", path, "rejected", "no_folder")
        return {"error": "path must be within a vault folder (e.g. '20-agent-patterns/note.md')"}

    if not _is_writable_folder(folder, config):
        _audit_log("WRITE", path, "rejected", f"folder_not_writable:{folder}")
        return {
            "error": (
                f"writes not allowed to folder: {folder!r} "
                f"(allowed: {sorted(config.get('allowed_folders', []))})"
            )
        }

    # 4. Must not overwrite existing
    if resolved.exists():
        _audit_log("WRITE", path, "rejected", "file_exists")
        return {"error": f"file already exists: {path} (use vault_update to modify)"}

    # 5. Frontmatter validation
    if not isinstance(frontmatter, dict) or not frontmatter:
        _audit_log("WRITE", path, "rejected", "missing_frontmatter")
        return {"error": "frontmatter is required and must be a non-empty dict"}

    valid, schema_errors = validate_frontmatter(frontmatter)
    if not valid:
        _audit_log("WRITE", path, "rejected", f"schema:{';'.join(schema_errors[:3])}")
        return {"error": "frontmatter validation failed", "schema_errors": schema_errors}

    # 6. Assemble and check size
    assembled = _assemble_note(frontmatter, body)
    note_bytes = len(assembled.encode("utf-8"))
    max_size = config.get("max_note_size_bytes", MAX_WRITE_SIZE)
    if note_bytes > max_size:
        _audit_log("WRITE", path, "rejected", f"oversized:{note_bytes}")
        return {"error": f"note too large: {note_bytes} bytes (max {max_size})"}

    # 7. Sensitive content check
    has_sensitive, sensitive_matches = detect_sensitive_content(assembled)
    if has_sensitive:
        _audit_log("WRITE", path, "rejected", "sensitive_content")
        return {
            "error": "note contains sensitive content",
            "sensitive_matches": sensitive_matches,
        }

    # 8. Rate limit check
    allowed, reason = _check_rate_limit(config)
    if not allowed:
        _audit_log("WRITE", path, "rejected", reason)
        return {"error": reason}

    # 9. Write the file
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        tmp = resolved.with_suffix(".tmp")
        tmp.write_text(assembled, encoding="utf-8")
        tmp.rename(resolved)
    except OSError as e:
        _audit_log("WRITE", path, "error", str(e))
        return {"error": f"write failed: {e}"}

    _record_write()
    _audit_log("WRITE", path, "accepted", f"size={note_bytes}")
    return {
        "path": path,
        "status": "created",
        "size": note_bytes,
    }


@server.tool(
    name="vault_update",
    description=(
        "Append a new section to an existing note in the Nova-Core "
        "Obsidian vault. Only appends to existing notes in approved "
        "writable folders. Pass 'path' (relative), 'section_heading' "
        "(e.g. '## New Evidence'), and 'section_body' (markdown text). "
        "The section is appended to the end of the note."
    ),
)
def vault_update(path: str, section_heading: str, section_body: str) -> dict:
    """Append a section to an existing note in an approved folder."""
    # 1. Feature flag check
    enabled, config = _is_write_enabled()
    if not enabled:
        _audit_log("UPDATE", path, "rejected", "feature_flag_disabled")
        return {"error": "vault writes are disabled (feature flag off)"}

    # 2. Path validation
    if not path or not path.strip():
        _audit_log("UPDATE", path or "(empty)", "rejected", "empty_path")
        return {"error": "path is required"}

    resolved = _safe_resolve(path)
    if resolved is None:
        _audit_log("UPDATE", path, "rejected", "path_safety_failed")
        return {"error": f"Invalid or unsafe path: {path}"}

    if not resolved.is_file():
        _audit_log("UPDATE", path, "rejected", "file_not_found")
        return {"error": f"file not found: {path}"}

    if not _is_markdown(resolved):
        _audit_log("UPDATE", path, "rejected", "not_markdown")
        return {"error": f"not a markdown file: {path}"}

    # 3. Folder restriction
    folder = _get_write_folder(path)
    if folder is None or not _is_writable_folder(folder, config):
        _audit_log("UPDATE", path, "rejected", f"folder_not_writable:{folder}")
        return {
            "error": (
                f"updates not allowed to folder: {folder!r} "
                f"(allowed: {sorted(config.get('allowed_folders', []))})"
            )
        }

    # 4. Validate inputs
    if not section_heading or not section_heading.strip():
        _audit_log("UPDATE", path, "rejected", "empty_heading")
        return {"error": "section_heading is required"}

    if not section_heading.strip().startswith("#"):
        _audit_log("UPDATE", path, "rejected", "heading_format")
        return {"error": "section_heading must start with '#' (markdown heading)"}

    if not section_body or not section_body.strip():
        _audit_log("UPDATE", path, "rejected", "empty_body")
        return {"error": "section_body is required"}

    # 5. Sensitive content check on new section
    new_content = f"{section_heading}\n\n{section_body}\n"
    has_sensitive, sensitive_matches = detect_sensitive_content(new_content)
    if has_sensitive:
        _audit_log("UPDATE", path, "rejected", "sensitive_content")
        return {
            "error": "section contains sensitive content",
            "sensitive_matches": sensitive_matches,
        }

    # 6. Size check after append
    try:
        existing = resolved.read_text(encoding="utf-8")
    except OSError as e:
        _audit_log("UPDATE", path, "error", f"read_failed:{e}")
        return {"error": f"cannot read existing file: {e}"}

    updated = existing.rstrip() + "\n\n" + new_content
    updated_bytes = len(updated.encode("utf-8"))
    max_size = config.get("max_note_size_bytes", MAX_WRITE_SIZE)
    if updated_bytes > max_size:
        _audit_log("UPDATE", path, "rejected", f"oversized_after_append:{updated_bytes}")
        return {
            "error": (
                f"note would be too large after update: {updated_bytes} bytes "
                f"(max {max_size})"
            )
        }

    # 7. Rate limit check
    allowed, reason = _check_rate_limit(config)
    if not allowed:
        _audit_log("UPDATE", path, "rejected", reason)
        return {"error": reason}

    # 8. Write the updated file
    try:
        tmp = resolved.with_suffix(".tmp")
        tmp.write_text(updated, encoding="utf-8")
        tmp.rename(resolved)
    except OSError as e:
        _audit_log("UPDATE", path, "error", str(e))
        return {"error": f"update failed: {e}"}

    _record_write()
    _audit_log("UPDATE", path, "accepted", f"size={updated_bytes}")
    return {
        "path": path,
        "status": "updated",
        "size": updated_bytes,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    server.run(transport="stdio")
