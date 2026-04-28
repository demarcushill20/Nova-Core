# Pine v5 Exit-Timing Parity Audit Findings

**Date:** 2026-04-28
**Audit branch:** pine-exit-timing-audit (off main at b054d66)
**Source spec:** docs/superpowers/specs/2026-04-28-exit-timing-parity-audit-design.md
**Plan:** docs/superpowers/plans/plan-pine-exit-timing-audit.md
**Probe scaffold commits:** ed3bc73 (env field), c2669ec (StrategyConfig), 3e77f17/6904c28 (regression test), 5fc5468 (D2), 03dcb83 (D3), 48c4a97 (D1), c056174 (D12)
**Measurement commits:** 91ab9fc (baseline pin), 490ae67 (individual probes), d983cb0 (paired D2+D3)
**Pine baseline (pinned):** `data/irb_novatrade_irb_v5_results_extracted.csv` (SHA256: `1880ea5ec975465434fb59ccea10a9c704e18793dfdb0902bcf6675a25f48d8c`)
**Baseline metrics:** trades 1066, PF 0.888, coverage 56.6% (matched 682/1204)

## Audit Method

Static code-walk + toggle-driven empirical probes (`env.parity_audit_toggles: frozenset[str]`). Toggles are diagnostic-only; live config (`irb_v5_m5_champion.yaml`) is unaffected, enforced by `tests/test_backtest_engine.py::TestParityAuditToggleNoOp`.

### Tiering thresholds

- Tier-1: ≥10 mismatches resolved (audit success criterion)
- Tier-2: 1–9 mismatches resolved
- Tier-3: 0 (or negative) — falsified, inert, or verified-equivalent

---

## Headline Result

**Zero Tier-1 findings. Audit success criterion FAILED. Risk #3 triggered.**

The audit's four mechanistic hypotheses (D1, D2, D3, D12) collectively resolve **+1 mismatch** out of the 906-mismatch parity gap (522 pine_only + 384 python_only). The strongest single probe — D3, zero cooldown — resolves +9, sitting just below the Tier-1 threshold. D2 (the strongest hypothesized Tier-1 candidate, `strategy.close` next-bar-open) produced a **NEGATIVE** effect (-6), actively introducing new mismatches.

