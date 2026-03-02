# Telegram Command Protocol

Version: 1.1
Scope: Defines parsing rules, canonical action objects, and response formats for all Telegram commands accepted by NovaCore.

---

## General Parsing Rules

1. **Command prefix**: All commands start with `/` as the first non-whitespace character.
2. **Whitespace**: Leading/trailing whitespace on the message is stripped before parsing. The command keyword is case-insensitive (`/Run` = `/run`). Arguments are case-sensitive unless noted.
3. **Task ID normalization**: Task IDs accept both `0005` and `#0005`. The `#` prefix is stripped during parsing. Internally all task IDs are bare zero-padded integers (e.g., `0005`).
4. **Unknown commands**: Any message starting with `/` that does not match a known command returns an error response (see Error Response Format).
5. **Non-command messages**: Messages that do not start with `/` are silently ignored (no response).
6. **Max message length**: 4096 characters (Telegram limit). Messages exceeding this are rejected before parsing.
7. **Timestamps**: All `ts` fields are Unix epoch floats (seconds). All human-readable timestamps in responses are displayed in **UTC** and labelled as such (e.g., `2025-03-02 14:00 UTC`).
8. **chat_id**: String representation of the Telegram chat ID from which the message originated.
9. **Canonical action objects**: Every parsed command produces a JSON action object. All action objects include at minimum: `action` (string), `source` (literal `"telegram"`), `chat_id` (string), `ts` (float).

---

## Commands

### /run

Create and queue a new task.

**Syntax:**
```
/run <title>
<body>
```

**Parsing rules:**
- First line after `/run ` is the title (everything after `/run ` up to the first newline).
- Title is required and must be non-empty after stripping whitespace. Max 200 characters.
- All subsequent lines form the body. Body is optional (defaults to empty string).
- Body preserves internal whitespace and newlines exactly as sent.
- No quoting mechanism — raw text only.
- Title is sanitized for use as a filename: `[^a-zA-Z0-9_-]` replaced with `_`, truncated to 80 chars for the stem.

**Canonical action:**
```json
{
  "action": "run_task",
  "title": "Deploy staging hotfix",
  "body": "Fix the auth timeout bug on staging.\nPriority: high",
  "source": "telegram",
  "chat_id": "123456789",
  "ts": 1740000000.0
}
```

**Side effects:**
- Creates `TASKS/<NNNN>_<sanitized_title>.md` with body as content.
- Task numbering: next sequential 4-digit zero-padded integer based on existing files in `TASKS/`.

**Response:**
```
Queued: 0005_Deploy_staging_hotfix.md (2025-03-02 14:00 UTC)
```

**Error cases:**
| Condition | Response |
|---|---|
| Missing title | `Error: /run requires a title. Usage: /run <title>` |
| Title > 200 chars | `Error: title too long (max 200 chars)` |

**Example:**
```
/run Fix login redirect
The /login endpoint redirects to /dashboard even when
session is expired. Should redirect to /login?expired=1.
```

---

### /status

Show recent task status.

**Syntax:**
```
/status
```

No arguments. Any trailing text is ignored.

**Canonical action:**
```json
{
  "action": "get_status",
  "source": "telegram",
  "chat_id": "123456789",
  "ts": 1740000000.0
}
```

**Side effects:** None (read-only).

**Response structure:**
Returns the last 10 tasks (adjustable by `/mode`: compact=5, normal=10, verbose=20) sorted by task number descending. Each entry shows:

```
#0005 Deploy_staging_hotfix    queued   2025-03-02 14:00 UTC
#0004 real_autonomy            failed   2025-03-01 22:30 UTC
#0003 agent_bootstrap          done     2025-03-01 18:15 UTC
```

Format per line:
```
#<NNNN> <stem>  <status>  <mtime YYYY-MM-DD HH:MM> UTC
```

Status is derived from file extension:
| Extension | Status |
|---|---|
| `.md` | `queued` |
| `.inprogress` | `inprogress` |
| `.done` | `done` |
| `.failed` | `failed` |
| `.skip` | `skip` |

If no tasks exist, respond: `No tasks found.`

**Example:**
```
/status
```

