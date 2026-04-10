"""Tests for compute_bbw utility and Sortino formula fix.

Verifies:
  - compute_bbw() produces correct Bollinger Band Width values
  - Sortino formula uses target=0 (industry standard)
"""

from __future__ import annotations

import math
import statistics

import pytest

from novatrade.backtest.engine import compute_bbw
from novatrade.backtest.metrics import _compute_sharpe_sortino

# ---------------------------------------------------------------------------
# compute_bbw tests
# ---------------------------------------------------------------------------


class TestComputeBBW:
    """Bollinger Band Width computation."""

    def test_constant_prices_bbw_zero(self):
        """Constant prices → std = 0 → BBW = 0."""
        closes = [1.1000] * 30
        bbw = compute_bbw(closes, 20)
        assert len(bbw) == 30
        assert bbw[19] == pytest.approx(0.0, abs=1e-10)

    def test_known_values(self):
        """Hand-calculated BBW for a small window."""
        closes = [1.0, 2.0, 3.0, 4.0, 5.0]
        bbw = compute_bbw(closes, 5)
        # sma = 3.0, std = sqrt(2.0), bbw = 2*sqrt(2)/3
        expected = 2 * math.sqrt(2.0) / 3.0
        assert bbw[4] == pytest.approx(expected, rel=1e-6)

    def test_nan_padding(self):
        """Values before warmup period should be NaN."""
        closes = [1.0 + i * 0.001 for i in range(25)]
        bbw = compute_bbw(closes, 20)
        for i in range(19):
            assert math.isnan(bbw[i])
        assert not math.isnan(bbw[19])

    def test_empty_input(self):
        """Empty input returns empty list."""
        assert compute_bbw([], 20) == []

    def test_too_short(self):
        """Input shorter than period returns all NaN."""
        bbw = compute_bbw([1.0, 2.0], 20)
        assert all(math.isnan(v) for v in bbw)

    def test_volatile_vs_flat(self):
        """Volatile prices should have higher BBW than flat."""
        flat = [1.1000] * 30
        volatile = [1.1000 + (0.01 if i % 2 else -0.01) for i in range(30)]
        bbw_flat = compute_bbw(flat, 20)
        bbw_vol = compute_bbw(volatile, 20)
        assert bbw_flat[29] < bbw_vol[29]


# ---------------------------------------------------------------------------
# Sortino formula tests (target=0 standard)
# ---------------------------------------------------------------------------


class TestSortinoStandard:
    """Verify Sortino uses target=0 (not risk-free rate) for downside deviation."""

    def test_sortino_target_zero(self):
        """Downside deviation should use min(r, 0) not min(r - rf, 0)."""
        trade_pnl = [100, -80, 100, -80, 100, -80, 100, -80, 100, -80]
        initial_equity = 100_000.0
        trading_days = 100.0

        _, sortino = _compute_sharpe_sortino(trade_pnl, initial_equity, trading_days)

        # Hand-calculate with target=0
        returns = [p / initial_equity for p in trade_pnl]
        mean_r = statistics.mean(returns)
        downside_squares = [min(r, 0.0) ** 2 for r in returns]
        downside_dev = (sum(downside_squares) / len(returns)) ** 0.5
        trades_per_year = (10 / 100) * 252
        annualization = trades_per_year**0.5
        expected = (mean_r / downside_dev) * annualization

        assert sortino == pytest.approx(expected, rel=1e-4)

    def test_sortino_all_positive_returns(self):
        """All positive returns → downside_dev = 0 → sortino = 0."""
        trade_pnl = [100, 200, 150, 300, 250]
        _, sortino = _compute_sharpe_sortino(trade_pnl, 100_000.0, 100.0)
        assert sortino == 0.0

    def test_sortino_all_negative_returns(self):
        """All negative returns → non-zero sortino (negative)."""
        trade_pnl = [-100, -200, -150, -300, -250]
        _, sortino = _compute_sharpe_sortino(trade_pnl, 100_000.0, 100.0)
        assert sortino < 0

    def test_sortino_greater_than_sharpe_positive_expectancy(self):
        """For positive-expectancy mixed returns, sortino > sharpe because
        downside_dev < std_dev (upside variance excluded)."""
        trade_pnl = [200, -50, 300, -30, 250, -20, 180, -40, 220, -10]
        sharpe, sortino = _compute_sharpe_sortino(trade_pnl, 100_000.0, 200.0)
        # Both should be positive
        assert sharpe > 0
        assert sortino > 0
        # Sortino should exceed Sharpe
        assert sortino > sharpe
