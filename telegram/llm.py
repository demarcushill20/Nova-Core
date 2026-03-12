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

_log = logging.getLogger("telegram_bot.llm")

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "/home/nova/.local/bin/claude")
CONVERSATION_TIMEOUT = 600  # seconds — Opus 4.6 + MCP tools need time for complex queries
MODEL = "claude-opus-4-6"

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

    cmd = [CLAUDE_BIN, "-p", "--model", MODEL, "--dangerously-skip-permissions"]
    # Append system prompt via flag if available
    if system_prompt:
        cmd += ["--append-system-prompt", system_prompt]
        # When using --append-system-prompt, the positional prompt is just
        # the user message + conversation context
        positional_parts: list[str] = []
        if conversation_context:
            positional_parts.append(f"RECENT CONVERSATION:\n{conversation_context}")
        positional_parts.append(f"USER MESSAGE:\n{prompt}")
        cmd.append("\n\n".join(positional_parts))
    else:
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
            err = stderr.decode("utf-8", errors="replace").strip()
            _log.error("Claude CLI error (rc=%d): %s", proc.returncode, err[:500])
            return "Sorry, I hit a snag processing that. Want to try again?"
        if not response:
            _log.warning("Claude CLI returned empty response")
            return "Hmm, I didn't get a response back. Could you try rephrasing?"

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
                return "I generated a response but it was flagged by security scanning. Let me try again differently."
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
        _log.error("Claude CLI timed out after %ds", CONVERSATION_TIMEOUT)
        if slog and llm_ctx:
            slog.event("telegram.llm_call_timeout", llm_ctx, level="error", timeout=CONVERSATION_TIMEOUT)
        try:
            proc.kill()  # type: ignore[possibly-undefined]
        except Exception:
            pass
        return "That took too long — let me try a simpler approach. What did you need?"

    except Exception as exc:
        _log.error("Claude CLI exception: %s", exc, exc_info=True)
        if slog and llm_ctx:
            slog.event("telegram.llm_call_error", llm_ctx, level="error", error=str(exc))
        return "Something went wrong on my end. Give me a moment and try again."


def format_history_for_prompt(history: list[dict]) -> str:
    """Format conversation history list into a string for the prompt."""
    if not history:
        return ""
    lines: list[str] = []
    for msg in history:
        role = "You" if msg["role"] == "assistant" else "Human"
        lines.append(f"{role}: {msg['content']}")
    return "\n".join(lines)
