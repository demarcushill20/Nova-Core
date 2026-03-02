# NovaCore Development Diary

Reverse-chronological. Each entry covers one working session.

---

## 2026-03-02 (Session 2) — Skill Activation Engine, Git Init, First Push

**Session span:** ~07:00–08:00 UTC

### What was built

#### Git Repository Init & First Push

- Initialized git repo in `~/nova-core`
- Created `.gitignore` (excludes `.venv/`, `__pycache__/`, `LOGS/`, lock files, audit logs, env files)
- Configured git identity (Demarcus Hill)
- Generated SSH deploy key (ed25519), added to GitHub as read/write deploy key
- Initial commit: 91 files, 5,051 lines → pushed to `github.com:demarcushill20/Nova-Core.git`

#### Step 2.4 — Skill Activation Engine

Created `tools/skills.py` (160 lines) — discovers, selects, and renders SKILL.md files for injection into Claude worker prompts.

| Function | Purpose |
|---|---|
| `load_skills()` | Scans `.claude/skills/*/SKILL.md`, parses YAML frontmatter (name, description, tags, version, activation.keywords) |
| `select_skills(task_text)` | Selects relevant skills based on task content — always includes `task-execution` + `self-verification`, then keyword-matches `git-ops`, `shell-ops`, `file-ops` via built-in rules + frontmatter `activation.keywords` + shell-command regex heuristic |
| `render_append_prompt(skills)` | Concatenates skill bodies under `## ACTIVE SKILLS` header in deterministic name order; 60KB hard cap with truncation note |

**Skill selection rules:**

| Skill | Triggered by |
|---|---|
| `task-execution` | Always on |
| `self-verification` | Always on |
| `git-ops` | "git", "commit", "branch", "merge", "push", "pull", "stage", "checkout" |
| `shell-ops` | "bash", "shell", "sudo", "pip", "python", "script", "command", "process", or lines matching `^\s*(\$\|sudo)\s+` |
| `file-ops` | "file", "read", "write", "edit", "diff", "patch", "path", or file extensions (.py .md .json .yaml .yml .txt .csv .toml .cfg .ini .sh) |

**Watcher integration** — patched `watcher.py` (4 changes):
1. Imports `tools.skills`
2. Before building Claude command: reads task file (50KB cap), calls `select_skills()`, writes `WORK/skill_injection_<stem>.txt`, adds `--append-system-prompt <content>` to the `cmd` list
3. Worker log header now includes `=== SKILLS: ... ===` line
4. Command log reflects skill count

**SKILL.md frontmatter updates** — all 5 skills gained `activation.keywords` lists for content-based matching beyond the built-in rules.

**Dev tool** — `tools/dev_check_skills.py` (52 lines): CLI self-test that prints selected skills for any task file or stdin text. Supports `--render` flag for full prompt output.

### Verified behavior

| Test input | Skills selected |
|---|---|
| Task mentioning files/paths | task-execution, self-verification, file-ops |
| "commit and push to git" | task-execution, self-verification, git-ops |
| `$ sudo apt install...` | task-execution, self-verification, file-ops, shell-ops |
| "health check" (no keywords) | task-execution, self-verification (always-on only) |
| "create a .py file and commit it via git" | task-execution, self-verification, file-ops, git-ops |

### Files created or modified

| File | Lines | Action |
|---|---|---|
| `.gitignore` | 14 | Created |
| `tools/skills.py` | 160 | Created |
| `tools/dev_check_skills.py` | 52 | Created |
| `watcher.py` | 401 | Modified (+33 lines) — skill injection integration |
| `.claude/skills/task-execution/SKILL.md` | 48 | Modified — added activation.keywords |
| `.claude/skills/self-verification/SKILL.md` | 53 | Modified — added activation.keywords |
| `.claude/skills/file-ops/SKILL.md` | 53 | Modified — added activation.keywords |
| `.claude/skills/shell-ops/SKILL.md` | 51 | Modified — added activation.keywords |
| `.claude/skills/git-ops/SKILL.md` | 49 | Modified — added activation.keywords |

### Git history

| Commit | Message |
|---|---|
| `5fc18b3` | Initial commit: NovaCore agent runtime (91 files, 5,051 lines) |
| `8a6c9e1` | feat: add Skill Activation Engine (Step 2.4) (8 files, +350 lines) |

### Design decisions

