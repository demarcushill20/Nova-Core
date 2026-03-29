"""Tests for skills.skill_patch and skills.skill_evolver.

Phases 3-5 of Self-Evolving Skills — tests covering:
- PatchError / GovernanceError exceptions
- apply_patch with FULL rewrite and SEARCH/REPLACE formats
- 4-level fuzzy matching (exact, trimmed, whitespace-normalized, indentation-flexible)
- Governance checking (forbidden targets, allowed modifications)
- SkillEvolver.evolve_fix lifecycle (creation, lineage, snapshots)
- SkillEvolver.evolve_derived lifecycle (derivation, parent stays active, custom names)
- detect_improvement_candidates (health-based filtering)
- Generation cap, cooldown, anti-loop guards
- Validation (name preservation, empty content, malformed frontmatter)
- Retry on PatchError / GovernanceError
- Prompt building and failure/success history formatting
- Mutation policy loading
- AUTO-LEARN: evolve_captured lifecycle, rate limiting, duplicate detection
- detect_novel_patterns for skillless successful tasks
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from skills.skill_patch import (
    GovernanceError,
    PatchError,
    _apply_indent,
    _apply_search_replace,
    _detect_indent,
    _fuzzy_replace,
    _indentation_flexible_replace,
    _is_search_replace,
    _map_normalized_index,
    _trimmed_replace,
    _whitespace_normalized_replace,
    apply_patch,
    check_governance,
)
from skills.skill_record import (
    ExecutionStats,
    Origin,
    SkillLineage,
    SkillVersion,
    generate_skill_id,
)
from skills.version_store import SkillVersionStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_skill(
    name: str = "test-skill",
    origin: Origin = Origin.IMPORTED,
    generation: int = 0,
    parent_ids: list[str] | None = None,
    is_active: bool = True,
    **kwargs,
) -> SkillVersion:
    """Create a SkillVersion with sensible defaults for testing."""
    skill_id = kwargs.pop("skill_id", generate_skill_id(name, origin, generation))
    return SkillVersion(
        skill_id=skill_id,
        name=name,
        description=kwargs.pop("description", f"Test skill: {name}"),
        path=kwargs.pop("path", f"SKILLS/{name}"),
        is_active=is_active,
        version=kwargs.pop("version", "1.0.0"),
        lineage=SkillLineage(
            origin=origin,
            generation=generation,
            parent_ids=parent_ids or [],
            change_summary=kwargs.pop("change_summary", None),
        ),
        stats=ExecutionStats(
            selections=kwargs.pop("selections", 0),
            executions=kwargs.pop("executions", 0),
            completions=kwargs.pop("completions", 0),
            failures=kwargs.pop("failures", 0),
            fallbacks=kwargs.pop("fallbacks", 0),
        ),
        created_by=kwargs.pop("created_by", "test"),
    )


SAMPLE_SKILL_MD = """---
name: test-skill
description: "A test skill for unit testing."
activation:
  keywords: [test, unit]
tool_doctrine:
  test:
    workflow:
      - step_one
      - step_two
output_contract:
  required:
    - result
    - verification
---

# Test Skill

This skill does testing things.

## Steps

