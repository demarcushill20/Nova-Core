from __future__ import annotations

import json
from dataclasses import dataclass

from anthropic import AsyncAnthropic

from .models import ParsedSignal, StatusUpdate

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


class SignalParser:
    def __init__(self, *, api_key: str, model: str = "claude-sonnet-4-6"):
        self.client = AsyncAnthropic(api_key=api_key)
        self.model = model

    async def parse(self, content: str) -> ParseResult:
        resp = await self.client.messages.create(
            model=self.model,
            max_tokens=512,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                },
            ],
            messages=[{"role": "user", "content": content}],
        )
        text = resp.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("```", 2)[1].lstrip("json").strip()
        data = json.loads(text)
        sig_data = data.get("signal")
        signal = ParsedSignal(**sig_data) if sig_data and sig_data.get("action") != "NONE" else None
        st_data = data.get("status")
        status = StatusUpdate(**st_data) if st_data and st_data.get("kind") != "NONE" else None
        return ParseResult(signal=signal, status=status)
