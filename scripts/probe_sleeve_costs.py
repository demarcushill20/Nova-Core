"""Retail-cost stress test of the gold/BTC sleeves (operator concern 2026-07-13:
'commission and spread will eat away my edge — can we focus on only forex pairs').

TSMOM is a SLOW strategy: vol-targeted positions drift, they don't churn. This probe
measures exactly how much each sleeve trades (annual turnover = sum |dpos|), how much
notional it holds (avg |pos|, which pays overnight swap), and reprices the sleeves under
realistic retail cost scenarios:

  spread+commission : charged per unit of turnover (bp of traded notional)
  swap/financing    : charged per day on HELD notional (annualized %), paid on BOTH
                      long and short pessimistically (real gold shorts often EARN swap)

Scenarios (round-trip bp / annual swap %):
  gold  modeled 2bp/0     | retail MT5 raw 5bp/1.5  | awful standard-acct 10bp/4
  btc   modeled 5bp/0     | spot-exchange 10bp/5    | retail CFD 25bp/20

Then rebuilds the winner blend (frozen weights, matched window) under each scenario and
prints the honest alternatives: 4-sleeve at retail costs vs fx+gold vs fx-only.
Gold PnL before 2021 comes from the 2bp cache; extra scenario drag there is applied as
the average annual drag measured on 2021+ (positions unavailable pre-2020) — stated
approximation, small because gold drag itself is small.
"""

from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
WORK = os.path.join(ROOT, "WORK")

from probe_fx_multifactor_v2 import (  # noqa: E402
    build_panel,
    carry_gate,
    excess_returns,
    hml_iv,
    monthly_vol,
)

