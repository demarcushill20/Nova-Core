"""The scoring gauntlet — the anti-overfitting core of the autoresearch loop.

A candidate's trades are turned into a daily R series (R = (gross_pips - cost) /
stop_pips, sizing-independent), from which we take:

  - `sharpe`        annualised daily-R Sharpe on TRAIN (the ranking signal);
  - `frac_pos_years` fraction of calendar years with positive net R (regime
    robustness — a high Sharpe concentrated in one year is fragile);
  - `dsr`           Deflated Sharpe Ratio given the loop's TOTAL trial count —
    the discount for having tried many candidates (this is what stops the loop
    from believing its own luckiest draw);
  - `grounded`      whether the family has a structural mechanism.

`holdout_sharpe` is filled in ONLY for survivors, evaluated once on the sealed
hold-out. A candidate is `deployable` only if it clears Sharpe + consistency +
DSR on train, confirms on the hold-out, AND is mechanism-grounded. Ungrounded
candidates that pass everything else are tier "watch", never "deploy".
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

import numpy as np

from novatrade.backtest.research.walkforward import deflated_sharpe_ratio
from novatrade.research.autoresearch.data import Dataset
from novatrade.research.autoresearch.families import Candidate, backtest

if TYPE_CHECKING:
    import pandas as pd

ANNUALIZATION = 252


@dataclass
class Thresholds:
    cost_pips: float = 0.30  # measured EURUSD in-hour spread
    min_trades: int = 200
    min_sharpe: float = 0.5
    min_frac_pos_years: float = 0.60
    min_dsr: float = 0.90  # López de Prado survival bar
    min_holdout_sharpe: float = 0.25
    max_stability_z: float = 4.0  # reject if |OOS - train| exceeds this many Sharpe-SEs


@dataclass
class Verdict:
    key: str
    family: str
    grounded: bool
    n_trades: int
    sharpe: float
    frac_pos_years: float
    dsr: float
    holdout_sharpe: float | None
    tier: str  # "deploy" | "watch" | "reject"
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


def _daily_r(trades: pd.DataFrame, cost_pips: float) -> pd.Series:
    net = (trades["gross_pips"] - cost_pips) / trades["stop_pips"]
    return net.groupby(trades["entry_time"].dt.date).sum()


def _sharpe(daily: pd.Series) -> float:
    if len(daily) < 20 or daily.std() == 0:
        return 0.0
    return float(daily.mean() / daily.std() * np.sqrt(ANNUALIZATION))


def _frac_pos_years(trades: pd.DataFrame, cost_pips: float) -> float:
    net = (trades["gross_pips"] - cost_pips) / trades["stop_pips"]
    yearly = net.groupby(trades["entry_time"].dt.year).sum()
    return float((yearly > 0).mean()) if len(yearly) else 0.0


def train_metrics(c: Candidate, train: Dataset, cost_pips: float) -> dict:
    """Cheap per-candidate metrics on TRAIN (no hold-out, no DSR yet)."""
    trades = backtest(c, train)
    if len(trades) < 2:
        return {"n_trades": len(trades), "sharpe": 0.0, "frac_pos_years": 0.0, "skew": 0.0, "kurt": 3.0, "n_days": 0}
    daily = _daily_r(trades, cost_pips)
    return {
        "n_trades": len(trades),
        "sharpe": _sharpe(daily),
        "frac_pos_years": _frac_pos_years(trades, cost_pips),
        "skew": float(daily.skew()) if len(daily) > 2 else 0.0,
        "kurt": float(daily.kurt() + 3.0) if len(daily) > 3 else 3.0,
        "n_days": len(daily),
    }


def holdout_eval(c: Candidate, holdout: Dataset, cost_pips: float) -> tuple[float, int]:
    """Hold-out Sharpe AND its day count (needed for the stability test)."""
    trades = backtest(c, holdout)
    if len(trades) < 2:
        return 0.0, 0
    daily = _daily_r(trades, cost_pips)
    return _sharpe(daily), len(daily)


def holdout_sharpe(c: Candidate, holdout: Dataset, cost_pips: float) -> float:
    return holdout_eval(c, holdout, cost_pips)[0]


def _sharpe_se(sharpe_ann: float, n_days: int) -> float:
    """Standard error of an annualised Sharpe over n daily obs (Lo 2002).
    Returns inf for tiny samples so the stability test can't pass on noise."""
    if n_days < 30:
        return float("inf")
    sr_p = sharpe_ann / np.sqrt(ANNUALIZATION)
    return float(np.sqrt((1 + 0.5 * sr_p**2) / n_days) * np.sqrt(ANNUALIZATION))


