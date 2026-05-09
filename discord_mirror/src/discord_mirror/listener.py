from __future__ import annotations

import logging
from datetime import timezone

import discord

from .storage import Storage

log = logging.getLogger(__name__)


class SignalListener(discord.Client):
    def __init__(self, *, channel_id: int, storage: Storage, **kwargs):
        super().__init__(**kwargs)
        self.channel_id = channel_id
        self.storage = storage

    async def on_ready(self):
        log.info("Listener ready as %s — watching channel %s", self.user, self.channel_id)

    async def on_message(self, message: discord.Message):
        if message.channel.id != self.channel_id:
            return
        ts = message.created_at
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        try:
            await self.storage.log_raw_message(
                discord_message_id=str(message.id),
                channel_id=str(message.channel.id),
                author=str(message.author),
                content=message.content,
                ts=ts,
            )
            log.info(
                "Logged message %s from %s (%d chars)",
                message.id,
                message.author,
                len(message.content),
            )
        except Exception:
            log.exception("Failed to log message %s", message.id)
