# file-ops Reference

Detailed rules, edge cases, and style guidelines for the file-ops skill.

## Edge Cases

### Missing File

- On read: report the path and that it does not exist. Do not create a placeholder or empty file.
- On edit: treat as a creation if the task clearly intends a new file; otherwise report the missing path and stop.

### Large File Edits

- Files over 50 KB: read only the relevant section (use offset/limit) rather than loading the entire file.
- Apply targeted edits to the specific region; avoid rewriting the full file when a surgical edit suffices.

### Conflicting Edits

- If the file contents changed between the initial read and the write (e.g., another process modified it), re-read the file, recompute the diff, and re-apply.
- Never silently overwrite concurrent changes.

### Binary Files

- Do not attempt to read or edit binary files (images, compiled objects, archives).
- Detect binary content by checking for null bytes in the first 512 bytes.
- Report the file type and suggest an alternative approach (e.g., replacing the file wholesale, using a dedicated tool).

### Partial Edits

- If an edit fails mid-operation (e.g., disk full, permission error), restore from `LOGS/backups/` if a pre-edit backup was made.
- If no backup exists, report the partial state clearly so the user can recover.

### Safe Fallback Behavior

- When uncertain whether a file should be created or edited, default to reading first and asking for clarification.
- When uncertain whether a deletion is authorized, default to refusing.
- When a path is ambiguous (e.g., relative path, symlink), resolve to absolute path and confirm it is within `~/nova-core` before proceeding.

## Style Rules

### Minimal Diffs

- Change only the lines that need changing. Do not reformat, re-indent, or reorganize surrounding code.
- Preserve the file's existing whitespace style (tabs vs. spaces, trailing newlines).

### Atomic Edits

- Each edit operation should be a single logical change. Do not bundle unrelated changes into one edit.
- If multiple unrelated changes are needed, perform them as separate edit operations.

### No Silent Overwrite

- Always read before writing. Always diff before applying.
- If the file already contains the intended content, report "no change needed" rather than writing identical content.

### Preserve Formatting

- Match the existing file's indentation, quoting style, and line endings.
- Do not add trailing whitespace or change line ending style (LF vs. CRLF).
- Do not add or remove blank lines unless the change specifically requires it.

## Sandbox Boundaries

- All operations must target paths within `~/nova-core`.
- Symlinks are resolved to their real path before the sandbox check.
- Environment variable expansion (`$HOME`, `~`) is resolved before validation.
- The `.claude/` directory is read-only except for sanctioned skill updates.

## Deletion Policy

| Location | Allowed | Condition |
|---|---|---|
| `LOGS/backups/` | Yes | Always |
| Anywhere else in `~/nova-core` | No | Unless explicit user approval |
| Outside `~/nova-core` | No | Never |
| `.inprogress` files | No | Never (active task) |
