# TIMBOT — XAUUSD 15m Backtest Findings (2026-05-17)

Default mode (`signalOnFlipOnly = true`). Window 2025-11-30 → 2026-05-14 (~5.5 months).

## Headline numbers (as exported)

| Metric          | Value         |
|-----------------|---------------|
| Closed trades   | 274           |
| Win rate        | 55.5%         |
| Net profit      | +$114,029 (114%) |
| Profit factor   | 14.42         |
| Max drawdown    | $1,854 (1.02%) |

**These numbers are NOT usable as a validation baseline.** They are the
product of broken position sizing and a fragmented exit engine, not trading
edge. Detail below.

## Red flag 1 — 56 margin calls (20% of all trades)

The broker emulator issued a "Margin call" on 56 of 274 trades. A margin call
means the strategy tried to hold a position larger than the account margin
allows, and the emulator force-liquidated part of it.

- 1 in 5 trades is over-leveraged enough to be forcibly cut.
- On a real demo/live account this is rejected orders or forced liquidation
  at bad prices — not a clean fill.
- Root cause: the order-size setting in Properties was set so each position is
  ~50% of equity ($50k–$210k positions on a ~$100k account). Gold's leverage
  plus the strategy's frequent position reversals exhausts available margin.

This is a **settings** problem (Properties → Order size), not a script-code
problem — so it can be fixed without touching the client's script.

## Red flag 2 — Profit factor 14.4 and 1% drawdown are not real

- Real, sound strategies sit around PF 1.2–2.5. PF 14 means losses are being
  artificially suppressed.
- A 114% return with a 1.02% max drawdown is not achievable by any genuine
  strategy. The two figures together are mathematically a tell that the
  backtest is mis-modelling something — here, the trade fragmentation below.

## Red flag 3 — Exit engine fragments every position into "fake" trades

The "274 trades" are not 274 independent trades. The script fires
`strategy.entry` plus three overlapping `strategy.exit` legs (P1 / P2 / BE),
and reverses on the opposite signal. TradingView splits this into many
counted "trades":

- 25 "HTF runner" exits closed for ~commission only (|P&L| < $25). The runner
  limit price was set at a level price had **already passed**, so that leg
  filled instantly on the entry bar — a dead leg, every time it happens.
  (In the script: `finalLongTarget = next786Top > close ? next786Top :
  nextHigh` — `nextHigh` can be below the entry, making the limit fill at once.)
- Trades #1 and #2 both "enter long" at the same timestamp and price
  (2025-11-30 23:15) — one entry, counted as two trades.

So win rate, trade count, and PF are all computed on fragments, not on real
round-trip trades. The statistics are not measuring what they appear to.

## Exit-reason breakdown

| Exit reason  | Count | Net P&L    |
|--------------|-------|------------|
| 2R partial   | 83    | +$51,574   |
| HTF runner   | 72    | +$33,610   |
| Margin call  | 56    | -$169      |
| L (reverse)  | 36    | +$14,974   |
| S (reverse)  | 27    | +$14,039   |

## What this means for the engagement

The strategy script may be fine — but **this backtest run cannot validate it**.
Before any number from TIMBOT can be trusted we must re-run the backtest with:

1. **Correct order sizing.** Set Properties → Order size to a sane fixed risk
   (e.g. 1–2% of equity per trade, or a fixed small contract size) so no trade
   can exceed margin. Target: **zero margin calls.** Settings-only fix.
2. **Realistic costs.** Confirm commission matches the demo broker and set
   slippage to 1–3 ticks (currently appears to be 0).
3. **Re-export and re-analyse.** If margin calls are gone and PF lands in a
   realistic 1.2–2.5 band, we have a real baseline.

The instant-runner-exit / leg-fragmentation behaviour (Red flag 3) is in the
**script logic**, not settings. The client asked to keep the script unchanged
— that is their call — but they should be told the runner leg is firing dead
on a meaningful share of trades, which drags on real performance. Document it;
let the client decide.

## Recommendation

Do not show the client "114% / PF 14" as a result — it will not survive
contact with a real account and sets a false expectation. Re-run with correct
sizing first, then report the honest baseline.
