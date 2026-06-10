"""Fidelity backtester — strategy decisions on a primary timeframe, fills
resolved on the finest bars available (M5 today, real ticks later).

Motivation
----------
The vault IRB engine resolves intra-bar fills with ``ohlc_path`` — a single-bar
heuristic that *guesses* which extreme came first within a bar. For a strategy
whose stops are tighter than a bar's range, that guess systematically flatters
results: every stop-vs-target coin-flip inside one bar is resolved by assumption,
not by data. This engine keeps the strategy logic byte-for-byte identical and
changes ONE thing — it replaces the guess with the real ordering reconstructed
from finer sub-bars. So any difference in the result is pure fill fidelity.

Provenance
----------
With the default (single-bar) resolver this reproduces the stock vault engine
exactly. Swapping in real M5 sub-bars flipped canonical IRB-on-H1 from +0.117R to
-0.014R (zero cost) — exposing that "edge" as a fill artifact. See the
``project_live_cost_stop_discovery`` memory and ``tests/test_fidelity.py``.

Usage
-----
    from novatrade.backtest.fidelity import run_fidelity_backtest
    res = run_fidelity_backtest(m5_df, primary_tf="60min", cost_pips=0.5)
    print(res.edge_r, res.breakeven_cost_pips, res.net_pnl)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import numpy as np

from novatrade.strategies.vault_irb_state import (
    IRBConfig,
    IRBStrategyState,
    TradeEvent,
    ohlc_path,
    prepare_dataframe,
)

if TYPE_CHECKING:
    import pandas as pd

PIP_DEFAULT = 0.0001


class FidelityIRBState(IRBStrategyState):
    """Vault IRB with real-sub-bar fill resolution + optional round-trip cost.

    Two faithful extensions of the vault core:

    * ``_intrabar_path`` returns a pre-resolved finer path for the bar (keyed by
      bar index) instead of the single-bar ``ohlc_path`` guess.
    * ``_close_tranche`` deducts a per-fill round-trip cost (folded into equity so
      compounding sizing is correct) and records the per-position stop distance so
      a sizing-independent edge can be computed.

    With ``sub_paths={}`` and ``cost_pips=0`` it is exactly the stock vault engine.
    """

    def __init__(
        self,
        cfg: IRBConfig,
        sub_paths: dict[int, list[float]] | None = None,
        cost_pips: float = 0.0,
        pip: float = PIP_DEFAULT,
    ) -> None:
        super().__init__(cfg)
        self._sub_paths = sub_paths or {}
        self._cost_pips = cost_pips
        self._pip = pip
        self._stop_dists: list[float] = []
        # Per-position ledger — correct by construction (a position can close in
        # TWO non-partial legs: tp_qty + runner_qty). Keyed by entry identity so
        # legs accumulate into one position with one stop, not N.
        self._positions: list[dict] = []

    def _intrabar_path(self, i: int, row: pd.Series) -> list[float]:
        path = self._sub_paths.get(i)
        return path if path else super()._intrabar_path(i, row)

    def _close_tranche(self, ts: Any, exit_price: float, qty: float, reason: str) -> None:
        if qty <= 0 or self.position is None:
            return
        side = self.position["side"]
        entry = self.position["entry_price"]
        gross = (exit_price - entry) * qty if side == "long" else (entry - exit_price) * qty
        net = gross - self._cost_pips * self._pip * qty
        self.equity += net

        # Accumulate into the position ledger by entry identity (one position can
        # close across multiple non-partial legs; all share one stop distance).
        eb = self.position.get("entry_bar")
        if not self._positions or self._positions[-1]["entry_bar"] != eb:
            self._positions.append(
                {
                    "entry_bar": eb,
                    "entry_time": self.position.get("entry_time"),
                    "stop_pips": abs(entry - self.position["initial_stop"]) / self._pip,
                    "qty": self.position["qty_total"],
                    "gross": 0.0,
                    "net": 0.0,
                }
            )
        self._positions[-1]["gross"] += gross
        self._positions[-1]["net"] += net

        if reason != "partial_tp":
            self._stop_dists.append(abs(entry - self.position["initial_stop"]) / self._pip)
        self.trades.append(
            TradeEvent(
                time=ts,
                type="exit",
                side=side,
                price=exit_price,
                qty=qty,
                equity_after=self.equity,
                reason=reason,
                pnl=net,
            )
        )


@dataclass
class FidelityResult:
    """Outcome of a fidelity backtest. Edge metrics are sizing-independent
    (per-trade R) and therefore robust to the compounding/ruin path-dependence
    that corrupts raw compounded dollar figures."""

    net_pnl: float
    total_trades: int
    edge_r: float  # mean per-trade R, net of cost
    median_stop_pips: float
    breakeven_cost_pips: float  # round-trip cost at which mean net R hits zero
    win_rate: float
    fine_fills: bool
    cost_pips: float
    trades: list = field(default_factory=list)
    raw_state: Any = None


def _resample_with_subs(fine_df: pd.DataFrame, rule: str):
    """Resample fine bars to the primary timeframe and return (primary_df, subs)
    where subs maps each primary bar's right-edge timestamp to its ordered list
    of (o, h, l, c) sub-bars."""
    g = fine_df.set_index("time").resample(rule, label="right", closed="right")
    primary = g.agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    subs = {
        lab: list(zip(grp["open"], grp["high"], grp["low"], grp["close"], strict=False)) for lab, grp in g if len(grp)
    }
    return primary.reset_index(), subs


def run_fidelity_backtest(
    fine_df: pd.DataFrame,
    primary_tf: str = "60min",
    cfg: IRBConfig | None = None,
    cost_pips: float = 0.0,
    fine_fills: bool = True,
    pip: float = PIP_DEFAULT,
) -> FidelityResult:
    """Run the IRB strategy: decisions on ``primary_tf``, fills on ``fine_df``.

    Args:
        fine_df: finest available bars; columns time, open, high, low, close.
        primary_tf: pandas offset for the decision timeframe (e.g. "60min").
        cfg: IRBConfig (defaults to the canonical vault config).
        cost_pips: per-trade round-trip cost charged at each fill.
        fine_fills: True resolves intra-bar order from real sub-bars; False uses
            the stock single-bar heuristic (reproduces the vault engine).
        pip: price value of one pip.

    Returns:
        FidelityResult.
    """
    cfg = cfg or IRBConfig()
    primary, subs = _resample_with_subs(fine_df, primary_tf)
    pdf = prepare_dataframe(primary, cfg)

    sub_paths: dict[int, list[float]] = {}
    if fine_fills:
        for i, r in pdf.iterrows():
            sub = subs.get(r["time"])
            if sub:
                path: list[float] = []
                for o, h, low, c in sub:
                    path += ohlc_path(o, h, low, c)
                sub_paths[i] = path

    st = FidelityIRBState(cfg, sub_paths=sub_paths, cost_pips=cost_pips, pip=pip)
    for i, row in pdf.iterrows():
        st.process_bar(i, row, pdf)

    return _summarize(st, cfg, pip, fine_fills, cost_pips)


def _summarize(st: FidelityIRBState, cfg: IRBConfig, pip: float, fine_fills: bool, cost_pips: float) -> FidelityResult:
    # Per-position R from the correct-by-construction ledger. gross_r and net_r
    # are sizing-independent (gross/(stop_pips*pip*qty)); net already has cost.
    gross_r: list[float] = []
    net_r: list[float] = []
    stop_pips: list[float] = []
    for p in st._positions:
        risk = p["stop_pips"] * pip * p["qty"]
        if risk > 0 and p["stop_pips"] > 0:
            gross_r.append(p["gross"] / risk)
            net_r.append(p["net"] / risk)
            stop_pips.append(p["stop_pips"])

    gr = np.array(gross_r) if gross_r else np.array([0.0])
    nr = np.array(net_r) if net_r else np.array([0.0])
    sp = np.array(stop_pips) if stop_pips else np.array([1.0])
    # breakeven cost solves mean(grossR - c/stop) = 0  ->  c = mean(grossR)/mean(1/stop)
    breakeven = float(gr.mean() / np.mean(1.0 / sp)) if stop_pips else 0.0
    wins = int((nr > 0).sum())

    return FidelityResult(
        net_pnl=float(st.equity - cfg.initial_capital),
        total_trades=len(net_r),
        edge_r=float(nr.mean()),
        median_stop_pips=float(np.median(sp)),
        breakeven_cost_pips=breakeven,
        win_rate=(wins / len(net_r)) if net_r else 0.0,
        fine_fills=fine_fills,
        cost_pips=cost_pips,
        trades=st.trades,
        raw_state=st,
    )
