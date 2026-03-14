#!/usr/bin/env python3
"""
Validate a Nova-Core skill's SKILL.md frontmatter and structure.

Derived from Anthropic's official skill-creator/scripts/quick_validate.py.
Adapted for Nova-Core's dual skill system (prompt + execution).

Usage:
    python validate_skill.py <path-to-skill-directory>
    python validate_skill.py /home/nova/nova-core/.claude/skills/web-research
    python validate_skill.py /home/nova/nova-core/SKILLS/code_improve
"""

import re
import sys
from pathlib import Path


def parse_frontmatter(content: str) -> tuple[dict, str]:
    """Extract YAML frontmatter from SKILL.md content."""
    if not content.startswith("---"):
        return {}, content

    end = content.find("---", 3)
    if end == -1:
        return {}, content

    fm_text = content[3:end].strip()
    body = content[end + 3 :].strip()

    # Lightweight YAML parsing (no external deps)
    fm = {}
    current_key = None
    current_list = None

    for line in fm_text.split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        indent = len(line) - len(line.lstrip())

        # List item
        if stripped.startswith("- ") and current_list is not None:
            current_list.append(stripped[2:].strip().strip('"').strip("'"))
            continue

        # Top-level group key (e.g. "activation:" at indent 0)
        if stripped.endswith(":") and ":" not in stripped[:-1] and indent == 0:
            current_key = stripped[:-1].strip()
            current_list = None
            continue

        # Any key: value or sub-key
        if ":" in stripped:
            parts = stripped.split(":", 1)
            key = parts[0].strip()
            value = parts[1].strip().strip('"').strip("'")

            # Compose nested key
            full_key = f"{current_key}.{key}" if current_key and indent > 0 else key

            if not value:
                # Sub-key with no value — expect list items next
                current_list = []
                fm[full_key] = current_list
                continue

            if value.startswith("[") and value.endswith("]"):
                items = [v.strip().strip('"').strip("'") for v in value[1:-1].split(",") if v.strip()]
                fm[full_key] = items
            else:
                fm[full_key] = value
            if indent == 0:
                current_key = ""
            current_list = None

    return fm, body


def detect_skill_type(path: Path) -> str:
    """Detect whether this is a prompt skill or execution skill."""
    abs_path = str(path.resolve())
    if ".claude/skills/" in abs_path:
        return "prompt"
    elif "/SKILLS/" in abs_path:
        return "execution"
    return "unknown"


