from __future__ import annotations

import logging
from datetime import timezone
from typing import Protocol

import discord

from .coalescer import CoalescedMessage, LiveCoalescer, RawMsg
from .models import ParsedSignal
from .parser import SignalParser
from .state import SignalStateMachine
from .status_handler import StatusHandler
from .storage import Storage

log = logging.getLogger(__name__)


class Executor(Protocol):
    async def execute(self, signal_id: int, signal: ParsedSignal) -> int: ...


class SignalListener(discord.Client):
    def __init__(
        self,
        *,
        channel_id: int,
        storage: Storage,
        parser: SignalParser | None = None,
        executor: Executor | None = None,
        status_handler: StatusHandler | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.channel_id = channel_id
        self.storage = storage
        self.parser = parser
        self.executor = executor
        self.status_handler = status_handler
        self._state_machines: dict[int, SignalStateMachine] = {}
        self._latest_signal_id: int | None = None
        self._coalescer = LiveCoalescer(on_emit=self._on_coalesced_group)

    async def on_ready(self):
        log.info("Listener ready as %s — channel %s", self.user, self.channel_id)
        await self._coalescer.start()

    async def close(self):
        try:
            await self._coalescer.stop()
        finally:
            await super().close()

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
        log.info("Logged raw message %s from %s: %s", message.id, message.author, message.content[:80])
        if not self.parser:
            return
        await self._coalescer.add(
            RawMsg(
                raw_id=raw_id,
                author=str(message.author),
                content=message.content or "",
                ts=ts,
            )
        )

    async def _on_coalesced_group(self, group: CoalescedMessage) -> None:
        """Called by the coalescer when a group is ready to parse."""
        if self.parser is None:
            return
        try:
            result = await self.parser.parse(group.combined_content)
        except Exception:
            log.exception("Parser failed on group %s", group.raw_ids)
            return
        if result.signal is None and result.status is None:
            return

        anchor_raw_id = group.raw_ids[0]
        if result.signal is not None:
            sid = await self.storage.log_parsed_signal(anchor_raw_id, result.signal.model_dump())
            self._state_machines[sid] = SignalStateMachine(tp_count=len(result.signal.tps))
            self._latest_signal_id = sid
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
        if result.status is not None and self.status_handler is not None and self._latest_signal_id is not None:
            sm = self._state_machines.get(self._latest_signal_id)
            if sm:
                try:
                    await self.status_handler.handle(
                        signal_id=self._latest_signal_id,
                        sm=sm,
                        update=result.status,
                    )
                except Exception:
                    log.exception("StatusHandler failed on signal %s", self._latest_signal_id)
            log.info("Status: %s tp_index=%s", result.status.kind, result.status.tp_index)
