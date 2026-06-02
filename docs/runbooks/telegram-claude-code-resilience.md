# Runbook: Telegram ↔ Claude Code Resilience (Hermes control plane)

**Purpose:** Fast recovery when the NovaCore Telegram bot (the Hermes control plane) misbehaves
because the underlying Claude Code CLI is unhealthy (auth expired, usage/quota limited, missing
binary, or hung). This runbook also covers the watcher worker backend.

**Scope:** `novacore-telegram.service` (bot / control plane) and `novacore-watcher.service`
(TASKS dispatcher that launches Claude Code workers). Does **not** cover NovaTrade live trading.

---

## Symptoms → where to look

| Symptom | Likely cause | Jump to |
|---|---|---|
| `/status` shows `Claude Code: degraded (auth_failed)` | OAuth token expired | [Auth failure](#auth-failure-401) |
| `/status` shows `degraded (usage_limited)` | Quota / rate limit hit | [Usage limit](#usage-limit--rate-limit) |
| `/status` shows `degraded (missing_binary)` | `claude` not on PATH / wrong `CLAUDE_BIN` | [Missing binary](#missing-binary) |
| `/status` shows `degraded (timeout)` | CLI cold-start slow or hung | [Timeout](#cli-timeout) |
| Bot replies only "Sorry, I hit a snag…" | LLM path failing; fallbacks should now cover known cases | [Snag-only replies](#snag-only-replies) |
| Worker tasks never finish / run for hours | Unbounded `claude -p` worker | [Runaway worker](#runaway-worker) |
| Completion summaries are terse/auto | Deterministic fallback active (expected during outage) | [Fallback active](#fallback-summaries-active) |

---

## First-line diagnostics

```bash
# Service state
systemctl status novacore-telegram.service --no-pager
journalctl -u novacore-telegram.service -n 120 --no-pager

systemctl status novacore-watcher.service --no-pager
journalctl -u novacore-watcher.service -n 120 --no-pager

# Claude Code health (manual probe — mirrors utils/claude_health.check_claude_health)
/home/nova/.local/bin/claude -p --model haiku --no-session-persistence 'Reply with exactly OK'

# Python-level health probe (same classifier the bot uses)
cd /home/nova/nova-core
python3 scripts/check_telegram_claude_health.py
```

> NOTE: `--max-turns` is **NOT** a valid flag on this Claude CLI (v2.1.x). Do not add it to any
> probe or worker command — it will make the invocation fail. Use `--no-session-persistence`
> for one-shot probes and `--max-budget-usd` to bound spend.

---

## Failure interpretation & recovery

### Auth failure (401)
Classifier reason: `auth_failed`. The CLI prints `Failed to authenticate` / `401 Invalid authentication credentials`.

```bash
# Re-authenticate on the VPS (operator action — interactive).
/home/nova/.local/bin/claude   # then complete the login flow
# Long-lived headless token lives in /etc/novacore/telegram.env as CLAUDE_CODE_OAUTH_TOKEN,
# shared by bot + watcher. If the 8h OAuth token expired, refresh the long-lived token there.
```
After re-auth, re-probe (command above) and confirm `/status` shows `Claude Code: healthy`.

### Usage limit / rate limit
Classifier reason: `usage_limited`. Wait for the quota window to reset, or switch the fallback
provider/model. During the outage the bot still: (a) returns deterministic queued-task
acknowledgments, and (b) returns deterministic completion summaries — so the control plane stays
usable. No restart needed; recovery is automatic once quota returns.

### Missing binary
Classifier reason: `missing_binary`.
```bash
which claude
ls -l /home/nova/.local/bin/claude
echo "CLAUDE_BIN=$CLAUDE_BIN"   # bot falls back to /home/nova/.local/bin/claude if unset
```
Fix PATH / `CLAUDE_BIN`, then re-probe.

### CLI timeout
Classifier reason: `timeout`. The probe uses a 5s timeout and its result is TTL-cached for 60s,
so a slow CLI degrades `/status` text but does **not** block the bot event loop (the status
handler runs via `asyncio.to_thread`). If timeouts persist, inspect CLI cold-start / network and
check VPS load.

### Snag-only replies
Known Claude auth/quota failures should now produce actionable messages (auth/quota specific) and,
on the completion path, a deterministic fallback summary — never a bare "snag" for those cases.
If you still see snag-only replies:
```bash
journalctl -u novacore-telegram.service -n 200 --no-pager | grep -iE "snag|auth|usage|budget|timeout"
python3 -m py_compile telegram_bot.py telegram/llm.py
```
Confirm the LLM error sentinels in `telegram/llm.py` still match `_LLM_ERROR_PREFIXES` in
`telegram_bot.py` (these are the strings the fallback detects).

### Runaway worker
Worker `claude -p` calls are bounded by `--max-budget-usd` (env `NOVA_CLAUDE_MAX_BUDGET_USD`,
default `2.00`; retry path `NOVA_CLAUDE_RETRY_BUDGET_USD`, default `1.00`) plus the existing
`TASK_TIMEOUT` and the worker-prompt CHUNKING CONTRACT (workers must emit `status=partial` +
`next_actions` rather than run indefinitely).
```bash
# Inspect a stuck worker
ls -lt LOGS/worker_*.log | head
tail -n 80 LOGS/worker_<id>.log
# Tighten budget without code change:
sudo systemctl edit novacore-watcher.service   # set Environment=NOVA_CLAUDE_MAX_BUDGET_USD=1.00
```

### Fallback summaries active
Terse completion summaries (first `Summary:` line, or "Task completed; see output for details.")
mean the deterministic fallback fired because the conversational LLM call returned a known error
string. This is expected during a Claude outage — reliability over style. It self-heals when the
CLI is healthy again.

---

## Verification suite (run before any restart)

```bash
cd /home/nova/nova-core
python3 -m py_compile telegram_bot.py telegram/llm.py watcher.py utils/claude_health.py utils/task_state.py
.venv/bin/pytest tests/test_claude_health.py tests/test_task_state.py -q
.venv/bin/pytest tests/test_claude_health_status.py tests/test_watcher_budget.py -q
.venv/bin/pytest tests/test_ceo_nova_phase1.py tests/test_phase9_presentation_layer.py -q
.venv/bin/pytest tests/test_watcher_core.py tests/test_watcher_enhanced.py -q
```
Expected: all pass. (Known pre-existing exception: `TestLLMHelpers::test_constants` asserts a stale
model constant `claude-opus-4-6` vs current `claude-opus-4-8` — unrelated to this work.)

## Restart (operator approval required)

```bash
sudo systemctl restart novacore-telegram.service
sudo systemctl restart novacore-watcher.service
systemctl is-active novacore-telegram.service novacore-watcher.service
journalctl -u novacore-telegram.service -n 40 --no-pager
journalctl -u novacore-watcher.service -n 40 --no-pager
```

## Related
- Plan: `docs/plans/2026-06-01-telegram-claude-code-resilience.md`
- Health module: `utils/claude_health.py` · Durable task state: `utils/task_state.py`
- Bot: `telegram_bot.py` · LLM wrapper: `telegram/llm.py` · Worker dispatcher: `watcher.py`
