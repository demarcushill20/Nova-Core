# Tier-3 Verified-Equivalent Findings (D4-D9, D11)

## Audit context
- **Audit doc target:** `docs/parity/exit-timing-audit.md` (Phase 10)
- **Source spec:** `docs/superpowers/specs/2026-04-28-exit-timing-parity-audit-design.md`
- **Worktree:** `pine-exit-timing-audit` at commit `d363fd1`
- **v5 config:** `configs/strategies/irb_v5_m5_pine_aligned.yaml`
- **Pine source:** `configs/pinescript/irb_v5_stag.pine`
- **Python source:** `novatrade/backtest/engine.py` (`_manage_position`)

Each entry walks the Pine source vs the Python source side-by-side, quotes the
load-bearing lines, and records a verdict. No code is modified by this phase —
this is a code-walk verification of hypothesised equivalences.

---

## D4 — `peak_fav` definition

### Pine
- `configs/pinescript/irb_v5_stag.pine:721` (long branch)
  ```pinescript
  peak_fav := math.max(nz(peak_fav, 0.0), high - strategy.position_avg_price)
  ```
- `configs/pinescript/irb_v5_stag.pine:765` (short branch)
  ```pinescript
  peak_fav := math.max(nz(peak_fav, 0.0), strategy.position_avg_price - low)
  ```
Pine uses **bar `high`** for the long peak excursion and **bar `low`** for the
short peak excursion, anchored on `strategy.position_avg_price`. Under v5 with
no partial exits (`partial_exit_enabled: false`), `strategy.position_avg_price`
is identically the original fill price for the life of the position, so it
collapses to `entry_price`.

### Python
- `novatrade/backtest/engine.py:906-912`
  ```python
  # --- Track peak favourable excursion (for stagnation guard) ---
  if pos.side == TradeSide.LONG:
      fav = bar.high - pos.entry_price
  else:
      fav = pos.entry_price - bar.low
  if fav > pos.peak_fav:
      pos.peak_fav = fav
  ```

### Verdict — EQUIVALENT
Both sides:
1. Use `bar.high` for longs / `bar.low` for shorts (wick-based, not close-based).
2. Anchor on the entry/avg fill price.
3. Use a monotonic max (Pine via `math.max(nz(…, 0.0), …)`, Python via the
   `if fav > pos.peak_fav` guard).
Because v5 has no partial exits, `strategy.position_avg_price` ≡ `entry_price`
throughout the position lifetime — there is no avg-price drift to reconcile.

---

## D5 — STAG adverse condition

### Pine
- `configs/pinescript/irb_v5_stag.pine:739` (long)
  ```pinescript
  if nz(peak_fav, 0.0) < STAG_ATR * entry_atr and close < strategy.position_avg_price
  ```
- `configs/pinescript/irb_v5_stag.pine:783` (short)
  ```pinescript
  if nz(peak_fav, 0.0) < STAG_ATR * entry_atr and close > strategy.position_avg_price
  ```
Adverse = bar `close` strictly **adverse vs avg fill** (long: `close < avg`;
short: `close > avg`).

### Python
- `novatrade/backtest/engine.py:918-921`
  ```python
  if self.env.use_stagnation_guard and pos.bars_held == self.env.stag_bars and pos.entry_atr > 0:
      stag_threshold = self.env.stag_atr_mult * pos.entry_atr
      adverse = bar.close < pos.entry_price if pos.side == TradeSide.LONG else bar.close > pos.entry_price
      if pos.peak_fav < stag_threshold and adverse:
  ```

### Verdict — EQUIVALENT
Both check `peak_fav < stag_threshold` AND a strict-adverse close vs the entry
fill (no `<=`). Under v5 the avg-price ≡ entry-price collapse from D4 makes
the comparator price identical. Threshold computation (`STAG_ATR * entry_atr`
in Pine vs `stag_atr_mult * pos.entry_atr` in Python) is the same product.

---

## D6 — STAG firing at exact `pos_bars == STAG_BARS`

### Pine
- `configs/pinescript/irb_v5_stag.pine:737`
  ```pinescript
  if USE_STAG and pos_bars == STAG_BARS and not na(entry_atr) and entry_atr > 0
  ```
