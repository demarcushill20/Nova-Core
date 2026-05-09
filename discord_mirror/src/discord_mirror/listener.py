from __future__ import annotations

import logging
from datetime import timezone
from typing import Protocol

import discord

from .models import ParsedSignal
from .parser import SignalParser
from .storage import Storage

log = logging.getLogger(__name__)


class _Executor(Protocol):
    async def execute(self, signal_id: int, signal: ParsedSignal) -> int: ...


class SignalListener(discord.Client):
    def __init__(
        self,
        *,
        channel_id: int,
        storage: Storage,
        parser: SignalParser | None = None,
        executor: _Executor | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.channel_id = channel_id
        self.storage = storage
        self.parser = parser
        self.executor = executor

    async def on_ready(self):
        log.info("Listener ready as %s — channel %s", self.user, self.channel_id)

    async def on_message(self, message: discord.Message):
        if message.channel.id != self.channel_id:
            return
        ts = message.created_at
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        try:
            raw_id = await self.storage.log_raw_message(
                discord_message_id=str(message.id),
                channel_id=str(message.channel.id),
                author=str(message.author),
                content=message.content,
                ts=ts,
            )
        except Exception:
            log.exception("Failed to log raw message %s", message.id)
            return
        log.info("Logged raw message %s from %s", message.id, message.author)
        if not self.parser:
            return
        try:
            result = await self.parser.parse(message.content)
        except Exception:
            log.exception("Parser failed on message %s", message.id)
            return
        if result.signal is not None:
            sid = await self.storage.log_parsed_signal(raw_id, result.signal.model_dump())
            log.info(
                "Signal id=%s %s %s SL=%s TPs=%s",
                sid,
                result.signal.direction,
                result.signal.symbol,
                result.signal.sl,
                result.signal.tps,
            )
            if self.executor and result.signal.action.value == "OPEN":
                try:
                    await self.executor.execute(sid, result.signal)
                except Exception:
                    log.exception("Executor failed on signal %s", sid)
        if result.status is not None:
            log.info("Status: %s tp_index=%s", result.status.kind, result.status.tp_index)
