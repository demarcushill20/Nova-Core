"""Own-hours multi-pair diversification test (Breedon-Ranaldo).

Thesis: each currency systematically depreciates during its OWN local trading
session. We validated EUR (short EURUSD in EU afternoon, t=-7). If the same effect
holds per currency, the trades sit in DIFFERENT UTC hours -> naturally decorrelated
-> a portfolio Sharpe that lifts ~sqrt(N_eff) over any single pair. This directly
attacks the strategy_search wall ("need ~30 uncorrelated instruments for Sharpe 1").

Discipline against data-mining:
  * Session windows are PRE-REGISTERED from local market hours (Frankfurt/London/
    Tokyo/Sydney/Wellington/Toronto), NOT picked from the data. We trade the WHOLE
    session (open->close), not a mined peak hour.
  * Per-pair edge reported in-sample (first 60%) AND out-of-sample (last 40%).
  * Net of conservative per-pair ECN spreads.
  * Correlation matrix shows the TRUE effective breadth (Asian pairs co-move).

Trade: at session-open hour, take the own-currency-weakness position on the USD
pair; flat at session-close hour. One position/day/pair. PnL = price return net of
spread (unitless, cross-pair comparable). Portfolio = equal-risk (inverse-vol) blend.

Data: data/candles/histdata/<PAIR>_M1.csv (HistData, UTC, 2004-2024; USDJPY 2016+).
"""

from __future__ import annotations

import os

import numpy as np
import pandas as pd

CACHE = "data/candles/_oh_cache"
os.makedirs(CACHE, exist_ok=True)

# Pre-registered own-hours config. dir = position on the INSTRUMENT (+1 long / -1 short)
# to be SHORT the local currency during its session. Sessions in UTC hours.
PAIRS = {
    #              instr_dir  start  end   pip      spread_pips (conservative ECN)
    "EURUSD": dict(d=-1, s=7, e=16, pip=1e-4, cost=0.4),  # short EUR, Frankfurt
    "GBPUSD": dict(d=-1, s=8, e=16, pip=1e-4, cost=0.6),  # short GBP, London
    "USDJPY": dict(d=+1, s=0, e=6, pip=1e-2, cost=0.6),  # short JPY = long USDJPY, Tokyo
    "AUDUSD": dict(d=-1, s=23, e=6, pip=1e-4, cost=0.6),  # short AUD, Sydney
    "NZDUSD": dict(d=-1, s=21, e=4, pip=1e-4, cost=1.0),  # short NZD, Wellington
    "USDCAD": dict(d=+1, s=13, e=21, pip=1e-4, cost=0.8),  # short CAD = long USDCAD, Toronto
}


def hourly(pair: str) -> pd.DataFrame:
    """Resample M1 CSV -> hourly open/close, cached to parquet."""
    cpath = f"{CACHE}/{pair}_1h.parquet"
    if os.path.exists(cpath):
        return pd.read_parquet(cpath)
    df = (
        pd.read_csv(f"data/candles/histdata/{pair}_M1.csv", usecols=["time", "open", "close"], parse_dates=["time"])
        .set_index("time")
        .sort_index()
    )
    o = df["open"].resample("1h").first()
    c = df["close"].resample("1h").last()
    h = pd.DataFrame({"open": o, "close": c}).dropna()
    if h.index.tz is None:
        h.index = h.index.tz_localize("UTC")
    h.to_parquet(cpath)
    return h


def session_trades(pair: str) -> pd.Series:
    """One net-return per trading day for the own-hours session trade."""
    cfg = PAIRS[pair]
    h = hourly(pair)
    dur = (cfg["e"] - cfg["s"]) % 24  # session length in hours (handles wrap)
    # entry bars = every bar at the session-open hour, on weekdays
    ent = h[(h.index.hour == cfg["s"]) & (h.index.weekday < 5)]
    rows = {}
    for ts, row in ent.iterrows():
        exit_ts = ts + pd.Timedelta(hours=dur)
        if exit_ts not in h.index:
            continue  # weekend/holiday gap -> skip
        entry = row["open"]
        exit_px = h.loc[exit_ts, "open"]
        ret = cfg["d"] * (exit_px - entry) / entry  # signed price return
        cost = cfg["cost"] * cfg["pip"] / entry  # round-trip spread as return
        rows[ts.normalize()] = ret - cost
    return pd.Series(rows, name=pair).sort_index()


