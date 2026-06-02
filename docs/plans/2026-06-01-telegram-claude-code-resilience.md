# Telegram Claude Code Resilience Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Make NovaCore Telegram autonomy resilient to Claude Code auth, quota, timeout, and long-running-process fragility.

**Architecture:** Telegram becomes a durable control plane that acknowledges quickly, queues work reliably, and reports results asynchronously. Long autonomous work is split into bounded resumable chunks coordinated by Python/Hermes state, while Claude Code remains a replaceable worker backend for short implementation/review steps.

**Tech Stack:** Python 3.10, python-telegram-bot, systemd, Claude Code CLI, NovaCore TASKS/OUTPUT/LOGS/STATE directories, pytest.

---

## Current Context

Relevant files:
- `telegram_bot.py` — Telegram message router, acknowledgments, completion watcher.
- `telegram/llm.py` — Claude Code CLI conversational wrapper.
- `watcher.py` — TASKS dispatcher that launches Claude Code workers.
- `systemd/novacore-telegram.service` — Telegram bot service definition.
- `systemd/novacore-watcher.service` — watcher service definition.
- `tests/test_ceo_nova_phase1.py` — Telegram routing/persona/conversation tests.
- `tests/test_phase9_presentation_layer.py` — Telegram presentation-layer tests.
- `LOGS/worker_*.log` — Claude worker execution logs.
- `STATE/` — durable runtime state.

Recent mitigation already applied:
- `telegram/llm.py` returns actionable auth/quota messages instead of only the generic snag fallback.
- `telegram_bot.py` uses deterministic queued-task acknowledgments if the LLM fails after the task is accepted.

Implementation stance:
- Keep diffs small and safe.
- Add tests before behavior changes where practical.
- Do not restart live services until after verification and operator approval.
- Do not change NovaTrade live trading behavior.

---

### Task 1: Add a Claude Code health probe module

**Objective:** Create a reusable preflight check that detects Claude Code availability, auth failures, and simple invocation failures before Telegram depends on the CLI.

**Files:**
- Create: `utils/claude_health.py`
- Create/modify: `tests/test_claude_health.py`

**Step 1: Write failing tests**

Add tests for:
- healthy CLI response
- missing binary
- auth failure text in stdout/stderr
- timeout

Test skeleton:

```python
from __future__ import annotations

import subprocess
from unittest import mock

from utils.claude_health import ClaudeHealth, check_claude_health


def test_claude_health_ok():
    completed = subprocess.CompletedProcess(
        args=["claude"], returncode=0, stdout="OK\n", stderr=""
    )
    with mock.patch("utils.claude_health.subprocess.run", return_value=completed):
        result = check_claude_health("/fake/claude", timeout_s=1)
    assert result.ok is True
    assert result.reason == "ok"


def test_claude_health_auth_failure():
    completed = subprocess.CompletedProcess(
        args=["claude"], returncode=1, stdout="Failed to authenticate. API Error: 401 Invalid authentication credentials", stderr=""
    )
    with mock.patch("utils.claude_health.subprocess.run", return_value=completed):
        result = check_claude_health("/fake/claude", timeout_s=1)
    assert result.ok is False
    assert result.reason == "auth_failed"
```

**Step 2: Run tests to verify failure**

Run:

```bash
.venv/bin/pytest tests/test_claude_health.py -q
```

Expected: FAIL because `utils.claude_health` does not exist.

**Step 3: Implement module**

Create `utils/claude_health.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
import subprocess


@dataclass(frozen=True)
class ClaudeHealth:
    ok: bool
    reason: str
    detail: str = ""


def classify_claude_error(text: str) -> str:
    lowered = text.lower()
    if "failed to authenticate" in lowered or "401 invalid authentication" in lowered:
        return "auth_failed"
    if "usage limit" in lowered or "rate_limit" in lowered or "rate limit" in lowered:
        return "usage_limited"
    if "not found" in lowered or "no such file" in lowered:
        return "missing_binary"
    return "cli_error"


def check_claude_health(claude_bin: str, timeout_s: int = 20) -> ClaudeHealth:
    cmd = [
        claude_bin,
        "-p",
        "--model",
        "haiku",
        "--max-turns",
        "1",
        "--no-session-persistence",
        "Reply with exactly OK",
    ]
    try:
        proc = subprocess.run(
            cmd,
            cwd="/home/nova/nova-core",
            text=True,
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
    except FileNotFoundError as exc:
        return ClaudeHealth(False, "missing_binary", str(exc))
    except subprocess.TimeoutExpired:
        return ClaudeHealth(False, "timeout", f"exceeded {timeout_s}s")
    except Exception as exc:
        return ClaudeHealth(False, "exception", str(exc))

    combined = f"{proc.stdout}\n{proc.stderr}".strip()
    if proc.returncode == 0 and proc.stdout.strip() == "OK":
        return ClaudeHealth(True, "ok", "")
    return ClaudeHealth(False, classify_claude_error(combined), combined[:500])
```

