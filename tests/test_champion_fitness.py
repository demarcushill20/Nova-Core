"""Tests for the ChampionFitness multi-horizon scorer."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from novatrade.evaluation.champion_fitness import (
    WEIGHTS,
    _max_drawdown_pct,
    _score_calmar,
    _score_max_drawdown,
    _score_pf_stability,
    _score_profitable_months,
    _score_worst_rolling_1yr,
    compute_champion_fitness,
    compute_champion_fitness_detailed,
)


@dataclass
class FakeTrade:
    """Minimal trade object for testing."""

    entry_timestamp: float = 0.0
    exit_timestamp: float = 0.0
    pnl_pips: float = 0.0
    pnl_usd: float = 0.0


def _ts(year: int, month: int, day: int = 15) -> float:
    """Helper: UTC timestamp for a date."""
    return datetime(year, month, day, tzinfo=timezone.utc).timestamp()


def _make_monthly_trades(
    monthly_pnls: list[tuple[int, int, float]],
    pip_factor: float = 100.0,
) -> list[FakeTrade]:
    """Create trades from (year, month, pnl_usd) tuples — one trade per month."""
    trades = []
    for year, month, pnl_usd in monthly_pnls:
        ts = _ts(year, month)
        trades.append(
            FakeTrade(
                entry_timestamp=ts - 86400,
                exit_timestamp=ts,
                pnl_usd=pnl_usd,
                pnl_pips=pnl_usd / pip_factor,
            )
        )
    return trades


# ---------------------------------------------------------------------------
# Hard gates
# ---------------------------------------------------------------------------


class TestHardGates:
    def test_too_few_trades(self):
        trades = [FakeTrade(exit_timestamp=_ts(2020, 1), pnl_usd=100, pnl_pips=10)] * 50
        assert compute_champion_fitness(trades, min_trades=100) == 0.0

    def test_unprofitable_strategy(self):
        """PF ≤ 1.0 → immediate 0."""
        trades = []
        for m in range(1, 13):
            # 5 losers for every 3 winners → PF < 1.0
            for _ in range(5):
                trades.append(FakeTrade(exit_timestamp=_ts(2020, m), pnl_usd=-100, pnl_pips=-10))
            for _ in range(3):
                trades.append(FakeTrade(exit_timestamp=_ts(2020, m), pnl_usd=100, pnl_pips=10))
        assert len(trades) >= 96
        assert compute_champion_fitness(trades, min_trades=50) == 0.0

    def test_excessive_drawdown(self):
        """Max DD > 15% → immediate 0."""
        trades = []
        # Huge loss followed by recovery
        trades.append(
            FakeTrade(entry_timestamp=_ts(2020, 1, 1), exit_timestamp=_ts(2020, 1, 2), pnl_usd=-20000, pnl_pips=-200)
        )
        for m in range(2, 13):
            for _ in range(10):
                trades.append(FakeTrade(exit_timestamp=_ts(2020, m), pnl_usd=500, pnl_pips=5))
        assert compute_champion_fitness(trades, min_trades=50) == 0.0


# ---------------------------------------------------------------------------
# Component: % Profitable Months
# ---------------------------------------------------------------------------


class TestProfitableMonths:
    def test_all_profitable(self):
        """100% profitable months → score ~1.0."""
        trades = _make_monthly_trades([(2020, m, 500) for m in range(1, 13)])
        score, details = _score_profitable_months(trades)
        assert score == 1.0
        assert details["pct"] == 100.0

    def test_half_profitable(self):
        """50% profitable → score ~0.2 (just above the 45% floor)."""
        months = [(2020, m, 500 if m <= 6 else -500) for m in range(1, 13)]
        trades = _make_monthly_trades(months)
        score, details = _score_profitable_months(trades)
        assert details["pct"] == 50.0
        assert 0.15 < score < 0.25

    def test_below_floor(self):
        """< 45% profitable → score 0.0."""
        months = [(2020, m, 500 if m <= 4 else -500) for m in range(1, 13)]
        trades = _make_monthly_trades(months)
        score, _ = _score_profitable_months(trades)
        assert score == 0.0

    def test_empty(self):
        score, details = _score_profitable_months([])
        assert score == 0.0
        assert details["total_months"] == 0


# ---------------------------------------------------------------------------
# Component: Max Drawdown
# ---------------------------------------------------------------------------


class TestMaxDrawdown:
    def test_no_drawdown(self):
        """All wins → DD=0 → score 1.0."""
        trades = _make_monthly_trades([(2020, m, 500) for m in range(1, 13)])
        score, details = _score_max_drawdown(trades, 100_000)
        assert score == 1.0
        assert details["max_dd_pct"] == 0.0

    def test_moderate_drawdown(self):
        """~7.5% DD → score ~0.5."""
        trades = [
            FakeTrade(exit_timestamp=_ts(2020, 1), pnl_usd=-7500, pnl_pips=-75),
            FakeTrade(exit_timestamp=_ts(2020, 6), pnl_usd=10000, pnl_pips=100),
        ]
        score, details = _score_max_drawdown(trades, 100_000)
        assert 0.45 < score < 0.55
        assert 7.0 < details["max_dd_pct"] < 8.0

    def test_max_drawdown_computation(self):
        """Verify the equity curve drawdown calc."""
        # Start 100k, lose 10k, gain 20k, lose 15k
        curve = [100_000, 90_000, 110_000, 95_000]
        dd = _max_drawdown_pct(curve)
        # Max DD: (110000 - 95000) / 110000 = 13.6%
        assert 13.5 < dd < 13.7


# ---------------------------------------------------------------------------
# Component: PF Stability
# ---------------------------------------------------------------------------


class TestPFStability:
    def test_perfect_stability(self):
        """Same PF every year → CV≈0 → score ~1.0."""
        trades = []
        for year in range(2016, 2026):
            for m in range(1, 13):
                # 7 wins, 3 losses → PF ≈ 2.33 every year
                for _ in range(7):
                    trades.append(FakeTrade(exit_timestamp=_ts(year, m), pnl_usd=100, pnl_pips=10))
                for _ in range(3):
                    trades.append(FakeTrade(exit_timestamp=_ts(year, m), pnl_usd=-100, pnl_pips=-10))
        score, details = _score_pf_stability(trades)
        assert score > 0.9
        assert details["cv"] < 0.05

    def test_unstable_pf(self):
        """Wildly varying PF → high CV → low score."""
        trades = []
        for year in range(2016, 2026):
            if year % 2 == 0:
                # Good year: PF = 3.0
                for m in range(1, 13):
                    trades.append(FakeTrade(exit_timestamp=_ts(year, m), pnl_usd=300, pnl_pips=30))
                    trades.append(FakeTrade(exit_timestamp=_ts(year, m), pnl_usd=-100, pnl_pips=-10))
            else:
                # Bad year: PF = 1.05
                for m in range(1, 13):
                    trades.append(FakeTrade(exit_timestamp=_ts(year, m), pnl_usd=105, pnl_pips=10.5))
                    trades.append(FakeTrade(exit_timestamp=_ts(year, m), pnl_usd=-100, pnl_pips=-10))
        score, details = _score_pf_stability(trades)
        assert score < 0.5

    def test_insufficient_years(self):
        """< 2 years → neutral 0.5."""
        trades = _make_monthly_trades([(2020, m, 500) for m in range(1, 13)])
        score, details = _score_pf_stability(trades)
        assert score == 0.5
        assert details["reason"] == "insufficient_years"


# ---------------------------------------------------------------------------
# Component: Worst Rolling 1yr Return
# ---------------------------------------------------------------------------


class TestWorstRolling1yr:
    def test_consistently_positive(self):
        """Every rolling 1yr is positive → high score."""
        trades = _make_monthly_trades([(2020 + m // 12, m % 12 + 1, 500) for m in range(36)])
        score, details = _score_worst_rolling_1yr(trades, 100_000)
        assert score > 0.8
        assert details["worst_rolling_1yr_pct"] > 0

    def test_one_bad_year(self):
        """One stretch of losses → worst rolling 1yr negative."""
        months = []
        for m in range(36):
            year = 2020 + m // 12
            month = m % 12 + 1
            # Months 7-18 are losing
            pnl = -300 if 7 <= m <= 18 else 500
            months.append((year, month, pnl))
        trades = _make_monthly_trades(months)
        score, details = _score_worst_rolling_1yr(trades, 100_000)
        assert details["worst_rolling_1yr_pct"] < 0
        assert score < 0.8

    def test_insufficient_months(self):
        """< 12 months → neutral 0.5."""
        trades = _make_monthly_trades([(2020, m, 500) for m in range(1, 10)])
        score, details = _score_worst_rolling_1yr(trades, 100_000)
        assert score == 0.5
        assert details["reason"] == "insufficient_months"


# ---------------------------------------------------------------------------
# Component: Calmar Ratio
# ---------------------------------------------------------------------------


class TestCalmar:
    def test_high_calmar(self):
        """High return, low DD → high Calmar → score ~1.0."""
        trades = []
        for year in range(2020, 2023):
            for m in range(1, 13):
                trades.append(
                    FakeTrade(
                        entry_timestamp=_ts(year, m, 1),
                        exit_timestamp=_ts(year, m, 15),
                        pnl_usd=2000,
                        pnl_pips=20,
                    )
                )
        score, details = _score_calmar(trades, 100_000)
        assert score > 0.7
        assert details["calmar"] > 2.0

    def test_low_calmar(self):
        """Mediocre return with significant DD."""
        trades = [
            FakeTrade(entry_timestamp=_ts(2020, 1, 1), exit_timestamp=_ts(2020, 2, 1), pnl_usd=-8000, pnl_pips=-80),
            FakeTrade(entry_timestamp=_ts(2020, 3, 1), exit_timestamp=_ts(2020, 12, 1), pnl_usd=10000, pnl_pips=100),
        ]
        score, details = _score_calmar(trades, 100_000)
        assert score < 0.5


# ---------------------------------------------------------------------------
# Integration: Full champion fitness
# ---------------------------------------------------------------------------


class TestChampionFitnessIntegration:
    def _build_consistent_strategy(self, years: int = 10) -> list[FakeTrade]:
        """Build a consistently profitable strategy over N years."""
        trades = []
        for y in range(years):
            year = 2016 + y
            for m in range(1, 13):
                # 8 wins, 2 losses per month → PF≈4.0, ~60% monthly profit
                for _ in range(8):
                    trades.append(
                        FakeTrade(
                            entry_timestamp=_ts(year, m, 1),
                            exit_timestamp=_ts(year, m, 10),
                            pnl_usd=200,
                            pnl_pips=2.0,
                        )
                    )
                for _ in range(2):
                    trades.append(
                        FakeTrade(
                            entry_timestamp=_ts(year, m, 11),
                            exit_timestamp=_ts(year, m, 20),
                            pnl_usd=-200,
                            pnl_pips=-2.0,
                        )
                    )
        return trades

    def test_consistent_strategy_scores_high(self):
        trades = self._build_consistent_strategy()
        score = compute_champion_fitness(trades, min_trades=50)
        assert score > 0.5

    def test_detailed_returns_components(self):
        trades = self._build_consistent_strategy()
        score, details = compute_champion_fitness_detailed(trades, min_trades=50)
        assert score > 0.5
        assert "components" in details
        assert set(details["components"]) == set(WEIGHTS)
        for _name, comp in details["components"].items():
            assert "score" in comp
            assert "weight" in comp
            assert 0.0 <= comp["score"] <= 1.0

    def test_weights_sum_to_one(self):
        assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9

    def test_score_between_zero_and_one(self):
        trades = self._build_consistent_strategy()
        score = compute_champion_fitness(trades, min_trades=50)
        assert 0.0 <= score <= 1.0

    def test_better_strategy_scores_higher(self):
        """A more consistent strategy should outscore a volatile one."""
        # Consistent: small steady wins
        consistent = []
        for y in range(5):
            year = 2020 + y
            for m in range(1, 13):
                for _ in range(10):
                    consistent.append(
                        FakeTrade(
                            entry_timestamp=_ts(year, m, 1),
                            exit_timestamp=_ts(year, m, 15),
                            pnl_usd=150,
                            pnl_pips=1.5,
                        )
                    )
                for _ in range(3):
                    consistent.append(
                        FakeTrade(
                            entry_timestamp=_ts(year, m, 16),
                            exit_timestamp=_ts(year, m, 25),
                            pnl_usd=-150,
                            pnl_pips=-1.5,
                        )
                    )

        # Volatile: big swings, same total profit
        volatile = []
        for y in range(5):
            year = 2020 + y
            for m in range(1, 13):
                if m % 3 == 0:
                    # Big losing month
                    for _ in range(10):
                        volatile.append(
                            FakeTrade(
                                entry_timestamp=_ts(year, m, 1),
                                exit_timestamp=_ts(year, m, 15),
                                pnl_usd=-500,
                                pnl_pips=-5.0,
                            )
                        )
                else:
                    # Big winning month
                    for _ in range(10):
                        volatile.append(
                            FakeTrade(
                                entry_timestamp=_ts(year, m, 1),
                                exit_timestamp=_ts(year, m, 15),
                                pnl_usd=400,
                                pnl_pips=4.0,
                            )
                        )
                    for _ in range(3):
                        volatile.append(
                            FakeTrade(
                                entry_timestamp=_ts(year, m, 16),
                                exit_timestamp=_ts(year, m, 25),
                                pnl_usd=-100,
                                pnl_pips=-1.0,
                            )
                        )

        score_consistent = compute_champion_fitness(consistent, min_trades=50)
        score_volatile = compute_champion_fitness(volatile, min_trades=50)
        assert score_consistent > score_volatile, (
            f"Consistent ({score_consistent:.3f}) should beat volatile ({score_volatile:.3f})"
        )
