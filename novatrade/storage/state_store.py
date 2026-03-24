"""SQLite-backed state persistence for TradingAgent and PositionTracker.

Provides durable storage for:
- Agent FSM state (5-state model + pending/position metadata)
- Idempotency keys (bounded, most recent N)
- Expected positions (from PositionTracker)

Uses connection-per-call pattern for async safety — each public method opens,
executes, commits, and closes its own connection.  This avoids threading issues
when called from asyncio coroutines.

WAL mode + busy_timeout provide crash safety and concurrent-read performance.
All methods wrap operations in try/except so persistence failures never crash
the trading pipeline.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

from novatrade.models import OrderSide, Position

log = logging.getLogger("novatrade.storage.state_store")

_SCHEMA_VERSION = 1

_CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS agent_state (
    agent_id         TEXT PRIMARY KEY DEFAULT 'default',
    state            TEXT NOT NULL,
    pending_order_id TEXT,
    pending_side     TEXT,
    pending_symbol   TEXT,
    position_id      TEXT,
    position_side    TEXT,
    position_symbol  TEXT,
    position_volume  REAL NOT NULL DEFAULT 0.0,
    updated_at       REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    key        TEXT PRIMARY KEY,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS expected_positions (
    position_id TEXT PRIMARY KEY,
    symbol      TEXT NOT NULL,
    side        TEXT NOT NULL,
    volume      REAL NOT NULL,
    open_price  REAL NOT NULL,
    stop_loss   REAL,
    take_profit REAL,
    open_time   REAL NOT NULL DEFAULT 0.0,
    strategy_id TEXT NOT NULL DEFAULT '',
    comment     TEXT NOT NULL DEFAULT ''
);
"""


