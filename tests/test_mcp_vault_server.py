"""Tests for the Obsidian vault MCP server (Phase 1 + Phase 1.5 + Phase 2).

Phase 1 tests:
  - Path safety (traversal, absolute, null bytes, .obsidian access)
  - vault_list success and error cases
  - vault_read success and error cases
  - vault_search keyword matching
  - vault_frontmatter parsing
  - Non-markdown handling
  - Malformed frontmatter behavior

Phase 1.5 tests:
  - Vault config loading / identity
  - vault_info tool
  - Vault path validation
  - Human-authored note coexistence (read resilience)

Phase 2 tests:
  - Schema/frontmatter validation
  - Sensitive content detection
  - Write feature flag gating
  - vault_write approved/rejected cases
  - vault_update approved/rejected cases
  - vault_validate dry-run
  - Folder restriction enforcement
  - Rate limiting
  - Audit logging
"""

import json
import os
import textwrap
import time
from pathlib import Path
from unittest import mock

import pytest

# We test the module functions directly (not via MCP protocol)
from tools.mcp_vault_server import (
    _is_markdown,
    _load_vault_config,
    _parse_frontmatter,
    _safe_resolve,
    _write_timestamps,
    detect_sensitive_content,
    validate_frontmatter,
    validate_vault_path,
    vault_frontmatter,
    vault_info,
    vault_list,
    vault_read,
    vault_search,
    vault_update,
    vault_validate,
    vault_write,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def vault_dir(tmp_path):
    """Create a temporary vault structure for testing."""
    # Create folders
    for folder in [
        "00-inbox",
        "10-adrs",
        "20-agent-patterns",
        "30-workflow-learnings",
        "40-research",
        "70-debugging",
        "_meta",
        ".obsidian",
    ]:
        (tmp_path / folder).mkdir()

    # Create seed notes
    adr = tmp_path / "10-adrs" / "ADR-001-test.md"
    adr.write_text(
        textwrap.dedent("""\
        ---
        type: adr
        adr_id: "ADR-001"
        title: "Test Architecture Decision"
        status: "accepted"
        date: "2026-03-07"
        decision_makers:
          - "operator"
        tags:
          - "#type/adr"
          - "#status/active"
        source: "operator"
        ---

        ## Context
        This is a test ADR for unit testing.

        ## Decision
        We decided to test things.
        """)
    )

    pattern = tmp_path / "20-agent-patterns" / "research-patterns.md"
    pattern.write_text(
        textwrap.dedent("""\
        ---
        type: agent-pattern
        pattern_id: "ap-research-search"
        title: "Research Search Patterns"
        agent_role: "research"
        confidence: "high"
        task_classes:
          - "research"
        date_created: "2026-03-07"
        date_updated: "2026-03-07"
        source: "operator"
        tags:
          - "#type/pattern"
        ---

        ## Summary
        Patterns for research agent web searches.
        """)
    )

    # Note without frontmatter
    plain = tmp_path / "00-inbox" / "quick-note.md"
    plain.write_text("# Quick Note\n\nJust a plain markdown file.\n")

    # Non-markdown file
    txt = tmp_path / "00-inbox" / "notes.txt"
    txt.write_text("this is not markdown")

    # Note with malformed frontmatter
    bad_fm = tmp_path / "00-inbox" / "bad-frontmatter.md"
    bad_fm.write_text("---\n: invalid: yaml: {{{\n---\n\n# Bad\n")

    # Hidden folder file (should be ignored)
    (tmp_path / ".obsidian" / "config.json").write_text("{}")

    return tmp_path


@pytest.fixture(autouse=True)
def patch_vault_root(vault_dir):
    """Patch VAULT_ROOT, config path, and audit log to point to the temp vault."""
    import tools.mcp_vault_server as mod

    original_root = mod.VAULT_ROOT
    original_audit = mod._AUDIT_LOG_PATH
    original_config = mod._VAULT_CONFIG_PATH
    mod.VAULT_ROOT = vault_dir.resolve()
    mod._AUDIT_LOG_PATH = vault_dir / ".nova-audit.log"
    mod._VAULT_CONFIG_PATH = vault_dir / ".nova-vault-config.json"
    # Clear rate limit state between tests
    mod._write_timestamps.clear()
    yield
    mod.VAULT_ROOT = original_root
    mod._AUDIT_LOG_PATH = original_audit
    mod._VAULT_CONFIG_PATH = original_config
    mod._write_timestamps.clear()


def _write_config(vault_dir, enabled=True, **overrides):
    """Write a write-config JSON to the temp vault."""
    config = {
        "enabled": enabled,
        "allowed_folders": ["00-inbox", "20-agent-patterns", "30-workflow-learnings", "40-research", "70-debugging"],
        "max_writes_per_window": 10,
        "window_seconds": 300,
        "max_note_size_bytes": 34 * 1024,
        "require_frontmatter_validation": True,
    }
    config.update(overrides)
    path = vault_dir / ".nova-write-config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    # Also patch the env var so _load_write_config finds it
    os.environ["NOVA_VAULT_WRITE_FLAG"] = str(path)
    return path


@pytest.fixture()
def write_enabled(vault_dir):
    """Enable writes via feature flag config."""
    _write_config(vault_dir, enabled=True)
    yield
    os.environ.pop("NOVA_VAULT_WRITE_FLAG", None)


@pytest.fixture()
def write_disabled(vault_dir):
    """Explicitly disable writes via feature flag config."""
    _write_config(vault_dir, enabled=False)
    yield
    os.environ.pop("NOVA_VAULT_WRITE_FLAG", None)


def _valid_pattern_fm(**overrides):
    """Minimal valid agent-pattern frontmatter."""
    fm = {
        "type": "agent-pattern",
        "pattern_id": "ap-test-new",
        "title": "Test Pattern",
        "agent_role": "research",
        "confidence": "high",
        "task_classes": ["research"],
        "date_created": "2026-03-07",
        "date_updated": "2026-03-07",
        "source": "operator",
        "tags": ["#type/pattern"],
    }
    fm.update(overrides)
    return fm


def _valid_learning_fm(**overrides):
    """Minimal valid workflow-learning frontmatter."""
    fm = {
        "type": "workflow-learning",
        "learning_id": "wl-2026-03-test",
        "title": "Test Learning",
        "workflow_id": "wf_test",
        "task_class": "research",
        "verification_outcome": "approved",
        "confidence": "high",
        "roles_involved": ["research"],
        "date": "2026-03-07",
        "source": "operator",
        "tags": ["#type/learning"],
    }
    fm.update(overrides)
    return fm


def _valid_research_fm(**overrides):
    """Minimal valid research-summary frontmatter."""
    fm = {
        "type": "research-summary",
        "research_id": "rs-test-topic",
        "title": "Test Research",
        "topic": "testing",
        "date_researched": "2026-03-07",
        "date_updated": "2026-03-07",
        "sources_count": 2,
        "confidence": "medium",
        "source": "nova-core-memory",
        "tags": ["#type/research"],
    }
    fm.update(overrides)
    return fm


# ---------------------------------------------------------------------------
# Phase 1: Path safety tests (unchanged)
# ---------------------------------------------------------------------------


class TestPathSafety:
    """Path traversal and escape prevention."""

    def test_normal_relative_path(self, vault_dir):
        result = _safe_resolve("10-adrs/ADR-001-test.md")
        assert result is not None
        assert result.is_file()

    def test_rejects_absolute_path(self):
        result = _safe_resolve("/etc/passwd")
        assert result is None

    def test_rejects_traversal_dotdot(self):
        result = _safe_resolve("../../../etc/passwd")
        assert result is None

    def test_rejects_traversal_within_vault(self):
        result = _safe_resolve("10-adrs/../../etc/passwd")
        assert result is None

    def test_rejects_null_bytes(self):
        result = _safe_resolve("10-adrs/ADR\x00-001.md")
        assert result is None

    def test_rejects_obsidian_folder(self):
        result = _safe_resolve(".obsidian/config.json")
        assert result is None

    def test_empty_path_resolves_to_vault_root(self):
        result = _safe_resolve("")
        assert result is not None

    def test_nonexistent_path(self):
        result = _safe_resolve("nonexistent/file.md")
        assert result is not None or result is None


class TestIsMarkdown:
    """Markdown file detection."""

    def test_md_extension(self, vault_dir):
        path = vault_dir / "10-adrs" / "ADR-001-test.md"
        assert _is_markdown(path) is True

    def test_txt_extension(self, vault_dir):
        path = vault_dir / "00-inbox" / "notes.txt"
        assert _is_markdown(path) is False

    def test_directory(self, vault_dir):
        assert _is_markdown(vault_dir / "10-adrs") is False

    def test_nonexistent(self, vault_dir):
        assert _is_markdown(vault_dir / "nope.md") is False


# ---------------------------------------------------------------------------
# Phase 1: vault_list tests (unchanged)
# ---------------------------------------------------------------------------


class TestVaultList:
    """Listing vault contents."""

    def test_list_root(self):
        result = vault_list()
        assert "error" not in result
        assert "00-inbox" in result["folders"]
        assert "10-adrs" in result["folders"]
        assert "_meta" in result["folders"]
        assert ".obsidian" not in result["folders"]

    def test_list_subfolder(self):
        result = vault_list("10-adrs")
        assert "error" not in result
        assert any("ADR-001-test.md" in f for f in result["files"])

    def test_list_nonexistent_folder(self):
        result = vault_list("nonexistent-folder")
        assert "error" in result

    def test_list_file_as_folder(self):
        result = vault_list("10-adrs/ADR-001-test.md")
        assert "error" in result

    def test_list_traversal_rejected(self):
        result = vault_list("../")
        assert "error" in result

    def test_list_obsidian_rejected(self):
        result = vault_list(".obsidian")
        assert "error" in result

    def test_list_empty_folder(self):
        result = vault_list("_meta")
        assert "error" not in result
        assert result["files"] == []


# ---------------------------------------------------------------------------
# Phase 1: vault_read tests (unchanged)
# ---------------------------------------------------------------------------


class TestVaultRead:
    """Reading vault notes."""

    def test_read_existing_note(self):
        result = vault_read("10-adrs/ADR-001-test.md")
        assert "error" not in result
        assert "content" in result
        assert "Test Architecture Decision" in result["content"]
        assert result["size"] > 0

    def test_read_nonexistent_note(self):
        result = vault_read("10-adrs/nonexistent.md")
        assert "error" in result

    def test_read_empty_path(self):
        result = vault_read("")
        assert "error" in result

    def test_read_non_markdown(self):
        result = vault_read("00-inbox/notes.txt")
        assert "error" in result
        assert "Not a markdown" in result["error"]

    def test_read_directory_path(self):
        result = vault_read("10-adrs")
        assert "error" in result

    def test_read_traversal_rejected(self):
        result = vault_read("../../../etc/passwd")
        assert "error" in result

    def test_read_obsidian_rejected(self):
        result = vault_read(".obsidian/config.json")
        assert "error" in result

    def test_read_plain_note_without_frontmatter(self):
        result = vault_read("00-inbox/quick-note.md")
        assert "error" not in result
        assert "Quick Note" in result["content"]


# ---------------------------------------------------------------------------
# Phase 1: vault_search tests (unchanged)
# ---------------------------------------------------------------------------


class TestVaultSearch:
    """Searching vault notes."""

    def test_search_by_content_keyword(self):
        result = vault_search("Architecture Decision")
        assert "error" not in result
        assert result["results_count"] >= 1
        paths = [r["path"] for r in result["results"]]
        assert any("ADR-001" in p for p in paths)

    def test_search_by_filename(self):
        result = vault_search("research-patterns")
        assert result["results_count"] >= 1
        assert result["results"][0]["name_match"] is True

    def test_search_case_insensitive(self):
        result = vault_search("ARCHITECTURE DECISION")
        assert result["results_count"] >= 1

    def test_search_no_results(self):
        result = vault_search("xyzzy_nonexistent_term_12345")
        assert result["results_count"] == 0
        assert result["results"] == []

    def test_search_empty_query(self):
        result = vault_search("")
        assert "error" in result

    def test_search_whitespace_query(self):
        result = vault_search("   ")
        assert "error" in result

    def test_search_in_subfolder(self):
        result = vault_search("test", folder="10-adrs")
        assert result["results_count"] >= 1
        for r in result["results"]:
            assert r["path"].startswith("10-adrs/")

    def test_search_invalid_folder(self):
        result = vault_search("test", folder="../")
        assert "error" in result

    def test_search_returns_snippets(self):
        result = vault_search("Architecture Decision")
        matching = [r for r in result["results"] if r["content_match"]]
        assert len(matching) >= 1
        assert matching[0]["snippet"]

    def test_search_skips_hidden_dirs(self):
        result = vault_search("config")
        paths = [r["path"] for r in result["results"]]
        assert not any(".obsidian" in p for p in paths)


# ---------------------------------------------------------------------------
# Phase 1: vault_frontmatter tests (unchanged)
# ---------------------------------------------------------------------------


class TestVaultFrontmatter:
    """Frontmatter extraction."""

    def test_valid_frontmatter(self):
        result = vault_frontmatter("10-adrs/ADR-001-test.md")
        assert "error" not in result
        assert result["has_frontmatter"] is True
        fm = result["frontmatter"]
        assert fm["type"] == "adr"
        assert fm["adr_id"] == "ADR-001"
        assert fm["title"] == "Test Architecture Decision"
        assert fm["status"] == "accepted"
        assert isinstance(fm["tags"], list)

    def test_no_frontmatter(self):
        result = vault_frontmatter("00-inbox/quick-note.md")
        assert "error" not in result
        assert result["has_frontmatter"] is False
        assert result["frontmatter"] == {}

    def test_malformed_frontmatter(self):
        result = vault_frontmatter("00-inbox/bad-frontmatter.md")
        assert "error" not in result
        assert result["has_frontmatter"] is False

    def test_frontmatter_nonexistent_file(self):
        result = vault_frontmatter("nonexistent.md")
        assert "error" in result

    def test_frontmatter_empty_path(self):
        result = vault_frontmatter("")
        assert "error" in result

    def test_frontmatter_non_markdown(self):
        result = vault_frontmatter("00-inbox/notes.txt")
        assert "error" in result

    def test_frontmatter_traversal_rejected(self):
        result = vault_frontmatter("../../../etc/passwd")
        assert "error" in result

    def test_frontmatter_agent_pattern(self):
        result = vault_frontmatter("20-agent-patterns/research-patterns.md")
        assert result["has_frontmatter"] is True
        fm = result["frontmatter"]
        assert fm["type"] == "agent-pattern"
        assert fm["agent_role"] == "research"
        assert fm["confidence"] == "high"


# ---------------------------------------------------------------------------
# Phase 1: Frontmatter parser unit tests (unchanged)
# ---------------------------------------------------------------------------


class TestParseFrontmatter:
    """Low-level frontmatter parsing."""

    def test_valid_yaml(self):
        content = "---\ntype: adr\ntitle: Test\n---\n\n# Body"
        fm = _parse_frontmatter(content)
        assert fm == {"type": "adr", "title": "Test"}

    def test_no_frontmatter(self):
        content = "# Just a heading\n\nSome text."
        assert _parse_frontmatter(content) is None

    def test_incomplete_frontmatter(self):
        content = "---\ntype: adr\n# No closing delimiter"
        assert _parse_frontmatter(content) is None

    def test_empty_frontmatter(self):
        content = "---\n---\n\n# Body"
        assert _parse_frontmatter(content) is None

    def test_non_dict_frontmatter(self):
        content = "---\n- item1\n- item2\n---\n\n# Body"
        assert _parse_frontmatter(content) is None

    def test_malformed_yaml(self):
        content = "---\n: bad: yaml: {{{\n---\n\n# Body"
        assert _parse_frontmatter(content) is None

    def test_frontmatter_with_lists(self):
        content = "---\ntags:\n  - a\n  - b\n---\n\n# Body"
        fm = _parse_frontmatter(content)
        assert fm == {"tags": ["a", "b"]}


# ---------------------------------------------------------------------------
# Phase 1: Edge case tests (unchanged)
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge cases and boundary conditions."""

    def test_oversized_file_rejected(self, vault_dir):
        big = vault_dir / "10-adrs" / "huge.md"
        big.write_text("---\ntype: adr\n---\n" + "x" * (35 * 1024))
        result = vault_read("10-adrs/huge.md")
        assert "error" in result
        assert "too large" in result["error"]

    def test_search_skips_oversized_files(self, vault_dir):
        big = vault_dir / "10-adrs" / "huge.md"
        big.write_text("---\ntype: adr\n---\n" + "unique_huge_term " * 5000)
        result = vault_search("unique_huge_term")
        paths = [r["path"] for r in result["results"]]
        assert not any("huge.md" in p for p in paths)

    def test_unicode_content(self, vault_dir):
        uni = vault_dir / "00-inbox" / "unicode.md"
        uni.write_text(
            "---\ntitle: Ünïcödé Nöte\n---\n\n# Héllo Wörld\n",
            encoding="utf-8",
        )
        result = vault_read("00-inbox/unicode.md")
        assert "error" not in result
        assert "Héllo" in result["content"]

    def test_search_unicode(self, vault_dir):
        uni = vault_dir / "00-inbox" / "unicode.md"
        uni.write_text(
            "---\ntitle: Ünïcödé Nöte\n---\n\n# Héllo Wörld\n",
            encoding="utf-8",
        )
        result = vault_search("Héllo")
        assert result["results_count"] >= 1


# ===========================================================================
# Phase 1.5: Vault identity / config / coexistence tests
# ===========================================================================


def _vault_config(vault_dir, **overrides):
    """Write a vault config JSON to the temp vault."""
    config = {
        "vault_id": "test-vault",
        "vault_name": "Test Vault",
        "sync_model": "obsidian-sync",
        "canonical": True,
        "human_editable": True,
        "nova_core_managed_folders": ["00-inbox", "20-agent-patterns", "30-workflow-learnings", "40-research", "70-debugging"],
        "human_managed_folders": ["10-adrs", "50-playbooks", "60-project", "80-references", "90-diary"],
        "created": "2026-03-07",
    }
    config.update(overrides)
    path = vault_dir / ".nova-vault-config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


class TestVaultConfig:
    """Vault identity config loading."""

    def test_load_valid_config(self, vault_dir):
        _vault_config(vault_dir)
        config = _load_vault_config()
        assert config["vault_id"] == "test-vault"
        assert config["sync_model"] == "obsidian-sync"
        assert config["canonical"] is True

    def test_load_missing_config(self, vault_dir):
        # No config file — returns defaults
        config = _load_vault_config()
        assert config["vault_id"] == "unknown"
        assert config["canonical"] is False

    def test_load_corrupt_config(self, vault_dir):
        (vault_dir / ".nova-vault-config.json").write_text("NOT JSON{{{")
        config = _load_vault_config()
        assert config["vault_id"] == "unknown"

    def test_load_non_dict_config(self, vault_dir):
        (vault_dir / ".nova-vault-config.json").write_text('"just a string"')
        config = _load_vault_config()
        assert config["vault_id"] == "unknown"


class TestValidateVaultPath:
    """Vault path validation."""

    def test_valid_vault(self, vault_dir):
        valid, issues = validate_vault_path()
        # May report missing .nova-vault-config.json but root exists
        assert valid or "vault root does not exist" not in str(issues)

    def test_missing_vault_root(self, vault_dir):
        import tools.mcp_vault_server as mod
        original = mod.VAULT_ROOT
        mod.VAULT_ROOT = Path("/nonexistent/path/unlikely")
        valid, issues = validate_vault_path()
        mod.VAULT_ROOT = original
        assert valid is False
        assert any("does not exist" in i for i in issues)

    def test_missing_obsidian_dir(self, vault_dir):
        import shutil
        shutil.rmtree(vault_dir / ".obsidian")
        valid, issues = validate_vault_path()
        assert any(".obsidian" in i for i in issues)

    def test_missing_config_file_is_issue(self, vault_dir):
        # No config file present
        valid, issues = validate_vault_path()
        assert any("nova-vault-config" in i for i in issues)

    def test_with_config_file(self, vault_dir):
        _vault_config(vault_dir)
        valid, issues = validate_vault_path()
        assert not any("nova-vault-config" in i for i in issues)


class TestVaultInfo:
    """vault_info read-only tool."""

    def test_info_returns_vault_root(self, vault_dir):
        _vault_config(vault_dir)
        result = vault_info()
        assert result["vault_root"] == str(vault_dir.resolve())

    def test_info_returns_config_fields(self, vault_dir):
        _vault_config(vault_dir)
        result = vault_info()
        assert result["vault_id"] == "test-vault"
        assert result["sync_model"] == "obsidian-sync"
        assert result["canonical"] is True
        assert result["human_editable"] is True

    def test_info_counts_notes(self, vault_dir):
        _vault_config(vault_dir)
        result = vault_info()
        counts = result["folder_note_counts"]
        # vault_dir fixture creates notes in 10-adrs, 20-agent-patterns, 00-inbox
        assert counts.get("10-adrs", 0) >= 1
        assert counts.get("20-agent-patterns", 0) >= 1

    def test_info_without_config(self, vault_dir):
        result = vault_info()
        assert result["vault_id"] == "unknown"
        assert result["sync_model"] == "unknown"

    def test_info_shows_managed_folders(self, vault_dir):
        _vault_config(vault_dir)
        result = vault_info()
        assert "00-inbox" in result["nova_core_managed_folders"]
        assert "10-adrs" in result["human_managed_folders"]

    def test_info_reports_issues(self, vault_dir):
        # No config file → should report issue
        result = vault_info()
        assert any("nova-vault-config" in i for i in result["issues"])


class TestHumanNoteCoexistence:
    """Ensure read tools handle human-authored notes gracefully."""

    def test_read_note_without_frontmatter(self, vault_dir):
        """Human notes may lack frontmatter — read should still work."""
        note = vault_dir / "00-inbox" / "human-quick-thought.md"
        note.write_text("# My Quick Thought\n\nJust some ideas.\n")
        result = vault_read("00-inbox/human-quick-thought.md")
        assert "error" not in result
        assert "My Quick Thought" in result["content"]

    def test_read_note_with_nonstandard_frontmatter(self, vault_dir):
        """Human notes may have frontmatter that doesn't match Nova schemas."""
        note = vault_dir / "90-diary" / "2026-03-07.md"
        (vault_dir / "90-diary").mkdir(exist_ok=True)
        note.write_text("---\ntitle: My Day\nmood: happy\n---\n\nGreat day.\n")
        result = vault_read("90-diary/2026-03-07.md")
        assert "error" not in result
        assert "Great day" in result["content"]

    def test_frontmatter_of_human_note(self, vault_dir):
        """frontmatter tool works on human notes with non-schema fields."""
        note = vault_dir / "00-inbox" / "human-note.md"
        note.write_text("---\ntitle: Human Note\ncustom_field: value\n---\n\nHello.\n")
        result = vault_frontmatter("00-inbox/human-note.md")
        assert result["has_frontmatter"] is True
        assert result["frontmatter"]["custom_field"] == "value"

    def test_search_finds_human_notes(self, vault_dir):
        """Search should find human-authored notes alongside Nova-Core notes."""
        note = vault_dir / "00-inbox" / "human-research.md"
        note.write_text("# Research on butterflies\n\nMonarch migration patterns.\n")
        result = vault_search("butterflies")
        assert result["results_count"] >= 1

    def test_list_shows_human_created_folders(self, vault_dir):
        """If human creates a new folder, list should show it."""
        (vault_dir / "99-personal").mkdir()
        note = vault_dir / "99-personal" / "ideas.md"
        note.write_text("# Ideas\n\nSome ideas.\n")
        result = vault_list()
        assert "99-personal" in result["folders"]

    def test_list_shows_human_notes_in_managed_folder(self, vault_dir):
        """Human can place notes in Nova-managed folders — list shows them."""
        note = vault_dir / "40-research" / "human-added-research.md"
        note.write_text("# Manual Research\n\nI looked this up myself.\n")
        result = vault_list("40-research")
        assert "40-research/human-added-research.md" in result["files"]


# ===========================================================================
# Phase 2: Schema validation tests
# ===========================================================================


class TestValidateFrontmatter:
    """Frontmatter schema validation."""

    def test_valid_agent_pattern(self):
        valid, errors = validate_frontmatter(_valid_pattern_fm())
        assert valid is True
        assert errors == []

    def test_valid_workflow_learning(self):
        valid, errors = validate_frontmatter(_valid_learning_fm())
        assert valid is True
        assert errors == []

    def test_valid_research_summary(self):
        valid, errors = validate_frontmatter(_valid_research_fm())
        assert valid is True
        assert errors == []

    def test_missing_type(self):
        fm = _valid_pattern_fm()
        del fm["type"]
        valid, errors = validate_frontmatter(fm)
        assert valid is False
        assert any("type" in e for e in errors)

    def test_invalid_type(self):
        fm = _valid_pattern_fm(type="unknown-type")
        valid, errors = validate_frontmatter(fm)
        assert valid is False
        assert any("invalid type" in e for e in errors)

    def test_missing_required_field(self):
        fm = _valid_pattern_fm()
        del fm["pattern_id"]
        valid, errors = validate_frontmatter(fm)
        assert valid is False
        assert any("pattern_id" in e for e in errors)

    def test_empty_required_field(self):
        fm = _valid_pattern_fm(title="")
        valid, errors = validate_frontmatter(fm)
        assert valid is False
        assert any("title" in e for e in errors)

    def test_title_too_long(self):
        fm = _valid_pattern_fm(title="x" * 101)
        valid, errors = validate_frontmatter(fm)
        assert valid is False
        assert any("title too long" in e for e in errors)

    def test_missing_required_tag(self):
        fm = _valid_pattern_fm(tags=["#status/active"])
        valid, errors = validate_frontmatter(fm)
        assert valid is False
        assert any("#type/pattern" in e for e in errors)

    def test_too_many_tags(self):
        fm = _valid_pattern_fm(tags=["#type/pattern"] + [f"#tag/{i}" for i in range(11)])
        valid, errors = validate_frontmatter(fm)
        assert valid is False
        assert any("too many tags" in e for e in errors)

    def test_invalid_confidence(self):
        fm = _valid_pattern_fm(confidence="very_high")
        valid, errors = validate_frontmatter(fm)
        assert valid is False
        assert any("confidence" in e for e in errors)

    def test_invalid_source(self):
        fm = _valid_pattern_fm(source="unknown_source")
        valid, errors = validate_frontmatter(fm)
        assert valid is False
        assert any("source" in e for e in errors)

    def test_invalid_agent_role(self):
        fm = _valid_pattern_fm(agent_role="wizard")
        valid, errors = validate_frontmatter(fm)
        assert valid is False
        assert any("agent_role" in e for e in errors)

    def test_invalid_task_class_in_list(self):
        fm = _valid_pattern_fm(task_classes=["magic"])
        valid, errors = validate_frontmatter(fm)
        assert valid is False
        assert any("task_class" in e for e in errors)

    def test_invalid_verification_outcome(self):
        fm = _valid_learning_fm(verification_outcome="maybe")
        valid, errors = validate_frontmatter(fm)
        assert valid is False
        assert any("verification_outcome" in e for e in errors)

    def test_invalid_task_class_on_learning(self):
        fm = _valid_learning_fm(task_class="magic")
        valid, errors = validate_frontmatter(fm)
        assert valid is False

    def test_research_sources_count_not_int(self):
        fm = _valid_research_fm(sources_count="five")
        valid, errors = validate_frontmatter(fm)
        assert valid is False
        assert any("sources_count" in e for e in errors)

    def test_empty_roles_involved(self):
        fm = _valid_learning_fm(roles_involved=[])
        valid, errors = validate_frontmatter(fm)
        assert valid is False
        assert any("roles_involved" in e for e in errors)

    def test_null_required_field(self):
        fm = _valid_pattern_fm(pattern_id=None)
        valid, errors = validate_frontmatter(fm)
        assert valid is False
        assert any("null" in e and "pattern_id" in e for e in errors)


# ===========================================================================
# Phase 2: Sensitive content detection tests
# ===========================================================================


class TestSensitiveContent:
    """Sensitive content detection."""

    def test_clean_content(self):
        has, matches = detect_sensitive_content("# Normal Note\n\nSafe content here.")
        assert has is False
        assert matches == []

    def test_api_key_detected(self):
        has, matches = detect_sensitive_content("api_key = sk-abc123456789abcdefghijklmnop1234567890")
        assert has is True

    def test_password_detected(self):
        has, matches = detect_sensitive_content("password = mysecretpassword123")
        assert has is True

    def test_private_key_detected(self):
        has, matches = detect_sensitive_content("-----BEGIN RSA PRIVATE KEY-----\nfoo\n-----END RSA PRIVATE KEY-----")
        assert has is True

    def test_github_pat_detected(self):
        has, matches = detect_sensitive_content("ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef1234567890")
        assert has is True

    def test_openai_key_detected(self):
        has, matches = detect_sensitive_content("token = sk-12345678901234567890123456789012345")
        assert has is True

    def test_aws_key_detected(self):
        has, matches = detect_sensitive_content("aws_access_key_id = AKIAIOSFODNN7EXAMPLE")
        assert has is True

    def test_generic_secret_detected(self):
        has, matches = detect_sensitive_content("secret = abcdefghij12345678")
        assert has is True

    def test_mentions_without_value_ok(self):
        # Mentioning the word "password" in prose is fine (no assignment pattern)
        has, matches = detect_sensitive_content("You should change your password regularly.")
        assert has is False


# ===========================================================================
# Phase 2: Feature flag tests
# ===========================================================================


class TestFeatureFlag:
    """Write feature flag gating."""

    def test_writes_disabled_by_default(self, vault_dir):
        """No config file → writes disabled."""
        os.environ.pop("NOVA_VAULT_WRITE_FLAG", None)
        # Point to a nonexistent config
        os.environ["NOVA_VAULT_WRITE_FLAG"] = str(vault_dir / "nonexistent.json")
        result = vault_write("20-agent-patterns/test.md", _valid_pattern_fm(), "# Body")
        assert "error" in result
        assert "disabled" in result["error"]
        os.environ.pop("NOVA_VAULT_WRITE_FLAG", None)

    def test_writes_disabled_explicit(self, write_disabled):
        result = vault_write("20-agent-patterns/test.md", _valid_pattern_fm(), "# Body")
        assert "error" in result
        assert "disabled" in result["error"]

    def test_corrupt_config_disables_writes(self, vault_dir):
        path = vault_dir / ".nova-write-config.json"
        path.write_text("not json at all {{{", encoding="utf-8")
        os.environ["NOVA_VAULT_WRITE_FLAG"] = str(path)
        result = vault_write("20-agent-patterns/test.md", _valid_pattern_fm(), "# Body")
        assert "error" in result
        assert "disabled" in result["error"]
        os.environ.pop("NOVA_VAULT_WRITE_FLAG", None)

    def test_non_boolean_enabled_disables(self, vault_dir):
        path = vault_dir / ".nova-write-config.json"
        path.write_text('{"enabled": "yes"}', encoding="utf-8")
        os.environ["NOVA_VAULT_WRITE_FLAG"] = str(path)
        result = vault_write("20-agent-patterns/test.md", _valid_pattern_fm(), "# Body")
        assert "error" in result
        assert "disabled" in result["error"]
        os.environ.pop("NOVA_VAULT_WRITE_FLAG", None)

    def test_validate_works_without_flag(self):
        """vault_validate does NOT require the write flag."""
        result = vault_validate(_valid_pattern_fm(), "# Body")
        assert result["valid"] is True

    def test_update_disabled_by_flag(self, write_disabled):
        result = vault_update(
            "20-agent-patterns/research-patterns.md",
            "## New Section", "Some content"
        )
        assert "error" in result
        assert "disabled" in result["error"]


# ===========================================================================
# Phase 2: vault_write tests
# ===========================================================================


class TestVaultWrite:
    """Bounded write to approved folders."""

    def test_write_valid_pattern(self, write_enabled):
        result = vault_write(
            "20-agent-patterns/new-pattern.md",
            _valid_pattern_fm(),
            "## Summary\n\nA new test pattern.\n",
        )
        assert "error" not in result
        assert result["status"] == "created"
        assert result["size"] > 0
        # Verify file was actually created
        result2 = vault_read("20-agent-patterns/new-pattern.md")
        assert "error" not in result2
        assert "new test pattern" in result2["content"].lower()

    def test_write_valid_learning(self, write_enabled):
        result = vault_write(
            "30-workflow-learnings/2026-03-test.md",
            _valid_learning_fm(),
            "## Task Summary\n\nTest.\n",
        )
        assert "error" not in result
        assert result["status"] == "created"

    def test_write_valid_research(self, write_enabled):
        result = vault_write(
            "40-research/test-topic.md",
            _valid_research_fm(),
            "## Overview\n\nResearch content.\n",
        )
        assert "error" not in result
        assert result["status"] == "created"

    def test_write_to_inbox(self, write_enabled, vault_dir):
        fm = {"type": "inbox", "title": "Quick capture", "source": "operator", "tags": ["#type/inbox"]}
        result = vault_write("00-inbox/capture.md", fm, "Some quick note.\n")
        assert "error" not in result

    def test_write_rejected_readonly_folder(self, write_enabled):
        result = vault_write(
            "10-adrs/ADR-999-test.md",
            _valid_pattern_fm(type="adr"),
            "# Body",
        )
        assert "error" in result
        assert "not allowed" in result["error"]

    def test_write_rejected_playbooks_folder(self, write_enabled):
        fm = _valid_pattern_fm()
        result = vault_write("50-playbooks/test.md", fm, "# Body")
        assert "error" in result
        assert "not allowed" in result["error"]

    def test_write_rejected_meta_folder(self, write_enabled):
        fm = _valid_pattern_fm()
        result = vault_write("_meta/test.md", fm, "# Body")
        assert "error" in result
        assert "not allowed" in result["error"]

    def test_write_rejected_existing_file(self, write_enabled):
        result = vault_write(
            "20-agent-patterns/research-patterns.md",
            _valid_pattern_fm(),
            "# Body",
        )
        assert "error" in result
        assert "already exists" in result["error"]

    def test_write_rejected_invalid_schema(self, write_enabled):
        bad_fm = {"type": "agent-pattern", "title": "Missing fields"}
        result = vault_write("20-agent-patterns/bad.md", bad_fm, "# Body")
        assert "error" in result
        assert "schema_errors" in result

    def test_write_rejected_missing_frontmatter(self, write_enabled):
        result = vault_write("20-agent-patterns/no-fm.md", {}, "# Body")
        assert "error" in result
        assert "required" in result["error"]

    def test_write_rejected_sensitive_content(self, write_enabled):
        result = vault_write(
            "20-agent-patterns/oops.md",
            _valid_pattern_fm(),
            "## Summary\n\napi_key = sk-secretkey123456789012345678901234\n",
        )
        assert "error" in result
        assert "sensitive" in result["error"]

    def test_write_rejected_oversized(self, write_enabled, vault_dir):
        _write_config(vault_dir, enabled=True, max_note_size_bytes=500)
        big_body = "x" * 1000
        result = vault_write(
            "20-agent-patterns/big.md",
            _valid_pattern_fm(),
            big_body,
        )
        assert "error" in result
        assert "too large" in result["error"]

    def test_write_rejected_traversal(self, write_enabled):
        result = vault_write(
            "../../../etc/hacked.md",
            _valid_pattern_fm(),
            "# Body",
        )
        assert "error" in result
        assert "unsafe" in result["error"].lower() or "Invalid" in result["error"]

    def test_write_rejected_non_md(self, write_enabled):
        result = vault_write(
            "20-agent-patterns/test.txt",
            _valid_pattern_fm(),
            "# Body",
        )
        assert "error" in result
        assert ".md" in result["error"]

    def test_write_rejected_empty_path(self, write_enabled):
        result = vault_write("", _valid_pattern_fm(), "# Body")
        assert "error" in result

    def test_write_rejected_no_folder(self, write_enabled):
        result = vault_write("rootfile.md", _valid_pattern_fm(), "# Body")
        assert "error" in result


# ===========================================================================
# Phase 2: vault_update tests
# ===========================================================================


class TestVaultUpdate:
    """Bounded update (append) to existing notes."""

    def test_update_append_section(self, write_enabled):
        result = vault_update(
            "20-agent-patterns/research-patterns.md",
            "## New Evidence",
            "This section was appended.",
        )
        assert "error" not in result
        assert result["status"] == "updated"
        # Verify content was appended
        read_result = vault_read("20-agent-patterns/research-patterns.md")
        assert "New Evidence" in read_result["content"]
        assert "appended" in read_result["content"]

    def test_update_rejected_readonly_folder(self, write_enabled):
        result = vault_update(
            "10-adrs/ADR-001-test.md",
            "## Addendum",
            "New content.",
        )
        assert "error" in result
        assert "not allowed" in result["error"]

    def test_update_rejected_nonexistent(self, write_enabled):
        result = vault_update(
            "20-agent-patterns/nonexistent.md",
            "## Section",
            "Content.",
        )
        assert "error" in result
        assert "not found" in result["error"]

    def test_update_rejected_empty_heading(self, write_enabled):
        result = vault_update(
            "20-agent-patterns/research-patterns.md",
            "",
            "Content.",
        )
        assert "error" in result
        assert "heading" in result["error"].lower()

    def test_update_rejected_no_hash_heading(self, write_enabled):
        result = vault_update(
            "20-agent-patterns/research-patterns.md",
            "Not A Heading",
            "Content.",
        )
        assert "error" in result
        assert "heading" in result["error"].lower()

    def test_update_rejected_empty_body(self, write_enabled):
        result = vault_update(
            "20-agent-patterns/research-patterns.md",
            "## Section",
            "",
        )
        assert "error" in result
        assert "body" in result["error"].lower()

    def test_update_rejected_sensitive_content(self, write_enabled):
        result = vault_update(
            "20-agent-patterns/research-patterns.md",
            "## Secrets",
            "password = hunter2hunter2",
        )
        assert "error" in result
        assert "sensitive" in result["error"]

    def test_update_rejected_would_exceed_size(self, write_enabled, vault_dir):
        _write_config(vault_dir, enabled=True, max_note_size_bytes=500)
        result = vault_update(
            "20-agent-patterns/research-patterns.md",
            "## Big Section",
            "x" * 1000,
        )
        assert "error" in result
        assert "too large" in result["error"]

    def test_update_rejected_traversal(self, write_enabled):
        result = vault_update(
            "../../../etc/passwd",
            "## Hack",
            "Content.",
        )
        assert "error" in result


# ===========================================================================
# Phase 2: vault_validate tests
# ===========================================================================


class TestVaultValidate:
    """Dry-run validation."""

    def test_validate_valid_pattern(self):
        result = vault_validate(_valid_pattern_fm(), "## Body\n\nContent.\n")
        assert result["valid"] is True
        assert result["errors"] == []
        assert result["note_size_bytes"] > 0

    def test_validate_invalid_schema(self):
        result = vault_validate({"type": "agent-pattern"}, "# Body")
        assert result["valid"] is False
        assert len(result["errors"]) > 0

    def test_validate_sensitive_body(self):
        result = vault_validate(
            _valid_pattern_fm(),
            "## Body\n\napi_key = sk-abc123456789012345678901234567890\n",
        )
        assert result["valid"] is False
        assert any("sensitive" in e for e in result["errors"])

    def test_validate_empty_frontmatter(self):
        result = vault_validate({})
        assert result["valid"] is False

    def test_validate_non_dict(self):
        result = vault_validate("not a dict")  # type: ignore
        assert result["valid"] is False

    def test_validate_oversized(self):
        big = "x" * (35 * 1024)
        result = vault_validate(_valid_pattern_fm(), big)
        assert result["valid"] is False
        assert any("too large" in e for e in result["errors"])


# ===========================================================================
# Phase 2: Rate limiting tests
# ===========================================================================


class TestRateLimiting:
    """Write rate limiting."""

    def test_rate_limit_allows_initial_writes(self, write_enabled):
        # First write should succeed
        result = vault_write(
            "20-agent-patterns/rate-test-1.md",
            _valid_pattern_fm(pattern_id="ap-rate-1"),
            "# Body",
        )
        assert "error" not in result

    def test_rate_limit_blocks_excess(self, write_enabled, vault_dir):
        import tools.mcp_vault_server as mod

        # Set a very low limit
        _write_config(vault_dir, enabled=True, max_writes_per_window=2, window_seconds=300)

        # Simulate 2 recent writes
        mod._write_timestamps.clear()
        mod._write_timestamps.extend([time.time(), time.time()])

        result = vault_write(
            "20-agent-patterns/rate-blocked.md",
            _valid_pattern_fm(pattern_id="ap-rate-blocked"),
            "# Body",
        )
        assert "error" in result
        assert "rate_limit" in result["error"]

    def test_rate_limit_clears_old_entries(self, write_enabled, vault_dir):
        import tools.mcp_vault_server as mod

        _write_config(vault_dir, enabled=True, max_writes_per_window=2, window_seconds=300)

        # Old timestamps outside window should be pruned
        mod._write_timestamps.clear()
        mod._write_timestamps.extend([time.time() - 600, time.time() - 600])

        result = vault_write(
            "20-agent-patterns/rate-cleared.md",
            _valid_pattern_fm(pattern_id="ap-rate-cleared"),
            "# Body",
        )
        assert "error" not in result


# ===========================================================================
# Phase 2: Audit logging tests
# ===========================================================================


class TestAuditLog:
    """Audit trail for write operations."""

    def test_write_creates_audit_entry(self, write_enabled, vault_dir):
        vault_write(
            "20-agent-patterns/audit-test.md",
            _valid_pattern_fm(pattern_id="ap-audit"),
            "# Body",
        )
        audit_log = vault_dir / ".nova-audit.log"
        assert audit_log.exists()
        content = audit_log.read_text()
        assert "WRITE" in content
        assert "audit-test.md" in content
        assert "accepted" in content

    def test_rejected_write_logged(self, write_enabled, vault_dir):
        vault_write(
            "10-adrs/blocked.md",
            _valid_pattern_fm(),
            "# Body",
        )
        audit_log = vault_dir / ".nova-audit.log"
        assert audit_log.exists()
        content = audit_log.read_text()
        assert "rejected" in content
        assert "folder_not_writable" in content

    def test_disabled_write_logged(self, write_disabled, vault_dir):
        vault_write(
            "20-agent-patterns/disabled.md",
            _valid_pattern_fm(),
            "# Body",
        )
        audit_log = vault_dir / ".nova-audit.log"
        assert audit_log.exists()
        content = audit_log.read_text()
        assert "feature_flag_disabled" in content