Response:
```
#0005 Deploy_staging_hotfix    queued   2025-03-02 14:00 UTC
#0004 real_autonomy            failed   2025-03-01 22:30 UTC
#0003 agent_bootstrap          done     2025-03-01 18:15 UTC
```

---

### /last

Show the most recent task's full details.

**Syntax:**
```
/last
```

No arguments. Trailing text is ignored.

**Parsing rules:**
- No arguments to parse.
- "Most recent" means the highest-numbered task file in `TASKS/` regardless of status.

**Canonical action:**
```json
{
  "action": "get_last",
  "source": "telegram",
  "chat_id": "123456789",
  "ts": 1740000000.0
}
```

**Side effects:** None (read-only).

**Response structure:**
```
#0005 Deploy_staging_hotfix [queued]
Created: 2025-03-02 14:00 UTC

Fix the auth timeout bug on staging.
Priority: high
```

Format:
- Line 1: `#<NNNN> <stem> [<status>]`
- Line 2: `Created: <mtime YYYY-MM-DD HH:MM> UTC`
- Line 3: blank
- Lines 4+: task file body (truncated to 2000 chars with `... [truncated]` suffix if exceeded)
- In `compact` mode: omit task body entirely (lines 3+ not shown).
- In `verbose` mode: also show file path and size.

If no tasks exist, respond: `No tasks found.`

**Error cases:**
| Condition | Response |
|---|---|
| No tasks exist | `No tasks found.` |

**Example:**
```
/last
```

Response:
```
#0005 Deploy_staging_hotfix [queued]
Created: 2025-03-02 14:00 UTC

Fix the auth timeout bug on staging.
Priority: high
```

---

### /get

Retrieve contents of an output file.

**Syntax:**
```
/get <filename> [<page>]
```

**Parsing rules:**
- `<filename>` is required. Matched against files in `OUTPUT/`.
- Partial match: if `<filename>` does not end with `.md`, append `.md`. If no exact match, try prefix match (e.g., `0005` matches `0005_Deploy_staging_hotfix_20250302_140500.md`). If multiple prefix matches, return the most recent (by mtime).
- `<page>` is optional, defaults to `1`. Must be a positive integer.
- Pages are 1-indexed.