**Step 4: Run tests to verify pass**

Run:

```bash
.venv/bin/pytest tests/test_claude_health.py -q
```

Expected: PASS.

---

### Task 2: Surface health status in Telegram `/status` or `/briefing`

**Objective:** Make the operator see Claude Code health without digging through systemd journals.

**Files:**
- Modify: `telegram_bot.py`
- Modify: `tests/test_ceo_nova_phase1.py` or add focused status test file.

**Step 1: Locate status handler**

Search:

```bash
python3 - <<'PY'
from pathlib import Path
s = Path('telegram_bot.py').read_text()
for needle in ['def handle_status', '_handle_briefing', '/status']:
    print(needle, s.find(needle))
PY
```

**Step 2: Write failing test**

Test that status/briefing includes a Claude health line when the health probe returns unhealthy.

**Step 3: Implement minimal integration**

Import the health check:

```python
from utils.claude_health import check_claude_health
```

Add a helper:

```python
def _format_claude_health() -> str:
    health = check_claude_health(os.environ.get("CLAUDE_BIN", "/home/nova/.local/bin/claude"), timeout_s=10)
    if health.ok:
        return "Claude Code: healthy"
    return f"Claude Code: degraded ({health.reason})"
```

Call it from the status or briefing response.

**Step 4: Run focused tests**

Run:

```bash
.venv/bin/pytest tests/test_ceo_nova_phase1.py -q
```

Expected: PASS.

---

### Task 3: Add durable task state for chunked execution

**Objective:** Track multi-step/autonomous jobs as state machines instead of one monolithic 4-hour Claude process.

**Files:**
- Create: `utils/task_state.py`
- Create: `tests/test_task_state.py`
- Directory used: `STATE/task_state/`

**Step 1: Write failing tests**

Cover:
- create state
- mark chunk started
- mark chunk completed
- resume after partial completion
- serialize/deserialize JSON

**Step 2: Implement state model**

Use a dataclass with fields:

```python
@dataclass
class TaskState:
    task_id: str
    status: str  # pending|running|blocked|completed|failed
    current_step: int
    max_steps: int
    last_error: str = ""
    updated_at: str = ""
```

Functions:
- `load_task_state(task_id: str) -> TaskState | None`
- `save_task_state(state: TaskState) -> None`
- `advance_task_state(task_id: str) -> TaskState`
- `fail_task_state(task_id: str, error: str) -> TaskState`

**Step 3: Run tests**

```bash
.venv/bin/pytest tests/test_task_state.py -q
```

Expected: PASS.

---

### Task 4: Bound watcher Claude Code invocations

**Objective:** Prevent runaway 4-hour `claude -p` executions by adding explicit turn/budget limits for normal worker invocations.

**Files:**
- Modify: `watcher.py`
- Modify/add: watcher command construction tests.

**Step 1: Write failing test**

Assert the watcher command contains:
- `--max-turns`
- a small default for routine tasks, e.g. `12`
- optional override for special lanes if already supported

**Step 2: Implement command additions**

In watcher command construction around the existing `cmd = [CLAUDE_BIN, '-p', ...]`, add:

```python
"--max-turns",
os.environ.get("NOVA_CLAUDE_MAX_TURNS", "12"),
"--max-budget-usd",
os.environ.get("NOVA_CLAUDE_MAX_BUDGET_USD", "2.00"),
```

Do the same for reflexion retry with smaller limits:

```python
"--max-turns", "8"
```

**Step 3: Verify with tests**

Run:

```bash
.venv/bin/pytest tests/test_watcher_core.py tests/test_watcher_enhanced.py -q
```

Expected: PASS.

**Step 4: Manual dry command inspection**

Run a non-mutating inspection or unit test to ensure generated command is valid.

---

### Task 5: Add chunked continuation protocol to worker prompt

**Objective:** Teach workers to stop cleanly at chunk boundaries and emit machine-readable continuation state.

**Files:**
- Modify: `watcher.py`
- Modify/add: tests around `DISPATCH_PROMPT_TEMPLATE`

**Step 1: Add prompt contract text**

Extend the dispatch prompt with:

```text
CHUNKING CONTRACT:
- Treat this execution as one bounded chunk, not an indefinite autonomous session.
- If the full task cannot be completed within this chunk, write an OUTPUT file with status=partial and next_actions.
- Never rely on staying alive for hours. Persist progress to OUTPUT/ and STATE/.
- Prefer small verified steps over broad unverified work.
```

**Step 2: Add tests**

Assert the prompt contains:
- `CHUNKING CONTRACT`
- `status=partial`
- `next_actions`

**Step 3: Run tests**

```bash
.venv/bin/pytest tests/test_watcher_core.py -q
```

Expected: PASS.

---

### Task 6: Add fallback path for Telegram chat summaries

