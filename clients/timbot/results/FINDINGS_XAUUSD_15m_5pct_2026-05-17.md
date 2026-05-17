# TIMBOT — XAUUSD 15m Backtest Findings, Order size 5% (2026-05-17)

Default mode (`signalOnFlipOnly = true`). Order size 5% of equity. $100k account.
Window 2025-11-30 → 2026-05-14 (~5.5 months). Supersedes the void 100% run.

## Numbers (as exported)

| Metric          | Value          |
|-----------------|----------------|
| Closed trades   | 218            |
| Win rate        | 69.7%          |
| Net profit      | +$3,960 (4.0%) |
| Profit factor   | 15.34          |
| Max drawdown    | $53 (0.05%)    |
| Avg win / loss  | +$27.87 / -$4.18 |
| Largest win     | $369.78        |
| Largest loss    | -$38.14        |
| Margin calls    | 0  ✅           |

## What's fixed

The sizing correction worked: **0 margin calls** (was 56). Positions are now a
sane ~$2,500 notional (~0.55 oz) on the $100k account. The result is no longer
trading on broken max leverage.

## The real result

**+4.0% over 5.5 months** — roughly **8–9% annualised**. Modest, positive, and
low-risk. That is the honest headline number, not "114%".

## What is STILL not trustworthy

**Profit factor 15.34 and 0.05% max drawdown remain unrealistic** — and this
time it is NOT a sizing artifact. It is structural, from two things:

1. **Exit-leg fragmentation / dead runner leg.** 56 of the 218 "trades" (26%)
   are "HTF runner" exits that closed for ~commission only (about -$0.50 each).
   The runner target keeps being set at a price already passed, so that leg
   fills instantly and dead on the entry bar. A quarter of the "trade list" is
   commission dust, not trades. So trade count, win rate and PF are computed on
   fragments — they do not describe 218 real round-trip trades (there are only
   ~110 actual entries, each split into legs).

2. **The strategy is always-in and flips on the 4H bias.** It exits one
   position by reversing into the opposite one ("L"/"S" exits). It rarely takes
   a real stop-loss hit, because the next bias flip closes the position first.
   That makes losses structurally tiny (avg -$4) — which inflates PF and
   crushes drawdown. It is not edge; it is the exit mechanic.

**Do not quote PF 15 or "0.05% drawdown" to the client.** They are distorted.

## Honest assessment

- The strategy is a **low-risk, always-in trend-follower** keyed off the 4H
  bias flip. In this backtest window gold trended hard up ($4,200 → $4,700,
  with a spike to $5,500). An always-in flip strategy makes modest money in a
  strong one-directional trend — which is exactly what happened.
- **+4% / 5.5 months is plausible and real**, but it is **regime-dependent**.
  In a ranging / choppy market this same always-in flip behaviour would likely
  bleed via repeated small reversals. 5.5 months is a short, single-regime
  sample — it does not prove the strategy survives a range.
- The **runner leg is broken** (fires dead 26% of the time), so the "let the
  winner run to HTF 78.6%" half of the design is effectively not working.
  Favorable-excursion data shows many trades gave back open profit. This is a
  script-logic bug; the client chose to keep the script — flag it, their call.

## Verdict for the engagement

This 5% run **is a usable demo baseline** — with the right framing:

- Headline to the client: **~4% over 5.5 months, ~70% win rate, very low risk
  per trade, 0 margin calls.** Modest but real.
- Caveats to state plainly: short single-regime sample; runner leg not
  functioning; PF/drawdown figures are distorted and should not be headlined.
- Recommended next step: proceed to demo (Phase 2) with **modest expectations**
  — this is a single-digit-annual-return, low-risk system on this evidence, not
  a high-performer. Optionally, backtest a ranging period (or a longer window)
  before the demo to see how it behaves outside a strong trend.
