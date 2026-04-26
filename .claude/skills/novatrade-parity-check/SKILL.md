---
name: novatrade-parity-check
description: "Verify that NovaTrade live FTMO trades match the IRB v5 backtest for a given window. Pulls live deals from MetaApi, runs the same window through the backtest engine, runs the parity diff, then classifies each divergence as uptime-gap / bar-feed / price-drift / strategy-drift / real-mismatch and reports a headline. Invoke on 'parity check', 'verify this week's trades', 'are live trades matching the backtest', 'live vs backtest for [date range]', 'did we trade what we should have', or any time the operator wants to know if live execution matches the validated baseline."
---

# NovaTrade Live vs Backtest Parity Check

## Overview

Answer a single question: **for a given date window, do the live FTMO trades match what the IRB v5 backtest would have produced on the same bars?** "Match" here is not strict equality — bar-feed jitter and execution slippage make pip-perfect agreement impossible. The skill grades divergences against known failure modes so the operator immediately sees whether a discrepancy is a real strategy bug or a known-and-classified artifact.

**Announce at start:** "I'm using the novatrade-parity-check skill to verify live vs backtest for <window>."

## When to use

- Operator asks to "verify this week's trades", "run a parity check", "compare live and backtest", "are live trades matching the backtest"
- After a multi-day live run, before claiming the live engine is faithfully executing the validated strategy
- After a code change to `novatrade/strategy/live_engine.py`, signal generation, or the backtest engine — to confirm parity didn't regress
- Before any decision that depends on live trades being a faithful proxy for the backtest (sizing changes, risk-budget reallocation, FTMO challenge progression)