**Conclusion:** the remaining gap is a **diffuse leak**, not a single-mechanism error. Pine TV log capture (Risk #3 in the design spec) is the recommended next step — empirical bar-by-bar diff against the Pine state stream, not further code-walk hypotheses.

### Measured impact summary

| Toggle | trades | PF | matched | pine_only | python_only | pine_only_resolved | python_only_resolved | total_resolved | Tier |
|---|---:|---:|---:|---:|---:|---:|---:|---:|:---:|
| baseline | 1066 | 0.888 | 682 | 522 | 384 | — | — | — | — |
| d1_post_ratchet_stop_fill | 1066 | 0.897 | 682 | 522 | 384 | 0 | 0 | 0 | **3** |
| d2_strategy_close_next_open | 1062 | 0.895 | 677 | 527 | 385 | -5 | -1 | **-6** | **3 (NEG)** |
| d3_zero_cooldown | 1077 | 0.876 | 692 | 512 | 385 | +10 | -1 | **+9** | **2** |
| d12_gap_fill_at_open | 1066 | 0.880 | 682 | 522 | 384 | 0 | 0 | 0 | **3** |
| d2+d3 paired | 1070 | 0.887 | 683 | 521 | 387 | +1 | -3 | **-2** | (paired) |

Source: `data/parity_audit/results_20260428T172136Z.json` (individual), `data/parity_audit/results_20260428T172151Z.json` (paired).

---

## Per-Finding Entries

### D1 — Post-ratchet stop fill                                  [Tier-3]

**Pine source:** `configs/pinescript/irb_v5_stag.pine:725-733` — at bar close, `cur_stop := math.max(nz(cur_stop, ema_stop_long), ema_stop_long)` then `strategy.exit("Long Exit", "Long", stop = cur_stop)` registers an intra-bar stop at the post-ratchet level.
**Python source:** `novatrade/backtest/engine.py:879-880` (toggle branch) — when `d1_post_ratchet_stop_fill` is set, `_ratchet_trail_only(...)` ratchets `pos.current_stop` BEFORE the stop-loss check at `:891-904`.
**Divergence (hypothesized):** Python may have evaluated the stop-loss check against the *pre*-ratchet stop, so a bar that ratchets through its own low fills at the old level rather than the post-ratchet level. Toggle moves the ratchet ahead of the stop check.
**Probe:** `d1_post_ratchet_stop_fill`
**Measured impact:** pine_only_resolved 0, python_only_resolved 0, ΔPF +0.009, Δtrades 0.
**Verdict:** **INERT (on parity counts).** PF improves modestly (intra-trade PnL shifts) but neither pine_only nor python_only mismatch counts move. Whatever ordering difference exists, it does not affect *which* bars match.
**Status:** documented
**Notes:** PnL drift without coverage drift means D1 is changing fill prices on already-matched trades, not unlocking new matches. Not actionable for parity coverage.

---

### D2 — `strategy.close` next-bar-open semantics                 [Tier-3 — HYPOTHESIS FALSIFIED]

**Pine source:** `configs/pinescript/irb_v5_stag.pine:741, 751, 785, 795` — STAG_EXIT and TIME_STOP exits use `strategy.close("Long", comment = "STAG_EXIT")` / `strategy.close("Short", ...)`. Pine documentation states `strategy.close` fills at the *next bar's open*.
**Python source:** `novatrade/backtest/engine.py:918-952` (STAG/TIME_STOP exit logic) — current Python closes on the same bar at `bar.close`. Toggle branches at `:922` and `:947` defer the exit to the next bar's open when `d2_strategy_close_next_open` is set.
**Divergence (hypothesized):** Pine should be exiting one bar later at the open price; Python's same-bar-close was suspected to be the largest single Tier-1 contributor.
**Probe:** `d2_strategy_close_next_open`
**Measured impact:** pine_only_resolved -5, python_only_resolved -1, ΔPF +0.007, Δtrades -4.
**Verdict:** **HYPOTHESIS FALSIFIED.** Activating Pine's documented next-bar-open semantics in Python made parity *worse* by 6 mismatches. The new mismatches are *introduced* by deferring exits, not resolved.
**Status:** documented
**Notes:** The most plausible mechanism is that **Pine's actual runtime behavior for `strategy.close` is not faithful to its documentation in all cases** — possibly `strategy.close` fills same-bar in some configurations (e.g., when `process_orders_on_close = true` is set, or when other exit orders are pending), or interacts with `strategy.exit` order priority differently than docs suggest. Python's current same-bar-close on `bar.close` may already be accidentally aligned with Pine's actual behavior. Pine TV log capture would resolve this empirically. **Do NOT pursue a "fix" along this axis.**

---

### D3 — Zero cooldown                                            [Tier-2]

**Pine source:** Pine has no explicit cooldown logic in the v5 STAG branch. After `strategy.close`/`strategy.exit` fires, Pine's `strategy.entry` is free to fire on the very next bar that satisfies the entry conditions.
**Python source:** `novatrade/backtest/engine.py:457` — entry gate `and "d3_zero_cooldown" not in e.parity_audit_toggles` skips a `cooldown_bars` post-exit gate when set. v5 config sets `cooldown_bars: 1`.
**Divergence (hypothesized):** Python's 1-bar post-exit cooldown blocks bar-i+1 entries that Pine takes. Each blocked entry is one pine_only mismatch.
**Probe:** `d3_zero_cooldown`
**Measured impact:** pine_only_resolved +10, python_only_resolved -1, ΔPF -0.012, Δtrades +11. **Net +9.**
**Verdict:** **CONFIRMED but sub-Tier-1.** The mechanism is real: removing the cooldown unlocks 10 pine_only matches. But it falls below the Tier-1 threshold of 10 net resolved (one new python_only mismatch is also introduced), so it does not single-handedly close the gap. The +11 trades come with a -0.012 PF degradation, suggesting the unlocked matches are net-losing — the cohort Pine takes that Python skips loses money on average.
**Status:** documented; promotion-to-fix candidate (see Recommendations).
**Notes:** A +9 impact in a 906-mismatch gap is small relative to the live regression risk. Whether to promote depends on the brainstorm trade-off analysis.

---

### D12 — Gap-fill at open                                        [Tier-3]

**Pine source:** `configs/pinescript/irb_v5_stag.pine:733, 777` — `strategy.exit` gap-fill semantics: when the bar opens past the stop trigger, fill at `bar.open` rather than the trigger price.
**Python source:** `novatrade/backtest/engine.py:894, 901` (toggle branches) — when `d12_gap_fill_at_open` is set, longs filling on `bar.low <= stop` use `bar.open` if `bar.open < stop`, and shorts symmetric on `bar.open > stop`.
**Divergence (hypothesized):** A gapped-open bar should fill at the open, not the stop level. Python without the toggle uses the stop level.
**Probe:** `d12_gap_fill_at_open`
**Measured impact:** pine_only_resolved 0, python_only_resolved 0, ΔPF -0.008, Δtrades 0.
**Verdict:** **INERT (on parity counts).** Like D1, this shifts intra-trade PnL on already-matched trades (here, slightly worse PF from accepting open-price fills on adverse gaps) but does not change which bars match.
**Status:** documented
**Notes:** Could still matter for absolute PnL accuracy in fills-comparison runs, but irrelevant for the trade-level coverage parity goal.

---

### D4 — `peak_fav` definition                                    [Tier-3 verified-equivalent]

See `data/parity_audit/tier3_verifications.md#D4`. Both sides use `bar.high` (long) / `bar.low` (short) anchored on entry/avg fill price with monotonic-max accumulation. v5 has no partial exits, so `strategy.position_avg_price ≡ entry_price` for the entire position.
**Verdict:** EQUIVALENT.

---

### D5 — STAG adverse condition                                   [Tier-3 verified-equivalent]

See `data/parity_audit/tier3_verifications.md#D5`. Pine and Python agree on the stagnation guard's adverse-excursion definition.
**Verdict:** EQUIVALENT.

---

### D6 — STAG retest condition                                    [Tier-3 verified-equivalent]

See `data/parity_audit/tier3_verifications.md#D6`.
**Verdict:** EQUIVALENT.

---

### D7 — STAG bar-count threshold                                 [Tier-3 verified-equivalent]

See `data/parity_audit/tier3_verifications.md#D7`.
**Verdict:** EQUIVALENT.

---

### D8 — TIME_STOP bar-count                                      [Tier-3 verified-equivalent]

See `data/parity_audit/tier3_verifications.md#D8`.
**Verdict:** EQUIVALENT.

---

### D9 — Trail-EMA period & calculation                           [Tier-3 verified-equivalent]

See `data/parity_audit/tier3_verifications.md#D9`.
**Verdict:** EQUIVALENT.

---

### D10 — Same-bar TRAILING_STOP edge                             [Tier-3 verified-equivalent]

See `data/parity_audit/d10_code_walk.md`. Pine and Python agree on the post-ratchet stop level for trailing exits; the D12 follow-up branch (gap-fill) is documented above as inert.
**Verdict:** EQUIVALENT.

---

### D11 — Breakeven trigger                                       [Tier-3 verified-equivalent]

See `data/parity_audit/tier3_verifications.md#D11`.
**Verdict:** EQUIVALENT.

---

## D2+D3 Paired Interaction

```
paired_resolved (1, -3)   = -2
individual_sum (D2: -6, D3: +9) = +3
interaction = -2 - 3 = -5  (DESTRUCTIVE)
```

When D2 (next-bar-open exits) and D3 (zero cooldown) are both active, D3's gains are mostly cancelled by D2's regressions. The two probes appear to fix overlapping mismatch cohorts from different angles, with destructive interference at their boundary. This further reinforces the diffuse-leak conclusion: there is no clean superposition of mechanistic fixes.

---

## Recommendations

1. **Do NOT fix any Tier-1 findings — there are none.** The audit's success criterion was not met.

2. **Consider promoting D3 to a fix — but only after a fix-plan brainstorm weighs the +9 coverage gain against live regression risk.** The change is a one-line config (`cooldown_bars: 0` in `irb_v5_m5_champion.yaml`). Backtest impact: PF 0.888 → 0.876 (slight degradation), trades 1066 → 1077 (+11). The PF degradation suggests the resolved mismatches are *net-losing* — verify the unlocked-cohort PnL profile before promoting. A +9 parity gain that *worsens* PF is not obviously a win.

3. **Drop D1 and D12 from further investigation.** Both are inert on parity counts — they shift intra-trade PnL on already-matched trades but do not change which bars match. They are not parity-relevant, even though they may be PnL-relevant in absolute-fill-comparison contexts.

4. **D2 is falsified.** Do NOT pursue Pine `strategy.close` next-bar-open semantics as a Python fix. Python's current same-bar-close on `bar.close` may already be accidentally aligned with Pine's actual runtime behavior (despite the docs). The mechanism for Pine's actual `strategy.close` fill timing is unknown and should be resolved empirically via TV state capture, not code-walk.

5. **Pine TV log capture is the highest-leverage next step.** Re-run the Pine baseline in TradingView with debug logging enabled — Pine source already emits JSON debug logs at `configs/pinescript/irb_v5_stag.pine:859–965` (entry-event log block) plus the trail-update block at `:945–955` and the trigger-window block at `:989+`. Capture the per-bar Pine state stream, then diff bar-by-bar against Python's per-bar state to find the actual divergence mechanism. This is a **manual operator task** (TV session); estimate 1–2 hours.

6. **Toggle scaffold is preserved.** `env.parity_audit_toggles` and the four probes (D1, D2, D3, D12) remain in code, gated off by default. Future audit cycles can reuse them without re-implementing the plumbing. Do not remove inert probes — they are part of the experimental record.

---

## Risk #3 Triggered

The design spec's Risk #3 mitigation (run Pine TV log capture as fallback when probes are inconclusive) is now active. **The fix-plan brainstorm should not start until the Pine state stream has been captured.** Brainstorming against null hypotheses produces speculation, not designs.

---

## Live-Champion Verification

The live champion config (`configs/strategies/irb_v5_m5_champion.yaml`) does not set `parity_audit_toggles`, so its behavior is bit-identical to pre-audit. Verified by:

- `TestParityAuditToggleNoOp` regression test (passing throughout phases 1–9; last run on `d983cb0`).
- Phase 8 baseline smoke-test: 1066 trades / PF 0.888 / 56.6% coverage — matches the pre-audit memory snapshot exactly (`MEMORY/.../project_pine_parity_state.md`).
- No live-config files modified in this branch (verified: `grep -rn "parity_audit_toggles" configs/` returns nothing — toggles are env/runtime only, never serialized into champion configs).

---

## Artifacts

- Findings doc (this file): `docs/parity/exit-timing-audit.md`
- Probe results (individual): `data/parity_audit/results_20260428T172136Z.json`
- Probe results (paired D2+D3): `data/parity_audit/results_20260428T172151Z.json`
- Baseline reference: `data/parity_audit/baseline_reference.json`
- Tier-3 code-walks: `data/parity_audit/tier3_verifications.md`
- D10 code-walk: `data/parity_audit/d10_code_walk.md`
- Measurement harness: `scripts/parity_audit.py`
- Probe code: `novatrade/backtest/engine.py` (lines 92, 457, 879, 894, 901, 922, 947, 1038)
- Toggle plumbing: `novatrade/backtest/environment.py`, `novatrade/cli/config_schema.py`
- Regression test: `tests/test_backtest_engine.py::TestParityAuditToggleNoOp`
