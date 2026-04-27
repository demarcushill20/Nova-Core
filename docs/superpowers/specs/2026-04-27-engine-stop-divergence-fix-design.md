# Engine-Stop-Divergence Fix — Design

**Date:** 2026-04-27
**Worktree:** `parity-engine-fixes` (off `feat/risk-reduction-0.33pct`)
**Author:** brainstorming session, Claude Opus 4.7
**Status:** awaiting operator review

## 1 — Problem

The Python `IRBBacktester` registers stop-out losses at ~2× the pip distance
of the certified Pine v5 baseline, even after closing every config-level and
filter-level parity gap and using fixed-1-lot sizing.

| Metric | Pine v5 | Python (Pine-aligned + fixed 1 lot) |
|---|---:|---:|
| Trades | 1,204 | 927 |
| Win rate | 19.4% | 18.4% |
| **avg_loss** | **~$76 (~7.6 pips)** | **$164 (~16 pips)** |
| avg_win | ~$394 (~39 pips) | $488 (~49 pips) |
| Profit factor | 1.078 | 0.675 |
| Net P&L | +$8,887 | −$40,292 |

Trade count and entry filtering are now within Pine tolerances; the residual
PF gap is driven entirely by losing trades booking ~2× more pips per stop-out
than Pine.

## 2 — Root cause

Pine v5 (`configs/pinescript/irb_v5_stag.pine`) uses **wick-based sizing
combined with EMA(40)-based stop placement**:

- Line 609: `float sp = low - PIP_BUF` is computed at IRB-bar close.
- Line 611: `float qv = f_qty(ep, sp)` uses `sp` for position sizing only.
- Line 729: `cur_stop := math.max(nz(cur_stop, ema_stop_long), ema_stop_long)`
  initializes the live trigger stop to `trail_ema` (the EMA-40 line) on the
  first bar in LONG state, then ratchets up.
- Line 733: `strategy.exit("Long Exit", "Long", stop = cur_stop)` — `sp` is
  never passed to the exit; the live stop is `cur_stop`.

The Python engine (`novatrade/backtest/engine.py`) instead initializes
`pos.current_stop = pos.stop_loss = bar.low - pip_buffer - spread_cushion`
(the IRB wick). The EMA-trail in `_manage_position` only ratchets up *from*
the wick; it never replaces the initial wick stop with the EMA value at
entry.

For a typical bullish IRB candle (range 10–20 pips):
- Pine's stop sits at the EMA(40) ~5–15 pips below entry.
- Python's stop sits at the wick ~12–22 pips below entry.

This is a structural difference, not a parameter mismatch.

## 3 — Decisions (locked in brainstorm)

| # | Decision | Operator choice |
|---|---|---|
| 1 | Fidelity goal | **A. Pine-faithful**: replicate wick-sized + EMA-stopped semantics exactly |
| 2 | Success bar | **A. Direction-only**: PF on 10yr Pine-aligned config crosses 1.0 |
| 3 | Scope | **A. EMA-stop only** (defer spread double-counting fix to follow-up) |
| 4 | Implementation | **Approach 1**: surgical override at fill time |
| 5 | Wrong-sided EMA | **No fallback** — match Pine literally; instant SL acceptable |
| 6 | Sizing anchor | **Wick stays at wick**: `pos.stop_loss` and `pos.initial_stop` unchanged |

## 4 — Architecture

Single-touch change inside `IRBBacktester` entry path. When
`env.trail_ema_period > 0` and `trail_ema[entry_bar]` is finite, override
`pos.current_stop` with the EMA value on position open. Sizing math
(`stop_distance_pips`), R-multiple math (`pos.initial_stop`), and downstream
trailing logic are all unchanged.

When `trail_ema_period == 0` (ATR-trail mode used by the live champion),
behavior is bit-identical to today.

## 5 — Components

