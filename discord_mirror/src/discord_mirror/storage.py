from __future__ import annotations

from datetime import datetime
from pathlib import Path

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_message_id TEXT NOT NULL UNIQUE,
    channel_id TEXT NOT NULL,
    author TEXT NOT NULL,
    content TEXT NOT NULL,
    ts TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_raw_messages_ts ON raw_messages(ts);
"""


class Storage:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._db: aiosqlite.Connection | None = None

    async def init(self) -> None:
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    @property
    def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Storage.init() must be called first")
        return self._db

    async def log_raw_message(
        self,
        *,
        discord_message_id: str,
        channel_id: str,
        author: str,
        content: str,
        ts: datetime,
    ) -> int:
        cur = await self._conn.execute(
            "INSERT INTO raw_messages (discord_message_id, channel_id, author, content, ts) VALUES (?, ?, ?, ?, ?)",
            (discord_message_id, channel_id, author, content, ts.isoformat()),
        )
        await self._conn.commit()
        if cur.lastrowid is None:
            raise RuntimeError("INSERT did not produce a lastrowid")
        return cur.lastrowid

    async def list_recent_raw_messages(self, limit: int = 50) -> list[dict]:
        cur = await self._conn.execute("SELECT * FROM raw_messages ORDER BY ts DESC LIMIT ?", (limit,))
        rows = await cur.fetchall()
        return [dict(r) for r in rows]
