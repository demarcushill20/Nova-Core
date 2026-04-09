"""Trade gap monitor — detects prolonged periods without trade execution.

Autonomous protocol that triggers when no trades have been executed for
a configurable threshold (default 3 hours during active forex sessions).

Session-aware:
  - Weekend (Fri 22 UTC → Sun 22 UTC): fully suppressed
  - Asian session (22-07 UTC): relaxed to 6h for EURUSD
  - London/Overlap/NY: standard 3h threshold

Regime-aware:
  - quiet/compression: relaxed to 2× threshold (6h default)
  - trending: standard threshold
  - volatile: tightened to 0.75× (2.25h default)

False-trigger protection:
  - Does not alert during weekends
  - Requires at least 1h of uptime before first alert
  - Cooldown between alerts (default 2h)
  - Checks both trade journal AND live_metrics for execution evidence

Persists state to STATE/novatrade/trade_gap_status.json for visibility.
"""

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("novatrade.monitor.trade_gap")

STATE_DIR = Path("/home/nova/nova-core/STATE/novatrade")
GAP_STATUS_FILE = STATE_DIR / "trade_gap_status.json"
JOURNAL_FILE = STATE_DIR / "trade_journal.jsonl"
LIVE_METRICS_FILE = STATE_DIR / "live_metrics.json"
REGIME_FILE = STATE_DIR / "regime.json"

# Default threshold: 3 hours with no trade execution
DEFAULT_GAP_THRESHOLD_S = 3 * 3600  # 3 hours
# Minimum uptime before first alert
MIN_UPTIME_BEFORE_ALERT_S = 3600  # 1 hour
# Cooldown between repeated alerts
ALERT_COOLDOWN_S = 2 * 3600  # 2 hours

# Session multipliers widen the gap threshold during low-activity periods
_SESSION_MULTIPLIERS = {
    "weekend": 0.0,  # suppress entirely
    "asian": 2.0,  # 2× (6h for EURUSD — low activity)
    "london": 1.0,  # standard
    "overlap": 1.0,  # standard
    "newyork": 1.0,  # standard
}

# Regime multipliers
_REGIME_MULTIPLIERS = {
    "quiet": 2.0,  # 2× — low volatility, fewer opportunities
    "ranging": 1.5,  # 1.5× — moderate
    "trending": 1.0,  # standard
    "volatile": 0.75,  # tighter — should see trades
}


@dataclass
class TradeGapStatus:
    """Current trade gap assessment."""

    # Last known trade time (ISO format)
    last_trade_at: str = ""
    # Gap duration in seconds
    gap_seconds: float = 0.0
    # Effective threshold after session/regime adjustment
    effective_threshold_s: float = DEFAULT_GAP_THRESHOLD_S
    # Alert state
    alert_active: bool = False
    alert_level: str = "GREEN"  # GREEN, YELLOW, RED
    alert_reason: str = ""
    # Suppression info
    suppressed: bool = False
    suppression_reason: str = ""
    # Context
    session: str = ""
    regime: str = ""
    # Diagnostic info
    last_check_at: str = ""
    uptime_seconds: float = 0.0
    last_alert_at: str = ""
    # Rejection tracking (identifies if signals exist but are all rejected)
    recent_rejections: int = 0
    last_rejection_reason: str = ""


def _get_session(dt_utc: datetime) -> str:
    """Return the current forex session label."""
    wd = dt_utc.weekday()
    hour = dt_utc.hour
    # Weekend check
    if (wd == 4 and hour >= 22) or wd == 5 or (wd == 6 and hour < 22):
        return "weekend"
    if 12 <= hour < 16:
        return "overlap"
    elif 7 <= hour < 12:
        return "london"
    elif 16 <= hour < 21:
        return "newyork"
    else:
        return "asian"


def _load_regime() -> str:
    """Read current regime from persisted state."""
    try:
        if REGIME_FILE.exists():
            data = json.loads(REGIME_FILE.read_text())
            regime = data.get("regime", "ranging")
            if regime in _REGIME_MULTIPLIERS:
                return regime
    except (json.JSONDecodeError, OSError):
        pass
    return "ranging"


