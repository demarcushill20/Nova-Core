"""Multi-sleeve paper book — daily reconciler (measurement only, no broker orders).

Paper-trades the validated 4-sleeve book at 6x (operator-approved 2026-07-12):
  eur_short 0.58 | gated G10 carry 0.22 | gold TSMOM 0.12 | gated BTC TSMOM 0.09
Backtest reference: Sharpe +1.88 / sealed +1.96; at 6x expect ~22% CAGR, ~-13-15% daily maxDD.

DESIGN — deterministic reconciler, not an order loop:
  every run it (1) extends local price caches from public sources, (2) recomputes the
  ENTIRE paper history from inception, (3) rewrites ledger.csv + state.json, and
  (4) on Fridays writes a weekly report into OUTPUT/. Missed runs self-heal; data-source
  lag (FRED spots publish T+1..3) self-corrects on later runs. No MetaApi, no orders —
  the 11:00 sleeve's execution realism is already covered by the intraday-drift service;
  here it is measured from real Dukascopy 11:00/12:00 quotes (spread paid).

Data sources (all keyless):
  EURUSD 11:00+12:00 UTC hours + gold 20:00-21:00 UTC close hour : Dukascopy datafeed
  BTC-USD daily closes                                           : Coinbase public candles
  G10 3M rates (monthly) + daily spots for carry mark + gate     : FRED fredgraph CSV

Run:  python3 services/multisleeve-paper/multisleeve_paper.py [--inception 2026-07-01]
Env:  MSP_STATE_DIR (default data/paper_multisleeve), MSP_LEVERAGE (default 6.0)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from fetch_dukascopy_ticks import fetch_hour  # noqa: E402  (repo utility)

from novatrade.strategies.multisleeve import (  # noqa: E402
    BLEND_WEIGHTS,
    CarryBook,
    TsmomConfig,
    blend_daily_return,
    carry_daily_return,
    eur_short_daily_return,
    form_carry_book,
    ledger_stats,
    tsmom_daily_returns,
    vol_regime_gate,
)

log = logging.getLogger("multisleeve-paper")

STATE_DIR = Path(os.environ.get("MSP_STATE_DIR", str(REPO / "data" / "paper_multisleeve")))
LEVERAGE = float(os.environ.get("MSP_LEVERAGE", "6.0"))
OUTPUT_DIR = REPO / "OUTPUT"

FRED = "https://fred.stlouisfed.org/graph/fredgraph.csv?id="
RATE_ID = {
    "USD": "IR3TIB01USM156N",
    "EUR": "IR3TIB01EZM156N",
    "GBP": "IR3TIB01GBM156N",
    "JPY": "IR3TIB01JPM156N",
    "AUD": "IR3TIB01AUM156N",
    "NZD": "IR3TIB01NZM156N",
    "CAD": "IR3TIB01CAM156N",
    "CHF": "IR3TIB01CHM156N",
    "NOK": "IR3TIB01NOM156N",
    "SEK": "IR3TIB01SEM156N",
}
SPOT_ID = {
    "EUR": ("DEXUSEU", False),
    "GBP": ("DEXUSUK", False),
    "AUD": ("DEXUSAL", False),
    "NZD": ("DEXUSNZ", False),
    "JPY": ("DEXJPUS", True),
    "CAD": ("DEXCAUS", True),
    "CHF": ("DEXSZUS", True),
    "NOK": ("DEXNOUS", True),
    "SEK": ("DEXSDUS", True),
}
COINBASE = "https://api.exchange.coinbase.com/products/BTC-USD/candles"
GOLD_TSMOM = TsmomConfig(cost_bp=2.0)
BTC_TSMOM = TsmomConfig(cost_bp=5.0)
# TSMOM needs 252d of history before inception — seed caches from research artifacts
SEED_GOLD = REPO / "WORK" / "gold_daily_close_2020_2026.csv"
SEED_BTC = REPO / "WORK" / "btc_daily_close_2015_2026.csv"


# --------------------------------------------------------------------------- fetchers
def _http_get(url: str, timeout: int = 30) -> bytes:
    # FRED slow-walls non-default UAs (measured: default 0.4s, custom UA times out);
    # Coinbase requires an explicit UA. Choose per host.
    if "fred.stlouisfed.org" in url:
        return urllib.request.urlopen(url, timeout=timeout).read()  # noqa: S310 (fixed host)
    req = urllib.request.Request(url, headers={"User-Agent": "novacore-paper/1.0"})  # noqa: S310
    return urllib.request.urlopen(req, timeout=timeout).read()  # noqa: S310 (fixed hosts)


def fred_series(series_id: str, cache_dir: Path, max_age_h: float = 20.0) -> pd.Series:
    cache_dir.mkdir(parents=True, exist_ok=True)
    cpath = cache_dir / f"{series_id}.csv"
    stale = (not cpath.exists()) or (time.time() - cpath.stat().st_mtime > max_age_h * 3600)
    if stale:
        try:
            cpath.write_bytes(_http_get(FRED + series_id))
        except Exception as e:
            log.warning("FRED fetch failed %s: %s (using cache/seed)", series_id, e)
    if not cpath.exists():
        seed = REPO / "data" / "rates" / f"{series_id}.csv"  # research cache fallback
        if seed.exists():
            cpath.write_bytes(seed.read_bytes())
        else:
            raise FileNotFoundError(f"no FRED data for {series_id} (fetch failed, no seed)")
    df = pd.read_csv(cpath, na_values=".").dropna()
    df.columns = ["date", "val"]
    return pd.Series(df["val"].values, index=pd.to_datetime(df["date"])).astype(float)


def coinbase_daily(start: pd.Timestamp, end: pd.Timestamp) -> pd.Series:
    rows: list[list] = []
    t = start
    while t < end:
        e = min(t + pd.Timedelta(days=290), end)
        url = f"{COINBASE}?granularity=86400&start={t.date()}T00:00:00Z&end={e.date()}T00:00:00Z"
        rows.extend(json.loads(_http_get(url)))
        t = e
        time.sleep(0.35)
    s = pd.Series({pd.Timestamp(r[0], unit="s"): float(r[4]) for r in rows}).sort_index()
    return s[~s.index.duplicated()]


# fetch_hour hardcodes POINT=1e5 (EURUSD, 5dp). Dukascopy XAUUSD uses 1e3 (3dp),
# so gold prices come back /100 too small -> rescale by 100 to true USD/oz.
SYMBOL_SCALE = {"EURUSD": 1.0, "XAUUSD": 100.0}


# Daily reconciler pacing: few retries (self-heals tomorrow), gentle rate, and a
# global wall-clock budget so a throttled Dukascopy can never wedge the service.
DUKA_RETRIES = 2
DUKA_PACE_S = 0.5
DUKA_BUDGET_S = float(os.environ.get("MSP_DUKA_BUDGET_S", "240"))  # per source
_duka_spent = {"t": 0.0}


def _reset_duka_budget() -> None:
    """Budget is per data source (EUR hours / gold closes), not global — one slow
    source must not starve the other. Missed days self-heal on the next run."""
    _duka_spent["t"] = 0.0


def _fetch_hour_paced(symbol: str, day: pd.Timestamp, hour: int) -> list:
    if _duka_spent["t"] > DUKA_BUDGET_S:
        return []
    t0 = time.time()
    try:
        ticks = fetch_hour(
            symbol,
            datetime(day.year, day.month, day.day, hour, tzinfo=timezone.utc),
            retries=DUKA_RETRIES,
        )
    except Exception as e:
        log.warning("dukascopy %s %s %02d: %s", symbol, day.date(), hour, e)
        ticks = []
    _duka_spent["t"] += time.time() - t0
    time.sleep(DUKA_PACE_S)
    return ticks


def dukascopy_hour_quotes(symbol: str, day: pd.Timestamp, hour: int) -> tuple[float, float] | None:
    """(first_bid, first_ask) of the given UTC hour; None if no ticks (holiday/weekend)."""
    ticks = _fetch_hour_paced(symbol, day, hour)
    if not ticks:
        return None
    _, bid, ask = ticks[0]  # fetch_hour returns (epoch, bid, ask)
    k = SYMBOL_SCALE.get(symbol, 1.0)
    return float(bid) * k, float(ask) * k


def dukascopy_hour_last_mid(symbol: str, day: pd.Timestamp, hour: int) -> float | None:
    ticks = _fetch_hour_paced(symbol, day, hour)
    if not ticks:
        return None
    _, bid, ask = ticks[-1]
    return (float(bid) + float(ask)) / 2.0 * SYMBOL_SCALE.get(symbol, 1.0)


# ----------------------------------------------------------------------- price caches
def load_or_seed(cache: Path, seed: Path) -> pd.Series:
    if cache.exists():
        return pd.read_csv(cache, index_col=0, parse_dates=True).iloc[:, 0]
    if seed.exists():
        s = pd.read_csv(seed, index_col=0, parse_dates=True).iloc[:, 0]
        log.info("seeded %s from %s (%d rows)", cache.name, seed.name, len(s))
        return s
    raise FileNotFoundError(f"no cache {cache} and no seed {seed}")


def _missing_weekdays(have: pd.Index, start: pd.Timestamp, end: pd.Timestamp) -> list[pd.Timestamp]:
    """All weekdays in [start, end] not yet cached — failed days retry every run."""
    all_days = pd.date_range(start.normalize(), end.normalize(), freq="D")
    have_set = {d.normalize() for d in have}
    return [d for d in all_days if d.weekday() < 5 and d not in have_set]


def update_gold_cache(today: pd.Timestamp) -> pd.Series:
    cache = STATE_DIR / "gold_daily_close.csv"
    s = load_or_seed(cache, SEED_GOLD)
    _reset_duka_budget()
    for day in _missing_weekdays(s.index, s.index[-1] + timedelta(days=1), today):
        # session close drifts with US DST (21:00 or 22:00 UTC); take the last
        # non-empty hour among the plausible close hours
        for close_hour in (21, 20, 19):
            mid = dukascopy_hour_last_mid("XAUUSD", day, close_hour)
            if mid is not None:
                s.loc[day] = mid
                s.sort_index().to_frame("close").to_csv(cache)  # incremental persist
                break
    s = s.sort_index()
    s.to_frame("close").to_csv(cache)
    return s


def update_btc_cache(today: pd.Timestamp) -> pd.Series:
    cache = STATE_DIR / "btc_daily_close.csv"
    s = load_or_seed(cache, SEED_BTC)
    if s.index[-1] < today:
        fresh = coinbase_daily(s.index[-1] - pd.Timedelta(days=3), today + pd.Timedelta(days=1))
        s = pd.concat([s[s.index < fresh.index[0]], fresh]).sort_index()
    s.to_frame("close").to_csv(cache)
    return s


def update_eur_hours_cache(today: pd.Timestamp, inception: pd.Timestamp) -> pd.DataFrame:
    """Per-day 11:00/12:00 UTC bid+ask for the eur_short mark."""
    cache = STATE_DIR / "eur_1100_1200_quotes.csv"
    df = pd.read_csv(cache, index_col=0, parse_dates=True) if cache.exists() else pd.DataFrame()
    if df.empty or "bid11" not in df.columns:  # guard: header-only/corrupt cache
        df = pd.DataFrame()
    _reset_duka_budget()
    have = df.index if not df.empty else pd.DatetimeIndex([])
    for day in _missing_weekdays(have, inception, today):
        q11 = dukascopy_hour_quotes("EURUSD", day, 11)
        q12 = dukascopy_hour_quotes("EURUSD", day, 12)
        if q11 and q12:
            df.loc[day, ["bid11", "ask11", "bid12", "ask12"]] = [*q11, *q12]
            df.sort_index().to_csv(cache)  # incremental persist
    df = df.sort_index()
    df.to_csv(cache)
    return df


# ------------------------------------------------------------------------- reconcile
def build_ledger(inception: pd.Timestamp, today: pd.Timestamp) -> pd.DataFrame:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    fred_dir = STATE_DIR / "fred"

    # EUR first — highest-weight sleeve gets first claim on Dukascopy goodwill
    eur = update_eur_hours_cache(today, inception)
    gold = update_gold_cache(today)
    btc = update_btc_cache(today)

    rates = pd.DataFrame({c: fred_series(sid, fred_dir).resample("ME").last() for c, sid in RATE_ID.items()}).ffill()
    spots = pd.DataFrame(
        {
            c: (1.0 / fred_series(sid, fred_dir) if inv else fred_series(sid, fred_dir))
            for c, (sid, inv) in SPOT_ID.items()
        }
    ).sort_index()
    gate_m = vol_regime_gate(spots)

    gold_r = tsmom_daily_returns(gold, GOLD_TSMOM)
    btc_r_raw = tsmom_daily_returns(btc, BTC_TSMOM)
    spot_ret_d = spots.pct_change(fill_method=None)
    vols_d = spot_ret_d.rolling(63, min_periods=40).std() * np.sqrt(252)

    rows = []
    book: CarryBook | None = None
    book_month: pd.Period | None = None
    for day in pd.date_range(inception, today, freq="D"):
        month = day.to_period("M")
        # (re)form the carry book at each new month from last-known data
        if book_month != month:
            r_known = rates[rates.index < day.normalize()]
            g_known = gate_m[gate_m.index < day.normalize()]
            v_known = vols_d[vols_d.index < day.normalize()]
            if len(r_known) and len(v_known):
                gate_val = float(g_known.iloc[-1]) if len(g_known) and np.isfinite(g_known.iloc[-1]) else 1.0
                book = form_carry_book(r_known.iloc[-1], v_known.iloc[-1], gate_val)
            book_month = month

        sleeves: dict[str, float] = {}
        if day in eur.index and not eur.loc[day].isna().any():
            q = eur.loc[day]
            sleeves["eur_short"] = eur_short_daily_return(q.bid11, q.ask11, q.bid12, q.ask12)
        if book is not None and book.weights and day in spot_ret_d.index:
            rk = rates[rates.index < day]
            if len(rk):
                last = rk.iloc[-1]
                rd = last - last["USD"]
                sleeves["carry"] = carry_daily_return(book, spot_ret_d.loc[day], rd)
        if day in gold_r.index:
            sleeves["gold_tsmom"] = float(gold_r.loc[day])
        if day in btc_r_raw.index:
            g_known = gate_m[gate_m.index < day.normalize()]
            gate_val = float(g_known.iloc[-1]) if len(g_known) and np.isfinite(g_known.iloc[-1]) else 1.0
            sleeves["btc_gated"] = float(btc_r_raw.loc[day]) * gate_val

        if not sleeves and day.weekday() >= 5:
            continue  # silent weekend with no BTC data point
        rows.append(
            {
                "date": day.date().isoformat(),
                **{f"r_{k}": sleeves.get(k, np.nan) for k in BLEND_WEIGHTS},
                "r_blend_6x": blend_daily_return(sleeves, LEVERAGE),
            }
        )
    return pd.DataFrame(rows).set_index("date")


def write_outputs(ledger: pd.DataFrame, today: pd.Timestamp) -> None:
    ledger.to_csv(STATE_DIR / "ledger.csv")
    blend = pd.Series(ledger["r_blend_6x"].values, index=pd.to_datetime(ledger.index))
    nav = float((1 + blend.fillna(0.0)).prod())
    stats = ledger_stats(blend)
    state = {
        "as_of": today.date().isoformat(),
        "leverage": LEVERAGE,
        "weights": BLEND_WEIGHTS,
        "nav": nav,
        "stats": stats,
        "backtest_ref": {
            "sharpe_full": 1.88,
            "sharpe_sealed": 1.96,
            "expected_daily_mean_6x": 0.00088,
            "expected_daily_vol_6x": 0.0075,
        },
    }
    (STATE_DIR / "state.json").write_text(json.dumps(state, indent=1))
    log.info("ledger %d days | NAV %.4f | stats %s", len(ledger), nav, stats)

    if today.weekday() == 4:  # Friday -> weekly report
        ts = today.strftime("%Y%m%d")
        lines = [
            f"# Multi-sleeve paper book — weekly report {today.date()}",
            "",
            f"Leverage 6x | weights {BLEND_WEIGHTS} | inception {ledger.index[0]}",
            f"NAV: **{nav:.4f}**  ({(nav - 1) * 100:+.2f}% cum)",
            f"Stats: {json.dumps(stats)}",
            "",
            "Backtest reference (should bracket realized): daily mean ~+0.088bp*6, "
            "vol ~0.75%/day at 6x; sealed Sharpe +1.96.",
            "",
            "Last 5 sessions:",
            ledger.tail(5).to_string(),
        ]
        (OUTPUT_DIR / f"multisleeve_paper_weekly_{ts}.md").write_text("\n".join(lines))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--inception", default="2026-07-01")
    args = ap.parse_args()
    inception = pd.Timestamp(args.inception)
    today = pd.Timestamp(datetime.now(timezone.utc).date()) - pd.Timedelta(days=1)  # complete days only
    ledger = build_ledger(inception, today)
    write_outputs(ledger, pd.Timestamp(datetime.now(timezone.utc).date()))


if __name__ == "__main__":
    main()
