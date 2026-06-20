"""Two-sleeve risk allocator — pure. Splits portfolio risk between the EUR own-hours
edge (Sharpe driver) and the de-risk-only carry sleeve (uncorrelated diversifier).

Sleeves are ~uncorrelated (corr ~-0.03), so the Sharpe-optimal risk split is
risk_i proportional to Sharpe_i, with the carry weight capped by the live ramp stage
(15% -> 27%). No I/O; sizing math only.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class AllocatorConfig:
    sharpe_eur: float = 1.1
    sharpe_carry: float = 0.4
    target_book_vol: float = 0.17
    carry_cap: float = 0.27
    em_cap: float = 0.40


def risk_weights(sharpe_eur: float, sharpe_carry: float, carry_cap: float) -> tuple[float, float]:
    """Sharpe-proportional risk split, carry capped at the ramp ceiling."""
    raw_carry = sharpe_carry / (sharpe_eur + sharpe_carry)
    w_carry = min(raw_carry, carry_cap)
    return 1.0 - w_carry, w_carry


def sleeve_sizing(equity: float, cfg: AllocatorConfig | None = None) -> dict:
    """Per-sleeve risk weight + target vol contribution + notional risk budget."""
    cfg = cfg or AllocatorConfig()
    w_eur, w_carry = risk_weights(cfg.sharpe_eur, cfg.sharpe_carry, cfg.carry_cap)
    sig_eur = cfg.target_book_vol * np.sqrt(w_eur)
    sig_carry = cfg.target_book_vol * np.sqrt(w_carry)
    return {
        "eur": {"risk_weight": w_eur, "target_vol": sig_eur, "risk_budget": equity * w_eur},
        "carry": {
            "risk_weight": w_carry,
            "target_vol": sig_carry,
            "risk_budget": equity * w_carry,
            "em_cap": cfg.em_cap,
        },
    }


def combined_backtest(eur_monthly: pd.Series, carry_monthly: pd.Series, w_eur: float, w_carry: float) -> dict:
    """Align the two monthly streams, blend at the given risk weights (each scaled to
    unit vol first), and report corr, blended annualized Sharpe, and maxDD."""
    df = pd.concat([carry_monthly.rename("carry"), eur_monthly.rename("eur")], axis=1).dropna()
    corr = float(df["carry"].corr(df["eur"]))
    comb = w_eur * df["eur"] / df["eur"].std() + w_carry * df["carry"] / df["carry"].std()
    blended_sharpe = float(comb.mean() / comb.std() * np.sqrt(12))
    eq = (1 + comb).cumprod()
    maxdd = float((eq / eq.cummax() - 1).min())
    return {"corr": corr, "blended_sharpe": blended_sharpe, "maxdd": maxdd, "n": len(df)}
