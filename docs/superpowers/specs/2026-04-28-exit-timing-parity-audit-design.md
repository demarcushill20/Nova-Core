# Pine v5 Exit-Timing Parity Audit — Design Spec

**Date:** 2026-04-28
**Author:** Claude (Opus 4.7) via brainstorming skill
**Status:** approved (single-gate, autonomous-mode brainstorm)
**Successor:** `/write-plan` produces audit execution tasks; fix work is a separate brainstorm.

## Goal

Identify, characterize, and rank-by-impact every divergence between Python's `IRBBacktester` exit logic and Pine v5's S_LONG/S_SHORT exit blocks (`configs/pinescript/irb_v5_stag.pine:717–795`). Audit-only deliverable; fixes ship in a separate plan.

## Non-goals

- Fixing divergences in this plan. Handoff to a fresh `/brainstorm` once audit is approved.
- Touching live champion behavior. Live config (`trail_ema_period: 0`, `mtf_lookback: 1`) must remain bit-identical, enforced by a regression test.
- Re-litigating entry-side filters or already-shipped fixes (`f602c45`, `fc1eac3`, `23adfe3`).

## Context

Post entry-side fixes, Python is at PF 0.888, 56.6% coverage, 1,066 trades vs Pine 1,204 / PF 1.078. Remaining gap is dominated by exit-side timing (~50% of 906 mismatches): 343 pine_only entries reject on `existing_position` at IRB bar; 70 python_only Bucket E entries (Pine in-position when Python re-enters); 233 state-machine residue. Existing diagnostic harness: `scripts/parity_match.py`, `scripts/parity_python_only_split.py`, `scripts/parity_irb_at_irb_bar.py`. Pine baseline trade log: `data/irb_novatrade_irb_v5_results_extracted.csv`.

Code-walk surfaces the structural sequencing difference:

