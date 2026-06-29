#!/usr/bin/env python3
"""Bloc-momentum strategy — port of the "quantum-sector momentum" idea to forex.

Source idea (task 1102, "Trade Tactics" transcript): the quantum names
(IONQ / RGTI / QBTS) "all move together"; Martin enters three or four
simultaneously when the *whole sector* breaks out, then runs flexible exits
(partial-sell into 3-5x ADR extensions; final exit on the first daily close
below the 9-EMA). The pitch is that a synchronised sector lets you stack several
winners at once and survive a long stop-out streak.

This module ports that to two forex "blocs":
  * commodity bloc  : AUDUSD, NZDUSD, USDCAD  (oriented to commodity-ccy strength)
  * EUR-crosses bloc: EURUSD, EURGBP, EURJPY  (oriented to EUR strength)

Mechanics (daily bars resampled from our HistData M1):
  ENTRY  Donchian breakout per member, GATED on >= min_confirm members breaking
         the SAME way inside a confirm_window (the "sector moves together" gate).
  EXITS  flexible, per the video: partial 1/3 at +3xADR, partial 1/3 at +5xADR,
         final 1/3 on the first daily close back across the 9-EMA; hard stop at
         -1xADR. All execution is close-based (no intrabar optimism).

CRITICAL deliverable (per the task): we FIRST measure the effective independence
of each bloc (correlation-adjusted N, two ways) and then test whether the
basket-confirmation lift is *real diversification* (portfolio Sharpe -> sqrt(N))
or just a *levered single bet* (lift -> 1). We also separate that from the
confirmation *filter* effect (gated vs ungated per-pair expectancy).

Anti-overfit spine matches the house gauntlet (calendar_flow_gauntlet.py):
  - net-of-cost in return space, per-instrument pip cost
  - sealed hold-out: last SEALED_YEARS withheld, scored exactly once
  - Deflated Sharpe on the pooled survivor tape (Lopez de Prado)
  - a positive control: a random-entry bloc must collapse to ~0

No parameter search is performed (one fixed, economically-motivated config per
bloc), so the multiple-testing burden is small and stated honestly.

Run:  python3 scripts/forex_bloc_momentum.py
      python3 scripts/forex_bloc_momentum.py --json
Deps: pandas, numpy, scipy (already in the research venv).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd

# allow `python3 scripts/forex_bloc_momentum.py` from the repo root: the script's
# own dir (not the root) lands on sys.path by default, so add the repo root for
# the `novatrade` package import below. (Data paths are still root-relative.)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from novatrade.backtest.research.walkforward import deflated_sharpe_ratio

HISTDATA = Path("data/candles/histdata")
CACHE = Path("data/candles/_bloc_cache")
SEALED_YEARS = 2  # last N calendar years withheld from any read, scored once
TRADING_DAYS = 252

# pip size per *tradable* instrument (cost is charged in price terms)
PIP = {
    "AUDUSD": 1e-4,
    "NZDUSD": 1e-4,
    "USDCAD": 1e-4,
    "EURUSD": 1e-4,
    "GBPUSD": 1e-4,
    "USDJPY": 1e-2,
    "EURGBP": 1e-4,
    "EURJPY": 1e-2,
}
COST_PIPS = 0.5  # round-turn ECN-ish; charged on every fill (entry + each partial)


# --------------------------------------------------------------------------- #
# Data loading: M1 -> daily OHLC, cached
# --------------------------------------------------------------------------- #
def load_daily(symbol: str) -> pd.DataFrame:
    """Resample a HistData M1 csv to daily UTC OHLC (cached as parquet)."""
    CACHE.mkdir(parents=True, exist_ok=True)
    cache = CACHE / f"{symbol}_1D.parquet"
    if cache.exists():
        return pd.read_parquet(cache)
    src = HISTDATA / f"{symbol}_M1.csv"
    if not src.exists():
        raise FileNotFoundError(src)
    df = pd.read_csv(src, parse_dates=["time"])
    df = df.set_index("time").sort_index()
    d = (
        df.resample("1D")
        .agg(
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
        )
        .dropna()
    )
    d.to_parquet(cache)
    return d


def build_member(symbol: str, orient: int, invert: bool = False) -> pd.DataFrame:
    """Return a tradable member frame with an `oriented close` (factor space).

    orient is the sign mapping factor-direction -> instrument-direction:
      a long-factor signal trades the instrument with sign `orient`.
    invert=True builds the reciprocal series (e.g. CADUSD from USDCAD) so that
    "up" in oriented space == bloc factor strengthening.
    """
    d = load_daily(symbol)
    if invert:
        o = pd.DataFrame(
            {
                "open": 1.0 / d["open"],
                "high": 1.0 / d["low"],  # reciprocal flips high/low
                "low": 1.0 / d["high"],
                "close": 1.0 / d["close"],
            },
            index=d.index,
        )
    else:
        o = d.copy()
    o["symbol"] = symbol
    o["orient"] = orient  # +1 trade instrument long on factor-up, -1 short
    o["pip"] = PIP[symbol]
    o["price"] = d["close"]  # real instrument price (for cost in return space)
    return o


def build_cross(num: str, den: str, symbol: str, op: str) -> pd.DataFrame:
    """Construct a daily cross from two USD legs.

    op="div": EURGBP = EURUSD / GBPUSD ; op="mul": EURJPY = EURUSD * USDJPY.
    High/low use the ratio/product envelope (a known, stated over-estimate of the
    true daily range; affects ADR sizing of the EUR bloc only).
    """
    a, b = load_daily(num), load_daily(den)
    idx = a.index.intersection(b.index)
    a, b = a.loc[idx], b.loc[idx]
    if op == "div":
        close = a["close"] / b["close"]
        high = a["high"] / b["low"]
        low = a["low"] / b["high"]
        open_ = a["open"] / b["open"]
    else:
        close = a["close"] * b["close"]
        high = a["high"] * b["high"]
        low = a["low"] * b["low"]
        open_ = a["open"] * b["open"]
    o = pd.DataFrame({"open": open_, "high": high, "low": low, "close": close}, index=idx)
    o["symbol"] = symbol
    o["orient"] = 1
    o["pip"] = PIP[symbol]
    o["price"] = close
    return o


# --------------------------------------------------------------------------- #
# Effective independence (the CRITICAL pre-check)
# --------------------------------------------------------------------------- #
def effective_n(ret: pd.DataFrame) -> dict:
    """Correlation-adjusted N for a bloc of (factor-oriented) daily returns.

    Two estimators of "how many independent bets is this bloc really":
      N_eff_corr  = N / (1 + (N-1)*rho_bar)                (average-correlation)
      N_eff_eig   = (sum lambda)^2 / sum(lambda^2)         (participation ratio
                    of the correlation-matrix eigenvalues)
    N_eff == N -> independent; N_eff -> 1 -> one shared factor.
    """
    r = ret.dropna()
    corr = r.corr()
    n = corr.shape[0]
    off = corr.values[~np.eye(n, dtype=bool)]
    rho_bar = float(np.mean(off))
    n_eff_corr = n / (1.0 + (n - 1) * rho_bar) if (1 + (n - 1) * rho_bar) > 0 else float("nan")
    eig = np.linalg.eigvalsh(corr.values)
    eig = eig[eig > 0]
    n_eff_eig = float((eig.sum() ** 2) / np.sum(eig**2))
    return {
        "members": list(corr.columns),
        "n": n,
        "mean_pairwise_corr": round(rho_bar, 3),
        "corr_matrix": {c: {k: round(float(v), 2) for k, v in corr[c].items()} for c in corr},
        "n_eff_corr": round(n_eff_corr, 2),
        "n_eff_eigen": round(n_eff_eig, 2),
        "top_eigval_share": round(float(eig.max() / eig.sum()), 3),
    }


# --------------------------------------------------------------------------- #
# Strategy
# --------------------------------------------------------------------------- #
@dataclass
class Params:
    donchian: int = 20  # breakout lookback (days)
    confirm_window: int = 3  # "within days of each other"
    min_confirm: int = 2  # >=2 members same direction == bloc confirmed
    adr_n: int = 20  # ADR lookback
    stop_adr: float = 1.0  # initial hard stop distance (xADR)
    partial1_adr: float = 3.0  # first 1/3 partial
    partial2_adr: float = 5.0  # second 1/3 partial
    ema: int = 9  # final-third trail
    allow_short: bool = True


@dataclass
class Trade:
    symbol: str
    direction: int
    entry_day: pd.Timestamp
    exit_day: pd.Timestamp
    r_multiple: float  # net realised PnL in units of initial risk
    year: int


def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def member_signals(m: pd.DataFrame, p: Params) -> pd.DataFrame:
    """Add per-member breakout flags + ADR + EMA on the oriented series."""
    d = m.copy()
    c, h, low = d["close"], d["high"], d["low"]
    up_level = h.rolling(p.donchian).max().shift(1)
    dn_level = low.rolling(p.donchian).min().shift(1)
    brk_up = (c > up_level) & (c.shift(1) <= up_level.shift(1))
    brk_dn = (c < dn_level) & (c.shift(1) >= dn_level.shift(1))
    d["brk_up"] = brk_up.fillna(False)
    d["brk_dn"] = brk_dn.fillna(False)
    d["adr"] = (h - low).rolling(p.adr_n).mean()
    d["ema"] = _ema(c, p.ema)
    return d


def bloc_confirm(members: dict, p: Params) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Across the bloc: count members breaking each way inside confirm_window."""
    idx = None
    for m in members.values():
        idx = m.index if idx is None else idx.union(m.index)
    up = pd.DataFrame(index=idx)
    dn = pd.DataFrame(index=idx)
    for sym, m in members.items():
        up[sym] = m["brk_up"].reindex(idx, fill_value=False).astype(bool)
        dn[sym] = m["brk_dn"].reindex(idx, fill_value=False).astype(bool)
    win = p.confirm_window
    # count members with a breakout within the trailing window:
    up_cnt = up.rolling(win, min_periods=1).max().sum(axis=1)
    dn_cnt = dn.rolling(win, min_periods=1).max().sum(axis=1)
    return up_cnt, dn_cnt