**Chunking rules:**
- Chunk size: 3000 characters per page (safe margin under Telegram's 4096 limit after formatting).
- Max chunks: 20 pages. If the file exceeds 60000 characters, pages beyond 20 return a truncation notice.
- Each page is prefixed with a header: `[<filename> page <N>/<total>]`

**Canonical action:**
```json
{
  "action": "get_output",
  "filename": "0005_Deploy_staging_hotfix_20250302_140500.md",
  "page": 1,
  "source": "telegram",
  "chat_id": "123456789",
  "ts": 1740000000.0
}
```

**Side effects:** None (read-only).

**Response:**
```
[0005_Deploy_staging_hotfix_20250302_140500.md page 1/3]

Task: Deploy staging hotfix
Status: success
...
```

If more pages exist, append: `— /get <filename> <next_page> for next page`

**Error cases:**
| Condition | Response |
|---|---|
| Missing filename | `Error: /get requires a filename. Usage: /get <filename> [page]` |
| File not found | `Error: no output file matching "<filename>"` |
| Invalid page number | `Error: page must be a positive integer` |
| Page out of range | `Error: page <N> out of range (1-<total>)` |

**Example:**
```
/get 0005
/get 0005_Deploy_staging_hotfix_20250302_140500.md 2
```

---

### /tail

Show the last N lines of a task's worker log.

**Syntax:**
```
/tail <task_id> [<lines>]
```

**Parsing rules:**
- `<task_id>` is required. Accepts `0005` or `#0005` (the `#` prefix is stripped).
- Matches against `LOGS/worker_<task_id>*.log` or `LOGS/task_<task_id>*.log`. Prefix matching applies (e.g., `0005` matches `worker_0005_Deploy_staging_hotfix.log`). If multiple matches, use the most recently modified file.
- `<lines>` is optional, defaults to `50`. Must be a positive integer. Max: `200`.

**Canonical action:**
```json
{
  "action": "tail_log",
  "task_id": "0005",
  "lines": 50,
  "source": "telegram",
  "chat_id": "123456789",
  "ts": 1740000000.0
}
```

**Side effects:** None (read-only).

**Response:**
```
[worker_0005_Deploy_staging_hotfix.log — last 50 lines]

2025-03-02 14:01:00 UTC Starting task...
2025-03-02 14:01:05 UTC Reading instructions...
...
```

Output is truncated to 3000 characters to stay within Telegram limits. If truncated, append suffix: `... [truncated to 3000 chars]`

**Error cases:**
| Condition | Response |
|---|---|
| Missing task_id | `Error: /tail requires a task_id. Usage: /tail <task_id> [lines]` |
| Log not found | `Error: no log file matching "<task_id>"` |
| Invalid lines | `Error: lines must be a positive integer (max 200)` |

**Example:**
```
/tail 0005
/tail 0005 100
```

---

### /cancel

Soft-cancel a task. Does **not** kill running processes.

**Syntax:**
```
/cancel <task_id|last>
```

**Parsing rules:**
- `<task_id>` is a task number prefix (e.g., `0005` or `#0005`; the `#` prefix is stripped).
- The literal keyword `last` resolves to the highest-numbered task file in `TASKS/`.
- Prefix matching: `0005` matches `TASKS/0005_Deploy_staging_hotfix.*`.

**Soft cancel semantics by current state:**

| Current state | Action taken |
|---|---|
| **queued** (`.md`) | Rename to `.skip`. Watcher will never dispatch it. |
| **inprogress** (`.inprogress`) | Create marker file `TASKS/.<stem>.cancel_requested`. The running worker is **not** killed. The watcher checks for this marker before writing output: if present, it renames the task to `.skip` after the worker exits and discards the output. A cancellation note is appended to the worker log: `[CANCELLED by user via Telegram at <YYYY-MM-DD HH:MM:SS> UTC]`. |
| **done** (`.done`) | No action. Respond: already completed. |
| **failed** (`.failed`) | No action. Respond: already completed. |
| **skip** (`.skip`) | No action. Respond: already cancelled. |

**Why a marker file for inprogress:** Renaming an `.inprogress` file while a worker holds it open risks race conditions. The marker file (`.0005_Deploy_staging_hotfix.cancel_requested`) is a zero-byte file that the watcher checks atomically. The watcher is responsible for the final rename to `.skip` after the worker process exits.

**Canonical action:**
```json
{
  "action": "cancel_task",
  "task_id": "0005",
  "source": "telegram",
  "chat_id": "123456789",
  "ts": 1740000000.0
}
```

**Side effects:**
- If queued: renames task file from `.md` to `.skip`.
- If inprogress: creates `TASKS/.<stem>.cancel_requested` marker file, appends cancellation note to `LOGS/worker_<stem>.log`.

**Response:**
```
Cancelled: 0005_Deploy_staging_hotfix (.md → .skip)
```

Or for inprogress tasks:
```
Cancel requested: 0005_Deploy_staging_hotfix (will skip after worker exits)
```

**Error cases:**
| Condition | Response |
|---|---|
| Missing task_id | `Error: /cancel requires a task_id or "last". Usage: /cancel <task_id\|last>` |
| Task not found | `Error: no task matching "0099"` |
| Already done/failed | `Error: task 0005 is already done, cannot cancel` |
| Already skip | `Error: task 0005 is already cancelled` |

**Example:**
```
/cancel 0005
/cancel #0005
/cancel last
```

---

### /mode

Set response verbosity for the current chat.

**Syntax:**
```
/mode <compact|normal|verbose>
```

**Parsing rules:**
- Argument is required and must be one of: `compact`, `normal`, `verbose` (case-insensitive, normalized to lowercase).
- With no argument, returns the current mode instead of an error.

**Storage:**
- Mode is persisted per `chat_id` in `STATE/chat_modes.json`.
- File format: `{ "<chat_id>": "<mode>", ... }`
- If the file does not exist or a `chat_id` has no entry, the default mode is `normal`.
- The file is created on first `/mode` invocation.

**Mode definitions:**

| Mode | `/status` count | `/last` body | Extra detail |
|---|---|---|---|
| `compact` | 5 tasks | Omitted (header only) | None |
| `normal` | 10 tasks | Included (up to 2000 chars) | None |
| `verbose` | 20 tasks | Included (up to 2000 chars) | File paths, byte sizes, timing info appended to all responses |

**Canonical action (setting a mode):**
```json
{
  "action": "set_mode",
  "mode": "compact",
  "source": "telegram",
  "chat_id": "123456789",
  "ts": 1740000000.0
}
```

**Canonical action (querying current mode):**
```json
{
  "action": "get_mode",
  "source": "telegram",
  "chat_id": "123456789",
  "ts": 1740000000.0
}
```

**Side effects:**
- When setting: writes/updates `STATE/chat_modes.json`.
- When querying: none (read-only).

**Response (set):**
```
Mode set to: compact
```

**Response (query — no argument):**
```
Current mode: normal
```

**Error cases:**
| Condition | Response |
|---|---|
| Invalid value | `Error: unknown mode "<value>". Choose: compact, normal, verbose` |

**Example:**
```
/mode compact
/mode verbose
/mode
```

---

### /help

Show available commands with short descriptions.

**Syntax:**
```
/help
```

No arguments. Trailing text is ignored.

**Parsing rules:**
- No arguments to parse.

**Canonical action:**
```json
{
  "action": "show_help",
  "source": "telegram",
  "chat_id": "123456789",
  "ts": 1740000000.0
}
```

**Side effects:** None (read-only).

**Response:**
```
NovaCore Commands:
/run <title>  — queue a new task (body on next lines)
/status       — show recent tasks and their status
/last         — show most recent task details
/get <file>   — retrieve output file (/get <file> 2 for page 2)
/tail <id>    — tail worker log (/tail <id> 100 for 100 lines)
/cancel <id>  — soft-cancel a task (/cancel last for newest)
/mode <level> — set verbosity: compact|normal|verbose
/help         — this message
```

**Error cases:** None — this command always succeeds.

**Example:**
```
/help
```

Response:
```
NovaCore Commands:
/run <title>  — queue a new task (body on next lines)
/status       — show recent tasks and their status
/last         — show most recent task details
/get <file>   — retrieve output file (/get <file> 2 for page 2)
/tail <id>    — tail worker log (/tail <id> 100 for 100 lines)
/cancel <id>  — soft-cancel a task (/cancel last for newest)
/mode <level> — set verbosity: compact|normal|verbose
/help         — this message
```

---

## Error Response Format

All error responses follow the pattern:
```
Error: <message>
```

For unknown commands:
```
Unknown command: /<word>. Send /help for available commands.
```

---

## File Path Mapping

| Command | Reads from | Writes to |
|---|---|---|
| `/run` | `TASKS/*` (to determine next number) | `TASKS/<NNNN>_<stem>.md` |
| `/status` | `TASKS/*` | — |
| `/last` | `TASKS/*` | — |
| `/get` | `OUTPUT/*` | — |
| `/tail` | `LOGS/worker_*`, `LOGS/task_*` | — |
| `/cancel` | `TASKS/*` | `TASKS/*` (rename or marker), `LOGS/*` (append) |
| `/mode` | `STATE/chat_modes.json` | `STATE/chat_modes.json` |
| `/help` | — | — |

Cancel marker files: `TASKS/.<stem>.cancel_requested` (zero-byte, created for inprogress tasks).

---

## Summary of Constants

| Parameter | Value |
|---|---|
| Max message length | 4096 chars (Telegram limit) |
| Max title length | 200 chars |
| Filename stem max | 80 chars |
| `/get` chunk size | 3000 chars/page |
| `/get` max pages | 20 |
| `/tail` default lines | 50 |
| `/tail` max lines | 200 |
| `/tail` + `/get` response cap | 3000 chars |
| `/last` body truncation | 2000 chars |
| `/status` count (compact) | 5 |
| `/status` count (normal) | 10 |
| `/status` count (verbose) | 20 |
| Default mode | `normal` |
| Mode storage | `STATE/chat_modes.json` |
| Task numbering | 4-digit zero-padded sequential |
