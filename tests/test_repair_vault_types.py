"""Tests for scripts/repair_vault_types.py — Phase 0 vault repair tool.

Covers:
- YAML frontmatter parsing (simple parser, no pyyaml)
- Status mapping for legacy values
- Fixability detection
- Dry-run vs repair mode
- Safety: unfixable notes must NOT be mutated
"""

import textwrap
from pathlib import Path

import pytest

from scripts.repair_vault_types import (
    _apply_fix,
    _is_fixable,
    _map_status,
    parse_frontmatter,
    scan_vault,
)

# ---------------------------------------------------------------------------
# Tests: parse_frontmatter
# ---------------------------------------------------------------------------


class TestParseFrontmatter:
    """Simple YAML frontmatter parser."""

    def test_basic_scalars(self):
        text = textwrap.dedent("""\
            ---
            type: agent-pattern
            title: Test Note
            confidence: high
            ---

            # Body
        """)
        fm = parse_frontmatter(text)
        assert fm is not None
        assert fm["type"] == "agent-pattern"
        assert fm["title"] == "Test Note"
        assert fm["confidence"] == "high"

    def test_quoted_values(self):
        text = textwrap.dedent("""\
            ---
            title: "My Note"
            status: 'active'
            ---

            # Body
        """)
        fm = parse_frontmatter(text)
        assert fm["title"] == "My Note"
        assert fm["status"] == "active"

    def test_list_block(self):
        text = textwrap.dedent("""\
            ---
            type: agent-pattern
            tags:
              - "#type/pattern"
              - "#status/active"
            ---

            # Body
        """)
        fm = parse_frontmatter(text)
        assert fm["tags"] == ["#type/pattern", "#status/active"]

    def test_inline_list(self):
        text = textwrap.dedent("""\
            ---
            task_classes: [research, code_impl]
            ---

            # Body
        """)
        fm = parse_frontmatter(text)
        assert fm["task_classes"] == ["research", "code_impl"]

    def test_integer_detection(self):
        text = textwrap.dedent("""\
            ---
            sources_count: 5
            ---

            # Body
        """)
        fm = parse_frontmatter(text)
        assert fm["sources_count"] == 5
        assert isinstance(fm["sources_count"], int)

    def test_boolean_detection(self):
        text = textwrap.dedent("""\
            ---
            enabled: true
            disabled: false
            ---

            # Body
        """)
        fm = parse_frontmatter(text)
        assert fm["enabled"] is True
        assert fm["disabled"] is False

    def test_no_frontmatter(self):
        text = "# Just a heading\n\nSome body text."
        assert parse_frontmatter(text) is None

    def test_empty_frontmatter(self):
        # Empty frontmatter block has no content between delimiters,
        # so the regex doesn't match — returns None (correctly)
        text = "---\n---\n\n# Body"
        fm = parse_frontmatter(text)
        assert fm is None

    def test_comment_lines_skipped(self):
        text = textwrap.dedent("""\
            ---
            # This is a comment
            type: inbox
            ---

            # Body
        """)
        fm = parse_frontmatter(text)
        assert "comment" not in str(fm)
        assert fm["type"] == "inbox"


# ---------------------------------------------------------------------------
# Tests: _map_status
# ---------------------------------------------------------------------------


class TestMapStatus:
    """Legacy status mapping for implementation-plan notes."""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("not-started", "backlog"),
            ("not_started", "backlog"),
            ("todo", "backlog"),
            ("pending", "backlog"),
            ("in-progress", "active"),
            ("in_progress", "active"),
            ("wip", "active"),
            ("done", "completed"),
            ("complete", "completed"),
            ("finished", "completed"),
            ("blocked", "paused"),
            ("on_hold", "paused"),
            ("on-hold", "paused"),
        ],
    )
    def test_legacy_mapping(self, raw, expected):
        assert _map_status(raw) == expected

    @pytest.mark.parametrize("canonical", ["backlog", "active", "completed", "paused"])
    def test_canonical_passthrough(self, canonical):
        assert _map_status(canonical) == canonical

    def test_unknown_returns_none(self):
        assert _map_status("nonsense") is None

    def test_case_insensitive(self):
        assert _map_status("NOT-STARTED") == "backlog"
        assert _map_status("WIP") == "active"


# ---------------------------------------------------------------------------
# Tests: _is_fixable
# ---------------------------------------------------------------------------