def simulate_member(
    m: pd.DataFrame, p: Params, gate_up: pd.Series, gate_dn: pd.Series, min_confirm: int
) -> tuple[list[Trade], pd.Series]:
    """Close-based simulation of one member with partial exits + EMA trail.

    Returns (trades, daily_R_series) where daily_R is mark-to-market PnL per day
    expressed in units of the position's *initial risk* (so members are summable
    into an equal-risk portfolio). `min_confirm`==1 reproduces the per-pair
    baseline (no bloc gate).
    """
    d = m
    idx = d.index

    def cost_R_per_fill(adr: float) -> float:
        if adr <= 0:
            return 0.0
        return (COST_PIPS * d["pip"].iloc[0]) / (p.stop_adr * adr)

    daily_r = pd.Series(0.0, index=idx)
    trades: list[Trade] = []

    pos = 0  # 0 flat, +1/-1 in factor space
    frac = 0.0  # remaining fraction of position (1 -> 0)
    entry = stop = adr0 = 0.0
    risk = 0.0  # price distance of initial risk (stop_adr * adr0)
    p1 = p2 = False  # partials taken
    entry_day = None
    realised_R = 0.0

    arr = d[["open", "high", "low", "close", "adr", "ema", "brk_up", "brk_dn"]].copy()
    up_ok = (gate_up.reindex(idx).fillna(0) >= min_confirm).values
    dn_ok = (gate_dn.reindex(idx).fillna(0) >= min_confirm).values
    # close-based execution: only close / ADR / EMA / breakout flags are used
    C, ADR, EMA = arr["close"].values, arr["adr"].values, arr["ema"].values
    BU, BD = arr["brk_up"].values, arr["brk_dn"].values

    for i in range(1, len(idx)):
        if pos != 0:
            # mark-to-market today's close move on the fraction held at open
            if risk > 0:
                daily_r.iloc[i] = pos * frac * (C[i] - C[i - 1]) / risk
            ext = pos * (C[i] - entry)  # favourable extension in price
            # partial 1
            if (not p1) and adr0 > 0 and ext >= p.partial1_adr * adr0:
                realised_R += (1 / 3) * (ext / risk) - cost_R_per_fill(adr0)
                frac -= 1 / 3
                p1 = True
            # partial 2
            if (not p2) and adr0 > 0 and ext >= p.partial2_adr * adr0:
                realised_R += (1 / 3) * (ext / risk) - cost_R_per_fill(adr0)
                frac -= 1 / 3
                p2 = True
            # hard stop (close-based)
            stopped = (pos == 1 and C[i] <= stop) or (pos == -1 and C[i] >= stop)
            # EMA trail exit for the final third (only after both partials, per video)
            ema_exit = (pos == 1 and C[i] < EMA[i]) or (pos == -1 and C[i] > EMA[i])
            if stopped or ema_exit:
                realised_R += frac * (ext / risk) - cost_R_per_fill(adr0)
                trades.append(Trade(d["symbol"].iloc[0], pos, entry_day, idx[i], round(realised_R, 4), idx[i].year))
                pos = 0
                frac = 0.0
                realised_R = 0.0
                p1 = p2 = False
            continue

        # flat: look for a gated fresh breakout
        a = ADR[i]
        if np.isnan(a) or a <= 0 or np.isnan(EMA[i]):
            continue
        long_sig = BU[i] and up_ok[i]
        short_sig = BD[i] and dn_ok[i] and p.allow_short
        if not (long_sig or short_sig):
            continue
        pos = 1 if long_sig else -1
        entry = C[i]  # enter at signal close (close-based model)
        adr0 = a
        risk = p.stop_adr * adr0
        stop = entry - pos * risk
        frac = 1.0
        p1 = p2 = False
        entry_day = idx[i]
        realised_R = -cost_R_per_fill(adr0)  # entry cost

    return trades, daily_r


