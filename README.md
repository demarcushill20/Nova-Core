# NovaCore Agent Runtime

A persistent autonomous AI runtime on a VPS. Claude operates as an executive agent coordinating research, coding, automation, and sub-agent workflows.

## Directory Structure

The repository separates **source code** (tracked in git) from **runtime state** (gitignored, created at execution time).

### Source (version-controlled)

```
telegram_bot.py       Telegram → TASKS bridge
telegram_notifier.py  OUTPUT → Telegram notifier
watcher.py            TASKS → Claude worker dispatcher
heartbeat.py          Periodic health monitoring
telegram/             Command parser (parse.py) and output formatter (format.py)
tools/                Tool runner, file ops, registry
systemd/              Systemd service and timer units
PROTOCOL/             Specification documents
SKILLS/               Reusable workflows and capabilities
AGENTS/               Agent configurations
MEMORY/               Persistent notes and learned context
.claude/skills/       Anthropic-style SKILL.md definitions
```

### Runtime (gitignored, created at execution time)

```
TASKS/          Incoming work items (.md → .inprogress → .done/.failed/.skip)
OUTPUT/         Completed results with timestamps
WORK/           Scratch space for worker artifacts
LOGS/           Execution logs (worker, task, shell, git)
STATE/          Runtime state (intents, chat modes, lock files, metrics, PID files)
HEARTBEAT.md    Auto-generated health status (written by heartbeat.py)
```

---

## Telegram Integration

NovaCore exposes a Telegram interface with three cooperating systemd services:

| Service | Script | Role |
|---|---|---|
| `novacore-telegram.service` | `telegram_bot.py` | Telegram → TASKS bridge (polls `getUpdates`, parses commands, creates task files) |
| `novacore-telegram-notifier.service` | `telegram_notifier.py` | OUTPUT → Telegram (watches `OUTPUT/` for `*.md`, sends notifications) |
| `novacore-watcher.service` | `watcher.py` | TASKS → Claude (picks up `.md` tasks, dispatches Claude workers) |
| `novacore-heartbeat.timer` | `heartbeat.py` | Periodic health monitoring (every 30min, writes HEARTBEAT.md, alerts on failure) |

### Telegram Command Protocol (v1.1)

Full specification: `PROTOCOL/telegram_commands.md`

Supported commands:

| Command | Action | Notes |
|---|---|---|
| `/run <title>` | Queue a new task | Body on subsequent lines; title max 200 chars |
| `/status` | List recent tasks | Count depends on mode: compact=5, normal=10, verbose=20 |
| `/last` | Show most recent task | Body included in normal/verbose modes |
| `/get <file> [page]` | Retrieve output file | 3000 chars/page, max 20 pages; prefix matching supported |
| `/tail <id> [lines]` | Tail worker log | Default 50 lines, max 200; 3000-char response cap |
| `/cancel <id\|last>` | Soft-cancel a task | Queued: `.md` → `.skip`; in-progress: marker file `TASKS/.<stem>.cancel_requested` |
| `/chat <text>` | Force chat-mode reply | Output stripped of report sections (CONTRACT, metadata, etc.) |
| `/report <text>` | Force structured report | Full structured output regardless of intent classification |
| `/mode [level]` | Get/set verbosity | `compact`, `normal`, `verbose`; persisted in `STATE/chat_modes.json` |
| `/help` | Show command list | |

Key behaviors:

- Non-command messages (plain text) are classified by intent and queued as tasks:
  - **Chat intent** (default): plain conversational text → worker runs, notifier strips report sections (CONTRACT, metadata, telemetry) → clean answer delivered
  - **Task intent**: messages containing keywords like "report", "debug", "verbose", "audit", "detailed" → full structured output preserved
  - Override with `/chat <text>` (force chat) or `/report <text>` (force structured)
- Intent is persisted in `STATE/intents/{stem}.intent` for the notifier to read
- Unknown `/command` returns an error with a `/help` hint.
- Task ID normalization: both `#0005` and `0005` accepted; `#` stripped during parsing.
- All timestamps in responses are UTC-labelled.
- Parser lives in `telegram/parse.py` — pure functions, no I/O, no side effects.
- Report stripping lives in `telegram/format.py` — `strip_report_sections()` removes CONTRACT blocks, Files Referenced, Security Notes, metadata lines, tool audit tables, and notifier telemetry.

### Import Shim

The local `telegram/` directory shadows the `python-telegram-bot` library's `telegram` package. An import shim in `telegram_bot.py` handles this:

