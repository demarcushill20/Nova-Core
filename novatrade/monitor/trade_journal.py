"""Trade journal — append-only JSONL log of trade lifecycle events.

Captures OPEN, CLOSE, and REJECT events for post-trade analysis.
Written to STATE/novatrade/trade_journal.jsonl.

Rotation: when the journal exceeds MAX_JOURNAL_LINES, the oldest entries
are archived to trade_journal.<timestamp>.jsonl and the active file is
truncated to the most recent KEEP_AFTER_ROTATE lines.
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

JOURNAL_DIR = Path("/home/nova/nova-core/STATE/novatrade")
JOURNAL_FILE = JOURNAL_DIR / "trade_journal.jsonl"

# Rotate when the journal exceeds this many lines
MAX_JOURNAL_LINES = 10_000
# After rotation, keep the most recent N lines in the active file
KEEP_AFTER_ROTATE = 2_000
# Check line count every N appends to avoid stat-ing every write
_ROTATE_CHECK_INTERVAL = 100
_append_counter = 0


def _ensure_dir() -> None:
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)


def _count_lines(path: Path) -> int:
    """Count lines in a file without loading it into memory."""
    try:
        count = 0
        with open(path, "rb") as f:
            for _ in f:
                count += 1
        return count
    except OSError:
        return 0


def _rotate_if_needed() -> None:
    """Archive old entries when the journal exceeds MAX_JOURNAL_LINES."""
    if not JOURNAL_FILE.exists():
        return
    line_count = _count_lines(JOURNAL_FILE)
    if line_count <= MAX_JOURNAL_LINES:
        return

    log.info("trade journal has %d lines (max %d), rotating", line_count, MAX_JOURNAL_LINES)
    try:
        with open(JOURNAL_FILE) as f:
            lines = f.readlines()

        # Archive the full file
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        archive_path = JOURNAL_DIR / f"trade_journal.{ts}.jsonl"
        with open(archive_path, "w") as f:
            f.writelines(lines)

        # Keep only the most recent entries in the active file
        keep = lines[-KEEP_AFTER_ROTATE:] if len(lines) > KEEP_AFTER_ROTATE else lines
        tmp_path = JOURNAL_FILE.with_suffix(".jsonl.tmp")
        with open(tmp_path, "w") as f:
            f.writelines(keep)
        os.replace(tmp_path, JOURNAL_FILE)

        log.info(
            "rotated: archived %d lines to %s, kept %d in active journal",
            len(lines),
            archive_path.name,
            len(keep),
        )
    except OSError as exc:
        log.warning("trade journal rotation failed: %s", exc)


def _append_entry(entry: dict) -> None:
    """Append a single JSON line to the trade journal, rotating if needed."""
    global _append_counter
    _ensure_dir()
    entry["logged_at"] = datetime.now(timezone.utc).isoformat()
    try:
        line = json.dumps(entry, default=str) + "\n"
        with open(JOURNAL_FILE, "a") as f:
            f.write(line)
        _append_counter += 1
        if _append_counter >= _ROTATE_CHECK_INTERVAL:
            _append_counter = 0
            _rotate_if_needed()
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


def log_trade_verify(
    *,
    position_id: str,
    symbol: str,
    side: str,
    verdict: str,
    match_score: float,
    detail: str,
    strategy: str = "IRB",
) -> None:
    """Record a post-trade verification result."""
    _append_entry(
        {
            "event": "VERIFY",
            "position_id": position_id,
            "symbol": symbol,
            "side": side,
            "verdict": verdict,
            "match_score": match_score,
            "detail": detail,
            "strategy": strategy,
        }
    )


def rotate_now() -> dict:
    """Force an immediate rotation regardless of line count.

    Returns a dict with keys: archived_lines, kept_lines, archive_path.
    Useful for manual cleanup or scheduled maintenance.
    """
    if not JOURNAL_FILE.exists():
        return {"archived_lines": 0, "kept_lines": 0, "archive_path": None}
    line_count = _count_lines(JOURNAL_FILE)
    if line_count == 0:
        return {"archived_lines": 0, "kept_lines": 0, "archive_path": None}

    try:
        with open(JOURNAL_FILE) as f:
            lines = f.readlines()

        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        archive_path = JOURNAL_DIR / f"trade_journal.{ts}.jsonl"
        with open(archive_path, "w") as f:
            f.writelines(lines)

        keep = lines[-KEEP_AFTER_ROTATE:] if len(lines) > KEEP_AFTER_ROTATE else lines
        tmp_path = JOURNAL_FILE.with_suffix(".jsonl.tmp")
        with open(tmp_path, "w") as f:
            f.writelines(keep)
        os.replace(tmp_path, JOURNAL_FILE)

        log.info("manual rotation: archived %d, kept %d", len(lines), len(keep))
        return {
            "archived_lines": len(lines),
            "kept_lines": len(keep),
            "archive_path": str(archive_path),
        }
    except OSError as exc:
        log.warning("manual rotation failed: %s", exc)
        return {"archived_lines": 0, "kept_lines": 0, "archive_path": None, "error": str(exc)}


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
