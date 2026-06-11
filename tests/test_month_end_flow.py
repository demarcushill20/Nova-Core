"""Tests for the month-end rebalancing-flow strategy logic."""

from __future__ import annotations

from datetime import date

import pandas as pd

from novatrade.strategies.month_end_flow import (
    MonthEndConfig,
    basket_orders,
    compute_signal,
    is_trade_day,
)


class TestTradeDay:
    def test_last_business_days_of_quarter_ends(self):
        # 2024: Mar 29 (Fri; 30/31 weekend), Jun 28 (Fri; 29/30 weekend),
        # Sep 30 (Mon), Dec 31 (Tue) are the last business days.
        assert is_trade_day(date(2024, 3, 29))
        assert is_trade_day(date(2024, 6, 28))
        assert is_trade_day(date(2024, 9, 30))
        assert is_trade_day(date(2024, 12, 31))

    def test_rejects_non_last_and_non_quarter(self):
        assert not is_trade_day(date(2024, 3, 28))  # not the last biz day
        assert not is_trade_day(date(2024, 4, 30))  # last biz day but April != quarter-end
        assert not is_trade_day(date(2024, 3, 30))  # Saturday
        assert not is_trade_day(date(2024, 12, 30))  # Mon, but 31 Tue is later

    def test_all_months_mode(self):
        cfg = MonthEndConfig(quarter_ends_only=False)
        assert is_trade_day(date(2024, 4, 30), cfg)  # last biz day of April now counts


class TestSignal:
    def _series(self, vals):
        idx = pd.date_range("2024-01-31", periods=len(vals), freq="B")
        return pd.Series(vals, index=idx)

    def test_up_quarter_sells_usd(self):
        # S&P rising through the quarter -> signal +1 (sell USD)
        s = self._series([100, 101, 102, 103, 104, 105])
        assert compute_signal(s, s.index[-1].date()) == 1

    def test_down_quarter_buys_usd(self):
        s = self._series([105, 104, 103, 102, 101, 100])
        assert compute_signal(s, s.index[-1].date()) == -1

    def test_no_lookahead_uses_prior_close(self):
        # asof equals a date; only strictly-prior closes are used.
        s = self._series([100, 101, 102, 103])
        sig = compute_signal(s, s.index[-1].date())
        assert sig == 1  # prior closes still rising


class TestBasketOrders:
    def test_sell_usd_directions(self):
        orders = basket_orders(1, equity=100_000, cfg=MonthEndConfig(leverage=5.0))
        by = {o.symbol: o for o in orders}
        assert by["EURUSD"].side == "BUY"  # long EUR == short USD
        assert by["GBPUSD"].side == "BUY"
        assert by["AUDUSD"].side == "BUY"
        assert by["NZDUSD"].side == "BUY"
        assert by["USDCAD"].side == "SELL"  # short USDCAD == short USD
        assert len(orders) == 5

    def test_buy_usd_flips_all(self):
        orders = basket_orders(-1, equity=100_000, cfg=MonthEndConfig(leverage=5.0))
        by = {o.symbol: o.side for o in orders}
        assert by == {"EURUSD": "SELL", "GBPUSD": "SELL", "AUDUSD": "SELL", "NZDUSD": "SELL", "USDCAD": "BUY"}

    def test_lot_sizing(self):
        # leverage 5, equity 100k, 5 legs -> per-leg notional 100k -> 1.0 lot
        orders = basket_orders(1, equity=100_000, cfg=MonthEndConfig(leverage=5.0))
        assert all(abs(o.lot - 1.0) < 1e-9 for o in orders)

    def test_min_lot_floor(self):
        orders = basket_orders(1, equity=1_000, cfg=MonthEndConfig(leverage=5.0))
        assert all(o.lot >= 0.01 for o in orders)