1. Run tests
2. Report results
"""


@pytest.fixture
def store(tmp_path: Path) -> SkillVersionStore:
    """Return a SkillVersionStore backed by a temp database."""
    return SkillVersionStore(db_path=tmp_path / "test_evolver.db")


@pytest.fixture
def skill_dir(tmp_path: Path) -> Path:
    """Create a skill directory with sample SKILL.md."""
    d = tmp_path / "test-skill"
    d.mkdir()
    (d / "SKILL.md").write_text(SAMPLE_SKILL_MD)
    (d / "metadata.json").write_text('{"name": "test-skill"}')
    return d


# ===========================================================================
# TestSkillPatch — patch application and fuzzy matching
# ===========================================================================


class TestSkillPatch:
    """Tests for skills.skill_patch module."""

    def test_apply_patch_full_rewrite(self, skill_dir: Path):
        """Full rewrite: the patch IS the new content."""
        new_content = (
            "---\nname: test-skill\n---\nNew body with enough content to pass the minimum length validation check."
        )
        result = apply_patch(
            str(skill_dir / "SKILL.md"),
            new_content,
        )
        assert result == new_content

    def test_apply_patch_search_replace(self, skill_dir: Path):
        """SEARCH/REPLACE format is auto-detected and applied."""
        patch_text = (
            "<<<<<<< SEARCH\n"
            "This skill does testing things.\n"
            "=======\n"
            "This skill does amazing testing things.\n"
            ">>>>>>> REPLACE"
        )
        result = apply_patch(str(skill_dir / "SKILL.md"), patch_text)
        assert "amazing testing things" in result
        assert "This skill does testing things." not in result

    def test_is_search_replace_true(self):
        """Detect SEARCH/REPLACE format."""
        text = "<<<<<<< SEARCH\nfoo\n=======\nbar\n>>>>>>> REPLACE"
        assert _is_search_replace(text) is True

    def test_is_search_replace_false(self):
        """Plain text is not SEARCH/REPLACE."""
        assert _is_search_replace("hello world") is False
        assert _is_search_replace("<<<<<<< SEARCH only") is False
        assert _is_search_replace(">>>>>>> REPLACE only") is False

    def test_exact_match_level_1(self):
        """Level 1: Exact string match."""
        content = "hello world foo bar"
        result = _fuzzy_replace(content, "foo", "baz")
        assert result == "hello world baz bar"

    def test_trimmed_match_level_2(self):
        """Level 2: Match ignoring leading/trailing whitespace per line."""
        content = "  hello  \n  world  "
        search = "hello\nworld"
        replace = "goodbye\nearth"
        result = _trimmed_replace(content, search, replace)
        assert result is not None
        assert "goodbye" in result
        assert "earth" in result

    def test_trimmed_match_no_match(self):
        """Level 2: Returns None when no trimmed match found."""
        content = "alpha\nbeta"
        result = _trimmed_replace(content, "gamma\ndelta", "x\ny")
        assert result is None

    def test_whitespace_normalized_match_level_3(self):
        """Level 3: Match with collapsed whitespace."""
        content = "hello   world\n  foo   bar"
        search = "hello world foo bar"
        replace = "replaced"
        result = _whitespace_normalized_replace(content, search, replace)
        assert result is not None
        assert "replaced" in result

    def test_whitespace_normalized_no_match(self):
        """Level 3: Returns None when no normalized match found."""
        result = _whitespace_normalized_replace("alpha beta", "gamma delta", "x")
        assert result is None

    def test_indentation_flexible_match_level_4(self):
        """Level 4: Match ignoring indentation differences."""
        content = "    def foo():\n        return 1\n    end"
        search = "def foo():\n    return 1\nend"
        replace = "def foo():\n    return 2\nend"
        result = _indentation_flexible_replace(content, search, replace)
        assert result is not None
        assert "return 2" in result

    def test_indentation_flexible_no_match(self):
        """Level 4: Returns None when similarity is below 85%."""
        content = "completely different content here"
        search = "nothing at all matches\nthis block\nof text"
        result = _indentation_flexible_replace(content, search, "x")
        assert result is None

    def test_patch_error_no_match_all_levels(self):
        """PatchError raised when all 4 fuzzy levels fail."""
        content = "alpha beta gamma"
        with pytest.raises(PatchError, match="Could not match"):
            _fuzzy_replace(content, "totally\nunrelated\ncontent", "x")

    def test_governance_check_blocks_forbidden(self, skill_dir: Path):
        """GovernanceError raised when patch modifies forbidden section."""
        # Write content with output_contract
        (skill_dir / "SKILL.md").write_text("---\noutput_contract: old\n---\nbody content here")
        # Patch that changes output_contract
        new_content = "---\noutput_contract: MODIFIED\n---\nbody content here"

        with pytest.raises(GovernanceError, match="Governance violations"):
            apply_patch(
                str(skill_dir / "SKILL.md"),
                new_content,
                governance_check=lambda o, n: check_governance(o, n, ["output_contract"]),
            )

    def test_governance_allows_non_forbidden(self):
        """Governance check allows modifications to non-forbidden sections."""
        old = "---\ndescription: old desc\n---\nbody"
        new = "---\ndescription: new desc\n---\nbody"
        violations = check_governance(old, new, ["output_contract"])
        assert violations == []

    def test_governance_no_forbidden_targets(self):
        """Governance check with no forbidden targets returns no violations."""
        violations = check_governance("old", "new", [])
        assert violations == []

    def test_governance_none_forbidden_targets(self):
        """Governance check with None forbidden targets returns no violations."""
        violations = check_governance("old", "new", None)
        assert violations == []

    def test_empty_patch_full_rewrite_rejected(self, skill_dir: Path):
        """Empty patch text is rejected as too short for full rewrite."""
        with pytest.raises(PatchError, match="too short"):
            apply_patch(str(skill_dir / "SKILL.md"), "")

    def test_multiple_search_replace_blocks(self, skill_dir: Path):
        """Multiple SEARCH/REPLACE blocks applied sequentially."""
        patch_text = (
            "<<<<<<< SEARCH\n"
            "This skill does testing things.\n"
            "=======\n"
            "This skill does improved things.\n"
            ">>>>>>> REPLACE\n\n"
            "<<<<<<< SEARCH\n"
            "1. Run tests\n"
            "=======\n"
            "1. Run improved tests\n"
            ">>>>>>> REPLACE"
        )
        result = apply_patch(str(skill_dir / "SKILL.md"), patch_text)
        assert "improved things" in result
        assert "improved tests" in result

    def test_apply_patch_preserves_non_matched_content(self, skill_dir: Path):
        """SEARCH/REPLACE only modifies matched portions."""
        patch_text = (
            "<<<<<<< SEARCH\nThis skill does testing things.\n=======\nThis skill does patched things.\n>>>>>>> REPLACE"
        )
        result = apply_patch(str(skill_dir / "SKILL.md"), patch_text)
        # Non-matched content preserved
        assert "name: test-skill" in result
        assert "2. Report results" in result
        # Matched content changed
        assert "patched things" in result

    def test_search_replace_no_valid_blocks(self):
        """PatchError for SEARCH/REPLACE markers but no valid blocks."""
        content = "hello"
        # Has markers but no proper block
        patch = "<<<<<<< SEARCH\n>>>>>>> REPLACE"
        with pytest.raises(PatchError, match="No valid SEARCH/REPLACE"):
            _apply_search_replace(content, patch)

    def test_map_normalized_index_basic(self):
        """Map from normalized index back to original."""
        original = "hello   world"
        # normalized: "hello world" => index 0 = 'h'
        idx = _map_normalized_index(original, 0)
        assert idx is not None
        assert idx == 0
        assert original[idx] == "h"
        # normalized index 5 => the space (maps to first space in original)
        idx5 = _map_normalized_index(original, 5)
        assert idx5 is not None
        assert original[idx5] == " "

    def test_map_normalized_index_end(self):
        """Map normalized index at end of string."""
        original = "ab"
        idx = _map_normalized_index(original, 2)
        assert idx == 2  # len(original)

    def test_map_normalized_index_beyond(self):
        """Map normalized index beyond string returns None."""
        original = "ab"
        idx = _map_normalized_index(original, 99)
        assert idx is None

    def test_detect_indent_empty_lines(self):
        """Detect indent with all empty lines returns empty string."""
        assert _detect_indent(["", "", ""]) == ""

    def test_detect_indent_mixed(self):
        """Detect minimum indent from mixed block."""
        lines = ["    line1", "        line2", "    line3"]
        assert _detect_indent(lines) == "    "

    def test_apply_indent_empty(self):
        """Apply indent to empty list returns empty."""
        assert _apply_indent([], "    ") == []

    def test_apply_indent_preserves_relative(self):
        """Apply indent preserves relative indentation."""
        lines = ["def foo():", "    return 1"]
        result = _apply_indent(lines, "    ")
        assert result[0] == "    def foo():"
        assert result[1] == "        return 1"

    def test_apply_indent_blank_lines(self):
        """Blank lines become empty strings."""
        lines = ["hello", "", "world"]
        result = _apply_indent(lines, "  ")
        assert result[1] == ""

    def test_indentation_flexible_empty_search(self):
        """Empty search lines does not crash — returns a string or None."""
        result = _indentation_flexible_replace("content", "", "replace")
        # Empty string splits to [""] which has length 1 — the key behavior
        # is it doesn't crash. It may match trivially or return None.
        assert result is None or isinstance(result, str)


# ===========================================================================
# TestSkillEvolver — evolution lifecycle
# ===========================================================================


class TestSkillEvolver:
    """Tests for skills.skill_evolver.SkillEvolver."""

    def _make_evolver(self, store: SkillVersionStore, tmp_path: Path):
        """Create a SkillEvolver with a test mutation policy."""
        from skills.skill_evolver import SkillEvolver

        policy_path = tmp_path / "mutation_policy.yaml"
        policy_path.write_text(
            "evolution_policy:\n"
            "  forbidden_targets:\n"
            "    - tool_permissions\n"
            "    - output_contract\n"
            "    - safety_rules\n"
        )
        return SkillEvolver(
            version_store=store,
            mutation_policy_path=str(policy_path),
            skills_base_dir=str(tmp_path),
        )

    def test_evolve_fix_creates_new_version(self, store: SkillVersionStore, skill_dir: Path, tmp_path: Path):
        """evolve_fix creates a new SkillVersion with correct lineage."""

        parent = _make_skill(
            name="test-skill",
            skill_id="parent_fix_1",
            path=str(skill_dir),
        )
        store.register_skill(parent)

        evolver = self._make_evolver(store, tmp_path)

        # Mock LLM to return a valid full rewrite
        fixed_content = SAMPLE_SKILL_MD.replace(
            "This skill does testing things.",
            "This skill does fixed things.",
        )
        with patch.object(evolver, "_call_llm", return_value=fixed_content):
            result = evolver.evolve_fix(
                skill_id="parent_fix_1",
                direction="Fix the test issue",
                task_id="task_001",
            )

        assert result is not None
        assert result.lineage.origin == Origin.FIXED
        assert result.lineage.generation == 1
        assert result.lineage.parent_ids == ["parent_fix_1"]
        assert result.created_by == "auto_fix"
        assert "AUTO-FIX" in result.lineage.change_summary

    def test_evolve_fix_respects_generation_cap(self, store: SkillVersionStore, skill_dir: Path, tmp_path: Path):
        """evolve_fix returns None when skill is at max generation."""
        from skills.skill_evolver import MAX_GENERATION

        parent = _make_skill(
            name="test-skill",
            skill_id="gen_cap_1",
            path=str(skill_dir),
            generation=MAX_GENERATION,
            origin=Origin.FIXED,
        )
        store.register_skill(parent)
        evolver = self._make_evolver(store, tmp_path)

        result = evolver.evolve_fix(
            skill_id="gen_cap_1",
            direction="Attempt fix beyond cap",
        )
        assert result is None

    def test_evolve_fix_respects_cooldown(self, store: SkillVersionStore, skill_dir: Path, tmp_path: Path):
        """evolve_fix returns None when skill is in cooldown."""

        parent = _make_skill(
            name="test-skill",
            skill_id="cool_1",
            path=str(skill_dir),
        )
        store.register_skill(parent)
        evolver = self._make_evolver(store, tmp_path)

        # Simulate recent evolution
        evolver._last_evolution_time["cool_1"] = time.time()

        result = evolver.evolve_fix(
            skill_id="cool_1",
            direction="Fix during cooldown",
        )
        assert result is None

    def test_evolve_fix_anti_loop(self, store: SkillVersionStore, skill_dir: Path, tmp_path: Path):
        """evolve_fix prevents same task from triggering fix twice."""

        parent = _make_skill(
            name="test-skill",
            skill_id="loop_1",
            path=str(skill_dir),
        )
        store.register_skill(parent)
        evolver = self._make_evolver(store, tmp_path)

        # Simulate already fixed for this task
        evolver._evolution_history["loop_1"] = "task_repeat"

        result = evolver.evolve_fix(
            skill_id="loop_1",
            direction="Fix again",
            task_id="task_repeat",
        )
        assert result is None

    def test_evolve_fix_missing_skill(self, store: SkillVersionStore, tmp_path: Path):
        """evolve_fix returns None for non-existent skill."""

        evolver = self._make_evolver(store, tmp_path)
        result = evolver.evolve_fix(
            skill_id="nonexistent",
            direction="Fix something",
        )
        assert result is None

    def test_evolve_fix_missing_directory(self, store: SkillVersionStore, tmp_path: Path):
        """evolve_fix returns None when skill directory doesn't exist."""

        parent = _make_skill(
            name="test-skill",
            skill_id="nodir_1",
            path=str(tmp_path / "nonexistent_dir"),
        )
        store.register_skill(parent)
        evolver = self._make_evolver(store, tmp_path)

        result = evolver.evolve_fix(
            skill_id="nodir_1",
            direction="Fix missing dir",
        )
        assert result is None

    def test_evolve_fix_saves_pre_snapshot(self, store: SkillVersionStore, skill_dir: Path, tmp_path: Path):
        """evolve_fix saves a pre-evolution snapshot."""

        parent = _make_skill(
            name="test-skill",
            skill_id="snap_pre_1",
            path=str(skill_dir),
        )
        store.register_skill(parent)
        evolver = self._make_evolver(store, tmp_path)

        fixed = SAMPLE_SKILL_MD.replace("testing things", "snapshot things")
        with patch.object(evolver, "_call_llm", return_value=fixed):
            evolver.evolve_fix(
                skill_id="snap_pre_1",
                direction="Fix for snapshot test",
            )

        # Pre-evolution snapshot should exist for parent
        snapshot = store.get_snapshot("snap_pre_1")
        assert snapshot is not None
        assert "SKILL.md" in snapshot

    def test_evolve_fix_saves_post_snapshot(self, store: SkillVersionStore, skill_dir: Path, tmp_path: Path):
        """evolve_fix saves a post-evolution snapshot for the new version."""

        parent = _make_skill(
            name="test-skill",
            skill_id="snap_post_1",
            path=str(skill_dir),
        )
        store.register_skill(parent)
        evolver = self._make_evolver(store, tmp_path)

        fixed = SAMPLE_SKILL_MD.replace("testing things", "post-snapshot things")
        with patch.object(evolver, "_call_llm", return_value=fixed):
            result = evolver.evolve_fix(
                skill_id="snap_post_1",
                direction="Fix for post-snapshot",
            )

        assert result is not None
        post_snapshot = store.get_snapshot(result.skill_id)
        assert post_snapshot is not None
        assert "SKILL.md" in post_snapshot

    def test_evolve_fix_validation_name_change(self, store: SkillVersionStore, skill_dir: Path, tmp_path: Path):
        """evolve_fix rejects patches that change the skill name."""

        parent = _make_skill(
            name="test-skill",
            skill_id="namechg_1",
            path=str(skill_dir),
        )
        store.register_skill(parent)
        evolver = self._make_evolver(store, tmp_path)

        # Return content with changed name — all 3 attempts
        bad_content = SAMPLE_SKILL_MD.replace("name: test-skill", "name: renamed-skill")
        with patch.object(evolver, "_call_llm", return_value=bad_content):
            result = evolver.evolve_fix(
                skill_id="namechg_1",
                direction="Should fail validation",
            )

        # Should fail because name was changed
        assert result is None

    def test_evolve_fix_validation_empty_content(self, store: SkillVersionStore, skill_dir: Path, tmp_path: Path):
        """evolve_fix rejects empty content."""

        parent = _make_skill(
            name="test-skill",
            skill_id="empty_1",
            path=str(skill_dir),
        )
        store.register_skill(parent)
        evolver = self._make_evolver(store, tmp_path)

        with patch.object(evolver, "_call_llm", return_value=""):
            result = evolver.evolve_fix(
                skill_id="empty_1",
                direction="Should fail with empty",
            )

        assert result is None

    def test_evolve_fix_validation_malformed_frontmatter(
        self, store: SkillVersionStore, skill_dir: Path, tmp_path: Path
    ):
        """evolve_fix rejects malformed frontmatter."""

        parent = _make_skill(
            name="test-skill",
            skill_id="malform_1",
            path=str(skill_dir),
        )
        store.register_skill(parent)
        evolver = self._make_evolver(store, tmp_path)

        # Only one --- delimiter
        bad_content = "---\nname: test-skill\nno closing delimiter"
        with patch.object(evolver, "_call_llm", return_value=bad_content):
            result = evolver.evolve_fix(
                skill_id="malform_1",
                direction="Malformed frontmatter",
            )

        assert result is None

    def test_evolve_fix_retry_on_patch_error(self, store: SkillVersionStore, skill_dir: Path, tmp_path: Path):
        """evolve_fix retries when PatchError occurs."""

        parent = _make_skill(
            name="test-skill",
            skill_id="patcherr_1",
            path=str(skill_dir),
        )
        store.register_skill(parent)
        evolver = self._make_evolver(store, tmp_path)

        # First call: invalid SEARCH/REPLACE, second: valid full rewrite
        bad_patch = "<<<<<<< SEARCH\nnonexistent content xyz\n=======\nreplacement\n>>>>>>> REPLACE"
        good_content = SAMPLE_SKILL_MD.replace("testing things", "retried things")
        call_count = 0

        def mock_llm(prompt):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return bad_patch
            return good_content

        with patch.object(evolver, "_call_llm", side_effect=mock_llm):
            result = evolver.evolve_fix(
                skill_id="patcherr_1",
                direction="Test retry",
            )

        assert result is not None
        assert call_count == 2

    def test_evolve_fix_retry_on_governance_error(self, store: SkillVersionStore, skill_dir: Path, tmp_path: Path):
        """evolve_fix retries when GovernanceError occurs."""

        parent = _make_skill(
            name="test-skill",
            skill_id="goverr_1",
            path=str(skill_dir),
        )
        store.register_skill(parent)
        evolver = self._make_evolver(store, tmp_path)

        # First: modifies output_contract (forbidden), second: clean
        bad_content = SAMPLE_SKILL_MD.replace("output_contract:", "output_contract: MODIFIED")
        good_content = SAMPLE_SKILL_MD.replace("testing things", "governance-clean things")
        call_count = 0

        def mock_llm(prompt):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return bad_content
            return good_content

        with patch.object(evolver, "_call_llm", side_effect=mock_llm):
            evolver.evolve_fix(
                skill_id="goverr_1",
                direction="Test governance retry",
            )

        # May succeed on second attempt if governance passes
        # The key is that it retries rather than crashing
        assert call_count >= 2

    def test_evolve_fix_returns_none_after_all_retries(self, store: SkillVersionStore, skill_dir: Path, tmp_path: Path):
        """evolve_fix returns None when all retry attempts fail."""
        from skills.skill_evolver import MAX_FIX_ATTEMPTS

        parent = _make_skill(
            name="test-skill",
            skill_id="allretry_1",
            path=str(skill_dir),
        )
        store.register_skill(parent)
        evolver = self._make_evolver(store, tmp_path)

        # Always return content with changed name
        bad_content = SAMPLE_SKILL_MD.replace("name: test-skill", "name: bad-name")
        call_count = 0

        def mock_llm(prompt):
            nonlocal call_count
            call_count += 1
            return bad_content

        with patch.object(evolver, "_call_llm", side_effect=mock_llm):
            result = evolver.evolve_fix(
                skill_id="allretry_1",
                direction="All retries fail",
            )

        assert result is None
        assert call_count == MAX_FIX_ATTEMPTS

    def test_build_fix_prompt_includes_context(self, tmp_path: Path):
        """_build_fix_prompt includes all relevant context."""

        store = SkillVersionStore(db_path=tmp_path / "prompt_test.db")
        evolver = self._make_evolver(store, tmp_path)

        prompt = evolver._build_fix_prompt(
            skill_name="my-skill",
            skill_content={
                "SKILL.md": "# My Skill",
                "metadata.json": '{"name": "my-skill"}',
            },
            direction="Fix the error handling",
            failure_context="TypeError on line 42",
            failure_history="- [FAILED] quality=0.2: bad",
            forbidden_targets=["output_contract", "safety_rules"],
        )

        assert "my-skill" in prompt
        assert "# My Skill" in prompt
        assert "Fix the error handling" in prompt
        assert "TypeError on line 42" in prompt
        assert "quality=0.2" in prompt
        assert "output_contract" in prompt
        assert "safety_rules" in prompt

    def test_build_retry_prompt_includes_errors(self, tmp_path: Path):
        """_build_retry_prompt includes previous errors."""

        store = SkillVersionStore(db_path=tmp_path / "retry_test.db")
        evolver = self._make_evolver(store, tmp_path)

        prompt = evolver._build_retry_prompt(
            original_prompt="Original prompt here",
            failed_patch="bad patch content",
            errors=["Name changed", "Content too short"],
        )

        assert "Previous attempt failed" in prompt
        assert "bad patch content" in prompt
        assert "Name changed" in prompt
        assert "Content too short" in prompt

    def test_format_failure_history(self, tmp_path: Path):
        """_format_failure_history formats analyses correctly."""

        store = SkillVersionStore(db_path=tmp_path / "history_test.db")
        evolver = self._make_evolver(store, tmp_path)

        analyses = [
            {
                "completed": False,
                "failure_reason": "timeout",
                "quality_score": 0.2,
            },
            {
                "completed": True,
                "failure_reason": "",
                "quality_score": 0.9,
            },
        ]

        result = evolver._format_failure_history(analyses)
        assert "[FAILED]" in result
        assert "[completed]" in result
        assert "timeout" in result
        assert "0.2" in result
        assert "0.9" in result

    def test_format_failure_history_empty(self, tmp_path: Path):
        """_format_failure_history returns empty for no analyses."""

        store = SkillVersionStore(db_path=tmp_path / "empty_hist.db")
        evolver = self._make_evolver(store, tmp_path)
        assert evolver._format_failure_history([]) == ""

    def test_load_forbidden_targets_evolution_policy(self, tmp_path: Path):
        """Loads forbidden_targets from evolution_policy section."""
        from skills.skill_evolver import SkillEvolver

        policy_path = tmp_path / "policy.yaml"
        policy_path.write_text(
            "evolution_policy:\n  forbidden_targets:\n    - tool_permissions\n    - output_contract\n"
        )

        store = SkillVersionStore(db_path=tmp_path / "policy_test.db")
        evolver = SkillEvolver(
            version_store=store,
            mutation_policy_path=str(policy_path),
        )
        assert "tool_permissions" in evolver.forbidden_targets
        assert "output_contract" in evolver.forbidden_targets

    def test_load_forbidden_targets_fallback_general(self, tmp_path: Path):
        """Falls back to general forbidden_targets when no evolution_policy."""
        from skills.skill_evolver import SkillEvolver

        policy_path = tmp_path / "policy.yaml"
        policy_path.write_text("forbidden_targets:\n  - safety_rules\n  - tool_doctrine\n")

        store = SkillVersionStore(db_path=tmp_path / "fallback_test.db")
        evolver = SkillEvolver(
            version_store=store,
            mutation_policy_path=str(policy_path),
        )
        assert "safety_rules" in evolver.forbidden_targets
        assert "tool_doctrine" in evolver.forbidden_targets

    def test_load_forbidden_targets_missing_file(self, tmp_path: Path):
        """Returns empty list when mutation policy file doesn't exist."""
        from skills.skill_evolver import SkillEvolver

        store = SkillVersionStore(db_path=tmp_path / "missing_test.db")
        evolver = SkillEvolver(
            version_store=store,
            mutation_policy_path=str(tmp_path / "nonexistent_policy.yaml"),
        )
        assert evolver.forbidden_targets == []

    def test_check_cooldown_not_in_cooldown(self, tmp_path: Path):
        """check_cooldown returns True when skill can be evolved."""

        store = SkillVersionStore(db_path=tmp_path / "cooldown1.db")
        evolver = self._make_evolver(store, tmp_path)
        # No previous evolution time
        assert evolver.check_cooldown("any_skill") is True

    def test_check_cooldown_in_cooldown(self, tmp_path: Path):
        """check_cooldown returns False during cooldown period."""

        store = SkillVersionStore(db_path=tmp_path / "cooldown2.db")
        evolver = self._make_evolver(store, tmp_path)
        evolver._last_evolution_time["skill_x"] = time.time()
        assert evolver.check_cooldown("skill_x") is False

    def test_validate_fix_valid_content(self, tmp_path: Path):
        """_validate_fix returns no errors for valid content."""

        store = SkillVersionStore(db_path=tmp_path / "valid_test.db")
        evolver = self._make_evolver(store, tmp_path)
        errors = evolver._validate_fix(SAMPLE_SKILL_MD, "test-skill")
        assert errors == []

    def test_validate_fix_empty_content(self, tmp_path: Path):
        """_validate_fix catches empty content."""

        store = SkillVersionStore(db_path=tmp_path / "empty_valid.db")
        evolver = self._make_evolver(store, tmp_path)
        errors = evolver._validate_fix("", "test-skill")
        assert any("empty" in e.lower() for e in errors)

    def test_validate_fix_name_changed(self, tmp_path: Path):
        """_validate_fix catches skill name changes."""

        store = SkillVersionStore(db_path=tmp_path / "name_valid.db")
        evolver = self._make_evolver(store, tmp_path)
        content = SAMPLE_SKILL_MD.replace("name: test-skill", "name: different-name")
        errors = evolver._validate_fix(content, "test-skill")
        assert any("name changed" in e.lower() for e in errors)

    def test_validate_fix_missing_name(self, tmp_path: Path):
        """_validate_fix catches missing name field."""

        store = SkillVersionStore(db_path=tmp_path / "noname_valid.db")
        evolver = self._make_evolver(store, tmp_path)
        content = "---\ndescription: no name here\n---\nbody content that is long enough to pass length check"
        errors = evolver._validate_fix(content, "test-skill")
        assert any("name" in e.lower() and "missing" in e.lower() for e in errors)

    def test_validate_fix_malformed_frontmatter(self, tmp_path: Path):
        """_validate_fix catches malformed frontmatter."""

        store = SkillVersionStore(db_path=tmp_path / "malformed_valid.db")
        evolver = self._make_evolver(store, tmp_path)
        content = "---\nname: test-skill\nno closing delimiter but long enough content here"
        errors = evolver._validate_fix(content, "test-skill")
        assert any("frontmatter" in e.lower() for e in errors)

    def test_validate_fix_short_content(self, tmp_path: Path):
        """_validate_fix catches suspiciously short content."""

        store = SkillVersionStore(db_path=tmp_path / "short_valid.db")
        evolver = self._make_evolver(store, tmp_path)
        errors = evolver._validate_fix("short", "test-skill")
        assert any("short" in e.lower() for e in errors)

    def test_validate_fix_no_frontmatter_allowed(self, tmp_path: Path):
        """_validate_fix allows content without frontmatter (body-only fix)."""

        store = SkillVersionStore(db_path=tmp_path / "nobody_valid.db")
        evolver = self._make_evolver(store, tmp_path)
        content = "# No Frontmatter Skill\n\nThis is a body-only content that is long enough to pass validation checks."
        errors = evolver._validate_fix(content, "test-skill")
        assert errors == []

    def test_get_evolution_history(self, tmp_path: Path):
        """get_evolution_history returns a copy of the history dict."""

        store = SkillVersionStore(db_path=tmp_path / "history_copy.db")
        evolver = self._make_evolver(store, tmp_path)
        evolver._evolution_history["s1"] = "t1"
        evolver._evolution_history["s2"] = "t2"

        history = evolver.get_evolution_history()
        assert history == {"s1": "t1", "s2": "t2"}
        # Should be a copy
        history["s3"] = "t3"
        assert "s3" not in evolver._evolution_history

    def test_evolve_fix_deactivates_parent(self, store: SkillVersionStore, skill_dir: Path, tmp_path: Path):
        """evolve_fix deactivates the parent version."""

        parent = _make_skill(
            name="test-skill",
            skill_id="deact_parent_1",
            path=str(skill_dir),
        )
        store.register_skill(parent)
        evolver = self._make_evolver(store, tmp_path)

        fixed = SAMPLE_SKILL_MD.replace("testing things", "deactivation test")
        with patch.object(evolver, "_call_llm", return_value=fixed):
            result = evolver.evolve_fix(
                skill_id="deact_parent_1",
                direction="Test parent deactivation",
            )

        assert result is not None
        parent_reloaded = store.get_skill("deact_parent_1")
        assert parent_reloaded is not None
        assert parent_reloaded.is_active is False

    def test_evolve_fix_empty_skill_dir(self, store: SkillVersionStore, tmp_path: Path):
        """evolve_fix returns None for empty skill directory (no files)."""

        empty_dir = tmp_path / "empty-skill"
        empty_dir.mkdir()

        parent = _make_skill(
            name="empty-skill",
            skill_id="empty_dir_1",
            path=str(empty_dir),
        )
        store.register_skill(parent)
        evolver = self._make_evolver(store, tmp_path)

        result = evolver.evolve_fix(
            skill_id="empty_dir_1",
            direction="Fix empty",
        )
        assert result is None

    def test_evolve_fix_anti_loop_empty_task_id(self, store: SkillVersionStore, skill_dir: Path, tmp_path: Path):
        """Anti-loop check is skipped for empty task_id."""

        parent = _make_skill(
            name="test-skill",
            skill_id="noloop_empty_1",
            path=str(skill_dir),
        )
        store.register_skill(parent)
        evolver = self._make_evolver(store, tmp_path)
        evolver._evolution_history["noloop_empty_1"] = ""

        fixed = SAMPLE_SKILL_MD.replace("testing things", "empty task id things")
        with patch.object(evolver, "_call_llm", return_value=fixed):
            result = evolver.evolve_fix(
                skill_id="noloop_empty_1",
                direction="Fix with empty task_id",
                task_id="",
            )

        # Should proceed since task_id is empty
        assert result is not None


