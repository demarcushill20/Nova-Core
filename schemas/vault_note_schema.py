"""Canonical vault note schema definitions and standalone validator.

Phase 0 deliverable — extracted from tools/mcp_vault_server.py and extended
with implementation-plan optional field validation.

This module can be imported by both the MCP vault server and repair scripts
to ensure a single source of truth for schema rules.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Canonical enums
# ---------------------------------------------------------------------------

VALID_NOTE_TYPES: dict[str, str] = {
    "agent-pattern": "#type/pattern",
    "workflow-learning": "#type/learning",
    "research-summary": "#type/research",
    "implementation-plan": "#type/plan",
    "debugging-guide": "#type/debugging",
    "inbox": "#type/inbox",
    "adr": "#type/adr",
    "moc": "#type/moc",
}

VALID_CONFIDENCES = frozenset({"high", "medium", "low"})
VALID_SOURCES = frozenset({"operator", "nova-core-memory"})
VALID_AGENT_ROLES = frozenset({"research", "coder", "critic", "verifier", "planner", "memory"})
VALID_TASK_CLASSES = frozenset({"research", "code_impl", "code_review", "system", "simple", "unknown"})
VALID_VERIFICATION_OUTCOMES = frozenset({"approved", "rejected", "partial", "not_verified"})
VALID_PLAN_STATUSES = frozenset({"backlog", "active", "completed", "paused"})
VALID_PLAN_PRIORITIES = frozenset({"high", "medium", "low"})
VALID_ADR_STATUSES = frozenset({"proposed", "accepted", "deprecated", "superseded"})
VALID_MOC_DOMAINS = frozenset(
    {
        "novatrade",
        "infrastructure",
        "memory",
        "autonomy",
        "research",
        "debugging",
        "agents",
        "risk",
        "trading-strategies",
        "operations",
    }
)
PROGRESS_RE = re.compile(r"^\d+/\d+$")

# Per-type required frontmatter fields
REQUIRED_FIELDS: dict[str, list[str]] = {
    "agent-pattern": [
        "type",
        "pattern_id",
        "title",
        "agent_role",
        "confidence",
        "task_classes",
        "date_created",
        "source",
        "tags",
    ],
    "workflow-learning": [
        "type",
        "learning_id",
        "title",
        "workflow_id",
        "task_class",
        "verification_outcome",
        "confidence",
        "roles_involved",
        "date",
        "source",
        "tags",
    ],
    "research-summary": [
        "type",
        "research_id",
        "title",
        "topic",
        "date_researched",
        "sources_count",
        "confidence",
        "source",
        "tags",
    ],
    "implementation-plan": [
        "type",
        "plan_id",
        "title",
        "date_created",
        "confidence",
        "source",
        "tags",
    ],
    "debugging-guide": [
        "type",
        "title",
        "date_created",
        "source",
        "tags",
    ],
    "inbox": [
        "type",
        "title",
        "source",
        "tags",
    ],
    "adr": [
        "type",
        "adr_id",
        "title",
        "status",
        "date",
        "source",
        "tags",
    ],
    "moc": [
        "type",
        "moc_id",
        "title",
        "domain",
        "date_created",
        "source",
        "tags",
    ],
}

MAX_TITLE_LENGTH = 100
MAX_TAGS = 10


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------


def validate_frontmatter(fm: dict, note_type: str | None = None) -> tuple[bool, list[str]]:
    """Validate frontmatter against the canonical vault note schemas.

    Returns (valid, errors). Fail-closed: any issue -> rejected.

    This function is a standalone reimplementation that mirrors the logic in
    tools/mcp_vault_server.py but adds implementation-plan optional field
    validation (status, priority, progress).
    """
    errors: list[str] = []

    # 1. type field
    actual_type = fm.get("type")
    if not actual_type:
        errors.append("missing required field: type")
        return False, errors

    if actual_type not in VALID_NOTE_TYPES:
        errors.append(f"invalid type: {actual_type!r} (allowed: {sorted(VALID_NOTE_TYPES)})")
        return False, errors

    if note_type and actual_type != note_type:
        errors.append(f"type mismatch: expected {note_type!r}, got {actual_type!r}")

    # 2. Required fields for this type
    required = REQUIRED_FIELDS.get(actual_type, [])
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
    if isinstance(title, str) and len(title) > MAX_TITLE_LENGTH:
        errors.append(f"title too long: {len(title)} chars (max {MAX_TITLE_LENGTH})")

    # 4. tags must be a non-empty list and include required type tag
    tags = fm.get("tags")
    if not isinstance(tags, list) or len(tags) == 0:
        errors.append("tags must be a non-empty list")
    else:
        required_tag = VALID_NOTE_TYPES.get(actual_type, "")
        if required_tag and required_tag not in tags:
            errors.append(f"tags must include {required_tag!r}")
        if len(tags) > MAX_TAGS:
            errors.append(f"too many tags: {len(tags)} (max {MAX_TAGS})")

    # 5. source field
    source = fm.get("source")
    if source and source not in VALID_SOURCES:
        errors.append(f"invalid source: {source!r} (allowed: {sorted(VALID_SOURCES)})")

    # 6. confidence field (if present for types that use it)
    confidence = fm.get("confidence")
    if confidence is not None and confidence not in VALID_CONFIDENCES:
        errors.append(f"invalid confidence: {confidence!r} (allowed: {sorted(VALID_CONFIDENCES)})")

    # 7. Type-specific validations
    if actual_type == "agent-pattern":
        role = fm.get("agent_role")
        if role and role not in VALID_AGENT_ROLES:
            errors.append(f"invalid agent_role: {role!r}")
        tc = fm.get("task_classes")
        if isinstance(tc, list):
            for c in tc:
                if c not in VALID_TASK_CLASSES:
                    errors.append(f"invalid task_class in task_classes: {c!r}")
        elif tc is not None:
            errors.append("task_classes must be a list")

    elif actual_type == "workflow-learning":
        tc = fm.get("task_class")
        if tc and tc not in VALID_TASK_CLASSES:
            errors.append(f"invalid task_class: {tc!r}")
        vo = fm.get("verification_outcome")
        if vo and vo not in VALID_VERIFICATION_OUTCOMES:
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

    elif actual_type == "adr":
        adr_status = fm.get("status")
        if adr_status and adr_status not in VALID_ADR_STATUSES:
            errors.append(f"invalid ADR status: {adr_status!r} (allowed: {sorted(VALID_ADR_STATUSES)})")

    elif actual_type == "moc":
        domain = fm.get("domain")
        if domain and domain not in VALID_MOC_DOMAINS:
            errors.append(f"invalid domain: {domain!r}")
        moc_id = fm.get("moc_id")
        if moc_id and not isinstance(moc_id, str):
            errors.append("moc_id must be a string")

    elif actual_type == "implementation-plan":
        # Optional fields validated when present (Phase 0 fix)
        status = fm.get("status")
        if status is not None and status not in VALID_PLAN_STATUSES:
            errors.append(f"invalid status: {status!r} (allowed: {sorted(VALID_PLAN_STATUSES)})")
        priority = fm.get("priority")
        if priority is not None and priority not in VALID_PLAN_PRIORITIES:
            errors.append(f"invalid priority: {priority!r} (allowed: {sorted(VALID_PLAN_PRIORITIES)})")
        progress = fm.get("progress")
        if progress is not None and (not isinstance(progress, str) or not PROGRESS_RE.match(progress)):
            errors.append(f"invalid progress format: {progress!r} (must match 'X/Y', e.g. '3/7')")

    return (len(errors) == 0), errors
