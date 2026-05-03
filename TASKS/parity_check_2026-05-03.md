# Parity Check — Validate 0.33% Risk Reduction

**Context**

On 2026-04-26 (commit 91692a7, branch feat/risk-reduction-0.33pct merged to main), per-trade risk_fraction was lowered 1.5% → 0.33% (4.5× safety margin). Two source-of-truth knobs changed: novatrade/backtest/environment.py:165 (env.risk_fraction default) and novatrade/risk/pre_trade_gate.py:122 (DrawdownProportionalRisk base_risk_pct). Three tests were recalibrated for the new size budget.

This task validates that the sizing change did not introduce alpha drift — i.e. live trades still match the IRB v5 backtest at the new risk level.

**Action**

Invoke the `novatrade-parity-check` skill for the window 2026-04-26 → 2026-05-03 (the 7-day period since the risk reduction shipped).

**Reporting requirements**

Report, in order:

1. Headline: total live trades vs total backtest trades in the window.
2. Divergence count by classification: uptime-gap / bar-feed / price-drift / strategy-drift / real-mismatch.
3. Any real-mismatch findings — full detail per trade. These are the ones that matter; everything else is noise.
4. Spot-check that the volume on at least 3 live trades is consistent with 0.33% sizing (i.e. roughly 4.5× smaller in lots than a 1.5%-sized equivalent for the same stop distance).
5. Final verdict: PASS (no real-mismatch, sizing looks right) or FAIL (with specifics).

If the live runtime had no trades in the window (all market-hours warnings), report that and suggest extending the window.

**Owner**: operator (Demarcus)
**Created by**: scheduled remote agent on 2026-05-03
