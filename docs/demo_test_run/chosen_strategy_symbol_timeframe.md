# NovaTrade Demo Test Run — Strategy, Symbol, Timeframe Selection

**Phase:** 0 (Scope Freeze) — AMENDED
**Date:** 2026-03-16 (original) | **Amended:** 2026-03-16
**Status:** APPROVED (amended — strategy changed from EMA Crossover to Rob Hoffman IRB)

---

## Amendment Notice

This document was originally approved on 2026-03-16 with EMA Crossover 9/21 as the selected strategy. The operator has directed a strategy change to the Rob Hoffman Inventory Retracement Bar (IRB) strategy. This amendment replaces the strategy selection while preserving the symbol and timeframe rationale where still applicable.

**Previous strategy:** EMA Crossover 9/21
**New strategy:** Rob Hoffman Inventory Retracement Bar (IRB)
**Reason for change:** Operator directive — IRB is a credible, competition-proven, publicly documented strategy with stronger institutional logic than a generic EMA crossover.

---

## 1. Chosen Symbol: EURUSD (UNCHANGED)

**Broker symbol:** EURUSD.sim
**Rationale (unchanged from original):**
- Already verified in FTMO preflight (13/13 PASS, bid=1.14313, ask=1.14316)
- Most liquid forex pair globally — tightest spreads, deepest liquidity
- Default recommendation from project docs (Fast Deployment Plan §8.1)
- FTMO standard account includes EURUSD
- NovaTrade risk defaults (5% daily DD, 30-point spread ceiling) are calibrated for major pairs

**Additional IRB-specific justification:**
- The forex-specific IRB source document (rob_hoffman_irb_forex_full.pdf) provides a worked example explicitly on **EUR/USD H1 continuation long** — this is a directly source-grounded pairing
- IRB recommended backtest grid lists EURUSD as the first instrument in the "6-10 liquid FX pairs" set
- EURUSD has the tightest spreads of any major, which is important because IRB uses **stop orders** (buy-stop / sell-stop) that are sensitive to spread and slippage at trigger

**Rejected alternatives (unchanged):**
- GBPUSD: Verified in preflight but wider spreads and more volatile — adds unnecessary complexity for a systems test
- USDJPY: Verified in preflight but JPY pairs have different pip conventions (2 decimals vs 4) — adds an avoidable edge case for the first run

---

## 2. Chosen Timeframe: H1 (UNCHANGED — with new MTF requirement noted)

**Rationale (core arguments unchanged):**
- Matches the first timeframe configured in env: `NOVATRADE_TIMEFRAMES=H1,H4`
- Generates enough bars for meaningful validation over 10 calendar days (~240 H1 bars)
- Not so fast that MetaApi latency matters (MetaApi adds 10-50ms; irrelevant for H1 decisions)
- Operational simplicity — one candle per hour, clean bar-close logic
- Architecture Decision Report §8 confirms latency is irrelevant for strategies with minute+ holding periods

**Additional IRB-specific justification:**
- The forex PDF provides a worked schematic example on **H1 EURUSD** specifically — directly source-grounded
- The recommended backtest grid lists H1 as a core timeframe alongside M15, H4, and D1
- Hoffman lists "60-min" explicitly as a swing timeframe suitable for IRB
- H1 provides sufficient candle structure for the 45% geometry rule to be meaningful (large enough range for stop order placement with reasonable pip distances)

**New complexity: Multi-Timeframe (MTF) requirement**
- The IRB strategy **requires** the next-higher timeframe (H4) to be trending in the same direction as the H1 trend
- This is documented in the source materials as "critical" — not optional
- The original charter excluded MTF "unless strategy spec requires it" — the IRB strategy spec requires it
- **Consequence:** H4 trend direction checking must be brought into scope for the strategy specification
- This adds one indicator computation (20 EMA on H4) and one directional alignment check
- It does NOT require H4 trade execution — only H4 trend reading

**Why H1 remains the best choice despite MTF complexity:**
- H1 is the only timeframe with a directly worked EURUSD example in the source materials
- M15 would require M60 (H1) MTF confirmation — same complexity but more signals and higher monitoring burden
- H4 would produce too few bars (~60 in 10 days) and too few IRB signals for pipeline validation
- D1 would produce far too few signals (<10 trades likely)

