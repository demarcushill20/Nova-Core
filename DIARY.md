# NovaCore Development Diary

Reverse-chronological. Each entry covers one working session.

---

## 2026-03-05 (Session 12) — Repository Hygiene & Telegram Reliability

**Session span:** ~17:45–18:10 UTC

### What was fixed

Four targeted fixes applied — no new features, minimal diffs, all existing behavior preserved.

#### 1. Runtime artifacts untracked from git

`TASKS/`, `OUTPUT/`, `WORK/`, `STATE/*`, and `HEARTBEAT.md` were all committed to git, polluting history with transient runtime state. Updated `.gitignore` to exclude them while preserving `.gitkeep` placeholders and `STATE/tools_registry.json` (config, not runtime). Removed 100+ runtime files from the git index; all remain on disk.

#### 2. Telegram message reliability

Removed `drop_pending_updates=True` from `telegram_bot.py`. This flag discarded all queued Telegram updates on every startup — any `/run` command sent during a restart was permanently lost. The existing `fcntl` process lock already guarantees single-instance. Updated systemd unit to `RestartSec=10` (was 3) and added `TimeoutStopSec=20` to let Telegram's server-side long-poll expire before the new process starts.

#### 3. README accuracy

Corrected several stale docs:
- Notifier glob: `tg_*.md` → `*.md`
- Non-command messages: "silently ignored" → documents chat intent classification
- Added `/chat` and `/report` commands to command table
- Updated task lifecycle diagram to show intent flow
- Fixed verification section (removed contradictory `drop_pending_updates` check)
- Added heartbeat timer to services table

#### 4. Repository structure documented

README now separates "Source (version-controlled)" from "Runtime (gitignored)" with clear directory tables.

### Files changed

