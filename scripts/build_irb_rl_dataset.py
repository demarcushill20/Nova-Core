"""Phase 1 — IRB-RL filter dataset builder.

Runs the canonical Rob Hoffman IRB v5 config on H1 decisions / M1 fills over the
full 10yr EURUSD Dukascopy feed, and emits one labelled row per *filled* setup:

    features (all relative / normalised, captured at the SETUP bar)
    + label  (realised net-R and gross-R of the resulting trade)

The filter we train in later phases is a one-shot take/skip decision per setup,
so the natural reward is the realised R of the trade (0 if skipped). This builder
produces the (features, R) table that both the cheap separability probe (Phase 2)
and the learned filter (Phase 3) consume.

Design notes
------------
* Strategy logic is the regression-locked vault engine, untouched. We only
  subclass FidelityIRBState to record an entry_bar -> (signal_bar, side) map by
  overriding `_emit_entry` (which fires while `self.pending` is still set).
* Features are read from the prepared H1 row at the *setup* bar (signal_bar) —
  the real decision point. Nothing forward-looking leaks into the feature set.
* Labels (net_r / gross_r) come from the correct-by-construction per-position
  ledger, identical to `fidelity._summarize`.

Gate: mean gross-R should reproduce the canonical IRB-H1 read (~+0.066R). If it
does, Phase 1 is sound and we proceed to the separability probe.

Usage
-----
    python scripts/build_irb_rl_dataset.py \
        --m1 data/candles/EURUSD_M1.csv \
        --out data/rl/irb_h1_setups.parquet \
        --cost-pips 0.5
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from novatrade.backtest.fidelity import PIP_DEFAULT, FidelityIRBState, _resample_with_subs
from novatrade.strategies.vault_irb_state import IRBConfig, ohlc_path, prepare_dataframe


class _RecorderState(FidelityIRBState):
    """FidelityIRBState that records which setup bar produced each fill.

    `_emit_entry` is called at fill time, after `self.position` is set but before
    `self.pending` is cleared — so both the entry bar and the originating signal
    bar are in hand. No strategy logic is changed.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.entry_to_signal: dict[int, tuple[int, str]] = {}

    def _emit_entry(self, ts, side, price, qty) -> None:  # type: ignore[override]
        if self.pending is not None and self.position is not None:
            self.entry_to_signal[self.position["entry_bar"]] = (
                self.pending["signal_bar"],
                self.position["side"],
            )
        super()._emit_entry(ts, side, price, qty)


def _add_features(pdf: pd.DataFrame, cfg: IRBConfig) -> list[str]:
    """Add relative/normalised feature columns to the prepared H1 frame.

    Every feature is a ratio, an ATR-normalised distance, or a cyclical time
    encoding — never an absolute price (per the RL-features discipline: absolute
    levels are meaningless across regimes).
    """
    atr = pdf["signal_atr"]
    rng = pdf["bar_range"].replace(0, np.nan)
    body_hi = pdf[["open", "close"]].max(axis=1)
    body_lo = pdf[["open", "close"]].min(axis=1)

    pdf["f_body_to_range"] = pdf["body_size"] / rng
    pdf["f_range_to_atr"] = pdf["bar_range"] / atr
    pdf["f_upper_wick"] = (pdf["high"] - body_hi) / rng
    pdf["f_lower_wick"] = (body_lo - pdf["low"]) / rng
    pdf["f_close_ema20_atr"] = (pdf["close"] - pdf["ema20"]) / atr
    pdf["f_close_ema50_atr"] = (pdf["close"] - pdf["ema50"]) / atr
    pdf["f_ema20_ema50_atr"] = (pdf["ema20"] - pdf["ema50"]) / atr
    pdf["f_ema20_slope_atr"] = (pdf["ema20"] - pdf["ema20"].shift(cfg.ema20_slope_lookback)) / atr
    pdf["f_htf_close_ema_atr"] = (pdf["htf_close"] - pdf["htf_ema"]) / atr
    pdf["f_htf_slope_atr"] = (pdf["htf_ema"] - pdf["htf_ema_prev"]) / atr
    pdf["f_atr_regime"] = atr / atr.rolling(100, min_periods=20).median()

    hour = pdf["time"].dt.hour + pdf["time"].dt.minute / 60.0
    pdf["f_hour_sin"] = np.sin(2 * np.pi * hour / 24.0)
    pdf["f_hour_cos"] = np.cos(2 * np.pi * hour / 24.0)
    dow = pdf["time"].dt.dayofweek
    pdf["f_dow_sin"] = np.sin(2 * np.pi * dow / 7.0)
    pdf["f_dow_cos"] = np.cos(2 * np.pi * dow / 7.0)
    h = pdf["time"].dt.hour
    pdf["f_london"] = ((h >= 7) & (h < 16)).astype(float)
    pdf["f_ny"] = ((h >= 12) & (h < 21)).astype(float)
    pdf["f_overlap"] = ((h >= 12) & (h < 16)).astype(float)

    return [c for c in pdf.columns if c.startswith("f_")]


