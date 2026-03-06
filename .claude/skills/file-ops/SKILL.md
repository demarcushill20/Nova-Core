---
name: file-ops
description: "Safely create, read, edit, move, and rename files within the nova-core sandbox using a diff-first, read-before-write workflow."
activation:
  keywords:
    - file
    - read
    - write
    - edit
    - diff
    - patch
    - path
    - .py
    - .md
    - .json
    - .yaml
    - .yml
    - .txt
    - .csv
    - .toml
    - .cfg
    - .ini
    - .sh
  when:
    - Task requires creating, reading, editing, or moving files
    - Task involves generating code, configs, or documentation
    - Task references specific file paths or extensions
    - Organizing outputs, logs, or memory artifacts
tool_doctrine:
  files:
    workflow:
      - read_before_write
      - diff_first
      - verify_after_write
output_contract:
  required:
    - summary
    - files_changed
    - verification
    - confidence
---

# When To Use

- A task requires creating, reading, editing, moving, or renaming files inside `~/nova-core`.
- Generating new code, configuration, or documentation files.
- Organizing outputs, logs, or memory artifacts.
- Cleaning up `LOGS/backups/` (the only location where deletions are allowed without explicit user approval).

# Workflow

1. **Validate path** — confirm the target resolves within `~/nova-core`. Reject any path that escapes the sandbox.
2. **Read first** — always read the existing file before writing or editing. No blind overwrites.
3. **Diff first** — compute the intended change as a minimal diff before applying it.
4. **Apply change** — write, edit, move, or rename the file.
5. **Verify after write** — re-read the file to confirm the change landed correctly.
6. **Log mutation** — log any state-changing operation to `LOGS/`.

For details on edge cases (missing files, large files, binary files, conflicts), see `reference.md`.

# Tool Usage Rules

- All paths must resolve within `~/nova-core`. Reject any path that escapes the sandbox.
- Never overwrite a file without reading it first.
- Prefer editing existing files over creating new ones.
- Produce minimal diffs — change only what is necessary, preserve surrounding formatting.
- **No deletions by default.** Permitted only inside `LOGS/backups/` or with explicit user approval.
- Before any destructive operation, back up the original to `LOGS/backups/`.
- Do not modify `.claude/` internals except through sanctioned skill updates.
- Never delete a file in `.inprogress` state.

# Verification

After every write or edit:

1. Re-read the modified file to confirm contents match intent.
2. If the file existed before, confirm the diff is minimal and correct.
3. If creating a new file, confirm it exists at the expected path with non-zero size.
4. For moves/renames, confirm the source is gone and the destination exists.

# Failure Handling

- **File not found on read**: report the missing path clearly; do not create a placeholder.
- **Path escapes sandbox**: refuse the operation and explain why.
- **Write conflict** (file changed between read and write): re-read, re-diff, and re-apply.
- **Binary file detected**: refuse to edit in-place; report the file type and suggest alternatives.
- **Partial edit failure**: restore from `LOGS/backups/` if a backup was created.
- **Permission denied**: report the error; do not attempt `chmod` or `sudo` workarounds.

# Output Contract

Every file-ops execution must end with a machine-checkable contract:

```
## CONTRACT
summary: <one-line description of what was done>
files_changed:
  - <path> (<action>)
verification: <how correctness was confirmed>
confidence: <high | medium | low>
```

See `examples.md` for concrete instances.