- **`--append-system-prompt` (inline string) over file-based injection:** Claude CLI only supports `--append-system-prompt <string>`, not a file variant. Linux `ARG_MAX` is 2MB on this VPS; 60KB skill payloads are safe.
- **No PyYAML dependency:** Frontmatter parser is hand-rolled (handles simple key-value and nested list syntax) to avoid adding a pip dependency for 5 small files.
- **Skill injection files written to `WORK/`:** Persisted as `WORK/skill_injection_<stem>.txt` for debugging/auditability, not cleaned up automatically.

---

## 2026-03-02 — Infrastructure Build-Out: Skills, Tools, Telegram Protocol, Bot Hardening

**Session span:** ~00:00–05:00 UTC (across two Claude Code context windows)

### What was built

#### Step 2.1 — Claude Skills Skeleton (00:13 UTC)

Created `.claude/skills/` with five SKILL.md files using Anthropic-style YAML frontmatter:

| Skill | Purpose |
|---|---|
| `task-execution` | Task lifecycle `.md` → `.inprogress` → `.done`/`.failed`; delegates to other skills |
| `file-ops` | Sandbox-scoped CRUD; no deletions outside `LOGS/backups/`; diff-first edits |
| `shell-ops` | Safe shell execution; denylist for destructive commands |
| `git-ops` | Allowlisted git subcommands; blocks force-push and hard-reset |
| `self-verification` | Health checks: required dirs, orphaned tasks, output matching, log freshness |

**Compliance patches applied:** Removed general delete capability from file-ops. Added tiered directory checking (required/optional/always) to self-verification. Added explicit skill invocation delegation to task-execution.

#### Step 2.2 — Tools Registry (00:25 UTC)

Created `STATE/tools_registry.json` — canonical registry for 6 tools:
`files.read`, `files.write`, `files.list`, `files.diff`, `shell.run`, `git.run`

Each tool has `args_schema`, `returns`, and `safety` constraints. Registry defines `sandbox_root: ~/nova-core` and `audit_log: STATE/tool_audit.jsonl`.

#### Step 2.3a — Registry Module (00:29 UTC)

Created `tools/registry.py` (98 lines):
- `load_registry()` — loads and validates JSON
- `get_tool()` — lookup by name
- `resolve_sandbox_root()` / `resolve_audit_log()` — path resolution
- `validate_registry()` — structural validation

#### Step 2.3b — Tool Runner: shell.run + git.run (00:40 UTC)

Created `tools/runner.py` (266 lines):
- `run_tool()` — central dispatch with audit logging
- `enforce_shell_safety()` — denylist matching
- `enforce_git_safety()` — allowlist + forbidden args
- `redact_secrets()` — regex replacement for 6 known secret key patterns
- `run_subprocess()` — capture, truncate (100 KB), redact
- Consistent return envelope: `{ok, exit_code, stdout, stderr}`

#### Step 2.3c — File Operations (00:37 UTC)

Created `tools/files.py` (251 lines):
- `read_text()`, `write_text()`, `list_glob()`, `unified_diff()`
- `dispatch_files_tool()` — routes `files.*` tool names
- Binary detection via NUL-byte check in first 8 KB
- Glob results capped at 1000 entries
- All paths enforced within sandbox_root

#### Step 2.3d — Runner Integration for files.* (00:40 UTC)

Patched `tools/runner.py` to route `files.*` tools through `dispatch_files_tool()`. Added `result` field to return envelope for structured output. Audit log includes compact `result_summary` (path, lines/bytes/count — never file contents).

#### Step 3.1 — Telegram Command Protocol (01:23 UTC)

Created `PROTOCOL/telegram_commands.md` (v1.1) — full specification for 8 commands. Defines canonical action JSON objects, parsing rules, response formats, error handling, and constants table.

**Finalization patches:** Fixed task ID normalization consistency, added UTC timestamp labelling, defined cancel marker file pattern (`TASKS/.<stem>.cancel_requested`), specified mode persistence in `STATE/chat_modes.json`, added paging constants (3000 chars/page, max 20 pages).

#### Step 3.2 — Telegram Command Parser (01:32 UTC)

Created `telegram/parse.py` (197 lines):
- Pure parsing, no I/O, no side effects
- `parse_message(text, chat_id, ts)` → `{ok, action}` / `{ok: false, error}` / `None`
- Per-command parsers for all 8 commands
- Constants: `_MAX_MSG_LEN=4096`, `_MAX_TITLE_LEN=200`, `_TAIL_DEFAULT=50`, `_TAIL_MAX=200`

#### Step 3.3 — Bot Integration: Parser + Dispatcher (01:40–02:00 UTC)

