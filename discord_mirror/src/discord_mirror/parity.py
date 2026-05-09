from __future__ import annotations

from .storage import Storage


async def daily_report(storage: Storage, since_iso: str) -> dict:
    db = storage._conn
    cur = await db.execute("SELECT COUNT(*) FROM raw_messages WHERE ts >= ?", (since_iso,))
    row = await cur.fetchone()
    raw = row[0] if row else 0

    cur = await db.execute(
        "SELECT COUNT(*) FROM signals WHERE created_at >= ? AND action = 'OPEN'",
        (since_iso,),
    )
    row = await cur.fetchone()
    open_signals = row[0] if row else 0

    cur = await db.execute("SELECT COUNT(*), SUM(lot) FROM trades WHERE created_at >= ?", (since_iso,))
    row = await cur.fetchone()
    trades = row[0] if row else 0
    total_lot = (row[1] if row else 0.0) or 0.0

    cur = await db.execute(
        "SELECT state, COUNT(*) FROM trades WHERE created_at >= ? GROUP BY state",
        (since_iso,),
    )
    states = {r[0]: r[1] for r in await cur.fetchall()}

    cur = await db.execute(
        "SELECT mode, COUNT(*) FROM trades WHERE created_at >= ? GROUP BY mode",
        (since_iso,),
    )
    modes = {r[0]: r[1] for r in await cur.fetchall()}

    return {
        "since": since_iso,
        "raw_messages": raw,
        "open_signals": open_signals,
        "trades": trades,
        "total_lot": round(total_lot, 4),
        "trade_states": states,
        "trade_modes": modes,
    }