# ===========================================================================
# TestGovernance — governance check integration
# ===========================================================================


class TestGovernance:
    """Tests for governance checking functions."""

    def test_no_forbidden_targets(self):
        """No violations with empty forbidden list."""
        violations = check_governance("old content", "new content", [])
        assert violations == []

    def test_detects_forbidden_modification(self):
        """Detects modification of a forbidden section."""
        old = "tool_permissions:\n  - read\n  - write"
        new = "tool_permissions:\n  - read\n  - write\n  - execute"
        violations = check_governance(old, new, ["tool_permissions"])
        assert len(violations) >= 1
        assert any("tool_permissions" in v for v in violations)

    def test_allows_non_forbidden_modification(self):
        """Allows modification of non-forbidden sections."""
        old = "description: old description\ntool_permissions: same"
        new = "description: new description\ntool_permissions: same"
        violations = check_governance(old, new, ["tool_permissions"])
        assert violations == []

    def test_multiple_forbidden_targets(self):
        """Checks all forbidden targets."""
        old = "safety_rules: original\noutput_contract: original\ndescription: same"
        new = "safety_rules: modified\noutput_contract: modified\ndescription: same"
        violations = check_governance(old, new, ["safety_rules", "output_contract"])
        assert len(violations) >= 2

    def test_underscore_hyphen_normalization(self):
        """Forbidden target names with underscores match hyphenated content."""
        old = "tool-permissions: read"
        new = "tool-permissions: write"
        violations = check_governance(old, new, ["tool_permissions"])
        assert len(violations) >= 1

    def test_identical_content_no_violations(self):
        """No violations when content is identical."""
        content = "tool_permissions: read\noutput_contract: strict"
        violations = check_governance(content, content, ["tool_permissions", "output_contract"])
        assert violations == []