def score_candidate(
    c: Candidate,
    train_m: dict,
    n_trials: int,
    th: Thresholds,
    holdout_sr: float | None = None,
    holdout_n_days: int | None = None,
) -> Verdict:
    """Combine train metrics + the loop's trial count into a tiered verdict.
    `holdout_sr` (and its `holdout_n_days`) are provided only for survivors."""
    dsr = deflated_sharpe_ratio(
        train_m["sharpe"],
        n_obs=max(train_m["n_days"], 2),
        n_trials=max(n_trials, 1),
        skew=train_m["skew"],
        kurt=train_m["kurt"],
    )
    dsr = 0.0 if (dsr != dsr) else float(dsr)  # NaN -> 0

    passes_train = (
        train_m["n_trades"] >= th.min_trades
        and train_m["sharpe"] >= th.min_sharpe
        and train_m["frac_pos_years"] >= th.min_frac_pos_years
        and dsr >= th.min_dsr
    )

    if not passes_train:
        why = []
        if train_m["n_trades"] < th.min_trades:
            why.append(f"trades {train_m['n_trades']}<{th.min_trades}")
        if train_m["sharpe"] < th.min_sharpe:
            why.append(f"sharpe {train_m['sharpe']:.2f}<{th.min_sharpe}")
        if train_m["frac_pos_years"] < th.min_frac_pos_years:
            why.append(f"posYears {train_m['frac_pos_years']:.2f}<{th.min_frac_pos_years}")
        if dsr < th.min_dsr:
            why.append(f"DSR {dsr:.2f}<{th.min_dsr}")
        tier, reason = "reject", "; ".join(why)
    elif holdout_sr is None:
        tier, reason = "watch", "passed train; awaiting hold-out confirm"
    elif holdout_sr < th.min_holdout_sharpe:
        tier, reason = "reject", f"hold-out sharpe {holdout_sr:.2f}<{th.min_holdout_sharpe} (overfit)"
    elif holdout_n_days and abs(holdout_sr - train_m["sharpe"]) > th.max_stability_z * _sharpe_se(
        train_m["sharpe"], holdout_n_days
    ):
        # OOS diverges from train by more than sampling error -> non-stationary /
        # small-sample artifact; neither estimate is a trustworthy forward number.
        tier, reason = (
            "reject",
            f"OOS {holdout_sr:.2f} vs train {train_m['sharpe']:.2f} unstable "
            f"(>{th.max_stability_z:.0f}σ — regime/small-sample artifact)",
        )
    elif not c.grounded:
        tier, reason = "watch", "confirmed on hold-out but NO structural mechanism — watch, do not deploy"
    else:
        tier, reason = "deploy", "passed train + DSR + hold-out + mechanism gate"

    return Verdict(
        key=c.key,
        family=c.family,
        grounded=c.grounded,
        n_trades=train_m["n_trades"],
        sharpe=round(train_m["sharpe"], 3),
        frac_pos_years=round(train_m["frac_pos_years"], 3),
        dsr=round(dsr, 3),
        holdout_sharpe=None if holdout_sr is None else round(holdout_sr, 3),
        tier=tier,
        reason=reason,
    )