Integrated `telegram/parse.py` into `telegram_bot.py`:
- Replaced individual CommandHandlers with single `on_message` dispatcher
- Implemented `handle_help()`, `handle_status()`, `handle_run_task()`
- Built import shim to resolve `telegram/` directory shadowing `python-telegram-bot` package

**Import shim approach:** Temporarily hide project root from `sys.path`, import library, restore path, register local module via `importlib.util`, then use canonical `from telegram.parse import parse_message`.

#### Step 3.4 — Remaining Command Handlers (02:30–03:30 UTC)

Implemented all remaining Telegram command actions in `telegram_bot.py`:

| Handler | Signature |
|---|---|
| `handle_get_last(chat_id)` | Finds highest-numbered task, mode-aware body display |
| `handle_get_output(chat_id, filename, page)` | Prefix/exact match in OUTPUT/, 3000-char paging |
| `handle_tail_log(chat_id, task_id, lines)` | Prefix match worker_/task_ logs, last N lines, 3000-char cap |
| `handle_cancel_task(chat_id, task_id_or_last)` | Queued → `.skip`; inprogress → marker file + log note |
| `handle_set_mode(chat_id, mode)` | Persists to `STATE/chat_modes.json` |
| `handle_get_mode(chat_id)` | Reads from `STATE/chat_modes.json`, defaults "normal" |

All 8 handler tests passed (mode persistence, get_last, output paging, log tailing, queued cancel with .skip rename, inprogress cancel with marker file, error handling).

### Bug fixes applied

1. **Handler signature normalization** — All handlers changed from `(action: dict)` to explicit args (e.g., `handle_set_mode(chat_id, mode)` instead of `handle_set_mode(action)`). Fixed `TypeError: handle_set_mode() takes 1 positional argument but 2 were given`.

2. **Tail log validation** — `handle_tail_log()` now rejects `lines <= 0` and `lines > 200` with protocol-compliant error message. Previously `lines=0` silently returned all lines (`list[-0:]` = full list) and `lines=500` was uncapped.

3. **Import shim restructure** — Fixed `from telegram.parse import parse_message` not being grep-matchable. Restructured shim to register module in `sys.modules` first, then use the explicit import statement.

### Conflict diagnosis and hardening

**Forensic investigation of `telegram.error.Conflict`:**

- Analyzed journalctl: 272 distinct PIDs since service creation, including a crash-restart loop of 218 rapid restarts (missing token), 52 more (placeholder token "REPLACE_ME"), then stable operation.
- Two Conflict episodes: single hit at 20:51 UTC, burst of 10 at 04:02–04:04 UTC with exponential backoff (5s → 30s).
- Root cause: `RestartSec=3` shorter than getUpdates long-poll timeout (10s), causing overlapping pollers during `systemctl restart`.
- Confirmed: no webhook set, no external pollers, notifier uses only `sendMessage` via httpx (never `getUpdates`), `__name__` guard prevents importlib test loads from triggering `main()`.

**Mitigations implemented:**

1. **fcntl process lock** — `STATE/telegram_bot.lock` with non-blocking exclusive flock. Second instance logs and exits 0. Lock auto-releases on process death (kernel guarantee). FD stashed to prevent GC.

2. **`drop_pending_updates=True` added then removed** — Initially added to `run_polling()`, then identified as a message-loss risk during the analysis phase: it tells Telegram to discard all queued updates on startup, silently dropping any commands sent during downtime. Reverted to `run_polling(close_loop=False)` only.

3. **Systemd unit hardening** (prepared, pending sudo apply):
   - `RestartSec=10` (was 3) — exceeds getUpdates long-poll timeout
   - `TimeoutStopSec=20` (was default 90) — caps graceful shutdown window
   - KillSignal left as default SIGTERM (library handles both SIGINT and SIGTERM identically)

**Systemd changes status:** Unit file written but not yet applied — requires `sudo` which needs an interactive terminal. Commands staged for manual execution.

### Documentation

Created `README.md` (244 lines) with four main sections:
- Telegram Integration (services, protocol v1.1, import shim, conflict mitigation, verification commands)
- Skills / Tooling Integration (5 skills, 6 tools, runner envelope, safety enforcement)
- Quick Start / Ops (service management, key paths, task lifecycle flow)

### Files created or modified today