**Don't use** for single-trade post-mortems (read the journal directly), or when the live engine wasn't running at all in the window (there's nothing to compare).

## The workflow

Three deterministic command-line steps, then one interpretation step. The first three are bundled into `scripts/run_parity_week.sh` so the model doesn't have to remember argument order or stamp filenames.

### Step 1: Pick the window

Default: **the current trading week** (Monday 00:00 UTC → now). Operator may specify other windows like "this week", "last 3 days", "Apr 20–24", or a single date.

Convert to two pieces:
- `--days N` for the live fetcher (covers now − N days back through now)
- `--start YYYY-MM-DD --end YYYY-MM-DD` for the backtest (end is **exclusive**)

Forex markets are closed Saturday and Sunday — a window that includes a weekend will have backtest trades on weekdays only. That's fine; just don't be confused if Sunday produces zero rows.

### Step 2: Run the wrapper

```bash
bash .claude/skills/novatrade-parity-check/scripts/run_parity_week.sh \
    --start 2026-04-20 \
    --end 2026-04-27 \
    --days 7
```

The wrapper runs three things in sequence and prints the three output paths:

1. `python3 scripts/fetch_ftmo_deal_history.py --days <N> --env-file OUTPUT/novatrade.env.updated`
   → `OUTPUT/parity/live_trades_<stamp>.csv`
2. `python3 scripts/backtest_window_for_parity.py --start <S> --end <E> --warmup-days 2 --env-file OUTPUT/novatrade.env.updated`
   → `OUTPUT/parity/backtest_trades_<stamp>.csv`
3. `python3 scripts/parity_report.py --live <live_csv> --backtest <bt_csv> --window-start <S> --window-end <E> --tolerance-min 15`
   → `OUTPUT/parity/parity_report_<stamp>.csv`

If any step fails, stop and surface the error to the operator — don't try to fabricate results from partial data.

### Step 3: Read the headline counters

The parity script prints a JSON summary like:

```json
{ "live_count": 21, "backtest_count": 32, "matched": 10,
  "outcome_match": 9, "outcome_flip": 1,
  "extra_in_live": 11, "missing_in_live": 22,
  "outcome_match_rate": 0.9 }
```

Capture these numbers. They're the headline for the report.

### Step 4: Classify each divergence row

Open `parity_report_<stamp>.csv`. Every row has a `status`. For each non-`MATCH` row, assign one of these categories — these are not arbitrary buckets, they map to **known failure modes documented in `OUTPUT/parity/bar_divergence_analysis_*.md` and our incident history**, and the goal is to separate "this is the system as-known" from "this is a new bug":

#### Category A: Uptime gap (live engine was offline)

**Signal:** A run of `MISSING_IN_LIVE` rows — typically 3+ entries within a single calendar day or across consecutive trading hours — when **no** live trades exist in the same span.

**Why it shows up:** the live daemon (`novacore-novatrade.service`) wasn't running, or was disconnected from MetaApi, so the backtest fired signals the live engine never received. Verify by spot-checking the journal:

```bash
sudo journalctl -u novacore-novatrade.service \
    --since "2026-04-20 00:00 UTC" --until "2026-04-21 00:00 UTC" \
    | head -30
```

If the journal is silent or shows reconnect loops in that span, the gap is uptime, not strategy.

#### Category B: Bar-feed divergence (known issue)

**Signal:** `EXTRA_IN_LIVE` rows that don't pair with anything in the backtest, *and* the live engine was demonstrably running at that timestamp (journal has `bar closed` lines).

**Why it shows up:** `OUTPUT/parity/bar_divergence_analysis_2026-04-24.md` documents that **~96% of M5 bars** between the live engine's real-time MetaApi feed and the retrospective `HistoricalFetcher` differ by **0.5–3 pips** on at least one of OHLC. Different bars → different IRB inside-bar detection → live takes setups the retrospective backtest doesn't see, and vice versa. This is a **known platform-level limitation, not a strategy bug**.

If you see `EXTRA_IN_LIVE` setups, reference that analysis explicitly in the report. Don't re-investigate it — point to the existing artifact.

#### Category C: Price drift on a paired trade

**Signal:** A `MATCH` or `OUTCOME_FLIP` row where `entry_price_diff_pips` or `sl_diff_pips` is greater than ~2 pips in absolute value.

**Why it shows up:** Same signal fired in both runs, but the live engine got a different fill (slippage, spread, MetaApi vs. retrospective bar OHLC for the trigger bar). On `OUTCOME_FLIP` rows specifically, this is the most common cause — the SL was placed a pip or two differently and one bar wick caught one but not the other.

A price-drift OUTCOME_FLIP is **not strategy drift** — same signal, different fill. Note it but don't escalate.

#### Category D: Strategy drift (real bug)

**Signal:** `OUTCOME_FLIP` rows where entry and SL drift are both small (≤2 pips) yet the trade resolved differently. Or `EXTRA_IN_LIVE` / `MISSING_IN_LIVE` rows where the live engine was up, the bars agree (no Category B evidence), and the signal logic itself disagreed.

**Why it matters:** This is the only category that implies the live strategy code has actually diverged from the validated baseline. **Stop and investigate before recommending any further live trading.**

#### Category E: Real mismatch (unclassified)

Anything not fitting A–D. Treat as Category D until proven otherwise.

### Step 5: Write the report

Use this exact template — operator scans it for the "Strategy drift?" line first:

```markdown
## Parity check — <window>

**Headline:** <live_count> live / <backtest_count> backtest entries.
Matched <matched> pairs, outcomes agree <outcome_match>/<matched> (<outcome_match_rate>).

| Category | Count | Notes |
|---|---|---|
| Uptime gap | <N> | <which days/hours, e.g., "Apr 20 all day; Apr 24 10:20 UTC onward"> |
| Bar-feed divergence | <N> | Known issue per `OUTPUT/parity/bar_divergence_analysis_*.md` |
| Price drift on paired trades | <N> | Avg entry drift <X> pips, SL drift <Y> pips |
| Strategy drift (REAL BUG) | <N> | <list rows or "none"> |
| Unclassified | <N> | <list rows or "none"> |

**Strategy drift?** <Yes / No>. <One-line action: "investigate row at <ts>" / "no live-engine code change required">.

**Artifacts:**
- Live: `OUTPUT/parity/live_trades_<stamp>.csv`
- Backtest: `OUTPUT/parity/backtest_trades_<stamp>.csv`
- Parity: `OUTPUT/parity/parity_report_<stamp>.csv`
```

Keep it tight. The operator already trusts the workflow — they want the answer to "should I worry?" first, supporting evidence second.

## Edge cases

- **Zero live trades, zero backtest trades:** weekend/holiday window, or live engine fully down with no signals in the backtest either. Report "no trades in window" and stop — there's nothing to grade.

- **Zero live trades, many backtest trades:** the live engine never opened a position. Either it was down the whole window (Category A) or there's a guard/halt blocking entries. Check `HardRiskSupervisor` halt state and the journal before assuming Category A.

- **Live trades in a window the backtest can't fetch:** `HistoricalFetcher` occasionally returns a short bar count. If `m5_bars` looks low for the window length (a 5-day window should produce ~1400 M5 bars on weekdays), rerun the backtest before grading — partial bar data will produce false `MISSING_IN_LIVE` rows.

- **OUTCOME_FLIP with massive PnL difference:** the backtest uses position sizing from `irb_v5_m5_champion.yaml`; live sizing is set by the FTMO challenge runtime. PnL magnitudes will not match. Compare **outcome signs** (WIN/LOSS), not dollar amounts.

## Why this skill exists (theory of operation)

The locked baseline strategy ("Rob Hoffman IRB v5 — Relaxed Reliable Build") was validated against 24 years of historical data. The live engine is meant to be a faithful runtime of that backtest. Every divergence is either (1) a known platform artifact we already understand and have decided to live with, or (2) evidence the runtime has drifted from the validated logic — which means the validation no longer applies.

The classification step is what separates this skill from a generic "diff two CSVs" report: it answers the operator's actual question, which is never "are the trades identical" (they can't be) but "is the live engine still running the strategy I validated?". Any divergence that lands in Category D invalidates that claim until investigated.
