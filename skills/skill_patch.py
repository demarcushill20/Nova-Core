"""Skill patch application with multi-level fuzzy matching.

Phase 3 of Self-Evolving Skills — pure utility module for applying
patches to skill files (SKILL.md).  Supports two formats:

1. SEARCH/REPLACE blocks with 4-level fuzzy matching
2. Full rewrite (the patch IS the new content)

Also provides governance checking to enforce mutation_policy.yaml
constraints during skill evolution.

IMPORTANT: stdlib only — no external dependencies.
"""

from __future__ import annotations

import difflib
import logging
import re
from difflib import SequenceMatcher
from pathlib import Path

logger = logging.getLogger(__name__)


class PatchError(Exception):
    """Raised when a patch cannot be applied."""

    pass


class GovernanceError(Exception):
    """Raised when a patch violates governance policy."""

    pass


def apply_patch(
    file_path: str,
    patch_text: str,
    governance_check: callable = None,
) -> str:
    """Apply a patch to a file. Auto-detects patch format.

    Returns the new file content after applying the patch.
    Raises PatchError if the patch cannot be applied.
    Raises GovernanceError if the patch violates governance policy.
    """
    content = Path(file_path).read_text(encoding="utf-8")

    # Auto-detect patch format
    if _is_search_replace(patch_text):
        new_content = _apply_search_replace(content, patch_text)
    else:
        # FULL rewrite — validate it looks like real content
        if len(patch_text.strip()) < 50:
            raise PatchError(
                "Full rewrite content is too short to be valid "
                f"({len(patch_text.strip())} chars, minimum 50)"
            )
        new_content = patch_text

    # Run governance check if provided
    if governance_check:
        violations = governance_check(content, new_content)
        if violations:
            raise GovernanceError(
                f"Governance violations: {'; '.join(violations)}"
            )

    return new_content


def _is_search_replace(patch_text: str) -> bool:
    """Detect if patch uses SEARCH/REPLACE format."""
    return "<<<<<<< SEARCH" in patch_text and ">>>>>>> REPLACE" in patch_text


def _apply_search_replace(content: str, patch_text: str) -> str:
    """Apply SEARCH/REPLACE blocks with 4-level fuzzy matching.

    Format::

        <<<<<<< SEARCH
        old content here
        =======
        new content here
        >>>>>>> REPLACE
    """
    # Parse all SEARCH/REPLACE blocks
    pattern = r"<<<<<<< SEARCH\n(.*?)\n=======\n(.*?)\n>>>>>>> REPLACE"
    blocks = re.findall(pattern, patch_text, re.DOTALL)

    if not blocks:
        raise PatchError("No valid SEARCH/REPLACE blocks found in patch")

    result = content
    for search_text, replace_text in blocks:
        result = _fuzzy_replace(result, search_text, replace_text)

    return result


def _fuzzy_replace(content: str, search: str, replace: str) -> str:
    """4-level fuzzy matching for SEARCH/REPLACE.

    Levels:
    1. Exact match
    2. Trimmed (strip leading/trailing whitespace per line)
    3. Whitespace-normalized (collapse all whitespace to single space)
    4. Indentation-flexible (match ignoring indentation, preserve original)
    """
    # Level 1: Exact match
    if search in content:
        return content.replace(search, replace, 1)

    # Level 2: Trimmed match
    result = _trimmed_replace(content, search, replace)
    if result is not None:
        return result

    # Level 3: Whitespace-normalized match
    result = _whitespace_normalized_replace(content, search, replace)
    if result is not None:
        return result

    # Level 4: Indentation-flexible match (using SequenceMatcher)
    result = _indentation_flexible_replace(content, search, replace)
    if result is not None:
        return result

    raise PatchError(
        f"Could not match search block (tried 4 fuzzy levels):\n{search[:200]}"
    )