**Pine S_LONG block (lines 717–751) — order per H1 close:**
1. `ema_stop_long = trail_ema` (current bar's close-based EMA)
2. `cur_stop := math.max(nz(cur_stop, ema_stop_long), ema_stop_long)` — ratchet
3. `strategy.exit(stop = cur_stop)` — registered, fires intra-bar against this bar's high/low
4. STAG check at `pos_bars == STAG_BARS` (sets `evt_stag` on close)
5. TIME_STOP check at `pos_bars >= TIME_STOP`, gated on `not evt_stag`

**Python `_manage_position` (lines 842–988) — order per H1 bar:**
1. STOP_LOSS check (line 866–872) — uses `pos.current_stop` carried over from prior bar
2. peak_fav update
3. STAG check (line 887–891)
4. TIME_STOP check (line 911–912) — *not gated on STAG firing this bar*
5. EMA-trail ratchet (line 939–988) — and may fire `TRAILING_STOP` on same bar after ratchet

## Decisions (resolved autonomously, ratified by operator)

### D-A. Audit method — empirical probes (Q2 = B)

Static code-walk plus toggle-driven empirical probes. Each hypothesized divergence gets a parity-audit toggle (`env.parity_audit_toggles: frozenset[str]`) that flips the suspect behavior. Probes are diagnostic, not fixes; they remain in the codebase post-audit gated off, available for the fix-plan TDD cycle and future regressions.

**Reasoning:** Static-only ends with hypothesized impact and the fix plan re-instruments anyway — work done twice. Pine-log-export-and-diff is the gold standard but blocks on operator action. Empirical probes give ranked impact data immediately.

**Trade-offs considered:**
- Static-only — faster but no impact ranking; rejected.
- Probes-only-removed-post-audit — cleaner codebase but loses TDD scaffolding for the fix plan; rejected.
- Re-run Pine in TV with logs — most thorough but blocks on TV session; reserved as fallback if probes are inconclusive.

### D-B. Scope & ranking — tiered (Q3 = C)

- **Tier-1** — divergences that resolve ≥10 mismatches when toggled. Fix plan acts on these.
- **Tier-2** — 1–9 mismatches. Logged as known-residue, fix plan considers but doesn't prioritize.
- **Tier-3** — verified-equivalent under v5 config. Documented for institutional memory ("we checked this, here's why it's a no-op").

### D-C. Deliverable format — repo doc primary + vault summary + memory pointer (Q4 = C)

**Locations:**
- Primary: `docs/parity/exit-timing-audit.md` — full findings, all tiers, schema below.
- Vault: `Engineering/parity/Pine v5 Exit-Timing Audit` — Tier-1 summary + link to repo doc.
- Memory: update `MEMORY/.../project_pine_parity_state.md` with audit-doc pointer.

**Reasoning:** Engineering-grade data with line refs and toggle results lives best alongside code in git where line refs stay valid. Vault provides cross-device operator surfacing without duplicating data. Memory pointer keeps cross-session context warm.

**Per-finding schema:**

```
### D{N} — {Title}                                           [Tier-1|2|3]

**Pine source:** `configs/pinescript/irb_v5_stag.pine:LINE` — code excerpt
**Python source:** `novatrade/backtest/engine.py:LINE` — code excerpt
**Divergence:** {mechanism, 1-3 sentences}
**Hypothesized impact:** {direction + which mismatch bucket}
**Probe:** {Tier-1/2 — toggle name + behavior change}
**Measured impact:** {Tier-1/2 — pine_only resolved, python_only resolved, ΔPF, Δtrades}
**Status:** documented | queued-for-fix | fixed-in-{commit}
**Notes:** edge cases, dependencies on other findings
```

### D-D. Success criteria

The audit is complete when:

- [ ] Toggle scaffold lands with passing live-regression test.
- [ ] Every D1–D12 candidate has a finding entry with a tier and either a probe result (Tier-1/2) or a code-walk verification (Tier-3).
- [ ] At least one Tier-1 finding has been identified and ranked by measured impact. Failure mode if zero Tier-1 found: audit found no leverage; revisit hypothesis or escalate to TV-log capture (Risk #3).
- [ ] `docs/parity/exit-timing-audit.md` committed.
- [ ] Vault summary written; auto-memory updated.
- [ ] Live champion regression test green (no live behavior change).

The audit's value is *ranked impact data + a fix-plan map*, not "all divergences fixed." Success is having the priorities right going into the fix plan, with the scaffold in place to TDD each fix.

## Candidate divergence inventory

Tier assignment is hypothesized; the audit run measures actual impact and may re-tier.

| ID | Title | Hypothesis | Tier (hyp) |
|---|---|---|---|
| D1 | Stop fill price on ratchet bar (pre- vs post-ratchet) | Python fills at pre-ratchet stop; Pine at post-ratchet. Affects pnl on stop-out bars where EMA tightened. | Tier-2 |
| D2 | STAG/TIME_STOP exit timing & price (current-bar-close vs next-bar-open) | Python exits at bar i close; Pine `strategy.close` fills at bar i+1 open. Pine stays in-position 1 bar longer → Bucket E. | **Tier-1** |
| D3 | Cooldown after exit | Python `cooldown_bars: 1` blocks bar i+1 re-entry; Pine `state := S_FLAT` allows immediate re-entry. | **Tier-1 (likely)** |
| D4 | peak_fav definition | Both use high-based; verify. | Tier-3 |
| D5 | STAG adverse condition | Both use close vs entry/avg; verify (no partial in v5). | Tier-3 |
| D6 | STAG firing at exact `pos_bars == STAG_BARS` | Both use exact equality; verify. | Tier-3 |
| D7 | TIME_STOP not-gated-on-STAG | Matters only if `stag_bars ≥ time_stop_bars`; current 12 vs 40. | Tier-3 |
| D8 | Breakeven & trail-delay paths | Disabled in v5 config; verify dead-code under env values. | Tier-3 |
| D9 | Partial-exit path | Disabled in v5 config; verify dead-code. | Tier-3 |
| D10 | Same-bar TRAILING_STOP fire on ratchet | Both engines fire same-bar; verify equivalence including the `bar.low > old_stop` strict-inequality edge. | Tier-2/3 |
| D11 | Initial-stop init at entry bar | Already fixed (`f602c45`/`fc1eac3`); regression-check only. | Tier-3 |
| D12 | strategy.exit fill price on gap-through | Pine fills at stop level (limit-style); Python may fill at `current_stop`. Bar gaps below stop → divergence. | Tier-2 |

D2 is hypothesized to be the dominant explainer for Bucket E (~70 entries). D3 is hypothesized to contribute to pine_only `existing_position` residue. Probes confirm or refute.

## Probe scaffold design

**Mechanism:** add `env.parity_audit_toggles: frozenset[str]` (default empty). Each toggle is a string label gating one branch in `_manage_position` / `_close_position`. Examples:

- `"d1_post_ratchet_stop_fill"` — reorder stop-check after ratchet for D1 measurement.
- `"d2_strategy_close_next_open"` — defer STAG/TIME_STOP exit by 1 bar, fill at next-bar open.
- `"d3_zero_cooldown"` — bypass `cooldown_bars` enforcement.

Toggle activation lives only in the parity-test harness (`scripts/parity_audit.py`); production configs never set it.

**Live-safety guarantee (regression test):**

```python
def test_parity_audit_toggle_default_is_no_op():
    """Live config must produce bit-identical results when parity_audit_toggles is empty."""
    env_default = build_env(...)  # parity_audit_toggles=frozenset()
    result = run_backtest(env_default, sample_window)
    assert result == golden_baseline  # exact equality on trade list, pnl, equity curve
```

Same shape as the existing `test_initial_stop_uses_wick_when_trail_ema_disabled` regression test that protects live from `trail_ema_period`-related changes.

## Audit execution flow (handed to /write-plan)

1. **Toggle scaffold** — add `env.parity_audit_toggles` field + plumbing through `Environment` / `config_schema`. Live regression test (assertion: empty toggle set ≡ current behavior).
2. **Probe per Tier-1/2 candidate (D1, D2, D3, D10, D12)** — implement each toggle, write measurement script, capture: `(pine_only_resolved, python_only_resolved, ΔPF, Δtrades)`.
3. **Verify Tier-3 candidates (D4–D9, D11)** — code-walk verification with line-refs; no toggle needed.
4. **Compose findings doc** at `docs/parity/exit-timing-audit.md` using schema above.
5. **Re-tier based on measured impact.** A Tier-2 hypothesis that resolves 50 mismatches gets promoted; a Tier-1 hypothesis that resolves 0 gets demoted.
6. **Write vault summary** + update memory pointer.
7. **Queue fix-plan brainstorm** — leave as a written next-action, do not invoke.

## Risks & mitigations

1. **Probe interactions** — fixing D2 might reduce D3's impact (cascade). Mitigation: measure each toggle in isolation AND in pairs for the top candidates; document interaction effects.
2. **Toggle scaffold leaks to live** — mitigated by the live-regression test gate (must run on every CI), and by config-schema validation rejecting `parity_audit_toggles` in live config files.
3. **Hypothesized Tier-1 misses real Tier-1** — possible if the dominant divergence isn't on the candidate list. Mitigation: after measured impact lands, sum resolved mismatches; if total < 50% of remaining 906, run an additional bar-level diff probe (Pine TV log capture fallback).
4. **Pine baseline data drift** — if `irb_novatrade_irb_v5_results_extracted.csv` is regenerated mid-audit with different parameters, results invalidate. Mitigation: pin baseline file SHA in the audit doc.

## Out of scope (explicit)

- Entry-side filters (covered by prior audits).
- Live champion config changes.
- M5 cadence work (Pine baseline confirmed H1).
- Any `partial_exit` / `breakeven` / `trail_delay` work (dead in v5).
- Pine TV log re-capture (fallback only if Risk #3 triggers).

## Handoff

After this design is committed and self-reviewed, `/write-plan` produces the bite-sized implementation tasks for the audit execution flow above.