# ===========================================================================
# TestMutationPolicyFile — integration test for configs/mutation_policy.yaml
# ===========================================================================


class TestMutationPolicyFile:
    """Test the actual mutation_policy.yaml has the evolution_policy section."""

    def test_evolution_policy_exists(self):
        """configs/mutation_policy.yaml has evolution_policy section."""
        import yaml

        path = Path("configs/mutation_policy.yaml")
        if not path.exists():
            pytest.skip("mutation_policy.yaml not found (CI)")

        with open(path) as f:
            policy = yaml.safe_load(f)

        assert "evolution_policy" in policy
        ep = policy["evolution_policy"]
        assert "forbidden_targets" in ep
        assert "allowed_targets" in ep
        assert "tool_permissions" in ep["forbidden_targets"]
        assert "output_contract" in ep["forbidden_targets"]
        assert "safety_rules" in ep["forbidden_targets"]

    def test_evolution_policy_fix_constraints(self):
        """Evolution policy has fix-specific constraints."""
        import yaml

        path = Path("configs/mutation_policy.yaml")
        if not path.exists():
            pytest.skip("mutation_policy.yaml not found (CI)")

        with open(path) as f:
            policy = yaml.safe_load(f)

        fix = policy["evolution_policy"]["fix"]
        assert fix["max_generation"] == 5
        assert fix["cooldown_seconds"] == 7200


# ===========================================================================
# TestBugFixes — regression tests for C1, C2, H2, H5 fixes
# ===========================================================================


