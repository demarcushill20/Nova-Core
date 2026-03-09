"""Claude CLI conversation wrapper for CEO Nova.

Spawns the same Claude binary as the Nova-Core watcher for fast
conversational responses. Uses asyncio subprocess for non-blocking I/O.
"""
from __future__ import annotations

import asyncio
import logging
import os

_log = logging.getLogger("telegram_bot.llm")

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "/home/nova/.local/bin/claude")
CONVERSATION_TIMEOUT = 60  # seconds — generous for Opus
MODEL = "claude-opus-4-6"


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
    # Build the full prompt with context
    parts: list[str] = []
    if system_prompt:
        parts.append(system_prompt)
    if conversation_context:
        parts.append(f"RECENT CONVERSATION:\n{conversation_context}")
    parts.append(f"USER MESSAGE:\n{prompt}")
    full_prompt = "\n\n".join(parts)

    cmd = [CLAUDE_BIN, "-p", "--model", MODEL, "--dangerously-skip-permissions"]
    # Append system prompt via flag if available
    if system_prompt:
        cmd += ["--append-system-prompt", system_prompt]
        # When using --append-system-prompt, the positional prompt is just
        # the user message + conversation context
        positional_parts: list[str] = []
        if conversation_context:
            positional_parts.append(
                f"RECENT CONVERSATION:\n{conversation_context}"
            )
        positional_parts.append(f"USER MESSAGE:\n{prompt}")
        cmd.append("\n\n".join(positional_parts))
    else:
        cmd.append(full_prompt)

    # Strip CLAUDECODE env var so child doesn't refuse to start
    child_env = os.environ.copy()
    child_env.pop("CLAUDECODE", None)

    _log.info("LLM call: prompt_len=%d timeout=%ds", len(prompt), CONVERSATION_TIMEOUT)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd="/home/nova/nova-core",
            env=child_env,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=CONVERSATION_TIMEOUT
        )
        response = stdout.decode("utf-8", errors="replace").strip()
        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="replace").strip()
            _log.error("Claude CLI error (rc=%d): %s", proc.returncode, err[:500])
            return "Sorry, I hit a snag processing that. Want to try again?"
        if not response:
            _log.warning("Claude CLI returned empty response")
            return "Hmm, I didn't get a response back. Could you try rephrasing?"
        _log.info("LLM response: len=%d", len(response))
        return response

    except asyncio.TimeoutError:
        _log.error("Claude CLI timed out after %ds", CONVERSATION_TIMEOUT)
        try:
            proc.kill()  # type: ignore[possibly-undefined]
        except Exception:
            pass
        return "That took too long — let me try a simpler approach. What did you need?"

    except Exception as exc:
        _log.error("Claude CLI exception: %s", exc, exc_info=True)
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
