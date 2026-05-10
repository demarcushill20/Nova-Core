"""Backfill historical Discord messages into SQLite and parse them.

Default: pulls newest-first (so --limit gives you the most recent N).
With no limit, pulls full channel history.

Pipeline:
  1. (Unless --reprocess) Fetch from Discord, write to raw_messages.
  2. Coalesce same-author messages around BUY/SELL triggers — signals span
     multiple messages: "BUY GOLD" / "SL 4700" / "TP1 4715, ...".
  3. Concurrently parse coalesced groups via the LLM backend, then write
     signals + status_events to SQLite. Each coalesced group's raw_ids map
     back to the first raw_message_id in the group.

Idempotent: existing discord_message_id rows are skipped on Discord fetch.
--reprocess wipes signals + status_events first, then re-parses everything
in raw_messages.

Usage:
    python scripts/backfill_history.py                       # fetch + parse all
    python scripts/backfill_history.py --limit 500           # newest 500
    python scripts/backfill_history.py --concurrency 8       # 8 parallel parses
    python scripts/backfill_history.py --skip-parse          # raw log only
    python scripts/backfill_history.py --reprocess           # skip Discord;
                                                              # rebuild signals+
                                                              # status_events
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord_mirror.coalescer import CoalescedMessage, RawMsg, coalesce
from discord_mirror.config import load_config
from discord_mirror.parser import SignalParser
from discord_mirror.storage import Storage
from dotenv import load_dotenv

log = logging.getLogger("backfill")


async def _ingest_raw(client: discord.Client, channel_id: int, storage: Storage, limit: int | None) -> int:
    """Pull raw messages from Discord; idempotent. Returns count of new rows."""
    channel = client.get_channel(channel_id) or await client.fetch_channel(channel_id)
    log.info("ingesting channel %s (%s)", channel_id, getattr(channel, "name", "?"))
    n_total = n_new = 0
    async for message in channel.history(limit=limit):  # newest-first
        n_total += 1
        if n_total % 100 == 0:
            log.info("  ingest progress: %d processed, %d new", n_total, n_new)
        if await storage.raw_message_exists(str(message.id)):
            continue
        ts = message.created_at
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        await storage.log_raw_message(
            discord_message_id=str(message.id),
            channel_id=str(message.channel.id),
            author=str(message.author),
            content=message.content or "",
            ts=ts,
        )
        n_new += 1
    log.info("ingest done: %d total, %d new", n_total, n_new)
    return n_new


async def _load_raw_chronological(storage: Storage) -> list[RawMsg]:
    cur = await storage._conn.execute("SELECT id, author, content, ts FROM raw_messages ORDER BY ts ASC")
    rows = await cur.fetchall()
    return [
        RawMsg(
            raw_id=row[0],
            author=row[1],
            content=row[2] or "",
            ts=datetime.fromisoformat(row[3]),
        )
        for row in rows
    ]


async def _wipe_parsed(storage: Storage) -> None:
    await storage._conn.execute("DELETE FROM status_events")
    await storage._conn.execute("DELETE FROM signals")
    await storage._conn.commit()


async def _parse_groups(
    parser: SignalParser,
    storage: Storage,
    groups: list[CoalescedMessage],
    concurrency: int,
) -> tuple[int, int, int]:
    sem = asyncio.Semaphore(concurrency)
    lock = asyncio.Lock()
    counters = {"signals": 0, "status": 0, "skipped": 0, "done": 0}

    async def _one(group: CoalescedMessage) -> None:
        async with sem:
            try:
                result = await parser.parse(group.combined_content)
            except Exception:
                log.exception("parser raised on raw_ids %s", group.raw_ids)
                async with lock:
                    counters["done"] += 1
                return
        if result.signal is None and result.status is None:
            async with lock:
                counters["skipped"] += 1
                counters["done"] += 1
                if counters["done"] % 25 == 0:
                    log.info("  parse progress: %d/%d", counters["done"], len(groups))
            return
        async with lock:
            anchor_raw_id = group.raw_ids[0]
            if result.signal is not None:
                await storage.log_parsed_signal(anchor_raw_id, result.signal.model_dump())
                counters["signals"] += 1
            if result.status is not None:
                await storage.log_status_event(
                    raw_message_id=anchor_raw_id,
                    kind=result.status.kind,
                    tp_index=result.status.tp_index,
                    ts=group.earliest_ts,
                )
                counters["status"] += 1
            counters["done"] += 1
            if counters["done"] % 25 == 0:
                log.info("  parse progress: %d/%d", counters["done"], len(groups))

    await asyncio.gather(*[_one(g) for g in groups])
    return counters["signals"], counters["status"], counters["skipped"]


async def _coalesce_and_parse(storage: Storage, parser: SignalParser | None, concurrency: int) -> None:
    raws = await _load_raw_chronological(storage)
    log.info("loaded %d raw_messages from DB", len(raws))
    groups = list(coalesce(raws))
    log.info(
        "coalesced into %d groups (%d are multi-message signal groups)",
        len(groups),
        sum(1 for g in groups if g.is_group),
    )

    # Pre-filter: skip groups that obviously have nothing for the parser.
    # A group is parseable if it's multi-message (signal candidate) OR contains
    # numeric content typical of a status update.
    import re

    _STATUS_HINT = re.compile(r"SMACK|SMASH|STOPPED|SL\s*HIT|TP\d|HIT", re.IGNORECASE)
    candidates = [g for g in groups if g.is_group or _STATUS_HINT.search(g.combined_content)]
    log.info("parser candidates: %d (skipped %d non-candidates)", len(candidates), len(groups) - len(candidates))

    if parser is None or not candidates:
        return
    log.info("parsing %d candidates with concurrency=%d", len(candidates), concurrency)
    sig, stat, skipped = await _parse_groups(parser, storage, candidates, concurrency)
    log.info("parse done: signals=%d  status_events=%d  null/skipped=%d", sig, stat, skipped)


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
            await _ingest_raw(self, self.channel_id, self.storage, self.limit)
            await _coalesce_and_parse(self.storage, self.parser, self.concurrency)
        finally:
            await self.close()


async def amain(limit: int | None, skip_parse: bool, concurrency: int, reprocess: bool) -> None:
    load_dotenv()
    cfg = load_config(Path("config.yaml"))
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s — %(message)s")
    storage = Storage(cfg.storage.db_path)
    await storage.init()
    parser = None if skip_parse else SignalParser()

    if reprocess:
        log.info("--reprocess: wiping signals + status_events, re-parsing raw_messages")
        await _wipe_parsed(storage)
        try:
            await _coalesce_and_parse(storage, parser, concurrency)
        finally:
            await storage.close()
        return

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
    p.add_argument(
        "--reprocess",
        action="store_true",
        help="skip Discord fetch; wipe signals + status_events and re-parse raw_messages",
    )
    args = p.parse_args()
    asyncio.run(amain(args.limit, args.skip_parse, args.concurrency, args.reprocess))


if __name__ == "__main__":
    main()
