"""Group same-author messages around a BUY/SELL trigger.

Signal providers commonly post a trade in 3 separate messages within seconds:
    @everyone BUY GOLD
    SL 4700
    TP1 4715, TP2 4720, ...

Sending each in isolation to the parser yields nothing — the parser correctly
rejects "@everyone BUY GOLD" alone as missing SL/TP. This module coalesces
followup messages from the same author within a time window so the parser sees
the full signal as one input.

Pure function. Stateless across calls — callers pass an ordered iterable of
raw messages and get an ordered iterable of CoalescedMessage back.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta

# Case-sensitive uppercase to avoid false positives like "buying", "sells out".
_TRIGGER = re.compile(r"\b(BUY|SELL)\b")
# Status updates should never be appended to a trigger buffer — they belong on their own.
_STATUS_LIKE = re.compile(r"SMACK|SMASH|STOPPED OUT|SL\s*HIT|TP\d+\s*HIT", re.IGNORECASE)


@dataclass(frozen=True)
class RawMsg:
    raw_id: int
    author: str
    content: str
    ts: datetime


@dataclass(frozen=True)
class CoalescedMessage:
    """One or more raw messages that should be parsed as a single unit."""

    raw_ids: list[int]
    author: str
    earliest_ts: datetime
    combined_content: str

    @property
    def is_group(self) -> bool:
        return len(self.raw_ids) > 1


def coalesce(
    messages: Iterable[RawMsg],
    *,
    window_seconds: int = 180,
    max_followups: int = 5,
) -> Iterator[CoalescedMessage]:
    """Yield coalesced groups in input order.

    Rules:
      - A message containing BUY or SELL (uppercase, standalone) starts a buffer.
      - Subsequent messages from the same author within window_seconds are appended.
      - A message matching status patterns (SMACKED/SMASHED/STOPPED) always flushes
        the buffer (if any) and is emitted standalone.
      - A new trigger flushes the previous buffer.
      - A different author also flushes.
      - Buffer caps at 1 + max_followups messages.
    """
    buffer: list[RawMsg] = []
    deadline: datetime | None = None

    def _build() -> CoalescedMessage:
        return CoalescedMessage(
            raw_ids=[m.raw_id for m in buffer],
            author=buffer[0].author,
            earliest_ts=buffer[0].ts,
            combined_content="\n".join(m.content for m in buffer),
        )

    for msg in messages:
        # Flush if window expired or different author
        if buffer and deadline is not None and (msg.ts > deadline or msg.author != buffer[0].author):
            yield _build()
            buffer = []
            deadline = None

        is_trigger = bool(_TRIGGER.search(msg.content))
        is_status = bool(_STATUS_LIKE.search(msg.content))

        if is_status:
            # Status updates always stand alone and flush any pending buffer
            if buffer:
                yield _build()
                buffer = []
                deadline = None
            yield CoalescedMessage(
                raw_ids=[msg.raw_id],
                author=msg.author,
                earliest_ts=msg.ts,
                combined_content=msg.content,
            )
        elif is_trigger:
            if buffer:
                yield _build()
            buffer = [msg]
            deadline = msg.ts + timedelta(seconds=window_seconds)
        elif buffer:
            # Same author, within window (we already verified above), not status/trigger
            buffer.append(msg)
            if len(buffer) >= 1 + max_followups:
                yield _build()
                buffer = []
                deadline = None
        else:
            yield CoalescedMessage(
                raw_ids=[msg.raw_id],
                author=msg.author,
                earliest_ts=msg.ts,
                combined_content=msg.content,
            )

    if buffer:
        yield _build()


class LiveCoalescer:
    """Stateful, per-author message coalescer for the live runtime.

    Adds two things on top of the batch `coalesce()` function:
      - Per-author buffering across time (multiple authors interleaved)
      - Background timer that flushes a pending buffer when no new messages
        arrive within the window (the batch version only flushes on next-msg-arrival)

    Lifecycle:
        c = LiveCoalescer(on_emit=async_callback)
        await c.start()              # starts the flush task
        await c.add(raw_msg)         # feed messages as they arrive
        await c.stop()               # cancel task, flush remaining
    """

    def __init__(
        self,
        on_emit,
        *,
        window_seconds: int = 180,
        max_followups: int = 5,
        flush_interval_seconds: int = 5,
    ):
        self.on_emit = on_emit
        self.window = timedelta(seconds=window_seconds)
        self.max_followups = max_followups
        self.flush_interval = flush_interval_seconds
        import asyncio as _asyncio

        self._pending: dict[str, dict] = {}
        self._flush_task: _asyncio.Task[None] | None = None

    async def start(self) -> None:
        import asyncio

        if self._flush_task is None:
            self._flush_task = asyncio.create_task(self._flush_loop())

    async def stop(self) -> None:
        if self._flush_task is not None:
            self._flush_task.cancel()
            self._flush_task = None
        for author in list(self._pending):
            await self._emit_author(author)

    async def add(self, msg: RawMsg) -> None:
        is_trigger = bool(_TRIGGER.search(msg.content))
        is_status = bool(_STATUS_LIKE.search(msg.content))

        if is_status:
            if msg.author in self._pending:
                await self._emit_author(msg.author)
            await self.on_emit(self._single(msg))
            return

        if is_trigger:
            if msg.author in self._pending:
                await self._emit_author(msg.author)
            self._pending[msg.author] = {"messages": [msg], "deadline": msg.ts + self.window}
            return

        if msg.author in self._pending:
            pending = self._pending[msg.author]
            if msg.ts > pending["deadline"]:
                await self._emit_author(msg.author)
                await self.on_emit(self._single(msg))
            else:
                pending["messages"].append(msg)
                if len(pending["messages"]) >= 1 + self.max_followups:
                    await self._emit_author(msg.author)
            return

        await self.on_emit(self._single(msg))

    async def _emit_author(self, author: str) -> None:
        p = self._pending.pop(author, None)
        if not p:
            return
        msgs = p["messages"]
        group = CoalescedMessage(
            raw_ids=[m.raw_id for m in msgs],
            author=msgs[0].author,
            earliest_ts=msgs[0].ts,
            combined_content="\n".join(m.content for m in msgs),
        )
        await self.on_emit(group)

    def _single(self, msg: RawMsg) -> CoalescedMessage:
        return CoalescedMessage(
            raw_ids=[msg.raw_id],
            author=msg.author,
            earliest_ts=msg.ts,
            combined_content=msg.content,
        )

    async def _flush_loop(self) -> None:
        import asyncio
        from datetime import timezone

        try:
            while True:
                await asyncio.sleep(self.flush_interval)
                now = datetime.now(timezone.utc)
                expired = [a for a, p in self._pending.items() if p["deadline"] < now]
                for author in expired:
                    await self._emit_author(author)
        except asyncio.CancelledError:
            return