class TestBugFixes:
    """Regression tests for critical and high-priority bug fixes."""

    # --- C1: whitespace-normalized replace with leading whitespace ---

    def test_c1_whitespace_normalized_leading_whitespace(self):
        """C1: Content with leading whitespace matches correctly."""
        # Content starts with whitespace — the old .strip() in normalize()
        # caused an off-by-one in index mapping
        content = "   hello   world   foo"
        search = "hello world"
        replace = "REPLACED"
        result = _whitespace_normalized_replace(content, search, replace)
        assert result is not None
        assert "REPLACED" in result
        # The leading whitespace before 'hello' should be preserved up to
        # the match point, and 'foo' should still be present after
        assert "foo" in result

    def test_c1_whitespace_normalized_newline_leading(self):
        """C1: Content with leading newlines matches correctly."""
        content = "\n\n  hello   world\n  bar"
        search = "hello world"
        replace = "FIXED"
        result = _whitespace_normalized_replace(content, search, replace)
        assert result is not None
        assert "FIXED" in result
        assert "bar" in result

    def test_c1_whitespace_normalized_tabs_leading(self):
        """C1: Content with leading tabs matches correctly."""
        content = "\t\thello\t\tworld"
        search = "hello world"
        replace = "TABFIX"
        result = _whitespace_normalized_replace(content, search, replace)
        assert result is not None
        assert "TABFIX" in result

    # --- C2: file rollback when evolve_skill DB call fails ---

    def test_c2_rollback_on_db_failure(self, store: SkillVersionStore, skill_dir: Path, tmp_path: Path):
        """C2: SKILL.md is restored when DB evolve_skill() fails."""
        from skills.skill_evolver import SkillEvolver

        parent = _make_skill(
            name="test-skill",
            skill_id="rollback_1",
            path=str(skill_dir),
        )
        store.register_skill(parent)

        evolver = SkillEvolver(
            version_store=store,
            mutation_policy_path=str(tmp_path / "mutation_policy.yaml"),
            skills_base_dir=str(tmp_path),
        )
        # Write a minimal policy
        (tmp_path / "mutation_policy.yaml").write_text("evolution_policy:\n  forbidden_targets: []\n")
        evolver.forbidden_targets = []

        # Save original content for comparison
        original_content = (skill_dir / "SKILL.md").read_text()

        fixed_content = SAMPLE_SKILL_MD.replace("testing things", "rollback-test things")

        # Make evolve_skill raise to simulate DB failure
        with (
            patch.object(evolver, "_call_llm", return_value=fixed_content),
            patch.object(
                store,
                "evolve_skill",
                side_effect=RuntimeError("DB connection lost"),
            ),
        ):
            result = evolver.evolve_fix(
                skill_id="rollback_1",
                direction="Fix that should be rolled back",
            )

        # Evolution should have failed
        assert result is None
        # SKILL.md should be restored to original content
        restored = (skill_dir / "SKILL.md").read_text()
        assert restored == original_content
        assert "rollback-test" not in restored

    # --- H2: cooldown bypass via new skill_id ---

    def test_h2_cooldown_by_name_prevents_bypass(self, store: SkillVersionStore, skill_dir: Path, tmp_path: Path):
        """H2: Cooldown tracked by skill name prevents bypass via new ID."""
        from skills.skill_evolver import SkillEvolver

        parent = _make_skill(
            name="test-skill",
            skill_id="bypass_parent_1",
            path=str(skill_dir),
        )
        store.register_skill(parent)
        evolver = SkillEvolver(
            version_store=store,
            mutation_policy_path=str(tmp_path / "mutation_policy.yaml"),
            skills_base_dir=str(tmp_path),
        )
        (tmp_path / "mutation_policy.yaml").write_text("evolution_policy:\n  forbidden_targets: []\n")
        evolver.forbidden_targets = []

        # Simulate that a previous evolution set the name-based cooldown
        evolver._last_evolution_time["name:test-skill"] = time.time()

        # Now try to evolve the parent — should be blocked by name cooldown
        result = evolver.evolve_fix(
            skill_id="bypass_parent_1",
            direction="Try to bypass cooldown",
        )
        assert result is None

    def test_h2_cooldown_set_for_new_id_and_name(self, store: SkillVersionStore, skill_dir: Path, tmp_path: Path):
        """H2: After successful fix, cooldown is set for parent, new ID, and name."""
        from skills.skill_evolver import SkillEvolver

        parent = _make_skill(
            name="test-skill",
            skill_id="cd_track_1",
            path=str(skill_dir),
        )
        store.register_skill(parent)
        evolver = SkillEvolver(
            version_store=store,
            mutation_policy_path=str(tmp_path / "mutation_policy.yaml"),
            skills_base_dir=str(tmp_path),
        )
        (tmp_path / "mutation_policy.yaml").write_text("evolution_policy:\n  forbidden_targets: []\n")
        evolver.forbidden_targets = []

        fixed = SAMPLE_SKILL_MD.replace("testing things", "cooldown-tracked")
        with patch.object(evolver, "_call_llm", return_value=fixed):
            result = evolver.evolve_fix(
                skill_id="cd_track_1",
                direction="Test cooldown tracking",
            )

        assert result is not None
        # Cooldown should be set for parent ID
        assert "cd_track_1" in evolver._last_evolution_time
        # Cooldown should be set for new skill ID
        assert result.skill_id in evolver._last_evolution_time
        # Cooldown should be set by name
        assert "name:test-skill" in evolver._last_evolution_time

    def test_h2_check_cooldown_by_name(self, tmp_path: Path):
        """H2: check_cooldown respects name-based cooldown."""
        from skills.skill_evolver import SkillEvolver

        store = SkillVersionStore(db_path=tmp_path / "cd_name.db")
        evolver = SkillEvolver(
            version_store=store,
            mutation_policy_path=str(tmp_path / "nonexistent.yaml"),
        )
        # Set cooldown by name only
        evolver._last_evolution_time["name:my-skill"] = time.time()
        # Checking by new_skill_id alone would bypass, but with name it blocks
        assert evolver.check_cooldown("new_id_123", skill_name="my-skill") is False
        # Without name, new ID would pass
        assert evolver.check_cooldown("new_id_123") is True

    # --- H5: full rewrite rejection when content too short ---

    def test_h5_full_rewrite_too_short_rejected(self, skill_dir: Path):
        """H5: Full rewrite with content < 50 chars is rejected."""
        with pytest.raises(PatchError, match="too short"):
            apply_patch(str(skill_dir / "SKILL.md"), "short garbage")

    def test_h5_full_rewrite_empty_rejected(self, skill_dir: Path):
        """H5: Empty full rewrite is rejected."""
        with pytest.raises(PatchError, match="too short"):
            apply_patch(str(skill_dir / "SKILL.md"), "")

    def test_h5_full_rewrite_whitespace_only_rejected(self, skill_dir: Path):
        """H5: Whitespace-only full rewrite is rejected."""
        with pytest.raises(PatchError, match="too short"):
            apply_patch(str(skill_dir / "SKILL.md"), "   \n\n\t  ")

    def test_h5_full_rewrite_valid_length_accepted(self, skill_dir: Path):
        """H5: Full rewrite with sufficient length is accepted."""
        content = "---\nname: test-skill\n---\n\nThis is a valid skill body with more than fifty characters of content."
        result = apply_patch(str(skill_dir / "SKILL.md"), content)
        assert result == content


# ===========================================================================
# TestEvolveDerived — AUTO-IMPROVE skill derivation (Phase 4)
# ===========================================================================


SAMPLE_DERIVED_SKILL_MD = """---
name: test-skill
description: "An enhanced test skill with improved capabilities."
activation:
  keywords: [test, unit, enhanced]
tool_doctrine:
  test:
    workflow:
      - step_one
      - step_two
      - step_three_enhanced
output_contract:
  required:
    - result
    - verification
    - enhancement_report
---

# Enhanced Test Skill

This skill does enhanced testing things with improved coverage.

## Steps

1. Run tests with enhanced coverage
2. Analyze results deeply
3. Report enhanced results
"""

SAMPLE_DERIVED_CUSTOM_NAME_MD = """---
name: test-skill-fast
description: "A fast variant of the test skill."
activation:
  keywords: [test, fast, unit]
tool_doctrine:
  test:
    workflow:
      - step_one_fast
      - step_two_fast
output_contract:
  required:
    - result
    - verification
---

# Fast Test Skill

This skill does fast testing things optimized for speed.

## Steps

1. Run fast tests
2. Report fast results
"""