| File | Lines | Action |
|---|---|---|
| `.claude/skills/task-execution/SKILL.md` | 42 | Created + patched |
| `.claude/skills/file-ops/SKILL.md` | 43 | Created + patched |
| `.claude/skills/shell-ops/SKILL.md` | 41 | Created |
| `.claude/skills/git-ops/SKILL.md` | 39 | Created |
| `.claude/skills/self-verification/SKILL.md` | 44 | Created + patched |
| `STATE/tools_registry.json` | ~150 | Created |
| `tools/__init__.py` | 0 | Created |
| `tools/registry.py` | 98 | Created |
| `tools/runner.py` | 266 | Created + patched (files.* support) |
| `tools/files.py` | 251 | Created |
| `PROTOCOL/telegram_commands.md` | ~300 | Created + finalized |
| `telegram/__init__.py` | 0 | Created |
| `telegram/parse.py` | 197 | Created |
| `telegram_bot.py` | 663 | Major rework: import shim, unified dispatcher, all 9 handlers, lock, signature normalization, tail validation, drop_pending removal |
| `README.md` | 244 | Created |
| `DIARY.md` | this file | Created |

**Total new code:** ~2,394 lines across 7 Python modules.

### What's pending

- [ ] Apply systemd unit changes (requires interactive `sudo`):
  ```
  sudo tee /etc/systemd/system/novacore-telegram.service < unit-content
  sudo systemctl daemon-reload
  sudo systemctl restart novacore-telegram.service
  ```
- [ ] Verify lock behavior live after systemd restart
- [ ] End-to-end Telegram test: send `/run` during restart window, confirm message not lost
- [ ] Task 0004 (real autonomy) still queued in `TASKS/`

---

## 2026-03-01 — Project Bootstrap, Watcher, Telegram Bot & Notifier

**Session span:** ~06:55–23:21 UTC (across ~28 Claude Code sessions)

### Phase 1: Project Bootstrap (06:55–08:31 UTC)

#### Initial setup

- Created project structure: `TASKS/`, `OUTPUT/`, `LOGS/`, `MEMORY/`, `WORK/`
- Created `CLAUDE.md` with operating rules, autonomy policy, execution model
- Created `TASKS/0001_bootstrap.md` — initial bootstrap task (completed)

#### watcher.py — Task Execution Dispatcher

Created `watcher.py` (368 lines) through iterative development across multiple sessions:

1. **v1 — Basic poller:** Polls `TASKS/` every 60 seconds, detects pending `.md` files, logs to `LOGS/watcher.log`. Graceful shutdown on SIGINT/SIGTERM.
2. **v2 — Execution dispatcher:** Added Claude subprocess execution via `claude --print` with `--allowedTools` flags. Task lifecycle: `.md` → `.inprogress` → `.done`/`.failed`.
3. **v3 — Artifact verification:** Post-execution checks verify OUTPUT file was created within 10 minutes. Specific artifact checks per task (e.g., task 0004 checks `WORK/real_autonomy_confirmed.txt`). Worker logs written to `LOGS/worker_<stem>.log`.
4. **v4 — Observable headless execution:** Added `cwd` setting, self-check prompt for the Claude worker, improved logging (command used, prompt header, verification results).

**Tasks completed via watcher:**
- 0001 bootstrap (manual)
- 0002 agent bootstrap (created watcher.py itself)
- 0004 real autonomy test (created `WORK/real_autonomy_confirmed.txt`)
- 0005 service test (confirmed systemd dispatch working)

#### Systemd Services Created

Three systemd unit files installed in `/etc/systemd/system/`:

| Service | Created | Purpose |
|---|---|---|
| `novacore-watcher.service` | ~08:30 UTC | Runs `watcher.py` as persistent daemon |
| `novacore-telegram.service` | ~18:35 UTC | Runs `telegram_bot.py` for Telegram → TASKS |
| `novacore-telegram-notifier.service` | ~19:00 UTC | Runs `telegram_notifier.py` for OUTPUT → Telegram |

All use `Restart=always`, `EnvironmentFile=/etc/novacore/telegram.env`, `User=nova`.

### Phase 2: Telegram Bot (18:35–21:30 UTC)

#### telegram_bot.py — Initial Creation

Created `telegram_bot.py` using `python-telegram-bot` library:

- Polls Telegram via `getUpdates` for incoming messages
- Authorized chat ID gating via `ALLOWED_CHAT_ID` env var
- Non-command messages create task files in `TASKS/` with `tg_` prefix and timestamp
- Task file format: markdown with `## Instruction` section containing message body
- Response sent back to user confirming task was queued

#### Step 1A — Command Protocol (first pass)