1. Temporarily hides the project root from `sys.path`
2. Imports the library (`from telegram import Update`)
3. Restores `sys.path`
4. Registers `telegram/parse.py` into `sys.modules` via `importlib.util`
5. Enables the canonical import: `from telegram.parse import parse_message`

### Conflict Mitigation (getUpdates)

**Problem:** `telegram.error.Conflict` occurs when two concurrent `getUpdates` long-poll connections exist on the same bot token — typically during `systemctl restart` when the old process's HTTP connection lingers at the Telegram API while the new process starts polling.

**Mitigations applied:**

1. **fcntl process lock** — `telegram_bot.py` acquires a non-blocking exclusive lock on `STATE/telegram_bot.lock` at startup. If the lock is already held, the process logs a message and exits cleanly (code 0). The lock auto-releases when the process exits (kernel guarantee).

2. **RestartSec=10** — the systemd unit waits 10 seconds between crash and restart, allowing the Telegram API's server-side long-poll (default timeout 10s) to expire before the new process begins polling.

3. **TimeoutStopSec=20** — caps the graceful shutdown window at 20 seconds before systemd escalates to SIGKILL.

4. **No `drop_pending_updates`** — `run_polling()` does NOT use `drop_pending_updates=True`. Telegram retains unacknowledged updates for 24 hours; the bot picks them up on restart. This ensures no user commands are lost during restarts.

### Verification Commands

```bash
# Service status
systemctl status novacore-telegram.service --no-pager -l
systemctl status novacore-telegram-notifier.service --no-pager -l
systemctl status novacore-watcher.service --no-pager -l

# Recent logs (check for Conflict errors)
journalctl -u novacore-telegram.service -n 50 --no-pager

# Confirm exactly one poller process
pgrep -af 'python.*telegram_bot.py'

# Confirm lock file exists and PID matches running process
cat STATE/telegram_bot.lock
pgrep -f 'python.*telegram_bot.py'
```

Expected results:
- Process count: exactly 1
- No `Conflict` errors in recent journal
- Lock file PID matches `pgrep` output

### Parser Sanity Test

The import shim means you cannot naively `import telegram.parse` from the project root. Use the same stub approach as the test suite:

```bash
python3 << 'EOF'
import importlib.util, sys, os
spec = importlib.util.spec_from_file_location("tbot", "telegram_bot.py")
fake_tg = type(sys)("telegram"); fake_tg.Update = None
fake_ext = type(sys)("telegram.ext")
fake_ext.Application = None; fake_ext.MessageHandler = None
fake_ext.ContextTypes = None; fake_ext.filters = None
sys.modules["telegram"] = fake_tg
sys.modules["telegram.ext"] = fake_ext
tbot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tbot)

print(tbot.handle_help()[:40] + "...")
print(tbot.handle_get_mode("test"))
EOF
```

---

## Skills / Tooling Integration

### Claude Skills (`.claude/skills/`)

Five SKILL.md files define operational discipline for the Claude agent:

| Skill | File | Purpose |
|---|---|---|
| **task-execution** | `.claude/skills/task-execution/SKILL.md` | Task lifecycle: `.md` → `.inprogress` → `.done`/`.failed`; outputs to `OUTPUT/`; logs to `LOGS/`; delegates to other skills |
| **file-ops** | `.claude/skills/file-ops/SKILL.md` | Sandbox-scoped file CRUD; no deletions outside `LOGS/backups/` without approval; diff-first edits |
| **shell-ops** | `.claude/skills/shell-ops/SKILL.md` | Safe shell execution; denylist for destructive commands; 120s default timeout |
| **git-ops** | `.claude/skills/git-ops/SKILL.md` | Allowlisted git subcommands; no force-push or hard-reset without approval |
| **self-verification** | `.claude/skills/self-verification/SKILL.md` | Health checks: required dirs, orphaned tasks, output matching, log freshness, CLAUDE.md |

### Tools Registry

`STATE/tools_registry.json` defines six registered tools:

| Tool | Description |
|---|---|
| `files.read` | Read file contents (full or line range) |
| `files.write` | Create or overwrite a file |
| `files.list` | Glob-match files within sandbox (cap: 1000 entries) |
| `files.diff` | Unified diff between two files or file vs string |
| `shell.run` | Execute shell commands with safety enforcement |
| `git.run` | Run allowlisted git subcommands |

