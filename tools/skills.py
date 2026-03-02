"""Skill Activation Engine — discover, select, and render SKILL.md files.

Scans .claude/skills/*/SKILL.md, parses YAML frontmatter, selects relevant
skills based on task text, and renders an append-system-prompt string.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SKILLS_DIR = BASE_DIR / ".claude" / "skills"

MAX_APPEND_BYTES = 60 * 1024  # 60 KB hard cap

# --- Always-on skills (injected regardless of task text) ---
ALWAYS_INCLUDE = {"task-execution", "self-verification"}

# --- Built-in keyword rules (checked case-insensitively) ---
_BUILTIN_RULES: dict[str, list[str]] = {
    "git-ops":   ["git", "commit", "branch", "merge"],
    "file-ops":  ["file", "read", "write", "edit", "diff", "patch", "path",
                  ".py", ".md", ".json", ".yaml", ".yml", ".txt", ".csv",
                  ".toml", ".cfg", ".ini", ".sh"],
}

# Regex for $-prefixed command lines (e.g. "$ ls -la")
_SHELL_CMD_RE = re.compile(r"(?m)^\s*\$\s+\S")

# shell-ops: explicit command names (word-boundary, safe — no false positives)
_SHELL_CMDS_RE = re.compile(
    r"\b(?:bash|sudo"
    r"|ls|cat|grep|sed|tail|head"
    r"|journalctl|systemctl|chmod|chown|apt|pip|python)\b",
    re.IGNORECASE,
)

# shell-ops: intent words — only trigger when paired with action context
# "shell" alone in "no shell commands" should NOT match; require e.g. "run shell"
_SHELL_INTENT_RE = re.compile(
    r"\b(?:run|execute|use|open|launch|start)\b"
    r".*\b(?:terminal|command|shell)\b"
    r"|\b(?:terminal|command|shell)\b"
    r".*\b(?:run|execute|use|open|launch|start)\b",
    re.IGNORECASE,
)


def _has_shell_intent(text: str) -> bool:
    """Return True if text shows explicit shell/command intent."""
    if _SHELL_CMD_RE.search(text):
        return True
    if _SHELL_CMDS_RE.search(text):
        return True
    if _SHELL_INTENT_RE.search(text):
        return True
    return False


@dataclass
class Skill:
    """Parsed skill from a SKILL.md file."""
    name: str
    description: str
    tags: list[str] = field(default_factory=list)
    version: str = ""
    keywords: list[str] = field(default_factory=list)
    body: str = ""          # full SKILL.md content with frontmatter stripped
    raw: str = ""           # full SKILL.md content including frontmatter
    path: Path = field(default_factory=Path)


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Extract YAML frontmatter (between --- fences) and the remaining body.

    Returns (meta_dict, body_text).  Handles simple key: value and
    nested activation.keywords lists without requiring PyYAML.
    """
    if not text.startswith("---"):
        return {}, text

    end = text.find("\n---", 3)
    if end == -1:
        return {}, text

    fm_block = text[3:end].strip()
    body = text[end + 4:].lstrip("\n")

    meta: dict = {}
    current_key = ""
    current_list: list[str] | None = None

    for line in fm_block.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        # List item under a key (e.g. "  - keyword")
        if stripped.startswith("- ") and current_list is not None:
            current_list.append(stripped[2:].strip().strip('"').strip("'"))
            continue

        # Nested key start (e.g. "activation:")
        if stripped.endswith(":") and ":" not in stripped[:-1]:
            current_key = stripped[:-1].strip()
            current_list = None
            continue

        # Top-level or nested key: value
        if ":" in stripped:
            key, _, val = stripped.partition(":")
            key = key.strip()
            val = val.strip().strip('"').strip("'")

            # Handle nested keys like "activation.keywords"
            full_key = f"{current_key}.{key}" if current_key and not val else key
            if current_key and not val:
                # This is a sub-key with no value — expect list items next
                current_list = []
                meta[full_key] = current_list
                continue

            if val.startswith("[") and val.endswith("]"):
                # Inline list: [a, b, c]
                items = [v.strip().strip('"').strip("'")
                         for v in val[1:-1].split(",") if v.strip()]
                meta[key] = items
            else:
                meta[key] = val
            current_key = "" if not current_key else current_key
            current_list = None

    return meta, body


def load_skills() -> list[Skill]:
    """Discover and parse all SKILL.md files under .claude/skills/."""
    skills: list[Skill] = []
    if not SKILLS_DIR.is_dir():
        return skills

    for skill_dir in sorted(SKILLS_DIR.iterdir()):
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.is_file():
            continue

        raw = skill_file.read_text(encoding="utf-8")
        meta, body = _parse_frontmatter(raw)

        name = meta.get("name", skill_dir.name)
        keywords_raw = meta.get("activation.keywords", [])
        if isinstance(keywords_raw, str):
            keywords_raw = [k.strip() for k in keywords_raw.split(",") if k.strip()]

        skills.append(Skill(
            name=name,
            description=meta.get("description", ""),
            tags=[t.strip() for t in meta.get("tags", "").split(",") if t.strip()]
                 if isinstance(meta.get("tags"), str) else meta.get("tags", []),
            version=meta.get("version", ""),
            keywords=keywords_raw,
            body=body,
            raw=raw,
            path=skill_file,
        ))

    return skills


def select_skills(task_text: str, skills: list[Skill] | None = None) -> list[Skill]:
    """Select relevant skills for a given task text.

    Selection logic:
      1. Always include ALWAYS_INCLUDE skills.
      2. Match built-in keyword rules (_BUILTIN_RULES).
      3. Match activation.keywords from frontmatter.
      4. Shell-command regex heuristic for shell-ops.

    Returns skills sorted by name for deterministic ordering.
    """
    if skills is None:
        skills = load_skills()

    text_lower = task_text.lower()
    selected_names: set[str] = set(ALWAYS_INCLUDE)

    # Built-in keyword rules (substring match — safe for longer tokens)
    for skill_name, keywords in _BUILTIN_RULES.items():
        for kw in keywords:
            if kw.lower() in text_lower:
                selected_names.add(skill_name)
                break

    # Shell-ops: token-based intent matching
    if _has_shell_intent(task_text):
        selected_names.add("shell-ops")

    # Activation keywords from frontmatter
    for skill in skills:
        if skill.name in selected_names:
            continue
        for kw in skill.keywords:
            if kw.lower() in text_lower:
                selected_names.add(skill.name)
                break

    # Filter and sort
    return sorted(
        [s for s in skills if s.name in selected_names],
        key=lambda s: s.name,
    )


def render_append_prompt(skills: list[Skill]) -> str:
    """Render selected skills into a single append-system-prompt string.

    Format:
        ## ACTIVE SKILLS
        --- <skill-name> ---
        <body>
        --- end <skill-name> ---

    Truncates at MAX_APPEND_BYTES with a note.
    """
    if not skills:
        return ""

    parts: list[str] = ["## ACTIVE SKILLS\n"]

    for skill in sorted(skills, key=lambda s: s.name):
        block = (
            f"--- {skill.name} ---\n"
            f"{skill.body.strip()}\n"
            f"--- end {skill.name} ---\n"
        )
        parts.append(block)

    result = "\n".join(parts)

    if len(result.encode("utf-8")) > MAX_APPEND_BYTES:
        # Truncate to fit, leaving room for the note
        note = "\n\n[TRUNCATED — skill prompt exceeded 60KB limit]\n"
        budget = MAX_APPEND_BYTES - len(note.encode("utf-8"))
        encoded = result.encode("utf-8")[:budget]
        result = encoded.decode("utf-8", errors="ignore") + note

    return result
