---
name: config-toggle
description: "Fast read-edit-verify cycle for simple config flag or value changes — completes in 3-5 turns with no ceremony."
activation:
  keywords:
    - toggle
    - config
    - flag
    - enable
    - disable
    - turn on
    - turn off
    - set to true
    - set to false
    - dry run
    - switch
    - flip
  when:
    - User asks to change a single config value, flag, or default
    - Task is a one-line edit to a known file
    - No multi-step planning or review is needed
tool_doctrine:
  files:
    workflow:
      - read_target
      - edit_one_line
      - read_back_verify
output_contract:
  required:
    - file_changed
    - old_value
    - new_value
---

# Config Toggle

Fast, minimal workflow for single config/flag changes. No task lifecycle, no output report, no planning phase.

# When To Use

- User asks to flip a boolean flag (`true` → `false` or vice versa)
- User asks to change a single config value (a number, string, or enum)
- The change is a one-line edit in a known config file
- No downstream verification (tests, builds, deploys) is required as part of the toggle itself

# When NOT To Use

- The change spans multiple files or multiple values
- The change requires understanding side effects or running tests
- The user is asking for a new feature, not a value change
- The config file doesn't exist yet

# Workflow

1. **Read** — Read the target file to locate the current value.
2. **Edit** — Make the single-line change using the Edit tool.
3. **Verify** — Read the file back to confirm the edit landed correctly.
4. **Report** — One-line confirmation with old and new values.

That's it. Four steps, no extras.

# Example

User: "Turn dry_run to false"

```
1. Read novatrade/config.py → find `dry_run: bool = True`
2. Edit: `dry_run: bool = True` → `dry_run: bool = False`
3. Read back → confirm line now says `dry_run: bool = False`
4. Reply: "Done — `dry_run` changed from `True` to `False` in novatrade/config.py"
```

# Tool Usage Rules

- Use **Read** to find the current value. Never guess the current state.
- Use **Edit** with exact string match for the change. Never use Write for a one-line toggle.
- Use **Read** again to verify. Never skip verification.
- Do not touch surrounding code, add comments, or refactor nearby lines.

# Error Handling

- **Value not found**: Report the expected location and ask the user to clarify which file/field.
- **Ambiguous match**: If the value appears in multiple places, list them and ask which one to change.
- **Edit fails** (non-unique string): Expand the context in `old_string` to make the match unique and retry once.

# Output Format

After the edit, reply with a single line:

```
Done — `<field>` changed from `<old>` to `<new>` in <file>.
```

No contract block, no summary section, no ceremony. Just confirm the change.

---

The skill is already registered and triggering (it's in the system-reminder skill list). The only cleanup needed is removing the stale "Skill created" note from line 96-98. Please approve the file write, or I can confirm the skill is functional as-is — that trailing text doesn't affect execution.