| File | Change | LOC est. |
|---|---|---:|
| `novatrade/backtest/engine.py` | (a) Stash `self._cur_trail_ema = trail_ema[i] if trail_ema_period > 0 else NaN` in `_process_bar`. (b) After `self._position = _OpenPosition(...)` in `_check_pending_fill`, conditionally override `pos.current_stop`. | ~12 |
| `tests/test_backtest_engine.py` | 4 unit tests + 1 integration smoke (described in §8). | ~120 |

No changes to `BacktestEnvironment`, `StrategyConfig`, `metrics`, configs,
or any caller. The fix piggybacks on the existing `trail_ema_period` field;
no new config knob.

## 6 — Data flow

```
_process_bar(i, bar, atr_h1, ..., trail_ema)
    ├─ self._cur_atr = atr_h1[i] if not NaN else 0.0           (existing)
    ├─ self._cur_trail_ema = trail_ema[i] if env.trail_ema_period > 0
    │                       and i < len(trail_ema)
    │                       and not isnan(trail_ema[i])
    │                       else NaN                          ← NEW
    ├─ _check_pending_fill(i, bar)
    │      └─ on fill:
    │             pos = _OpenPosition(
    │                 current_stop=p.stop_loss,                (unchanged)
    │                 stop_loss=p.stop_loss,                   (unchanged)
    │                 initial_stop=p.stop_loss,                (unchanged)
    │                 entry_atr=getattr(self, "_cur_atr", 0.0),(unchanged)
    │                 ...,
    │             )
    │             ema_stop = getattr(self, "_cur_trail_ema", float("nan"))
    │             if env.trail_ema_period > 0 and not isnan(ema_stop):
    │                 pos.current_stop = ema_stop              ← key line
    │                 if (LONG and ema_stop >= fill_price) or
    │                    (SHORT and ema_stop <= fill_price):
    │                     log.debug("bar %d: EMA wrong-sided at entry", i)
    └─ _manage_position(i, bar, atr_h1, trail_ema)
            └─ ratchets pos.current_stop UP from EMA value     (existing)
```

`stop_loss` and `initial_stop` remain at the wick everywhere — they drive
sizing (`stop_distance_pips = abs(entry_price - stop_loss) / pip`) and
risk-R math (`pnl_pips / abs(entry_price - initial_stop)`), both of which
must stay Pine-faithful.

## 7 — Error handling

Three guard conditions, all silent fallbacks (no exceptions, no warnings —
these are routine):

| Condition | Behavior | Rationale |
|---|---|---|
| `env.trail_ema_period == 0` | Skip override; `current_stop` stays at wick. | Live-champion ATR-trail mode is unaffected. |
| `trail_ema[i]` is NaN (warmup) | Skip override; `current_stop` stays at wick. | Defensive against off-by-one warmup race; Pine's `warmup_ok` gate should already prevent this in practice. |
| `_cur_trail_ema` attribute missing | `getattr(..., float("nan"))` flows into NaN guard. | Belt-and-braces for any future ordering change in `_process_bar`. |

One `log.debug` when EMA is on the unfavorable side at entry (long with
`ema ≥ fill_price`, or short with `ema ≤ fill_price`). Helps grep
postmortems; no behavior change. Pine takes these as instant-SL trades and
so does Python.

## 8 — Testing

### Unit (4) — `tests/test_backtest_engine.py`

1. **`test_initial_stop_uses_trail_ema_when_enabled`**
   Build a small synthetic candle series with trail_ema_period=40, drive the
   engine to a LONG fill. Assert:
   - `pos.current_stop == trail_ema_at_entry_bar` (not `bar.low - pip_buffer`)
   - `pos.stop_loss == bar.low - pip_buffer - spread_cushion` (wick preserved)
   - `pos.initial_stop == pos.stop_loss` (sizing anchor preserved)

2. **`test_initial_stop_uses_wick_when_trail_ema_disabled`**
   Same setup, `trail_ema_period=0`. Assert
   `pos.current_stop == pos.stop_loss == wick − buffer`. Regression guard
   for live-champion behavior.

3. **`test_initial_stop_falls_back_to_wick_during_ema_warmup`**
   Fill bar at index 1 (before EMA(40) has enough history). Assert
   `pos.current_stop` falls back to wick (NaN guard active).