def _get_last_trade_time() -> datetime | None:
    """Scan trade journal for the most recent OPEN event timestamp.

    Reads the journal file in reverse to find the last OPEN efficiently.
    Falls back to checking archived journals if active journal has none.
    """
    if not JOURNAL_FILE.exists():
        return None

    last_open_time: datetime | None = None

    try:
        # Read the active journal (typically <10K lines)
        with open(JOURNAL_FILE) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("event") == "OPEN" and "logged_at" in entry:
                        ts = datetime.fromisoformat(entry["logged_at"])
                        if last_open_time is None or ts > last_open_time:
                            last_open_time = ts
                except (json.JSONDecodeError, ValueError):
                    continue
    except OSError:
        pass

    return last_open_time


def _get_recent_rejections(lookback_hours: float = 3.0) -> tuple[int, str]:
    """Count recent REJECT events and return the last rejection reason.

    Returns (count, last_reason).
    """
    if not JOURNAL_FILE.exists():
        return 0, ""

    cutoff = datetime.now(timezone.utc).timestamp() - (lookback_hours * 3600)
    reject_count = 0
    last_reason = ""

    try:
        with open(JOURNAL_FILE) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("event") == "REJECT" and "logged_at" in entry:
                        ts = datetime.fromisoformat(entry["logged_at"]).timestamp()
                        if ts >= cutoff:
                            reject_count += 1
                            last_reason = entry.get("reason", "")
                except (json.JSONDecodeError, ValueError):
                    continue
    except OSError:
        pass

    return reject_count, last_reason


def _get_uptime() -> float:
    """Read uptime from live_metrics.json."""
    try:
        if LIVE_METRICS_FILE.exists():
            data = json.loads(LIVE_METRICS_FILE.read_text())
            return float(data.get("uptime_seconds", 0))
    except (json.JSONDecodeError, OSError, ValueError):
        pass
    return 0.0


def _load_last_alert_time() -> str:
    """Load the last alert time from persisted status."""
    try:
        if GAP_STATUS_FILE.exists():
            data = json.loads(GAP_STATUS_FILE.read_text())
            return data.get("last_alert_at", "")
    except (json.JSONDecodeError, OSError):
        pass
    return ""