Registry settings:
- `sandbox_root`: `~/nova-core` — all paths must resolve within this boundary
- `audit_log`: `STATE/tool_audit.jsonl` — append-only JSONL log of all tool invocations

### Tool Runner (`tools/runner.py`)

Central execution engine. All tool calls pass through `run_tool()`.

**Return envelope** (consistent across all tools):

```python
{
    "ok":        bool,   # True if the operation succeeded
    "exit_code": int,    # 0 on success, -1 on safety/validation error, else process code
    "stdout":    str,    # text output (empty for files.* tools)
    "stderr":    str,    # error message on failure (empty on success)
    "result":    dict,   # structured output from files.* tools (absent for shell/git)
}
```

**Safety enforcement:**

- **shell.run denylist:** Blocks `rm -rf /`, `mkfs`, `dd if=`, `fdisk`, `shutdown`, `reboot`, `halt`, `poweroff`, `init 0`, `init 6`, fork bombs. System package managers (`apt`, `dnf`, `yum`) require explicit approval.
- **git.run allowlist:** Only `status`, `diff`, `log`, `add`, `commit`, `branch`, `checkout`, `show`. Blocks `--force`, `reset --hard`, `clean -fd/-fx`, `rebase`, `filter-branch`.
- **Secret redaction:** Output from all tools is scanned for known secret key patterns (`TELEGRAM_TOKEN`, `BOT_TOKEN`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `CLAUDE_WEB_COOKIE`, `SESSION_KEY`) and values are replaced with `<REDACTED>`.
- **Output truncation:** stdout/stderr capped at 100 KB.

**Audit log** (`STATE/tool_audit.jsonl`): Each invocation records timestamp, tool name, sanitized args, ok/exit_code, elapsed_ms. Files tools include a compact `result_summary` (path, lines/bytes/count — never file contents).

### Supporting Modules

- `tools/registry.py` — `load_registry()`, `get_tool()`, `resolve_sandbox_root()`, `resolve_audit_log()`, `validate_registry()`
- `tools/files.py` — `read_text()`, `write_text()`, `list_glob()`, `unified_diff()`, `dispatch_files_tool()`; binary detection via NUL-byte check in first 8 KB

---

## Quick Start / Ops

### Service Management

```bash
# Start all three services
sudo systemctl start novacore-telegram.service
sudo systemctl start novacore-telegram-notifier.service
sudo systemctl start novacore-watcher.service

# Stop all three services
sudo systemctl stop novacore-telegram.service
sudo systemctl stop novacore-telegram-notifier.service
sudo systemctl stop novacore-watcher.service

# Restart the Telegram bridge (e.g. after code changes)
sudo systemctl restart novacore-telegram.service

# Check status of all three
systemctl status novacore-telegram.service --no-pager
systemctl status novacore-telegram-notifier.service --no-pager
systemctl status novacore-watcher.service --no-pager
```

### Key Paths

| Path | Purpose |
|---|---|
| `TASKS/` | Pending, in-progress, done, failed, and skipped task files |
| `OUTPUT/` | Completed task outputs (timestamped `.md` files) |
| `LOGS/` | Worker logs, task logs, shell logs |
| `STATE/` | Runtime state: `chat_modes.json`, `tool_audit.jsonl`, `telegram_bot.lock`, `tools_registry.json` |
| `PROTOCOL/` | Specification documents (`telegram_commands.md` v1.1) |
| `telegram_bot.py` | Telegram → TASKS bridge (systemd: `novacore-telegram.service`) |
| `telegram_notifier.py` | OUTPUT → Telegram notifier (systemd: `novacore-telegram-notifier.service`) |
| `watcher.py` | TASKS → Claude worker dispatcher (systemd: `novacore-watcher.service`) |

### Task Lifecycle

```
User sends message or /run via Telegram
  → telegram_bot.py classifies intent (chat/task), creates TASKS/NNNN_title.md
    → stores intent in STATE/intents/{stem}.intent
    → watcher.py picks it up, renames to .inprogress, spawns Claude worker
      → Worker writes OUTPUT/..._YYYYMMDD-HHMMSS.md, renames task to .done
        → telegram_notifier.py detects *.md in OUTPUT/, reads intent
          → chat intent: strips report sections → clean answer
          → task intent: full structured output
```

Cancel flow: `/cancel` on a queued task renames `.md` → `.skip`. On an in-progress task, creates `TASKS/.<stem>.cancel_requested` marker (worker checks for it; no process killing).
