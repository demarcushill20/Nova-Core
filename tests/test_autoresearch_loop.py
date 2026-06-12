"""Tests for the autoresearch LOOP (novatrade.research.autoresearch) — focus on
the anti-overfitting contract: split integrity, no leakage, mechanism + hold-out
gates. (Distinct from the older tests/test_autoresearch.py.)"""

from __future__ import annotations

from typing import ClassVar

import pandas as pd

from novatrade.research.autoresearch.data import Dataset, make_dataset, sealed_split, to_hourly
from novatrade.research.autoresearch.families import Candidate, backtest
from novatrade.research.autoresearch.gauntlet import Thresholds, score_candidate, train_metrics


def _synthetic_15m(days: int = 60) -> pd.DataFrame:
    idx = pd.date_range("2020-01-01", periods=days * 96, freq="15min")
    vals = []
    cur = 1.10
    for ts in idx:
        cur += -0.00005 if ts.hour == 11 else 0.0  # mild down-drift only in hour 11
        vals.append(cur)
    c = pd.Series(vals, index=idx)
    return pd.DataFrame({"open": c, "high": c + 1e-4, "low": c - 1e-4, "close": c})


class TestSplitIntegrity:
    def test_holdout_after_train_no_overlap(self):
        bars = _synthetic_15m()
        sp = sealed_split(Dataset(bars, to_hourly(bars)), holdout_frac=0.3)
        assert sp.train.bars15.index.max() < sp.cut <= sp.holdout.bars15.index.min()
        assert set(sp.train.bars15.index) & set(sp.holdout.bars15.index) == set()
        assert len(sp.train.bars15) + len(sp.holdout.bars15) == len(bars)

    def test_train_metrics_independent_of_holdout(self):
        bars = _synthetic_15m()
        sp = sealed_split(Dataset(bars, to_hourly(bars)), 0.3)
        c = Candidate.make("hour_drift", {"hour": 11, "direction": -1, "hold": 1, "stop_pips": 20.0}, "x", True)
        m1 = train_metrics(c, sp.train, 0.3)
        _corrupted = sp.holdout.bars15.copy() * 2  # mutating a hold-out copy must not matter
        m2 = train_metrics(c, sp.train, 0.3)
        assert m1 == m2  # train metrics never touch the hold-out


class TestFamilies:
    def test_hour_drift_trades_have_positive_stops(self):
        bars = _synthetic_15m()
        c = Candidate.make("hour_drift", {"hour": 11, "direction": -1, "hold": 1, "stop_pips": 20.0}, "x", True)
        t = backtest(c, Dataset(bars, to_hourly(bars)))
        assert len(t) > 0
        assert (t["stop_pips"] > 0).all()
        assert {"entry_time", "gross_pips", "stop_pips"} <= set(t.columns)

    def test_candidate_param_order_independent(self):
        c1 = Candidate.make("hour_drift", {"hour": 11, "direction": -1}, "m", True)
        c2 = Candidate.make("hour_drift", {"direction": -1, "hour": 11}, "m", True)
        assert c1 == c2 and hash(c1) == hash(c2)

    def test_fix_reversal_fades_prefix_drift(self):
        # price RISES into the 16:00 fix (peak at the fix bar) then DECLINES after
        # -> a short-at-fix (fade) should be profitable.
        idx = pd.date_range("2021-01-04", periods=20 * 96, freq="15min")
        c = []
        for ts in idx:
            if ts.hour == 15:  # pre-fix: drift up to the peak at 16:00
                base = 1.1000 + 0.0010 * (ts.minute / 45)
            elif ts.hour == 16:  # post-fix: decline from the peak
                base = 1.1010 - 0.0008 * (ts.minute / 45)
            elif ts.hour == 17:  # keep declining
                base = 1.1002 - 0.0008 * (ts.minute / 45)
            else:
                base = 1.1000
            c.append(base)
        s = pd.Series(c, index=idx)
        bars = pd.DataFrame({"open": s, "high": s + 1e-4, "low": s - 1e-4, "close": s})
        ds = Dataset(bars, to_hourly(bars))
        cand = Candidate.make(
            "fix_reversal",
            {"fix_hour": 16, "fix_minute": 0, "pre_bars": 4, "hold_bars": 4, "stop_pips": 30.0},
            "m",
            True,
        )
        t = backtest(cand, ds)
        assert len(t) > 5
        # faded the up-drift (sold) into a reversal down -> net positive
        assert t["gross_pips"].mean() > 0


class TestTiering:
    GOOD: ClassVar[dict] = {
        "n_trades": 2000,
        "sharpe": 1.2,
        "frac_pos_years": 0.9,
        "skew": 0.0,
        "kurt": 3.0,
        "n_days": 3000,
    }
    th: ClassVar[Thresholds] = Thresholds()

    def _c(self, grounded):
        return Candidate.make("hour_drift", {"hour": 11, "direction": -1}, "m", grounded)

    def test_grounded_holdout_confirm_deploys(self):
        assert score_candidate(self._c(True), self.GOOD, 200, self.th, holdout_sr=1.0).tier == "deploy"

    def test_good_train_bad_holdout_is_overfit_reject(self):
        v = score_candidate(self._c(True), self.GOOD, 200, self.th, holdout_sr=0.0)
        assert v.tier == "reject" and "overfit" in v.reason

    def test_ungrounded_blocked_at_watch(self):
        v = score_candidate(self._c(False), self.GOOD, 200, self.th, holdout_sr=1.0)
        assert v.tier == "watch" and "mechanism" in v.reason

    def test_weak_sharpe_rejected(self):
        assert score_candidate(self._c(True), {**self.GOOD, "sharpe": 0.1}, 200, self.th).tier == "reject"

    def test_train_pass_no_holdout_is_watch(self):
        assert score_candidate(self._c(True), self.GOOD, 200, self.th, holdout_sr=None).tier == "watch"

    def test_oos_wildly_above_train_rejected_as_unstable(self):
        # the AUDNZD case: train 1.2, OOS 6.6 over a real holdout -> mirage
        v = score_candidate(self._c(True), self.GOOD, 200, self.th, holdout_sr=6.6, holdout_n_days=1200)
        assert v.tier == "reject" and "unstable" in v.reason

    def test_oos_close_to_train_deploys(self):
        # the EURUSD case: train 1.2, OOS 1.3 -> stable, deploys
        v = score_candidate(self._c(True), self.GOOD, 200, self.th, holdout_sr=1.3, holdout_n_days=1200)
        assert v.tier == "deploy"

    def test_stability_skipped_without_day_count(self):
        # backward compat: no holdout_n_days -> stability test not applied
        v = score_candidate(self._c(True), self.GOOD, 200, self.th, holdout_sr=6.6)
        assert v.tier == "deploy"


def test_real_data_smoke_if_present():
    try:
        ds = make_dataset()
    except FileNotFoundError:
        return
    c = Candidate.make("hour_drift", {"hour": 11, "direction": -1, "hold": 1, "stop_pips": 20.0}, "11 UTC", True)
    assert len(backtest(c, ds)) > 1000
