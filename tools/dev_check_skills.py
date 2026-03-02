#!/usr/bin/env python3
"""Dev tool: print which skills would be selected for a given task file.

Usage:
    python tools/dev_check_skills.py TASKS/0004_real_autonomy.md.done
    python tools/dev_check_skills.py - <<< "commit the changes and push"
"""

import sys
from pathlib import Path

# Ensure project root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.skills import load_skills, select_skills, render_append_prompt


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <task-file-or-'-'>")
        print("  Pass '-' to read from stdin.")
        sys.exit(1)

    arg = sys.argv[1]
    if arg == "-":
        task_text = sys.stdin.read()
        source = "<stdin>"
    else:
        path = Path(arg)
        if not path.is_file():
            print(f"Error: {arg} is not a file")
            sys.exit(1)
        task_text = path.read_text(encoding="utf-8")[:50 * 1024]
        source = str(path)

    all_skills = load_skills()
    print(f"Loaded {len(all_skills)} skill(s): {', '.join(s.name for s in all_skills)}")
    print(f"Task source: {source} ({len(task_text)} chars)")
    print()

    selected = select_skills(task_text, all_skills)
    print(f"Selected {len(selected)} skill(s):")
    for s in selected:
        reason = "always-on" if s.name in {"task-execution", "self-verification"} else "keyword match"
        print(f"  [{reason}] {s.name}: {s.description[:80]}")

    print()
    rendered = render_append_prompt(selected)
    print(f"Rendered prompt size: {len(rendered.encode('utf-8'))} bytes")

    if "--render" in sys.argv:
        print()
        print("=== RENDERED PROMPT ===")
        print(rendered)


if __name__ == "__main__":
    main()