class TestEvolveDerived:
    """Tests for SkillEvolver.evolve_derived (AUTO-IMPROVE, Phase 4)."""

    def _make_evolver(self, store: SkillVersionStore, tmp_path: Path):
        """Create a SkillEvolver with a test mutation policy."""
        from skills.skill_evolver import SkillEvolver

        policy_path = tmp_path / "mutation_policy.yaml"
        policy_path.write_text(
            "evolution_policy:\n"
            "  forbidden_targets:\n"
            "    - tool_permissions\n"
            "    - output_contract\n"
            "    - safety_rules\n"
        )
        return SkillEvolver(
            version_store=store,
            mutation_policy_path=str(policy_path),
            skills_base_dir=str(tmp_path),
        )

    def test_evolve_derived_creates_new_version(self, store: SkillVersionStore, skill_dir: Path, tmp_path: Path):
        """evolve_derived creates a new SkillVersion from a successful parent."""
        parent = _make_skill(
            name="test-skill",
            skill_id="derived_parent_1",
            path=str(skill_dir),
        )
        store.register_skill(parent)
        evolver = self._make_evolver(store, tmp_path)

        with patch.object(evolver, "_call_llm", return_value=SAMPLE_DERIVED_SKILL_MD):
            result = evolver.evolve_derived(
                parent_id="derived_parent_1",
                direction="Add enhanced coverage analysis",
                task_id="task_derive_1",
                success_context="Skill succeeded at testing with 95% coverage",
            )

        assert result is not None
        assert result.name == "test-skill"
        assert result.is_active is True
        assert result.lineage.generation == 1
        assert result.created_by == "auto_improve"

    def test_evolve_derived_parent_stays_active(self, store: SkillVersionStore, skill_dir: Path, tmp_path: Path):
        """evolve_derived does NOT deactivate the parent (unlike evolve_fix)."""
        parent = _make_skill(
            name="test-skill",
            skill_id="active_parent_1",
            path=str(skill_dir),
        )
        store.register_skill(parent)
        evolver = self._make_evolver(store, tmp_path)

        with patch.object(evolver, "_call_llm", return_value=SAMPLE_DERIVED_SKILL_MD):
            result = evolver.evolve_derived(
                parent_id="active_parent_1",
                direction="Improve test reporting",
            )

        assert result is not None
        # Parent MUST still be active
        parent_reloaded = store.get_skill("active_parent_1")
        assert parent_reloaded is not None
        assert parent_reloaded.is_active is True

    def test_evolve_derived_with_custom_name(self, store: SkillVersionStore, skill_dir: Path, tmp_path: Path):
        """evolve_derived uses new_name when provided."""
        parent = _make_skill(
            name="test-skill",
            skill_id="custom_name_1",
            path=str(skill_dir),
        )
        store.register_skill(parent)
        evolver = self._make_evolver(store, tmp_path)

        with patch.object(evolver, "_call_llm", return_value=SAMPLE_DERIVED_CUSTOM_NAME_MD):
            result = evolver.evolve_derived(
                parent_id="custom_name_1",
                direction="Create a fast variant",
                new_name="test-skill-fast",
            )

        assert result is not None
        assert result.name == "test-skill-fast"

    def test_evolve_derived_inherits_parent_name_by_default(
        self, store: SkillVersionStore, skill_dir: Path, tmp_path: Path
    ):
        """evolve_derived inherits parent name when new_name is None."""
        parent = _make_skill(
            name="test-skill",
            skill_id="inherit_name_1",
            path=str(skill_dir),
        )
        store.register_skill(parent)
        evolver = self._make_evolver(store, tmp_path)

        with patch.object(evolver, "_call_llm", return_value=SAMPLE_DERIVED_SKILL_MD):
            result = evolver.evolve_derived(
                parent_id="inherit_name_1",
                direction="Enhance without renaming",
            )

        assert result is not None
        assert result.name == "test-skill"

    def test_evolve_derived_origin_is_derived(self, store: SkillVersionStore, skill_dir: Path, tmp_path: Path):
        """evolve_derived sets origin to DERIVED."""
        parent = _make_skill(
            name="test-skill",
            skill_id="origin_derived_1",
            path=str(skill_dir),
        )
        store.register_skill(parent)
        evolver = self._make_evolver(store, tmp_path)

        with patch.object(evolver, "_call_llm", return_value=SAMPLE_DERIVED_SKILL_MD):
            result = evolver.evolve_derived(
                parent_id="origin_derived_1",
                direction="Specialize for Python testing",
            )

        assert result is not None
        assert result.lineage.origin == Origin.DERIVED

    def test_evolve_derived_generation_incremented(self, store: SkillVersionStore, skill_dir: Path, tmp_path: Path):
        """evolve_derived increments generation from parent."""
        parent = _make_skill(
            name="test-skill",
            skill_id="gen_inc_1",
            path=str(skill_dir),
            generation=2,
            origin=Origin.DERIVED,
        )
        store.register_skill(parent)
        evolver = self._make_evolver(store, tmp_path)

        with patch.object(evolver, "_call_llm", return_value=SAMPLE_DERIVED_SKILL_MD):
            result = evolver.evolve_derived(
                parent_id="gen_inc_1",
                direction="Further enhance",
            )

        assert result is not None
        assert result.lineage.generation == 3
        assert result.version == "1.3.0"

    def test_evolve_derived_respects_generation_cap(self, store: SkillVersionStore, skill_dir: Path, tmp_path: Path):
        """evolve_derived returns None when parent is at MAX_GENERATION."""
        from skills.skill_evolver import MAX_GENERATION

        parent = _make_skill(
            name="test-skill",
            skill_id="gen_cap_1",
            path=str(skill_dir),
            generation=MAX_GENERATION,
            origin=Origin.DERIVED,
        )
        store.register_skill(parent)
        evolver = self._make_evolver(store, tmp_path)

        result = evolver.evolve_derived(
            parent_id="gen_cap_1",
            direction="Try to exceed generation cap",
        )

        assert result is None

    def test_evolve_derived_respects_cooldown(self, store: SkillVersionStore, skill_dir: Path, tmp_path: Path):
        """evolve_derived returns None during cooldown period."""
        parent = _make_skill(
            name="test-skill",
            skill_id="cooldown_der_1",
            path=str(skill_dir),
        )
        store.register_skill(parent)
        evolver = self._make_evolver(store, tmp_path)

        # Set cooldown
        evolver._last_evolution_time["cooldown_der_1"] = time.time()

        result = evolver.evolve_derived(
            parent_id="cooldown_der_1",
            direction="Should be blocked by cooldown",
        )

        assert result is None

    def test_evolve_derived_saves_snapshots(self, store: SkillVersionStore, skill_dir: Path, tmp_path: Path):
        """evolve_derived saves pre- and post-evolution snapshots."""
        parent = _make_skill(
            name="test-skill",
            skill_id="snap_der_1",
            path=str(skill_dir),
        )
        store.register_skill(parent)
        evolver = self._make_evolver(store, tmp_path)

        with patch.object(evolver, "_call_llm", return_value=SAMPLE_DERIVED_SKILL_MD):
            result = evolver.evolve_derived(
                parent_id="snap_der_1",
                direction="Test snapshot saving",
            )

        assert result is not None
        # Pre-evolution snapshot for parent
        parent_snapshot = store.get_snapshot("snap_der_1")
        assert parent_snapshot is not None
        assert "SKILL.md" in parent_snapshot
        # Post-evolution snapshot for new version
        new_snapshot = store.get_snapshot(result.skill_id)
        assert new_snapshot is not None
        assert "SKILL.md" in new_snapshot

    def test_evolve_derived_content_diff_generated(self, store: SkillVersionStore, skill_dir: Path, tmp_path: Path):
        """evolve_derived generates a content diff in the lineage."""
        parent = _make_skill(
            name="test-skill",
            skill_id="diff_der_1",
            path=str(skill_dir),
        )
        store.register_skill(parent)
        evolver = self._make_evolver(store, tmp_path)

        with patch.object(evolver, "_call_llm", return_value=SAMPLE_DERIVED_SKILL_MD):
            result = evolver.evolve_derived(
                parent_id="diff_der_1",
                direction="Generate a diff test",
            )

        assert result is not None
        assert result.lineage.content_diff is not None
        assert result.lineage.content_diff != ""
        assert result.lineage.change_summary == "AUTO-IMPROVE: Generate a diff test"

    def test_evolve_derived_missing_parent_returns_none(self, store: SkillVersionStore, tmp_path: Path):
        """evolve_derived returns None when parent_id is not in store."""
        evolver = self._make_evolver(store, tmp_path)

        result = evolver.evolve_derived(
            parent_id="nonexistent_skill_id",
            direction="Should fail",
        )

        assert result is None

    def test_evolve_derived_missing_skill_dir_returns_none(self, store: SkillVersionStore, tmp_path: Path):
        """evolve_derived returns None when skill directory doesn't exist."""
        parent = _make_skill(
            name="test-skill",
            skill_id="nodir_der_1",
            path=str(tmp_path / "nonexistent_dir"),
        )
        store.register_skill(parent)
        evolver = self._make_evolver(store, tmp_path)

        result = evolver.evolve_derived(
            parent_id="nodir_der_1",
            direction="Should fail — no dir",
        )

        assert result is None

    def test_evolve_derived_retry_on_failure(self, store: SkillVersionStore, skill_dir: Path, tmp_path: Path):
        """evolve_derived retries when validation fails on first attempt."""
        parent = _make_skill(
            name="test-skill",
            skill_id="retry_der_1",
            path=str(skill_dir),
        )
        store.register_skill(parent)
        evolver = self._make_evolver(store, tmp_path)

        # First call: bad content (name changed), second: good content
        bad_content = SAMPLE_DERIVED_SKILL_MD.replace("name: test-skill", "name: bad-name")
        call_count = 0

        def mock_llm(prompt):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return bad_content
            return SAMPLE_DERIVED_SKILL_MD

        with patch.object(evolver, "_call_llm", side_effect=mock_llm):
            result = evolver.evolve_derived(
                parent_id="retry_der_1",
                direction="Test retry on bad name",
            )

        assert result is not None
        assert call_count == 2

    def test_evolve_derived_returns_none_after_all_retries(
        self, store: SkillVersionStore, skill_dir: Path, tmp_path: Path
    ):
        """evolve_derived returns None when all retry attempts fail."""
        from skills.skill_evolver import MAX_FIX_ATTEMPTS

        parent = _make_skill(
            name="test-skill",
            skill_id="allretry_der_1",
            path=str(skill_dir),
        )
        store.register_skill(parent)
        evolver = self._make_evolver(store, tmp_path)

        # Always return empty (invalid) content
        call_count = 0

        def mock_llm(prompt):
            nonlocal call_count
            call_count += 1
            return ""

        with patch.object(evolver, "_call_llm", side_effect=mock_llm):
            result = evolver.evolve_derived(
                parent_id="allretry_der_1",
                direction="All retries should fail",
            )

        assert result is None
        assert call_count == MAX_FIX_ATTEMPTS

    def test_detect_improvement_candidates(self, store: SkillVersionStore, tmp_path: Path):
        """detect_improvement_candidates finds healthy high-quality skills."""
        evolver = self._make_evolver(store, tmp_path)

        # Register a skill with enough stats to pass health check
        skill = _make_skill(
            name="high-quality-skill",
            skill_id="hq_1",
            selections=20,
            executions=18,
            completions=15,
            failures=1,
            fallbacks=0,
        )
        store.register_skill(skill)

        # Mock get_skill_health to return a healthy skill
        from skills.execution_analysis import SkillHealth

        mock_health = SkillHealth(
            skill_name="high-quality-skill",
            skill_id="hq_1",
            selections=20,
            executions=18,
            completions=15,
            failures=1,
            fallbacks=0,
            applied_rate=0.9,
            completion_rate=0.83,
            effective_rate=0.75,
            fallback_rate=0.0,
            avg_quality_score=0.85,
            is_healthy=True,
            health_issues=[],
        )

        with patch.object(store, "get_skill_health", return_value=[mock_health]):
            candidates = evolver.detect_improvement_candidates(min_completions=10, min_quality=0.7)

        assert len(candidates) == 1
        assert candidates[0]["skill_id"] == "hq_1"
        assert candidates[0]["skill_name"] == "high-quality-skill"
        assert "suggestion" in candidates[0]
        # Should contain generic suggestion since no DERIVED analyses exist
        assert "High-performing skill" in candidates[0]["suggestion"]

    def test_detect_improvement_candidates_filters_unhealthy(self, store: SkillVersionStore, tmp_path: Path):
        """detect_improvement_candidates excludes unhealthy skills."""
        evolver = self._make_evolver(store, tmp_path)

        from skills.execution_analysis import SkillHealth

        unhealthy = SkillHealth(
            skill_name="unhealthy-skill",
            skill_id="uh_1",
            selections=20,
            executions=18,
            completions=5,
            failures=13,
            fallbacks=10,
            applied_rate=0.9,
            completion_rate=0.28,
            effective_rate=0.25,
            fallback_rate=0.5,
            avg_quality_score=0.3,
            is_healthy=False,
            health_issues=["Low completion rate"],
        )

        with patch.object(store, "get_skill_health", return_value=[unhealthy]):
            candidates = evolver.detect_improvement_candidates(min_completions=5, min_quality=0.2)

        assert len(candidates) == 0

    def test_detect_improvement_candidates_filters_low_quality(self, store: SkillVersionStore, tmp_path: Path):
        """detect_improvement_candidates excludes skills below quality threshold."""
        evolver = self._make_evolver(store, tmp_path)

        from skills.execution_analysis import SkillHealth

        low_quality = SkillHealth(
            skill_name="low-quality-skill",
            skill_id="lq_1",
            selections=20,
            executions=18,
            completions=15,
            failures=1,
            fallbacks=0,
            applied_rate=0.9,
            completion_rate=0.83,
            effective_rate=0.75,
            fallback_rate=0.0,
            avg_quality_score=0.5,  # Below default 0.7 threshold
            is_healthy=True,
            health_issues=[],
        )

        with patch.object(store, "get_skill_health", return_value=[low_quality]):
            candidates = evolver.detect_improvement_candidates(min_completions=10, min_quality=0.7)

        assert len(candidates) == 0

    def test_build_derived_prompt_includes_context(self, tmp_path: Path):
        """_build_derived_prompt includes all relevant context."""

        store = SkillVersionStore(db_path=tmp_path / "derived_prompt_test.db")
        evolver = self._make_evolver(store, tmp_path)

        prompt = evolver._build_derived_prompt(
            skill_name="my-skill",
            effective_name="my-skill",
            skill_content={
                "SKILL.md": "# My Skill",
                "metadata.json": '{"name": "my-skill"}',
            },
            direction="Add parallel execution support",
            success_context="Completed 50 tasks with 95% success rate",
            success_history="- [completed] quality=0.9",
        )

        assert "my-skill" in prompt
        assert "# My Skill" in prompt
        assert "Add parallel execution support" in prompt
        assert "Completed 50 tasks" in prompt
        assert "quality=0.9" in prompt
        assert "enhancement" in prompt.lower()

    def test_build_derived_prompt_custom_name_instruction(self, tmp_path: Path):
        """_build_derived_prompt uses correct name instruction for custom name."""

        store = SkillVersionStore(db_path=tmp_path / "derived_name_prompt.db")
        evolver = self._make_evolver(store, tmp_path)

        prompt = evolver._build_derived_prompt(
            skill_name="original-skill",
            effective_name="specialized-skill",
            skill_content={"SKILL.md": "# Skill"},
            direction="Specialize",
            success_context="",
            success_history="",
        )

        assert 'MUST be "specialized-skill"' in prompt
        # Should NOT say "remain"
        assert "remain" not in prompt

    def test_format_success_history(self, tmp_path: Path):
        """_format_success_history formats analyses correctly."""

        store = SkillVersionStore(db_path=tmp_path / "success_hist.db")
        evolver = self._make_evolver(store, tmp_path)

        analyses = [
            {"completed": True, "quality_score": 0.9},
            {"completed": True, "quality_score": 0.85},
            {"completed": False, "quality_score": 0.3},
        ]

        result = evolver._format_success_history(analyses)
        assert "[completed]" in result
        assert "[FAILED]" in result
        assert "0.9" in result
        assert "0.85" in result

    def test_format_success_history_empty(self, tmp_path: Path):
        """_format_success_history returns empty for no analyses."""

        store = SkillVersionStore(db_path=tmp_path / "empty_success.db")
        evolver = self._make_evolver(store, tmp_path)
        assert evolver._format_success_history([]) == ""


