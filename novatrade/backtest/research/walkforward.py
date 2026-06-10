"""Walk-forward / sub-period consistency on top of the fidelity engine.

The fixed canonical config is NOT fit to this data, so running it across rolling
sub-periods is a legitimate out-of-sample consistency test: is the edge broad and
time-stable, or concentrated in a few regimes (= fragile)? Per-trade R is
sizing-independent (gross_r = gross_pips / stop_pips), so cost can be applied
analytically (net_r = gross_r - cost_pips/stop_pips) without re-running.

Includes a Deflated Sharpe Ratio utility for the later search phase (Phase 4),
where many configs are tried and the best must be discounted for trial count.
"""

from __future__ import annotations

import math

import pandas as pd

PIP_DEFAULT = 0.0001


def trade_records(state, pip: float = PIP_DEFAULT) -> pd.DataFrame:
    """Per-position (entry_time, gross_r, net_r, stop_pips) from the engine's
    correct-by-construction position ledger (``state._positions``)."""
    recs = []
    for p in state._positions:
        risk = p["stop_pips"] * pip * p["qty"]
        if risk > 0 and p["stop_pips"] > 0:
            recs.append(
                {
                    "entry_time": p["entry_time"],
                    "gross_r": p["gross"] / risk,
                    "net_r": p["net"] / risk,
                    "stop_pips": p["stop_pips"],
                }
            )
    df = pd.DataFrame(recs)
    if len(df):
        df["entry_time"] = pd.to_datetime(df["entry_time"], utc=True).dt.tz_localize(None)
    return df


def subperiod_consistency(records: pd.DataFrame, freq: str = "Y", cost_pips: float = 0.0) -> pd.DataFrame:
    """Per-period mean net-R and trade count. freq: pandas period freq (Y=year, Q=quarter)."""
    r = records.copy()
    r["net_r"] = r["gross_r"] - cost_pips / r["stop_pips"]
    r["period"] = r["entry_time"].dt.to_period(freq)
    g = r.groupby("period")["net_r"].agg(n="count", mean_R="mean")
    return g


def summary(records: pd.DataFrame, costs=(0.0, 0.5), freq: str = "Y") -> dict:
    periods: dict = {}
    out = {"n_trades": len(records), "periods": periods}
    for c in costs:
        tab = subperiod_consistency(records, freq=freq, cost_pips=c)
        pos = int((tab["mean_R"] > 0).sum())
        periods[c] = {
            "overall_mean_R": float((records["gross_r"] - c / records["stop_pips"]).mean()),
            "n_periods": len(tab),
            "positive_periods": pos,
            "frac_positive": pos / len(tab) if len(tab) else 0.0,
            "worst_period_R": float(tab["mean_R"].min()),
            "best_period_R": float(tab["mean_R"].max()),
            "table": tab,
        }
    return out


def deflated_sharpe_ratio(sharpe: float, n_obs: int, n_trials: int, skew: float = 0.0, kurt: float = 3.0) -> float:
    """López de Prado Deflated Sharpe Ratio — probability the true Sharpe>0 after
    correcting for `n_trials` strategies tried. For Phase 4 (config search)."""
    if n_trials < 1 or n_obs < 2:
        return float("nan")
    emc = 0.5772156649
    # expected max of n_trials standard normals (approx)
    e_max = (1 - emc) * _norm_ppf(1 - 1.0 / n_trials) + emc * _norm_ppf(1 - 1.0 / (n_trials * math.e))
    sr_std = math.sqrt((1 - skew * sharpe + (kurt - 1) / 4 * sharpe**2) / (n_obs - 1))
    if sr_std == 0:
        return float("nan")
    return _norm_cdf((sharpe - e_max * sr_std) / sr_std)


def _norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _norm_ppf(p: float) -> float:
    # Acklam's rational approximation (sufficient for DSR)
    a = [
        -3.969683028665376e1,
        2.209460984245205e2,
        -2.759285104469687e2,
        1.383577518672690e2,
        -3.066479806614716e1,
        2.506628277459239,
    ]
    b = [-5.447609879822406e1, 1.615858368580409e2, -1.556989798598866e2, 6.680131188771972e1, -1.328068155288572e1]
    c = [
        -7.784894002430293e-3,
        -3.223964580411365e-1,
        -2.400758277161838,
        -2.549732539343734,
        4.374664141464968,
        2.938163982698783,
    ]
    d = [7.784695709041462e-3, 3.224671290700398e-1, 2.445134137142996, 3.754408661907416]
    pl, ph = 0.02425, 1 - 0.02425
    if p < pl:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1
        )
    if p <= ph:
        q = p - 0.5
        r = q * q
        return (
            (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5])
            * q
            / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)
        )
    q = math.sqrt(-2 * math.log(1 - p))
    return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
        (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1
    )
