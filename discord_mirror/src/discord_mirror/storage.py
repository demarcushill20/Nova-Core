from __future__ import annotations

import json
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

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_message_id INTEGER NOT NULL REFERENCES raw_messages(id),
    action TEXT NOT NULL,
    direction TEXT,
    symbol TEXT,
    entry REAL,
    sl REAL,
    tps_json TEXT NOT NULL,
    confidence REAL,
    state TEXT NOT NULL DEFAULT 'OPEN',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_signals_state ON signals(state);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER NOT NULL REFERENCES signals(id),
    mode TEXT NOT NULL,
    broker_order_id TEXT,
    direction TEXT NOT NULL,
    symbol TEXT NOT NULL,
    entry REAL NOT NULL,
    sl REAL NOT NULL,
    tp REAL NOT NULL,
    lot REAL NOT NULL,
    state TEXT NOT NULL DEFAULT 'OPEN',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    closed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_signal ON trades(signal_id);
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

    async def log_parsed_signal(self, raw_message_id: int, signal: dict) -> int:
        cur = await self._conn.execute(
            "INSERT INTO signals (raw_message_id, action, direction, symbol, entry, sl, tps_json, confidence) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                raw_message_id,
                signal["action"],
                signal.get("direction"),
                signal.get("symbol"),
                signal.get("entry"),
                signal.get("sl"),
                json.dumps(signal.get("tps", [])),
                signal.get("confidence", 0.5),
            ),
        )
        await self._conn.commit()
        if cur.lastrowid is None:
            raise RuntimeError("INSERT did not produce a lastrowid")
        return cur.lastrowid

    async def list_open_signals(self) -> list[dict]:
        cur = await self._conn.execute("SELECT * FROM signals WHERE state = 'OPEN' ORDER BY id DESC")
        return [dict(r) for r in await cur.fetchall()]

    async def update_signal_state(self, signal_id: int, state: str) -> None:
        await self._conn.execute("UPDATE signals SET state = ? WHERE id = ?", (state, signal_id))
        await self._conn.commit()

    async def log_paper_fill(
        self,
        *,
        signal_id: int,
        direction: str,
        symbol: str,
        entry: float,
        sl: float,
        tp: float,
        lot: float,
    ) -> int:
        cur = await self._conn.execute(
            "INSERT INTO trades (signal_id, mode, direction, symbol, entry, sl, tp, lot) "
            "VALUES (?, 'paper', ?, ?, ?, ?, ?, ?)",
            (signal_id, direction, symbol, entry, sl, tp, lot),
        )
        await self._conn.commit()
        if cur.lastrowid is None:
            raise RuntimeError("INSERT did not produce a lastrowid")
        return cur.lastrowid
