# NovaTrade Demo Test Run — IRB Source Boundary

**Phase:** 0.5 (Strategy Baseline Change)
**Date:** 2026-03-17
**Status:** LOCKED
**Purpose:** Define the strict separation between what NovaTrade adopts as the test-run strategy baseline and what remains unresolved for later formalization.

---

## Governing Principle

NovaTrade does not invent strategies. It implements strategies from credible, publicly documented sources. The IRB source boundary defines exactly which rules are adopted from the source materials and which decisions remain open for the Strategy Spec Agent to quantify.

**Later phases cannot smuggle in discretionary assumptions.** Every rule in the strategy spec must trace back either to a HIGH-confidence source rule (adopted as-is) or to a documented MEDIUM-confidence rule with an explicit quantification choice and its rationale.

---

## Source Documents

| ID | Document | Location | Authority |
|----|----------|----------|-----------|
| S1 | Rob Hoffman IRB Strategy (comprehensive breakdown) | `OUTPUT/rob_hoffman_irb_strategy.pdf` | Primary |
| S2 | Rob Hoffman IRB Strategy for Forex (full analysis) | `OUTPUT/rob_hoffman_irb_forex_full.pdf` | Primary |

No other sources are authoritative for rule derivation. TradingView indicators (UCSgears, Noski), WH SelfInvest platform tools, and community forum discussions are **reference implementations**, not rule sources. They may inform implementation but cannot override S1/S2.

---

## ADOPTED: Rules NovaTrade Accepts As Baseline (HIGH Confidence)

These rules are grounded in the source materials with sufficient precision for direct implementation. The Strategy Spec Agent MUST implement them without modification.

### A1. IRB Candle Geometry (The 45% Rule)

**Source:** S1 p1-2, S2 p2

**Uptrend IRB:** Open AND close must be >= 45% below the candle's high, relative to its total range.
```
threshold_from_high = high - (0.45 * (high - low))
is_uptrend_irb = (open <= threshold_from_high) AND (close <= threshold_from_high)
```

**Downtrend IRB:** Open AND close must be >= 45% above the candle's low, relative to its total range.
```
threshold_from_low = low + (0.45 * (high - low))
is_downtrend_irb = (open >= threshold_from_low) AND (close >= threshold_from_low)
```

**Candle color is irrelevant.** No extra weight based on whether the candle is green or red.

### A2. Entry via Stop Order

**Source:** S1 p3, S2 p3

- **Long entry:** Buy-stop order at IRB high + 1 pip (buffer)
- **Short entry:** Sell-stop order at IRB low - 1 pip (buffer)

Entry is conditional — price must break the IRB extreme to trigger the fill.

### A3. Stop Loss on Opposite Side of IRB

**Source:** S1 p3, S2 p3, p6

- **Long trade:** Stop loss at IRB low - 1 pip
- **Short trade:** Stop loss at IRB high + 1 pip

This is a **thesis stop** — the IRB defines a local structure, and if price crosses through the opposite side, the continuation thesis is invalidated.

### A4. IRB Replacement Rule

**Source:** S1 p3, S2 p8

If a new qualifying IRB forms before the previous IRB's stop order is triggered, the new IRB **replaces** the old one. The pending order moves to the new IRB's levels.

### A5. Risk Per Trade

**Source:** S1 p4, S2 p6

Risk less than 1% of account equity per trade. Stop distance (from entry to SL) determines position size.

### A6. No Trading in Sideways Markets

**Source:** S1 p4, S2 p4

The strategy is designed exclusively for trend continuations. It must not be used in sideways/ranging/choppy conditions.

### A7. Trend Direction via 20 EMA

**Source:** S1 p2, S2 p3

A 20-period EMA applied to the trading timeframe determines trend direction. The EMA should be sloping at approximately 45 degrees over the last 20 bars.

### A8. Multi-Timeframe Alignment (Critical)

**Source:** S1 p2, S2 p3

The next-higher timeframe must be trending in the same direction as the trading timeframe. For H1 trading, H4 must also show trend alignment. If the higher timeframe is sideways or opposite, the setup has "significantly higher probability of failure."

### A9. Trailing Stop Philosophy

**Source:** S1 p3, S2 p3, p6

Hoffman does NOT use fixed take-profit targets. He uses a dynamic trailing stop:
- Trail stop to protect ~50% of open profit as trade moves favorably
- Tighten to 80-90% as price approaches major S/R levels
- Tighten to 90%+ at major S/R; exit if no further progress

### A10. ATR Overextension Warning

**Source:** S1 p4, S2 p6

If the IRB candle has an abnormally large range compared to ATR(10+), skip the trade. An overextended IRB means most move energy is consumed, leaving a large stop and small profit potential.

---

## UNRESOLVED: Decisions That Must Be Quantified in Phase 1

These rules are directionally clear in the source materials but lack the numeric precision needed for deterministic backtesting. The Strategy Spec Agent MUST make explicit quantification choices and document the rationale for each.

### U1. Trend Filter Quantification

**Source ambiguity:** "45-degree slope" is a **visual** description, not a mathematical one. Screen angle depends on chart scaling, zoom level, and aspect ratio.

**Phase 1 must decide:** How to operationalize "45-degree slope" as a deterministic formula.

**Options from source materials (S2 p3, p8-9):**
- **TF-A (literal EMA framing):** EMA(20) is rising for longs / falling for shorts, plus higher TF EMA(20) in same direction. Simple but permissive.
- **TF-B (quantified slope):** `(EMA20[t] - EMA20[t-20]) / ATR20[t] >= s` for longs (and <= -s for shorts), where `s` is a tunable threshold (suggested range 0.2-0.8). More restrictive, closer to "45-degree" intent.

