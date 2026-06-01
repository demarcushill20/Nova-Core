"""Claude CLI conversation wrapper for CEO Nova.

Spawns the same Claude binary as the Nova-Core watcher for fast
conversational responses. Uses asyncio subprocess for non-blocking I/O.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

try:
    from utils.langfuse_tracing import trace_llm_call
    from utils.structured_log import slog
    from utils.trace_context import TraceContext
except ImportError:
    slog = None  # type: ignore[assignment]
    TraceContext = None  # type: ignore[assignment,misc]
    trace_llm_call = None  # type: ignore[assignment]

try:
    from utils.max_plan_guard import record_invocation as _mpg_record
except ImportError:
    _mpg_record = None  # type: ignore[assignment]

_log = logging.getLogger("telegram_bot.llm")

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "/home/nova/.local/bin/claude")
# Conversational replies must stay fast. Heavy research/coding is delegated to the
# watcher task queue (Bash/Write/Edit are disallowed on this path), so a chat reply
# only ever does a few MCP read calls. The old 4h ceiling meant a wedged `claude -p`
# subprocess would hang a Telegram reply for hours — cap it tight instead.
CONVERSATION_TIMEOUT = int(os.environ.get("TELEGRAM_LLM_TIMEOUT", "120"))  # seconds
# Bound a wedged subprocess by turns as well as wall-clock: a tool-call loop then
# terminates deterministically instead of spinning until the wall-clock timeout.
CONVERSATION_MAX_TURNS = int(os.environ.get("TELEGRAM_LLM_MAX_TURNS", "12"))
MODEL = "claude-opus-4-8"

# Telegram-specific MCP config: only servers the bot needs
# (nova-memory, nova-vault, brave-search, tavily, fetch)
_TELEGRAM_MCP_CONFIG = str(Path(__file__).resolve().parent / ".mcp.json")

# Load MCP API keys from .mcp.env (gitignored) into process environment
_MCP_ENV_FILE = Path(__file__).resolve().parent.parent / ".mcp.env"
if _MCP_ENV_FILE.is_file():
    for _line in _MCP_ENV_FILE.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _key, _, _val = _line.partition("=")
            os.environ.setdefault(_key.strip(), _val.strip())


async def generate_response(
    prompt: str,
    system_prompt: str = "",
    conversation_context: str = "",
) -> str:
    """Generate a conversational response via Claude CLI.

    Args:
        prompt: The user's message.
        system_prompt: CEO Nova persona prompt.
        conversation_context: Formatted recent conversation history.

    Returns:
        Claude's response text, or an error fallback string.
    """
    # Phase 1.1: Import delimiter defense
    try:
        from telegram.input_security import INSTRUCTION_HIERARCHY, wrap_user_input

        wrapped_prompt = wrap_user_input(prompt)
    except ImportError:
        wrapped_prompt = prompt
        INSTRUCTION_HIERARCHY = ""

    # Build the full prompt with context + instruction hierarchy
    parts: list[str] = []
    if system_prompt:
        parts.append(system_prompt)
    if INSTRUCTION_HIERARCHY:
        parts.append(INSTRUCTION_HIERARCHY)
    if conversation_context:
        parts.append(f"RECENT CONVERSATION:\n{conversation_context}")
    parts.append(f"USER MESSAGE:\n{wrapped_prompt}")
    full_prompt = "\n\n".join(parts)

    cmd = [
        CLAUDE_BIN,
        "-p",
        "--model",
        MODEL,
        "--max-turns",
        str(CONVERSATION_MAX_TURNS),
        "--dangerously-skip-permissions",
        "--mcp-config",
        _TELEGRAM_MCP_CONFIG,
        "--strict-mcp-config",
        # CEO Nova handles conversation only — execution is delegated to the task queue.
        # Without this, the LLM will sometimes ignore the persona prompt's "do not use
        # Bash/Write/Edit" rule and run things like the full pytest suite, blocking the
        # bot for hours. Enforce it at the CLI layer so the rule is unbypassable.
        "--disallowed-tools",
        "Bash,Write,Edit,NotebookEdit",
    ]
    # Append system prompt via flag if available
    if system_prompt:
        cmd += ["--append-system-prompt", system_prompt]
        # When using --append-system-prompt, the positional prompt is just
        # the user message + conversation context
        positional_parts: list[str] = []
        if conversation_context:
            positional_parts.append(f"RECENT CONVERSATION:\n{conversation_context}")
        positional_parts.append(f"USER MESSAGE:\n{prompt}")
        # "--" ends option parsing so --disallowed-tools (variadic) doesn't
        # consume the positional prompt as a tool name.
        cmd.append("--")
        cmd.append("\n\n".join(positional_parts))
    else:
        cmd.append("--")
        cmd.append(full_prompt)

    # Strip CLAUDECODE env var so child doesn't refuse to start
    child_env = os.environ.copy()
    child_env.pop("CLAUDECODE", None)

    # Phase 2.2: Budget pre-flight check
    try:
        from agents.budget_enforcer import budget

        can_go, budget_msg = budget.can_proceed(estimated_tokens=len(prompt) // 4)
        if not can_go:
            _log.warning("BUDGET_EXCEEDED: %s", budget_msg)
            return f"Budget limit reached: {budget_msg}. Will resume next period."
    except ImportError:
        pass

    _log.info("LLM call: prompt_len=%d timeout=%ds", len(prompt), CONVERSATION_TIMEOUT)

    llm_ctx = TraceContext.new("telegram.llm") if TraceContext is not None else None
    if slog and llm_ctx:
        slog.event("telegram.llm_call_start", llm_ctx, prompt_len=len(prompt), timeout=CONVERSATION_TIMEOUT)

    # Max-plan guard: record invocation (operator-initiated via Telegram)
    import time as _time

    _tg_t0 = _time.monotonic()

    MAX_RETRIES = 2  # total attempts (1 original + 1 retry)

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd="/home/nova/nova-core",
                env=child_env,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=CONVERSATION_TIMEOUT)
            response = stdout.decode("utf-8", errors="replace").strip()
            if proc.returncode != 0:
                err_stderr = stderr.decode("utf-8", errors="replace").strip()
                # Claude CLI sometimes puts error details in stdout
                err_stdout = response if response and len(response) < 500 else ""
                err_combined = f"{err_stderr}\n{err_stdout}".strip()
                _log.error(
                    "Claude CLI error (rc=%d, attempt %d/%d) stderr=%s stdout_hint=%s",
                    proc.returncode,
                    attempt,
                    MAX_RETRIES,
                    err_stderr[:500],
                    err_stdout[:200],
                )
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(3)  # brief pause before retry
                    continue
                lowered = err_combined.lower()
                if "401 invalid authentication credentials" in lowered or "failed to authenticate" in lowered:
                    return (
                        "Claude auth failed for the Telegram bot. Re-authenticate Claude Code on the VPS, then retry."
                    )
                if "usage limit" in lowered or "rate_limit" in lowered:
                    return (
                        "Claude usage limit reached for the Telegram bot. "
                        "I queued what I could; retry after the limit resets."
                    )
                return "Sorry, I hit a snag processing that. Want to try again?"
            if not response:
                _log.warning("Claude CLI returned empty response (attempt %d/%d)", attempt, MAX_RETRIES)
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(2)
                    continue
                return "Hmm, I didn't get a response back. Could you try rephrasing?"

            # === Success path ===

            # Phase 2.2: Record token usage (estimate from char counts)
            try:
                from agents.budget_enforcer import budget

                budget.record_usage(
                    input_tokens=len(prompt) // 4,
                    output_tokens=len(response) // 4,
                    model=MODEL,
                )
            except ImportError:
                pass

            # Max-plan guard: record completion
            if _mpg_record is not None:
                _mpg_record(
                    caller="telegram",
                    component="telegram.llm.generate_response",
                    model=MODEL,
                    success=True,
                    duration_secs=_time.monotonic() - _tg_t0,
                    automated=False,
                )

            # Phase 2.4: Redact secrets from LLM response before sending
            try:
                from utils.secrets import redact_text

                response = redact_text(response)
            except ImportError:
                pass

            # Phase 3.2: DLP gate — scan outbound response
            try:
                from utils.dlp_gate import dlp

                response = dlp.scan_and_redact(response, context="telegram_llm")
            except ImportError:
                pass

            # Phase 3.3: LLM Guard output scan
            try:
                from telegram.llm_guard_middleware import scan_output

                out_result = scan_output(response)
                if out_result.action == "block":
                    _log.warning("LLM_GUARD_OUTPUT_BLOCKED: findings=%s", out_result.findings)
                    return (
                        "I generated a response but it was flagged by security scanning. Let me try again differently."
                    )
            except ImportError:
                pass

            _log.info("LLM response: len=%d", len(response))

            # Langfuse LLM call tracing for cost tracking
            if trace_llm_call is not None and llm_ctx:
                trace_llm_call(
                    name="telegram:conversation",
                    input_text=prompt[:4000],
                    output_text=response[:4000],
                    model=MODEL,
                    metadata={"trace_id": llm_ctx.trace_id},
                )
            if slog and llm_ctx:
                slog.event(
                    "telegram.llm_call_complete",
                    llm_ctx,
                    response_len=len(response),
                    duration_ms=llm_ctx.elapsed_ms(),
                )

            return response

        except asyncio.TimeoutError:
            _log.error("Claude CLI timed out after %ds (attempt %d/%d)", CONVERSATION_TIMEOUT, attempt, MAX_RETRIES)
            if slog and llm_ctx:
                slog.event("telegram.llm_call_timeout", llm_ctx, level="error", timeout=CONVERSATION_TIMEOUT)
            try:
                proc.kill()  # type: ignore[possibly-undefined]
            except Exception:  # noqa: S110
                pass
            if attempt < MAX_RETRIES:
                continue
            return "That took too long — let me try a simpler approach. What did you need?"

        except Exception as exc:
            _log.error("Claude CLI exception (attempt %d/%d): %s", attempt, MAX_RETRIES, exc, exc_info=True)
            if slog and llm_ctx:
                slog.event("telegram.llm_call_error", llm_ctx, level="error", error=str(exc))
            if attempt < MAX_RETRIES:
                await asyncio.sleep(3)
                continue
            return "Something went wrong on my end. Give me a moment and try again."

    # Should never reach here, but safety fallback
    return "Sorry, I hit a snag processing that. Want to try again?"


def format_history_for_prompt(history: list[dict]) -> str:
    """Format conversation history list into a string for the prompt."""
    if not history:
        return ""
    lines: list[str] = []
    for msg in history:
        role = "You" if msg["role"] == "assistant" else "Human"
        lines.append(f"{role}: {msg['content']}")
    return "\n".join(lines)
