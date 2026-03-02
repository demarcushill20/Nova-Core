# self-verification Reference

Detailed philosophy, scoring guidance, and operational rules for the self-verification skill.

## Verification Philosophy

**Never assume success.** Every operation can fail silently:

- A file write can succeed (exit 0) but write wrong content.
- A git commit can succeed but include the wrong files.
- A shell command can return exit 0 but emit warnings on stderr.
- A task can be marked `.done` but have no output file.

Verification exists to catch these gaps. It is the last line of defense before an agent declares work complete.

## Read-After-Write Principle

After any file modification:

1. Re-read the file using an independent read operation.
2. Compare the actual content against the intended content.
3. If creating a new file, confirm it exists at the expected path with non-zero size.
4. If editing, confirm the diff is minimal and only the intended lines changed.

This catches:
- Silent write failures (disk full, permission denied but swallowed).
- Encoding corruption.
- Race conditions where another process modifies the file.

## Exit Code Discipline

Exit codes are necessary but not sufficient:

| Exit Code | Trust Level | Action |
|---|---|---|
| 0 | Partial trust | Also check stdout/stderr for warnings |
| Non-zero | Definitive failure | Report immediately |
| 126 | Blocked by runner | Do not retry; rephrase |
| 127 | Command not found | Check PATH/installation |
| -1 | Timeout | Investigate duration |

**Never treat exit 0 as proof of correctness.** Always perform at least one additional check:
- Read the output file.
- Query the expected state.
- Check for side effects.

## Contract Field Validation

Every skill output contract has required fields. Validation rules:

| Field | Valid If |
|---|---|
| `summary` | Non-empty string, under 200 characters |
| `files_changed` | List with at least one entry (or empty list for no-ops) |
| `commands_executed` | List with at least one entry showing command and exit code |
| `task_id` | Matches the task stem pattern (e.g., `0010_deploy_fix`) |
| `status` | One of: `done`, `failed` |
| `verification` | Non-empty string describing how correctness was confirmed |
| `checks_performed` | List with at least one check and its pass/fail result |
| `result` | One of: `pass`, `fail`, `partial` |
| `confidence` | One of: `high`, `medium`, `low` |

Missing required fields cause an automatic `fail` result.

## Common False-Positive Patterns

Be aware of these traps where verification might incorrectly report success:

### Stale reads
Reading a cached or stale version of a file instead of the current one. Mitigate by reading from disk after a brief pause, or by checking modification timestamps.

### Checking the wrong file
Verifying a file at the old path after a rename. Always verify the destination path, not the source.

### Trusting formatted output
A command may print "Success" while actually failing. Always check exit codes and actual state, not just human-readable output.

### Partial writes
A file may exist and have non-zero size but be incomplete (truncated). For critical files, check for expected markers (e.g., closing tags, contract footers).

### Race conditions
Another process may modify the file between the write and the verification read. In practice, this is rare in NovaCore's single-worker model, but be aware during concurrent operations.

## Confidence Scoring Guidance

| Confidence | Criteria |
|---|---|
| **high** | All checks passed. At least one read-after-write confirmed. Exit codes verified. Contract fields validated. |
| **medium** | Most checks passed. Some checks skipped or inconclusive. No failures detected but coverage is incomplete. |
| **low** | Some checks failed. OR: only exit code was checked (no read-after-write). OR: contract fields missing. |

Rules:
- Start at `high` and degrade based on gaps.
- Any single `fail` check drops confidence to at most `medium`.
- Two or more `fail` checks drop confidence to `low`.
- If no read-after-write was performed, cap at `medium`.
- If the verification itself errors, set confidence to `low`.

## Environment Health Checks

For periodic system-level verification:

| Check | Expected State |
|---|---|
| `TASKS/` directory | Exists |
| `OUTPUT/` directory | Exists |
| `LOGS/` directory | Exists |
| `.claude/skills/` directory | Exists |
| `CLAUDE.md` | Exists, parseable |
| Orphaned `.inprogress` files | None (or flagged) |
| `.done` tasks without output | None |
| `LOGS/` freshness | Entries within last 24 hours |