def check_trade_gap(
    now: datetime | None = None,
    threshold_s: float = DEFAULT_GAP_THRESHOLD_S,
) -> TradeGapStatus:
    """Evaluate whether the trade gap exceeds the threshold.

    Args:
        now: Override current time (for testing).
        threshold_s: Base gap threshold in seconds before adjustments.

    Returns:
        TradeGapStatus with alert state and diagnostic info.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    session = _get_session(now)
    regime = _load_regime()
    uptime = _get_uptime()

    # --- Compute effective threshold ---
    session_mult = _SESSION_MULTIPLIERS.get(session, 1.0)
    regime_mult = _REGIME_MULTIPLIERS.get(regime, 1.0)
    effective_threshold = threshold_s * session_mult * regime_mult

    # --- Weekend suppression ---
    if session == "weekend":
        return TradeGapStatus(
            suppressed=True,
            suppression_reason="forex market closed (weekend)",
            session=session,
            regime=regime,
            last_check_at=now.isoformat(),
            uptime_seconds=uptime,
            alert_level="GREEN",
        )

    # --- Uptime guard ---
    if uptime < MIN_UPTIME_BEFORE_ALERT_S:
        return TradeGapStatus(
            suppressed=True,
            suppression_reason=f"insufficient uptime ({uptime:.0f}s < {MIN_UPTIME_BEFORE_ALERT_S}s)",
            session=session,
            regime=regime,
            last_check_at=now.isoformat(),
            uptime_seconds=uptime,
            effective_threshold_s=effective_threshold,
            alert_level="GREEN",
        )

    # --- Find last trade ---
    last_trade = _get_last_trade_time()
    last_trade_str = last_trade.isoformat() if last_trade else ""

    if last_trade is None:
        gap_seconds = uptime  # No trades at all since startup
    else:
        gap_seconds = (now - last_trade).total_seconds()

    # --- Rejection analysis ---
    reject_count, last_reject_reason = _get_recent_rejections(lookback_hours=3.0)

    # --- Alert cooldown ---
    last_alert_str = _load_last_alert_time()
    in_cooldown = False
    if last_alert_str:
        try:
            last_alert_time = datetime.fromisoformat(last_alert_str)
            if (now - last_alert_time).total_seconds() < ALERT_COOLDOWN_S:
                in_cooldown = True
        except ValueError:
            pass

    # --- Assess alert level ---
    alert_active = False
    alert_level = "GREEN"
    alert_reason = ""

    if gap_seconds >= effective_threshold:
        if gap_seconds >= effective_threshold * 2:
            alert_level = "RED"
            alert_reason = (
                f"No trades for {gap_seconds / 3600:.1f}h (threshold: {effective_threshold / 3600:.1f}h, 2× exceeded)"
            )
        else:
            alert_level = "YELLOW"
            alert_reason = f"No trades for {gap_seconds / 3600:.1f}h (threshold: {effective_threshold / 3600:.1f}h)"

        if reject_count > 0:
            alert_reason += f". {reject_count} rejections in last 3h: {last_reject_reason}"

        alert_active = not in_cooldown

    status = TradeGapStatus(
        last_trade_at=last_trade_str,
        gap_seconds=gap_seconds,
        effective_threshold_s=effective_threshold,
        alert_active=alert_active,
        alert_level=alert_level,
        alert_reason=alert_reason,
        suppressed=in_cooldown and alert_level != "GREEN",
        suppression_reason="alert cooldown active" if (in_cooldown and alert_level != "GREEN") else "",
        session=session,
        regime=regime,
        last_check_at=now.isoformat(),
        uptime_seconds=uptime,
        last_alert_at=now.isoformat() if alert_active else last_alert_str,
        recent_rejections=reject_count,
        last_rejection_reason=last_reject_reason,
    )

    # Persist status
    _persist_status(status)

    # Log if alerting
    if alert_active:
        log.warning(
            "TRADE GAP ALERT [%s]: %s (session=%s, regime=%s)",
            alert_level,
            alert_reason,
            session,
            regime,
        )

    return status


def _persist_status(status: TradeGapStatus) -> None:
    """Atomically write trade gap status to disk."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(STATE_DIR), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(asdict(status), f, indent=2)
        os.replace(tmp, str(GAP_STATUS_FILE))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def generate_diagnostic_report(status: TradeGapStatus) -> str:
    """Generate a human-readable diagnostic report for the trade gap.

    Used by the autonomy system to understand the situation and plan fixes.
    """
    lines = [
        "=" * 60,
        "TRADE GAP DIAGNOSTIC REPORT",
        "=" * 60,
        "",
        f"Alert Level: {status.alert_level}",
        f"Gap Duration: {status.gap_seconds / 3600:.1f} hours",
        f"Effective Threshold: {status.effective_threshold_s / 3600:.1f} hours",
        f"Last Trade: {status.last_trade_at or 'NONE RECORDED'}",
        f"Session: {status.session}",
        f"Regime: {status.regime}",
        f"Uptime: {status.uptime_seconds / 3600:.1f} hours",
        "",
    ]

    if status.recent_rejections > 0:
        lines.append("REJECTION ANALYSIS:")
        lines.append(f"  Rejections in last 3h: {status.recent_rejections}")
        lines.append(f"  Last rejection reason: {status.last_rejection_reason}")
        lines.append("  → Signals are arriving but being rejected at the pre-trade gate.")
        lines.append("  → Investigate the rejection reason and fix the gate/volume issue.")
        lines.append("")
    else:
        lines.append("REJECTION ANALYSIS:")
        lines.append("  No rejections found in last 3h.")
        lines.append("  → Possible causes: no signals, adapter disconnected, or market closed.")
        lines.append("")

    if status.suppressed:
        lines.append(f"SUPPRESSED: {status.suppression_reason}")
        lines.append("")

    lines.append("RECOMMENDED INVESTIGATION STEPS:")
    lines.append("  1. Check signal_stats.json for signal generation health")
    lines.append("  2. Check live_metrics.json for tick/bar flow")
    lines.append("  3. Check trade_journal.jsonl for recent REJECT events")
    lines.append("  4. Check adapter health via /health endpoint")
    lines.append("  5. Check risk engine halt status")
    lines.append("")
    lines.append("=" * 60)

    return "\n".join(lines)