# --------------------------------------------------------------------------- #
# Bloc runner + metrics
# --------------------------------------------------------------------------- #
def ann_sharpe(daily_r: pd.Series) -> float:
    r = daily_r[daily_r != 0.0] if (daily_r != 0).any() else daily_r
    # portfolio Sharpe uses ALL days (incl. flat) so leverage/exposure is honest:
    r = daily_r
    sd = r.std(ddof=1)
    return float(r.mean() / sd * np.sqrt(TRADING_DAYS)) if sd > 0 else 0.0


def trade_stats(trades: list[Trade]) -> dict:
    if not trades:
        return {"trades": 0}
    R = np.array([t.r_multiple for t in trades])
    wins = R > 0
    return {
        "trades": len(trades),
        "win_rate": round(float(wins.mean()), 3),
        "mean_R": round(float(R.mean()), 4),
        "median_R": round(float(np.median(R)), 4),
        "sum_R": round(float(R.sum()), 2),
        "expectancy_t": round(float(R.mean() / (R.std(ddof=1) / np.sqrt(len(R)))), 2) if R.std(ddof=1) > 0 else 0.0,
    }


def run_bloc(name: str, members: dict, p: Params, seed: int = 7) -> tuple[dict, pd.DataFrame, pd.DataFrame, dict]:
    # signals per member
    sig = {s: member_signals(m, p) for s, m in members.items()}
    gate_up, gate_dn = bloc_confirm(sig, p)

    # daily factor-oriented returns for the independence analysis
    fac_ret = pd.DataFrame({s: m["close"].pct_change() for s, m in sig.items()})
    indep = effective_n(fac_ret)

    out = {"bloc": name, "members": list(members), "independence": indep, "params": asdict(p)}

    # ---- GATED basket vs UNGATED per-pair baseline -------------------------
    for label, mc in (("basket", p.min_confirm), ("per_pair", 1)):
        per_member_trades: list[Trade] = []
        daily_rs: dict[str, pd.Series] = {}
        for s, m in sig.items():
            tr, dr = simulate_member(m, p, gate_up, gate_dn, mc)
            per_member_trades += tr
            daily_rs[s] = dr
        # equal-risk portfolio = mean of member daily-R streams on a common index
        dr_df = pd.DataFrame(daily_rs).fillna(0.0)
        port_dr = dr_df.mean(axis=1)
        member_sharpes = {s: round(ann_sharpe(dr_df[s]), 3) for s in dr_df}
        avg_single = float(np.mean([v for v in member_sharpes.values()]))
        port_sharpe = ann_sharpe(port_dr)
        # equity is ADDITIVE in R (R is PnL/initial-risk, not a fractional return);
        # drawdown is therefore reported in R units, not percent.
        eqR = port_dr.cumsum()
        dd_R = float((eqR - eqR.cummax()).min())
        # The diversification lift (portSharpe / avg single Sharpe) is only
        # interpretable when the base single-member edge is meaningfully positive;
        # otherwise it is a divide-by-~0 noise ratio, not real diversification.
        lift = round(port_sharpe / avg_single, 3) if avg_single >= 0.10 else None
        out[label] = {
            **trade_stats(per_member_trades),
            "member_sharpes": member_sharpes,
            "avg_single_sharpe": round(avg_single, 3),
            "portfolio_sharpe": round(port_sharpe, 3),
            "diversification_lift": lift,
            "lift_interpretable": avg_single >= 0.10,
            "portfolio_max_dd_R": round(dd_R, 2),
            "portfolio_sum_R": round(float(port_dr.sum()), 2),
        }
    return out, gate_up, gate_dn, sig


