# carry-basket (SHADOW)

Monthly shadow job for the carry sleeve. Computes dollar-neutral HML target weights
(9 currencies, ranked by lagged 3M rate) + a de-risk-only vol scalar, logs them with a
simulated P&L to `LOGS/carry_shadow_ledger.jsonl`. **Places no real orders**
(`ORDERS_ENABLED = False`, hard-guarded). Purpose: validate the live signal tracks the
backtest over 2-3 months before the executor (deferred plan) is built.

Run: `python services/carry-basket/carry_basket_shadow.py`
Gate to build the executor: shadow `sim_pnl` should track `scripts/probe_carry_portfolio.py`.
Isolated from the live FTMO/vault engine, irb-bridge, month-end, and intraday-drift.
