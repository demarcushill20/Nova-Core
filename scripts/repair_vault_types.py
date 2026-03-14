#!/usr/bin/env python3
"""Repair script for vault note frontmatter schema violations.

Phase 0 deliverable — scans all vault notes, validates frontmatter against
the canonical schema, and optionally repairs fixable violations.

Usage:
    python3 scripts/repair_vault_types.py              # Dry-run audit only
    python3 scripts/repair_vault_types.py --repair      # Apply fixes
    python3 scripts/repair_vault_types.py --verbose      # Show valid notes too
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from schemas.vault_note_schema import (  # noqa: E402
    VALID_PLAN_STATUSES,
    validate_frontmatter,
)

# Project root for later use
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Default vault root
VAULT_ROOT = Path("/home/nova/nova-vault")

# YAML frontmatter extraction (simple, no pyyaml dependency)
_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def parse_frontmatter(text: str) -> dict | None:
    """Extract YAML frontmatter from markdown text.

    Uses a simple line-based parser to avoid requiring pyyaml.
    Handles scalar values, lists (- item), and quoted strings.
    """
    match = _FM_RE.match(text)
    if not match:
        return None

    fm: dict = {}
    current_key: str | None = None
    current_list: list[str] | None = None

    for line in match.group(1).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        # List item continuation
        if stripped.startswith("- ") and current_key is not None and current_list is not None:
            val = stripped[2:].strip().strip("'\"")
            current_list.append(val)
            fm[current_key] = current_list
            continue

        # Key: value line
        if ":" in stripped:
            # Flush any pending list
            current_list = None

            key, _, val = stripped.partition(":")
            key = key.strip()
            val = val.strip()

            if not val:
                # Could be start of a list or empty value
                current_key = key
                current_list = []
                fm[key] = current_list
                continue

            # Strip quotes
            if (val.startswith("'") and val.endswith("'")) or (val.startswith('"') and val.endswith('"')):
                val = val[1:-1]

            # Detect inline list [a, b, c]
            if val.startswith("[") and val.endswith("]"):
                items = [v.strip().strip("'\"") for v in val[1:-1].split(",") if v.strip()]
                fm[key] = items
                current_key = key
                current_list = None
                continue

            # Integer detection
            if val.isdigit():
                fm[key] = int(val)
            elif val.lower() in ("true", "false"):
                fm[key] = val.lower() == "true"
            else:
                fm[key] = val

            current_key = key
            current_list = None
        else:
            current_list = None

    return fm


def scan_vault(vault_root: Path, verbose: bool = False) -> list[dict]:
    """Scan all .md files in vault and validate frontmatter.

    Returns list of result dicts with: path, valid, errors, frontmatter, fixable.
    """
    results: list[dict] = []

    for md_file in sorted(vault_root.rglob("*.md")):
        # Skip hidden/meta directories
        rel = md_file.relative_to(vault_root)
        if any(part.startswith(".") or part == "_meta" for part in rel.parts):
            continue

        try:
            text = md_file.read_text(encoding="utf-8")
        except Exception as exc:
            results.append(
                {
                    "path": str(rel),
                    "valid": False,
                    "errors": [f"read error: {exc}"],
                    "frontmatter": None,
                    "fixable": False,
                }
            )
            continue

        fm = parse_frontmatter(text)
        if fm is None:
            results.append(
                {
                    "path": str(rel),
                    "valid": False,
                    "errors": ["no frontmatter found"],
                    "frontmatter": None,
                    "fixable": False,
                }
            )
            continue

        valid, errors = validate_frontmatter(fm)

        # Determine if errors are auto-fixable
        fixable = False
        if not valid:
            fixable = all(_is_fixable(e, fm) for e in errors)

        results.append(
            {
                "path": str(rel),
                "valid": valid,
                "errors": errors,
                "frontmatter": fm,
                "fixable": fixable,
            }
        )

    return results


def _is_fixable(error: str, fm: dict) -> bool:
    """Check if a validation error can be automatically repaired."""
    # Missing type tag in tags list — can add it
    if "tags must include" in error:
        return True
    # Invalid status on implementation-plan — can be mapped
    return "invalid status" in error


def _apply_fix(md_file: Path, fm: dict, errors: list[str]) -> list[str]:
    """Apply automatic fixes to a vault note. Returns list of fixes applied."""
    text = md_file.read_text(encoding="utf-8")
    fixes_applied: list[str] = []

    for error in errors:
        # Fix: missing type tag in tags list
        match = re.search(r"tags must include '(#[^']+)'", error)
        if match:
            required_tag = match.group(1)
            # Add the tag to the tags list in frontmatter
            tag_line_re = re.compile(r"^(tags:\s*)$", re.MULTILINE)
            if tag_line_re.search(text):
                # tags: is a block list — add as first item
                text = tag_line_re.sub(f"tags:\n  - '{required_tag}'", text, count=1)
                fixes_applied.append(f"added {required_tag} to tags list")
            continue

        # Fix: invalid status — map known typos
        if "invalid status" in error:
            status_val = fm.get("status", "")
            mapped = _map_status(status_val)
            if mapped and mapped != status_val:
                text = text.replace(f"status: {status_val}", f"status: {mapped}", 1)
                fixes_applied.append(f"status: {status_val!r} → {mapped!r}")
            continue

    if fixes_applied:
        tmp = md_file.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.rename(md_file)

    return fixes_applied


def _map_status(raw: str) -> str | None:
    """Map a status string to canonical value."""
    raw_lower = raw.strip().lower()
    mapping = {
        "done": "completed",
        "complete": "completed",
        "finished": "completed",
        "in_progress": "active",
        "in-progress": "active",
        "wip": "active",
        "todo": "backlog",
        "pending": "backlog",
        "not-started": "backlog",
        "not_started": "backlog",
        "blocked": "paused",
        "on_hold": "paused",
        "on-hold": "paused",
    }
    if raw_lower in VALID_PLAN_STATUSES:
        return raw_lower
    return mapping.get(raw_lower)


def main():
    parser = argparse.ArgumentParser(description="Audit and repair vault note frontmatter")
    parser.add_argument("--vault", type=Path, default=VAULT_ROOT, help="Vault root directory")
    parser.add_argument("--repair", action="store_true", help="Apply automatic fixes")
    parser.add_argument("--verbose", action="store_true", help="Show valid notes too")
    args = parser.parse_args()

    if not args.vault.is_dir():
        print(f"ERROR: Vault root not found: {args.vault}", file=sys.stderr)
        sys.exit(1)

    print(f"Scanning vault: {args.vault}")
    print(f"Mode: {'REPAIR' if args.repair else 'AUDIT (dry-run)'}")
    print()

    results = scan_vault(args.vault, verbose=args.verbose)

    valid_count = 0
    invalid_count = 0
    fixable_count = 0
    fixed_count = 0
    no_frontmatter_count = 0

    for r in results:
        if r["frontmatter"] is None and "no frontmatter" in str(r["errors"]):
            no_frontmatter_count += 1
            if args.verbose:
                print(f"  SKIP  {r['path']} — no frontmatter")
            continue

        if r["valid"]:
            valid_count += 1
            if args.verbose:
                note_type = r["frontmatter"].get("type", "?") if r["frontmatter"] else "?"
                print(f"  OK    {r['path']} (type={note_type})")
        else:
            invalid_count += 1
            print(f"  FAIL  {r['path']}")
            for e in r["errors"]:
                print(f"        → {e}")

            if r["fixable"]:
                fixable_count += 1
                if args.repair:
                    full_path = args.vault / r["path"]
                    fixes = _apply_fix(full_path, r["frontmatter"], r["errors"])
                    if fixes:
                        fixed_count += 1
                        for fix in fixes:
                            print(f"        FIXED: {fix}")
                else:
                    print("        (fixable — run with --repair)")

    print()
    print("=" * 60)
    print(f"Total notes scanned:  {len(results)}")
    print(f"  Valid:              {valid_count}")
    print(f"  Invalid:            {invalid_count}")
    print(f"  No frontmatter:     {no_frontmatter_count}")
    print(f"  Fixable:            {fixable_count}")
    if args.repair:
        print(f"  Fixed:              {fixed_count}")
    print("=" * 60)

    # Exit code: 0 if all valid, 1 if any invalid remain
    remaining_invalid = invalid_count - fixed_count
    sys.exit(0 if remaining_invalid == 0 else 1)


if __name__ == "__main__":
    main()
