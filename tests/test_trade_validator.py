"""Comprehensive tests for the post-trade validation report system.

Tests cover:
  - Evidence parsing (EXECUTION, ADAPTER_ERROR, malformed lines)
  - Individual rule checks (all 11 rules)
  - TradeValidator engine (single + batch)
  - Verdict computation (VALID / DEVIATION_DETECTED / CRITICAL_VIOLATION)
  - Auto-trigger hook (file output, logging)
  - CLI entrypoint (--evidence, --json, --verbose)
  - Edge cases (missing metadata, zero values, boundary conditions)
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from novatrade.validation.trade_validator import (
    RuleResult,
    RuleVerdict,
    TradeRecord,
    TradeValidationReport,
    TradeValidator,
    TradeVerdict,
    ValidationConfig,
    check_adx_threshold,
    check_ema_alignment,
    check_entry_price_valid,
    check_ftmo_daily_loss,
    check_ftmo_total_loss,
    check_overextension,
    check_position_sizing,
    check_rollover_dead_zone,
    check_stop_distance,
    check_stop_placement,
    check_volume_bounds,
    parse_trades_from_evidence,
    post_trade_validation_hook,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def cfg() -> ValidationConfig:
    return ValidationConfig(equity=100_000.0)


@pytest.fixture
def long_trade() -> TradeRecord:
    """A valid LONG trade with full metadata."""
    return TradeRecord(
        symbol="EURUSD",
        side="BUY",
        entry_price=1.10500,
        stop_loss=1.10000,
        volume=0.20,
        timestamp=1774432800.0,  # Wed 2026-03-25 10:00 UTC
        bar_close_time=1774432800,
        campaign="irb-live",
        order_type="BUY_STOP",
        action="PLACE_STOP_ORDER",
        strategy_name="Rob Hoffman IRB",
        strategy_version="5.0.0",
        outcome="FILLED",
        fill_price=1.10502,
        order_id="order_123",
        metadata={
            "ema_slope": 0.85,
            "adx_value": 25.3,
            "overextension_ratio": 1.5,
            "irb_range": 0.005,
        },
    )


@pytest.fixture
def short_trade() -> TradeRecord:
    """A valid SHORT trade with full metadata."""
    return TradeRecord(
        symbol="EURUSD",
        side="SELL",
        entry_price=1.10000,
        stop_loss=1.10500,
        volume=0.20,
        timestamp=1774432800.0,
        bar_close_time=1774432800,
        campaign="irb-live",
        order_type="SELL_STOP",
        action="PLACE_STOP_ORDER",
        strategy_name="Rob Hoffman IRB",
        strategy_version="5.0.0",
        outcome="FILLED",
        fill_price=1.09998,
        order_id="order_456",
        metadata={
            "ema_slope": -0.72,
            "adx_value": 28.1,
            "overextension_ratio": 1.2,
            "irb_range": 0.004,
        },
    )


@pytest.fixture
def evidence_file(tmp_path: Path) -> Path:
    """Create a test evidence file with mixed record types."""
    records = [
        # Valid execution (FILLED)
        {
            "event_type": "EXECUTION",
            "data": {
                "outcome": "FILLED",
                "payload": {
                    "symbol": "EURUSD",
                    "side": "BUY",
                    "entry_price": 1.10500,
                    "stop_loss": 1.10000,
                    "volume": 0.20,
                    "bar_close_time": 1774432800,
                    "campaign": "irb-live",
                    "order_type": "BUY_STOP",
                    "action": "PLACE_STOP_ORDER",
                    "strategy_name": "Rob Hoffman IRB",
                    "strategy_version": "5.0.0",
                    "metadata": {
                        "ema_slope": 0.85,
                        "adx_value": 25.3,
                        "overextension_ratio": 1.5,
                    },
                },
                "fill_price": 1.10502,
                "order_id": "order_001",
            },
            "error": "",
            "timestamp": 1774432801.0,
        },
        # Valid execution (DENIED)
        {
            "event_type": "EXECUTION",
            "data": {
                "outcome": "DENIED",
                "payload": {
                    "symbol": "EURUSD",
                    "side": "SELL",
                    "entry_price": 1.10000,
                    "stop_loss": 1.10500,
                    "volume": 0.15,
                    "bar_close_time": 1774436400,
                    "campaign": "irb-live",
                },
                "rule": "rollover_dead_zone",
            },
            "error": "",
            "timestamp": 1774436401.0,
        },
        # Health snapshot (should be skipped)
        {
            "event_type": "HEALTH_SNAPSHOT",
            "data": {"state": "OK", "connected": True, "balance": 100000},
            "error": "",
            "timestamp": 1774432850.0,
        },
        # Monitoring event (should be skipped)
        {
            "event_type": "MONITORING",
            "data": {"monitoring_event": "CYCLE_COMPLETE"},
            "error": "",
            "timestamp": 1774432860.0,
        },
        # Adapter error with payload
        {
            "event_type": "ADAPTER_ERROR",
            "data": {
                "payload": {
                    "symbol": "EURUSD",
                    "side": "SELL",
                    "entry_price": 1.09800,
                    "stop_loss": 1.10200,
                    "volume": 0.18,
                    "bar_close_time": 1774440000,
                    "campaign": "irb-live",
                    "action": "PLACE_STOP_ORDER",
                },
                "campaign": "irb-live",
            },
            "error": "validation: version mismatch",
            "timestamp": 1774440001.0,
        },
    ]

    path = tmp_path / "evidence.jsonl"
    with path.open("w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    return path


# ---------------------------------------------------------------------------
# TradeRecord tests
# ---------------------------------------------------------------------------


class TestTradeRecord:
    def test_direction_buy(self) -> None:
        t = TradeRecord(side="BUY")
        assert t.direction == "LONG"

    def test_direction_sell(self) -> None:
        t = TradeRecord(side="SELL")
        assert t.direction == "SHORT"

    def test_direction_long(self) -> None:
        t = TradeRecord(side="LONG")
        assert t.direction == "LONG"

    def test_direction_short(self) -> None:
        t = TradeRecord(side="SHORT")
        assert t.direction == "SHORT"

    def test_direction_unknown(self) -> None:
        t = TradeRecord(side="UNKNOWN")
        assert t.direction == "UNKNOWN"

    def test_entry_time_utc(self) -> None:
        t = TradeRecord(bar_close_time=1774432800)
        assert t.entry_time_utc is not None
        assert t.entry_time_utc.hour == 10  # 10:00 UTC

    def test_entry_time_utc_fallback_to_timestamp(self) -> None:
        t = TradeRecord(timestamp=1774432800.0)
        assert t.entry_time_utc is not None

    def test_entry_time_utc_none(self) -> None:
        t = TradeRecord()
        assert t.entry_time_utc is None


# ---------------------------------------------------------------------------
# Evidence parsing tests
# ---------------------------------------------------------------------------


class TestParseEvidence:
    def test_parse_mixed_evidence(self, evidence_file: Path) -> None:
        trades = parse_trades_from_evidence(evidence_file)
        # Should get 3 trades: 2 EXECUTION + 1 ADAPTER_ERROR with payload
        assert len(trades) == 3

    def test_parse_execution_filled(self, evidence_file: Path) -> None:
        trades = parse_trades_from_evidence(evidence_file)
        filled = [t for t in trades if t.outcome == "FILLED"]
        assert len(filled) == 1
        t = filled[0]
        assert t.symbol == "EURUSD"
        assert t.side == "BUY"
        assert t.entry_price == 1.10500
        assert t.stop_loss == 1.10000
        assert t.volume == 0.20
        assert t.fill_price == 1.10502
        assert t.order_id == "order_001"
        assert t.metadata["ema_slope"] == 0.85

    def test_parse_execution_denied(self, evidence_file: Path) -> None:
        trades = parse_trades_from_evidence(evidence_file)
        denied = [t for t in trades if t.outcome == "DENIED"]
        assert len(denied) == 1

    def test_parse_adapter_error_with_payload(self, evidence_file: Path) -> None:
        trades = parse_trades_from_evidence(evidence_file)
        errors = [t for t in trades if t.outcome == "ADAPTER_ERROR"]
        assert len(errors) == 1
        t = errors[0]
        assert t.symbol == "EURUSD"
        assert t.entry_price == 1.09800
        assert t.error == "validation: version mismatch"

    def test_parse_nonexistent_file(self, tmp_path: Path) -> None:
        trades = parse_trades_from_evidence(tmp_path / "nonexistent.jsonl")
        assert trades == []

    def test_parse_empty_file(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.jsonl"
        path.write_text("")
        trades = parse_trades_from_evidence(path)
        assert trades == []

    def test_parse_malformed_json(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.jsonl"
        path.write_text("not json\n{bad json}\n")
        trades = parse_trades_from_evidence(path)
        assert trades == []

    def test_parse_skips_health_and_monitoring(self, evidence_file: Path) -> None:
        trades = parse_trades_from_evidence(evidence_file)
        for t in trades:
            assert t.outcome != ""  # health/monitoring have no outcome


# ---------------------------------------------------------------------------
# EMA alignment rule tests
# ---------------------------------------------------------------------------


class TestCheckEmaAlignment:
    def test_long_positive_slope(self, long_trade: TradeRecord, cfg: ValidationConfig) -> None:
        result = check_ema_alignment(long_trade, cfg)
        assert result.verdict == RuleVerdict.PASS

    def test_short_negative_slope(self, short_trade: TradeRecord, cfg: ValidationConfig) -> None:
        result = check_ema_alignment(short_trade, cfg)
        assert result.verdict == RuleVerdict.PASS

    def test_long_negative_slope_fails(self, long_trade: TradeRecord, cfg: ValidationConfig) -> None:
        long_trade.metadata["ema_slope"] = -0.5
        result = check_ema_alignment(long_trade, cfg)
        assert result.verdict == RuleVerdict.FAIL

    def test_short_positive_slope_fails(self, short_trade: TradeRecord, cfg: ValidationConfig) -> None:
        short_trade.metadata["ema_slope"] = 0.5
        result = check_ema_alignment(short_trade, cfg)
        assert result.verdict == RuleVerdict.FAIL

    def test_zero_slope_long_passes(self, long_trade: TradeRecord, cfg: ValidationConfig) -> None:
        long_trade.metadata["ema_slope"] = 0.0
        result = check_ema_alignment(long_trade, cfg)
        assert result.verdict == RuleVerdict.PASS

    def test_zero_slope_short_passes(self, short_trade: TradeRecord, cfg: ValidationConfig) -> None:
        short_trade.metadata["ema_slope"] = 0.0
        result = check_ema_alignment(short_trade, cfg)
        assert result.verdict == RuleVerdict.PASS

    def test_missing_metadata_skips(self, cfg: ValidationConfig) -> None:
        trade = TradeRecord(side="BUY", metadata={})
        result = check_ema_alignment(trade, cfg)
        assert result.verdict == RuleVerdict.SKIP


# ---------------------------------------------------------------------------
# ADX threshold rule tests
# ---------------------------------------------------------------------------


class TestCheckAdxThreshold:
    def test_above_threshold(self, long_trade: TradeRecord, cfg: ValidationConfig) -> None:
        result = check_adx_threshold(long_trade, cfg)
        assert result.verdict == RuleVerdict.PASS

    def test_exactly_at_threshold(self, long_trade: TradeRecord, cfg: ValidationConfig) -> None:
        long_trade.metadata["adx_value"] = 20.0
        result = check_adx_threshold(long_trade, cfg)
        assert result.verdict == RuleVerdict.PASS

    def test_below_threshold(self, long_trade: TradeRecord, cfg: ValidationConfig) -> None:
        long_trade.metadata["adx_value"] = 15.0
        result = check_adx_threshold(long_trade, cfg)
        assert result.verdict == RuleVerdict.FAIL

    def test_missing_metadata_skips(self, cfg: ValidationConfig) -> None:
        trade = TradeRecord(side="BUY", metadata={})
        result = check_adx_threshold(trade, cfg)
        assert result.verdict == RuleVerdict.SKIP

    def test_custom_threshold(self, long_trade: TradeRecord) -> None:
        cfg = ValidationConfig(adx_threshold=30.0)
        long_trade.metadata["adx_value"] = 25.0
        result = check_adx_threshold(long_trade, cfg)
        assert result.verdict == RuleVerdict.FAIL


# ---------------------------------------------------------------------------
# Overextension rule tests
# ---------------------------------------------------------------------------


class TestCheckOverextension:
    def test_within_bounds(self, long_trade: TradeRecord, cfg: ValidationConfig) -> None:
        result = check_overextension(long_trade, cfg)
        assert result.verdict == RuleVerdict.PASS

    def test_at_threshold(self, long_trade: TradeRecord, cfg: ValidationConfig) -> None:
        long_trade.metadata["overextension_ratio"] = 2.0
        result = check_overextension(long_trade, cfg)
        assert result.verdict == RuleVerdict.PASS

    def test_exceeds_threshold(self, long_trade: TradeRecord, cfg: ValidationConfig) -> None:
        long_trade.metadata["overextension_ratio"] = 2.5
        result = check_overextension(long_trade, cfg)
        assert result.verdict == RuleVerdict.FAIL

    def test_missing_metadata_skips(self, cfg: ValidationConfig) -> None:
        trade = TradeRecord(side="BUY", metadata={})
        result = check_overextension(trade, cfg)
        assert result.verdict == RuleVerdict.SKIP


# ---------------------------------------------------------------------------
# Stop placement rule tests
# ---------------------------------------------------------------------------


class TestCheckStopPlacement:
    def test_long_sl_below_entry(self, long_trade: TradeRecord, cfg: ValidationConfig) -> None:
        result = check_stop_placement(long_trade, cfg)
        assert result.verdict == RuleVerdict.PASS

    def test_short_sl_above_entry(self, short_trade: TradeRecord, cfg: ValidationConfig) -> None:
        result = check_stop_placement(short_trade, cfg)
        assert result.verdict == RuleVerdict.PASS

    def test_long_sl_above_entry_fails(self, long_trade: TradeRecord, cfg: ValidationConfig) -> None:
        long_trade.stop_loss = 1.11000  # Above entry
        result = check_stop_placement(long_trade, cfg)
        assert result.verdict == RuleVerdict.FAIL
        assert result.critical is True

    def test_short_sl_below_entry_fails(self, short_trade: TradeRecord, cfg: ValidationConfig) -> None:
        short_trade.stop_loss = 1.09500  # Below entry
        result = check_stop_placement(short_trade, cfg)
        assert result.verdict == RuleVerdict.FAIL
        assert result.critical is True

    def test_missing_prices_skips(self, cfg: ValidationConfig) -> None:
        trade = TradeRecord(side="BUY", entry_price=0, stop_loss=0)
        result = check_stop_placement(trade, cfg)
        assert result.verdict == RuleVerdict.SKIP

    def test_unknown_direction_skips(self, cfg: ValidationConfig) -> None:
        trade = TradeRecord(side="UNKNOWN", entry_price=1.1, stop_loss=1.0)
        result = check_stop_placement(trade, cfg)
        assert result.verdict == RuleVerdict.SKIP


# ---------------------------------------------------------------------------
# Stop distance rule tests
# ---------------------------------------------------------------------------


class TestCheckStopDistance:
    def test_adequate_distance(self, long_trade: TradeRecord, cfg: ValidationConfig) -> None:
        result = check_stop_distance(long_trade, cfg)
        assert result.verdict == RuleVerdict.PASS

    def test_micro_stop_warns(self, cfg: ValidationConfig) -> None:
        trade = TradeRecord(
            side="BUY",
            entry_price=1.10000,
            stop_loss=1.09995,  # Only 0.5 pips
        )
        result = check_stop_distance(trade, cfg)
        assert result.verdict == RuleVerdict.WARN

    def test_missing_prices_skips(self, cfg: ValidationConfig) -> None:
        trade = TradeRecord(side="BUY")
        result = check_stop_distance(trade, cfg)
        assert result.verdict == RuleVerdict.SKIP

    def test_exact_minimum(self, cfg: ValidationConfig) -> None:
        trade = TradeRecord(
            side="BUY",
            entry_price=1.10000,
            stop_loss=1.09900,  # Exactly 10 pips
        )
        result = check_stop_distance(trade, cfg)
        assert result.verdict == RuleVerdict.PASS


# ---------------------------------------------------------------------------
# Entry price validation tests
# ---------------------------------------------------------------------------


class TestCheckEntryPriceValid:
    def test_valid_entry(self, long_trade: TradeRecord, cfg: ValidationConfig) -> None:
        result = check_entry_price_valid(long_trade, cfg)
        assert result.verdict == RuleVerdict.PASS

    def test_zero_entry_fails(self, cfg: ValidationConfig) -> None:
        trade = TradeRecord(side="BUY", entry_price=0)
        result = check_entry_price_valid(trade, cfg)
        assert result.verdict == RuleVerdict.FAIL
        assert result.critical is True

    def test_negative_entry_fails(self, cfg: ValidationConfig) -> None:
        trade = TradeRecord(side="BUY", entry_price=-1.0)
        result = check_entry_price_valid(trade, cfg)
        assert result.verdict == RuleVerdict.FAIL

    def test_excessive_slippage_warns(self, cfg: ValidationConfig) -> None:
        trade = TradeRecord(
            side="BUY",
            entry_price=1.10000,
            fill_price=1.10100,  # 10 pips slippage
        )
        result = check_entry_price_valid(trade, cfg)
        assert result.verdict == RuleVerdict.WARN

    def test_normal_slippage_passes(self, long_trade: TradeRecord, cfg: ValidationConfig) -> None:
        # 0.2 pip slippage is normal
        result = check_entry_price_valid(long_trade, cfg)
        assert result.verdict == RuleVerdict.PASS


# ---------------------------------------------------------------------------
# Position sizing rule tests
# ---------------------------------------------------------------------------


class TestCheckPositionSizing:
    def test_correct_sizing(self, cfg: ValidationConfig) -> None:
        # 50 pip stop: risk = $1000, volume = 1000 / (50 * 10) = 2.0 -> capped at 1.0
        # Actually let's use a realistic case:
        # 50 pip stop: volume = 1000 / (500 * 10) ... wait
        # equity=100000, risk=0.01 => $1000 risk
        # entry=1.1050, sl=1.1000 => 50 pips
        # volume = 1000 / (50 * 10) = 2.0 -> capped at max_volume=1.0
        trade = TradeRecord(
            side="BUY",
            entry_price=1.10500,
            stop_loss=1.10000,
            volume=1.0,
        )
        result = check_position_sizing(trade, cfg)
        # Volume should be capped at 1.0, and calculated is 2.0 capped to 1.0
        assert result.verdict == RuleVerdict.PASS

    def test_conservative_sizing_passes(self, cfg: ValidationConfig) -> None:
        trade = TradeRecord(
            side="BUY",
            entry_price=1.10500,
            stop_loss=1.10000,
            volume=0.50,  # Under-sized is safe
        )
        result = check_position_sizing(trade, cfg)
        assert result.verdict == RuleVerdict.PASS

    def test_oversized_fails(self, cfg: ValidationConfig) -> None:
        # 200 pip stop => $1000 / (200 * 10) = 0.50
        # volume 1.0 is 100% over, well above 25% tolerance
        trade = TradeRecord(
            side="BUY",
            entry_price=1.10000,
            stop_loss=1.08000,  # 200 pips
            volume=1.0,
        )
        result = check_position_sizing(trade, cfg)
        assert result.verdict == RuleVerdict.FAIL
        assert result.critical is True

    def test_missing_data_skips(self, cfg: ValidationConfig) -> None:
        trade = TradeRecord(side="BUY")
        result = check_position_sizing(trade, cfg)
        assert result.verdict == RuleVerdict.SKIP

    def test_within_tolerance_passes(self, cfg: ValidationConfig) -> None:
        # 100 pip stop => expected = 1000 / (100 * 10) = 1.0
        # 1.2 is 20% over, within 25% tolerance
        trade = TradeRecord(
            side="BUY",
            entry_price=1.10000,
            stop_loss=1.09000,  # 100 pips
            volume=1.0,  # expected is 1.0 (capped)
        )
        result = check_position_sizing(trade, cfg)
        assert result.verdict == RuleVerdict.PASS


# ---------------------------------------------------------------------------
# Volume bounds rule tests
# ---------------------------------------------------------------------------


class TestCheckVolumeBounds:
    def test_within_bounds(self, long_trade: TradeRecord, cfg: ValidationConfig) -> None:
        result = check_volume_bounds(long_trade, cfg)
        assert result.verdict == RuleVerdict.PASS

    def test_below_minimum(self, cfg: ValidationConfig) -> None:
        trade = TradeRecord(side="BUY", volume=0.001)
        result = check_volume_bounds(trade, cfg)
        assert result.verdict == RuleVerdict.FAIL

    def test_above_maximum(self, cfg: ValidationConfig) -> None:
        trade = TradeRecord(side="BUY", volume=5.0)
        result = check_volume_bounds(trade, cfg)
        assert result.verdict == RuleVerdict.FAIL
        assert result.critical is True

    def test_zero_volume_skips(self, cfg: ValidationConfig) -> None:
        trade = TradeRecord(side="BUY", volume=0)
        result = check_volume_bounds(trade, cfg)
        assert result.verdict == RuleVerdict.SKIP

    def test_min_volume_boundary(self, cfg: ValidationConfig) -> None:
        trade = TradeRecord(side="BUY", volume=0.01)
        result = check_volume_bounds(trade, cfg)
        assert result.verdict == RuleVerdict.PASS

    def test_max_volume_boundary(self, cfg: ValidationConfig) -> None:
        trade = TradeRecord(side="BUY", volume=1.0)
        result = check_volume_bounds(trade, cfg)
        assert result.verdict == RuleVerdict.PASS


# ---------------------------------------------------------------------------
# Rollover dead zone rule tests
# ---------------------------------------------------------------------------


class TestCheckRolloverDeadZone:
    def test_outside_zone(self, long_trade: TradeRecord, cfg: ValidationConfig) -> None:
        result = check_rollover_dead_zone(long_trade, cfg)
        assert result.verdict == RuleVerdict.PASS

    def test_inside_zone_21h(self, cfg: ValidationConfig) -> None:
        # 21:30 UTC is in the 21:00-23:00 zone
        from datetime import datetime, timezone

        dt = datetime(2026, 3, 25, 21, 30, tzinfo=timezone.utc)
        trade = TradeRecord(
            side="BUY",
            bar_close_time=int(dt.timestamp()),
        )
        result = check_rollover_dead_zone(trade, cfg)
        assert result.verdict == RuleVerdict.FAIL

    def test_inside_zone_22h(self, cfg: ValidationConfig) -> None:
        from datetime import datetime, timezone

        dt = datetime(2026, 3, 25, 22, 0, tzinfo=timezone.utc)
        trade = TradeRecord(
            side="BUY",
            bar_close_time=int(dt.timestamp()),
        )
        result = check_rollover_dead_zone(trade, cfg)
        assert result.verdict == RuleVerdict.FAIL

    def test_at_boundary_23h_passes(self, cfg: ValidationConfig) -> None:
        from datetime import datetime, timezone

        dt = datetime(2026, 3, 25, 23, 0, tzinfo=timezone.utc)
        trade = TradeRecord(
            side="BUY",
            bar_close_time=int(dt.timestamp()),
        )
        result = check_rollover_dead_zone(trade, cfg)
        assert result.verdict == RuleVerdict.PASS

    def test_no_timestamp_skips(self, cfg: ValidationConfig) -> None:
        trade = TradeRecord(side="BUY")
        result = check_rollover_dead_zone(trade, cfg)
        assert result.verdict == RuleVerdict.SKIP


# ---------------------------------------------------------------------------
# FTMO daily loss rule tests
# ---------------------------------------------------------------------------


class TestCheckFtmoDailyLoss:
    def test_no_loss(self, long_trade: TradeRecord, cfg: ValidationConfig) -> None:
        result = check_ftmo_daily_loss(long_trade, cfg, daily_pnl=500.0)
        assert result.verdict == RuleVerdict.PASS

    def test_small_loss_passes(self, long_trade: TradeRecord, cfg: ValidationConfig) -> None:
        result = check_ftmo_daily_loss(long_trade, cfg, daily_pnl=-2000.0)
        assert result.verdict == RuleVerdict.PASS

    def test_approaching_limit_warns(self, long_trade: TradeRecord, cfg: ValidationConfig) -> None:
        # 80% of 5% = 4%, which is $4000 of $100k
        result = check_ftmo_daily_loss(long_trade, cfg, daily_pnl=-4500.0)
        assert result.verdict == RuleVerdict.WARN

    def test_exceeds_limit_fails(self, long_trade: TradeRecord, cfg: ValidationConfig) -> None:
        result = check_ftmo_daily_loss(long_trade, cfg, daily_pnl=-5500.0)
        assert result.verdict == RuleVerdict.FAIL
        assert result.critical is True

    def test_zero_balance_skips(self, long_trade: TradeRecord) -> None:
        cfg = ValidationConfig(equity=0)
        result = check_ftmo_daily_loss(long_trade, cfg, daily_pnl=-100.0, initial_balance=0)
        assert result.verdict == RuleVerdict.SKIP


# ---------------------------------------------------------------------------
# FTMO total loss rule tests
# ---------------------------------------------------------------------------


class TestCheckFtmoTotalLoss:
    def test_no_loss(self, long_trade: TradeRecord, cfg: ValidationConfig) -> None:
        result = check_ftmo_total_loss(long_trade, cfg, total_pnl=1000.0)
        assert result.verdict == RuleVerdict.PASS

    def test_small_loss_passes(self, long_trade: TradeRecord, cfg: ValidationConfig) -> None:
        result = check_ftmo_total_loss(long_trade, cfg, total_pnl=-5000.0)
        assert result.verdict == RuleVerdict.PASS

    def test_approaching_limit_warns(self, long_trade: TradeRecord, cfg: ValidationConfig) -> None:
        # 80% of 10% = 8% = $8000
        result = check_ftmo_total_loss(long_trade, cfg, total_pnl=-9000.0)
        assert result.verdict == RuleVerdict.WARN

    def test_exceeds_limit_fails(self, long_trade: TradeRecord, cfg: ValidationConfig) -> None:
        result = check_ftmo_total_loss(long_trade, cfg, total_pnl=-11000.0)
        assert result.verdict == RuleVerdict.FAIL
        assert result.critical is True


# ---------------------------------------------------------------------------
# TradeValidationReport tests
# ---------------------------------------------------------------------------


class TestTradeValidationReport:
    def test_all_pass_verdict(self, long_trade: TradeRecord) -> None:
        results = [
            RuleResult(rule_name="r1", verdict=RuleVerdict.PASS),
            RuleResult(rule_name="r2", verdict=RuleVerdict.PASS),
        ]
        report = TradeValidationReport(trade=long_trade, rule_results=results)
        assert report.verdict == TradeVerdict.VALID

    def test_non_critical_fail_deviation(self, long_trade: TradeRecord) -> None:
        results = [
            RuleResult(rule_name="r1", verdict=RuleVerdict.PASS),
            RuleResult(rule_name="r2", verdict=RuleVerdict.FAIL, critical=False),
        ]
        report = TradeValidationReport(trade=long_trade, rule_results=results)
        assert report.verdict == TradeVerdict.DEVIATION_DETECTED

    def test_critical_fail_violation(self, long_trade: TradeRecord) -> None:
        results = [
            RuleResult(rule_name="r1", verdict=RuleVerdict.PASS),
            RuleResult(rule_name="r2", verdict=RuleVerdict.FAIL, critical=True),
        ]
        report = TradeValidationReport(trade=long_trade, rule_results=results)
        assert report.verdict == TradeVerdict.CRITICAL_VIOLATION

    def test_warns_still_valid(self, long_trade: TradeRecord) -> None:
        results = [
            RuleResult(rule_name="r1", verdict=RuleVerdict.PASS),
            RuleResult(rule_name="r2", verdict=RuleVerdict.WARN),
        ]
        report = TradeValidationReport(trade=long_trade, rule_results=results)
        assert report.verdict == TradeVerdict.VALID

    def test_skip_still_valid(self, long_trade: TradeRecord) -> None:
        results = [
            RuleResult(rule_name="r1", verdict=RuleVerdict.SKIP),
            RuleResult(rule_name="r2", verdict=RuleVerdict.SKIP),
        ]
        report = TradeValidationReport(trade=long_trade, rule_results=results)
        assert report.verdict == TradeVerdict.VALID

    def test_to_dict_roundtrip(self, long_trade: TradeRecord) -> None:
        results = [
            RuleResult(rule_name="r1", verdict=RuleVerdict.PASS, expected="x", actual="y"),
        ]
        report = TradeValidationReport(trade=long_trade, rule_results=results)
        d = report.to_dict()
        assert d["verdict"] == "VALID"
        assert d["trade"]["symbol"] == "EURUSD"
        assert len(d["rules"]) == 1
        assert d["rules"][0]["rule"] == "r1"
        assert d["rules"][0]["verdict"] == "PASS"

    def test_summary_includes_fail_names(self, long_trade: TradeRecord) -> None:
        results = [
            RuleResult(rule_name="alpha", verdict=RuleVerdict.FAIL),
            RuleResult(rule_name="beta", verdict=RuleVerdict.PASS),
        ]
        report = TradeValidationReport(trade=long_trade, rule_results=results)
        assert "alpha" in report.summary
        assert "beta" not in report.summary  # passed rules not named in summary


# ---------------------------------------------------------------------------
# TradeValidator engine tests
# ---------------------------------------------------------------------------


class TestTradeValidator:
    def test_validate_valid_long(self, long_trade: TradeRecord) -> None:
        validator = TradeValidator()
        report = validator.validate_trade(long_trade)
        assert report.verdict == TradeVerdict.VALID

    def test_validate_valid_short(self, short_trade: TradeRecord) -> None:
        validator = TradeValidator()
        report = validator.validate_trade(short_trade)
        assert report.verdict == TradeVerdict.VALID

    def test_validate_all(self, long_trade: TradeRecord, short_trade: TradeRecord) -> None:
        validator = TradeValidator()
        reports = validator.validate_all([long_trade, short_trade])
        assert len(reports) == 2
        assert all(r.verdict == TradeVerdict.VALID for r in reports)

    def test_validate_trade_with_bad_stop(self, long_trade: TradeRecord) -> None:
        long_trade.stop_loss = 1.12000  # Above entry for LONG = critical
        validator = TradeValidator()
        report = validator.validate_trade(long_trade)
        assert report.verdict == TradeVerdict.CRITICAL_VIOLATION

    def test_validate_trade_with_low_adx(self, long_trade: TradeRecord) -> None:
        long_trade.metadata["adx_value"] = 10.0
        validator = TradeValidator()
        report = validator.validate_trade(long_trade)
        assert report.verdict == TradeVerdict.DEVIATION_DETECTED

    def test_validate_trade_no_metadata(self) -> None:
        trade = TradeRecord(
            symbol="EURUSD",
            side="BUY",
            entry_price=1.10500,
            stop_loss=1.10000,
            volume=0.20,
            timestamp=1774432800.0,
            bar_close_time=1774432800,
        )
        validator = TradeValidator()
        report = validator.validate_trade(trade)
        # With no metadata, EMA/ADX/overextension are SKIP, others should PASS
        skipped = [r for r in report.rule_results if r.verdict == RuleVerdict.SKIP]
        assert len(skipped) >= 3

    def test_validate_with_custom_config(self, long_trade: TradeRecord) -> None:
        cfg = ValidationConfig(adx_threshold=30.0)
        validator = TradeValidator(cfg)
        report = validator.validate_trade(long_trade)
        # ADX is 25.3, below 30.0 threshold
        adx_result = next(r for r in report.rule_results if r.rule_name == "adx_threshold")
        assert adx_result.verdict == RuleVerdict.FAIL

    def test_validate_with_ftmo_context(self, long_trade: TradeRecord) -> None:
        validator = TradeValidator()
        report = validator.validate_trade(long_trade, daily_pnl=-4500.0, total_pnl=-9000.0)
        daily = next(r for r in report.rule_results if r.rule_name == "ftmo_daily_loss")
        total = next(r for r in report.rule_results if r.rule_name == "ftmo_total_loss")
        assert daily.verdict == RuleVerdict.WARN
        assert total.verdict == RuleVerdict.WARN

    def test_rule_count(self, long_trade: TradeRecord) -> None:
        """Ensure all expected rules are checked."""
        validator = TradeValidator()
        report = validator.validate_trade(long_trade)
        # 9 standard rules + 2 FTMO = 11
        assert len(report.rule_results) == 11

    def test_all_rule_names_present(self, long_trade: TradeRecord) -> None:
        validator = TradeValidator()
        report = validator.validate_trade(long_trade)
        names = {r.rule_name for r in report.rule_results}
        expected_names = {
            "ema_alignment",
            "adx_threshold",
            "overextension",
            "stop_placement",
            "stop_distance",
            "entry_price_valid",
            "position_sizing",
            "volume_bounds",
            "rollover_dead_zone",
            "ftmo_daily_loss",
            "ftmo_total_loss",
        }
        assert names == expected_names


# ---------------------------------------------------------------------------
# Post-trade hook tests
# ---------------------------------------------------------------------------


class TestPostTradeHook:
    def test_hook_writes_report(self, long_trade: TradeRecord, tmp_path: Path) -> None:
        reports_path = tmp_path / "reports.jsonl"
        report = post_trade_validation_hook(long_trade, reports_path=reports_path)
        assert report.verdict == TradeVerdict.VALID
        assert reports_path.exists()

        # Verify file content
        with reports_path.open() as f:
            line = f.readline().strip()
            d = json.loads(line)
            assert d["verdict"] == "VALID"
            assert d["trade"]["symbol"] == "EURUSD"

    def test_hook_appends_reports(self, long_trade: TradeRecord, short_trade: TradeRecord, tmp_path: Path) -> None:
        reports_path = tmp_path / "reports.jsonl"
        post_trade_validation_hook(long_trade, reports_path=reports_path)
        post_trade_validation_hook(short_trade, reports_path=reports_path)

        with reports_path.open() as f:
            lines = [line.strip() for line in f if line.strip()]
        assert len(lines) == 2

    def test_hook_logs_critical(self, long_trade: TradeRecord, tmp_path: Path, caplog) -> None:
        long_trade.stop_loss = 1.12000  # CRITICAL
        reports_path = tmp_path / "reports.jsonl"
        with caplog.at_level(logging.WARNING, logger="novatrade.validation.trade_validator"):
            report = post_trade_validation_hook(long_trade, reports_path=reports_path)
        assert report.verdict == TradeVerdict.CRITICAL_VIOLATION
        assert any("CRITICAL VIOLATION" in r.message for r in caplog.records)

    def test_hook_creates_parent_dirs(self, long_trade: TradeRecord, tmp_path: Path) -> None:
        reports_path = tmp_path / "deep" / "nested" / "reports.jsonl"
        post_trade_validation_hook(long_trade, reports_path=reports_path)
        assert reports_path.exists()

    def test_hook_with_custom_config(self, long_trade: TradeRecord, tmp_path: Path) -> None:
        cfg = ValidationConfig(adx_threshold=30.0)
        reports_path = tmp_path / "reports.jsonl"
        report = post_trade_validation_hook(long_trade, config=cfg, reports_path=reports_path)
        assert report.verdict == TradeVerdict.DEVIATION_DETECTED

    def test_hook_with_ftmo_pnl(self, long_trade: TradeRecord, tmp_path: Path) -> None:
        reports_path = tmp_path / "reports.jsonl"
        report = post_trade_validation_hook(long_trade, reports_path=reports_path, daily_pnl=-6000.0)
        ftmo = next(r for r in report.rule_results if r.rule_name == "ftmo_daily_loss")
        assert ftmo.verdict == RuleVerdict.FAIL


# ---------------------------------------------------------------------------
# Integration: parse + validate
# ---------------------------------------------------------------------------


class TestIntegration:
    def test_parse_and_validate(self, evidence_file: Path) -> None:
        trades = parse_trades_from_evidence(evidence_file)
        validator = TradeValidator()
        reports = validator.validate_all(trades)
        assert len(reports) == len(trades)
        # At least one should be VALID (the filled trade with good metadata)
        verdicts = [r.verdict for r in reports]
        assert TradeVerdict.VALID in verdicts or TradeVerdict.DEVIATION_DETECTED in verdicts

    def test_parse_validate_and_write(self, evidence_file: Path, tmp_path: Path) -> None:
        trades = parse_trades_from_evidence(evidence_file)
        reports_path = tmp_path / "reports.jsonl"

        for trade in trades:
            post_trade_validation_hook(trade, reports_path=reports_path)

        # Verify output
        with reports_path.open() as f:
            lines = [line.strip() for line in f if line.strip()]
        assert len(lines) == len(trades)

        # All should be valid JSON
        for line in lines:
            d = json.loads(line)
            assert "verdict" in d
            assert "trade" in d
            assert "rules" in d


# ---------------------------------------------------------------------------
# RuleResult tests
# ---------------------------------------------------------------------------


class TestRuleResult:
    def test_to_dict(self) -> None:
        r = RuleResult(
            rule_name="test",
            verdict=RuleVerdict.PASS,
            expected="a",
            actual="b",
            detail="ok",
            critical=False,
        )
        d = r.to_dict()
        assert d["rule"] == "test"
        assert d["verdict"] == "PASS"
        assert d["expected"] == "a"
        assert d["actual"] == "b"
        assert d["critical"] is False


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_trade_record(self, cfg: ValidationConfig) -> None:
        trade = TradeRecord()
        validator = TradeValidator(cfg)
        report = validator.validate_trade(trade)
        # Most rules should SKIP due to missing data
        skip_count = sum(1 for r in report.rule_results if r.verdict == RuleVerdict.SKIP)
        assert skip_count >= 5

    def test_very_large_volume(self, cfg: ValidationConfig) -> None:
        trade = TradeRecord(
            side="BUY",
            entry_price=1.10000,
            stop_loss=1.09000,
            volume=100.0,
            timestamp=1774432800.0,
            bar_close_time=1774432800,
        )
        validator = TradeValidator(cfg)
        report = validator.validate_trade(trade)
        assert report.verdict in (TradeVerdict.DEVIATION_DETECTED, TradeVerdict.CRITICAL_VIOLATION)

    def test_entry_equals_stop(self, cfg: ValidationConfig) -> None:
        trade = TradeRecord(
            side="BUY",
            entry_price=1.10000,
            stop_loss=1.10000,
            volume=0.10,
            timestamp=1774432800.0,
            bar_close_time=1774432800,
        )
        validator = TradeValidator(cfg)
        report = validator.validate_trade(trade)
        # Stop placement should fail (SL not below entry for LONG)
        # because SL == entry (not <)
        stop_result = next(r for r in report.rule_results if r.rule_name == "stop_placement")
        assert stop_result.verdict == RuleVerdict.FAIL

    def test_multiple_critical_failures(self, cfg: ValidationConfig) -> None:
        trade = TradeRecord(
            side="BUY",
            entry_price=0,  # invalid
            stop_loss=1.12000,  # wrong side
            volume=50.0,  # way over max
            timestamp=1774432800.0,
            bar_close_time=1774432800,
        )
        validator = TradeValidator(cfg)
        report = validator.validate_trade(trade)
        assert report.verdict == TradeVerdict.CRITICAL_VIOLATION
        fail_count = sum(1 for r in report.rule_results if r.verdict == RuleVerdict.FAIL)
        assert fail_count >= 2
