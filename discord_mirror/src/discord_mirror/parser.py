from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Protocol

from pydantic import ValidationError

from .models import ParsedSignal, StatusUpdate

log = logging.getLogger(__name__)

_TP_INDEX = re.compile(r"TP\s*(\d+)", re.IGNORECASE)

SYSTEM_PROMPT = """You parse forex/commodity trade signal messages from a Discord channel.

Output STRICT JSON with this schema:
{
  "signal": {
    "action": "OPEN" | "NONE",
    "direction": "BUY" | "SELL" | null,
    "symbol": string | null,
    "entry": number | null,
    "sl": number | null,
    "tps": [number, ...],
    "confidence": number,
    "notes": string
  } | null,
  "status": {
    "kind": "TP_HIT" | "ALL_TPS_HIT" | "SL_HIT" | "CLOSED" | "NONE",
    "tp_index": number | null,
    "raw_text": string
  } | null
}

Rules:
1. If the message opens a new trade (BUY/SELL X with SL and TPs),
   set "signal" with action=OPEN.
2. If the message reports a TP/SL outcome ("TP1 SMACKED", "ALL TPs SMASHED",
   "stopped out", "closed"), set "status" with the matching kind.
3. If both apply, set both. If neither, set "signal": null and "status": null.
4. TPs may be labelled non-contiguously (TP1..TP7, TP9 with TP8 missing) — list
   them in the order they appear, all of them.
5. Symbol normalization: keep what was written ("GOLD" stays "GOLD", "XAUUSD"
   stays "XAUUSD"). The executor maps to broker symbols.
6. Numbers: parse as floats; strip commas.
7. NEVER invent values not in the message. If SL is missing on an OPEN, set
   action=NONE and explain in notes.
8. Output only the JSON object — no prose, no markdown fences."""


@dataclass
class ParseResult:
    signal: ParsedSignal | None
    status: StatusUpdate | None


class _Backend(Protocol):
    async def complete(self, system: str, user: str) -> str: ...


class AnthropicSDKBackend:
    """Calls Anthropic's API directly. Requires ANTHROPIC_API_KEY."""

    def __init__(self, *, api_key: str, model: str = "claude-sonnet-4-6"):
        from anthropic import AsyncAnthropic

        self.client = AsyncAnthropic(api_key=api_key)
        self.model = model

    async def complete(self, system: str, user: str) -> str:
        resp = await self.client.messages.create(
            model=self.model,
            max_tokens=512,
            system=[
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                },
            ],
            messages=[{"role": "user", "content": user}],
        )
        return resp.content[0].text


class ClaudeCLIBackend:
    """Subprocess-invokes the `claude` CLI — uses the CC subscription's auth.

    No ANTHROPIC_API_KEY required. Spawns one subprocess per parse.

    Retries on transient failures (CLI exit != 0). Claude Code rate-limit
    windows can produce sporadic exit-1 with empty stderr for a few minutes.
    """

    def __init__(
        self,
        *,
        model: str = "sonnet",
        binary: str = "claude",
        max_retries: int = 4,
        retry_base_seconds: float = 5.0,
    ):
        self.model = model
        self.binary = binary
        self.max_retries = max_retries
        self.retry_base_seconds = retry_base_seconds

    async def complete(self, system: str, user: str) -> str:
        last_err = ""
        for attempt in range(self.max_retries + 1):
            proc = await asyncio.create_subprocess_exec(
                self.binary,
                "--print",
                "--model",
                self.model,
                "--append-system-prompt",
                system,
                user,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode == 0:
                return stdout.decode()
            # Capture both streams; CLI sometimes prints errors to stdout
            err_msg = stderr.decode().strip() or stdout.decode().strip() or "<empty>"
            last_err = f"exit {proc.returncode}: {err_msg[:200]}"
            if attempt < self.max_retries:
                delay = self.retry_base_seconds * (2**attempt)
                log.warning(
                    "claude CLI attempt %d/%d failed (%s); retrying in %.0fs",
                    attempt + 1,
                    self.max_retries + 1,
                    last_err,
                    delay,
                )
                await asyncio.sleep(delay)
        raise RuntimeError(f"claude CLI failed after {self.max_retries + 1} attempts: {last_err}")


def _make_default_backend(model: str | None) -> _Backend:
    """Pick a backend based on environment.

    - LLM_BACKEND=cli → ClaudeCLIBackend
    - LLM_BACKEND=sdk → AnthropicSDKBackend (requires ANTHROPIC_API_KEY)
    - unset → CLI if `claude` is on PATH and no API key set, else SDK
    """
    explicit = os.environ.get("LLM_BACKEND", "").lower()
    api_key = os.environ.get("ANTHROPIC_API_KEY")

    if explicit == "sdk":
        if not api_key:
            raise RuntimeError("LLM_BACKEND=sdk requires ANTHROPIC_API_KEY")
        return AnthropicSDKBackend(api_key=api_key, model=model or "claude-sonnet-4-6")
    if explicit == "cli":
        return ClaudeCLIBackend(model=model or "sonnet")

    if api_key:
        return AnthropicSDKBackend(api_key=api_key, model=model or "claude-sonnet-4-6")
    return ClaudeCLIBackend(model=model or "sonnet")


class SignalParser:
    def __init__(
        self,
        *,
        backend: _Backend | None = None,
        api_key: str | None = None,
        model: str | None = None,
    ):
        if backend is not None:
            self.backend = backend
        elif api_key is not None:
            self.backend = AnthropicSDKBackend(api_key=api_key, model=model or "claude-sonnet-4-6")
        else:
            self.backend = _make_default_backend(model)

    async def parse(self, content: str) -> ParseResult:
        text = (await self.backend.complete(SYSTEM_PROMPT, content)).strip()
        if text.startswith("```"):
            text = text.split("```", 2)[1].lstrip("json").strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            log.warning("parser got non-JSON output (first 80 chars): %r", text[:80])
            return ParseResult(signal=None, status=None)
        return ParseResult(
            signal=_build_signal(data.get("signal")),
            status=_build_status(data.get("status"), content),
        )


def _build_signal(sig_data: dict | None) -> ParsedSignal | None:
    if not sig_data or sig_data.get("action") == "NONE":
        return None
    try:
        return ParsedSignal(**sig_data)
    except ValidationError as e:
        log.warning("invalid signal payload: %s", e.errors()[0]["msg"])
        return None


def _build_status(st_data: dict | None, raw_content: str) -> StatusUpdate | None:
    if not st_data or st_data.get("kind") == "NONE":
        return None
    # Regex-recover tp_index if the model returned null for a TP_HIT
    if st_data.get("kind") == "TP_HIT" and st_data.get("tp_index") is None:
        m = _TP_INDEX.search(st_data.get("raw_text") or raw_content)
        if m:
            st_data["tp_index"] = int(m.group(1))
    try:
        return StatusUpdate(**st_data)
    except ValidationError as e:
        log.warning("invalid status payload: %s", e.errors()[0]["msg"])
        return None