**Constraint:** Phase 1 must choose ONE option and justify it. It may not leave this ambiguous.

### U2. ATR Overextension Threshold

**Source ambiguity:** Source says "abnormally large range compared to ATR" but provides no hard threshold.

**Phase 1 must decide:** The value of `k` in the filter `(High - Low) / ATR(14) <= k`.

**Guidance from source:** S2 p9 suggests tuning `k` in range 1.5-3.0 during robustness testing.

### U3. Trailing Stop Mechanics

**Source ambiguity:** The concept is clear (50% → 90% near S/R) but the exact algorithm varies between source descriptions. The magazine version is less prescriptive; a 2017 presentation copy provides a more mechanical "50 → 80 → 90 → to-price" trail sequence.

**Phase 1 must decide:** Which deterministic trailing stop variant to implement.

**Options from source materials (S2 p8-9):**
- **Exit-1 (S/R proxy trailing):** After MFE >= X·risk, trail at 50% of open profit; tighten to 90% when price touches a deterministic S/R proxy (pivot R1/S1, prior day high/low).
- **Exit-2 (ATR trailing):** Trail at `max(highest_close_since_entry - k·ATR(14), stop)` for longs. Approximates "don't give back profits."
- **Exit-3 (time stop):** Exit after T bars if neither stop nor trailing exit hit. Prevents infinite holds.

**Constraint:** Phase 1 must implement at least one deterministic variant. It may recommend testing multiple variants but must choose one as the baseline.

### U4. Sideways Market Detection

**Source ambiguity:** Source explicitly warns against sideways markets but provides no detection method.

**Phase 1 must decide:** How to detect and avoid sideways/ranging conditions.

**Options:**
- ADX(14) < threshold (e.g., 18-22) → no trade
- EMA(20) flatness: `abs(EMA20[t] - EMA20[t-20]) / ATR20[t] < threshold` → no trade
- Bollinger Band width below threshold

**Constraint:** Phase 1 must choose ONE method. This may overlap with U1 (trend filter) — if TF-B with slope threshold already filters sideways, a separate filter may be unnecessary.

### U5. Trigger Window Enforcement

**Source ambiguity:** Source says breakout should "ideally happen within the next 20 bars" and "sooner is better (next 5 bars is ideal)" — but states this as a "preference," not a hard rule.

**Phase 1 must decide:** Is 20 bars a hard cancellation window or a soft preference?

**Recommendation from source (S2 p3, p8):** Implement as a hard window (default N=20, sweepable 10-40). This aligns with the IRB replacement rule and prevents stale pending orders.

### U6. Body-Size Filter

**Source ambiguity:** Community implementations add `body < 0.45 * range` as a filter. The canonical Hoffman description does NOT state this as mandatory — it is a **variant** (S2 p2).

**Phase 1 must decide:** Include or exclude this filter.

**Recommendation:** Exclude for baseline (stick to canonical O+C threshold only). Test as a variant during robustness analysis.

### U7. Position Sizing Method

**Source:** <1% risk per trade is stated. But the demo run's previous approach used fixed lots (0.10).

**Phase 1 must decide:** Risk-based sizing (lot size computed from stop distance and 1% equity risk) or fixed lots.

**Consideration:** Risk-based sizing is more faithful to the source but adds equity-computation complexity. Fixed lots are simpler for a first systems test but waste the stop-distance information.

### U8. Trade Invalidation vs Stop Loss

**Source ambiguity:** S1 p3 says "if price crosses through to the opposite side of the IRB after entry, exit immediately at a loss." S2 p6 frames this as: "price should not retrace back beyond the opposite side of the IRB."

**Phase 1 must decide:** Is this the same as the stop loss (A3), or a separate invalidation rule? If the stop is at IRB_low - 1 pip, the invalidation ("crosses through opposite side") is essentially the stop being hit. Clarify whether these are redundant or whether invalidation adds a separate exit condition.

---

## EXCLUDED: Rules That NovaTrade Will NOT Implement

These are either proprietary, insufficiently documented, or out of scope for the first demo run.

| Item | Why Excluded |
|------|-------------|
| Reverse IRBs | Advanced variant mentioned in live room — not in core public description |
| IRB Trackers | Proprietary indicator (Hoffman's WealthCharts) |
| Champion Cross | Proprietary indicator |
| Breakout Forecasters | Proprietary indicator |
| ITP (Institutional Trader Package) | Proprietary indicator bundle |
| Speed Lines (MA 3/5 on typical price) | BBT glossary item — not part of core IRB setup |
| Discretionary S/R identification | Source lists Fibonacci, pivots, prior highs/lows as examples — Phase 1 must use deterministic proxies only |
| Multiple simultaneous IRB trades | Out of scope — one position at a time for first demo |
| Pre-market timing rules (9:40 AM ET wait) | Equity-market specific — not applicable to 24/5 forex |

---

## Traceability Requirements

Every rule in the Phase 1 StrategySpec must carry a **source tag**:

- `[A1]`-`[A10]` for adopted rules (must match source exactly)
- `[U1]`-`[U8]` for quantified rules (must document the choice and its rationale)
- `[EXCLUDED]` for any rule explicitly rejected

Any rule without a source tag is a **smuggled assumption** and must be challenged.
