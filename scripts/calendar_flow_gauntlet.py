#!/usr/bin/env python3
"""Tier 1A — Calendar/flow seasonality gauntlet (multi-instrument).

Generalizes scripts/backtest_1048_seasonal_drift.py (the study that validated the
11:00 UTC EURUSD short) from a single EURUSD weekday×hour scan into a
calendar-key-agnostic, multi-instrument harvester. The anti-overfit spine is
UNCHANGED — only the bucketing key, the instrument loop, a Deflated-Sharpe gate,
and a sealed hold-out are added.

Gauntlet (per family × instrument):
  1. WALK-FORWARD: select cells on prior years (anchored expanding), trade OOS.
  2. MULTIPLE-TESTING: Benjamini-Hochberg FDR (q<=0.10) on in-sample t-stats.
  3. PERSISTENCE: cell must be FDR-eligible + stable-signed in >=K/N folds.
  4. NET-OF-COST: per-instrument cost in return space (cost_pips * pip / price).
Then, across the whole sweep:
  5. CROSS-INSTRUMENT REPLICATION: a real flow effect repeats on >=2 instruments
     with the same sign (our strongest, cheapest filter against noise).
  6. DEFLATED SHARPE: discount the pooled survivor Sharpe for n_trials = every
     cell tested across all families × instruments (López de Prado).
  7. SEALED HOLD-OUT: the last 2 years are excluded from discovery and scored
     exactly once at the end.

POSITIVE CONTROL: the known EURUSD hourly 11:00 UTC short must re-emerge from the
'hourly' family on EURUSD. If it doesn't, the harness is broken — abort the read.

Data: data/candles/{symbol}_15m.parquet (UTC), resampled per family.
Usage: python3 scripts/calendar_flow_gauntlet.py
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

from novatrade.backtest.research.walkforward import deflated_sharpe_ratio

CANDLES = Path("data/candles")
COST_PIPS = 0.5
FDR_Q = 0.10
PERSIST_K = 4
SEALED_YEARS = 2  # last N calendar years withheld from discovery, scored once

# pip size per instrument (return-space cost = cost_pips * pip / price)
PIP = {
    "eurusd": 1e-4,
    "gbpusd": 1e-4,
    "audusd": 1e-4,
    "usdcad": 1e-4,
    "nzdusd": 1e-4,
    "audnzd": 1e-4,
    "usdjpy": 1e-2,
    "xauusd": 1e-2,
}
INSTRUMENTS = list(PIP.keys())


@dataclass(frozen=True)
class Family:
    name: str
    tf: str  # pandas resample rule for the decision/return bar
    hold: int  # bars held (entry close -> +hold close)
    keycols: tuple[str, ...]
    mechanism: str


FAMILIES = [
    Family("hourly", "1h", 1, ("weekday", "hour"), "session/fix flow by hour-of-day"),
    Family("dow", "1D", 1, ("weekday",), "weekend-risk positioning by weekday"),
    Family("monthend", "1D", 1, ("bme_bucket",), "month-end rebalancing / fix flow"),
    Family("tom", "1D", 1, ("dom_bucket",), "turn-of-month fund inflows"),
]


def load_close(symbol: str, tf: str) -> pd.Series:
    df = pd.read_parquet(CANDLES / f"{symbol}_15m.parquet")
    if "time" in df.columns:
        df = df.set_index("time")
    df.index = pd.to_datetime(df.index, utc=True)
    return df["close"].resample(tf, label="right", closed="right").last().dropna()


def build_frame(symbol: str, fam: Family) -> pd.DataFrame:
    s = load_close(symbol, fam.tf)
    f = pd.DataFrame({"close": s})
    f["ret"] = s.shift(-fam.hold) / s - 1.0
    f = f.dropna(subset=["ret"])
    idx = f.index
    f["weekday"] = idx.weekday
    f["hour"] = idx.hour
    f["year"] = idx.year
    f = f[f["weekday"] < 5].copy()  # drop illiquid weekend stubs
    # derived calendar keys
    if "bme_bucket" in fam.keycols:
        ym = f.index.tz_localize(None).to_period("M")
        pos = f.groupby(ym).cumcount()
        mx = pos.groupby(ym).transform("max")
        bme = (mx - pos).to_numpy()
        f["bme_bucket"] = np.where(bme <= 2, np.char.add("me", bme.astype(str)), "mid")
    if "dom_bucket" in fam.keycols:
        dom = f.index.day
        f["dom_bucket"] = np.where(dom <= 3, np.char.add("d", dom.astype(str)), "mid")
    f["key"] = list(zip(*[f[c] for c in fam.keycols], strict=False))
    f["cost_ret"] = COST_PIPS * PIP[symbol] / f["close"]
    return f


def benjamini_hochberg(pvals: np.ndarray, q: float) -> np.ndarray:
    n = len(pvals)
    if n == 0:
        return np.zeros(0, dtype=bool)
    order = np.argsort(pvals)
    passed = pvals[order] <= q * (np.arange(1, n + 1) / n)
    mask = np.zeros(n, dtype=bool)
    if passed.any():
        mask[order[: np.max(np.where(passed)[0]) + 1]] = True
    return mask


def cell_stats(sample: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for key, g in sample.groupby("key"):
        r = g["ret"].to_numpy()
        n = len(r)
        if n < 30:
            continue
        sd = r.std(ddof=1)
        if sd == 0:
            continue
        t = r.mean() / (sd / np.sqrt(n))
        rows.append(dict(key=key, mean=r.mean(), t=t, p=2 * (1 - stats.norm.cdf(abs(t))), n=n))
    return pd.DataFrame(rows)


def select_cells(insample: pd.DataFrame) -> pd.DataFrame:
    sdf = cell_stats(insample)
    if sdf.empty:
        return sdf
    sdf = sdf[benjamini_hochberg(sdf["p"].to_numpy(), FDR_Q)].copy()
    sdf["sign"] = np.sign(sdf["mean"]).astype(int)
    return sdf


def walk_forward(df: pd.DataFrame):
    """Anchored expanding walk-forward. Returns (tradable_cells, oos_tape, n_cells_universe)."""
    years = sorted(df["year"].unique())
    if len(years) < 4:
        return [], pd.DataFrame(), 0
    oos_years = years[3:]
    n_folds = len(oos_years)
    persist_k = min(PERSIST_K, max(2, n_folds // 2))

    elig_count: dict = {}
    sign_votes: dict = {}
    fold_oos = []
    for y in oos_years:
        elig = select_cells(df[df["year"] < y])
        if elig.empty:
            continue
        for _, row in elig.iterrows():
            elig_count[row.key] = elig_count.get(row.key, 0) + 1
            sign_votes.setdefault(row.key, []).append(int(row.sign))
        oos = df[df["year"] == y]
        sel = oos[oos["key"].isin(set(elig["key"]))].copy()
        sel["sign"] = sel["key"].map(dict(zip(elig["key"], elig["sign"], strict=False)))
        fold_oos.append(sel)

    tradable = []
    for key, cnt in elig_count.items():
        if cnt < persist_k:
            continue
        votes = sign_votes[key]
        majority = int(np.sign(sum(votes)))
        if majority != 0 and sum(1 for v in votes if v == majority) >= persist_k:
            tradable.append((key, majority, cnt))

    tape = pd.concat(fold_oos, ignore_index=True) if fold_oos else pd.DataFrame()
    if len(tape):
        tkeys = {k: s for k, s, _ in tradable}
        tape = tape[tape["key"].isin(tkeys)].copy()
        tape["fsign"] = tape["key"].map(tkeys)
        tape["net"] = tape["fsign"] * tape["ret"] - tape["cost_ret"]
    n_universe = len(cell_stats(df))
    return tradable, tape, n_universe


def score(tape: pd.DataFrame) -> dict:
    if tape is None or len(tape) == 0:
        return dict(n=0, mean=0.0, t=0.0, sharpe=0.0, win=0.0)
    net = tape["net"].to_numpy()
    sd = net.std(ddof=1) if len(net) > 1 else 0.0
    t = net.mean() / (sd / np.sqrt(len(net))) if sd > 0 else 0.0
    return dict(
        n=len(net),
        mean=float(net.mean()),
        t=float(t),
        sharpe=float(net.mean() / sd) if sd > 0 else 0.0,
        win=float((net > 0).mean()),
    )


def main() -> int:  # noqa: C901 — linear report builder; complexity is fine for a research script
    print("=" * 78)
    print("Tier 1A — calendar/flow gauntlet (multi-instrument, FDR + persistence + DSR)")
    print("=" * 78)

    all_survivors = []  # (family, symbol, key, sign, folds)
    pooled_oos = []  # OOS survivor tapes (discovery, pre-sealed)
    sealed_oos = []  # sealed hold-out tapes
    n_trials = 0
    ctrl_ok = False

    for fam in FAMILIES:
        for sym in INSTRUMENTS:
            path = CANDLES / f"{sym}_15m.parquet"
            if not path.exists():
                continue
            f = build_frame(sym, fam)
            if f.empty:
                continue
            max_y = int(f["year"].max())
            sealed_start = max_y - SEALED_YEARS + 1
            disc = f[f["year"] < sealed_start]
            seal = f[f["year"] >= sealed_start]

            tradable, tape, n_uni = walk_forward(disc)
            n_trials += n_uni
            if not tradable:
                continue
            for key, sgn, cnt in tradable:
                all_survivors.append((fam.name, sym, key, sgn, cnt))
            if len(tape):
                tape["family"], tape["sym"] = fam.name, sym
                pooled_oos.append(tape)
            # sealed hold-out: score the discovered tradable cells, once
            tkeys = {k: s for k, s, _ in tradable}
            ss = seal[seal["key"].isin(tkeys)].copy()
            if len(ss):
                ss["net"] = ss["key"].map(tkeys) * ss["ret"] - ss["cost_ret"]
                ss["family"], ss["sym"] = fam.name, sym
                sealed_oos.append(ss)
            # positive control
            if fam.name == "hourly" and sym == "eurusd":
                ctrl_ok = any(k[1] == 11 and s < 0 for k, s, _ in tradable)

    print(f"\ntotal cells tested (n_trials for DSR): {n_trials:,}")
    print(f"POSITIVE CONTROL (EURUSD hourly 11:00 UTC short re-emerged): {'YES' if ctrl_ok else 'NO'}")
    if not ctrl_ok:
        print("\n!! Harness failed its positive control — do NOT trust any result below. Abort.")
        return 1

    # survivor table
    print("\n" + "-" * 78)
    print(f"SURVIVORS (FDR + persistence, per family × instrument): {len(all_survivors)}")
    wd = {0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu", 4: "Fri"}
    for fam in FAMILIES:
        rows = [s for s in all_survivors if s[0] == fam.name]
        if not rows:
            continue
        print(f"\n[{fam.name}]  {fam.mechanism}")
        for _, sym, key, sgn, cnt in sorted(rows, key=lambda r: (r[1], str(r[2]))):
            label = f"{wd.get(key[0], key[0])} {key[1]:02d}:00" if fam.name == "hourly" else str(key[0])
            print(f"   {sym:<7} {label:<10} {'SHORT' if sgn < 0 else 'LONG':<6} (elig {cnt} folds)")

    # cross-instrument replication
    print("\n" + "-" * 78)
    print("CROSS-INSTRUMENT REPLICATION (same family+key+sign on >=2 instruments):")
    rep: dict = {}
    for fam_name, sym, key, sgn, _ in all_survivors:
        rep.setdefault((fam_name, key, sgn), []).append(sym)
    replicated = {k: v for k, v in rep.items() if len(v) >= 2}
    if replicated:
        for (fam_name, key, sgn), syms in sorted(replicated.items(), key=lambda x: -len(x[1])):
            label = f"{wd.get(key[0], key[0])} {key[1]:02d}:00" if fam_name == "hourly" else str(key[0])
            print(f"   {fam_name:<9} {label:<10} {'SHORT' if sgn < 0 else 'LONG':<6} x{len(syms)}: {', '.join(syms)}")
    else:
        print("   (none — no calendar cell replicated across >=2 instruments)")

    # pooled OOS + DSR + sealed hold-out
    pooled = pd.concat(pooled_oos, ignore_index=True) if pooled_oos else pd.DataFrame()
    sealed = pd.concat(sealed_oos, ignore_index=True) if sealed_oos else pd.DataFrame()
    ps, ss = score(pooled), score(sealed)
    dsr = deflated_sharpe_ratio(ps["sharpe"], ps["n"], max(n_trials, 1)) if ps["n"] else float("nan")

    print("\n" + "-" * 78)
    print("POOLED SURVIVOR BASKET")
    print(
        f"   discovery OOS : {ps['n']:>6,} trades | mean {ps['mean'] * 1e4:+.3f}bp | "
        f"t {ps['t']:+.2f} | per-trade Sharpe {ps['sharpe']:+.4f} | win {ps['win'] * 100:.1f}%"
    )
    print(f"   Deflated Sharpe Ratio (n_trials={n_trials:,}): {dsr:.3f}   (gate > 0.90)")
    print(
        f"   SEALED hold-out: {ss['n']:>6,} trades | mean {ss['mean'] * 1e4:+.3f}bp | "
        f"t {ss['t']:+.2f} | win {ss['win'] * 100:.1f}%"
    )

    print("\n" + "=" * 78)
    strong = bool(replicated) and dsr > 0.90 and ss["t"] > 2.0 and ss["mean"] > 0
    if strong:
        print(
            "VERDICT: candidate flow edge(s) replicate cross-instrument, survive DSR, and "
            "hold on the sealed years. Promote to Phase 2 hardening."
        )
    elif replicated and ps["t"] > 2.0:
        print(
            "VERDICT: cross-instrument cells survive discovery but NOT the full DSR+sealed "
            "bar. Marginal — inspect individually in Phase 2 before trusting."
        )
    else:
        print(
            "VERDICT: beyond the known 11:00 control, no calendar cell replicates "
            "cross-instrument AND clears DSR+sealed. Honest negative for the swept families."
        )
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