def _trimmed_replace(
    content: str, search: str, replace: str
) -> str | None:
    """Level 2: Strip leading/trailing whitespace from each line before matching."""
    content_lines = content.split("\n")
    search_lines = [line.strip() for line in search.split("\n")]

    # Find the search block in content (by trimmed comparison)
    for i in range(len(content_lines) - len(search_lines) + 1):
        window = [
            line.strip() for line in content_lines[i : i + len(search_lines)]
        ]
        if window == search_lines:
            # Replace the matched lines
            new_lines = (
                content_lines[:i]
                + replace.split("\n")
                + content_lines[i + len(search_lines) :]
            )
            return "\n".join(new_lines)

    return None


def _whitespace_normalized_replace(
    content: str, search: str, replace: str
) -> str | None:
    """Level 3: Collapse all whitespace to single space before matching."""

    def normalize(s: str) -> str:
        return re.sub(r"\s+", " ", s)  # NO .strip() — preserve leading offset

    content_normalized = normalize(content)
    search_normalized = normalize(search).strip()  # only strip the search pattern

    idx = content_normalized.find(search_normalized)
    if idx == -1:
        return None

    # Find the actual position in the original content
    # Map normalized index back to original
    orig_idx = _map_normalized_index(content, idx)
    orig_end = _map_normalized_index(content, idx + len(search_normalized))

    if orig_idx is not None and orig_end is not None:
        return content[:orig_idx] + replace + content[orig_end:]

    return None


def _map_normalized_index(original: str, normalized_idx: int) -> int | None:
    """Map an index in normalized (whitespace-collapsed) text back to original text."""
    norm_pos = 0
    in_whitespace = False

    for i, ch in enumerate(original):
        if norm_pos >= normalized_idx:
            return i
        if ch in " \t\n\r":
            if not in_whitespace:
                norm_pos += 1  # One space in normalized
                in_whitespace = True
        else:
            norm_pos += 1
            in_whitespace = False

    if norm_pos >= normalized_idx:
        return len(original)
    return None


def _indentation_flexible_replace(
    content: str, search: str, replace: str
) -> str | None:
    """Level 4: Match ignoring indentation differences using SequenceMatcher."""
    content_lines = content.split("\n")
    search_lines = search.split("\n")

    if not search_lines:
        return None

    # Strip all indentation for matching
    search_stripped = [line.lstrip() for line in search_lines]

    best_ratio = 0.0
    best_start = -1

    for i in range(len(content_lines) - len(search_lines) + 1):
        window = content_lines[i : i + len(search_lines)]
        window_stripped = [line.lstrip() for line in window]

        # Compare stripped versions
        s1 = "\n".join(window_stripped)
        s2 = "\n".join(search_stripped)
        ratio = SequenceMatcher(None, s1, s2).ratio()

        if ratio > best_ratio:
            best_ratio = ratio
            best_start = i

    # Require at least 85% similarity
    if best_ratio >= 0.85 and best_start >= 0:
        # Detect indentation of matched block
        matched_lines = content_lines[
            best_start : best_start + len(search_lines)
        ]
        base_indent = _detect_indent(matched_lines)

        # Apply indentation to replacement
        replace_lines = replace.split("\n")
        indented_replace = _apply_indent(replace_lines, base_indent)

        new_lines = (
            content_lines[:best_start]
            + indented_replace
            + content_lines[best_start + len(search_lines) :]
        )
        return "\n".join(new_lines)

    return None


def _detect_indent(lines: list[str]) -> str:
    """Detect the common leading indentation of a block of lines."""
    non_empty = [line for line in lines if line.strip()]
    if not non_empty:
        return ""

    # Find minimum indentation
    indents = []
    for line in non_empty:
        stripped = line.lstrip()
        indent = line[: len(line) - len(stripped)]
        indents.append(indent)

    if not indents:
        return ""

    # Return the shortest common indent
    return min(indents, key=len)


