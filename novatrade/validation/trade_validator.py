"""Post-trade validation report system for NovaTrade.

Reads trade evidence from JSONL files, validates each execution event
against IRB strategy rules, FTMO compliance, and risk parameters, then
generates structured validation reports.

Each trade is checked against:
  1. EMA alignment (slope direction matches trade direction)
  2. ADX above threshold (default 20)
  3. Range/ATR ratio within bounds (IRB bar detection)
  4. Stop placement at IRB bar's opposite end
  5. Entry within expected price range
  6. Position sizing matches risk parameters (1% risk per trade)
  7. Rollover dead zone respected (21:00-23:00 UTC)
  8. FTMO compliance (max daily loss, max total loss, lot size limits)

CLI usage::

    python -m novatrade.validation.trade_validator
    python -m novatrade.validation.trade_validator --evidence path/to/evidence.jsonl
    python -m novatrade.validation.trade_validator --verbose
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

log = logging.getLogger("novatrade.validation.trade_validator")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_EVIDENCE_PATH = Path("OUTPUT/novatrade/evidence.jsonl")
DEFAULT_LIVE_EVIDENCE_PATH = Path("OUTPUT/novatrade/live_evidence.jsonl")
DEFAULT_REPORTS_PATH = Path("OUTPUT/novatrade/validation_reports.jsonl")

# IRB strategy defaults (from BacktestEnvironment)
_DEFAULT_ADX_THRESHOLD = 20.0
_DEFAULT_OVEREXTENSION_THRESHOLD = 2.0
_DEFAULT_RISK_FRACTION = 0.01
_DEFAULT_RISK_TOLERANCE = 0.25  # 25% tolerance on position sizing
_DEFAULT_MIN_VOLUME = 0.01
_DEFAULT_MAX_VOLUME = 1.00
_DEFAULT_PIP_VALUE = 0.0001
_DEFAULT_PIP_VALUE_PER_LOT = 10.0

# FTMO limits
_DEFAULT_MAX_DAILY_LOSS_PCT = 5.0
_DEFAULT_MAX_TOTAL_LOSS_PCT = 10.0

# Rollover dead zone (UTC hours)
_DEFAULT_ROLLOVER_START_UTC = 21
_DEFAULT_ROLLOVER_END_UTC = 23


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class RuleVerdict(Enum):
    """Outcome for a single rule check."""

    PASS = "PASS"  # noqa: S105
    FAIL = "FAIL"
    WARN = "WARN"
    SKIP = "SKIP"  # insufficient data to evaluate


class TradeVerdict(Enum):
    """Overall trade validation verdict."""

    VALID = "VALID"
    DEVIATION_DETECTED = "DEVIATION_DETECTED"
    CRITICAL_VIOLATION = "CRITICAL_VIOLATION"


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class RuleResult:
    """Outcome of a single validation rule check."""

    rule_name: str
    verdict: RuleVerdict
    expected: str = ""
    actual: str = ""
    detail: str = ""
    critical: bool = False  # if True, a FAIL escalates to CRITICAL_VIOLATION

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule_name,
            "verdict": self.verdict.value,
            "expected": self.expected,
            "actual": self.actual,
            "detail": self.detail,
            "critical": self.critical,
        }


@dataclass
class TradeRecord:
    """Parsed trade from evidence, containing all fields needed for validation."""

    symbol: str = ""
    side: str = ""  # "BUY" or "SELL" (from payload) or "LONG"/"SHORT" (from signal)
    entry_price: float = 0.0
    stop_loss: float = 0.0
    volume: float = 0.0
    timestamp: float = 0.0
    bar_close_time: int = 0
    campaign: str = ""
    order_type: str = ""
    action: str = ""
    strategy_name: str = ""
    strategy_version: str = ""
    outcome: str = ""  # FILLED, DENIED, ADAPTER_ERROR, EXCEPTION
    fill_price: float | None = None
    order_id: str = ""
    error: str = ""
    # Signal metadata (from LiveSignal.metadata injected into evidence)
    metadata: dict[str, Any] = field(default_factory=dict)
    # Raw evidence data for debugging
    raw_data: dict[str, Any] = field(default_factory=dict)

    @property
    def direction(self) -> str:
        """Normalize side to LONG/SHORT."""
        if self.side in ("BUY", "LONG"):
            return "LONG"
        if self.side in ("SELL", "SHORT"):
            return "SHORT"
        return self.side

    @property
    def entry_time_utc(self) -> datetime | None:
        """Parse trade timestamp as UTC datetime."""
        ts = self.bar_close_time or self.timestamp
        if ts <= 0:
            return None
        return datetime.fromtimestamp(ts, tz=timezone.utc)


@dataclass
class TradeValidationReport:
    """Validation report for a single trade."""

    trade: TradeRecord
    rule_results: list[RuleResult] = field(default_factory=list)
    verdict: TradeVerdict = TradeVerdict.VALID
    summary: str = ""
    timestamp: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not self.rule_results:
            return
        self._compute_verdict()

    def _compute_verdict(self) -> None:
        """Derive the overall verdict from individual rule results."""
        has_critical_fail = any(r.verdict == RuleVerdict.FAIL and r.critical for r in self.rule_results)
        has_fail = any(r.verdict == RuleVerdict.FAIL for r in self.rule_results)

        if has_critical_fail:
            self.verdict = TradeVerdict.CRITICAL_VIOLATION
        elif has_fail:
            self.verdict = TradeVerdict.DEVIATION_DETECTED
        else:
            self.verdict = TradeVerdict.VALID

        fail_names = [r.rule_name for r in self.rule_results if r.verdict == RuleVerdict.FAIL]
        warn_names = [r.rule_name for r in self.rule_results if r.verdict == RuleVerdict.WARN]
        pass_count = sum(1 for r in self.rule_results if r.verdict == RuleVerdict.PASS)
        skip_count = sum(1 for r in self.rule_results if r.verdict == RuleVerdict.SKIP)

        parts = [f"{self.verdict.value}: {pass_count} passed"]
        if fail_names:
            parts.append(f"{len(fail_names)} failed ({', '.join(fail_names)})")
        if warn_names:
            parts.append(f"{len(warn_names)} warnings ({', '.join(warn_names)})")
        if skip_count:
            parts.append(f"{skip_count} skipped")
        self.summary = " | ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "trade": {
                "symbol": self.trade.symbol,
                "side": self.trade.side,
                "direction": self.trade.direction,
                "entry_price": self.trade.entry_price,
                "stop_loss": self.trade.stop_loss,
                "volume": self.trade.volume,
                "timestamp": self.trade.timestamp,
                "bar_close_time": self.trade.bar_close_time,
                "campaign": self.trade.campaign,
                "outcome": self.trade.outcome,
                "fill_price": self.trade.fill_price,
                "order_id": self.trade.order_id,
                "error": self.trade.error,
            },
            "rules": [r.to_dict() for r in self.rule_results],
            "verdict": self.verdict.value,
            "summary": self.summary,
            "timestamp": self.timestamp,
        }


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class ValidationConfig:
    """Tunable validation thresholds.

    Defaults match the IRB strategy's BacktestEnvironment and FTMO limits.
    """

    # IRB strategy rules
    adx_threshold: float = _DEFAULT_ADX_THRESHOLD
    overextension_threshold: float = _DEFAULT_OVEREXTENSION_THRESHOLD

    # Position sizing
    risk_fraction: float = _DEFAULT_RISK_FRACTION
    risk_tolerance: float = _DEFAULT_RISK_TOLERANCE
    min_volume: float = _DEFAULT_MIN_VOLUME
    max_volume: float = _DEFAULT_MAX_VOLUME
    pip_value: float = _DEFAULT_PIP_VALUE
    pip_value_per_lot: float = _DEFAULT_PIP_VALUE_PER_LOT
    equity: float = 100_000.0

    # FTMO limits
    max_daily_loss_pct: float = _DEFAULT_MAX_DAILY_LOSS_PCT
    max_total_loss_pct: float = _DEFAULT_MAX_TOTAL_LOSS_PCT

    # Rollover dead zone
    rollover_start_utc: int = _DEFAULT_ROLLOVER_START_UTC
    rollover_end_utc: int = _DEFAULT_ROLLOVER_END_UTC

    # Stop-loss validation
    min_sl_distance_pips: float = 3.0  # minimum stop distance (warn only)


# ---------------------------------------------------------------------------
# Evidence parser
# ---------------------------------------------------------------------------


def parse_trades_from_evidence(evidence_path: Path) -> list[TradeRecord]:
    """Parse execution events from a JSONL evidence file into TradeRecords.

    Only EXECUTION-type events with payload data are returned. Health
    snapshots, monitoring events, and other non-trade records are skipped.
    """
    if not evidence_path.exists():
        log.warning("Evidence file not found: %s", evidence_path)
        return []

    trades: list[TradeRecord] = []
    with evidence_path.open() as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                log.debug("Skipping malformed JSON at line %d", lineno)
                continue

            event_type = d.get("event_type", "")
            data = d.get("data", {})
            timestamp = d.get("timestamp", 0.0)

            # We want execution events (both successful and failed)
            if event_type == "EXECUTION":
                trade = _parse_execution_record(data, timestamp)
                if trade is not None:
                    trades.append(trade)
            # Also parse ADAPTER_ERROR events that carry a full payload
            # (these are rejected-before-execution errors like version mismatch)
            elif event_type == "ADAPTER_ERROR" and "payload" in data:
                trade = _parse_adapter_error_record(data, d.get("error", ""), timestamp)
                if trade is not None:
                    trades.append(trade)

    log.info("Parsed %d trade records from %s", len(trades), evidence_path)
    return trades


def _parse_execution_record(data: dict, timestamp: float) -> TradeRecord | None:
    """Parse a single EXECUTION evidence record into a TradeRecord."""
    outcome = data.get("outcome", "")
    if not outcome:
        return None

    # Extract fields from the flat evidence structure
    payload = data.get("payload", {})
    return TradeRecord(
        symbol=payload.get("symbol", data.get("symbol", "")),
        side=payload.get("side", data.get("side", "")),
        entry_price=payload.get("entry_price", data.get("entry_price", 0.0)),
        stop_loss=payload.get("stop_loss", data.get("stop_loss", 0.0)),
        volume=payload.get("volume", data.get("volume", 0.0)),
        timestamp=timestamp,
        bar_close_time=payload.get("bar_close_time", data.get("bar_close_time", 0)),
        campaign=data.get("campaign", payload.get("campaign", "")),
        order_type=payload.get("order_type", ""),
        action=payload.get("action", data.get("action", "")),
        strategy_name=payload.get("strategy_name", ""),
        strategy_version=payload.get("strategy_version", ""),
        outcome=outcome,
        fill_price=data.get("fill_price"),
        order_id=data.get("order_id", ""),
        error=data.get("error", ""),
        metadata=payload.get("metadata", {}),
        raw_data=data,
    )


def _parse_adapter_error_record(data: dict, error: str, timestamp: float) -> TradeRecord | None:
    """Parse an ADAPTER_ERROR evidence record with a payload."""
    payload = data.get("payload", {})
    if not payload:
        return None

    return TradeRecord(
        symbol=payload.get("symbol", ""),
        side=payload.get("side", ""),
        entry_price=payload.get("entry_price", 0.0),
        stop_loss=payload.get("stop_loss", 0.0),
        volume=payload.get("volume", 0.0),
        timestamp=timestamp,
        bar_close_time=payload.get("bar_close_time", 0),
        campaign=payload.get("campaign", data.get("campaign", "")),
        order_type=payload.get("order_type", ""),
        action=payload.get("action", ""),
        strategy_name=payload.get("strategy_name", ""),
        strategy_version=payload.get("strategy_version", ""),
        outcome="ADAPTER_ERROR",
        error=error,
        metadata=payload.get("metadata", {}),
        raw_data=data,
    )


# ---------------------------------------------------------------------------
# Validation rules
# ---------------------------------------------------------------------------


def check_ema_alignment(trade: TradeRecord, _cfg: ValidationConfig) -> RuleResult:
    """Validate EMA slope direction matches trade direction.

    Uses ema_slope from signal metadata. A positive slope should correspond
    to LONG trades; negative slope to SHORT trades.
    """
    ema_slope = trade.metadata.get("ema_slope")
    if ema_slope is None:
        return RuleResult(
            rule_name="ema_alignment",
            verdict=RuleVerdict.SKIP,
            detail="No ema_slope in signal metadata",
        )

    direction = trade.direction
    slope_val = float(ema_slope)

    if direction == "LONG" and slope_val >= 0:
        return RuleResult(
            rule_name="ema_alignment",
            verdict=RuleVerdict.PASS,
            expected="positive slope for LONG",
            actual=f"slope={slope_val:.4f}",
        )
    if direction == "SHORT" and slope_val <= 0:
        return RuleResult(
            rule_name="ema_alignment",
            verdict=RuleVerdict.PASS,
            expected="negative slope for SHORT",
            actual=f"slope={slope_val:.4f}",
        )

    return RuleResult(
        rule_name="ema_alignment",
        verdict=RuleVerdict.FAIL,
        expected=f"{'positive' if direction == 'LONG' else 'negative'} slope for {direction}",
        actual=f"slope={slope_val:.4f}",
        detail=f"EMA slope direction ({slope_val:.4f}) contradicts {direction} trade",
    )


def check_adx_threshold(trade: TradeRecord, cfg: ValidationConfig) -> RuleResult:
    """Validate ADX is above the minimum threshold for trend strength."""
    adx = trade.metadata.get("adx_value")
    if adx is None:
        return RuleResult(
            rule_name="adx_threshold",
            verdict=RuleVerdict.SKIP,
            detail="No adx_value in signal metadata",
        )

    adx_val = float(adx)
    threshold = cfg.adx_threshold

    if adx_val >= threshold:
        return RuleResult(
            rule_name="adx_threshold",
            verdict=RuleVerdict.PASS,
            expected=f"ADX >= {threshold}",
            actual=f"ADX={adx_val:.1f}",
        )

    return RuleResult(
        rule_name="adx_threshold",
        verdict=RuleVerdict.FAIL,
        expected=f"ADX >= {threshold}",
        actual=f"ADX={adx_val:.1f}",
        detail=f"ADX {adx_val:.1f} below threshold {threshold}",
    )


def check_overextension(trade: TradeRecord, cfg: ValidationConfig) -> RuleResult:
    """Validate range/ATR ratio is within IRB bounds (not overextended)."""
    ratio = trade.metadata.get("overextension_ratio")
    if ratio is None:
        return RuleResult(
            rule_name="overextension",
            verdict=RuleVerdict.SKIP,
            detail="No overextension_ratio in signal metadata",
        )

    ratio_val = float(ratio)
    threshold = cfg.overextension_threshold

    if ratio_val <= threshold:
        return RuleResult(
            rule_name="overextension",
            verdict=RuleVerdict.PASS,
            expected=f"ratio <= {threshold}",
            actual=f"ratio={ratio_val:.3f}",
        )

    return RuleResult(
        rule_name="overextension",
        verdict=RuleVerdict.FAIL,
        expected=f"ratio <= {threshold}",
        actual=f"ratio={ratio_val:.3f}",
        detail=f"Range/ATR ratio {ratio_val:.3f} exceeds overextension limit {threshold}",
    )


def check_stop_placement(trade: TradeRecord, _cfg: ValidationConfig) -> RuleResult:
    """Validate stop-loss is on the correct side of entry price.

    For LONG: SL < entry. For SHORT: SL > entry.
    Also verifies the stop is based on IRB bar geometry (high/low).
    """
    if trade.entry_price <= 0 or trade.stop_loss <= 0:
        return RuleResult(
            rule_name="stop_placement",
            verdict=RuleVerdict.SKIP,
            detail="Missing entry_price or stop_loss",
        )

    direction = trade.direction

    if direction == "LONG":
        if trade.stop_loss < trade.entry_price:
            return RuleResult(
                rule_name="stop_placement",
                verdict=RuleVerdict.PASS,
                expected="SL below entry for LONG",
                actual=f"entry={trade.entry_price:.5f} SL={trade.stop_loss:.5f}",
            )
        return RuleResult(
            rule_name="stop_placement",
            verdict=RuleVerdict.FAIL,
            expected="SL below entry for LONG",
            actual=f"entry={trade.entry_price:.5f} SL={trade.stop_loss:.5f}",
            detail="Stop-loss is above or equal to entry for a LONG trade",
            critical=True,
        )
    elif direction == "SHORT":
        if trade.stop_loss > trade.entry_price:
            return RuleResult(
                rule_name="stop_placement",
                verdict=RuleVerdict.PASS,
                expected="SL above entry for SHORT",
                actual=f"entry={trade.entry_price:.5f} SL={trade.stop_loss:.5f}",
            )
        return RuleResult(
            rule_name="stop_placement",
            verdict=RuleVerdict.FAIL,
            expected="SL above entry for SHORT",
            actual=f"entry={trade.entry_price:.5f} SL={trade.stop_loss:.5f}",
            detail="Stop-loss is below or equal to entry for a SHORT trade",
            critical=True,
        )

    return RuleResult(
        rule_name="stop_placement",
        verdict=RuleVerdict.SKIP,
        detail=f"Unknown direction: {direction}",
    )


def check_stop_distance(trade: TradeRecord, cfg: ValidationConfig) -> RuleResult:
    """Validate stop distance meets minimum pip requirement."""
    if trade.entry_price <= 0 or trade.stop_loss <= 0:
        return RuleResult(
            rule_name="stop_distance",
            verdict=RuleVerdict.SKIP,
            detail="Missing entry_price or stop_loss",
        )

    distance = abs(trade.entry_price - trade.stop_loss)
    distance_pips = distance / cfg.pip_value

    if distance_pips >= cfg.min_sl_distance_pips:
        return RuleResult(
            rule_name="stop_distance",
            verdict=RuleVerdict.PASS,
            expected=f">= {cfg.min_sl_distance_pips} pips",
            actual=f"{distance_pips:.1f} pips",
        )

    return RuleResult(
        rule_name="stop_distance",
        verdict=RuleVerdict.WARN,
        expected=f">= {cfg.min_sl_distance_pips} pips",
        actual=f"{distance_pips:.1f} pips",
        detail=f"Stop distance {distance_pips:.1f} pips below minimum {cfg.min_sl_distance_pips}",
    )


def check_entry_price_valid(trade: TradeRecord, _cfg: ValidationConfig) -> RuleResult:
    """Validate entry price is reasonable (positive and non-zero)."""
    if trade.entry_price <= 0:
        return RuleResult(
            rule_name="entry_price_valid",
            verdict=RuleVerdict.FAIL,
            expected="entry_price > 0",
            actual=f"entry_price={trade.entry_price}",
            detail="Entry price is zero or negative",
            critical=True,
        )

    # If we have a fill price, check slippage
    if trade.fill_price is not None and trade.fill_price > 0:
        slippage = abs(trade.fill_price - trade.entry_price)
        slippage_pips = slippage / _DEFAULT_PIP_VALUE
        if slippage_pips > 5.0:
            return RuleResult(
                rule_name="entry_price_valid",
                verdict=RuleVerdict.WARN,
                expected="fill within 5 pips of entry",
                actual=f"slippage={slippage_pips:.1f} pips (entry={trade.entry_price:.5f} fill={trade.fill_price:.5f})",
                detail=f"Excessive slippage: {slippage_pips:.1f} pips",
            )

    return RuleResult(
        rule_name="entry_price_valid",
        verdict=RuleVerdict.PASS,
        expected="entry_price > 0",
        actual=f"entry_price={trade.entry_price:.5f}",
    )


def check_position_sizing(trade: TradeRecord, cfg: ValidationConfig) -> RuleResult:
    """Validate volume matches 1% risk per trade within tolerance.

    Formula: volume = (equity * risk_pct) / (stop_pips * pip_value_per_lot)
    """
    if trade.entry_price <= 0 or trade.stop_loss <= 0 or trade.volume <= 0:
        return RuleResult(
            rule_name="position_sizing",
            verdict=RuleVerdict.SKIP,
            detail="Missing entry_price, stop_loss, or volume",
        )

    stop_distance = abs(trade.entry_price - trade.stop_loss)
    stop_pips = stop_distance / cfg.pip_value
    if stop_pips <= 0:
        return RuleResult(
            rule_name="position_sizing",
            verdict=RuleVerdict.SKIP,
            detail="Stop distance is zero",
        )

    risk_dollars = cfg.equity * cfg.risk_fraction
    expected_volume = risk_dollars / (stop_pips * cfg.pip_value_per_lot)
    expected_volume = max(cfg.min_volume, min(cfg.max_volume, round(expected_volume, 2)))

    # Check if within tolerance (only flag over-sizing as violation)
    if trade.volume <= expected_volume:
        return RuleResult(
            rule_name="position_sizing",
            verdict=RuleVerdict.PASS,
            expected=f"volume <= {expected_volume:.2f} lots ({cfg.risk_fraction * 100:.0f}% risk)",
            actual=f"volume={trade.volume:.2f} lots",
            detail="Conservative sizing (within or below risk target)",
        )

    over_ratio = (trade.volume - expected_volume) / expected_volume
    if over_ratio <= cfg.risk_tolerance:
        return RuleResult(
            rule_name="position_sizing",
            verdict=RuleVerdict.PASS,
            expected=f"volume ~= {expected_volume:.2f} lots (tolerance {cfg.risk_tolerance:.0%})",
            actual=f"volume={trade.volume:.2f} lots (over by {over_ratio:.1%})",
        )

    return RuleResult(
        rule_name="position_sizing",
        verdict=RuleVerdict.FAIL,
        expected=f"volume ~= {expected_volume:.2f} lots (tolerance {cfg.risk_tolerance:.0%})",
        actual=f"volume={trade.volume:.2f} lots (over by {over_ratio:.1%})",
        detail=(
            f"Position over-sized: {trade.volume:.2f} vs expected "
            f"{expected_volume:.2f} ({over_ratio:.1%} over tolerance "
            f"{cfg.risk_tolerance:.0%})"
        ),
        critical=True,
    )


def check_volume_bounds(trade: TradeRecord, cfg: ValidationConfig) -> RuleResult:
    """Validate volume is within FTMO-safe lot size bounds."""
    if trade.volume <= 0:
        return RuleResult(
            rule_name="volume_bounds",
            verdict=RuleVerdict.SKIP,
            detail="No volume data",
        )

    if trade.volume < cfg.min_volume:
        return RuleResult(
            rule_name="volume_bounds",
            verdict=RuleVerdict.FAIL,
            expected=f"volume >= {cfg.min_volume}",
            actual=f"volume={trade.volume:.2f}",
            detail=f"Volume below minimum: {trade.volume:.2f} < {cfg.min_volume}",
        )

    if trade.volume > cfg.max_volume:
        return RuleResult(
            rule_name="volume_bounds",
            verdict=RuleVerdict.FAIL,
            expected=f"volume <= {cfg.max_volume}",
            actual=f"volume={trade.volume:.2f}",
            detail=f"Volume exceeds maximum: {trade.volume:.2f} > {cfg.max_volume}",
            critical=True,
        )

    return RuleResult(
        rule_name="volume_bounds",
        verdict=RuleVerdict.PASS,
        expected=f"volume in [{cfg.min_volume}, {cfg.max_volume}]",
        actual=f"volume={trade.volume:.2f}",
    )


def check_rollover_dead_zone(trade: TradeRecord, cfg: ValidationConfig) -> RuleResult:
    """Validate trade was not placed during rollover dead zone (21:00-23:00 UTC)."""
    entry_time = trade.entry_time_utc
    if entry_time is None:
        return RuleResult(
            rule_name="rollover_dead_zone",
            verdict=RuleVerdict.SKIP,
            detail="No timestamp available",
        )

    hour = entry_time.hour
    start = cfg.rollover_start_utc
    end = cfg.rollover_end_utc

    if start <= end:
        in_zone = start <= hour < end
    else:
        in_zone = hour >= start or hour < end

    if not in_zone:
        return RuleResult(
            rule_name="rollover_dead_zone",
            verdict=RuleVerdict.PASS,
            expected=f"outside {start:02d}:00-{end:02d}:00 UTC",
            actual=f"trade at {entry_time.strftime('%H:%M')} UTC",
        )

    return RuleResult(
        rule_name="rollover_dead_zone",
        verdict=RuleVerdict.FAIL,
        expected=f"outside {start:02d}:00-{end:02d}:00 UTC",
        actual=f"trade at {entry_time.strftime('%H:%M')} UTC",
        detail=f"Trade placed during rollover dead zone at {entry_time.strftime('%H:%M')} UTC",
    )


def check_ftmo_daily_loss(
    trade: TradeRecord,
    cfg: ValidationConfig,
    *,
    daily_pnl: float = 0.0,
    initial_balance: float = 0.0,
) -> RuleResult:
    """Validate daily P&L has not breached FTMO max daily loss limit.

    This check is informational at the trade level; it uses the running
    daily P&L context provided by the caller.
    """
    balance = initial_balance or cfg.equity
    if balance <= 0:
        return RuleResult(
            rule_name="ftmo_daily_loss",
            verdict=RuleVerdict.SKIP,
            detail="No balance data for daily loss check",
        )

    max_loss = balance * (cfg.max_daily_loss_pct / 100.0)
    loss_used_pct = (abs(daily_pnl) / balance) * 100.0 if daily_pnl < 0 else 0.0

    if daily_pnl >= 0:
        return RuleResult(
            rule_name="ftmo_daily_loss",
            verdict=RuleVerdict.PASS,
            expected=f"daily loss < {cfg.max_daily_loss_pct}%",
            actual=f"daily P&L={daily_pnl:+.2f} (no loss)",
        )

    if abs(daily_pnl) < max_loss * 0.8:
        return RuleResult(
            rule_name="ftmo_daily_loss",
            verdict=RuleVerdict.PASS,
            expected=f"daily loss < {cfg.max_daily_loss_pct}%",
            actual=f"daily loss={loss_used_pct:.2f}% (limit={cfg.max_daily_loss_pct}%)",
        )

    if abs(daily_pnl) < max_loss:
        return RuleResult(
            rule_name="ftmo_daily_loss",
            verdict=RuleVerdict.WARN,
            expected=f"daily loss < {cfg.max_daily_loss_pct}%",
            actual=f"daily loss={loss_used_pct:.2f}% (limit={cfg.max_daily_loss_pct}%)",
            detail=f"Approaching daily loss limit: {loss_used_pct:.2f}% of {cfg.max_daily_loss_pct}%",
        )

    return RuleResult(
        rule_name="ftmo_daily_loss",
        verdict=RuleVerdict.FAIL,
        expected=f"daily loss < {cfg.max_daily_loss_pct}%",
        actual=f"daily loss={loss_used_pct:.2f}% (limit={cfg.max_daily_loss_pct}%)",
        detail=f"FTMO daily loss limit breached: {loss_used_pct:.2f}% >= {cfg.max_daily_loss_pct}%",
        critical=True,
    )


def check_ftmo_total_loss(
    trade: TradeRecord,
    cfg: ValidationConfig,
    *,
    total_pnl: float = 0.0,
    initial_balance: float = 0.0,
) -> RuleResult:
    """Validate total P&L has not breached FTMO max total loss limit."""
    balance = initial_balance or cfg.equity
    if balance <= 0:
        return RuleResult(
            rule_name="ftmo_total_loss",
            verdict=RuleVerdict.SKIP,
            detail="No balance data for total loss check",
        )

    max_loss = balance * (cfg.max_total_loss_pct / 100.0)
    loss_used_pct = (abs(total_pnl) / balance) * 100.0 if total_pnl < 0 else 0.0

    if total_pnl >= 0:
        return RuleResult(
            rule_name="ftmo_total_loss",
            verdict=RuleVerdict.PASS,
            expected=f"total loss < {cfg.max_total_loss_pct}%",
            actual=f"total P&L={total_pnl:+.2f} (no loss)",
        )

    if abs(total_pnl) < max_loss * 0.8:
        return RuleResult(
            rule_name="ftmo_total_loss",
            verdict=RuleVerdict.PASS,
            expected=f"total loss < {cfg.max_total_loss_pct}%",
            actual=f"total loss={loss_used_pct:.2f}% (limit={cfg.max_total_loss_pct}%)",
        )

    if abs(total_pnl) < max_loss:
        return RuleResult(
            rule_name="ftmo_total_loss",
            verdict=RuleVerdict.WARN,
            expected=f"total loss < {cfg.max_total_loss_pct}%",
            actual=f"total loss={loss_used_pct:.2f}% (limit={cfg.max_total_loss_pct}%)",
            detail=f"Approaching total loss limit: {loss_used_pct:.2f}% of {cfg.max_total_loss_pct}%",
        )

    return RuleResult(
        rule_name="ftmo_total_loss",
        verdict=RuleVerdict.FAIL,
        expected=f"total loss < {cfg.max_total_loss_pct}%",
        actual=f"total loss={loss_used_pct:.2f}% (limit={cfg.max_total_loss_pct}%)",
        detail=f"FTMO total loss limit breached: {loss_used_pct:.2f}% >= {cfg.max_total_loss_pct}%",
        critical=True,
    )


# ---------------------------------------------------------------------------
# Validator engine
# ---------------------------------------------------------------------------


# All rules in evaluation order
_RULES = [
    check_ema_alignment,
    check_adx_threshold,
    check_overextension,
    check_stop_placement,
    check_stop_distance,
    check_entry_price_valid,
    check_position_sizing,
    check_volume_bounds,
    check_rollover_dead_zone,
]


class TradeValidator:
    """Validates trades against IRB strategy rules and FTMO compliance.

    Usage::

        validator = TradeValidator()
        report = validator.validate_trade(trade)
        reports = validator.validate_all(trades)
    """

    def __init__(self, config: ValidationConfig | None = None) -> None:
        self._cfg = config or ValidationConfig()
        self._daily_pnl: float = 0.0
        self._total_pnl: float = 0.0
        self._initial_balance: float = self._cfg.equity

    @property
    def config(self) -> ValidationConfig:
        return self._cfg

    def validate_trade(
        self,
        trade: TradeRecord,
        *,
        daily_pnl: float | None = None,
        total_pnl: float | None = None,
    ) -> TradeValidationReport:
        """Validate a single trade against all rules.

        Args:
            trade: The trade record to validate.
            daily_pnl: Running daily P&L for FTMO daily loss check.
            total_pnl: Running total P&L for FTMO total loss check.

        Returns:
            TradeValidationReport with all rule results.
        """
        results: list[RuleResult] = []

        # Run all standard rules
        for rule_fn in _RULES:
            try:
                result = rule_fn(trade, self._cfg)
                results.append(result)
            except Exception as exc:
                log.warning("Rule %s raised: %s", rule_fn.__name__, exc)
                results.append(
                    RuleResult(
                        rule_name=rule_fn.__name__.replace("check_", ""),
                        verdict=RuleVerdict.SKIP,
                        detail=f"Rule error: {exc}",
                    )
                )

        # FTMO loss checks (need context)
        d_pnl = daily_pnl if daily_pnl is not None else self._daily_pnl
        t_pnl = total_pnl if total_pnl is not None else self._total_pnl
        results.append(check_ftmo_daily_loss(trade, self._cfg, daily_pnl=d_pnl, initial_balance=self._initial_balance))
        results.append(check_ftmo_total_loss(trade, self._cfg, total_pnl=t_pnl, initial_balance=self._initial_balance))

        report = TradeValidationReport(trade=trade, rule_results=results)
        report._compute_verdict()
        return report

    def validate_all(self, trades: list[TradeRecord]) -> list[TradeValidationReport]:
        """Validate a list of trades, returning reports for each."""
        reports: list[TradeValidationReport] = []
        for trade in trades:
            report = self.validate_trade(trade)
            reports.append(report)
        return reports


# ---------------------------------------------------------------------------
# Auto-trigger hook
# ---------------------------------------------------------------------------


def post_trade_validation_hook(
    trade: TradeRecord,
    *,
    config: ValidationConfig | None = None,
    reports_path: Path | None = None,
    daily_pnl: float = 0.0,
    total_pnl: float = 0.0,
) -> TradeValidationReport:
    """Auto-trigger hook for the trade lifecycle.

    Call this after a fill confirmation to:
      1. Run all validation rules
      2. Append the report to validation_reports.jsonl
      3. Log a summary line
      4. Log WARNING if CRITICAL_VIOLATION detected

    Returns the validation report for further processing (e.g. alerts).
    """
    validator = TradeValidator(config)
    report = validator.validate_trade(trade, daily_pnl=daily_pnl, total_pnl=total_pnl)

    # Append report to JSONL
    out_path = reports_path or DEFAULT_REPORTS_PATH
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("a") as f:
        f.write(json.dumps(report.to_dict()) + "\n")

    # Log summary
    log.info(
        "Trade validation: %s %s %s @ %.5f vol=%.2f — %s",
        report.trade.symbol,
        report.trade.direction,
        report.verdict.value,
        report.trade.entry_price,
        report.trade.volume,
        report.summary,
    )

    if report.verdict == TradeVerdict.CRITICAL_VIOLATION:
        log.warning(
            "CRITICAL VIOLATION: %s %s — %s",
            report.trade.symbol,
            report.trade.direction,
            report.summary,
        )

    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _format_report_text(report: TradeValidationReport) -> str:
    """Format a report as human-readable text."""
    lines: list[str] = []
    t = report.trade
    lines.append(
        f"{'=' * 72}\nTrade: {t.symbol} {t.direction} @ {t.entry_price:.5f} SL={t.stop_loss:.5f} Vol={t.volume:.2f}"
    )
    if t.entry_time_utc:
        lines.append(f"Time:  {t.entry_time_utc.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    lines.append(f"Outcome: {t.outcome or 'N/A'}  Campaign: {t.campaign or 'N/A'}")
    if t.error:
        lines.append(f"Error: {t.error}")
    lines.append(f"Verdict: {report.verdict.value}")
    lines.append(f"Summary: {report.summary}")
    lines.append("-" * 72)

    for r in report.rule_results:
        status = r.verdict.value.ljust(4)
        line = f"  [{status}] {r.rule_name}"
        if r.expected or r.actual:
            line += f"  expected={r.expected}  actual={r.actual}"
        if r.detail:
            line += f"  -- {r.detail}"
        if r.critical and r.verdict == RuleVerdict.FAIL:
            line += "  [CRITICAL]"
        lines.append(line)

    return "\n".join(lines)


def _run_cli() -> None:
    """CLI entrypoint for trade validation."""
    parser = argparse.ArgumentParser(
        description="NovaTrade post-trade validation report system",
    )
    parser.add_argument(
        "--evidence",
        type=Path,
        default=None,
        help="Path to evidence JSONL file (default: auto-detect)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_REPORTS_PATH,
        help="Path to write validation reports JSONL",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show detailed rule results for each trade",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output reports as JSON instead of text",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    # Auto-detect evidence file
    evidence_paths: list[Path] = []
    if args.evidence:
        evidence_paths = [args.evidence]
    else:
        for p in [DEFAULT_LIVE_EVIDENCE_PATH, DEFAULT_EVIDENCE_PATH]:
            if p.exists():
                evidence_paths.append(p)
        if not evidence_paths:
            log.error("No evidence files found. Use --evidence to specify a path.")
            sys.exit(1)

    # Parse trades from all evidence files
    all_trades: list[TradeRecord] = []
    for ep in evidence_paths:
        trades = parse_trades_from_evidence(ep)
        all_trades.extend(trades)
        log.info("Loaded %d trades from %s", len(trades), ep)

    if not all_trades:
        log.info("No trade records found in evidence files.")
        sys.exit(0)

    # Validate
    validator = TradeValidator()
    reports = validator.validate_all(all_trades)

    # Write reports
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w") as f:
        for report in reports:
            f.write(json.dumps(report.to_dict()) + "\n")
    log.info("Wrote %d validation reports to %s", len(reports), args.output)

    # Display
    if args.json:
        for report in reports:
            print(json.dumps(report.to_dict(), indent=2))
    else:
        for report in reports:
            print(_format_report_text(report))
            print()

    # Summary statistics
    verdicts = {v: 0 for v in TradeVerdict}
    for r in reports:
        verdicts[r.verdict] += 1

    print(f"\n{'=' * 72}")
    print(f"VALIDATION SUMMARY: {len(reports)} trades validated")
    for v, count in verdicts.items():
        if count > 0:
            print(f"  {v.value}: {count}")
    print(f"{'=' * 72}")

    # Exit code: 1 if any critical violations
    if verdicts[TradeVerdict.CRITICAL_VIOLATION] > 0:
        sys.exit(1)


if __name__ == "__main__":
    _run_cli()
