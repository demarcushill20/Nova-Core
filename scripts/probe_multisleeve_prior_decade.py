"""Prior-decade pseudo-OOS test of the multi-sleeve winner book (operator question:
'will it survive a 10-year backtest?').

The book was assembled and validated on 2016-03..2026-03 (Sharpe +1.88). This probe
replays its sleeves on the DECADE BEFORE the assembly window — 2004..2015, covering the
GFC, the EUR crisis, and the zero-rate era — data the book-construction never saw.

Sleeves testable pre-2016 (BTC did not exist; gold daily unavailable pre-2010 after the
FRED LBMA series was pulled):
  eur_short : HistData EURUSD M1 2004+ (combined file is UTC-normalized by the fetch
              pipeline -> select 11:00/12:00 UTC bars directly), net of the measured
              0.30p spread (same convention as the original edge study)
  carry     : gated G10 HML from FRED (native monthly, 1982+) — reuses v2 machinery
  gold_tsmom: WORK sleeve4 PnL cache (2010+) -> joins for 2010-2015

Blends use the FROZEN live weights renormalized over available sleeves:
  2004-2015: eur+carry (79.7% of book)   2010-2015: eur+carry+gold (91.2% of book)
Bar: prior-decade blend Sharpe. >0.7 = strong survive; 0.3-0.7 = regime-sensitive but
positive; <0 = the 2016+ result is likely regime luck.
"""

from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from probe_fx_multifactor_v2 import (  # noqa: E402
    build_panel,
    carry_gate,
    excess_returns,
    hml_iv,
    monthly_vol,
)

G10 = ["USD", "EUR", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF", "NOK", "SEK"]
WEIGHTS = {"eur_short": 0.5758, "carry": 0.2214, "gold_tsmom": 0.1151}  # btc excluded pre-2016
SPREAD_PIPS = 0.30
M1 = os.path.join(ROOT, "data/candles/histdata/EURUSD_M1.csv")
GOLD_CACHE = os.path.join(ROOT, "WORK/sleeve4_gold_tsmom_daily.csv")


def eur_short_daily_from_m1() -> pd.Series:
    """11:00->12:00 UTC short from HistData M1. The combined M1 file is UTC-normalized
    by the fetch pipeline (verified: last 2024 bar is 21:58 = UTC session close, not the
    16:58 an EST-stamped file would show) -> select UTC hours directly. Net of the
    measured 0.30p round-trip spread."""
    px = pd.read_csv(M1, usecols=["time", "open"], parse_dates=["time"])
    px = px.set_index("time")["open"]
    e = px[(px.index.hour == 11) & (px.index.minute == 0)]
    x = px[(px.index.hour == 12) & (px.index.minute == 0)]
    e.index = e.index.normalize()
    x.index = x.index.normalize()
    df = pd.DataFrame({"entry": e, "exit": x}).dropna()
    ret = (df["entry"] - df["exit"] - SPREAD_PIPS * 1e-4) / df["entry"]
    return ret


def to_m(daily: pd.Series) -> pd.Series:
    return (1 + daily).groupby(daily.index.to_period("M")).prod() - 1


def ann(m: pd.Series):
    m = m.dropna()
    ar, av = m.mean() * 12, m.std() * np.sqrt(12)
    eq = (1 + m).cumprod()
    dd = ((eq - eq.cummax()) / eq.cummax()).min()
    return (ar / av if av else np.nan), ar, av, dd


def line(m: pd.Series, label: str) -> str:
    m = m.dropna()
    if len(m) < 6:
        return f"{label:42s} insufficient ({len(m)}mo)"
    sh, ar, av, dd = ann(m)
    return (
        f"{label:42s} Sharpe {sh:+.2f}  ret {ar * 100:+5.1f}%  vol {av * 100:4.1f}%  "
        f"maxDD {dd * 100:5.1f}%  ({len(m)}mo {m.index[0]}..{m.index[-1]})"
    )


def main() -> None:
    print("=" * 112)
    print("PRIOR-DECADE PSEUDO-OOS — winner book sleeves on 2004-2015 (GFC, EUR crisis, ZIRP; never seen in assembly)")
    print("=" * 112)

    eur_m = to_m(eur_short_daily_from_m1())

    S, R, SD = build_panel()
    vol = monthly_vol(SD, S.index)
    gate = carry_gate(vol, G10)
    carry_m = hml_iv(R.sub(R["USD"], axis=0), excess_returns(S, R), vol, G10, gate=gate)
    carry_m.index = carry_m.index.to_period("M")

    gold_d = pd.read_csv(GOLD_CACHE, index_col=0, parse_dates=True).iloc[:, 0]
    gold_m = to_m(gold_d)

    panel = pd.DataFrame({"eur_short": eur_m, "carry": carry_m, "gold_tsmom": gold_m})

    print("\nper-sleeve by era:")
    eras = [
        ("2004-2007 (pre-GFC)", "2004-01", "2007-12"),
        ("2008-2009 (GFC)", "2008-01", "2009-12"),
        ("2010-2015 (ZIRP/EUR crisis)", "2010-01", "2015-12"),
        ("2004-2015 (FULL prior decade)", "2004-01", "2015-12"),
        ("2016-2026 (assembly window, ref)", "2016-01", "2026-12"),
    ]
    for c in panel.columns:
        for name, a, b in eras:
            print(line(panel[c].loc[a:b], f"  {c} {name}"))  # type: ignore[misc]
        print()

    def blend(cols, a, b, label):
        sub = panel[cols].loc[a:b].dropna()
        w = pd.Series({c: WEIGHTS[c] for c in cols})
        w = w / w.sum()
        port = (sub * w).sum(axis=1)
        print(line(port, label + f"  w={dict(w.round(2))}"))
        return port

    print("blends (FROZEN live weights, renormalized over available sleeves):")
    p2 = blend(["eur_short", "carry"], "2004-01", "2015-12", "2-sleeve 2004-2015")
    blend(["eur_short", "carry"], "2008-01", "2009-12", "  2-sleeve GFC only")
    p3 = blend(["eur_short", "carry", "gold_tsmom"], "2010-01", "2015-12", "3-sleeve 2010-2015")
    blend(["eur_short", "carry", "gold_tsmom"], "2016-01", "2026-12", "3-sleeve 2016-2026 (ref)")

    # 22-year continuous 2-sleeve read
    p_all = blend(["eur_short", "carry"], "2004-01", "2026-12", "2-sleeve 2004-2026 (22yr)")

    sh2, *_ = ann(p2)
    sh3, *_ = ann(p3)
    sha, *_ = ann(p_all)
    print("\n" + "#" * 112)
    print(f"VERDICT: prior-decade 2-sleeve Sharpe {sh2:+.2f}; 2010-15 3-sleeve {sh3:+.2f}; 22yr 2-sleeve {sha:+.2f}.")
    print("  Bar: >0.7 strong survive | 0.3-0.7 regime-sensitive | <0 -> 2016+ result is regime luck.")
    print("#" * 112)


if __name__ == "__main__":
    main()