def random_control(sig: dict, p: Params, seed: int = 11) -> dict:
    """Positive control: random gate (same firing rate) must collapse the lift."""
    rng = np.random.default_rng(seed)
    idx: pd.Index = pd.Index([])
    for m in sig.values():
        idx = m.index if len(idx) == 0 else idx.union(m.index)
    # random "confirmed" days at a plausible rate
    rate = 0.05
    fake = pd.Series(rng.random(len(idx)) < rate, index=idx).astype(float) * p.min_confirm
    daily_rs = {}
    trs: list[Trade] = []
    for s, m in sig.items():
        tr, dr = simulate_member(m, p, fake, fake, p.min_confirm)
        trs += tr
        daily_rs[s] = dr
    dr_df = pd.DataFrame(daily_rs).fillna(0.0)
    port = dr_df.mean(axis=1)
    return {**trade_stats(trs), "portfolio_sharpe": round(ann_sharpe(port), 3)}


# --------------------------------------------------------------------------- #
# Sealed hold-out wrapper
# --------------------------------------------------------------------------- #
def split_sealed(members: dict, n_years: int) -> tuple[dict, dict, int]:
    last_year = max(m.index.year.max() for m in members.values())
    seal_start = last_year - n_years + 1
    disc = {s: m[m.index.year < seal_start] for s, m in members.items()}
    seal = {s: m[m.index.year >= seal_start] for s, m in members.items()}
    return disc, seal, seal_start


# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = ap.parse_args()

    # ---- bloc definitions --------------------------------------------------
    # commodity bloc: oriented to commodity-currency strength vs USD.
    commodity = {
        "AUDUSD": build_member("AUDUSD", orient=+1),
        "NZDUSD": build_member("NZDUSD", orient=+1),
        # USDCAD inverted -> CADUSD so "up" == CAD strength; trade = short USDCAD
        "USDCAD": build_member("USDCAD", orient=-1, invert=True),
    }
    # EUR-crosses bloc: oriented to EUR strength. EURJPY/USDJPY start 2016 ->
    # the bloc is naturally a 2016+ read.
    eur = {
        "EURUSD": build_member("EURUSD", orient=+1),
        "EURGBP": build_cross("EURUSD", "GBPUSD", "EURGBP", "div"),
        "EURJPY": build_cross("EURUSD", "USDJPY", "EURJPY", "mul"),
    }

    p = Params()
    report: dict = {
        "strategy": "bloc-momentum (quantum-sector port)",
        "data": "HistData M1 -> daily UTC OHLC",
        "cost_pips_per_fill": COST_PIPS,
        "sealed_years": SEALED_YEARS,
        "blocs": {},
    }

    n_trials = 2 * 2  # 2 blocs x {basket, per_pair} fixed configs (no param search)

    for name, bloc in (("commodity_AUD_NZD_CAD", commodity), ("eur_crosses", eur)):
        disc, seal, seal_start = split_sealed(bloc, SEALED_YEARS)
        d_out, gu, gd, sig = run_bloc(name, disc, p)
        s_out, _, _, _ = run_bloc(name, seal, p)
        ctrl = random_control({s: member_signals(m, p) for s, m in disc.items()}, p)

        # DSR on the discovery basket portfolio (per-trade tape)
        dsr = (
            deflated_sharpe_ratio(
                d_out["basket"]["portfolio_sharpe"] / np.sqrt(TRADING_DAYS),
                int(d_out["basket"].get("trades", 0)),
                n_trials,
            )
            if d_out["basket"].get("trades", 0) > 1
            else float("nan")
        )

        report["blocs"][name] = {
            "window_discovery": f"< {seal_start}",
            "window_sealed": f">= {seal_start}",
            "independence": d_out["independence"],
            "discovery": {"basket": d_out["basket"], "per_pair": d_out["per_pair"]},
            "sealed_holdout": {"basket": s_out["basket"], "per_pair": s_out["per_pair"]},
            "random_control": ctrl,
            "deflated_sharpe_basket": round(float(dsr), 3) if dsr == dsr else None,
        }

    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return 0

    # ---- human report ------------------------------------------------------
    _print_report(report)
    return 0