class StateStore:
    """SQLite-backed state persistence for the NovaTrade trading pipeline.

    All methods are synchronous and safe to call from async contexts
    (each call uses its own short-lived connection).

    Args:
        db_path: Path to the SQLite database file. Created if it doesn't exist.
    """

    def __init__(self, db_path: Path | str) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        """Create a new connection with standard pragmas."""
        conn = sqlite3.connect(str(self._db_path), timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        """Create tables and set schema version."""
        try:
            conn = self._connect()
            conn.executescript(_CREATE_TABLES)
            conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
            conn.commit()
            conn.close()
            log.info("state_store: initialized at %s", self._db_path)
        except Exception:
            log.exception("state_store: failed to initialize database")

    # -----------------------------------------------------------------
    # Agent state
    # -----------------------------------------------------------------

    def save_agent_state(
        self,
        state: str,
        *,
        agent_id: str = "default",
        pending_order_id: str | None = None,
        pending_side: str | None = None,
        pending_symbol: str | None = None,
        position_id: str | None = None,
        position_side: str | None = None,
        position_symbol: str | None = None,
        position_volume: float = 0.0,
    ) -> None:
        """Persist the current agent FSM state (upsert)."""
        try:
            conn = self._connect()
            conn.execute(
                """INSERT OR REPLACE INTO agent_state
                   (agent_id, state, pending_order_id, pending_side,
                    pending_symbol, position_id, position_side,
                    position_symbol, position_volume, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    agent_id,
                    state,
                    pending_order_id,
                    pending_side,
                    pending_symbol,
                    position_id,
                    position_side,
                    position_symbol,
                    position_volume,
                    time.time(),
                ),
            )
            conn.commit()
            conn.close()
        except Exception:
            log.exception("state_store: failed to save agent state")

    def load_agent_state(self, agent_id: str = "default") -> dict | None:
        """Load the persisted agent FSM state.

        Returns a dict with FSM fields, or None if no state exists.
        """
        try:
            conn = self._connect()
            row = conn.execute(
                "SELECT * FROM agent_state WHERE agent_id = ?",
                (agent_id,),
            ).fetchone()
            conn.close()
            if row is None:
                return None
            return dict(row)
        except Exception:
            log.exception("state_store: failed to load agent state")
            return None

    # -----------------------------------------------------------------
    # Idempotency keys
    # -----------------------------------------------------------------

    def add_idempotency_key(self, key: str) -> None:
        """Add an idempotency key to the store."""
        try:
            conn = self._connect()
            conn.execute(
                "INSERT OR IGNORE INTO idempotency_keys (key, created_at) VALUES (?, ?)",
                (key, time.time()),
            )
            conn.commit()
            conn.close()
        except Exception:
            log.exception("state_store: failed to add idempotency key")

    def has_idempotency_key(self, key: str) -> bool:
        """Check if an idempotency key exists."""
        try:
            conn = self._connect()
            row = conn.execute(
                "SELECT 1 FROM idempotency_keys WHERE key = ?",
                (key,),
            ).fetchone()
            conn.close()
            return row is not None
        except Exception:
            log.exception("state_store: failed to check idempotency key")
            return False

    def load_idempotency_keys(self, limit: int = 1000) -> set[str]:
        """Load the most recent idempotency keys."""
        try:
            conn = self._connect()
            rows = conn.execute(
                "SELECT key FROM idempotency_keys ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            conn.close()
            return {row["key"] for row in rows}
        except Exception:
            log.exception("state_store: failed to load idempotency keys")
            return set()

    def prune_idempotency_keys(self, keep: int = 1000) -> int:
        """Delete idempotency keys beyond the most recent *keep* entries.

        Returns the number of deleted rows.
        """
        try:
            conn = self._connect()
            cursor = conn.execute(
                """DELETE FROM idempotency_keys
                   WHERE key NOT IN (
                       SELECT key FROM idempotency_keys
                       ORDER BY created_at DESC LIMIT ?
                   )""",
                (keep,),
            )
            deleted = cursor.rowcount
            conn.commit()
            conn.close()
            if deleted > 0:
                log.debug("state_store: pruned %d idempotency keys", deleted)
            return deleted
        except Exception:
            log.exception("state_store: failed to prune idempotency keys")
            return 0

    # -----------------------------------------------------------------
    # Expected positions
    # -----------------------------------------------------------------

    def save_expected_position(self, position: Position) -> None:
        """Persist an expected position (upsert)."""
        try:
            conn = self._connect()
            conn.execute(
                """INSERT OR REPLACE INTO expected_positions
                   (position_id, symbol, side, volume, open_price,
                    stop_loss, take_profit, open_time, strategy_id, comment)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    position.position_id,
                    position.symbol,
                    position.side.value,
                    position.volume,
                    position.open_price,
                    position.stop_loss,
                    position.take_profit,
                    position.open_time,
                    position.strategy_id,
                    position.comment,
                ),
            )
            conn.commit()
            conn.close()
        except Exception:
            log.exception("state_store: failed to save expected position")

    def remove_expected_position(self, position_id: str) -> None:
        """Remove an expected position by ID."""
        try:
            conn = self._connect()
            conn.execute(
                "DELETE FROM expected_positions WHERE position_id = ?",
                (position_id,),
            )
            conn.commit()
            conn.close()
        except Exception:
            log.exception("state_store: failed to remove expected position")

    def load_expected_positions(self) -> dict[str, Position]:
        """Load all expected positions.

        Returns a dict mapping position_id -> Position.
        """
        try:
            conn = self._connect()
            rows = conn.execute("SELECT * FROM expected_positions").fetchall()
            conn.close()
            result: dict[str, Position] = {}
            for row in rows:
                pos = Position(
                    position_id=row["position_id"],
                    symbol=row["symbol"],
                    side=OrderSide(row["side"]),
                    volume=row["volume"],
                    open_price=row["open_price"],
                    stop_loss=row["stop_loss"],
                    take_profit=row["take_profit"],
                    open_time=row["open_time"],
                    strategy_id=row["strategy_id"],
                    comment=row["comment"],
                )
                result[pos.position_id] = pos
            return result
        except Exception:
            log.exception("state_store: failed to load expected positions")
            return {}

    def clear_expected_positions(self) -> None:
        """Remove all expected positions."""
        try:
            conn = self._connect()
            conn.execute("DELETE FROM expected_positions")
            conn.commit()
            conn.close()
        except Exception:
            log.exception("state_store: failed to clear expected positions")
