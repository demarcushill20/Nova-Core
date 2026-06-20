"""Carry-basket SHADOW job — computes the monthly carry target weights + de-risk scalar
and logs them with a simulated P&L update. Places ZERO real orders (ORDERS_ENABLED is a
hard False this phase — the executor is a separate, deferred plan). Mirrors the IDF
dry-run discipline: a dedicated demo account is referenced for equity only.

Run monthly (cron/systemd). Appends one JSON line per run to the shadow ledger so we can
check, over 2-3 months, whether the live signal tracks scripts/probe_carry_portfolio.py
before investing in execution infra.

Env:
  CBS_LEDGER (default LOGS/carry_shadow_ledger.jsonl)
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd  # noqa: TC002 -- runtime data dep (DataFrame panel); not typing-only

from novatrade.strategies.carry_basket import CarryConfig, derisk_scalar, rank_weights
from scripts.probe_carry_portfolio import build_panel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("carry-basket-shadow")

# HARD SAFETY GUARD: this phase shadows only. The executor lives in a separate plan.
ORDERS_ENABLED = False


def load_panel() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Refresh + load the cached FRED rate/spot panel (I/O lives here, not in the module)."""
    return build_panel()


def build_shadow_entry(spot: pd.DataFrame, rates: pd.DataFrame, cfg: CarryConfig | None = None) -> dict:
    """Latest-month target weights + de-risk scalar + a simulated P&L from last month's
    weights applied to the realized spot move."""
    cfg = cfg or CarryConfig()
    ccys = [c for c in cfg.currencies if c in spot.columns and c in rates.columns]
    S, R = spot[ccys], rates[ccys]
    rrank = R.shift(1)
    dt = rrank.dropna(how="all").index[-1]
    rk = rrank.loc[dt].dropna()
    w = rank_weights(rk, cfg.k_frac)
    raw_hist = (S.pct_change() + (R.sub(R["USD"], axis=0) / 1200.0).shift(1)).dropna(how="all")
    sleeve_hist = raw_hist.mean(axis=1).dropna()
    scalar = derisk_scalar(sleeve_hist, cfg)
    if len(raw_hist) >= 1:
        last = raw_hist.iloc[-1]
        sim_pnl = float((w.reindex(last.index).fillna(0.0) * last.fillna(0.0)).sum()) * scalar
    else:
        sim_pnl = 0.0
    return {
        "as_of": dt.strftime("%Y-%m-%d"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "target_weights": {k: round(float(v), 4) for k, v in w[w != 0.0].items()},
        "derisk_scalar": round(scalar, 4),
        "sim_pnl_bps": round(sim_pnl * 1e4, 2),
        "orders_enabled": ORDERS_ENABLED,
    }


def main() -> None:
    if ORDERS_ENABLED:
        raise SystemExit("ORDERS_ENABLED must be False in the shadow phase")
    S, R = load_panel()
    entry = build_shadow_entry(S, R)
    ledger = Path(os.environ.get("CBS_LEDGER", "LOGS/carry_shadow_ledger.jsonl"))
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a") as f:
        f.write(json.dumps(entry) + "\n")
    log.info(
        "SHADOW (no orders) %s  scalar=%.2f  sim_pnl=%.1fbps  weights=%s",
        entry["as_of"],
        entry["derisk_scalar"],
        entry["sim_pnl_bps"],
        entry["target_weights"],
    )


if __name__ == "__main__":
    main()