def validate(skill_dir: str) -> list[dict]:
    """Validate a skill and return list of issues."""
    issues = []
    path = Path(skill_dir)

    if not path.is_dir():
        issues.append({"severity": "error", "message": f"Not a directory: {skill_dir}"})
        return issues

    skill_md = path / "SKILL.md"
    if not skill_md.exists():
        issues.append({"severity": "error", "message": "Missing SKILL.md"})
        return issues

    content = skill_md.read_text()
    fm, body = parse_frontmatter(content)
    skill_type = detect_skill_type(path)

    # --- Required fields ---
    if "name" not in fm:
        issues.append({"severity": "error", "message": "Missing required field: name"})
    else:
        name = fm["name"]
        # Kebab-case check
        if not re.match(r"^[a-z][a-z0-9-]*$", name):
            issues.append(
                {
                    "severity": "error",
                    "message": f"Name '{name}' must be kebab-case (lowercase letters, digits, hyphens)",
                }
            )
        # Length check
        if len(name) > 64:
            issues.append(
                {
                    "severity": "error",
                    "message": f"Name '{name}' exceeds 64 characters ({len(name)})",
                }
            )
        # Directory match
        if name != path.name:
            issues.append(
                {
                    "severity": "warning",
                    "message": f"Name '{name}' doesn't match directory name '{path.name}'",
                }
            )

    if "description" not in fm:
        issues.append({"severity": "error", "message": "Missing required field: description"})
    else:
        desc = fm["description"]
        if len(desc) > 1024:
            issues.append(
                {
                    "severity": "error",
                    "message": f"Description exceeds 1024 characters ({len(desc)})",
                }
            )
        if len(desc) < 20:
            issues.append(
                {
                    "severity": "warning",
                    "message": "Description is very short — may not trigger reliably",
                }
            )
        # Check for angle brackets (Anthropic convention)
        if "<" in desc or ">" in desc:
            issues.append(
                {
                    "severity": "warning",
                    "message": "Description contains angle brackets — may be a template placeholder",
                }
            )

    # --- Type-specific checks ---
    if skill_type == "prompt":
        # Check activation keywords
        has_keywords = any(k.startswith("activation") for k in fm)
        if not has_keywords:
            issues.append(
                {
                    "severity": "warning",
                    "message": "No activation.keywords — skill may not trigger via tools/skills.py",
                }
            )

        # Check for output contract
        has_contract = any(k.startswith("output_contract") for k in fm)
        if not has_contract:
            issues.append(
                {
                    "severity": "info",
                    "message": "No output_contract defined — consider adding required output fields",
                }
            )

    elif skill_type == "execution":
        # Check version
        if "version" not in fm:
            issues.append(
                {
                    "severity": "warning",
                    "message": "No version field — execution skills should have semver",
                }
            )

        # Check for CONTRACT block in body
        if "## CONTRACT" not in body and "# Output Contract" not in body:
            issues.append(
                {
                    "severity": "warning",
                    "message": "No Output Contract section found in body",
                }
            )

        # Check for required sections
        required_sections = ["Workflow", "Tool Usage", "Verification", "Failure Handling"]
        for section in required_sections:
            if section not in body:
                issues.append(
                    {
                        "severity": "warning",
                        "message": f"Missing recommended section: {section}",
                    }
                )

    # --- Body checks ---
    lines = body.split("\n")
    line_count = len(lines)

    if line_count > 500:
        issues.append(
            {
                "severity": "warning",
                "message": f"Body is {line_count} lines — consider using progressive disclosure (references/ files)",
            }
        )

    if line_count < 10:
        issues.append(
            {
                "severity": "warning",
                "message": f"Body is only {line_count} lines — may be too sparse",
            }
        )

    # Check for examples
    if "example" not in body.lower() and "## Examples" not in body:
        issues.append(
            {
                "severity": "info",
                "message": "No examples found — consider adding realistic usage examples",
            }
        )

    return issues


def main():
    if len(sys.argv) < 2:
        print("Usage: python validate_skill.py <path-to-skill-directory>")
        print("Example: python validate_skill.py .claude/skills/web-research")
        sys.exit(1)

    skill_dir = sys.argv[1]
    issues = validate(skill_dir)
    skill_type = detect_skill_type(Path(skill_dir))

    print(f"\n{'=' * 60}")
    print(f"Skill Validation: {Path(skill_dir).name}")
    print(f"Type: {skill_type}")
    print(f"{'=' * 60}\n")

    if not issues:
        print("  PASS  No issues found.\n")
        sys.exit(0)

    errors = [i for i in issues if i["severity"] == "error"]
    warnings = [i for i in issues if i["severity"] == "warning"]
    infos = [i for i in issues if i["severity"] == "info"]

    for issue in errors:
        print(f"  ERROR    {issue['message']}")
    for issue in warnings:
        print(f"  WARNING  {issue['message']}")
    for issue in infos:
        print(f"  INFO     {issue['message']}")

    print(f"\n  Summary: {len(errors)} error(s), {len(warnings)} warning(s), {len(infos)} info(s)\n")

    if errors:
        print("  FAIL  Skill has validation errors.\n")
        sys.exit(1)
    else:
        print("  PASS  Skill is valid (with warnings/info).\n")
        sys.exit(0)


if __name__ == "__main__":
    main()