**Rejected alternatives:**
- H4: Too few bars over 10 days (~60 bars) — may not generate enough IRB signals for operational validation
- M15: Unnecessarily fast for a first systems test; more signals but higher noise and monitoring burden
- D1: Far too few signals — insufficient for pipeline validation
- M5/M1: Hoffman's competition timeframes, but too fast for a first automated systems test

---

## 3. Chosen Strategy: ROB HOFFMAN INVENTORY RETRACEMENT BAR (IRB) (APPROVED)

### Why IRB Replaces EMA Crossover

The EMA Crossover 9/21 was selected as a maximally simple systems test strategy. The operator has directed a change to the Rob Hoffman IRB strategy. This is a valid upgrade because:

1. **Credible source with public specificity.** The IRB strategy is documented in TRADERS' magazine, Hoffman's BBT educational materials, WH SelfInvest platform tools, and multiple TradingView community indicators. This satisfies WP1's doctrine that strategies come from verifiable, extractable sources.

2. **Competition-proven.** Rob Hoffman has won 35+ live real-money trading competitions using IRB as his core method — including 3 consecutive Paris Salon du Trading wins (2012-2014) with real capital in elimination format. While competition results don't prove the setup alone was responsible, they demonstrate the strategy operates profitably under real-market, real-capital conditions.

3. **Still mechanical at its core.** The IRB candle geometry (45% rule) is fully deterministic. The entry (stop order beyond IRB extreme) is fully deterministic. The stop loss (opposite side of IRB) is fully deterministic. Only the trailing stop and trend filter require quantification choices for backtesting.

4. **Stronger institutional logic.** Unlike an EMA crossover (which is a pure trend-following signal with no market microstructure rationale), the IRB is grounded in a specific market behavior: institutional inventory clearing creates brief counter-trend pullbacks that are not true reversals. This gives the strategy a falsifiable thesis.

5. **Adequate public reference.** There are free TradingView indicators (UCSgears, Noski), platform tools (WH SelfInvest, NanoTrader), and pseudocode (forex PDF) that can be used as implementation references. Not as abundant as EMA crossover, but sufficient.

6. **Still a valid systems test.** The IRB is more complex than EMA crossover but still within the capability of a single-strategy pipeline test. If the pipeline can execute IRB correctly — with stop orders, trailing stops, MTF checks, and IRB detection — it proves more pipeline capability than a simple crossover would.

### What the IRB Strategy Is (from source materials)

The IRB is a **price-action trend-continuation setup** built on identifying a single "retracement bar" whose open and close sit deep inside the candle's range (>=45% away from the extreme), interpreted as a brief institutional counter-trend push that interrupts an existing trend.

**Core rules (all HIGH confidence from source materials):**

| Component | Rule | Source Confidence |
|-----------|------|-------------------|
| **IRB geometry** | Open AND close >=45% from extreme (high in uptrend, low in downtrend) | HIGH — authored description + glossary + code |
| **Trend filter** | 20 EMA sloping at ~45 degrees in trade direction | HIGH (directional) / MEDIUM (numeric threshold) |
| **MTF alignment** | Next-higher timeframe must trend in same direction | HIGH — stated as "critical" |
| **Entry** | Stop order 1 pip beyond IRB extreme (buy-stop above high, sell-stop below low) | HIGH — directly stated |
| **Stop loss** | Opposite side of IRB +/- 1 pip | HIGH — directly stated |
| **Trigger window** | Breakout should occur within 20 bars (preference, not hard rule) | MEDIUM — stated as preference |
| **IRB replacement** | New IRB replaces old pending order | HIGH — directly stated |
| **Trailing stop** | Protect 50% of profit, tighten to 90%+ near S/R | MEDIUM — concept clear, mechanics vary |
| **ATR filter** | Skip if IRB range >> ATR(10+) | MEDIUM — explicit warning, no hard threshold |
| **Risk per trade** | <1% of account | HIGH — directly stated |
| **Sideways filter** | Do not trade IRBs in ranging/choppy markets | HIGH — explicit prohibition |
| **Candle color** | Irrelevant — geometry matters, not green/red | HIGH — directly stated |

### Key Differences from EMA Crossover That Affect the Pipeline

