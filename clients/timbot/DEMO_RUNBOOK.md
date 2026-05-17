# TIMBOT — Demo Runbook (TradingView Paper Trading)

> SUPERSEDED 2026-05-17. The engagement uses **Path B** (fully-automated
> TradingView → MetaApi MT5 demo). See `bridge/BRIDGE_DESIGN.md` for the
> active deployment. This file is kept only as a record of the Path A option.
> Note: the original Step 2 below was wrong — a TradingView strategy does NOT
> auto-route orders into Paper Trading; that limitation is why Path B was chosen.

---

# TIMBOT — Demo Runbook (TradingView Paper Trading)

Fast hands-off demo path. No broker approval, no webhook bridge, no cost.
Target: TIMBOT auto-trading a TradingView Paper account on XAUUSD 15m.

## Prerequisites

- The strategy is on a **XAUUSD, 15m** chart.
- Properties tab: Initial capital **100,000**, Order size **5 % of equity**,
  Slippage 1–3 ticks. (Same as the validated baseline — do not change mid-run.)
- TradingView plan: strategy alerts need a **paid plan** (Essential or above).
  Free plan cannot place the alert needed for hands-off execution.

## Step 1 — Connect Paper Trading

1. Open the **Trading Panel** (bottom of the chart) → tab **Paper Trading**.
2. Click **Connect**. TradingView creates a simulated account.
3. Set the paper account balance to **100,000** (gear/settings in the panel)
   so it matches the backtest baseline.

## Step 2 — Create the strategy alert (this is what makes it hands-off)

1. On the chart with TIMBOT loaded, click the **Alert** (clock) icon →
   **Create Alert**.
2. **Condition:** select the strategy — `TIMBOT OFFICIAL — Universal`.
   Choose **"Order fills only"** (alert fires on every entry/exit the strategy
   generates).
3. **Notifications / actions:** there is no webhook needed — because the alert
   is on a strategy and Paper Trading is connected, TradingView routes the
   strategy's orders into the paper account automatically.
4. **Expiration:** set as far out as TradingView allows (open-ended on paid).
5. Name it `TIMBOT XAUUSD 15m — demo` and create it.

> Once created, the alert runs **server-side** — you do NOT need to keep the
> browser open. Orders execute on **bar close** (the strategy is bar-close,
> `calc_on_every_tick = false`).

## Step 3 — Confirm it's live

- The Trading Panel → Paper Trading → **Positions / Orders** updates as the
  strategy fires.
- First sanity check: on the next 4H bias flip the strategy should open a
  long or short in the paper account. Confirm direction matches the chart
  signal (green triangle = long, red = short).

## Step 4 — Log every demo trade

Append each closed trade to `results/demo_trade_log_TEMPLATE.csv`
(copy it to `results/demo_trades_2026-05.csv` first). Per trade record:
entry time, direction, entry, stop, both TP legs, exit reason, R result.

Per-trade checks:
- Entry fired on a 4H bias flip, in session.
- 50% partial executed near 2R.
- Stop moved to break-even after the partial.
- Runner exited at the HTF target — **or** note if the runner leg closed dead
  (the known script bug; expect this on ~1 in 4 trades).

## Deadline reality check

A demo needs **real forward time** to produce trades — backtests are instant,
live/paper is not. On XAUUSD 15m in default flip mode, expect roughly
**3–6 trades per week**.

- For a credible demo: **2 weeks minimum** (~8–12 trades).
- If the client deadline is shorter than that, the deliverable is "demo is
  live and executing correctly" + the validated backtest baseline — not a
  full statistical validation. State that distinction to the client plainly;
  do not present a 3-day demo as proof of performance.

## Step 5 — Report

Fill in `results/VALIDATION_REPORT_TEMPLATE.md` (copy to a dated file):
backtest vs demo table, divergence notes, execution-integrity checks, verdict.
Headline honestly: ~4%/5.5mo backtest baseline, low risk, runner leg bug noted.