- `configs/pinescript/irb_v5_stag.pine:781` (short branch — symmetric)

### Python
- `novatrade/backtest/engine.py:918`
  ```python
  if self.env.use_stagnation_guard and pos.bars_held == self.env.stag_bars and pos.entry_atr > 0:
  ```

### Verdict — EQUIVALENT
Both use **strict equality** on the bar counter (`==`), not `>=`. STAG can
therefore only fire on the exact stag-bar; missing it (e.g., because the bar
was already exited via stop or because the adverse-close test failed) means
STAG never fires for that position. `bars_held` in Python is incremented at
`engine.py:882` (`pos.bars_held = i - pos.entry_bar`) which mirrors Pine's
`pos_bars += 1` at line 549.

---

## D7 — TIME_STOP not gated on STAG

### Pine
- `configs/pinescript/irb_v5_stag.pine:749`
  ```pinescript
  if not evt_stag and pos_bars >= TIME_STOP
      strategy.close("Long", comment = "TIME_STOP")
  ```
Pine **does** explicitly gate TIME_STOP on `not evt_stag` so a single bar
can't fire both events.

### Python
- `novatrade/backtest/engine.py:946-952`
  ```python
  if pos.bars_held >= self.env.time_stop_bars:
      …
      self._close_position(i, bar.close, ExitReason.TIME_STOP)
      return
  ```
Python has **no `evt_stag` gate**, but the STAG branch (lines 918-927)
unconditionally `return`s after firing, so control flow can never reach the
TIME_STOP block on the same bar.

### Verdict — EQUIVALENT under current config — **CONFIG-INVARIANT FLAG**
With v5's `stag_bars: 12` and `time_stop_bars: 40`, the two checks fire on
disjoint bars (12 < 40) so the gating is moot. The `return` after STAG also
makes them mutually exclusive on any single bar regardless of config.

**Risk flag:** if a future config sets `stag_bars >= time_stop_bars` (e.g., a
sweep run with `stag_bars=40, time_stop_bars=40`), the bars_held condition
in Python would satisfy both checks on the same bar. STAG fires first and
returns, so Python still gates correctly **as long as STAG's adverse + peak
conditions are met**. If STAG's predicates fail (e.g., peak_fav above
threshold), Pine still fires TIME_STOP at `pos_bars >= TIME_STOP` and so does
Python — equivalent. The hidden divergence: in Pine `evt_stag` is a separate
boolean that can be `false` even when `pos_bars == STAG_BARS`, then TIME_STOP
fires on the same bar. Python via early-`return` after STAG **only** skips
TIME_STOP if STAG actually exited the position — so behaviour matches Pine.

This entry is therefore **truly config-invariant equivalent**, not just
"happens to work under v5". Promote to Tier-1 only if a future code change
moves STAG's `return` out from under its conditional.

---

## D8 — Breakeven & trail-delay paths

### Pine
- `grep -n -i "breakeven\|trail_delay" configs/pinescript/irb_v5_stag.pine` → **no matches**.
Pine v5 has neither a breakeven mechanism nor a trail-delay window. The only
exit machinery is: hard SL at `cur_stop`, EMA-trail ratchet, STAG, TIME_STOP.

### Python
- Breakeven, `novatrade/backtest/engine.py:955-971`
  ```python
  if self.env.breakeven_r > 0 and not pos.breakeven_hit:
      …
  ```
- Trail-delay, `novatrade/backtest/engine.py:974-975`
  ```python
  if self.env.trail_delay_bars > 0 and pos.bars_held < self.env.trail_delay_bars:
      return
  ```

### v5 config (`configs/strategies/irb_v5_m5_pine_aligned.yaml`)
- `breakeven_r: 0.0`
- `trail_delay_bars: 0`

### Verdict — EQUIVALENT (dead code under v5)
Both code paths are gated on a strictly-positive config value. With both
flags at `0.0`/`0`, neither block executes. The branches are vestigial v4
mechanics retained for backward compatibility with non-Pine-aligned configs;
they cannot perturb Pine parity under the v5 config.