# ===========================================================================
# TestEvolveCaptured — AUTO-LEARN skill capture (Phase 5)
# ===========================================================================

SAMPLE_CAPTURED_SKILL_MD = """---
name: data-pipeline-cleanup
description: "Clean up data pipeline artifacts after processing."
activation:
  keywords: [cleanup, pipeline, data, artifacts]
tool_doctrine:
  cleanup:
    workflow:
      - identify_artifacts
      - remove_stale
      - verify_clean
output_contract:
  required:
    - cleanup_report
    - verification
---

# Data Pipeline Cleanup

This skill handles cleanup of data pipeline artifacts.

## Steps

1. Identify stale artifacts
2. Remove artifacts safely
3. Verify cleanup completed
"""


class TestEvolveCaptured:
    """Tests for SkillEvolver.evolve_captured (AUTO-LEARN, Phase 5)."""

    def _make_evolver(self, store: SkillVersionStore, tmp_path: Path):
        """Create a SkillEvolver with a test mutation policy."""
        from skills.skill_evolver import SkillEvolver

        policy_path = tmp_path / "mutation_policy.yaml"
        policy_path.write_text(
            "evolution_policy:\n"
            "  forbidden_targets:\n"
            "    - tool_permissions\n"
            "    - output_contract\n"
            "    - safety_rules\n"
        )
        return SkillEvolver(
            version_store=store,
            mutation_policy_path=str(policy_path),
            skills_base_dir=str(tmp_path / "skills"),
        )

    def test_evolve_captured_creates_new_skill(self, store: SkillVersionStore, tmp_path: Path):
        """evolve_captured creates a new SkillVersion from a pattern."""
        evolver = self._make_evolver(store, tmp_path)

        with patch.object(evolver, "_call_llm", return_value=SAMPLE_CAPTURED_SKILL_MD):
            result = evolver.evolve_captured(
                pattern_description="Cleaning up data pipeline artifacts after ETL jobs",
                task_examples=[
                    "Clean up staging data after pipeline run",
                    "Remove temporary files from ETL processing",
                ],
                suggested_name="data-pipeline-cleanup",
                task_id="task_capture_1",
            )

        assert result is not None
        assert result.name == "data-pipeline-cleanup"

    def test_evolve_captured_origin_is_captured(self, store: SkillVersionStore, tmp_path: Path):
        """evolve_captured sets origin to CAPTURED."""
        evolver = self._make_evolver(store, tmp_path)

        with patch.object(evolver, "_call_llm", return_value=SAMPLE_CAPTURED_SKILL_MD):
            result = evolver.evolve_captured(
                pattern_description="Pattern for origin test",
                task_examples=["Example task 1", "Example task 2"],
                suggested_name="data-pipeline-cleanup",
            )

        assert result is not None
        assert result.lineage.origin == Origin.CAPTURED

    def test_evolve_captured_generation_is_zero(self, store: SkillVersionStore, tmp_path: Path):
        """evolve_captured creates skill at generation 0."""
        evolver = self._make_evolver(store, tmp_path)

        with patch.object(evolver, "_call_llm", return_value=SAMPLE_CAPTURED_SKILL_MD):
            result = evolver.evolve_captured(
                pattern_description="Pattern for generation test",
                task_examples=["Task A", "Task B"],
                suggested_name="data-pipeline-cleanup",
            )

        assert result is not None
        assert result.lineage.generation == 0

    def test_evolve_captured_no_parent(self, store: SkillVersionStore, tmp_path: Path):
        """evolve_captured creates skill with no parent IDs."""
        evolver = self._make_evolver(store, tmp_path)

        with patch.object(evolver, "_call_llm", return_value=SAMPLE_CAPTURED_SKILL_MD):
            result = evolver.evolve_captured(
                pattern_description="Pattern for parent test",
                task_examples=["Task X", "Task Y"],
                suggested_name="data-pipeline-cleanup",
            )

        assert result is not None
        assert result.lineage.parent_ids == []

    def test_evolve_captured_creates_skill_directory(self, store: SkillVersionStore, tmp_path: Path):
        """evolve_captured creates the skill directory on disk."""
        evolver = self._make_evolver(store, tmp_path)

        with patch.object(evolver, "_call_llm", return_value=SAMPLE_CAPTURED_SKILL_MD):
            result = evolver.evolve_captured(
                pattern_description="Pattern for directory test",
                task_examples=["Task 1", "Task 2"],
                suggested_name="data-pipeline-cleanup",
            )

        assert result is not None
        skill_dir = tmp_path / "skills" / "data-pipeline-cleanup"
        assert skill_dir.exists()
        assert skill_dir.is_dir()

    def test_evolve_captured_writes_skill_md(self, store: SkillVersionStore, tmp_path: Path):
        """evolve_captured writes SKILL.md to the skill directory."""
        evolver = self._make_evolver(store, tmp_path)

        with patch.object(evolver, "_call_llm", return_value=SAMPLE_CAPTURED_SKILL_MD):
            result = evolver.evolve_captured(
                pattern_description="Pattern for SKILL.md test",
                task_examples=["Task alpha", "Task beta"],
                suggested_name="data-pipeline-cleanup",
            )

        assert result is not None
        skill_md_path = tmp_path / "skills" / "data-pipeline-cleanup" / "SKILL.md"
        assert skill_md_path.exists()
        content = skill_md_path.read_text(encoding="utf-8")
        assert "data-pipeline-cleanup" in content
        assert "---" in content

    def test_evolve_captured_writes_metadata_json(self, store: SkillVersionStore, tmp_path: Path):
        """evolve_captured writes metadata.json with quarantine flag."""
        evolver = self._make_evolver(store, tmp_path)

        with patch.object(evolver, "_call_llm", return_value=SAMPLE_CAPTURED_SKILL_MD):
            result = evolver.evolve_captured(
                pattern_description="Pattern for metadata test",
                task_examples=["Task one", "Task two"],
                suggested_name="data-pipeline-cleanup",
                task_id="task_meta_1",
            )

        assert result is not None
        metadata_path = tmp_path / "skills" / "data-pipeline-cleanup" / "metadata.json"
        assert metadata_path.exists()
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        assert metadata["name"] == "data-pipeline-cleanup"
        assert metadata["origin"] == "captured"
        assert metadata["optimization_enabled"] is False
        assert metadata["quarantine"] is True
        assert metadata["quarantine_successes_required"] == 5
        assert metadata["captured_from"]["task_id"] == "task_meta_1"

    def test_evolve_captured_rate_limit(self, store: SkillVersionStore, tmp_path: Path):
        """evolve_captured enforces max 2 captures per day."""
        from skills.skill_evolver import MAX_CAPTURES_PER_DAY

        evolver = self._make_evolver(store, tmp_path)

        # Use different names to avoid duplicate detection
        skill_names = [
            "first-captured-skill",
            "second-captured-skill",
            "third-captured-skill",
        ]
        skill_mds = []
        for name in skill_names:
            skill_mds.append(SAMPLE_CAPTURED_SKILL_MD.replace("data-pipeline-cleanup", name))

        results = []
        for i, (name, md) in enumerate(zip(skill_names, skill_mds, strict=False)):
            with patch.object(evolver, "_call_llm", return_value=md):
                result = evolver.evolve_captured(
                    pattern_description=f"Pattern {i}",
                    task_examples=[f"Task {i}a", f"Task {i}b"],
                    suggested_name=name,
                )
                results.append(result)

        # First two should succeed
        assert results[0] is not None
        assert results[1] is not None
        # Third should be rate-limited
        assert results[2] is None
        assert evolver._captures_today == MAX_CAPTURES_PER_DAY

    def test_evolve_captured_requires_min_task_examples(self, store: SkillVersionStore, tmp_path: Path):
        """evolve_captured requires at least 2 task examples."""
        evolver = self._make_evolver(store, tmp_path)

        # Only one example — should fail
        result = evolver.evolve_captured(
            pattern_description="Insufficient examples pattern",
            task_examples=["Only one task"],
            suggested_name="single-example-skill",
        )
        assert result is None

        # Zero examples — should fail
        result = evolver.evolve_captured(
            pattern_description="No examples pattern",
            task_examples=[],
            suggested_name="zero-example-skill",
        )
        assert result is None

    def test_evolve_captured_duplicate_detection(self, store: SkillVersionStore, tmp_path: Path):
        """evolve_captured rejects skills with similar existing names."""
        evolver = self._make_evolver(store, tmp_path)

        # Register an existing skill
        existing = _make_skill(
            name="data-pipeline-cleanup",
            origin=Origin.IMPORTED,
        )
        store.register_skill(existing)

        # Try to capture a skill with the same name
        with patch.object(evolver, "_call_llm", return_value=SAMPLE_CAPTURED_SKILL_MD):
            result = evolver.evolve_captured(
                pattern_description="Duplicate pattern",
                task_examples=["Task dup 1", "Task dup 2"],
                suggested_name="data-pipeline-cleanup",
            )

        assert result is None

    def test_evolve_captured_returns_none_on_llm_failure(self, store: SkillVersionStore, tmp_path: Path):
        """evolve_captured returns None when LLM returns empty response."""
        evolver = self._make_evolver(store, tmp_path)

        with patch.object(evolver, "_call_llm", return_value=""):
            result = evolver.evolve_captured(
                pattern_description="LLM failure pattern",
                task_examples=["Task fail 1", "Task fail 2"],
                suggested_name="llm-fail-skill",
            )

        assert result is None

    def test_evolve_captured_returns_none_on_short_llm_response(self, store: SkillVersionStore, tmp_path: Path):
        """evolve_captured returns None when LLM response is too short."""
        evolver = self._make_evolver(store, tmp_path)

        with patch.object(evolver, "_call_llm", return_value="short"):
            result = evolver.evolve_captured(
                pattern_description="Short LLM response pattern",
                task_examples=["Task short 1", "Task short 2"],
                suggested_name="short-response-skill",
            )

        assert result is None

    def test_detect_novel_patterns(self, store: SkillVersionStore, tmp_path: Path):
        """detect_novel_patterns finds tasks that succeeded without skill application."""
        from skills.execution_analysis import (
            ExecutionAnalysis,
            SkillJudgment,
        )

        evolver = self._make_evolver(store, tmp_path)

        # Register a skill so the analyses have a valid skill reference
        skill = _make_skill(
            name="existing-skill",
            skill_id="exist_1",
        )
        store.register_skill(skill)

        # Store analyses for tasks where no skill was applied but work completed
        for i in range(3):
            analysis = ExecutionAnalysis(
                task_id=f"novel_task_{i}",
                task_description=f"Novel task {i}",
                skill_judgments=[
                    SkillJudgment(
                        skill_name="existing-skill",
                        skill_id="exist_1",
                        applied=False,
                        completed=True,
                        quality_score=0.8,
                    ),
                ],
                overall_quality=0.8,
            )
            store.store_analysis(analysis)

        patterns = evolver.detect_novel_patterns(min_occurrences=2)
        assert len(patterns) >= 1
        # Check pattern structure
        pattern = patterns[0]
        assert "task_ids" in pattern
        assert "description_pattern" in pattern
        assert len(pattern["task_ids"]) >= 2

    def test_detect_novel_patterns_excludes_applied(self, store: SkillVersionStore, tmp_path: Path):
        """detect_novel_patterns excludes tasks where a skill was applied."""
        from skills.execution_analysis import (
            ExecutionAnalysis,
            SkillJudgment,
        )

        evolver = self._make_evolver(store, tmp_path)

        skill = _make_skill(
            name="applied-skill",
            skill_id="applied_1",
        )
        store.register_skill(skill)

        # Store analyses where skill WAS applied
        for i in range(3):
            analysis = ExecutionAnalysis(
                task_id=f"applied_task_{i}",
                task_description=f"Applied task {i}",
                skill_judgments=[
                    SkillJudgment(
                        skill_name="applied-skill",
                        skill_id="applied_1",
                        applied=True,
                        completed=True,
                        quality_score=0.9,
                    ),
                ],
                overall_quality=0.9,
            )
            store.store_analysis(analysis)

        patterns = evolver.detect_novel_patterns(min_occurrences=2)
        # Should find no novel patterns since all tasks had skills applied
        assert len(patterns) == 0

    def test_evolve_captured_sanitize_name(self, store: SkillVersionStore, tmp_path: Path):
        """evolve_captured sanitizes suggested names properly."""

        evolver = self._make_evolver(store, tmp_path)

        # Verify sanitization
        assert evolver._sanitize_skill_name("My Cool Skill!") == "my-cool-skill-"[: len("my-cool-skill")]
        assert evolver._sanitize_skill_name("  UPPER case  ") == "upper-case"
        assert evolver._sanitize_skill_name("a--b---c") == "a-b-c"
        assert evolver._sanitize_skill_name("") != ""  # fallback name

    def test_evolve_captured_created_by_auto_capture(self, store: SkillVersionStore, tmp_path: Path):
        """evolve_captured sets created_by to 'auto_capture'."""
        evolver = self._make_evolver(store, tmp_path)

        with patch.object(evolver, "_call_llm", return_value=SAMPLE_CAPTURED_SKILL_MD):
            result = evolver.evolve_captured(
                pattern_description="Pattern for created_by test",
                task_examples=["Task cb1", "Task cb2"],
                suggested_name="data-pipeline-cleanup",
            )

        assert result is not None
        assert result.created_by == "auto_capture"

    def test_evolve_captured_registers_in_version_store(self, store: SkillVersionStore, tmp_path: Path):
        """evolve_captured registers the new skill in the version store."""
        evolver = self._make_evolver(store, tmp_path)

        with patch.object(evolver, "_call_llm", return_value=SAMPLE_CAPTURED_SKILL_MD):
            result = evolver.evolve_captured(
                pattern_description="Pattern for store registration test",
                task_examples=["Task reg1", "Task reg2"],
                suggested_name="data-pipeline-cleanup",
            )

        assert result is not None
        # Should be retrievable from the version store
        stored = store.get_skill(result.skill_id)
        assert stored is not None
        assert stored.name == "data-pipeline-cleanup"
        assert stored.lineage.origin == Origin.CAPTURED
