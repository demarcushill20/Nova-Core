"""Enhanced tests for tools/skills.py — core skills system coverage.

Covers skill loading, selection, rendering, frontmatter parsing, and edge cases.
"""

from unittest.mock import patch

from tools.skills import (
    ALWAYS_INCLUDE,
    MAX_APPEND_BYTES,
    Skill,
    _has_shell_intent,
    _parse_frontmatter,
    load_skills,
    render_append_prompt,
    select_skills,
)


class TestFrontmatterParsing:
    """Test YAML-like frontmatter parsing."""

    def test_parse_simple_kv(self):
        text = "---\nname: my-skill\ndescription: A test skill\n---\nBody text."
        meta, body = _parse_frontmatter(text)
        assert meta["name"] == "my-skill"
        assert meta["description"] == "A test skill"
        assert body == "Body text."

    def test_parse_no_frontmatter(self):
        text = "No frontmatter here."
        meta, body = _parse_frontmatter(text)
        assert meta == {}
        assert body == text

    def test_parse_inline_list(self):
        text = "---\ntags: [a, b, c]\n---\nBody."
        meta, body = _parse_frontmatter(text)
        assert meta["tags"] == ["a", "b", "c"]

    def test_parse_nested_keyword_list(self):
        text = "---\nactivation:\n  keywords:\n    - deploy\n    - release\n---\nBody."
        meta, body = _parse_frontmatter(text)
        assert meta["activation.keywords"] == ["deploy", "release"]

    def test_parse_empty_frontmatter(self):
        text = "---\n---\nJust body."
        meta, body = _parse_frontmatter(text)
        assert meta == {}
        assert body == "Just body."

    def test_parse_missing_end_fence(self):
        text = "---\nname: broken\nNo closing fence."
        meta, body = _parse_frontmatter(text)
        assert meta == {}


class TestShellIntent:
    """Test shell-command intent detection."""

    def test_dollar_prefix(self):
        assert _has_shell_intent("$ ls -la") is True

    def test_explicit_command(self):
        assert _has_shell_intent("run bash to check logs") is True
        assert _has_shell_intent("use pip install") is True

    def test_intent_phrases(self):
        assert _has_shell_intent("run the terminal command") is True
        assert _has_shell_intent("execute a shell script") is True

    def test_no_intent(self):
        assert _has_shell_intent("write a function to sort numbers") is False
        assert _has_shell_intent("read the documentation") is False

    def test_no_false_positive_on_shell_alone(self):
        # "no shell commands" should NOT trigger
        assert _has_shell_intent("no shell commands allowed") is False


class TestLoadSkills:
    """Test skill discovery from filesystem."""

    def test_load_from_missing_dir(self, tmp_path):
        with patch("tools.skills.SKILLS_DIR", tmp_path / "nonexistent"):
            result = load_skills()
            assert result == []

    def test_load_from_empty_dir(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        with patch("tools.skills.SKILLS_DIR", skills_dir):
            result = load_skills()
            assert result == []

    def test_load_single_skill(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skill_dir = skills_dir / "test-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("---\nname: test-skill\ndescription: A test\n---\nDo the thing.")
        with patch("tools.skills.SKILLS_DIR", skills_dir):
            result = load_skills()
            assert len(result) == 1
            assert result[0].name == "test-skill"
            assert result[0].description == "A test"
            assert result[0].body == "Do the thing."

    def test_load_skill_with_keywords(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skill_dir = skills_dir / "deploy"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: deploy\nactivation:\n  keywords:\n    - deploy\n    - release\n---\nDeploy stuff."
        )
        with patch("tools.skills.SKILLS_DIR", skills_dir):
            result = load_skills()
            assert result[0].keywords == ["deploy", "release"]

    def test_skip_dir_without_skill_md(self, tmp_path):
        skills_dir = tmp_path / "skills"
        (skills_dir / "no-skill-file").mkdir(parents=True)
        (skills_dir / "has-skill").mkdir(parents=True)
        (skills_dir / "has-skill" / "SKILL.md").write_text("---\nname: has-skill\n---\nBody.")
        with patch("tools.skills.SKILLS_DIR", skills_dir):
            result = load_skills()
            assert len(result) == 1
            assert result[0].name == "has-skill"

    def test_fallback_name_from_dirname(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skill_dir = skills_dir / "dirname-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("---\ndescription: no name key\n---\nBody.")
        with patch("tools.skills.SKILLS_DIR", skills_dir):
            result = load_skills()
            assert result[0].name == "dirname-skill"


class TestSelectSkills:
    """Test skill selection logic."""

    def _make_skill(self, name, keywords=None):
        return Skill(name=name, description="", keywords=keywords or [])

    def test_always_include(self):
        skills = [self._make_skill(n) for n in ALWAYS_INCLUDE]
        selected = select_skills("anything", skills)
        selected_names = {s.name for s in selected}
        assert ALWAYS_INCLUDE.issubset(selected_names)

    def test_builtin_git_rule(self):
        skills = [self._make_skill("git-ops")]
        selected = select_skills("commit the changes", skills)
        assert any(s.name == "git-ops" for s in selected)

    def test_builtin_file_rule(self):
        skills = [self._make_skill("file-ops")]
        selected = select_skills("edit file config.yaml", skills)
        assert any(s.name == "file-ops" for s in selected)

    def test_shell_intent_triggers(self):
        skills = [self._make_skill("shell-ops")]
        selected = select_skills("$ ls -la", skills)
        assert any(s.name == "shell-ops" for s in selected)

    def test_activation_keywords(self):
        skills = [self._make_skill("deploy-skill", keywords=["deploy", "release"])]
        selected = select_skills("deploy the service", skills)
        assert any(s.name == "deploy-skill" for s in selected)

    def test_no_match(self):
        skills = [self._make_skill("unrelated", keywords=["quantum"])]
        selected = select_skills("make a sandwich", skills)
        selected_names = {s.name for s in selected}
        assert "unrelated" not in selected_names

    def test_sorted_output(self):
        skills = [self._make_skill(n) for n in ["zebra", "alpha", "middle"]]
        # Force all selected via ALWAYS_INCLUDE
        for s in skills:
            ALWAYS_INCLUDE.add(s.name)
        try:
            selected = select_skills("test", skills)
            names = [s.name for s in selected]
            assert names == sorted(names)
        finally:
            for s in skills:
                ALWAYS_INCLUDE.discard(s.name)


class TestRenderAppendPrompt:
    """Test skill prompt rendering."""

    def test_empty_skills(self):
        assert render_append_prompt([]) == ""

    def test_single_skill(self):
        skill = Skill(name="test", description="", body="Do stuff.")
        result = render_append_prompt([skill])
        assert "## ACTIVE SKILLS" in result
        assert "--- test ---" in result
        assert "Do stuff." in result
        assert "--- end test ---" in result

    def test_multiple_skills_sorted(self):
        s1 = Skill(name="beta", description="", body="B content.")
        s2 = Skill(name="alpha", description="", body="A content.")
        result = render_append_prompt([s1, s2])
        alpha_pos = result.index("--- alpha ---")
        beta_pos = result.index("--- beta ---")
        assert alpha_pos < beta_pos

    def test_truncation_at_max_bytes(self):
        body = "x" * (MAX_APPEND_BYTES + 1000)
        skill = Skill(name="big", description="", body=body)
        result = render_append_prompt([skill])
        assert len(result.encode("utf-8")) <= MAX_APPEND_BYTES
        assert "TRUNCATED" in result