---

## D9 — Partial-exit path

### Pine
- `grep -n -i "partial" configs/pinescript/irb_v5_stag.pine` → **no matches**.
Pine v5 takes one fill and closes the entire position on stop / STAG /
TIME_STOP / EMA-trail.

### Python
- `novatrade/backtest/engine.py:929-943`
  ```python
  if self.env.partial_exit_enabled and not pos.partial_taken:
      stop_distance = abs(pos.entry_price - pos.initial_stop)
      …
      self._partial_close_position(i, partial_target_price)
  ```
- `_partial_close_position` body at `novatrade/backtest/engine.py:1091+` moves
  the stop to breakeven after partial — but is unreachable when the gate is
  off.

### v5 config
- `partial_exit_enabled: false`

### Verdict — EQUIVALENT (dead code under v5)
The partial-exit block is gated on `self.env.partial_exit_enabled`. Under v5
this is `false`, so the entire branch (and the implicit BE-after-partial
side-effect at line 1120) never executes. Position size, exit count, and
exit pricing therefore all remain Pine-equivalent.

Note: this is also the reason the D4 "avg-price collapses to entry-price"
argument holds — partial exits are the only mechanism that would shift
`strategy.position_avg_price` away from `entry_price`, and they're disabled.

---

## D11 — Initial-stop init at entry bar

### Status
Pre-existing fix. Two commits established Pine fidelity here:
- `f602c45` — EMA-trail Pine-fidelity initial stop (override at fill time so
  the initial `current_stop` is set to `trail_ema` when EMA mode is on, with
  fallback to wick when EMA is NaN/disabled).
- `fc1eac3` — `fix(backtest): skip _manage_position on entry bar` — matches
  Pine's `strategy.exit` semantics that the trail/STAG/TIME stop machinery
  doesn't run on the same bar a position is opened.

### Regression sentinel
- `tests/test_backtest_engine.py:1599`
  ```python
  def test_initial_stop_uses_wick_when_trail_ema_disabled(self):
      """Live-champion regression guard: trail_ema_period=0 keeps wick stop."""
  ```
- Companion tests in the same class (`tests/test_backtest_engine.py:1618` and
  `:1635`+) cover the EMA-NaN fallback and the wrong-sided EMA case (Pine
  does NOT clamp; Python must not either).

### Verdict — EQUIVALENT (regression-protected)
The behaviour is locked by an executable sentinel test that fails if either
the wick-stop fallback or the unconditional EMA-takeover semantics regress.
No code change in this phase. If a future refactor reintroduces the
"clamp EMA stop to wick" bug, the sentinel and its three siblings catch it.

---

## Summary table

| ID  | Hypothesis | Verdict | Notes |
|-----|------------|---------|-------|
| D4  | `peak_fav` uses bar.high (long) / bar.low (short) | EQUIVALENT | avg-price ≡ entry-price under v5 (no partial) |
| D5  | STAG adverse condition is close-based | EQUIVALENT | strict adverse, no `<=` boundary diff |
| D6  | STAG fires at exact `pos_bars == STAG_BARS` | EQUIVALENT | strict equality both sides |
| D7  | TIME_STOP not gated on STAG event | EQUIVALENT (config-invariant) | Python gates via early `return` after STAG; matches Pine even if `stag_bars >= time_stop_bars` |
| D8  | Breakeven + trail-delay disabled in Pine | EQUIVALENT | dead code under v5 (`breakeven_r=0`, `trail_delay_bars=0`) |
| D9  | Partial-exit disabled in Pine | EQUIVALENT | dead code under v5 (`partial_exit_enabled: false`) |
| D11 | Initial stop set at entry bar to EMA / wick fallback | EQUIVALENT | regression-protected by `test_initial_stop_uses_wick_when_trail_ema_disabled` and 3 siblings |

All seven entries verified equivalent under the current `irb_v5_m5_pine_aligned`
config. None require code changes. The only forward-looking risk is D7's
config-invariance argument: it relies on the STAG block's early `return`
remaining structurally intact. Refactors to that block should preserve the
no-fall-through semantics or this entry must be re-classified.
