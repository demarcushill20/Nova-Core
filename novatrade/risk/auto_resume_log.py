"""Rolling counter for daily-halt auto-resumes.

When a daily-scoped halt (daily loss cap or daily drawdown breach) is tripped,
the deterministic daily reset at the Prague midnight boundary auto-resumes it
so trading can continue the next day. A strategy that keeps hitting the cap
every day is *bleeding* — auto-resuming forever masks the problem and defers
operator attention.

This module records each auto-resume to an append-only JSONL file and exposes
a rolling 7-day count. Callers check the count before auto-resuming: if more
than ``MAX_AUTO_RESUMES_PER_WEEK`` resumes have happened in the last rolling
week, the halt stays active and requires a manual operator resume.

The file location is ``STATE/supervisor/daily_halt_resume_log.jsonl``. Both
``HardRiskSupervisor`` and ``RiskEngine`` share the same log so the count is
aggregate across components.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path

log = logging.getLogger("novatrade.risk.auto_resume_log")

DEFAULT_LOG_PATH = Path("STATE/supervisor/daily_halt_resume_log.jsonl")

MAX_AUTO_RESUMES_PER_WEEK = 2
_ROLLING_WINDOW_DAYS = 7
_RETAIN_WINDOW_DAYS = 14


def record_auto_resume(
    halt_reason: str,
    *,
    origin: str,
    equity: float | None = None,
    path: Path = DEFAULT_LOG_PATH,
) -> None:
    """Append a single auto-resume event to the rolling log."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        entry: dict[str, str | float] = {
            "ts_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "origin": origin,
            "halt_reason": halt_reason,
            "resumed_by": "auto_daily_reset",
        }
        if equity is not None:
            entry["equity"] = round(float(equity), 2)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError as exc:
        log.error("auto_resume_log: append failed: %s", exc)


def count_recent_auto_resumes(
    *,
    path: Path = DEFAULT_LOG_PATH,
    now: dt.datetime | None = None,
) -> int:
    """Return the count of auto-resume events within the rolling week."""
    if not path.exists():
        return 0
    if now is None:
        now = dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(days=_ROLLING_WINDOW_DAYS)
    retained_cutoff = now - dt.timedelta(days=_RETAIN_WINDOW_DAYS)

    retained_lines: list[str] = []
    in_window = 0
    try:
        with path.open("r", encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                    ts = dt.datetime.fromisoformat(entry["ts_utc"])
                except (ValueError, KeyError, TypeError):
                    continue
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=dt.timezone.utc)
                if ts >= retained_cutoff:
                    retained_lines.append(raw)
                if ts >= cutoff:
                    in_window += 1
    except OSError as exc:
        log.warning("auto_resume_log: read failed: %s", exc)
        return 0

    # Trim ancient entries so the file doesn't grow unbounded.
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            ("\n".join(retained_lines) + "\n") if retained_lines else "",
            encoding="utf-8",
        )
        tmp.replace(path)
    except OSError as exc:
        log.debug("auto_resume_log: trim failed: %s", exc)

    return in_window


def auto_resume_allowed(
    *,
    path: Path = DEFAULT_LOG_PATH,
    now: dt.datetime | None = None,
) -> bool:
    """Return True if another auto-resume is allowed in the rolling window."""
    return count_recent_auto_resumes(path=path, now=now) < MAX_AUTO_RESUMES_PER_WEEK