| Aspect | EMA Crossover (previous) | IRB (new) | Pipeline Impact |
|--------|-------------------------|-----------|----------------|
| **Entry type** | Market order at bar close | Stop order (buy-stop/sell-stop) | Requires pending order management — new execution path |
| **Signal detection** | Simple EMA crossover | Candle geometry + trend filter + MTF check | More complex signal logic |
| **Stop loss** | Fixed 50 pips | Dynamic — opposite side of IRB candle | Variable stop distance per trade |
| **Take profit** | Fixed 75 pips | Trailing stop (no fixed TP) | Requires active trade management |
| **Position management** | Set-and-forget after entry | Active trailing stop adjustment | Requires ongoing position monitoring |
| **MTF data** | Not needed | H4 EMA(20) direction required | Additional data feed / indicator |
| **Pending orders** | Not used | Core mechanism (stop orders) | Order type management added |
| **State complexity** | 3 states (FLAT/LONG/SHORT) | More states (FLAT/PENDING_LONG/PENDING_SHORT/LONG/SHORT) | Larger state machine |

### What Is NOT Decided Yet

These are Phase 1 decisions — they require the Strategy Spec Agent to formalize quantitative choices:

| Item | Why Not Decided | Source Ambiguity |
|------|----------------|------------------|
| Trend filter quantification | "45-degree slope" is visual, not mathematical | Must choose: EMA slope / ATR normalization with threshold, or simpler rising/falling check |
| ATR overextension threshold | Source says "extraordinary range" but no hard k value | Must choose k (source suggests tuning in 1.5-3.0 range) |
| Trailing stop mechanics | Source gives concept (50% → 90% near S/R) but not exact algorithm | Must choose deterministic S/R proxy and trail implementation |
| Sideways market filter | Source says "avoid sideways" but no detection method | Must choose: ADX threshold, EMA flatness, or other |
| 20-bar trigger window enforcement | Source says "preference" — unclear if hard cancellation required | Must decide: hard cancel vs soft preference |
| Body-size filter | Community variant (body < 45% of range) — not in canonical description | Must decide: include or exclude |
| Position sizing method | Source says <1% risk — unclear if risk-based sizing or fixed lots | Must decide for first demo run |
| IRB invalidation logic | Source says exit if price crosses through opposite side | Must define: is this the stop loss, or a separate invalidation rule? |

---

## 4. Selection Criteria Used

From the project doctrine (unchanged):
- "Use a strategy that is mechanically clear" (Fast Deployment Plan §8.1) — **IRB satisfies: core geometry and entry rules are fully mechanical**
- "Not sub-second sensitive" (Fast Deployment Plan §8.1) — **IRB satisfies: stop orders are placed in advance, not latency-dependent**
- "Not dependent on exotic MT5 features" (Fast Deployment Plan §8.1) — **IRB satisfies: uses standard stop orders, EMA, and candle OHLC data**
- "Not requiring multiple simultaneous accounts" (Fast Deployment Plan §8.1) — **IRB satisfies: single account, single symbol**
- "Ideally one with enough public specificity to validate in TradingView" (First Steps V3 §Phase 2) — **IRB satisfies: free TradingView indicators exist (UCSgears, Noski)**
- "The demo run is a systems test, not a profit test" — **IRB is not expected to be profitable in a 10-day test; it is expected to be correctly executed**

Additional criterion for IRB:
- "Strategies should come from credible sources with public specificity — not from agent invention" (WP1 §2) — **IRB directly satisfies: authored by Rob Hoffman, publicly documented in TRADERS' magazine, competition-proven**

---

## 5. Source Materials

The following documents constitute the IRB source boundary for this project:

| Document | Location | Role |
|----------|----------|------|
| Rob Hoffman IRB Strategy (comprehensive breakdown) | `OUTPUT/rob_hoffman_irb_strategy.pdf` | Primary — 6 pages covering all core rules |
| Rob Hoffman IRB Strategy for Forex (full analysis) | `OUTPUT/rob_hoffman_irb_forex_full.pdf` | Primary — 16 pages with formal rule set, worked examples, backtest guidance, pseudocode |

See `docs/demo_test_run/irb_source_boundary.md` for the formal source boundary definition.
