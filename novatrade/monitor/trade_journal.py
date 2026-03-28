"""Trade journal — append-only JSONL log of trade lifecycle events.

Captures OPEN, CLOSE, and REJECT events for post-trade analysis.
Written to STATE/novatrade/trade_journal.jsonl.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

JOURNAL_DIR = Path("/home/nova/nova-core/STATE/novatrade")
JOURNAL_FILE = JOURNAL_DIR / "trade_journal.jsonl"

# Keep journal bounded — rotate after this many lines
MAX_JOURNAL_LINES = 10_000


def _ensure_dir() -> None:
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)


def _append_entry(entry: dict) -> None:
    """Append a single JSON line to the trade journal."""
    _ensure_dir()
    entry["logged_at"] = datetime.now(timezone.utc).isoformat()
    try:
        line = json.dumps(entry, default=str) + "\n"
        with open(JOURNAL_FILE, "a") as f:
            f.write(line)
    except OSError as exc:
        log.warning("trade journal write failed: %s", exc)


def log_trade_open(
    *,
    position_id: str,
    symbol: str,
    side: str,
    volume: float,
    entry_price: float,
    stop_loss: float,
    strategy: str = "IRB",
) -> None:
    """Record a trade open event."""
    _append_entry(
        {
            "event": "OPEN",
            "position_id": position_id,
            "symbol": symbol,
            "side": side,
            "volume": volume,
            "entry_price": entry_price,
            "stop_loss": stop_loss,
            "strategy": strategy,
        }
    )


def log_trade_close(
    *,
    position_id: str,
    symbol: str,
    side: str,
    volume: float,
    pnl_usd: float,
    pnl_pips: float = 0.0,
    exit_reason: str = "",
    strategy: str = "IRB",
) -> None:
    """Record a trade close event."""
    _append_entry(
        {
            "event": "CLOSE",
            "position_id": position_id,
            "symbol": symbol,
            "side": side,
            "volume": volume,
            "pnl_usd": pnl_usd,
            "pnl_pips": pnl_pips,
            "exit_reason": exit_reason,
            "strategy": strategy,
        }
    )


def log_trade_reject(
    *,
    symbol: str,
    side: str,
    reason: str,
    gate: str = "",
    strategy: str = "IRB",
) -> None:
    """Record a signal rejection by risk gates."""
    _append_entry(
        {
            "event": "REJECT",
            "symbol": symbol,
            "side": side,
            "reason": reason,
            "gate": gate,
            "strategy": strategy,
        }
    )


def get_journal_stats() -> dict:
    """Return summary stats from the trade journal."""
    if not JOURNAL_FILE.exists():
        return {"total": 0, "opens": 0, "closes": 0, "rejects": 0}
    opens = closes = rejects = total = 0
    try:
        with open(JOURNAL_FILE) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                total += 1
                try:
                    entry = json.loads(line)
                    ev = entry.get("event", "")
                    if ev == "OPEN":
                        opens += 1
                    elif ev == "CLOSE":
                        closes += 1
                    elif ev == "REJECT":
                        rejects += 1
                except json.JSONDecodeError:
                    pass
    except OSError:
        pass
    return {"total": total, "opens": opens, "closes": closes, "rejects": rejects}
