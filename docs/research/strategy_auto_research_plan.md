# Strategy Auto-Research Plan — Robustness Search on Real-Tick Fidelity

**Status:** DRAFT (2026-06-09) — written while EURUSD tick history downloads.
**Author:** orchestrated research loop, Karpathy-recipe spine + quant overfitting rigor.
**Prime directive:** find a *forward-robust* edge or *prove there isn't one* — never maximize in-sample profit.

---

## 0. Why this plan exists (the lessons it must encode)

This plan is a direct response to a documented failure (`MEMORY/project_live_cost_stop_discovery`):
a backtest showed **+$1M** on IRB v5 while the live demo bled, because the bar engine's
intra-bar fill *heuristic* manufactured a phantom **~0.13R/trade** edge. Under honest M5-resolved
fills the edge collapsed to **−0.014R**. Five independent bar-engines "agreed" only because they
shared the same intra-bar lie.

Three lessons are now law for any optimization:

1. **Fidelity is the test, not engine agreement.** All evaluation runs on the fidelity engine
   (`novatrade/backtest/fidelity.py`) with real-tick fills. Bar-fill results are inadmissible.
2. **The objective is robustness, not profit.** "Maximize backtest profit" is the exact mechanism
   that produced the phantom edge. The optimizer is a lie-amplifier; the adversary is overfitting.
3. **Edges here are tiny (~0.06–0.12R) and near the noise floor.** Most "improvements" will be
   noise. We must deflate for the number of trials or we will fool ourselves at scale.

---

## 1. Objective, success criteria, kill criteria

**Objective:** identify a strategy configuration *or structure* with a positive net-of-cost edge
that survives walk-forward out-of-sample, real-tick fills, realistic spread+commission, AND a
sealed hold-out — or conclusively demonstrate the IRB family has no such edge.

**Success (a candidate ships to paper) requires ALL of:**
- Positive mean net-R/trade on **walk-forward out-of-sample** (not in-sample).
- **Deflated** performance metric positive after correcting for trial count (DSR / reality check).
- **Plateau stability**: edge holds across a neighborhood of parameters (no sharp spike).
- Survives the **sealed hold-out** (last ~18–24 months), tested exactly once.
- **Economic rationale** stated (why should this edge exist?) — not "it just works".
- Net-of-cost positive at the **real measured spread** (~0.2p liquid / wider off-session) + commission.

**Kill (declare the family dead, pivot structure):**
- Nothing clears walk-forward after Phase 3, or every survivor dies on the hold-out.
- A clean negative result is a **win** — it's the cheap proof that saves the next 6 months.

**Anti-goals (auto-fail a run):** any result on bar fills; any metric without deflation; any
candidate chosen by peeking at the hold-out; in-sample cherry-picking.

---

## 2. The substrate (non-negotiable)

| Component | Source |
|---|---|
| Eval engine | `novatrade/backtest/fidelity.py` — extend to consume **raw ticks** as the intra-bar path (today it uses sub-bars; ticks are the limit) |
| Fills | entry at **ask**, exit at **bid** from real tick quotes; not a flat cost assumption |
| Cost | real spread (from ticks) + commission (per-lot) + a slippage stress band |
| Data | EURUSD ticks 2016→2026 (`data/ticks/EURUSD/*.parquet`); later GBPUSD/USDJPY for cross-instrument robustness |
| Metric core | per-trade **R** (sizing-independent), plus deflated Sharpe; never raw compounded $ (path/ruin-dependent — see the buggy-PF incident) |

---

## 3. Data splits & validation protocol — the 90%

This section is the actual point of the plan. Get it wrong and the whole thing is theater.

- **Walk-forward (anchored or rolling):** optimize on a window of N months → score on the *next*
  unseen M months → step forward. The reported edge is the *concatenation of out-of-sample slices
  only*. In-sample numbers are diagnostics, never results.
  - Start: N=24 train, M=6 test, step=6 (tune later).
- **Sealed hold-out:** reserve the most recent ~18–24 months. **Touched exactly once**, at the very
  end, for the single chosen candidate. If we look more than once, it's contaminated.
- **Multiple-testing control:** log every trial. Apply **Deflated Sharpe Ratio** (López de Prado) or
  **White's Reality Check / Hansen SPA** to discount the best-of-K. A 2-Sharpe winner out of 5,000
  trials is noise.
- **Plateau test:** for any candidate, perturb each parameter ±1–2 steps; the edge must degrade
  gracefully, not fall off a cliff. Sharp optima = overfit.
- **Sub-period / regime consistency:** the edge shouldn't live entirely in one year (e.g., 2020).
  Report per-year out-of-sample R; require it not be a single-regime artifact.
- **Cross-instrument (gate, later):** a real structural edge usually generalizes; test survivors on
  ≥2 other majors before trusting.

---

## 4. The loop (Karpathy recipe, mapped)

> One change at a time. A logbook entry per candidate (hypothesis → one change → honest OOS R →
> keep/kill). Visualize everything. Trust no one, including ourselves.