class TestIsFixable:
    """Detect which errors can be auto-repaired."""

    def test_missing_tag_is_fixable(self):
        assert _is_fixable("tags must include '#type/pattern'", {}) is True

    def test_invalid_status_is_fixable(self):
        assert _is_fixable("invalid status: 'not-started'", {}) is True

    def test_missing_field_not_fixable(self):
        assert _is_fixable("missing required field: plan_id", {}) is False

    def test_invalid_confidence_not_fixable(self):
        assert _is_fixable("invalid confidence: '0.9'", {}) is False

    def test_empty_field_not_fixable(self):
        assert _is_fixable("empty required field: title", {}) is False


# ---------------------------------------------------------------------------
# Tests: scan_vault (integration with temp directory)
# ---------------------------------------------------------------------------


class TestScanVault:
    """Vault scanning with temporary directory structure."""

    def _make_vault(self, tmp_path: Path, notes: dict[str, str]):
        """Create a minimal vault structure."""
        for rel_path, content in notes.items():
            p = tmp_path / rel_path
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        return tmp_path

    def test_valid_note_passes(self, tmp_path):
        vault = self._make_vault(
            tmp_path,
            {
                "20-agent-patterns/test-pattern.md": textwrap.dedent("""\
                ---
                type: agent-pattern
                pattern_id: ap-test
                title: Test Pattern
                agent_role: research
                confidence: high
                task_classes:
                  - research
                date_created: "2026-03-13"
                source: nova-core-memory
                tags:
                  - "#type/pattern"
                ---

                # Body
            """),
            },
        )
        results = scan_vault(vault)
        assert len(results) == 1
        assert results[0]["valid"] is True

    def test_invalid_note_fails(self, tmp_path):
        vault = self._make_vault(
            tmp_path,
            {
                "00-inbox/bad.md": textwrap.dedent("""\
                ---
                type: unknown-type
                title: Bad
                ---

                # Body
            """),
            },
        )
        results = scan_vault(vault)
        assert len(results) == 1
        assert results[0]["valid"] is False
        assert any("invalid type" in e for e in results[0]["errors"])

    def test_no_frontmatter_flagged(self, tmp_path):
        vault = self._make_vault(
            tmp_path,
            {
                "00-inbox/plain.md": "# Just a heading\n\nNo frontmatter here.",
            },
        )
        results = scan_vault(vault)
        assert len(results) == 1
        assert results[0]["frontmatter"] is None
        assert "no frontmatter" in str(results[0]["errors"])

    def test_hidden_dirs_skipped(self, tmp_path):
        vault = self._make_vault(
            tmp_path,
            {
                ".obsidian/config.md": "---\ntype: inbox\n---\nInternal",
                "_meta/internal.md": "---\ntype: inbox\n---\nInternal",
                "00-inbox/visible.md": (
                    "---\ntype: inbox\ntitle: Visible\nsource: operator\ntags:\n  - '#type/inbox'\n---\n\n# Visible"
                ),
            },
        )
        results = scan_vault(vault)
        # Only the visible note should be scanned
        assert len(results) == 1
        assert "visible.md" in results[0]["path"]


# ---------------------------------------------------------------------------
# Tests: _apply_fix safety
# ---------------------------------------------------------------------------


class TestApplyFix:
    """Repair script must only fix what it claims to fix."""

    def test_status_fix_applied(self, tmp_path):
        note = tmp_path / "test.md"
        content = textwrap.dedent("""\
            ---
            type: implementation-plan
            plan_id: plan-test
            title: Test Plan
            date_created: "2026-03-13"
            confidence: medium
            source: nova-core-memory
            status: not-started
            tags:
              - "#type/plan"
            ---

            # Body
        """)
        note.write_text(content, encoding="utf-8")
        fm = parse_frontmatter(content)
        fixes = _apply_fix(note, fm, ["invalid status: 'not-started'"])
        assert len(fixes) == 1
        assert "backlog" in fixes[0]
        # Verify the file was updated
        updated = note.read_text()
        assert "status: backlog" in updated
        assert "not-started" not in updated

    def test_unfixable_error_not_mutated(self, tmp_path):
        note = tmp_path / "test.md"
        content = textwrap.dedent("""\
            ---
            type: research-summary
            research_id: rs-test
            title: Test
            topic: testing
            date_researched: "2026-03-13"
            sources_count: 5
            confidence: "0.9"
            source: operator
            tags:
              - "#type/research"
            ---

            # Body
        """)
        note.write_text(content, encoding="utf-8")
        fm = parse_frontmatter(content)
        # _apply_fix is only called for fixable errors, but if called
        # with unfixable errors, it should not modify the file
        fixes = _apply_fix(note, fm, ["invalid confidence: '0.9'"])
        assert fixes == []
        assert note.read_text() == content  # file unchanged