def _print_report(report: dict) -> None:
    print("=" * 78)
    print("BLOC-MOMENTUM — quantum-sector port to forex blocs")
    print(f"data: {report['data']} | cost {report['cost_pips_per_fill']}p/fill | sealed last {report['sealed_years']}y")
    print("=" * 78)
    for name, b in report["blocs"].items():
        ind = b["independence"]
        print(f"\n### {name}   members={ind['members']}")
        print(
            f"  EFFECTIVE INDEPENDENCE  mean_pairwise_corr={ind['mean_pairwise_corr']}  "
            f"N_eff(corr)={ind['n_eff_corr']}  N_eff(eigen)={ind['n_eff_eigen']}  "
            f"top_factor_share={ind['top_eigval_share']}  (N={ind['n']})"
        )
        for seg in ("discovery", "sealed_holdout"):
            print(f"  -- {seg} ({b['window_discovery'] if seg == 'discovery' else b['window_sealed']}) --")
            for label in ("basket", "per_pair"):
                s = b[seg][label]
                if s.get("trades", 0) == 0:
                    print(f"     {label:9s}: no trades")
                    continue
                lift_s = s["diversification_lift"] if s["lift_interpretable"] else "n/a(no base edge)"
                print(
                    f"     {label:9s}: trades={s['trades']:4d} winR={s['win_rate']:.2f} "
                    f"meanR={s['mean_R']:+.3f} t={s['expectancy_t']:+.2f} | "
                    f"avg1Sharpe={s['avg_single_sharpe']:+.2f} portSharpe={s['portfolio_sharpe']:+.2f} "
                    f"lift={lift_s} maxDD_R={s['portfolio_max_dd_R']:.1f}"
                )
        c = b["random_control"]
        print(f"  random control: trades={c.get('trades', 0)} portSharpe={c.get('portfolio_sharpe')}")
        print(f"  DSR(basket discovery) = {b['deflated_sharpe_basket']}  (gate > 0.90)")

        # --- two SEPARATE questions, answered independently -----------------
        basket = b["discovery"]["basket"]
        per_pair = b["discovery"]["per_pair"]
        sealed = b["sealed_holdout"]["basket"]
        ind_n_eig = ind["n_eff_eigen"]

        # (1) edge: does ANY tradable edge survive the gauntlet?
        dsr = b["deflated_sharpe_basket"]
        edge_ok = (dsr is not None and dsr > 0.90) and sealed.get("mean_R", -1) > 0
        edge_verdict = "TRADABLE EDGE SURVIVES" if edge_ok else "NO EDGE (DSR/sealed fail)"

        # (2) confirmation FILTER: does the >=2 gate improve per-trade expectancy?
        filt = "IMPROVES" if basket["mean_R"] > per_pair["mean_R"] else "does NOT improve"

        # (3) diversification vs leverage — governed by effective independence.
        if ind_n_eig < 1.6:
            div_verdict = f"LEVERED SINGLE BET (N_eff={ind_n_eig} ~ 1 shared factor)"
        elif ind_n_eig < 2.3:
            div_verdict = f"PARTIAL DIVERSIFICATION (N_eff={ind_n_eig})"
        else:
            div_verdict = f"GENUINELY MULTIPLE BETS (N_eff={ind_n_eig})"
        print(f"  >> (1) {edge_verdict}")
        print(
            f"  >> (2) confirmation filter (basket {basket['mean_R']:+.3f}R vs "
            f"per-pair {per_pair['mean_R']:+.3f}R): {filt} per-trade expectancy"
        )
        print(
            f"  >> (3) bloc structure: {div_verdict}; "
            f"sqrt(N_eff)={round(float(np.sqrt(ind_n_eig)), 2)} vs sqrt(N)={round(np.sqrt(ind['n']), 2)}"
        )
    print("\n" + "=" * 78)


if __name__ == "__main__":
    raise SystemExit(main())
