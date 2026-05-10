"""Backfill historical Discord messages into SQLite and parse them.

Default: pulls newest-first (so --limit gives you the most recent N).
With no limit, pulls full channel history.

Two-pass design:
  1. Sequentially fetch + log raw messages from Discord
  2. Concurrently parse candidate messages (regex-prefiltered) via the
     LLM backend, then write signals + status_events to SQLite

Idempotent: existing discord_message_id rows are skipped, so re-running
catches only new messages.

Usage:
    python scripts/backfill_history.py                       # all history
    python scripts/backfill_history.py --limit 500           # newest 500
    python scripts/backfill_history.py --concurrency 8       # 8 parallel parses
    python scripts/backfill_history.py --skip-parse          # raw log only
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
from datetime import timezone
from pathlib import Path

import discord
from discord_mirror.config import load_config
from discord_mirror.parser import SignalParser
from discord_mirror.storage import Storage
from dotenv import load_dotenv

log = logging.getLogger("backfill")

_LOOKS_LIKE_SIGNAL = re.compile(
    r"\b(BUY|SELL|TP\d|SL\b|SMACK|SMASH|HIT|STOP|CLOS|TARGET|ENTRY)\b",
    re.IGNORECASE,
)


async def _ingest_raw(
    client: discord.Client, channel_id: int, storage: Storage, limit: int | None
) -> list[tuple[int, str, str]]:
    """Pass 1: pull raw messages, write to raw_messages, return parse candidates."""
    channel = client.get_channel(channel_id) or await client.fetch_channel(channel_id)
    log.info("ingesting channel %s (%s)", channel_id, getattr(channel, "name", "?"))

    candidates: list[tuple[int, str, str]] = []  # (raw_id, content, ts_iso)
    n_total = n_new = 0
    async for message in channel.history(limit=limit):  # newest-first by default
        n_total += 1
        if n_total % 100 == 0:
            log.info("  ingest progress: %d processed, %d new", n_total, n_new)
        if await storage.raw_message_exists(str(message.id)):
            continue
        ts = message.created_at
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        raw_id = await storage.log_raw_message(
            discord_message_id=str(message.id),
            channel_id=str(message.channel.id),
            author=str(message.author),
            content=message.content or "",
            ts=ts,
        )
        n_new += 1
        if message.content and _LOOKS_LIKE_SIGNAL.search(message.content):
            candidates.append((raw_id, message.content, ts.isoformat()))
    log.info("ingest done: %d total, %d new, %d parse candidates", n_total, n_new, len(candidates))
    return candidates


async def _parse_concurrent(
    parser: SignalParser, storage: Storage, candidates: list[tuple[int, str, str]], concurrency: int
) -> tuple[int, int]:
    from datetime import datetime

    sem = asyncio.Semaphore(concurrency)
    lock = asyncio.Lock()
    counters = {"signals": 0, "status": 0, "done": 0}

    async def _one(raw_id: int, content: str, ts_iso: str) -> None:
        async with sem:
            try:
                result = await parser.parse(content)
            except Exception:
                log.exception("parser failed on raw_id %s", raw_id)
                async with lock:
                    counters["done"] += 1
                return
        async with lock:
            ts = datetime.fromisoformat(ts_iso)
            if result.signal is not None:
                await storage.log_parsed_signal(raw_id, result.signal.model_dump())
                counters["signals"] += 1
            if result.status is not None:
                await storage.log_status_event(
                    raw_message_id=raw_id,
                    kind=result.status.kind,
                    tp_index=result.status.tp_index,
                    ts=ts,
                )
                counters["status"] += 1
            counters["done"] += 1
            if counters["done"] % 25 == 0:
                log.info("  parse progress: %d/%d", counters["done"], len(candidates))

    await asyncio.gather(*[_one(r, c, t) for (r, c, t) in candidates])
    return counters["signals"], counters["status"]


class BackfillClient(discord.Client):
    def __init__(self, *, channel_id, storage, parser, limit, concurrency, **kwargs):
        super().__init__(**kwargs)
        self.channel_id = channel_id
        self.storage = storage
        self.parser = parser
        self.limit = limit
        self.concurrency = concurrency

    async def on_ready(self):
        log.info("logged in as %s", self.user)
        try:
            candidates = await _ingest_raw(self, self.channel_id, self.storage, self.limit)
            if self.parser and candidates:
                log.info("parsing %d candidates with concurrency=%d", len(candidates), self.concurrency)
                sig, stat = await _parse_concurrent(
                    self.parser,
                    self.storage,
                    candidates,
                    self.concurrency,
                )
                log.info("parse done: signals=%d status_events=%d", sig, stat)
        finally:
            await self.close()


async def amain(limit: int | None, skip_parse: bool, concurrency: int) -> None:
    load_dotenv()
    cfg = load_config(Path("config.yaml"))
    logging.basicConfig(
        level="INFO",
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    storage = Storage(cfg.storage.db_path)
    await storage.init()
    parser = None if skip_parse else SignalParser()
    client = BackfillClient(
        channel_id=cfg.discord.channel_id,
        storage=storage,
        parser=parser,
        limit=limit,
        concurrency=concurrency,
    )
    try:
        await client.start(os.environ["DISCORD_USER_TOKEN"])
    finally:
        await storage.close()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=None, help="max messages to pull (default: all history)")
    p.add_argument("--skip-parse", action="store_true", help="store raw messages only; don't run LLM parser")
    p.add_argument("--concurrency", type=int, default=5, help="parallel parse workers (default 5)")
    args = p.parse_args()
    asyncio.run(amain(args.limit, args.skip_parse, args.concurrency))


if __name__ == "__main__":
    main()
