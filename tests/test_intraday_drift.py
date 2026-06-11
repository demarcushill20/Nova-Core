"""Tests for the intraday EUR-weakness drift strategy logic."""

from __future__ import annotations

from datetime import datetime, timezone

from novatrade.strategies.intraday_drift import (
    DriftConfig,
    build_order,
    is_entry_window,
    is_exit_time,
    is_trade_day,
    position_size,
    side,
    stop_price,
)

UTC = timezone.utc


class TestSizing:
    def test_one_percent_risk_maps_to_lots(self):
        # 1% of 100k = $1000 risk; 20p stop * $10/pip/lot = $200/lot -> 5.0 lots
        cfg = DriftConfig(stop_pips=20.0, risk_frac=0.01)
        assert abs(position_size(100_000, cfg) - 5.0) < 1e-9

    def test_risk_scales_with_stop(self):
        # tighter stop -> bigger size (same $ risk over fewer pips)
        cfg10 = DriftConfig(stop_pips=10.0)
        cfg40 = DriftConfig(stop_pips=40.0)
        assert position_size(100_000, cfg10) == 10.0
        assert position_size(100_000, cfg40) == 2.5

    def test_size_scales_with_equity(self):
        cfg = DriftConfig(stop_pips=20.0, risk_frac=0.01)
        assert position_size(50_000, cfg) == 2.5
        assert position_size(200_000, cfg) == 10.0

    def test_min_lot_floor(self):
        cfg = DriftConfig(stop_pips=20.0, risk_frac=0.01)
        assert position_size(100, cfg) >= cfg.min_lot

    def test_max_lot_cap(self):
        cfg = DriftConfig(stop_pips=1.0, risk_frac=0.01, max_lot=50.0)
        assert position_size(10_000_000, cfg) == 50.0


class TestStopAndSide:
    def test_short_stop_is_above_entry(self):
        cfg = DriftConfig(direction=-1, stop_pips=20.0)
        assert side(cfg) == "SELL"
        # entry 1.1000, short -> stop 20p above = 1.1020
        assert abs(stop_price(1.1000, cfg) - 1.1020) < 1e-9

    def test_long_stop_is_below_entry(self):
        cfg = DriftConfig(direction=1, stop_pips=20.0)
        assert side(cfg) == "BUY"
        assert abs(stop_price(1.1000, cfg) - 1.0980) < 1e-9


class TestTiming:
    def test_entry_window_only_at_11_utc_weekday(self):
        cfg = DriftConfig()
        assert is_entry_window(datetime(2026, 6, 11, 11, 0, tzinfo=UTC), cfg)  # Thu 11:00
        assert is_entry_window(datetime(2026, 6, 11, 11, 45, tzinfo=UTC), cfg)
        assert not is_entry_window(datetime(2026, 6, 11, 10, 59, tzinfo=UTC), cfg)
        assert not is_entry_window(datetime(2026, 6, 11, 12, 0, tzinfo=UTC), cfg)

    def test_no_entry_on_weekend(self):
        cfg = DriftConfig()
        # 2026-06-13 is a Saturday
        assert not is_entry_window(datetime(2026, 6, 13, 11, 0, tzinfo=UTC), cfg)
        assert not is_trade_day(datetime(2026, 6, 13, 11, 0, tzinfo=UTC).date(), cfg)

    def test_exit_time_at_or_after_12_utc(self):
        cfg = DriftConfig()
        assert not is_exit_time(datetime(2026, 6, 11, 11, 59, tzinfo=UTC), cfg)
        assert is_exit_time(datetime(2026, 6, 11, 12, 0, tzinfo=UTC), cfg)
        assert is_exit_time(datetime(2026, 6, 11, 15, 0, tzinfo=UTC), cfg)


class TestBuildOrder:
    def test_full_order(self):
        cfg = DriftConfig(stop_pips=20.0, risk_frac=0.01)
        o = build_order(100_000, 1.1000, cfg)
        assert o.symbol == "EURUSD"
        assert o.side == "SELL"
        assert o.lot == 5.0
        assert abs(o.stop_price - 1.1020) < 1e-9