def _apply_indent(lines: list[str], base_indent: str) -> list[str]:
    """Apply base indentation to all lines, preserving relative indentation."""
    if not lines:
        return lines

    # Detect existing indent of replacement
    non_empty = [line for line in lines if line.strip()]
    if non_empty:
        existing_indent = len(non_empty[0]) - len(non_empty[0].lstrip())
    else:
        existing_indent = 0

    result = []
    for line in lines:
        if not line.strip():
            result.append("")
        else:
            # Remove existing base indent, add new base indent
            stripped_indent = len(line) - len(line.lstrip())
            relative_indent = max(0, stripped_indent - existing_indent)
            result.append(
                base_indent + " " * relative_indent + line.lstrip()
            )

    return result


def check_governance(
    old_content: str,
    new_content: str,
    forbidden_targets: list[str] | None = None,
) -> list[str]:
    """Check if a patch violates governance rules.

    Args:
        old_content: Original file content
        new_content: Patched file content
        forbidden_targets: List of forbidden mutation targets from mutation_policy.yaml

    Returns:
        List of violation descriptions. Empty list means no violations.
    """
    violations = []

    if forbidden_targets is None:
        forbidden_targets = []

    # Check forbidden sections by looking for section headers in the diff
    for target in forbidden_targets:
        target_lower = target.lower().replace("_", " ").replace("-", " ")
        # Check if the diff region touches a forbidden section
        if _section_modified(old_content, new_content, target_lower):
            violations.append(
                f"Modification touches forbidden target: {target}"
            )

    return violations


def _section_modified(old: str, new: str, section_name: str) -> bool:
    """Check if a named section was modified between old and new content.

    Uses two strategies:
    1. Direct: changed lines (+/-) that themselves contain the section name
    2. Section membership: identify which YAML/Markdown section each line
       belongs to (based on top-level keys), and check if any changed line
       falls within the forbidden section
    """

    def _normalize(s: str) -> str:
        return s.lower().strip().replace("_", " ").replace("-", " ")

    old_lines = old.split("\n")
    new_lines = new.split("\n")

    diff = list(difflib.unified_diff(old_lines, new_lines, lineterm=""))

    # Strategy 1: changed line directly contains the section name
    for line in diff:
        if line.startswith("+") or line.startswith("-"):
            # Skip diff headers (--- and +++ lines)
            if line.startswith("---") or line.startswith("+++"):
                continue
            if section_name in _normalize(line[1:]):
                return True

    # Strategy 2: check if changed lines in the old/new content fall
    # within a section headed by the forbidden target.
    # Build a section map for the old content.
    def _build_section_map(lines: list[str]) -> dict[int, str]:
        """Map line index -> section name (normalized).

        Handles YAML frontmatter delimiters (---) as section boundaries
        that reset the current section to empty (body content).
        """
        section_map: dict[int, str] = {}
        current_section = ""
        frontmatter_delim_count = 0
        for i, line in enumerate(lines):
            stripped = line.strip()
            # Track frontmatter delimiters
            if stripped == "---":
                frontmatter_delim_count += 1
                if frontmatter_delim_count >= 2:
                    # Closing delimiter — body starts, reset section
                    current_section = ""
                section_map[i] = current_section
                continue
            # Detect top-level keys (no leading whitespace, has ':')
            if stripped and not line[0].isspace() and ":" in stripped:
                key = stripped.split(":", 1)[0]
                current_section = _normalize(key)
            section_map[i] = current_section
        return section_map

    old_sections = _build_section_map(old_lines)
    new_sections = _build_section_map(new_lines)

    # Find which lines changed
    import difflib as _dl

    matcher = _dl.SequenceMatcher(None, old_lines, new_lines)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        # Check if removed lines were in the forbidden section
        if tag in ("delete", "replace"):
            for i in range(i1, i2):
                if old_sections.get(i, "") == section_name:
                    return True
        # Check if added lines are in the forbidden section
        if tag in ("insert", "replace"):
            for j in range(j1, j2):
                if new_sections.get(j, "") == section_name:
                    return True

    return False
