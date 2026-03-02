---
name: file-ops
description: "Create, read, edit, move, and rename files and directories within the nova-core sandbox. Deletions restricted by default."
---

# file-ops

## When to use

- A task requires creating, reading, editing, moving, or renaming files.
- Organizing outputs, logs, or memory artifacts.
- Generating new code or configuration files.
- Cleaning up `LOGS/backups/` (the only location where deletions are allowed without explicit user approval).

## Rules / Safety

- All paths must resolve within `~/nova-core`. Reject any path that escapes the sandbox.
- Never overwrite a file without reading it first.
- Prefer editing existing files over creating new ones.
- **No deletions by default.** Deletions are permitted only inside `LOGS/backups/` or with explicit user approval.
- Before any destructive operation, back up the original to `LOGS/backups/`.
- Do not modify `.claude/` internals except through sanctioned skill updates.

## Workflow

1. Validate that the target path is within `~/nova-core`.
2. For reads: return file contents.
3. For writes/edits: read existing content first (diff-first), apply changes, write result.
4. For moves/renames: verify destination does not collide, then rename.
5. For deletes: **refuse** unless the target is inside `LOGS/backups/` or explicit user approval was given. Never delete a file in `.inprogress` state.
6. Log the operation to `LOGS/` if it mutates state.

## Output format

Action confirmation with the absolute path and a one-line summary:

```
[file-ops] CREATED ~/nova-core/OUTPUT/report.md
[file-ops] EDITED  ~/nova-core/SKILLS/foo/SKILL.md (added workflow section)
[file-ops] MOVED   ~/nova-core/LOGS/old.log → ~/nova-core/LOGS/backups/old.log
[file-ops] DELETED ~/nova-core/LOGS/backups/old.log (permitted — inside LOGS/backups/)
```