def build(m1_path: Path, out_path: Path, primary_tf: str, cost_pips: float) -> pd.DataFrame:
    cfg = IRBConfig()
    pip = PIP_DEFAULT

    print(f"[load] {m1_path}")
    fine = pd.read_csv(m1_path)
    fine.columns = [c.lower().strip() for c in fine.columns]
    if "time" not in fine.columns and "timestamp" in fine.columns:
        fine = fine.rename(columns={"timestamp": "time"})
    fine["time"] = pd.to_datetime(fine["time"])
    fine = fine[["time", "open", "high", "low", "close"]].sort_values("time").reset_index(drop=True)
    print(f"[load] {len(fine):,} M1 bars  {fine['time'].iloc[0]} -> {fine['time'].iloc[-1]}")

    primary, subs = _resample_with_subs(fine, primary_tf)
    pdf = prepare_dataframe(primary, cfg)
    print(f"[prep] {len(pdf):,} {primary_tf} decision bars")

    sub_paths: dict[int, list[float]] = {}
    for i, r in pdf.iterrows():
        sub = subs.get(r["time"])
        if sub:
            path: list[float] = []
            for o, hi, lo, c in sub:
                path += ohlc_path(o, hi, lo, c)
            sub_paths[i] = path

    feature_cols = _add_features(pdf, cfg)

    st = _RecorderState(cfg, sub_paths=sub_paths, cost_pips=cost_pips, pip=pip)
    for i, row in pdf.iterrows():
        st.process_bar(i, row, pdf)
    print(f"[run]  {len(st._positions):,} filled positions  cost={cost_pips}p")

    rows: list[dict] = []
    for p in st._positions:
        eb = p["entry_bar"]
        link = st.entry_to_signal.get(eb)
        if link is None:
            continue
        sb, side = link
        risk = p["stop_pips"] * pip * p["qty"]
        if risk <= 0 or p["stop_pips"] <= 0:
            continue
        frow = pdf.iloc[sb]
        rec = {c: float(frow[c]) for c in feature_cols}
        rec["f_side"] = 1.0 if side == "long" else 0.0
        rec["signal_time"] = frow["time"]
        rec["entry_time"] = p["entry_time"]
        rec["stop_pips"] = float(p["stop_pips"])
        rec["gross_r"] = float(p["gross"] / risk)
        rec["net_r"] = float(p["net"] / risk)
        rec["win"] = int(p["net"] > 0)
        rows.append(rec)

    out = pd.DataFrame(rows).sort_values("signal_time").reset_index(drop=True)

    # Phase 1 gate: reproduce the canonical IRB-H1 edge.
    print("\n=== Phase 1 dataset summary ===")
    print(f"labelled setups : {len(out):,}")
    print(f"mean gross R    : {out['gross_r'].mean():+.4f}  (canonical IRB-H1 ~ +0.066)")
    print(f"mean net R      : {out['net_r'].mean():+.4f}  (net of {cost_pips}p cost)")
    print(f"win rate (net)  : {out['win'].mean():.3f}")
    print(f"long / short    : {int(out['f_side'].sum())} / {int((out['f_side'] == 0).sum())}")
    print(f"features        : {len(feature_cols) + 1} -> {feature_cols + ['f_side']}")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out.to_parquet(out_path, index=False)
    print(f"\n[save] {out_path}  ({len(out):,} rows)")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--m1", default="data/candles/EURUSD_M1.csv")
    ap.add_argument("--out", default="data/rl/irb_h1_setups.parquet")
    ap.add_argument("--primary-tf", default="60min")
    ap.add_argument("--cost-pips", type=float, default=0.5)
    args = ap.parse_args()
    build(Path(args.m1), Path(args.out), args.primary_tf, args.cost_pips)


if __name__ == "__main__":
    main()