def stats(r: pd.Series) -> dict:
    r = r.dropna()
    if len(r) < 30:
        return dict(n=len(r), t=np.nan, sharpe=np.nan, mean_bps=np.nan, win=np.nan)
    return dict(
        n=len(r),
        mean_bps=r.mean() * 1e4,
        t=r.mean() / (r.std() / np.sqrt(len(r))),
        sharpe=r.mean() / r.std() * np.sqrt(252),
        win=(r > 0).mean(),
    )


def split_stats(r: pd.Series):
    r = r.dropna()
    cut = int(len(r) * 0.60)
    return stats(r.iloc[:cut]), stats(r.iloc[cut:])


def max_dd(equity: pd.Series) -> float:
    peak = equity.cummax()
    return float(((equity - peak) / peak).min())


def main():
    print(f"{'=' * 78}\nOWN-HOURS MULTI-PAIR DIVERSIFICATION TEST\n{'=' * 78}")
    series = {}
    print(
        f"\n{'pair':7s} {'n':>5s} {'mean(bps)':>9s} {'t':>7s} {'Sharpe':>7s} {'win%':>6s}   "
        f"{'IS_t':>6s} {'OOS_t':>6s} {'OOS_Shrp':>8s}  session(UTC)"
    )
    print("-" * 92)
    for pair, cfg in PAIRS.items():
        r = session_trades(pair)
        series[pair] = r
        st = stats(r)
        isst, oosst = split_stats(r)
        sess = f"{cfg['s']:02d}->{cfg['e']:02d} dir{cfg['d']:+d}"
        print(
            f"{pair:7s} {st['n']:5d} {st['mean_bps']:+9.3f} {st['t']:+7.2f} {st['sharpe']:+7.2f} "
            f"{st['win'] * 100:5.1f}   {isst['t']:+6.2f} {oosst['t']:+6.2f} {oosst['sharpe']:+8.2f}  {sess}"
        )

    # align into a date x pair matrix
    M = pd.DataFrame(series).sort_index()
    print("\nReturn-stream correlation (daily, overlapping days):")
    corr = M.corr()
    print(corr.round(2).to_string())
    # effective breadth: N_eff = (sum w)^2 / (w' C w) for equal weights
    C = corr.values
    n = C.shape[0]
    w = np.ones(n) / n
    n_eff = 1.0 / (w @ C @ w)
    avg_corr = (corr.values[np.triu_indices(n, 1)]).mean()
    print(f"\navg pairwise corr = {avg_corr:+.3f}   effective independent bets N_eff = {n_eff:.2f} (of {n})")

    # equal-RISK portfolio: inverse-vol weight each pair, blend available days
    vol = M.std()
    w_iv = (1.0 / vol) / (1.0 / vol).sum()
    port = (M * w_iv).sum(axis=1, min_count=1)  # daily portfolio return
    # scale so a "full" day (all pairs present) targets comparable risk
    port = port.dropna()
    pst = stats(port)
    eq = (1 + port).cumprod()
    dd = max_dd(eq)
    years = (port.index[-1] - port.index[0]).days / 365.25
    cagr = eq.iloc[-1] ** (1 / years) - 1
    # OOS portfolio
    cut = int(len(port) * 0.60)
    pst_oos = stats(port.iloc[cut:])
    eq_oos = (1 + port.iloc[cut:]).cumprod()

    best_single = max(stats(s)["sharpe"] for s in series.values())
    print(f"\n{'#' * 78}\nPORTFOLIO (inverse-vol, {n} own-hours streams)")
    print(
        f"  full-sample : Sharpe {pst['sharpe']:+.2f}   CAGR {cagr * 100:+.1f}%   maxDD {dd * 100:.1f}%   "
        f"t {pst['t']:+.2f}   n_days {pst['n']}"
    )
    print(
        f"  OOS (last40%): Sharpe {pst_oos['sharpe']:+.2f}   maxDD {max_dd(eq_oos) * 100:.1f}%   t {pst_oos['t']:+.2f}"
    )
    print(f"  best single pair Sharpe = {best_single:+.2f}   ->  portfolio lift = {pst['sharpe'] / best_single:.2f}x")
    print(f"  sqrt(N_eff) lift ceiling = {np.sqrt(n_eff):.2f}x")
    verdict = "TRADEABLE-ish" if (pst_oos["sharpe"] >= 0.8 and dd > -0.35) else "STILL THIN"
    print(f"\n  VERDICT: portfolio OOS Sharpe {pst_oos['sharpe']:+.2f}, maxDD {dd * 100:.0f}%  ->  {verdict}")
    print("#" * 78)


if __name__ == "__main__":
    main()
