"""Tests for schemas/vault_note_schema.py — the standalone Phase 0 validator.

Verifies that the standalone module stays in sync with the MCP vault server
and correctly validates all canonical note types including Phase 0 additions
(ADR type, implementation-plan optional fields).
"""

from typing import ClassVar

import pytest

from schemas.vault_note_schema import (
    PROGRESS_RE,
    REQUIRED_FIELDS,
    VALID_ADR_STATUSES,
    VALID_MOC_DOMAINS,
    VALID_NOTE_TYPES,
    VALID_PLAN_PRIORITIES,
    VALID_PLAN_STATUSES,
    validate_frontmatter,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fm_agent_pattern(**kw):
    base = {
        "type": "agent-pattern",
        "pattern_id": "ap-test",
        "title": "Test Pattern",
        "agent_role": "research",
        "confidence": "high",
        "task_classes": ["research"],
        "date_created": "2026-03-13",
        "source": "nova-core-memory",
        "tags": ["#type/pattern"],
    }
    base.update(kw)
    return base


def _fm_adr(**kw):
    base = {
        "type": "adr",
        "adr_id": "ADR-001",
        "title": "Test ADR",
        "status": "proposed",
        "date": "2026-03-13",
        "source": "nova-core-memory",
        "tags": ["#type/adr"],
    }
    base.update(kw)
    return base


def _fm_plan(**kw):
    base = {
        "type": "implementation-plan",
        "plan_id": "plan-test",
        "title": "Test Plan",
        "date_created": "2026-03-13",
        "confidence": "medium",
        "source": "nova-core-memory",
        "tags": ["#type/plan"],
    }
    base.update(kw)
    return base


def _fm_inbox(**kw):
    base = {
        "type": "inbox",
        "title": "Quick Note",
        "source": "nova-core-memory",
        "tags": ["#type/inbox"],
    }
    base.update(kw)
    return base


def _fm_debug(**kw):
    base = {
        "type": "debugging-guide",
        "title": "Debug Playbook",
        "date_created": "2026-03-13",
        "source": "nova-core-memory",
        "tags": ["#type/debugging"],
    }
    base.update(kw)
    return base


def _fm_learning(**kw):
    base = {
        "type": "workflow-learning",
        "learning_id": "wl-2026-03-test",
        "title": "Test Learning",
        "workflow_id": "wf_test",
        "task_class": "research",
        "verification_outcome": "approved",
        "confidence": "high",
        "roles_involved": ["research"],
        "date": "2026-03-13",
        "source": "nova-core-memory",
        "tags": ["#type/learning"],
    }
    base.update(kw)
    return base


def _fm_research(**kw):
    base = {
        "type": "research-summary",
        "research_id": "rs-test",
        "title": "Test Research",
        "topic": "testing",
        "date_researched": "2026-03-13",
        "sources_count": 3,
        "confidence": "medium",
        "source": "nova-core-memory",
        "tags": ["#type/research"],
    }
    base.update(kw)
    return base


def _fm_moc(**kw):
    base = {
        "type": "moc",
        "moc_id": "moc-novatrade",
        "title": "MOC: NovaTrade",
        "domain": "novatrade",
        "date_created": "2026-04-06",
        "source": "nova-core-memory",
        "tags": ["#type/moc"],
    }
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# Tests: all canonical types accepted
# ---------------------------------------------------------------------------


class TestAllCanonicalTypes:
    """Each canonical type must be accepted with valid frontmatter."""

    BUILDERS: ClassVar[dict] = {
        "agent-pattern": _fm_agent_pattern,
        "workflow-learning": _fm_learning,
        "research-summary": _fm_research,
        "implementation-plan": _fm_plan,
        "debugging-guide": _fm_debug,
        "inbox": _fm_inbox,
        "adr": _fm_adr,
        "moc": _fm_moc,
    }

    def test_all_types_have_builders(self):
        assert set(self.BUILDERS.keys()) == set(VALID_NOTE_TYPES.keys())

    @pytest.mark.parametrize("note_type", sorted(VALID_NOTE_TYPES.keys()))
    def test_valid_note_accepted(self, note_type):
        builder = self.BUILDERS[note_type]
        valid, errors = validate_frontmatter(builder())
        assert valid is True, f"{note_type} rejected: {errors}"

    @pytest.mark.parametrize("note_type", sorted(VALID_NOTE_TYPES.keys()))
    def test_required_fields_defined(self, note_type):
        assert note_type in REQUIRED_FIELDS, f"No required fields for {note_type}"


# ---------------------------------------------------------------------------
# Tests: rejection paths
# ---------------------------------------------------------------------------


class TestRejection:
    """Invalid input must be rejected with clear error messages."""

    def test_missing_type(self):
        valid, errors = validate_frontmatter({"title": "No type"})
        assert valid is False
        assert any("type" in e for e in errors)

    def test_unknown_type(self):
        valid, errors = validate_frontmatter({"type": "blog-post"})
        assert valid is False
        assert any("invalid type" in e for e in errors)

    def test_empty_type(self):
        valid, errors = validate_frontmatter({"type": ""})
        assert valid is False

    def test_none_type(self):
        valid, errors = validate_frontmatter({"type": None})
        assert valid is False

    def test_missing_required_field(self):
        fm = _fm_agent_pattern()
        del fm["pattern_id"]
        valid, errors = validate_frontmatter(fm)
        assert valid is False
        assert any("pattern_id" in e for e in errors)

    def test_empty_string_field(self):
        valid, errors = validate_frontmatter(_fm_agent_pattern(title=""))
        assert valid is False
        assert any("title" in e for e in errors)

    def test_null_field(self):
        valid, errors = validate_frontmatter(_fm_agent_pattern(pattern_id=None))
        assert valid is False
        assert any("null" in e for e in errors)

    def test_title_too_long(self):
        valid, errors = validate_frontmatter(_fm_agent_pattern(title="x" * 101))
        assert valid is False
        assert any("title too long" in e for e in errors)

    def test_tags_not_list(self):
        valid, errors = validate_frontmatter(_fm_agent_pattern(tags="#type/pattern"))
        assert valid is False
        assert any("tags" in e for e in errors)

    def test_tags_empty(self):
        valid, errors = validate_frontmatter(_fm_agent_pattern(tags=[]))
        assert valid is False

    def test_tags_missing_required(self):
        valid, errors = validate_frontmatter(_fm_agent_pattern(tags=["#other"]))
        assert valid is False
        assert any("#type/pattern" in e for e in errors)

    def test_too_many_tags(self):
        tags = ["#type/pattern"] + [f"#t/{i}" for i in range(11)]
        valid, errors = validate_frontmatter(_fm_agent_pattern(tags=tags))
        assert valid is False
        assert any("too many" in e for e in errors)

    def test_invalid_source(self):
        valid, errors = validate_frontmatter(_fm_agent_pattern(source="github"))
        assert valid is False
        assert any("source" in e for e in errors)

    def test_invalid_confidence(self):
        valid, errors = validate_frontmatter(_fm_agent_pattern(confidence="0.9"))
        assert valid is False
        assert any("confidence" in e for e in errors)


# ---------------------------------------------------------------------------
# Tests: ADR type (Phase 0 addition)
# ---------------------------------------------------------------------------


class TestADRValidation:
    """ADR type added in Phase 0 — validate all status enum values."""

    @pytest.mark.parametrize("status", sorted(VALID_ADR_STATUSES))
    def test_valid_adr_statuses(self, status):
        valid, errors = validate_frontmatter(_fm_adr(status=status))
        assert valid is True, f"ADR status {status!r} rejected: {errors}"

    def test_invalid_adr_status(self):
        valid, errors = validate_frontmatter(_fm_adr(status="draft"))
        assert valid is False
        assert any("ADR status" in e for e in errors)

    def test_adr_missing_adr_id(self):
        fm = _fm_adr()
        del fm["adr_id"]
        valid, errors = validate_frontmatter(fm)
        assert valid is False

    def test_adr_missing_date(self):
        fm = _fm_adr()
        del fm["date"]
        valid, errors = validate_frontmatter(fm)
        assert valid is False

    def test_adr_required_tag(self):
        valid, errors = validate_frontmatter(_fm_adr(tags=["#other"]))
        assert valid is False
        assert any("#type/adr" in e for e in errors)


# ---------------------------------------------------------------------------
# Tests: MOC type validation
# ---------------------------------------------------------------------------


class TestMOCValidation:
    """MOC type validation."""

    def test_valid_moc_passes(self):
        valid, errors = validate_frontmatter(_fm_moc())
        assert valid is True, f"MOC rejected: {errors}"

    def test_moc_missing_domain_fails(self):
        fm = _fm_moc()
        del fm["domain"]
        valid, errors = validate_frontmatter(fm)
        assert valid is False
        assert any("domain" in e for e in errors)

    def test_moc_invalid_domain_fails(self):
        valid, errors = validate_frontmatter(_fm_moc(domain="fantasy"))
        assert valid is False
        assert any("domain" in e for e in errors)

    def test_moc_missing_moc_id_fails(self):
        fm = _fm_moc()
        del fm["moc_id"]
        valid, errors = validate_frontmatter(fm)
        assert valid is False
        assert any("moc_id" in e for e in errors)

    def test_moc_required_tag(self):
        valid, errors = validate_frontmatter(_fm_moc(tags=["#other"]))
        assert valid is False
        assert any("#type/moc" in e for e in errors)

    def test_moc_all_valid_domains(self):
        for domain in sorted(VALID_MOC_DOMAINS):
            valid, errors = validate_frontmatter(_fm_moc(domain=domain))
            assert valid is True, f"MOC domain {domain!r} rejected: {errors}"


# ---------------------------------------------------------------------------
# Tests: implementation-plan optional fields (Phase 0 addition)
# ---------------------------------------------------------------------------


class TestPlanOptionalFields:
    """Status, priority, progress fields added in Phase 0."""

    def test_plan_without_optionals(self):
        valid, errors = validate_frontmatter(_fm_plan())
        assert valid is True, errors

    @pytest.mark.parametrize("status", sorted(VALID_PLAN_STATUSES))
    def test_valid_plan_statuses(self, status):
        valid, errors = validate_frontmatter(_fm_plan(status=status))
        assert valid is True, f"Plan status {status!r} rejected: {errors}"

    @pytest.mark.parametrize("priority", sorted(VALID_PLAN_PRIORITIES))
    def test_valid_plan_priorities(self, priority):
        valid, errors = validate_frontmatter(_fm_plan(priority=priority))
        assert valid is True, f"Plan priority {priority!r} rejected: {errors}"

    @pytest.mark.parametrize("progress", ["0/5", "3/7", "10/10", "0/0"])
    def test_valid_progress_formats(self, progress):
        valid, errors = validate_frontmatter(_fm_plan(progress=progress))
        assert valid is True, f"Progress {progress!r} rejected: {errors}"

    def test_invalid_status(self):
        valid, errors = validate_frontmatter(_fm_plan(status="not-started"))
        assert valid is False
        assert any("status" in e for e in errors)

    def test_invalid_priority(self):
        valid, errors = validate_frontmatter(_fm_plan(priority="urgent"))
        assert valid is False
        assert any("priority" in e for e in errors)

    @pytest.mark.parametrize("bad_progress", ["almost", "3 of 7", "3-7", "3/", "/7"])
    def test_invalid_progress(self, bad_progress):
        valid, errors = validate_frontmatter(_fm_plan(progress=bad_progress))
        assert valid is False
        assert any("progress" in e for e in errors)

    def test_progress_non_string(self):
        valid, errors = validate_frontmatter(_fm_plan(progress=3))
        assert valid is False
        assert any("progress" in e for e in errors)

    def test_all_optionals_valid(self):
        valid, errors = validate_frontmatter(_fm_plan(status="active", priority="high", progress="3/7"))
        assert valid is True, errors


# ---------------------------------------------------------------------------
# Tests: type-specific field validation
# ---------------------------------------------------------------------------


class TestTypeSpecificValidation:
    """Enum validations on type-specific fields."""

    def test_invalid_agent_role(self):
        valid, errors = validate_frontmatter(_fm_agent_pattern(agent_role="wizard"))
        assert valid is False

    def test_task_classes_not_list(self):
        valid, errors = validate_frontmatter(_fm_agent_pattern(task_classes="research"))
        assert valid is False

    def test_invalid_task_class_in_list(self):
        valid, errors = validate_frontmatter(_fm_agent_pattern(task_classes=["magic"]))
        assert valid is False

    def test_invalid_task_class_on_learning(self):
        valid, errors = validate_frontmatter(_fm_learning(task_class="magic"))
        assert valid is False

    def test_invalid_verification_outcome(self):
        valid, errors = validate_frontmatter(_fm_learning(verification_outcome="maybe"))
        assert valid is False

    def test_empty_roles_involved(self):
        valid, errors = validate_frontmatter(_fm_learning(roles_involved=[]))
        assert valid is False

    def test_roles_involved_not_list(self):
        valid, errors = validate_frontmatter(_fm_learning(roles_involved="research"))
        assert valid is False

    def test_sources_count_not_int(self):
        valid, errors = validate_frontmatter(_fm_research(sources_count="five"))
        assert valid is False


# ---------------------------------------------------------------------------
# Tests: enum completeness
# ---------------------------------------------------------------------------


class TestEnumCompleteness:
    """Verify canonical enums match MCP server expectations."""

    def test_note_types_include_adr(self):
        assert "adr" in VALID_NOTE_TYPES

    def test_adr_has_required_tag(self):
        assert VALID_NOTE_TYPES["adr"] == "#type/adr"

    def test_plan_statuses_complete(self):
        assert VALID_PLAN_STATUSES == {"backlog", "active", "completed", "paused"}

    def test_plan_priorities_complete(self):
        assert VALID_PLAN_PRIORITIES == {"high", "medium", "low"}

    def test_adr_statuses_complete(self):
        assert VALID_ADR_STATUSES == {"proposed", "accepted", "deprecated", "superseded"}

    def test_note_types_include_moc(self):
        assert "moc" in VALID_NOTE_TYPES

    def test_moc_has_required_tag(self):
        assert VALID_NOTE_TYPES["moc"] == "#type/moc"

    def test_moc_domains_complete(self):
        expected = {
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
        assert VALID_MOC_DOMAINS == expected

    def test_progress_regex_valid(self):
        assert PROGRESS_RE.match("3/7")
        assert PROGRESS_RE.match("0/0")
        assert PROGRESS_RE.match("10/10")
        assert not PROGRESS_RE.match("3/")
        assert not PROGRESS_RE.match("/7")
        assert not PROGRESS_RE.match("three/seven")


# ---------------------------------------------------------------------------
# Tests: MCP server sync check
# ---------------------------------------------------------------------------


class TestMCPServerSync:
    """Verify standalone schema matches MCP vault server."""

    def test_type_enums_match(self):
        """Standalone VALID_NOTE_TYPES must match MCP server."""
        import importlib

        # Load the MCP server module's valid types
        mcp_path = "/home/nova/nova-core/tools/mcp_vault_server.py"
        spec = importlib.util.spec_from_file_location("mcp_vault_server", mcp_path)
        _ = importlib.util.module_from_spec(spec)
        # Don't actually run the server — just parse
        # Instead, extract _VALID_NOTE_TYPES via grep
        import re

        with open(mcp_path) as f:
            content = f.read()

        # Find all type keys in the MCP server's dict
        mcp_types = set()
        in_block = False
        for line in content.splitlines():
            if "_VALID_NOTE_TYPES" in line and "=" in line:
                in_block = True
                continue
            if in_block:
                if "}" in line:
                    # Extract any type on closing line too
                    m = re.search(r'"([^"]+)":\s*"#type/', line)
                    if m:
                        mcp_types.add(m.group(1))
                    break
                m = re.search(r'"([^"]+)":\s*"#type/', line)
                if m:
                    mcp_types.add(m.group(1))

        assert mcp_types == set(VALID_NOTE_TYPES.keys()), (
            f"Standalone types {set(VALID_NOTE_TYPES.keys())} != MCP server types {mcp_types}"
        )
