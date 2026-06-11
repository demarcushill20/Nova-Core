"""Autoresearch loop — disciplined automated strategy search for NovaTrade.

A Karpathy-style propose -> backtest -> score -> refine loop, built so the
*scoring function is the anti-overfitting machine*, not an afterthought. Naive
automated search on market data is a mirage generator (the multiple-testing
problem is lethal); this loop counters it with:

  - a SEALED hold-out (most-recent slice) that selection never touches;
  - per-candidate sub-period (yearly) consistency, not just aggregate Sharpe;
  - a trial-count-aware Deflated Sharpe Ratio (López de Prado) — the loop counts
    its own attempts and discounts the best accordingly;
  - a MECHANISM gate — a candidate only reaches "deploy" tier if it has a
    structural reason to work (flow/session seasonality), not just a nice curve.

It is designed to rediscover the known edges (the 11:00 UTC EUR-weakness drift)
and to REJECT the cost-mirage families (mean-reversion fade, breakout) — if it
can't do that, it can't be trusted to find new ones.

Entry point: `python -m novatrade.research.autoresearch.run`.
"""

from __future__ import annotations

from novatrade.research.autoresearch.families import Candidate, backtest
from novatrade.research.autoresearch.gauntlet import Verdict, score_candidate

__all__ = ["Candidate", "Verdict", "backtest", "score_candidate"]