**Objective:** Keep Telegram responsive when Claude Code CLI is unhealthy by returning deterministic summaries for common paths.

**Files:**
- Modify: `telegram_bot.py`
- Modify: `telegram/llm.py`
- Add/modify Telegram tests.

**Step 1: Identify LLM-dependent Telegram paths**

Paths include:
- conversation replies
- delegation acknowledgments
- completion summaries
- autonomous completion notifications
- briefing

**Step 2: Add fallback formatter helpers**

In `telegram_bot.py`, create deterministic helpers:

```python
def _fallback_completion_summary(output_content: str) -> str:
    lines = [line.strip() for line in output_content.splitlines() if line.strip()]
    summary = next((line for line in lines if line.lower().startswith("summary")), "Task completed; see output for details.")
    return summary[:1000]
```

**Step 3: Use fallback when `generate_response` returns LLM error**

For completion summaries:

```python
summary = await generate_response(...)
if _is_llm_error_response(summary):
    summary = _fallback_completion_summary(output_content)
```

**Step 4: Test fallback behavior**

Mock `generate_response` to return `Claude auth failed...` and assert Telegram still produces useful text.

---

### Task 7: Add operational runbook and service-health checks

**Objective:** Make future recovery obvious and fast.

**Files:**
- Create: `docs/runbooks/telegram-claude-code-resilience.md`
- Optionally create: `scripts/check_telegram_claude_health.py`

**Step 1: Write runbook**

Include exact commands:

```bash
systemctl status novacore-telegram.service --no-pager
journalctl -u novacore-telegram.service -n 120 --no-pager
/home/nova/.local/bin/claude auth status --text
/home/nova/.local/bin/claude -p --model haiku --max-turns 1 'Reply with exactly OK'
python3 -m py_compile telegram_bot.py telegram/llm.py
.venv/bin/pytest tests/test_ceo_nova_phase1.py tests/test_phase9_presentation_layer.py -q
sudo systemctl restart novacore-telegram.service
```

**Step 2: Add failure interpretation table**

Table entries:
- 401 auth failure → run Claude Code auth login on VPS.
- usage limit/rate limit → wait/reset or switch fallback provider.
- 4h watcher timeout → inspect worker log, resume via chunk continuation.
- Telegram Bad Gateway → Telegram API transient unless persistent.

**Step 3: Verify commands manually where safe**

Do not restart service during plan execution without operator approval.

---

## Rollout Order

1. Implement Task 1 and Task 2 first so the system can diagnose itself.
2. Implement Task 6 next so Telegram stays useful during Claude outages.
3. Implement Task 3, Task 4, and Task 5 to reduce long-running Claude Code fragility.
4. Implement Task 7 last to document operations after behavior is stable.

## Acceptance Criteria

- Telegram never shows only the generic snag message for known Claude auth/quota failures.
- Queued tasks always return a deterministic queued acknowledgment even if conversational LLM generation fails.
- `/status` or `/briefing` surfaces Claude Code health.
- Worker Claude Code calls are bounded with max turns/budget.
- Long jobs can produce partial outputs and next actions instead of timing out silently after 4 hours.
- Tests cover health classification, Telegram fallback behavior, and watcher command bounds.
- Runbook exists with exact recovery commands.

## Verification Suite

Run before service restart:

```bash
python3 -m py_compile telegram_bot.py telegram/llm.py watcher.py utils/claude_health.py utils/task_state.py
.venv/bin/pytest tests/test_claude_health.py tests/test_task_state.py -q
.venv/bin/pytest tests/test_ceo_nova_phase1.py tests/test_phase9_presentation_layer.py -q
.venv/bin/pytest tests/test_watcher_core.py tests/test_watcher_enhanced.py -q
```

Expected: all pass.

## Deployment

After verification and operator approval:

```bash
sudo systemctl restart novacore-telegram.service
sudo systemctl restart novacore-watcher.service
systemctl is-active novacore-telegram.service novacore-watcher.service
journalctl -u novacore-telegram.service -n 40 --no-pager
journalctl -u novacore-watcher.service -n 40 --no-pager
```

## Risks and Mitigations

- Risk: `--max-turns` too low causes more partial outputs.
  - Mitigation: make max turns environment-configurable and start with moderate defaults.
- Risk: health probe consumes quota.
  - Mitigation: use haiku, max-turns 1, and cache health results briefly.
- Risk: chunking creates too many partial artifacts.
  - Mitigation: clear contract for partial status and next actions, plus cleanup rules later.
- Risk: fallback summaries are less natural.
  - Mitigation: prioritize reliability over style for outage paths.

## Non-Goals

- Replacing Claude Code entirely in this phase.
- Changing NovaTrade live trading strategy or risk behavior.
- Restarting live services automatically without operator approval.
- Building a full distributed queue system; use existing TASKS/OUTPUT/STATE primitives first.