4. **`test_initial_stop_unconditional_when_ema_wrong_sided`**
   Synthetic LONG entry with `trail_ema > fill_price`. Assert
   `pos.current_stop == trail_ema` (no clamping). Step engine forward one
   bar; assert position stops out at the EMA price (`ExitReason.STOP_LOSS`,
   `exit_price ≈ trail_ema_at_entry`).

### Integration (1) — `tests/test_backtest_engine.py` (new test class)

5. **`test_pine_aligned_2022_pf_crosses_one`**
   Loads the 2022 EURUSD H1+H4 slice from `data/candles/EURUSD_H1_10yr.csv`,
   runs the engine with `configs/strategies/irb_v5_m5_pine_aligned.yaml`.
   Asserts:
   - PF ≥ 1.0 (the headline success criterion; Pine reports PF 2.06 for 2022).
   - Trade count within ±20% of Pine's 108.
   - No `ExitReason.STOP_LOSS` trade has `pnl_pips < -50` (sanity guard).

   **Data-availability gated**: `data/` is gitignored (`/data/` line 42 of
   `.gitignore`), so CI doesn't have the CSV. The test uses
   `pytest.importorskip`-style guard:

   ```python
   @pytest.mark.skipif(
       not (Path("data/candles/EURUSD_H1_10yr.csv").exists()),
       reason="10yr historical data not available (gitignored under /data/)"
   )
   ```

   Runs in ~3 seconds on the 1-year window when data is local. Acts as a
   developer-machine smoke + a regression guard if the CSV is later moved
   to a tracked fixtures directory.

### Manual smoke (the actual merge gate)

Because the integration test is data-gated, the **operator-run manual smoke
is the de-facto pre-merge gate**, not CI:

`scripts/parity_fixed_sizing.py` rerun on the operator's machine before
merge. All five sizing scenarios should show PF improvements toward Pine,
and at least one fixed-sizing scenario should reach PF ≥ 1.0. This is
called out explicitly in §11 as a required validation gate.

## 9 — Out of scope (deferred to follow-ups)

- Spread double-counting fix (env.py:228-230). Estimated ~10% of remaining gap.
- Per-trade matching tool for tighter parity validation (Question 2 Option B).
- `from_champion_config` symmetry for any field added in §5 (none in this fix).
- Re-running the live champion with EMA-trail enabled. Live champion stays on
  ATR-trail; switching is a separate strategy-design decision, not a parity fix.

## 10 — Risk & rollback

| Risk | Mitigation |
|---|---|
| Live trading affected | Live champion uses `trail_atr_multiplier > 0` and `trail_ema_period == 0`; behavior bit-identical. Verified by unit test #2. |
| Backtest history drift | Anyone running historical backtests with `trail_ema_period > 0` will see different numbers. Communicate via PR description. The only such config in-repo is `irb_v5_m5_pine_aligned.yaml`, which is parity-test only. |
| Headline PF doesn't cross 1.0 | Integration test #5 fails fast in CI. Roll forward by triggering follow-up brainstorm on spread double-counting (next-largest contributor). |
| Test data unavailable in CI | `data/candles/EURUSD_H1_10yr.csv` is checked in (763k bars, 2.6 MB on H1 only). Slice for 2022 in-memory. |

Rollback path: revert the single commit. No schema, no config, no migration.

## 11 — Validation gates before merge

1. All 325 backtest tests green (CI).
2. New unit tests added and green (CI).
3. Integration test #5 either (a) green when data available locally, or
   (b) skipped with reason recorded — operator confirms skip is data-only.
4. **Manual smoke (required)**: operator runs `scripts/parity_fixed_sizing.py`
   on local machine; PF ≥ 1.0 reached on at least one fixed-sizing scenario.
5. `dual-code-review` (Codex + Opus) on the engine diff.
6. Operator approval.

## 12 — Open questions

None remaining for this scope. Spread double-counting is acknowledged as a
known engine limitation (env.py:228-230) and tracked for a follow-up design.