Added command handling to `telegram_bot.py`:
- `/run <title>` — queue a task with explicit title
- `/status` — list recent tasks
- `/cancel <id>` — cancel a task
- `/help` — show available commands
- Non-command messages still routed to task creation (original behavior)

#### Telegram Integration Testing (18:43–21:30 UTC)

End-to-end tests via live Telegram messages:

| Test | Time | Result |
|---|---|---|
| First Telegram task (`tg_ok.txt`) | 18:43 | Task created, worker executed, output generated |
| Auth gate test (`auth_ok.txt`) | 18:53 | ALLOWED_CHAT_ID correctly enforced |
| Notifier push test (`notifier_ok.txt`) | 19:01 | OUTPUT → Telegram notification delivered |
| Verbose mode test | 19:19 | Mode-aware notification formatting confirmed |
| Hello/echo test | 19:59 | Non-command message correctly created task |
| Ping/parsing test | 20:10 | Parser and notifier metadata extraction validated |
| Log test | 20:15 | Worker log creation and content verified |
| Format test | 20:20 | Output report formatting confirmed |
| Cancel test | 20:47 | Task cancellation flow verified |
| Sleep/timeout test | 20:52 | 90-second sleep task with worker timeout behavior |
| Cancel-last test | 21:21 | `/cancel last` resolved to most recent task |

### Phase 3: Telegram Notifier (19:00–23:21 UTC)

#### telegram_notifier.py — Creation and Iteration

Created `telegram_notifier.py` (551 lines) — watches `OUTPUT/` for `tg_*.md` files and sends Telegram notifications:

1. **v1 — Basic notifier:** Watchdog-based filesystem observer on `OUTPUT/`. Sends completion message via Telegram Bot API (`sendMessage` via httpx). Dedup via `STATE/tg_sent_outputs.txt` flat file.
2. **v2 — Smart summary extraction:** 6-tier fallback for extracting summaries from output reports (`## Summary` → `## Actions Taken` → `## Instruction` → header paragraph → any `##` section → first content line).
3. **v3 — Metrics and mode support:** Added latency calculation from filename timestamps, output size, worker log resolution (3-tier: exact base match → task_id match → glob fallback). Mode-aware formatting (compact/normal/verbose).
4. **v4 — Durable dedup (marker files):** Replaced flat-file dedup with atomic `O_CREAT|O_EXCL` marker files in `STATE/notified/`. One-time migration from legacy format. 7-day marker cleanup. Catch-up-on-start for unsent outputs. PID/hostname footer for debugging.

#### Key fixes during notifier development

- **Task ID extraction:** Added multi-pattern parsing (`**Task ID:**`, `**Task:**`, header regex)
- **Timestamp normalization:** ISO 8601 → human-readable UTC, filename-inferred timestamps as fallback
- **Worker log resolution:** 3-tier search (exact base, task_id, glob wildcard) to handle naming variations
- **Duplicate notification prevention:** Moved from flat-file append to atomic marker files to handle race conditions between filesystem events and catch-up scans
- **Chunked sending:** Messages over 3500 chars split at newline boundaries

### Systemd restarts during development

Multiple `systemctl restart novacore-telegram.service` calls during the session (20:58, 21:10, 21:19, 22:44 stop, 22:46 start) as code was iterated. These restarts with `RestartSec=3` are the root cause of the Conflict errors diagnosed on Mar 2.

### Environment setup

- Python venv at `/home/nova/nova-core/.venv/`
- Key packages: `python-telegram-bot`, `httpx`, `watchdog`
- Env file: `/etc/novacore/telegram.env` (root:root 0600) containing `TELEGRAM_BOT_TOKEN` and `ALLOWED_CHAT_ID`
- VPS: Vultr, Linux 5.15.0-171-generic

### Files created on Mar 1

| File | Lines | Notes |
|---|---|---|
| `CLAUDE.md` | ~40 | Project instructions, autonomy policy |
| `watcher.py` | 368 | Task execution dispatcher (4 iterations) |
| `telegram_bot.py` | ~350 | Initial bot with command handling (pre-Mar 2 rework) |
| `telegram_notifier.py` | 551 | OUTPUT → Telegram notifier (4 iterations) |
| Various `TASKS/` files | — | 0001–0005 + tg_* task files |
| Various `OUTPUT/` files | — | 14 output reports from executed tasks |
| `STATE/chat_modes.json` | — | Mode persistence |
| `STATE/notifier_mode.txt` | — | Notifier mode setting |
| `MEMORY.md` | ~20 | Auto-memory with project state |

---

*Diary format: one entry per working session. Add new entries above this line.*