G10 = ["USD", "EUR", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF", "NOK", "SEK"]
FROZEN_W = {"eur_short": 0.5758, "carry": 0.2214, "gold_tsmom": 0.1151, "btc_gated": 0.0877}


def tsmom_pos(close: pd.Series) -> pd.Series:
    ret = close.pct_change(fill_method=None)
    vol63 = ret.rolling(63, min_periods=40).std() * np.sqrt(252)
    sig = 0.5 * np.sign(close.pct_change(252, fill_method=None)) + 0.5 * np.sign(close.pct_change(63, fill_method=None))
    return (sig * (0.10 / vol63)).clip(-2.0, 2.0)


def scenario_pnl(close: pd.Series, spread_bp: float, swap_ann: float, gate: pd.Series | None = None) -> pd.Series:
    """Net daily sleeve PnL: position return - turnover*spread - held*swap/252."""
    pos = tsmom_pos(close)
    if gate is not None:
        g = pd.Series(pos.index.to_period("M").map(gate).astype(float), index=pos.index).fillna(0.0)
        pos = pos * g
    ret = close.pct_change(fill_method=None)
    return (pos.shift(1) * ret - pos.diff().abs() * spread_bp * 1e-4 - pos.shift(1).abs() * swap_ann / 252).dropna()


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
        return f"{label:44s} insufficient"
    sh, ar, av, dd = ann(m)
    return f"{label:44s} Sharpe {sh:+.2f}  ret {ar * 100:+5.1f}%  vol {av * 100:4.1f}%  maxDD {dd * 100:5.1f}%"


def main() -> None:
    print("=" * 112)
    print("SLEEVE COST STRESS — does retail spread/commission/swap actually kill gold+BTC TSMOM?")
    print("=" * 112)

    gold_close = pd.read_csv(os.path.join(WORK, "gold_daily_close_2020_2026.csv"), index_col=0, parse_dates=True).iloc[
        :, 0
    ]
    btc_close = pd.read_csv(os.path.join(WORK, "btc_daily_close_2015_2026.csv"), index_col=0, parse_dates=True).iloc[
        :, 0
    ]
    s1 = pd.read_csv(os.path.join(WORK, "sleeve1_1100short_daily.csv"), index_col=0, parse_dates=True).iloc[:, 0]
    gold_pre = pd.read_csv(os.path.join(WORK, "sleeve4_gold_tsmom_daily.csv"), index_col=0, parse_dates=True).iloc[:, 0]

    # ---- carry + gate (monthly) ----
    S, R, SD = build_panel()
    vol = monthly_vol(SD, S.index)
    gate_m = carry_gate(vol, G10)
    carry_m = hml_iv(R.sub(R["USD"], axis=0), excess_returns(S, R), vol, G10, gate=gate_m)
    carry_m.index = carry_m.index.to_period("M")
    gate_p = gate_m.copy()
    gate_p.index = gate_p.index.to_period("M")

    # ---- turnover / holding profile ----
    print("\nHOW MUCH DO THESE SLEEVES ACTUALLY TRADE? (the premise check)")
    for name, close, gate in (("gold", gold_close, None), ("btc_gated", btc_close, gate_p)):
        pos = tsmom_pos(close)
        if gate is not None:
            g = pd.Series(pos.index.to_period("M").map(gate).astype(float), index=pos.index).fillna(0.0)
            pos = pos * g
        pos = pos.dropna()
        yrs = len(pos) / 252
        turn = pos.diff().abs().sum() / yrs
        print(
            f"  {name:10s} avg|pos| {pos.abs().mean():.2f}x  annual turnover {turn:5.1f}x notional "
            f"(~{turn / 2:.0f} round-trips/yr of the full position)"
        )
        for label, bp, swap in (
            ("modeled", 2.0 if name == "gold" else 5.0, 0.0),
            ("retail", 5.0 if name == "gold" else 10.0, 0.015 if name == "gold" else 0.05),
            ("awful", 10.0 if name == "gold" else 25.0, 0.04 if name == "gold" else 0.20),
        ):
            drag = turn * bp * 1e-4 + pos.abs().mean() * swap
            print(f"      {label:8s} {bp:4.0f}bp + swap {swap * 100:4.1f}%/yr  -> total drag {drag * 100:5.2f}%/yr")

    # ---- sleeve PnL under scenarios ----
    scen = {
        "gold": {"modeled": (2.0, 0.0), "retail": (5.0, 0.015), "awful": (10.0, 0.04)},
        "btc": {"modeled": (5.0, 0.0), "exchange": (10.0, 0.05), "cfd": (25.0, 0.20)},
    }
    gold_d: dict[str, pd.Series] = {}
    btc_d: dict[str, pd.Series] = {}
    print("\nSLEEVE SHARPE UNDER COST SCENARIOS:")
    for label, (bp, swap) in scen["gold"].items():
        fresh = scenario_pnl(gold_close, bp, swap).loc["2021-01-01":]  # type: ignore[misc]
        base = scenario_pnl(gold_close, *scen["gold"]["modeled"]).loc["2021-01-01":]  # type: ignore[misc]
        extra_daily = float((base - fresh).mean())  # avg extra daily drag vs modeled
        pre = gold_pre[gold_pre.index < fresh.index[0]] - extra_daily
        gold_d[label] = pd.concat([pre, fresh]).sort_index()
        print(line(to_m(gold_d[label]), f"  gold {label} ({bp:.0f}bp, swap {swap * 100:.1f}%)"))
    for label, (bp, swap) in scen["btc"].items():
        btc_d[label] = scenario_pnl(btc_close, bp, swap, gate=gate_p)
        print(line(to_m(btc_d[label]), f"  btc_gated {label} ({bp:.0f}bp, swap {swap * 100:.0f}%)"))

    # ---- blends on the matched winner window ----
    def blend_stats(parts: dict[str, pd.Series], label: str) -> None:
        panel = pd.DataFrame(parts)
        panel["carry"] = carry_m
        panel = panel.loc[pd.Period("2016-03", "M") : pd.Period("2026-03", "M")].dropna()
        w = pd.Series({c: FROZEN_W[c] for c in panel.columns})
        w = w / w.sum()
        port = (panel * w).sum(axis=1)
        sh, ar, av, _ = ann(port)
        p6 = port * 6.0
        eq6 = (1 + p6).cumprod()
        cagr6 = eq6.iloc[-1] ** (12 / len(p6)) - 1
        dd6 = ((eq6 - eq6.cummax()) / eq6.cummax()).min()
        solve_l = 0.30 / ar if ar > 0 else np.nan
        print(
            f"  {label:34s} Sharpe {sh:+.2f} | at 6x: CAGR {cagr6 * 100:+5.1f}% maxDD(mo) {dd6 * 100:5.1f}% | "
            f"30%: L={solve_l:4.1f}"
        )

    print(f"\nBLENDS (matched window 2016-03..2026-03, frozen weights renormalized; ref 4s modeled = {1.88:.2f}):")
    blend_stats(
        {"eur_short": to_m(s1), "gold_tsmom": to_m(gold_d["modeled"]), "btc_gated": to_m(btc_d["modeled"])},
        "4-sleeve modeled (ref)",
    )
    blend_stats(
        {"eur_short": to_m(s1), "gold_tsmom": to_m(gold_d["retail"]), "btc_gated": to_m(btc_d["exchange"])},
        "4-sleeve retail (gold MT5 + btc exch)",
    )
    blend_stats(
        {"eur_short": to_m(s1), "gold_tsmom": to_m(gold_d["retail"]), "btc_gated": to_m(btc_d["cfd"])},
        "4-sleeve worst (gold MT5 + btc CFD)",
    )
    blend_stats(
        {"eur_short": to_m(s1), "gold_tsmom": to_m(gold_d["awful"]), "btc_gated": to_m(btc_d["cfd"])},
        "4-sleeve awful-everything",
    )
    blend_stats({"eur_short": to_m(s1), "gold_tsmom": to_m(gold_d["retail"])}, "3-sleeve fx+gold retail (no BTC)")
    blend_stats({"eur_short": to_m(s1)}, "2-sleeve FX-ONLY (eur+carry)")

    print("\n" + "#" * 112)
    print("READ: if '4-sleeve retail/worst' holds near the ref, the cost premise is wrong for these sleeves;")
    print("      'fx-only' line is what dropping them actually costs.")
    print("#" * 112)


if __name__ == "__main__":
    main()