| File | Action | Purpose |
|---|---|---|
| `.gitignore` | Modified | Exclude TASKS/, OUTPUT/, WORK/, STATE/*, HEARTBEAT.md; keep .gitkeep |
| `telegram_bot.py` | Modified | Removed `drop_pending_updates=True` (1 line) |
| `systemd/novacore-telegram.service` | **NEW** | Repo copy with RestartSec=10, TimeoutStopSec=20 |
| `README.md` | Modified | Fixed inaccuracies, documented repo structure separation |
| `TASKS/.gitkeep` | **NEW** | Directory placeholder |
| `OUTPUT/.gitkeep` | **NEW** | Directory placeholder |
| `WORK/.gitkeep` | **NEW** | Directory placeholder |
| `STATE/.gitkeep` | **NEW** | Directory placeholder |

### Test results

194 total, all passing. No behavioral changes.

---

## 2026-03-05 (Session 11) — Heartbeat Health Monitoring System

**Session span:** ~16:35–17:45 UTC

### What was built

#### heartbeat.py — proactive health monitoring

Standalone stdlib-only script (~240 lines) that runs 9 health checks and reports status. Designed as a systemd oneshot triggered every 30 minutes.

**Health checks:**
1. `service:novacore-watcher` — systemd is-active + PID/uptime
2. `service:novacore-telegram` — same
3. `service:novacore-telegram-notifier` — same
4. `disk` — `os.statvfs`, warns at >85% usage
5. `claude_binary` — `/usr/bin/claude` exists and is executable
6. `task_queue` — pending count (warns >10), orphaned `.inprogress` files (>15min)
7. `last_output` — most recent OUTPUT/ file age (informational)
8. `stale_workers` — dead PIDs in `STATE/running/*.pid`
9. `metrics` — `STATE/metrics.json` failure rate (warns >50%)

**Outputs:**
- Writes `HEARTBEAT.md` with timestamped checklist
- Appends to `LOGS/heartbeat.log`
- Sends Telegram alert on failure (via `TELEGRAM_BOT_TOKEN` + `ALLOWED_CHAT_ID`)
- Injects self-repair tasks into `TASKS/` for service failures (rate-limited)

**Metrics parsing fix:** `contract_failure`/`contract_success` in `metrics.json` are dicts with `_total` keys, not plain ints. Added `isinstance(cf, dict)` dispatch.

#### systemd timer deployment

- `systemd/novacore-heartbeat.service` — Type=oneshot, EnvironmentFile=/etc/novacore/telegram.env
- `systemd/novacore-heartbeat.timer` — OnBootSec=2min, OnUnitActiveSec=30min
- Deployed to `/etc/systemd/system/`, enabled, verified firing

### Files changed

| File | Action | Purpose |
|---|---|---|
| `heartbeat.py` | **NEW** | 9 health checks, HEARTBEAT.md writer, Telegram alerter, self-repair injector |
| `tests/test_heartbeat.py` | **NEW** | 30 tests covering all checks, edge cases, integration |
| `systemd/novacore-heartbeat.service` | **NEW** | Oneshot service unit |
| `systemd/novacore-heartbeat.timer` | **NEW** | 30-minute timer unit |
| `HEARTBEAT.md` | **NEW** | Auto-generated health status (gitignored runtime artifact) |

### Test results

194 total (164 existing + 30 new), all passing.

### First live run

Timer fired immediately on enable. All services healthy, disk at 17.3%, only flag was metrics at 62.5% failure rate (5/8 historical contract failures) — will normalize as task volume grows.

---

## 2026-03-05 (Session 10) — Production Incident Recovery + Chat Mode

**Session span:** ~15:37–16:35 UTC

### Production incident: Telegram bot silent

**Root cause 1 — Conflict loop:** After a network disruption on Mar 3, the bot entered a `telegram.error.Conflict` loop (overlapping `getUpdates` requests). Fixed by SIGTERM + systemd respawn.

**Root cause 2 — Notifier only processed `tg_*` files:** The notifier filtered on `tg_*.md` output files (legacy naming). New numbered tasks (`0009_Wow_ok`) were never notified. Fixed by broadening the glob from `tg_*.md` to `*.md`.

**Hardening applied:**
- `drop_pending_updates=True` on `run_polling()` to avoid stale update conflicts on restart
- Error handler that calls `os._exit(1)` on Conflict errors → clean systemd restart
- Added structured logging (`_log = logging.getLogger("telegram_bot")`) with MSG/ACTION/PARSE_ERR/SKIP lines to stdout

### Feature: plain text as task

Previously only `/commands` were processed; plain text was silently ignored. Now any plain text message is routed through `_parse_run()` as a task — users can type conversationally.

### Feature: chat mode (intent-based output formatting)

**Problem:** Telegram replies were over-structured — CONTRACT blocks, Files Referenced, Security Notes, tool audit tables, `notifier_pid` telemetry, metadata lines. Normal questions deserve clean answers.

**Solution:** Intent classification + report stripping, applied at the notifier layer (single source of truth).

**Intent classification** (`classify_intent()` in `telegram/parse.py`):
- Plain text → `chat` (default)
- `/run`, `/status`, other commands → `task`
- `/chat <text>` → forced `chat`
- `/report <text>` → forced `task`
- Text containing "report", "contract", "verbose", "audit", "debug", "show files", "detailed" → `task`

**Report stripping** (`strip_report_sections()` in `telegram/format.py`):
- Truncates from first `## CONTRACT` / `## Files Referenced` / `## Security Notes` through EOF
- Removes metadata lines (`**Task:**`, `**Completed:**`, `**Source:**`)
- Removes title line (`# Task NNNN: ...`)
- Removes `notifier_pid=` / `host=` footer
- Removes CONTRACT field lines (`task_id:`, `status:`, `verification:`)
- Preserves all answer content including tables and code blocks

**Pipeline:** Bot stores intent in `STATE/intents/{stem}.intent` → notifier reads it → chat intent uses `strip_report_sections()`, task intent uses existing `build_message()`.

### Import shim fix

Initial implementation put `classify_intent()` in `telegram/format.py`, but `from telegram.format import ...` resolved to the python-telegram-bot library (not our local dir) due to the sys.path shim. Moved `classify_intent()` into `telegram/parse.py` which is already registered in `sys.modules` via the import shim in `telegram_bot.py`.

### Files changed

| File | Action | Purpose |
|---|---|---|
| `telegram/format.py` | **NEW** | `strip_report_sections()` — report section stripper |
| `telegram/parse.py` | Modified | Added `classify_intent()`, `/chat`, `/report` commands, plain-text-as-task routing |
| `telegram_bot.py` | Modified | Intent storage (`STATE/intents/`), logging, Conflict handler, `drop_pending_updates`, help text |
| `telegram_notifier.py` | Modified | Intent-aware formatting, broadened `*.md` glob, chat-mode message builder |
| `tests/test_chat_format.py` | **NEW** | 39 tests for classify_intent + strip_report_sections |

### Test results

164 total (125 existing + 39 new), all passing.

### Verified end-to-end

Task 0011 "Should we give Nova-Core a heartbeat first or persistent memory?" — classified as `chat`, worker produced 5232-char structured report, notifier stripped to 4737-char clean answer, delivered to Telegram without CONTRACT/metadata/footer.

---

## 2026-03-03 (Session 9) — Phase 3 Completion + Phase 4 Execution Audit & logs.tail

**Session span:** ~15:50–16:30 UTC

### What was built

#### Phase 3 / Step 4 — Observability Metrics

Added lightweight metrics tracking to `STATE/metrics.json` for contract validation and retry outcomes.

- `_update_metrics(event, tool_name)` helper in `watcher.py` — increments counters, never throws, never blocks
- 5 tracked events: `contract_success`, `contract_failure`, `retry_issued`, `retry_success`, `retry_failed`
- Per-stem breakdown with `_total` rollup
- Corruption-safe: invalid JSON or non-dict resets silently
- Wired into `verify_artifacts()` (success/failure + retry outcomes) and `_maybe_create_retry()` (retry_issued)
- `STATE/metrics.json` added to `.gitignore` (runtime state)
- 15 tests in `tests/test_metrics.py`

#### Phase 4 / Step 1 — Execution Audit Envelope

Added `_execute_with_audit()` wrapper to `tools/runner.py` that wraps every tool execution in a structured envelope.

- Validates tool exists in `tools_registry.json` before execution
- Times execution, returns `{tool, ok, duration_ms, result}` envelope
- Unregistered tools raise `ValueError` immediately — function never called
- `run_tool()` refactored: registry gate at top, all dispatches go through wrapper
- Error cases (blocked commands, unregistered tools) still return well-formed envelopes
- 10 tests in `tests/test_runner_audit.py`

#### Phase 4 / Step 2 — Semantic Tool: logs.tail

Added read-only log tailing for systemd services via journalctl.

- New adapter: `tools/adapters/logs_tool.py` — `logs_tail(service, lines)` function
- Runs `journalctl -u <service> -n <lines> --no-pager -o short-iso`
- Lines clamped to 1–500, output capped at 100KB
- Service name sanitized (alphanumeric, dash, underscore, dot, @)
- Returns structured dict: `{service, lines, entries[], truncated}`
- Registered in `tools/tools_registry.json` as `logs.tail`
- Wired into runner dispatch via `_run_logs_tail()`
- 13 tests in `tests/test_logs_tail.py`

### Test suite

| File | Tests |
|------|-------|
| test_contract_gate.py | 13/13 |
| test_contract_retry.py | 17/17 |
| test_contracts.py | 14/14 |
| test_git_repo.py | 28/28 |
| test_logs_tail.py | 13/13 |
| test_metrics.py | 15/15 |
| test_runner_audit.py | 10/10 |
| test_system_service.py | 15/15 |
| **Total** | **125/125** |

### Files changed

- `watcher.py` — metrics helper + wiring (+43 lines)
- `.gitignore` — added `STATE/metrics.json`
- `tools/runner.py` — audit envelope wrapper, registry gate, logs.tail dispatch (+86 lines)
- `tools/tools_registry.json` — added `logs.tail` entry
- `tools/adapters/logs_tool.py` — new adapter
- `tests/test_metrics.py` — new (15 tests)
- `tests/test_runner_audit.py` — new (10 tests)
- `tests/test_logs_tail.py` — new (13 tests)

### Commits

- `c85efc2` — Phase 3 Step 4: observability metrics
- `d71f688` — Phase 4 Step 1: execution audit envelope
- (pending) — Phase 4 Step 2: logs.tail semantic tool

---

## 2026-03-03 (Session 8) — MCP Server Setup + MCP Skills (Web/Fetch/Browser/Research)

**Session span:** ~14:00–15:00 UTC

### What was built

#### MCP Server Integration (4 servers)

Added 4 Model Context Protocol servers to Claude Code, providing web search, HTTP fetch, and browser automation capabilities.

**Discovery & fixes:**
- All 4 were initially configured with wrong package names (`@anthropic-ai/mcp-server-*` — doesn't exist on npm)
- Learned that MCP servers must be registered via `claude mcp add` into `~/.claude.json`, NOT manually in `~/.claude/settings.json`
- Each server required iterative debugging across multiple Claude Code restarts

| Server | Wrong package | Correct package | Notes |
|---|---|---|---|
| brave-search | `@anthropic-ai/mcp-server-brave-search` | `@brave/brave-search-mcp-server` | API key moved from env var to `--brave-api-key` CLI flag |
| tavily | (correct from start) | `tavily-mcp@latest` | Worked on first try |
| fetch | `@anthropic-ai/mcp-server-fetch` (npm) | `mcp-server-fetch` (Python via `uv tool run`) | Official server is Python-only, not npm |
| playwright | `@anthropic-ai/mcp-server-playwright` | `@playwright/mcp` | Required `--headless --no-sandbox --executable-path` flags |

#### Playwright System Dependencies (no-sudo workaround)

Chromium binary requires GTK/ATK system libraries not present on the headless VPS. Without sudo access:

1. Downloaded 24 `.deb` packages via `apt-get download` (no sudo needed)
2. Extracted shared libraries to `~/.local/usr/lib/x86_64-linux-gnu/` via `dpkg-deb -x`
3. Configured `LD_LIBRARY_PATH` env var in the MCP server config
4. Final config: `--headless --no-sandbox --executable-path /home/nova/.cache/ms-playwright/chromium-1208/chrome-linux64/chrome`

Missing libs resolved: `libatk-1.0`, `libatk-bridge-2.0`, `libcups2`, `libxkbcommon0`, `libatspi2.0`, `libxcomposite1`, `libxdamage1`, `libxrandr2`, `libcairo2`, `libpango-1.0`, `libasound2`, + transitive deps.

#### MCP Skills (4 new skills under `.claude/skills/`)

Built Anthropic-style Claude Code Skills teaching correct MCP tool usage:

| Skill | Path | Purpose | Mode | Tools |
|---|---|---|---|---|
| `web-research` | `.claude/skills/web-research/` | Multi-engine search with citations & query log | Auto-invoked | 4 (brave_web_search, brave_news_search, tavily_search, tavily_research) |
| `http-fetch` | `.claude/skills/http-fetch/` | Deterministic URL retrieval with parsing patterns | Auto-invoked | 2 (fetch, tavily_extract) |
| `browser-automation` | `.claude/skills/browser-automation/` | Playwright automation with failure handling | Auto-invoked | 17 playwright tools |
| `research-to-action` | `.claude/skills/research-to-action/` | Workflow chaining search→fetch→browse | Operator (`/research-to-action`) | 14 (union of key tools) |

**Skill conventions (Anthropic-style):**
- YAML frontmatter: `name`, `description`, `disable-model-invocation`, `allowed-tools`
- Each SKILL.md contains: When to use, Inputs, Workflow, Tool usage rules, Outputs/contract, 2 Examples
- Reference files linked one level deep (no nesting)

**Supporting reference files:**
- `web-research/reference/SOURCES_RUBRIC.md` — 4-tier source quality rubric + conflict resolution rules
- `http-fetch/reference/PARSING_PATTERNS.md` — HTML/JSON/text parsing patterns + failure handling table

**Key design: research-to-action decision tree:**
```
SEARCH (always start here)
  → found answer in snippet? → SYNTHESIZE
  → found URL? → FETCH
  → nothing? → retry once → REPORT GAP

FETCH
  → got content? → SYNTHESIZE
  → empty/JS-rendered? → BROWSE (only if depth=full)
  → failed? → try tavily_extract → REPORT CONSTRAINT

BROWSE (last resort)
  → got content? → close browser → SYNTHESIZE
  → login/captcha? → STOP → REPORT CONSTRAINT
```

### Tool inventory (MCP tools discovered)

| Server | Tool count | Key tools |
|---|---|---|
| brave-search | 6 | `brave_web_search`, `brave_news_search`, `brave_local_search`, `brave_video_search`, `brave_image_search`, `brave_summarizer` |
| tavily | 5 | `tavily_search`, `tavily_extract`, `tavily_crawl`, `tavily_map`, `tavily_research` |
| fetch | 1 | `fetch` |
| playwright | 22 | `browser_navigate`, `browser_snapshot`, `browser_click`, `browser_type`, `browser_evaluate`, + 17 more |

All tool names use MCP qualified format: `mcp__<server>__<tool_name>`

### Files created

| File | Action |
|---|---|
| `.claude/skills/web-research/SKILL.md` | Created — web research skill |
| `.claude/skills/web-research/reference/SOURCES_RUBRIC.md` | Created — source quality rubric |
| `.claude/skills/http-fetch/SKILL.md` | Created — HTTP fetch skill |
| `.claude/skills/http-fetch/reference/PARSING_PATTERNS.md` | Created — parsing patterns |
| `.claude/skills/browser-automation/SKILL.md` | Created — browser automation skill |
| `.claude/skills/research-to-action/SKILL.md` | Created — workflow chaining skill |

**Also modified (infrastructure):**
| File | Action |
|---|---|
| `~/.claude.json` | MCP servers registered (brave-search, tavily, fetch, playwright) |
| `~/.claude/settings.json` | Cleaned up stale `mcpServers` key |
| `~/.local/usr/lib/x86_64-linux-gnu/` | 47 shared libraries extracted for Playwright |

### Packages installed

| Package | Method | Purpose |
|---|---|---|
| `uv` (0.10.7) | `pip3 install uv` | Python tool runner for mcp-server-fetch |
| Chromium 145.0.7632.6 | `npx playwright install chromium` | Browser for Playwright MCP |
| 24 .deb packages | `apt-get download` + `dpkg-deb -x` | System libs for Chromium |

### Design decisions

- **`claude mcp add` over manual JSON editing**: Claude Code reads MCP config from `~/.claude.json`, not `~/.claude/settings.json`. The `claude mcp add -s user` CLI is the canonical way to register servers.
- **Local lib extraction over sudo**: Extracted .deb packages to `~/.local/` and used `LD_LIBRARY_PATH` rather than requiring root access. Makes the setup portable and non-destructive.
- **Separate skills over one mega-skill**: Each MCP capability (search, fetch, browse) gets its own skill with minimal tool lists. The `research-to-action` workflow skill chains them but is operator-invoked only (`disable-model-invocation: true`).
- **Anthropic frontmatter style for new skills**: Used `allowed-tools` + `disable-model-invocation` format (Anthropic convention) rather than the existing `activation.keywords` + `tool_doctrine` format used by Phase 1 skills. Both formats coexist — Phase 1 skills teach behavior, MCP skills teach tool selection.

---

## 2026-03-03 (Session 7) — Phase 2 Completion + Phase 3 Contract Enforcement

**Session span:** ~14:00–16:00 UTC

### What was built

#### Phase 2 / Step 5 — `repo.git.commit` (state-changing, safety-enforced)

Extended `tools/adapters/git_repo.py`:
- `git_commit(message, paths=None)` → structured dict with `action`, `message`, `commit_hash`, `files`, `success`, `verification`
- **Forbidden flag rejection**: regex blocks `--amend`, `--no-verify`, `--force`, `--allow-empty`, `-a` in commit messages
- **Flag injection prevention**: paths starting with `-` are rejected; `--` separator used before path args
- **5-step workflow**: status check → stage paths → verify staging (`git diff --cached --name-only`) → commit → verify via `git log -1 --oneline`
- Runner dispatch wired via lazy import in `_run_repo_git_commit()`

**Phase 2 complete.** All 5 semantic tool adapters built and tested:

| Tool | Type | Adapter |
|---|---|---|
| `system.service.status` | Read-only | `system_service.py` |
| `system.service.restart` | State-changing (gated) | `system_service.py` |
| `repo.git.status` | Read-only | `git_repo.py` |
| `repo.git.diff` | Read-only | `git_repo.py` |
| `repo.git.commit` | State-changing (safety-enforced) | `git_repo.py` |

#### Phase 3 / Step 1 — `contracts.validate` tool

Created `tools/contracts.py`:
- `validate_contract(text)` → `{valid, errors, warnings, contract}`
- Locates the **last** `## CONTRACT` header in text, parses `key: value` pairs below it
- Required fields: `summary`, `verification`, `confidence`
- Requires at least one action-detail field: `files_changed`, `commands_executed`, `git_commands_executed`, `task_id`, `status`, `checks_performed`
- Confidence validation: accepts `0.0–1.0` float or `low`/`medium`/`high`
- Code fences inside CONTRACT blocks are skipped (content inside ``` ignored)
- Deterministic — no LLM calls, pure string parsing

Registered as `contracts.validate` in `tools/tools_registry.json`. Runner dispatch via `_run_contracts_validate()`.

#### Phase 3 / Step 2 — Contract enforcement gate on task completion

Modified `watcher.py` `verify_artifacts()`:
- After finding the OUTPUT file, calls `_check_contract(output_file)` which runs `validate_contract(text)`
- **Valid contract** → proceeds to `.done` as before
- **Invalid contract** → `passed = False` → task becomes `.failed`
- Failure appends a `## CONTRACT VALIDATION FAILED` section to the output file with:
  - List of validation errors
  - List of warnings (if any)
  - Suggestion to fix output with required fields

Flow: OUTPUT found → contract validated → `.done` **or** contract invalid → failure section appended → `.failed`

### Test results

| Test suite | Tests | Status |
|---|---|---|
| `tests/test_git_repo.py` | 28 | All passing |
| `tests/test_system_service.py` | 15 | All passing |
| `tests/test_contracts.py` | 14 | All passing |
| `tests/test_contract_gate.py` | 13 | All passing |
| **Total** | **70** | **All passing** |

### Files created or modified

| File | Action |
|---|---|
| `tools/adapters/git_repo.py` | Extended — `git_commit()` added |
| `tools/contracts.py` | Created — contract validator |
| `tools/runner.py` | Modified — dispatch for `repo.git.commit` + `contracts.validate` |
| `tools/tools_registry.json` | Modified — 2 new tool entries |
| `watcher.py` | Modified — `_check_contract()` + contract gate in `verify_artifacts()` |
| `tests/test_git_repo.py` | Extended — 7 commit tests |
| `tests/test_contracts.py` | Created — 14 validator tests |
| `tests/test_contract_gate.py` | Created — 13 gate tests |

### Git history (session 7)

| Commit | Message |
|---|---|
| `cce37f2` | feat: Phase 2 add repo.git.commit adapter |
| `4b72994` | feat: Phase 3 add contracts.validate tool |
| `e64523d` | feat: Phase 3 add contract enforcement gate on task completion |

### Design decisions

- **Gate at `verify_artifacts()`, not at file write time**: The contract gate runs after Claude finishes and the OUTPUT file exists. This means Claude's raw output is preserved (the failure section is appended, not replacing). The gate is the single chokepoint before `.done` transition.
- **Last CONTRACT block wins**: If an output has multiple `## CONTRACT` sections (e.g., from retries), only the last one is validated. This supports iterative correction within a single output.
- **Failure report appended to output file**: Rather than creating a separate error file, the validation failure is appended to the existing OUTPUT file. This keeps the full context (original output + why it failed) in one place.

---

## 2026-03-02 (Session 6) — Phase 2 Tool Abstraction Layer (Semantic Adapters)

**Session span:** ~15:00–17:00 UTC

### What was built

#### Phase 2 — Tool Abstraction Layer (Steps 1–4)

Replaced raw shell/git stdout parsing with structured JSON tool adapters. Agents now interact with semantic APIs instead of parsing terminal output.

#### Architecture decisions

- **Code-owned registry**: moved authoritative tool definitions from `STATE/tools_registry.json` (runtime state) to `tools/tools_registry.json` (code-owned, versioned). `tools/registry.py` updated to default to `tools/tools_registry.json`.
- **Lazy imports**: runner dispatches to adapters via lazy `from tools.adapters.X import Y` inside handler functions, avoiding circular imports (adapters import `run_subprocess` from runner).
- **Adapter pattern**: each adapter module lives under `tools/adapters/`, calls `run_subprocess()` from runner, parses output, returns structured dict.

#### Step 1 — `system.service.status` (read-only)

Created `tools/adapters/system_service.py`:
- `parse_status_output(stdout)` — extracts Loaded, Active, Main PID from systemctl output
- `service_status(name)` → structured dict with `service`, `loaded`, `active_state`, `sub_state`, `main_pid`, `raw_excerpt`
- Service name sanitized via regex (alphanumeric, dash, underscore, dot, @)
- Exit code 3 (inactive) treated as non-error

#### Step 2 — `system.service.restart` (state-changing, gated)

Extended `tools/adapters/system_service.py`:
- `service_restart(name)` → structured dict with `service`, `action`, `success`, `active_state`, `sub_state`, `main_pid`, `verification`
- **Confirmation gate**: requires `NOVACORE_CONFIRM=ALLOW_DESTRUCTIVE` env var. Without it, returns `blocked: true` with reason — command never executes.
- **Post-restart verification**: immediately calls `service_status()` after restart to confirm the service came back `active (running)`.
- Restart failures return `blocked: false` with stderr reason.

#### Step 3 — `repo.git.status` (read-only)

Created `tools/adapters/git_repo.py`:
- `parse_porcelain(output)` — parses `git status --porcelain=v1 -b` output
- `git_status()` → structured dict with `branch`, `remote`, `ahead`, `behind`, `staged`, `modified`, `untracked`, `clean`
- Staged/modified files include status code (`A`, `M`, `D`, etc.) and path
- `MM` files correctly appear in both staged and modified lists

#### Step 4 — `repo.git.diff` (read-only)

Extended `tools/adapters/git_repo.py`:
- `parse_diff(output)` — parses `git diff --unified=3` output into per-file records
- `git_diff(path=None)` → structured dict with `files`, `total_files`, `total_additions`, `total_deletions`, `empty`
- Each file entry contains `path`, `additions`, `deletions`, `excerpt` (first ~20 lines)
- Counts exclude `+++`/`---` header lines (only real content changes)
- Optional `path` argument scopes diff to a single file (uses `--` separator to prevent flag injection)
- Paths starting with `-` are rejected as potential flag injection

### Test results

| Test suite | Tests | Status |
|---|---|---|
| `tests/test_system_service.py` | 15 | All passing |
| `tests/test_git_repo.py` | 21 | All passing |
| **Total** | **36** | **All passing** |

All tests use mocked subprocess output — no systemd or git repo required.

### Files created or modified

| File | Action |
|---|---|
| `tools/adapters/__init__.py` | Created (package) |
| `tools/adapters/system_service.py` | Created (status + restart adapters) |
| `tools/adapters/git_repo.py` | Created (git status + diff adapters) |
| `tools/tools_registry.json` | Created (code-owned, 4 new tools registered) |
| `tools/registry.py` | Modified — default path → `tools/tools_registry.json` |
| `tools/runner.py` | Modified — dispatch for 3 new tool names |
| `tests/__init__.py` | Created (package) |
| `tests/test_system_service.py` | Created (15 tests) |
| `tests/test_git_repo.py` | Created (21 tests) |

### Git history (session 6)

| Commit | Message |
|---|---|
| `c21426b` | feat: Phase 2 add system.service.status adapter |
| `75d4f87` | feat: Phase 2 add system.service.restart adapter |
| `9463bf3` | feat: Phase 2 add repo.git.status adapter |
| (pending) | feat: Phase 2 add repo.git.diff adapter |

### Semantic tool namespace (current state)

| Tool | Type | Adapter |
|---|---|---|
| `system.service.status` | Read-only | `system_service.py` |
| `system.service.restart` | State-changing (gated) | `system_service.py` |
| `repo.git.status` | Read-only | `git_repo.py` |
| `repo.git.diff` | Read-only | `git_repo.py` |

---

## 2026-03-02 (Session 5) — Phase 1 Skill Standardization (All 5 Skills)

**Session span:** ~10:20–15:00 UTC

### What was built

#### Phase 1 — Skill Standardization (Foundation Layer)

Brought all 5 skills into a standardized format following Anthropic-aligned skill doctrine. Each skill now has:

- **Proper YAML frontmatter** with `name`, `description`, `activation.when`, `tool_doctrine`, and `output_contract`
- **6 mandatory sections** in exact order: `When To Use`, `Workflow`, `Tool Usage Rules`, `Verification`, `Failure Handling`, `Output Contract`
- **Progressive disclosure docs**: `reference.md` (deep detail, edge cases) and `examples.md` (concrete workflows with machine-checkable contracts)

#### Skills standardized (in order)

| Skill | Commit | Files | Key reference.md topics |
|---|---|---|---|
| `file-ops` | `a3205c0` | 3 (292 ins) | Edge cases (missing file, large file, binary, conflicts), style rules (minimal diffs, atomic edits, no silent overwrite) |
| `shell-ops` | `2a51570` | 3 (322 ins) | Deny pattern philosophy, confirmation override (`NOVACORE_CONFIRM`), safe command patterns, exit code interpretation |
| `git-ops` | `09ae19e` | 3 (321 ins) | Git as audit trail, forbidden operations table, safe operation sequence, commit message discipline, divergence handling |
| `task-execution` | `1c4734e` | 3 (346 ins) | Lifecycle states/transitions, atomic rename philosophy, crash recovery, output naming conventions, idempotency, never-deletion |
| `self-verification` | `95061b9` | 3 (339 ins) | Verification philosophy (never assume success), read-after-write principle, exit code discipline, confidence scoring, false-positive patterns |

#### Output Contract standard

Every skill now ends with a machine-checkable contract. Each skill defines its own required fields:

| Skill | Contract fields |
|---|---|
| `file-ops` | `summary`, `files_changed`, `verification` |
| `shell-ops` | `summary`, `commands_executed`, `verification` |
| `git-ops` | `summary`, `git_commands_executed`, `verification` |
| `task-execution` | `summary`, `task_id`, `status`, `verification` |
| `self-verification` | `summary`, `checks_performed`, `result`, `confidence` |

#### Tool doctrine standard

Each skill declares its behavioral workflow rules in frontmatter:

| Skill | Doctrine key | Workflow rules |
|---|---|---|
| `file-ops` | `tool_doctrine.files.workflow` | `read_before_write`, `diff_first`, `verify_after_write` |
| `shell-ops` | `tool_doctrine.runtime.workflow` | `sandbox_only`, `never_bypass_runner`, `respect_deny_patterns`, `require_confirmation_for_sensitive` |
| `git-ops` | `tool_doctrine.repo.workflow` | `prefer_status_diff_first`, `no_force_push`, `no_rebase`, `no_reset_hard`, `no_clean_fd` |
| `task-execution` | `tool_doctrine.runtime.workflow` | `read_task_before_execute`, `atomic_state_transition`, `write_output_before_done`, `never_delete_task_files` |
| `self-verification` | `tool_doctrine.runtime.workflow` | `check_expected_state`, `confirm_no_errors`, `validate_contract_fields`, `prefer_read_after_write` |

### Verification discipline

Each step followed a spot-check protocol before commit:
- YAML frontmatter parsed with `python3 -c "import yaml; ..."` to confirm validity
- `grep -n "## CONTRACT"` on examples.md to confirm all examples have contract blocks
- `git status --short` to confirm only the target skill directory was modified
- `git diff --stat` to confirm no runtime code changed

### Files created or modified

| File | Action |
|---|---|
| `.claude/skills/file-ops/SKILL.md` | Rewritten (Phase 1 format) |
| `.claude/skills/file-ops/reference.md` | Created |
| `.claude/skills/file-ops/examples.md` | Created |
| `.claude/skills/shell-ops/SKILL.md` | Rewritten (Phase 1 format) |
| `.claude/skills/shell-ops/reference.md` | Created |
| `.claude/skills/shell-ops/examples.md` | Created |
| `.claude/skills/git-ops/SKILL.md` | Rewritten (Phase 1 format) |
| `.claude/skills/git-ops/reference.md` | Created |
| `.claude/skills/git-ops/examples.md` | Created |
| `.claude/skills/task-execution/SKILL.md` | Rewritten (Phase 1 format) |
| `.claude/skills/task-execution/reference.md` | Created |
| `.claude/skills/task-execution/examples.md` | Created |
| `.claude/skills/self-verification/SKILL.md` | Rewritten (Phase 1 format) |
| `.claude/skills/self-verification/reference.md` | Created |
| `.claude/skills/self-verification/examples.md` | Created |

**Total:** 15 files (5 rewritten, 10 created), ~1,620 lines added.

### Git history (session 5)

| Commit | Message |
|---|---|
| `a3205c0` | feat: Phase 1 standardize file-ops skill |
| `2a51570` | feat: Phase 1 standardize shell-ops skill |
| `09ae19e` | feat: Phase 1 standardize git-ops skill |
| `1c4734e` | feat: Phase 1 standardize task-execution skill |
| `95061b9` | feat: Phase 1 standardize self-verification skill |

### Design decisions

- **No runtime code changes:** Phase 1 is purely cognitive — skills guide agent behavior, runner enforces safety. The two layers evolve independently.
- **Progressive disclosure (SKILL.md → reference.md → examples.md):** Keeps the injected prompt concise while providing deep detail when needed. Mirrors Anthropic's internal skill playbook structure.
- **Machine-checkable contracts over freeform output:** Enables future Phase 3 contract validation (automated verification that agent outputs meet spec).

---

## 2026-03-02 (Session 4) — Runner Safety Hardening & Shell Skill Smoke Test

**Session span:** ~09:10–09:45 UTC

### What was built

#### Step 2.5-alt — Runner Safety Hardening (`tools/runner.py`)

Replaced naive substring denylist with regex-based, word-boundary-aware safety enforcement. Even with `--dangerously-skip-permissions` on the Claude CLI, destructive operations are blocked at the tool-runner layer.

**Shell denylist — 11 regex patterns:**

| Pattern | Blocks |
|---|---|
| `rm -rf /` | Recursive delete on critical paths (`/`, `~`, `/home`, `/etc`, `/usr`, `/bin`, `/lib`) |
| `dd of=/` | Block-device writes |
| `mkfs`, `wipefs`, `shred` | Filesystem destructors |
| `:(){ :|:& };:` | Fork bombs |
| `shutdown`, `reboot`, `halt`, `poweroff` | System power commands |
| `init 0`, `init 6` | Runlevel changes |
| `chmod/chown -R /` | Recursive permission changes on critical paths |
| `curl\|bash`, `wget\|sh` | Pipe remote content to shell |
| `> /etc/...` | Redirect writes to system directories |
| `fdisk` | Disk partitioning |
| `apt`, `dnf`, `yum` | Package managers (require approval) |

**Git safety — expanded allowlist + 7 deny patterns:**

- Allowlist added: `fetch`, `pull`, `merge`, `tag`, `stash`, `remote`, `switch`, `restore`, `rev-parse`
- Deny patterns: `--force`, `-f`, `--force-with-lease`, `reset --hard`, `clean -fdx`, `rebase`, `filter-branch`, `merge --strategy=ours`

**Confirmation escape hatch:**

Blocked commands return `exit_code: 126` with message: `"BLOCKED: <reason>. To override, set env NOVACORE_CONFIRM=ALLOW_DESTRUCTIVE"`. Override checked via `os.environ.get("NOVACORE_CONFIRM")`.

**Secret redaction — expanded:**

Added token-prefix patterns: `ghp_` (GitHub PATs), `github_pat_` (fine-grained PATs), `xoxb-` (Slack bot tokens). Applied to both stdout and stderr.

#### Shell Skill Smoke Test (Task 0007)

Created `TASKS/0007_shell_skill_smoke.md` with `$ pwd` and `$ ls -la` command lines. Verified:
- `_SHELL_CMD_RE` correctly triggered shell-ops from `$`-prefixed lines
- Skills selected: `self-verification`, `shell-ops`, `task-execution` (no file-ops — correct)
- Worker executed both commands and reported active skills in output

### Test results

`tools/dev_safety_smoke.py` — 26/26 passing:

| Test | Result |
|---|---|
| `rm -rf /` | Blocked |
| `curl https://x \| bash` | Blocked |
| `ls -la` | Allowed |
| `cat /etc/hostname` | Allowed |
| `grep -r pattern .` | Allowed |
| `systemctl status foo` | Allowed |
| `mkfs.ext4 /dev/sda1` | Blocked |
| `wget http://x \| sh` | Blocked |
| `chmod -R 777 /` | Blocked |
| `echo > /etc/passwd` | Blocked |
| `dd of=/dev/sda` | Blocked |
| `shred /dev/sda` | Blocked |
| `git push --force` | Blocked |
| `git status` | Allowed |
| `git push -f` | Blocked |
| `git reset --hard` | Blocked |
| `git diff` | Allowed |
| `git commit -m 'msg'` | Allowed |
| `git fetch origin` | Allowed |
| `git pull` | Allowed |
| `git rebase main` | Blocked |
| `git push --force-with-lease` | Blocked |
| `git filter-branch` | Blocked |
| `TELEGRAM_TOKEN` redacted | Pass |
| `ghp_` token redacted | Pass |
| `xoxb-` token redacted | Pass |

### Bug fixes

1. **Regex `\b` after `/`** — `\b` (word boundary) doesn't fire after `/` since `/` is not a word character. Fixed by using `(\s|$)` instead of `\b` at end of critical-path patterns (`rm -rf /`, `chmod -R /`).

### Files created or modified

| File | Lines | Action |
|---|---|---|
| `tools/runner.py` | 310 | Major rework: regex denylist, expanded git safety, confirm token, token redaction |
| `tools/dev_safety_smoke.py` | 96 | Created — 26 inline smoke tests |
| `OUTPUT/0007_shell_skill_smoke__20260302-091514.md` | 52 | Task output (shell skill verification) |
| `TASKS/0007_shell_skill_smoke.md.done` | — | Completed task |
| `WORK/skill_injection_0007_shell_skill_smoke.txt` | — | Skill injection audit artifact |

### Git history (session 4)

| Commit | Message |
|---|---|
| `28d030f` | test: skill smoke tests — file-ops and shell-ops activation verified |
| `a0987d6` | feat: Step 2.5-alt runner safety hardening (denylist + confirm token) |

---

## 2026-03-02 (Session 3) — Skill Activation Hardening & End-to-End Verification

**Session span:** ~08:30–09:15 UTC

### What was built

#### Skill Injection CLI Flag Fix

Resolved the `--append-system-prompt-file` vs `--append-system-prompt` mismatch:
- The Claude CLI (`/usr/bin/claude -p --help`) only supports `--append-system-prompt <prompt>` (inline string), not a file-based variant.
- An intermediate attempt added feature-detection (`claude_supports_append_prompt_file()`) but this was unnecessary complexity.
- Final state: inline `--append-system-prompt` with content passed directly. Skill injection files still written to `WORK/` for auditability.

#### Shell-ops False Positive Elimination (Two Rounds)

**Round 1 — Substring → word-boundary:** Moved shell-ops keywords from naive substring matching (`"ls" in text`) to `\b` word-boundary regex. Fixed "lines" matching "ls" and "include" matching "run".

**Round 2 — Word-boundary → intent-based:** Discovered `\bshell\b` still matched "no shell commands" in plain English. Split shell-ops matching into three layers:

| Layer | Pattern | Example |
|---|---|---|
| `_SHELL_CMD_RE` | `^\s*\$\s+\S` (multiline) | `$ ls -la` |
| `_SHELL_CMDS_RE` | `\b(ls\|sudo\|systemctl\|...)\b` | `sudo apt install htop` |
| `_SHELL_INTENT_RE` | action verb + intent word pairing | `run this command in terminal` |

Key insight: intent words like "shell", "terminal", "command" only trigger when paired with action verbs ("run", "execute", "use", "open", "launch", "start"). This prevents false positives from sentences like "no shell commands" while still matching "run this command in terminal".

#### End-to-End Verification

- Killed stale watcher process (PID 147234 — old code from before skill patches)
- Tasks 0006 + 0007 confirmed skill injection pipeline works
- Task 0006_skill_smoke confirmed worker reports active skills in output

### Bug fixes

1. **Stale watcher process** — Two watcher.py processes running simultaneously (old PID 147234 pre-dating skill patches, new PID 179373 from restart). Old process dispatched task 0006 without skill injection. Killed stale process; subsequent tasks used correct code.

2. **shell-ops false positive (round 1)** — Substring match `"ls" in text_lower` matched "lines", "false", etc. Fixed with `\b` word-boundary regex.

3. **shell-ops false positive (round 2)** — `\bshell\b` matched "no shell commands" in plain English. Fixed by requiring intent words to co-occur with action verbs via `_SHELL_INTENT_RE`.

### Test matrix (final state)

| Input | shell-ops? | Correct? |
|---|---|---|
| "This line mentions lines and include but no shell commands." | No | Yes |
| `$ ls -la` | Yes | Yes |
| `sudo apt install htop` | Yes | Yes |
| `run this command in terminal` | Yes | Yes |
| `use systemctl to restart the service` | Yes | Yes |
| `show the first 15 lines` | No | Yes |
| `List the directory` | No | Yes |

### Git history (session 3)

| Commit | Message |
|---|---|
| `ac8f0d6` | test: verify skill activation engine end-to-end |
| `8408e63` | fix: prevent accidental shell-ops activation (word-boundary matching) |
| `e9f926f` | fix: eliminate shell-ops false positives (token-based intent matching) |

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
