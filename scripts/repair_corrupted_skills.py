#!/usr/bin/env python3
"""Scan .claude/skills/*/SKILL.md for corrupted files where conversational
preamble text appears before the frontmatter ``---`` delimiter.  Strip the
preamble and rewrite the file.
"""

import argparse
import re
from pathlib import Path


def repair_file(path: Path, dry_run: bool = False) -> bool:
    """Return True if the file was repaired (or would be in dry-run mode)."""
    content = path.read_text(encoding="utf-8")

    # Already clean — starts with frontmatter delimiter.
    if content.startswith("---"):
        return False

    # Look for the first ``---`` line anywhere in the file.
    match = re.search(r"^---\s*$", content, re.MULTILINE)
    if match is None:
        # No frontmatter at all — intentionally body-only, skip.
        return False

    # Corrupted: strip everything before the first ``---`` line.
    repaired = content[match.start() :]

    if dry_run:
        print(f"[DRY-RUN] Would repair: {path}")
    else:
        path.write_text(repaired, encoding="utf-8")
        print(f"Repaired: {path}")

    return True


def main() -> None:
    default_skills_dir = Path(__file__).resolve().parent.parent / ".claude" / "skills"

    parser = argparse.ArgumentParser(
        description="Repair corrupted SKILL.md files that have preamble before frontmatter.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Show what would be repaired without writing changes.",
    )
    parser.add_argument(
        "--skills-dir",
        type=Path,
        default=default_skills_dir,
        help=f"Override the skills directory (default: {default_skills_dir}).",
    )
    args = parser.parse_args()

    skills_dir: Path = args.skills_dir
    skill_files = sorted(skills_dir.glob("*/SKILL.md"))

    repaired = 0
    for skill_file in skill_files:
        if repair_file(skill_file, dry_run=args.dry_run):
            repaired += 1

    label = "would repair" if args.dry_run else "repaired"
    print(f"\n{repaired} files {label} ({len(skill_files)} total scanned)")


if __name__ == "__main__":
    main()