- **Phase 0 — Become one with the data.** *No optimization.* Tick microstructure: spread by session
  and year, gap behavior, the real intra-bar path distribution (how often does stop precede target
  in the same bar — the thing the heuristic guessed). When does IRB actually win vs lose? Hand-read
  the worst and best trades on ticks. Output: a data-understanding note + the slippage/spread model.
- **Phase 1 — Skeleton + dumb baseline.** Wire: fidelity-on-ticks eval + walk-forward harness +
  deflation + logbook + reproducible seeds. Baseline = current canonical IRB. Gate: the pipeline
  produces honest, reproducible, deflated OOS numbers end-to-end. (No search yet.)
- **Phase 2 — Overfit-to-verify.** Confirm the search *can* find in-sample edge on a single window —
  this validates the search machinery and space, and is treated as the **disease** we then cure.
- **Phase 3 — Regularize (the whole game).** Walk-forward + deflation + plateau on a small,
  hand-chosen set of variants. Most candidates die here. **Go/no-go gate:** does *anything* survive
  walk-forward with a positive deflated edge? If no → likely kill the IRB family.
- **Phase 4 — Tune.** Search the space: random → Bayesian (e.g. Optuna) over parameters *and*
  structural variants. Every candidate scored by Phase-3 gauntlet. Constrain dimensionality
  aggressively (fewer free knobs = less overfit).
- **Phase 5 — Squeeze.** Only if a robust core survives: regime gating, light ensembling,
  sizing/risk overlay. Then, and only then, the sealed hold-out test.

---

## 5. Search space (start narrow, expand by evidence)

- **IRB parameters:** EMA periods, IRB %, ATR multipliers, stop/entry buffers, signal-expiry,
  time-stop, partial/runner logic, breakeven, session filter, max-trades/day, cooldown.
- **Structural variants:** entry trigger (stop vs limit vs confirmation), exit logic (trail type,
  target structure), trend filter variants, volatility regime gates.
- **Timeframe & instrument:** primary TF, HTF, instrument.
- **Beyond IRB:** if the IRB family dies in Phase 3, the search must be allowed to swap the strategy
  *structure* (this is a structure search, not a knob search). Tuning knobs cannot create an edge
  that isn't there.
- **Dimensionality discipline:** prefer structural priors over many free parameters. Each added
  degree of freedom is a unit of overfitting risk and must earn its place.

---

## 6. Infrastructure & orchestration

- **Fan-out evaluation:** candidates evaluated in parallel (multi-agent / workflow orchestration);
  survivors get an independent adversarial re-check (a skeptic agent that tries to *refute* each
  surviving edge — re-runs on a different split, perturbs params, stress-tests cost).
- **Logbook:** append-only; every candidate's hypothesis, single change, deflated OOS R, decision.
- **Reproducibility:** pinned engine commit, versioned tick data, fixed seeds; a candidate is a
  config blob + structure id that reproduces exactly.
- **Reuse:** the multi-engine cross-validation framework remains a *secondary* sanity check, but the
  fidelity-on-ticks engine is the source of truth.

---

## 7. Failure modes & honesty rails

- **Most likely outcome:** the IRB family has no robust edge; the search confirms it and we pivot.
  Budget for this and treat it as success (we'll *know*, cheaply).
- **Self-deception guards** (the things that fooled us): never trust a single backtest number;
  cross-check equity vs summed-PnL (caught a buggy PF this session); never read the hold-out twice;
  deflate every "winner"; a plateau beats a peak; an economic story beats a curve.
- **Cost honesty:** the demo broker's 1.75p spread was ~9× the real ~0.2p — but the edge died even
  at 0.2p. Use the *real* measured spread, and a slippage stress band, not an optimistic constant.

---

## 8. Decision gates & sequence

1. **Phase 0–1 complete** → honest pipeline + data understanding (no edge claims).
2. **Phase 3 go/no-go** → does anything survive walk-forward with positive deflated R?
   - No → write the negative-result memo, pivot to a new structure (or stop).
   - Yes → proceed to tune.
3. **Phase 4–5** → a single best candidate emerges, robust and plateau-stable.
4. **Sealed hold-out** → tested once. Pass → paper trade (and re-run the *live-vs-fidelity* parity
   check after a window). Fail → back to Phase 4 or kill.

No live capital until a candidate clears the hold-out **and** a paper window matches the fidelity
backtest within tolerance.

---

## Appendix — concrete first tasks (when ticks land)

- [ ] Extend `fidelity.py` to accept a raw-tick path source (ticks as the intra-bar sequence).
- [ ] Build the walk-forward + deflation harness (`novatrade/backtest/research/`).
- [ ] Phase 0 data-understanding note from the tick data (spread/session/regime, intra-bar order stats).
- [ ] Re-run the IRB-H1 fidelity test on **real ticks** as the first honest baseline number.
- [ ] Logbook scaffold + reproducibility (pinned commit + data version + seeds).
